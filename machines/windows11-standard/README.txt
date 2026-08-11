Windows 11 unattended VirtualBox profile
==========================================

Files
-----
create_virtualbox_vm.py
    Creates the VM and configures VirtualBox's unattended Windows installer.

vm.yaml
    Declarative VM + Windows installation configuration.

Recommended project locations
-----------------------------
~/vbox-environment/scripts/create_virtualbox_vm.py
~/vbox-environment/machines/windows11-standard/vm.yaml
~/vbox-environment/isos/Win11_25H2_English_x64_v2.iso

ISO
---
Place the Windows installer ISO in the project isos directory with this exact
path:

~/vbox-environment/isos/Win11_25H2_English_x64_v2.iso

The path must match the installation.iso value in vm.yaml. If you use a
different Windows ISO filename, update vm.yaml before running the provisioner.

Host requirements
-----------------
Docker and VirtualBox must already be installed on the host. The Docker wrapper
provides the Python runtime and mounts the host VirtualBox installation.

Secrets
-------
Do not put the Windows product key or Windows password in vm.yaml.

The provisioner prompts for both values at runtime:

* VM name: visible input; press Enter to use vm.yaml's default name.
* Windows VM password: hidden input.
* Windows product key: visible input; press Enter to install without a key.

Credentials are kept only in Python process memory for provisioning. They are
not written to vm.yaml.

Run
---
python3 ~/vbox-environment/scripts/create_virtualbox_vm.py \
  ~/vbox-environment/machines/windows11-standard/vm.yaml

Docker smoke test
-----------------
~/vbox-environment/scripts/docker-provision.sh --check

This only checks Docker + host VirtualBox access. It does not ask for VM name,
Windows password, or product key.

Create a VM with Docker
-----------------------
~/vbox-environment/scripts/docker-provision.sh \
  ~/vbox-environment/machines/windows11-standard/vm.yaml

This command prompts for VM name, Windows password, and optional product key.
If the VM name already exists, it asks whether to delete and recreate it. Type
"yes" to overwrite, or anything else to cancel.

What should happen
------------------
1. VirtualBox creates the Windows 11 VM.
2. A 120 GB dynamic VDI is created.
3. VirtualBox generates unattended Windows Setup media.
4. The Windows 11 ISO and answer media are attached.
5. The VM starts in the GUI.
6. Windows Setup installs automatically.
7. A local administrator account named "rob" is created.
8. VirtualBox Guest Additions are installed.
9. After Windows finishes, log in using the password entered above.
