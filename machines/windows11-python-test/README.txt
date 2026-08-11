Windows 11 unattended VirtualBox builder
==========================================

Files
-----
create_windows_vm.py
    Creates the VM with the VirtualBox Python Main API and configures
    VirtualBox's unattended Windows installer.

vm.yaml
    Declarative VM + Windows installation configuration.

Recommended project locations
-----------------------------
~/vbox-environment/scripts/create_windows_vm.py
~/vbox-environment/machines/windows11-python-test/vm.yaml
~/vbox-environment/isos/Win11_25H2_English_x64_v2.iso

Ubuntu dependency
-----------------
sudo apt install -y python3-yaml

Secrets
-------
Do not put the Windows product key or Windows password in vm.yaml.

The builder prompts for both values at runtime:

* VM name: visible input; press Enter to use vm.yaml's default name.
* Windows VM password: hidden input.
* Windows product key: visible input; press Enter to install without a key.

Credentials are kept only in Python process memory for provisioning. They are
not written to vm.yaml.

Run
---
python3 ~/vbox-environment/scripts/create_windows_vm.py \
  ~/vbox-environment/machines/windows11-python-test/vm.yaml

Docker smoke test
-----------------
~/vbox-environment/scripts/docker-provision.sh --check

This only checks Docker + host VirtualBox access. It does not ask for VM name,
Windows password, or product key.

Create a VM with Docker
-----------------------
~/vbox-environment/scripts/docker-provision.sh \
  ~/vbox-environment/machines/windows11-python-test/vm.yaml

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
