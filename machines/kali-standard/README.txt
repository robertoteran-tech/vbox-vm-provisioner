Kali Linux unattended VirtualBox profile
=======================================

Files
-----
create_virtualbox_vm.py
    Creates the VM and generates a Kali/Debian preseed autoinstall ISO.

vm.yaml
    Declarative VM + Kali installation configuration.

Host requirements
-----------------
Docker and VirtualBox must already be installed on the host. The Docker wrapper
provides the Python runtime and mounts the host VirtualBox installation.

ISO
---
Place the Kali installer ISO here:

~/vbox-environment/isos/kali-linux-2026.1-installer-amd64.iso

Secrets
-------
Do not put the Kali password in vm.yaml.

The provisioner prompts for:

* VM name: visible input; press Enter to use vm.yaml's default name.
* Kali user password: hidden input.

Create a VM with Docker
-----------------------
~/vbox-environment/scripts/docker-provision.sh \
  ~/vbox-environment/machines/kali-standard/vm.yaml

What should happen
------------------
1. VirtualBox creates the Kali Linux VM.
2. An 80 GB dynamic VDI is created.
3. A Kali autoinstall ISO is generated from the source Kali installer ISO.
4. The VM starts in the GUI.
5. Kali installs automatically with the local user from vm.yaml.
6. After installation finishes and reboots, log in with that user and the
   password entered at the prompt.
