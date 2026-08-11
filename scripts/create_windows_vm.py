#!/usr/bin/env python3
"""
Create and start a fully unattended Windows 11 VirtualBox VM from YAML.

VirtualBox 7.2.x / Linux host / vboxapi (XPCOM).

The script:
  1. Prompts for the Windows password and product key.
  2. Creates and registers the VM.
  3. Applies VirtualBox's Windows 11 defaults.
  4. Sets CPU/RAM/video/networking from YAML.
  5. Creates and attaches a dynamically allocated VDI.
  6. Uses VirtualBox IUnattended to generate Windows Setup answer media.
  7. Creates the local Windows administrator account.
  8. Supplies the Windows product key when one is entered.
  9. Installs VirtualBox Guest Additions.
 10. Starts the VM so Windows installs without normal setup interaction.

Credentials are intentionally NOT stored in vm.yaml.
"""

from __future__ import annotations

import argparse
import os
import re
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


def create_windows_vm(config_path: Path):
    backend = os.environ.get("VBOX_BACKEND", "api").lower()
    if backend in {"vboxmanage", "cli"}:
        return create_windows_vm_vboxmanage(config_path)
    if backend == "api":
        return create_windows_vm_api(config_path)
    raise ValueError("VBOX_BACKEND must be 'api' or 'vboxmanage'.")


def main():
    parser = argparse.ArgumentParser(
        description="Provision an unattended Windows 11 VirtualBox VM."
    )
    parser.add_argument(
        "config",
        type=Path,
        help="Path to the VM YAML configuration file.",
    )
    args = parser.parse_args()

    try:
        create_windows_vm(args.config.expanduser().resolve())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
