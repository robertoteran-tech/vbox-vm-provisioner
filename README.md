# Portable VirtualBox VM Provisioning

This project creates unattended Windows 11 VirtualBox VMs from a YAML config.
The main goal is to keep the provisioning environment portable while still
using the VirtualBox installation and kernel drivers from the Linux host.

## What This Does

- Builds a small Docker image with Python and runtime libraries.
- Mounts the host VirtualBox installation into the container.
- Reads a VM definition from `machines/windows11-python-test/vm.yaml`.
- Prompts for VM name, Windows password, and optional product key.
- Creates the VM, disk, and unattended Windows install media.
- Starts the VM from the host VirtualBox installation.

## Requirements

The host machine must already have:

- Docker installed and usable by your user.
- VirtualBox installed and working on the host.
- VirtualBox kernel modules loaded on the host.
- Your user in the `docker` group.
- Your user in the `vboxusers` group.
- A Windows ISO placed under `isos/`.

This project does not install VirtualBox. The container uses these host paths:

```text
/usr/lib/virtualbox
/usr/share/virtualbox
~/.config/VirtualBox
~/VirtualBox VMs
```

## Project Layout

```text
.
├── Dockerfile
├── README.md
├── isos/
│   └── Win11_25H2_English_x64_v2.iso
├── machines/
│   └── windows11-python-test/
│       ├── README.txt
│       └── vm.yaml
└── scripts/
    ├── create_windows_vm.py
    └── docker-provision.sh
```

Large files such as ISOs, VDI disks, generated unattended files, and logs are
ignored by Git.

## Quick Start

From the project directory:

```bash
cd /home/rob/vbox-environment
scripts/docker-provision.sh --check
```

Expected output ends with your VirtualBox version and default VM folder:

```text
VirtualBox: 7.2.14r174565
/home/rob/VirtualBox VMs
```

Then create a VM:

```bash
scripts/docker-provision.sh machines/windows11-python-test/vm.yaml
```

The script prompts for:

```text
VM name [windows11-python-test]:
Delete existing VM/files for '...' and recreate it? Type yes to continue:
Windows VM password:
Confirm Windows VM password:
Windows product key (blank to skip):
```

At the VM name prompt, press Enter to use the YAML default or type a new name.
If a VM or VM folder already exists with that name, type `yes` to delete it and
create a fresh replacement. Any other answer cancels the run.
At the product-key prompt, press Enter to install Windows without a key and
activate later.

## Running Without Docker

You can run the Python script directly:

```bash
python3 scripts/create_windows_vm.py machines/windows11-python-test/vm.yaml
```

Native runs default to the VirtualBox Python API backend. To use the same
`VBoxManage` backend as Docker:

```bash
VBOX_BACKEND=vboxmanage python3 scripts/create_windows_vm.py \
  machines/windows11-python-test/vm.yaml
```

## Setting The VM Name Non-Interactively

Use `VBOX_VM_NAME`:

```bash
VBOX_VM_NAME=win11-test-2 scripts/docker-provision.sh \
  machines/windows11-python-test/vm.yaml
```

This is useful when creating multiple VMs from the same YAML template.

## Overwriting An Existing VM

If the chosen VM name already exists, the script asks before deleting it:

```text
Delete existing VM/files for 'Windows_Machine' and recreate it? Type yes to continue:
```

Type exactly:

```text
yes
```

The script refuses to overwrite a VM that is currently running or paused. Power
it off first.

For non-interactive runs, set:

```bash
VBOX_OVERWRITE=1 VBOX_VM_NAME=Windows_Machine scripts/docker-provision.sh \
  machines/windows11-python-test/vm.yaml
```

## VM Configuration

Edit:

```text
machines/windows11-python-test/vm.yaml
```

Important settings:

```yaml
vm:
  name: windows11-python-test

hardware:
  memory_mb: 8192
  cpus: 4
  vram_mb: 128

storage:
  disk_gb: 120

installation:
  iso: ../../isos/Win11_25H2_English_x64_v2.iso
  image_index: 1
```

The `image_index` selects the Windows edition from the ISO. The current config
uses index `1`, which is Windows 11 Home for the listed ISO.

## Common Commands

List registered VMs:

```bash
VBoxManage list vms
```

Start an existing VM:

```bash
VBoxManage startvm "Windows_Machine" --type gui
```

Show VM state:

```bash
VBoxManage showvminfo "Windows_Machine" --machinereadable | grep VMState
```

Delete a VM and its files:

```bash
VBoxManage unregistervm "Windows_Machine" --delete
```

Use Docker privileged mode if device access fails:

```bash
VBOX_DOCKER_PRIVILEGED=1 scripts/docker-provision.sh \
  machines/windows11-python-test/vm.yaml
```

## Troubleshooting

### VM Name Already Exists

If you see:

```text
A registered VM named '...' already exists.
```

choose a different VM name at the prompt or type `yes` when asked to overwrite
it. You can also delete the existing VM manually:

```bash
VBoxManage unregistervm "VM_NAME" --delete
```

### VM Folder Already Exists

If a previous failed run left a folder behind, remove or rename it after
confirming you do not need its files:

```bash
rm -rf "$HOME/VirtualBox VMs/VM_NAME"
```

Prefer `VBoxManage unregistervm "VM_NAME" --delete` when the VM is still
registered.

### Docker Check Fails

Run:

```bash
scripts/docker-provision.sh --check
```

If this fails, verify:

```bash
docker --version
VBoxManage --version
ls -ld /usr/lib/virtualbox /usr/share/virtualbox
ls -l /dev/vbox*
id
```

Your user should be able to run Docker and should be in `vboxusers`.

### Product Key

The product key is optional. Press Enter at this prompt to skip it:

```text
Windows product key (blank to skip):
```

Windows can be activated later from inside the guest.

## Notes

- Docker does not contain a full VirtualBox install.
- The host VirtualBox version is the version that matters.
- VM startup is handled on the host so the GUI opens correctly.
- Credentials are prompted at runtime and are not stored in `vm.yaml`.
