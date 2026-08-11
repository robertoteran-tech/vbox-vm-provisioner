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
  8. Supplies the Windows product key to the unattended installer.
  9. Installs VirtualBox Guest Additions.
 10. Starts the VM so Windows installs without normal setup interaction.

Credentials are intentionally NOT stored in vm.yaml.
"""

from __future__ import annotations

import argparse
import re
import sys
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
except ImportError:
    print(
        "ERROR: vboxapi is not available in this Python installation.",
        file=sys.stderr,
    )
    raise SystemExit(1)


MIB = 1024 * 1024
GIB = 1024 * MIB
PRODUCT_KEY_RE = re.compile(r"^[A-Z0-9]{5}(?:-[A-Z0-9]{5}){4}$")


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

    product_key = input("Windows product key: ").strip()

    if not product_key:
        raise RuntimeError("Windows product key cannot be empty.")

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


def machine_exists(vbox, mgr, name: str) -> bool:
    return any(m.name == name for m in mgr.getArray(vbox, "machines"))


def machine_folder_exists(vbox, name: str) -> bool:
    machine_folder = Path(vbox.systemProperties.defaultMachineFolder) / name
    return machine_folder.exists()


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
    unattended.productKey = product_key
    if "image_index" in installation_cfg:
        unattended.imageIndex = int(installation_cfg["image_index"])

    unattended.hostname = str(
        unattended_cfg.get("hostname", "win11-lab.local")
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


def create_windows_vm(config_path: Path):
    cfg = load_yaml(config_path)

    name = str(get_required(cfg, "vm", "name"))
    os_type = str(cfg["vm"].get("os_type", "Windows11_64"))

    hardware = get_required(cfg, "hardware")
    memory_mb = int(hardware.get("memory_mb", 8192))
    cpus = int(hardware.get("cpus", 4))
    vram_mb = int(hardware.get("vram_mb", 128))

    storage = get_required(cfg, "storage")
    disk_gb = int(storage.get("disk_gb", 120))
    disk_format = str(storage.get("format", "VDI")).upper()
    dynamic = bool(storage.get("dynamic", True))

    iso_path = Path(
        get_required(cfg, "installation", "iso")
    ).expanduser().resolve()
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

    if machine_exists(vbox, mgr, name):
        raise RuntimeError(
            f"A registered VM named {name!r} already exists."
        )
    if machine_folder_exists(vbox, name):
        raise RuntimeError(
            "A VM folder already exists for this name. Remove or rename it "
            f"before provisioning: "
            f"{Path(vbox.systemProperties.defaultMachineFolder) / name}"
        )

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
