#!/usr/bin/env python3
"""
Create and start unattended VirtualBox VMs from YAML profiles.

VirtualBox 7.2.x / Linux host / vboxapi (XPCOM).

The script:
  1. Prompts for the VM name and guest credentials.
  2. Creates and registers the VM.
  3. Applies hardware, storage, networking, and install settings from YAML.
  4. Builds unattended install media for the selected OS profile.
  5. Starts the VM so the OS installs without normal setup interaction.

Credentials are intentionally NOT stored in vm.yaml.
"""

from __future__ import annotations

import argparse
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
from getpass import getpass
from pathlib import Path

try:
    import yaml
except ImportError:
    print(
        "ERROR: PyYAML is missing.\n"
        "Install it with:\n"
        "  sudo apt install python3-yaml",
        file=sys.stderr,
    )
    raise SystemExit(1)

try:
    from vboxapi import VirtualBoxManager
except ImportError as exc:
    VirtualBoxManager = None
    VBOXAPI_IMPORT_ERROR = exc
else:
    VBOXAPI_IMPORT_ERROR = None


MIB = 1024 * 1024
GIB = 1024 * MIB
PRODUCT_KEY_RE = re.compile(r"^[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}$")
INVALID_VM_NAME_RE = re.compile(r"[/\x00]|[\x01-\x1f]")


def load_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)

    if not isinstance(data, dict):
        raise ValueError("The YAML root must be a mapping.")
    return data


def get_required(cfg: dict, *keys):
    cur = cfg
    for key in keys:
        if not isinstance(cur, dict) or key not in cur:
            raise KeyError("Missing required setting: " + ".".join(keys))
        cur = cur[key]
    return cur


def resolve_config_path(value: str, config_path: Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = config_path.parent / path
    return path.resolve()


def validate_vm_name(name: str) -> str:
    name = name.strip()
    if not name:
        raise ValueError("VM name cannot be empty.")
    if INVALID_VM_NAME_RE.search(name):
        raise ValueError(
            "VM name cannot contain slashes, NUL bytes, or control characters."
        )
    return name


def get_vm_name(default_name: str) -> str:
    env_name = os.environ.get("VBOX_VM_NAME")
    if env_name:
        return validate_vm_name(env_name)

    prompt = f"VM name [{default_name}]: "
    while True:
        try:
            entered = input(prompt)
        except EOFError:
            return validate_vm_name(default_name)

        try:
            return validate_vm_name(entered or default_name)
        except ValueError as exc:
            print(exc)


def normalize_product_key(value: str) -> str:
    compact = re.sub(r"[^A-Za-z0-9]", "", value).upper()
    if len(compact) != 25:
        raise RuntimeError(
            "Windows product key must contain 25 letters/numbers."
        )

    product_key = "-".join(
        compact[i:i + 5] for i in range(0, len(compact), 5)
    )
    if not PRODUCT_KEY_RE.fullmatch(product_key):
        raise RuntimeError("Windows product key format is invalid.")

    return product_key


def get_install_credentials():
    while True:
        password = getpass("Windows VM password: ")
        confirm = getpass("Confirm Windows VM password: ")

        if not password:
            print("Password cannot be empty.")
            continue

        if password != confirm:
            print("Passwords do not match. Try again.")
            continue

        break

    product_key = input("Windows product key (blank to skip): ").strip()

    if not product_key:
        return password, ""

    return password, normalize_product_key(product_key)


def get_guest_password(label: str = "Guest OS"):
    while True:
        password = getpass(f"{label} password: ")
        confirm = getpass(f"Confirm {label} password: ")

        if not password:
            print("Password cannot be empty.")
            continue

        if "\n" in password or "\r" in password:
            print("Password cannot contain newline characters.")
            continue

        if password != confirm:
            print("Passwords do not match. Try again.")
            continue

        return password


def preseed_value(value) -> str:
    text = str(value)
    if "\n" in text or "\r" in text:
        raise ValueError("Preseed values cannot contain newline characters.")
    return text


def locale_with_encoding(locale: str) -> str:
    locale = locale.strip()
    if "." in locale:
        return locale
    return f"{locale}.UTF-8"


def language_from_locale(locale: str) -> str:
    return locale.split("_", 1)[0].split(".", 1)[0] or "en"


def sha512_password_hash(password: str) -> str:
    salt = secrets.token_urlsafe(12).replace("-", ".").replace("_", ".")
    result = subprocess.run(
        ["openssl", "passwd", "-6", "-salt", salt, "-stdin"],
        input=password,
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout.strip()


def is_not_implemented_error(exc: Exception) -> bool:
    text = f"{exc!r} {exc}"
    return "0x80004001" in text or "-2147467263" in text


def set_optional_unattended_property(unattended, name: str, value):
    try:
        setattr(unattended, name, value)
    except Exception as exc:
        if not is_not_implemented_error(exc):
            raise
        print(
            f"VirtualBox does not implement unattended.{name}; "
            "continuing without that optional setting.",
            file=sys.stderr,
        )


def force_unattended_no_ui(answer_file: Path):
    if not answer_file.is_file():
        raise FileNotFoundError(
            f"VirtualBox did not create the answer file: {answer_file}"
        )

    text = answer_file.read_text(encoding="utf-8")
    text = text.replace(
        "<WillShowUI>OnError</WillShowUI>",
        "<WillShowUI>Never</WillShowUI>",
    )
    answer_file.write_text(text, encoding="utf-8")


def find_unattended_answer_file(machine) -> Path:
    vm_folder = Path(machine.settingsFilePath).parent
    expected = vm_folder / f"Unattended-{machine.id}-autounattend.xml"
    if expected.is_file():
        return expected

    matches = sorted(vm_folder.glob("Unattended-*-autounattend.xml"))
    if len(matches) == 1:
        return matches[0]

    raise FileNotFoundError(
        f"Could not find generated autounattend.xml in {vm_folder}"
    )


def progress_wait(progress, action: str):
    progress.waitForCompletion(-1)
    if progress.resultCode != 0:
        info = getattr(progress, "errorInfo", None)
        detail = getattr(info, "text", "") if info else ""
        if detail:
            detail = f": {detail}"
        raise RuntimeError(
            f"{action} failed with result code {progress.resultCode}{detail}"
        )


def run_vboxmanage(args: list[str], *, capture: bool = False):
    cmd = ["VBoxManage", *args]
    result = subprocess.run(
        cmd,
        check=True,
        text=True,
        stdout=subprocess.PIPE if capture else None,
        stderr=subprocess.PIPE if capture else None,
    )
    return result.stdout if capture else ""


def env_enabled(name: str) -> bool:
    return os.environ.get(name, "").lower() in {"1", "true", "yes", "on"}


def confirm_overwrite(name: str, detail: str) -> bool:
    if env_enabled("VBOX_OVERWRITE"):
        return True

    print(detail)
    answer = input(
        f"Delete existing VM/files for {name!r} and recreate it? "
        "Type yes to continue: "
    )
    return answer.strip().lower() == "yes"


def write_created_vm_info(name: str, frontend: str, settings_file: Path):
    info_file = os.environ.get("VBOX_CREATED_VM_INFO_FILE")
    if not info_file:
        return

    Path(info_file).write_text(
        f"{name}\n{frontend}\n{settings_file}\n",
        encoding="utf-8",
    )


def vboxmanage_vm_exists(name: str) -> bool:
    result = subprocess.run(
        ["VBoxManage", "showvminfo", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0


def vboxmanage_vm_state(name: str) -> str | None:
    try:
        output = run_vboxmanage(
            ["showvminfo", name, "--machinereadable"],
            capture=True,
        )
    except subprocess.CalledProcessError:
        return None

    for line in output.splitlines():
        if line.startswith("VMState="):
            return line.split("=", 1)[1].strip().strip('"')
    return None


def delete_existing_vboxmanage_vm(name: str, machine_folder: Path):
    if vboxmanage_vm_exists(name):
        state = vboxmanage_vm_state(name)
        if state in {"running", "paused", "stuck"}:
            raise RuntimeError(
                f"VM {name!r} is {state}. Power it off before overwriting."
            )

        print(f"Deleting registered VM {name!r}...")
        run_vboxmanage(["unregistervm", name, "--delete"])

    if machine_folder.exists():
        print(f"Removing existing VM folder: {machine_folder}")
        shutil.rmtree(machine_folder)


def close_vboxmanage_hdds_by_location(path: Path):
    path = path.expanduser().resolve()
    output = run_vboxmanage(["list", "hdds", "--long"], capture=True)
    current_uuid = None
    current_location = None

    def close_current():
        if current_uuid and current_location == str(path):
            print(f"Removing stale medium registry entry: {current_uuid}")
            run_vboxmanage(["closemedium", "disk", current_uuid])

    for line in output.splitlines():
        if line.startswith("UUID:"):
            close_current()
            current_uuid = line.split(":", 1)[1].strip()
            current_location = None
        elif line.startswith("Location:"):
            current_location = line.split(":", 1)[1].strip()

    close_current()


def find_auxiliary_unattended_iso(machine_folder: Path) -> Path:
    matches = sorted(machine_folder.glob("Unattended-*-aux-iso.viso"))
    if not matches:
        raise FileNotFoundError(
            f"Could not find generated unattended auxiliary ISO in {machine_folder}"
        )
    return matches[-1]


def ensure_vboxmanage_install_media(name: str, machine_folder: Path, iso_path: Path):
    aux_iso = find_auxiliary_unattended_iso(machine_folder)

    run_vboxmanage([
        "storagectl",
        name,
        "--name=SATA",
        "--portcount",
        "3",
    ])

    for port in ("1", "2"):
        subprocess.run(
            [
                "VBoxManage",
                "storageattach",
                name,
                "--storagectl=SATA",
                f"--port={port}",
                "--device=0",
                "--type=dvddrive",
                "--medium=none",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

    run_vboxmanage([
        "storageattach",
        name,
        "--storagectl=SATA",
        "--port=1",
        "--device=0",
        "--type=dvddrive",
        f"--medium={iso_path}",
    ])
    run_vboxmanage([
        "storageattach",
        name,
        "--storagectl=SATA",
        "--port=2",
        "--device=0",
        "--type=dvddrive",
        f"--medium={aux_iso}",
    ])
    run_vboxmanage([
        "modifyvm",
        name,
        "--boot1=dvd",
        "--boot2=disk",
        "--boot3=none",
        "--boot4=none",
    ])


def render_kali_preseed(cfg: dict, password_hash: str) -> str:
    unattended = get_required(cfg, "unattended")
    username = preseed_value(unattended.get("username", "rob"))
    full_name = preseed_value(unattended.get("full_name", username))
    hostname = preseed_value(unattended.get("hostname", "kali-workstation"))
    timezone = preseed_value(unattended.get("timezone", "US/Eastern"))
    locale = preseed_value(unattended.get("locale", "en_US"))
    locale_encoded = preseed_value(locale_with_encoding(locale))
    keyboard = preseed_value(unattended.get("keyboard_layout", "us"))
    country = preseed_value(unattended.get("country", "US"))
    password_hash = preseed_value(password_hash)

    return f"""# Generated by create_virtualbox_vm.py. Do not store secrets here.
d-i debian-installer/language string en
d-i debian-installer/locale string {locale_encoded}
d-i debian-installer/country string {country}
d-i localechooser/supported-locales multiselect {locale_encoded}
d-i keyboard-configuration/xkb-keymap select {keyboard}
d-i keyboard-configuration/layoutcode string {keyboard}
d-i console-setup/ask_detect boolean false
d-i netcfg/choose_interface select auto
d-i netcfg/get_hostname string {hostname}
d-i netcfg/get_domain string local
d-i hw-detect/load_firmware boolean true

d-i mirror/country string enter information manually
d-i mirror/http/hostname string http.kali.org
d-i mirror/http/directory string /kali
d-i mirror/http/proxy string
d-i apt-setup/use_mirror boolean false
d-i apt-setup/services-select multiselect
d-i apt-setup/contrib boolean true
d-i apt-setup/non-free boolean true
d-i apt-setup/non-free-firmware boolean true
d-i apt-setup/disable-cdrom-entries boolean true
d-i apt-setup/enable-source-repositories boolean false

d-i passwd/root-login boolean false
d-i passwd/make-user boolean true
d-i passwd/user-fullname string {full_name}
d-i passwd/username string {username}
d-i passwd/user-password-crypted password {password_hash}
d-i user-setup/allow-password-weak boolean true

d-i clock-setup/utc boolean true
d-i time/zone string {timezone}
d-i clock-setup/ntp boolean true

d-i partman-auto/method string regular
d-i partman-auto/disk string /dev/sda
d-i partman/default_filesystem string ext4
d-i partman-lvm/device_remove_lvm boolean true
d-i partman-md/device_remove_md boolean true
d-i partman-auto/choose_recipe select atomic
d-i partman-basicfilesystems/no_swap boolean false
d-i partman-partitioning/confirm_write_new_label boolean true
d-i partman/choose_partition select finish
d-i partman/confirm boolean true
d-i partman/confirm_nooverwrite boolean true
d-i partman-auto/confirm boolean true

d-i pkgsel/upgrade select none
d-i pkgsel/update-policy select none
popularity-contest popularity-contest/participate boolean false

d-i grub-installer/only_debian boolean true
d-i grub-installer/with_other_os boolean true
d-i grub-installer/bootdev string default

d-i cdrom-detect/eject boolean true
d-i finish-install/reboot_in_progress note

console-setup console-setup/charmap47 select UTF-8
samba-common samba-common/dhcp boolean false
encfs encfs/security-information boolean true
encfs encfs/security-information seen true
macchanger macchanger/automatically_run boolean false
wireshark-common wireshark-common/install-setuid boolean true
"""


def kali_boot_append(
    preseed_path: str,
    desktop: str,
    locale: str,
    country: str,
    keyboard: str,
) -> str:
    boot_locale = locale_with_encoding(locale)
    language = language_from_locale(locale)
    return (
        "net.ifnames=0 "
        "auto=true priority=critical "
        f"language={language} country={country} locale={boot_locale} "
        f"keyboard-configuration/xkb-keymap={keyboard} "
        "console-setup/ask_detect=false "
        f"preseed/file={preseed_path} "
        f"file={preseed_path} "
        "simple-cdd/profiles=kali,offline "
        f"desktop={desktop} "
        "vga=788 "
        "initrd=/install.amd/initrd.gz --- quiet"
    )


def write_kali_boot_configs(
    work_dir: Path,
    desktop: str,
    locale: str,
    country: str,
    keyboard: str,
):
    append = kali_boot_append(
        "/cdrom/simple-cdd/default.preseed",
        desktop,
        locale=locale,
        country=country,
        keyboard=keyboard,
    )
    gtk_append = append.replace(
        "initrd=/install.amd/initrd.gz",
        "initrd=/install.amd/gtk/initrd.gz",
    )

    (work_dir / "txt.cfg").write_text(
        "default install\n"
        "label install\n"
        "\tmenu label ^Automated install\n"
        "\tmenu default\n"
        "\tkernel /install.amd/vmlinuz\n"
        f"\tappend {append}\n",
        encoding="utf-8",
    )
    (work_dir / "gtk.cfg").write_text(
        "default installgui\n"
        "label installgui\n"
        "\tmenu label ^Automated graphical install\n"
        "\tmenu default\n"
        "\tkernel /install.amd/vmlinuz\n"
        f"\tappend {gtk_append}\n",
        encoding="utf-8",
    )
    (work_dir / "isolinux.cfg").write_text(
        "default install\n"
        "prompt 0\n"
        "timeout 1\n"
        "label install\n"
        "\tkernel /install.amd/vmlinuz\n"
        f"\tappend {append}\n",
        encoding="utf-8",
    )
    (work_dir / "grub.cfg").write_text(
        "set default=0\n"
        "set timeout=1\n"
        "menuentry 'Automated graphical install' {\n"
        "    set background_color=black\n"
        "    linux /install.amd/vmlinuz "
        f"{gtk_append.replace('initrd=/install.amd/gtk/initrd.gz', '')}\n"
        "    initrd /install.amd/gtk/initrd.gz\n"
        "}\n"
        "menuentry 'Automated install' {\n"
        "    set background_color=black\n"
        "    linux /install.amd/vmlinuz "
        f"{append.replace('initrd=/install.amd/initrd.gz', '')}\n"
        "    initrd /install.amd/initrd.gz\n"
        "}\n",
        encoding="utf-8",
    )


def build_kali_autoinstall_iso(
    source_iso: Path,
    machine_folder: Path,
    cfg: dict,
    password: str,
) -> Path:
    if not shutil.which("xorriso"):
        raise RuntimeError(
            "xorriso is required to build the Kali autoinstall ISO."
        )

    installation = get_required(cfg, "installation")
    unattended = get_required(cfg, "unattended")
    desktop = str(installation.get("desktop", "xfce"))
    locale = str(unattended.get("locale", "en_US"))
    country = str(unattended.get("country", "US"))
    keyboard = str(unattended.get("keyboard_layout", "us"))
    work_dir = machine_folder / "autoinstall"
    work_dir.mkdir(parents=True, exist_ok=True)

    preseed_path = work_dir / "default.preseed"
    output_iso = machine_folder / "kali-autoinstall.iso"

    preseed_path.write_text(
        render_kali_preseed(cfg, sha512_password_hash(password)),
        encoding="utf-8",
    )
    write_kali_boot_configs(work_dir, desktop, locale, country, keyboard)

    if output_iso.exists():
        output_iso.unlink()

    print("Building Kali autoinstall ISO...")
    run_vboxmanage(["--version"], capture=True)
    subprocess.run(
        [
            "xorriso",
            "-indev",
            str(source_iso),
            "-outdev",
            str(output_iso),
            "-boot_image",
            "any",
            "replay",
            "-map",
            str(preseed_path),
            "/simple-cdd/default.preseed",
            "-map",
            str(work_dir / "isolinux.cfg"),
            "/isolinux/isolinux.cfg",
            "-map",
            str(work_dir / "txt.cfg"),
            "/isolinux/txt.cfg",
            "-map",
            str(work_dir / "gtk.cfg"),
            "/isolinux/gtk.cfg",
            "-map",
            str(work_dir / "grub.cfg"),
            "/boot/grub/grub.cfg",
        ],
        check=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return output_iso


def prepare_vboxmanage_target(name: str, machine_folder: Path):
    vm_exists = vboxmanage_vm_exists(name)
    folder_exists = machine_folder.exists()

    if not vm_exists and not folder_exists:
        return

    reasons = []
    if vm_exists:
        reasons.append(f"a registered VM named {name!r} already exists")
    if folder_exists:
        reasons.append(f"a VM folder already exists at {machine_folder}")

    detail = "Cannot create a fresh VM because " + " and ".join(reasons) + "."
    if not confirm_overwrite(name, detail):
        raise RuntimeError("Overwrite cancelled.")

    delete_existing_vboxmanage_vm(name, machine_folder)


def get_vboxmanage_default_machine_folder() -> Path:
    output = run_vboxmanage(["list", "systemproperties"], capture=True)
    for line in output.splitlines():
        if line.startswith("Default machine folder:"):
            return Path(line.split(":", 1)[1].strip()).expanduser()
    raise RuntimeError("Could not determine VirtualBox default machine folder.")


def machine_exists(vbox, mgr, name: str) -> bool:
    return any(m.name == name for m in mgr.getArray(vbox, "machines"))


def machine_folder_exists(vbox, name: str) -> bool:
    machine_folder = Path(vbox.systemProperties.defaultMachineFolder) / name
    return machine_folder.exists()


def find_machine_by_name(vbox, mgr, name: str):
    for machine in mgr.getArray(vbox, "machines"):
        if machine.name == name:
            return machine
    return None


def delete_existing_api_vm(vbox, mgr, constants, name: str, machine_folder: Path):
    machine = find_machine_by_name(vbox, mgr, name)
    if machine is not None:
        if machine.state != constants.MachineState_PoweredOff:
            raise RuntimeError(
                f"VM {name!r} is not powered off. Power it off before "
                "overwriting."
            )

        print(f"Deleting registered VM {name!r}...")
        media = machine.unregister(constants.CleanupMode_DetachAllReturnHardDisksOnly)
        progress = machine.deleteConfig(media)
        progress_wait(progress, "Existing VM deletion")

    if machine_folder.exists():
        print(f"Removing existing VM folder: {machine_folder}")
        shutil.rmtree(machine_folder)


def prepare_api_target(vbox, mgr, constants, name: str):
    machine_folder = Path(vbox.systemProperties.defaultMachineFolder) / name
    vm_exists = find_machine_by_name(vbox, mgr, name) is not None
    folder_exists = machine_folder.exists()

    if not vm_exists and not folder_exists:
        return

    reasons = []
    if vm_exists:
        reasons.append(f"a registered VM named {name!r} already exists")
    if folder_exists:
        reasons.append(f"a VM folder already exists at {machine_folder}")

    detail = "Cannot create a fresh VM because " + " and ".join(reasons) + "."
    if not confirm_overwrite(name, detail):
        raise RuntimeError("Overwrite cancelled.")

    delete_existing_api_vm(vbox, mgr, constants, name, machine_folder)


def find_controller_for_bus(machine, mgr, bus):
    for controller in mgr.getArray(machine, "storageControllers"):
        if controller.bus == bus:
            return controller
    return None


def ensure_disk_controller(machine, mgr, guest_type):
    controller = find_controller_for_bus(
        machine,
        mgr,
        guest_type.recommendedHDStorageBus,
    )
    if controller is not None:
        return controller

    controller = machine.addStorageController(
        "Disk Controller",
        guest_type.recommendedHDStorageBus,
    )
    controller.controllerType = guest_type.recommendedHDStorageController
    return controller


def configure_network(machine, constants, cfg: dict):
    network = cfg.get("network", {})
    mode = str(network.get("mode", "nat")).lower()

    nic = machine.getNetworkAdapter(0)
    nic.enabled = True

    if mode == "nat":
        nic.attachmentType = constants.NetworkAttachmentType_NAT
    elif mode == "bridged":
        interface = network.get("interface")
        if not interface:
            raise ValueError(
                "network.interface is required for bridged networking."
            )
        nic.attachmentType = constants.NetworkAttachmentType_Bridged
        nic.bridgedInterface = str(interface)
    else:
        raise ValueError(
            f"Unsupported network.mode {mode!r}; use nat or bridged."
        )


def configure_integration(machine, constants, cfg: dict):
    integration = cfg.get("integration", {})

    if integration.get("shared_clipboard", True):
        machine.clipboardMode = constants.ClipboardMode_Bidirectional

    if integration.get("drag_and_drop", False):
        machine.dnDMode = constants.DnDMode_Bidirectional


def attach_disk(
    machine,
    vbox,
    mgr,
    constants,
    guest_type,
    disk_gb: int,
    disk_format: str,
    dynamic: bool,
):
    vm_folder = Path(machine.settingsFilePath).parent
    extension = disk_format.lower()
    disk_path = vm_folder / f"{machine.name}.{extension}"

    if disk_path.exists():
        raise FileExistsError(f"Virtual disk already exists: {disk_path}")

    print(
        f"Creating {'dynamic' if dynamic else 'fixed'} "
        f"{disk_gb} GB {disk_format} disk..."
    )

    medium = vbox.createMedium(
        disk_format,
        str(disk_path),
        constants.AccessMode_ReadWrite,
        constants.DeviceType_HardDisk,
    )

    variant = (
        constants.MediumVariant_Standard
        if dynamic
        else constants.MediumVariant_Fixed
    )

    progress = medium.createBaseStorage(
        disk_gb * GIB,
        (variant,),
    )
    progress_wait(progress, "Virtual disk creation")

    session = mgr.getSessionObject(vbox)
    try:
        machine.lockMachine(session, constants.LockType_Write)
        mutable = session.machine

        controller = ensure_disk_controller(
            mutable,
            mgr,
            guest_type,
        )

        mutable.attachDevice(
            controller.name,
            0,
            0,
            constants.DeviceType_HardDisk,
            medium,
        )
        mutable.saveSettings()
    finally:
        try:
            mgr.closeMachineSession(session)
        except Exception:
            try:
                session.unlockMachine()
            except Exception:
                pass

    return disk_path


def create_windows_vm_vboxmanage(config_path: Path):
    cfg = load_yaml(config_path)

    name = get_vm_name(str(get_required(cfg, "vm", "name")))
    os_type = str(cfg["vm"].get("os_type", "Windows11_64"))

    hardware = get_required(cfg, "hardware")
    memory_mb = int(hardware.get("memory_mb", 8192))
    cpus = int(hardware.get("cpus", 4))
    vram_mb = int(hardware.get("vram_mb", 128))

    storage = get_required(cfg, "storage")
    disk_gb = int(storage.get("disk_gb", 120))
    disk_format = str(storage.get("format", "VDI")).upper()
    dynamic = bool(storage.get("dynamic", True))

    installation = get_required(cfg, "installation")
    iso_path = resolve_config_path(
        str(get_required(cfg, "installation", "iso")),
        config_path,
    )
    image_index = installation.get("image_index")

    if not iso_path.is_file():
        raise FileNotFoundError(f"Windows ISO not found: {iso_path}")
    if memory_mb < 4096:
        raise ValueError("Windows 11 requires at least 4096 MB RAM.")
    if cpus < 2:
        raise ValueError("Windows 11 requires at least 2 vCPUs.")
    if disk_gb < 64:
        raise ValueError("Windows 11 requires at least a 64 GB disk.")
    if vram_mb <= 0:
        raise ValueError("Video RAM must be greater than 0 MB.")
    if disk_format != "VDI":
        raise ValueError("storage.format must be VDI for this project.")
    if image_index is not None and int(image_index) < 1:
        raise ValueError("installation.image_index must be 1 or greater.")

    default_machine_folder = get_vboxmanage_default_machine_folder()
    machine_folder = default_machine_folder / name
    prepare_vboxmanage_target(name, machine_folder)
    close_vboxmanage_hdds_by_location(
        machine_folder / f"{name}.{disk_format.lower()}"
    )

    password, product_key = get_install_credentials()

    print(f"VirtualBox: {run_vboxmanage(['--version'], capture=True).strip()}")
    print(f"VM name:    {name}")
    print(f"ISO:        {iso_path}")

    print("Creating VM from VirtualBox Windows defaults...")
    run_vboxmanage([
        "createvm",
        f"--name={name}",
        "--platform-architecture=x86",
        f"--ostype={os_type}",
        "--default",
        "--register",
    ])

    try:
        network = cfg.get("network", {})
        network_mode = str(network.get("mode", "nat")).lower()
        modify_args = [
            "modifyvm",
            name,
            f"--memory={memory_mb}",
            f"--cpus={cpus}",
            "--cpu-execution-cap=100",
            f"--vram={vram_mb}",
            "--clipboard-mode=bidirectional"
            if cfg.get("integration", {}).get("shared_clipboard", True)
            else "--clipboard-mode=disabled",
            "--drag-and-drop=bidirectional"
            if cfg.get("integration", {}).get("drag_and_drop", False)
            else "--drag-and-drop=disabled",
        ]

        if network_mode == "nat":
            modify_args.append("--nic1=nat")
        elif network_mode == "bridged":
            interface = network.get("interface")
            if not interface:
                raise ValueError(
                    "network.interface is required for bridged networking."
                )
            modify_args.extend([
                "--nic1=bridged",
                f"--bridge-adapter1={interface}",
            ])
        else:
            raise ValueError(
                f"Unsupported network.mode {network_mode!r}; use nat or bridged."
            )

        run_vboxmanage(modify_args)
        run_vboxmanage([
            "modifyvm",
            name,
            "--description",
            (
                f"Provisioned from {config_path.name}. "
                f"{cpus} vCPU, {memory_mb} MB RAM, {disk_gb} GB disk."
            ),
        ])

        disk_path = machine_folder / f"{name}.{disk_format.lower()}"
        if disk_path.exists():
            raise FileExistsError(f"Virtual disk already exists: {disk_path}")

        print(
            f"Creating {'dynamic' if dynamic else 'fixed'} "
            f"{disk_gb} GB {disk_format} disk..."
        )
        run_vboxmanage([
            "createmedium",
            "disk",
            f"--filename={disk_path}",
            f"--size={disk_gb * 1024}",
            f"--format={disk_format}",
            f"--variant={'Standard' if dynamic else 'Fixed'}",
        ])
        run_vboxmanage([
            "storageattach",
            name,
            "--storagectl=SATA",
            "--port=0",
            "--device=0",
            "--type=hdd",
            f"--medium={disk_path}",
        ])

        unattended = get_required(cfg, "unattended")
        with tempfile.NamedTemporaryFile("w", encoding="utf-8") as password_file:
            password_file.write(password)
            password_file.flush()

            runtime = cfg.get("runtime", {})
            start_after_create = bool(runtime.get("start_after_create", True))
            frontend = str(runtime.get("frontend", "gui"))
            defer_start = env_enabled("VBOX_DEFER_START")

            unattended_args = [
                "unattended",
                "install",
                name,
                f"--iso={iso_path}",
                f"--user={unattended.get('username', 'rob')}",
                f"--user-password-file={password_file.name}",
                f"--admin-password-file={password_file.name}",
                "--full-user-name="
                f"{unattended.get('full_name', unattended.get('username', 'rob'))}",
                f"--locale={unattended.get('locale', 'en_US')}",
                f"--country={unattended.get('country', 'US')}",
                "--time-zone="
                f"{unattended.get('timezone', 'Eastern Standard Time')}",
                f"--hostname={unattended.get('hostname', 'win11-workstation.local')}",
                f"--image-index={int(image_index or 1)}",
                "--install-additions"
                if unattended.get("install_guest_additions", True)
                else "--no-install-additions",
                f"--start-vm={frontend}"
                if start_after_create and not defer_start
                else "--start-vm=none",
            ]
            if product_key:
                unattended_args.append(f"--key={product_key}")

            print(
                "Creating Windows unattended answer media and "
                "reconfiguring VM..."
            )
            run_vboxmanage(unattended_args, capture=True)
            ensure_vboxmanage_install_media(name, machine_folder, iso_path)

        print()
        print("Provisioning prepared successfully.")
        print(f"Disk: {disk_path}")
        if start_after_create and defer_start:
            write_created_vm_info(
                name,
                frontend,
                machine_folder / f"{name}.vbox",
            )
            print(f'Starting is deferred to the host wrapper: "{name}"')
        elif start_after_create:
            print()
            print(
                "Windows Setup is now running unattended. "
                "When setup finishes, log in with the local account "
                "defined in vm.yaml and the password you supplied "
                "at the interactive prompt."
            )
        else:
            print(f'Start it later with: VBoxManage startvm "{name}" --type gui')

    except Exception:
        print(
            "\nProvisioning failed after the VM was registered.\n"
            "The partially created VM was NOT deleted automatically "
            "to avoid destroying anything unexpectedly.",
            file=sys.stderr,
        )
        raise


def create_kali_vm_vboxmanage(config_path: Path):
    cfg = load_yaml(config_path)

    name = get_vm_name(str(get_required(cfg, "vm", "name")))
    os_type = str(cfg["vm"].get("os_type", "Debian13_64"))

    hardware = get_required(cfg, "hardware")
    memory_mb = int(hardware.get("memory_mb", 4096))
    cpus = int(hardware.get("cpus", 2))
    vram_mb = int(hardware.get("vram_mb", 128))

    storage = get_required(cfg, "storage")
    disk_gb = int(storage.get("disk_gb", 80))
    disk_format = str(storage.get("format", "VDI")).upper()
    dynamic = bool(storage.get("dynamic", True))

    iso_path = resolve_config_path(
        str(get_required(cfg, "installation", "iso")),
        config_path,
    )

    if not iso_path.is_file():
        raise FileNotFoundError(f"Kali ISO not found: {iso_path}")
    if memory_mb < 2048:
        raise ValueError("Kali Linux should have at least 2048 MB RAM.")
    if cpus < 1:
        raise ValueError("CPU count must be at least 1.")
    if disk_gb < 30:
        raise ValueError("Kali Linux should have at least a 30 GB disk.")
    if vram_mb <= 0:
        raise ValueError("Video RAM must be greater than 0 MB.")
    if disk_format != "VDI":
        raise ValueError("storage.format must be VDI for this project.")

    default_machine_folder = get_vboxmanage_default_machine_folder()
    machine_folder = default_machine_folder / name
    prepare_vboxmanage_target(name, machine_folder)
    close_vboxmanage_hdds_by_location(
        machine_folder / f"{name}.{disk_format.lower()}"
    )

    password = get_guest_password("Kali user")

    print(f"VirtualBox: {run_vboxmanage(['--version'], capture=True).strip()}")
    print(f"VM name:    {name}")
    print(f"ISO:        {iso_path}")

    print("Creating VM from VirtualBox Linux defaults...")
    run_vboxmanage([
        "createvm",
        f"--name={name}",
        "--platform-architecture=x86",
        f"--ostype={os_type}",
        "--default",
        "--register",
    ])

    try:
        network = cfg.get("network", {})
        network_mode = str(network.get("mode", "nat")).lower()
        modify_args = [
            "modifyvm",
            name,
            f"--memory={memory_mb}",
            f"--cpus={cpus}",
            "--cpu-execution-cap=100",
            f"--vram={vram_mb}",
            "--graphicscontroller=vmsvga",
            "--clipboard-mode=bidirectional"
            if cfg.get("integration", {}).get("shared_clipboard", True)
            else "--clipboard-mode=disabled",
            "--drag-and-drop=bidirectional"
            if cfg.get("integration", {}).get("drag_and_drop", False)
            else "--drag-and-drop=disabled",
        ]

        if network_mode == "nat":
            modify_args.append("--nic1=nat")
        elif network_mode == "bridged":
            interface = network.get("interface")
            if not interface:
                raise ValueError(
                    "network.interface is required for bridged networking."
                )
            modify_args.extend([
                "--nic1=bridged",
                f"--bridge-adapter1={interface}",
            ])
        else:
            raise ValueError(
                f"Unsupported network.mode {network_mode!r}; use nat or bridged."
            )

        run_vboxmanage(modify_args)
        run_vboxmanage([
            "modifyvm",
            name,
            "--description",
            (
                f"Provisioned from {config_path.name}. "
                f"{cpus} vCPU, {memory_mb} MB RAM, {disk_gb} GB disk."
            ),
        ])

        disk_path = machine_folder / f"{name}.{disk_format.lower()}"
        if disk_path.exists():
            raise FileExistsError(f"Virtual disk already exists: {disk_path}")

        print(
            f"Creating {'dynamic' if dynamic else 'fixed'} "
            f"{disk_gb} GB {disk_format} disk..."
        )
        run_vboxmanage([
            "createmedium",
            "disk",
            f"--filename={disk_path}",
            f"--size={disk_gb * 1024}",
            f"--format={disk_format}",
            f"--variant={'Standard' if dynamic else 'Fixed'}",
        ])
        run_vboxmanage([
            "storageattach",
            name,
            "--storagectl=SATA",
            "--port=0",
            "--device=0",
            "--type=hdd",
            f"--medium={disk_path}",
        ])

        autoinstall_iso = build_kali_autoinstall_iso(
            iso_path,
            machine_folder,
            cfg,
            password,
        )

        run_vboxmanage([
            "storageattach",
            name,
            "--storagectl=SATA",
            "--port=1",
            "--device=0",
            "--type=dvddrive",
            f"--medium={autoinstall_iso}",
        ])
        run_vboxmanage([
            "modifyvm",
            name,
            "--boot1=dvd",
            "--boot2=disk",
            "--boot3=none",
            "--boot4=none",
        ])

        runtime = cfg.get("runtime", {})
        start_after_create = bool(runtime.get("start_after_create", True))
        frontend = str(runtime.get("frontend", "gui"))

        print()
        print("Provisioning prepared successfully.")
        print(f"Disk: {disk_path}")
        print(f"Autoinstall ISO: {autoinstall_iso}")
        if start_after_create and env_enabled("VBOX_DEFER_START"):
            write_created_vm_info(
                name,
                frontend,
                machine_folder / f"{name}.vbox",
            )
            print(f'Starting is deferred to the host wrapper: "{name}"')
        elif start_after_create:
            run_vboxmanage(["startvm", name, "--type", frontend])
        else:
            print(f'Start it later with: VBoxManage startvm "{name}" --type gui')

    except Exception:
        print(
            "\nProvisioning failed after the VM was registered.\n"
            "The partially created VM was NOT deleted automatically "
            "to avoid destroying anything unexpectedly.",
            file=sys.stderr,
        )
        raise


def configure_unattended(
    vbox,
    machine,
    cfg: dict,
    iso_path: Path,
    password: str,
    product_key: str,
):
    unattended_cfg = get_required(cfg, "unattended")
    installation_cfg = get_required(cfg, "installation")

    unattended = vbox.createUnattendedInstaller()

    # IUnattended workflow: ISO -> registered machine -> properties ->
    # prepare -> constructMedia -> reconfigureVM.
    unattended.isoPath = str(iso_path)
    unattended.machine = machine

    unattended.user = str(unattended_cfg.get("username", "rob"))
    unattended.fullUserName = str(
        unattended_cfg.get("full_name", unattended.user)
    )
    unattended.userPassword = password
    unattended.adminPassword = password
    if product_key:
        unattended.productKey = product_key
    if "image_index" in installation_cfg:
        unattended.imageIndex = int(installation_cfg["image_index"])

    unattended.hostname = str(
        unattended_cfg.get("hostname", "win11-workstation.local")
    )
    unattended.timeZone = str(
        unattended_cfg.get("timezone", "Eastern Standard Time")
    )
    unattended.locale = str(
        unattended_cfg.get("locale", "en_US")
    )
    set_optional_unattended_property(
        unattended,
        "keyboardLayout",
        str(unattended_cfg.get("keyboard_layout", "us")),
    )
    unattended.country = str(
        unattended_cfg.get("country", "US")
    )

    # Keeping setup updates off makes the initial build faster and more
    # deterministic. Windows Update can be run once the desktop is ready.
    unattended.avoidUpdatesOverNetwork = bool(
        unattended_cfg.get("avoid_updates_during_install", True)
    )

    unattended.installGuestAdditions = bool(
        unattended_cfg.get("install_guest_additions", True)
    )

    # Let VirtualBox use its Windows-specific unattended answer templates.
    unattended.prepare()

    if not unattended.isUnattendedInstallSupported:
        raise RuntimeError(
            "VirtualBox reports that unattended installation is not "
            "supported for this ISO/guest combination."
        )

    print("Creating Windows unattended answer media...")
    unattended.constructMedia()

    force_unattended_no_ui(find_unattended_answer_file(machine))

    print("Attaching Windows installation and unattended media...")
    unattended.reconfigureVM()

    # The internal installer is no longer needed after the VM is configured.
    unattended.done()


def start_vm(machine, vbox, mgr, frontend: str):
    print(f"Starting VM with frontend: {frontend}")
    session = mgr.getSessionObject(vbox)
    try:
        progress = machine.launchVMProcess(session, frontend, "")
        progress_wait(progress, "VM startup")
    finally:
        try:
            mgr.closeMachineSession(session)
        except Exception:
            pass


def create_windows_vm_api(config_path: Path):
    if VirtualBoxManager is None:
        raise RuntimeError(
            "vboxapi is not available in this Python installation: "
            f"{VBOXAPI_IMPORT_ERROR}"
        )

    cfg = load_yaml(config_path)

    name = get_vm_name(str(get_required(cfg, "vm", "name")))
    os_type = str(cfg["vm"].get("os_type", "Windows11_64"))

    hardware = get_required(cfg, "hardware")
    memory_mb = int(hardware.get("memory_mb", 8192))
    cpus = int(hardware.get("cpus", 4))
    vram_mb = int(hardware.get("vram_mb", 128))

    storage = get_required(cfg, "storage")
    disk_gb = int(storage.get("disk_gb", 120))
    disk_format = str(storage.get("format", "VDI")).upper()
    dynamic = bool(storage.get("dynamic", True))

    iso_path = resolve_config_path(
        str(get_required(cfg, "installation", "iso")),
        config_path,
    )
    image_index = cfg["installation"].get("image_index")

    if not iso_path.is_file():
        raise FileNotFoundError(f"Windows ISO not found: {iso_path}")

    if memory_mb < 4096:
        raise ValueError("Windows 11 requires at least 4096 MB RAM.")
    if cpus < 2:
        raise ValueError("Windows 11 requires at least 2 vCPUs.")
    if disk_gb < 64:
        raise ValueError("Windows 11 requires at least a 64 GB disk.")
    if vram_mb <= 0:
        raise ValueError("Video RAM must be greater than 0 MB.")
    if disk_format != "VDI":
        raise ValueError("storage.format must be VDI for this project.")
    if image_index is not None and int(image_index) < 1:
        raise ValueError("installation.image_index must be 1 or greater.")

    mgr = VirtualBoxManager(None, None)
    vbox = mgr.getVirtualBox()
    c = mgr.constants

    prepare_api_target(vbox, mgr, c, name)

    password, product_key = get_install_credentials()

    print(f"VirtualBox: {vbox.version}")
    print(f"VM name:    {name}")
    print(f"ISO:        {iso_path}")

    guest_type = vbox.getGuestOSType(os_type)

    machine = vbox.createMachine(
        "",
        name,
        c.PlatformArchitecture_x86,
        [],
        os_type,
        "",
        "",
        "",
        "",
    )

    # VirtualBox applies its Windows 11 defaults here, including the guest's
    # recommended platform/firmware-related configuration.
    machine.applyDefaults("")

    machine.memorySize = memory_mb
    machine.CPUCount = cpus
    machine.CPUExecutionCap = 100
    machine.graphicsAdapter.VRAMSize = vram_mb

    configure_network(machine, c, cfg)
    configure_integration(machine, c, cfg)

    machine.description = (
        f"Provisioned from {config_path.name}. "
        f"{cpus} vCPU, {memory_mb} MB RAM, {disk_gb} GB disk."
    )

    machine.saveSettings()
    vbox.registerMachine(machine)

    try:
        disk_path = attach_disk(
            machine,
            vbox,
            mgr,
            c,
            guest_type,
            disk_gb,
            disk_format,
            dynamic,
        )

        configure_unattended(
            vbox,
            machine,
            cfg,
            iso_path,
            password,
            product_key,
        )

        print()
        print("Provisioning prepared successfully.")
        print(f"Disk: {disk_path}")

        start_after_create = bool(
            cfg.get("runtime", {}).get("start_after_create", True)
        )

        if start_after_create:
            frontend = str(
                cfg.get("runtime", {}).get("frontend", "gui")
            )
            start_vm(machine, vbox, mgr, frontend)
            print()
            print(
                "Windows Setup is now running unattended. "
                "When setup finishes, log in with the local account "
                "defined in vm.yaml and the password you supplied "
                "at the interactive prompt."
            )
        else:
            print(
                f'Start it later with: VBoxManage startvm "{name}" --type gui'
            )

    except Exception:
        print(
            "\nProvisioning failed after the VM was registered.\n"
            "The partially created VM was NOT deleted automatically "
            "to avoid destroying anything unexpectedly.",
            file=sys.stderr,
        )
        raise


def create_virtualbox_vm(config_path: Path):
    cfg = load_yaml(config_path)
    os_family = str(cfg.get("vm", {}).get("os_family", "windows")).lower()
    backend = os.environ.get("VBOX_BACKEND", "api").lower()

    if os_family in {"kali", "linux"}:
        if backend not in {"vboxmanage", "cli"}:
            raise RuntimeError(
                "Kali/Linux profiles require VBOX_BACKEND=vboxmanage."
            )
        return create_kali_vm_vboxmanage(config_path)

    if backend in {"vboxmanage", "cli"}:
        return create_windows_vm_vboxmanage(config_path)
    if backend == "api":
        return create_windows_vm_api(config_path)
    raise ValueError("VBOX_BACKEND must be 'api' or 'vboxmanage'.")


def main():
    parser = argparse.ArgumentParser(
        description="Provision an unattended VirtualBox VM from YAML."
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to the VM YAML configuration file.",
    )
    args = parser.parse_args()

    try:
        create_virtualbox_vm(args.config.expanduser().resolve())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
