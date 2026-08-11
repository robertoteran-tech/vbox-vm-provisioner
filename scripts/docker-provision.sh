#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd -- "${SCRIPT_DIR}/.." && pwd)"
IMAGE_NAME="${VBOX_ENV_IMAGE:-vbox-environment-provisioner:local}"
DEFAULT_CONFIG_PATH="${PROJECT_ROOT}/machines/windows11-python-test/vm.yaml"
MODE="provision"
CONFIG_PATH="${DEFAULT_CONFIG_PATH}"
CREATED_VM_INFO_FILE=""

usage() {
    cat <<EOF
Usage:
  ${0##*/} [--check] [vm.yaml]
  ${0##*/} --help

Build and run the portable Python provisioning container against the host
VirtualBox installation.

Options:
  --check   Build the image and verify the container can load host VirtualBox.
  --help    Show this help text.

Environment:
  VBOX_ENV_IMAGE             Docker image tag to build/run.
  VBOX_DOCKER_PRIVILEGED=1   Run the container with --privileged.
EOF
}

vm_is_registered() {
    VBoxManage showvminfo "$1" >/dev/null 2>&1
}

register_vm_if_needed() {
    local vm_name="$1"
    local settings_file="$2"

    if vm_is_registered "${vm_name}"; then
        return
    fi

    if [[ -n "${settings_file}" && -f "${settings_file}" ]]; then
        echo "Registering VM on host: ${settings_file}"
        VBoxManage registervm "${settings_file}"
    else
        echo "ERROR: VM ${vm_name} is not registered and settings file was not found: ${settings_file}" >&2
        exit 1
    fi
}

close_inaccessible_hdds_by_location() {
    local disk_path="$1"
    local current_uuid=""
    local current_location=""
    local current_state=""

    close_current_if_needed() {
        if [[ "${current_location}" == "${disk_path}" && "${current_state}" == "inaccessible" ]]; then
            echo "Removing stale medium registry entry: ${current_uuid}"
            VBoxManage closemedium disk "${current_uuid}" || true
        fi
    }

    while IFS= read -r line; do
        case "${line}" in
            UUID:*)
                close_current_if_needed
                current_uuid="$(sed 's/^UUID:[[:space:]]*//' <<<"${line}")"
                current_location=""
                current_state=""
                ;;
            Location:*)
                current_location="$(sed 's/^Location:[[:space:]]*//' <<<"${line}")"
                ;;
            State:*)
                current_state="$(sed 's/^State:[[:space:]]*//' <<<"${line}")"
                ;;
        esac
    done < <(VBoxManage list hdds --long)

    close_current_if_needed
}

repair_vm_disk_attachment() {
    local vm_name="$1"
    local settings_file="$2"
    local vm_dir
    local disk_path
    local attached_disk
    local attached_uuid

    vm_dir="$(dirname -- "${settings_file}")"
    disk_path="${vm_dir}/${vm_name}.vdi"

    if [[ ! -f "${disk_path}" ]]; then
        return
    fi

    close_inaccessible_hdds_by_location "${disk_path}"

    attached_disk="$(
        VBoxManage showvminfo "${vm_name}" --machinereadable \
            | sed -n 's/^"SATA-0-0"="\(.*\)"$/\1/p'
    )"
    attached_uuid="$(
        VBoxManage showvminfo "${vm_name}" --machinereadable \
            | sed -n 's/^"SATA-ImageUUID-0-0"="\(.*\)"$/\1/p'
    )"

    if [[ "${attached_disk}" != "${disk_path}" || -z "${attached_uuid}" ]]; then
        VBoxManage storageattach "${vm_name}" \
            --storagectl SATA \
            --port 0 \
            --device 0 \
            --type hdd \
            --medium none >/dev/null 2>&1 || true
        close_inaccessible_hdds_by_location "${disk_path}"
        VBoxManage storageattach "${vm_name}" \
            --storagectl SATA \
            --port 0 \
            --device 0 \
            --type hdd \
            --medium "${disk_path}"
        return
    fi

    if VBoxManage list hdds --long | grep -A12 -F "UUID:           ${attached_uuid}" \
        | grep -q 'State:          inaccessible'; then
        echo "Repairing stale disk registry entry for: ${disk_path}"
        VBoxManage storageattach "${vm_name}" \
            --storagectl SATA \
            --port 0 \
            --device 0 \
            --type hdd \
            --medium none || true
        VBoxManage closemedium disk "${attached_uuid}" || true
        VBoxManage storageattach "${vm_name}" \
            --storagectl SATA \
            --port 0 \
            --device 0 \
            --type hdd \
            --medium "${disk_path}"
    fi
}

start_vm_on_host() {
    local vm_name="$1"
    local frontend="$2"
    local settings_file="$3"

    register_vm_if_needed "${vm_name}" "${settings_file}"
    repair_vm_disk_attachment "${vm_name}" "${settings_file}"

    echo "Starting VM on host with frontend: ${frontend}"
    if ! VBoxManage startvm "${vm_name}" --type "${frontend}"; then
        echo "VM start failed once; retrying after host registration/disk repair..."
        register_vm_if_needed "${vm_name}" "${settings_file}"
        repair_vm_disk_attachment "${vm_name}" "${settings_file}"
        VBoxManage startvm "${vm_name}" --type "${frontend}"
    fi
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --check)
            MODE="check"
            shift
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "ERROR: Unknown option: $1" >&2
            usage >&2
            exit 1
            ;;
        *)
            CONFIG_PATH="$1"
            shift
            ;;
    esac
done

if [[ $# -gt 0 ]]; then
    CONFIG_PATH="$1"
fi

if [[ "${CONFIG_PATH}" != /* ]]; then
    CONFIG_PATH="${PROJECT_ROOT}/${CONFIG_PATH}"
fi

if [[ "${MODE}" == "provision" && ! -f "${CONFIG_PATH}" ]]; then
    echo "ERROR: Config file not found: ${CONFIG_PATH}" >&2
    exit 1
fi

if ! command -v docker >/dev/null 2>&1; then
    echo "ERROR: docker is not installed or not on PATH." >&2
    exit 1
fi

for required_path in \
    /usr/lib/virtualbox \
    /usr/share/virtualbox
do
    if [[ ! -e "${required_path}" ]]; then
        echo "ERROR: Required host VirtualBox path is missing: ${required_path}" >&2
        exit 1
    fi
done

mkdir -p "${HOME}/.config/VirtualBox"
mkdir -p "${HOME}/VirtualBox VMs"

if [[ "${MODE}" == "provision" ]]; then
    CREATED_VM_INFO_FILE="$(mktemp "${PROJECT_ROOT}/.vbox-created.XXXXXX")"
    rm -f "${CREATED_VM_INFO_FILE}"
    trap '[[ -n "${CREATED_VM_INFO_FILE}" ]] && rm -f "${CREATED_VM_INFO_FILE}"' EXIT
fi

docker build -t "${IMAGE_NAME}" "${PROJECT_ROOT}"

docker_args=(
    run
    --rm
    --network host
    --user "$(id -u):$(id -g)"
    --workdir "${PROJECT_ROOT}"
    -e "HOME=${HOME}"
    -e "USER=$(id -un)"
    -e "DISPLAY=${DISPLAY:-}"
    -e "VBOX_BACKEND=vboxmanage"
    -e "VBOX_DEFER_START=1"
    -e "VBOX_CREATED_VM_INFO_FILE=${CREATED_VM_INFO_FILE}"
    -e "VBOX_USER_HOME=${HOME}/.config/VirtualBox"
    -v "${PROJECT_ROOT}:${PROJECT_ROOT}"
    -v "${HOME}/.config/VirtualBox:${HOME}/.config/VirtualBox"
    -v "${HOME}/VirtualBox VMs:${HOME}/VirtualBox VMs"
    -v /usr/lib/virtualbox:/usr/lib/virtualbox:ro
    -v /usr/share/virtualbox:/usr/share/virtualbox:ro
    -v /etc/passwd:/etc/passwd:ro
    -v /etc/group:/etc/group:ro
)

if [[ -t 0 && -t 1 ]]; then
    docker_args+=(-it)
elif [[ "${MODE}" == "provision" ]]; then
    docker_args+=(-i)
fi

if [[ -d /tmp/.X11-unix ]]; then
    docker_args+=(-v /tmp/.X11-unix:/tmp/.X11-unix)
fi

if [[ -n "${XAUTHORITY:-}" && -f "${XAUTHORITY}" ]]; then
    docker_args+=(-e "XAUTHORITY=${XAUTHORITY}" -v "${XAUTHORITY}:${XAUTHORITY}:ro")
elif [[ -f "${HOME}/.Xauthority" ]]; then
    docker_args+=(-e "XAUTHORITY=${HOME}/.Xauthority" -v "${HOME}/.Xauthority:${HOME}/.Xauthority:ro")
fi

for device_path in /dev/vboxdrv /dev/vboxdrvu /dev/vboxnetctl; do
    if [[ -e "${device_path}" ]]; then
        docker_args+=(--device "${device_path}:${device_path}")
    fi
done

if [[ -d /dev/vboxusb ]]; then
    docker_args+=(-v /dev/vboxusb:/dev/vboxusb)
fi

if [[ "${VBOX_DOCKER_PRIVILEGED:-0}" == "1" ]]; then
    docker_args+=(--privileged)
fi

if [[ "${MODE}" == "check" ]]; then
    docker_args+=(
        "${IMAGE_NAME}"
        -c
        "import subprocess; print('VirtualBox:', subprocess.check_output(['VBoxManage', '--version'], text=True).strip()); print(subprocess.check_output(['VBoxManage', 'list', 'systemproperties'], text=True).split('Default machine folder:', 1)[1].splitlines()[0].strip())"
    )
else
    docker_args+=(
        "${IMAGE_NAME}"
        "${PROJECT_ROOT}/scripts/create_windows_vm.py"
        "${CONFIG_PATH}"
    )
fi

docker "${docker_args[@]}"
status=$?

if [[ "${status}" -eq 0 && -n "${CREATED_VM_INFO_FILE}" && -s "${CREATED_VM_INFO_FILE}" ]]; then
    vm_name="$(sed -n '1p' "${CREATED_VM_INFO_FILE}")"
    frontend="$(sed -n '2p' "${CREATED_VM_INFO_FILE}")"
    settings_file="$(sed -n '3p' "${CREATED_VM_INFO_FILE}")"
    start_vm_on_host "${vm_name}" "${frontend}" "${settings_file}"
fi

exit "${status}"
