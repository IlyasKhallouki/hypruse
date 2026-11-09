# Shared settings and helpers for the Omarchy test VM scripts in scripts/vm/.
# Sourced, not run. Every path and number here can be overridden from the
# environment, so one checkout can drive several VMs side by side.
# shellcheck shell=bash

set -euo pipefail

VM_SCRIPTS=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd -- "$VM_SCRIPTS/../.." && pwd)

VM_DIR=${HYPRUSE_VM_DIR:-$HOME/.cache/hypruse-vm}
OMARCHY_VERSION=${OMARCHY_VERSION:-4.5.0}
ISO=${HYPRUSE_VM_ISO:-$VM_DIR/omarchy-$OMARCHY_VERSION-x86.iso}
DISK=${HYPRUSE_VM_DISK:-$VM_DIR/omarchy.qcow2}
# The UEFI variable store holds the boot entry the installer writes. It lives
# outside the qcow2, so the snapshot scripts copy it next to each snapshot.
VARS=${HYPRUSE_VM_VARS:-$VM_DIR/OVMF_VARS.4m.fd}
SNAP_DIR=$VM_DIR/snapshots
OVMF_CODE=${HYPRUSE_VM_OVMF_CODE:-/usr/share/edk2/x64/OVMF_CODE.4m.fd}
OVMF_VARS_TEMPLATE=${HYPRUSE_VM_OVMF_VARS_TEMPLATE:-/usr/share/edk2/x64/OVMF_VARS.4m.fd}

SSH_PORT=${HYPRUSE_VM_SSH_PORT:-2222}
GUEST_USER=${HYPRUSE_VM_USER:-tester}
SSH_KEY=${HYPRUSE_VM_SSH_KEY:-$VM_DIR/id_ed25519}
# the ssh program, for a wrapper or a test double
SSH_BIN=${HYPRUSE_VM_SSH_BIN:-ssh}

PIDFILE=$VM_DIR/qemu.pid
QMP_SOCK=$VM_DIR/qmp.sock
SERIAL_LOG=$VM_DIR/serial.log

die() {
  printf '%s: %s\n' "${0##*/}" "$*" >&2
  exit 1
}

log() {
  printf '%s\n' "$*" >&2
}

need() {
  command -v "$1" >/dev/null 2>&1 || die "$1 not found${2:+ ($2)}"
}

# Print the calling script's header comment, from line 2 to the first line
# that is not a comment.
usage() {
  sed -n '2,/^[^#]/p' "$0" | sed -e '$d' -e 's/^# \{0,1\}//'
}

# Remove these paths when the script exits, however it exits.
CLEANUP=()
cleanup() {
  ((${#CLEANUP[@]})) && rm -rf -- "${CLEANUP[@]}"
  return 0
}
trap cleanup EXIT

human() {
  numfmt --to=iec-i --suffix=B "$1"
}

# Free bytes on the filesystem that holds $1, or would hold it once created.
free_bytes() {
  local dir=$1
  while [[ ! -e $dir ]]; do dir=$(dirname -- "$dir"); done
  df --output=avail -B1 -- "$dir" | tail -1 | tr -d ' '
}

vm_pid() {
  [[ -f $PIDFILE ]] || return 1
  local pid
  pid=$(<"$PIDFILE")
  [[ -n $pid ]] && kill -0 "$pid" 2>/dev/null && printf '%s\n' "$pid"
}

vm_running() {
  vm_pid >/dev/null
}

require_stopped() {
  local pid
  if pid=$(vm_pid); then
    die "the VM is running (pid $pid); shut it down first: scripts/vm/stop"
  fi
}

# Send QMP commands to the running VM, one JSON object per argument, and
# print each reply. Python's standard library is enough, and it is on every
# machine that builds hypruse.
qmp() {
  [[ -S $QMP_SOCK ]] || die "no QMP socket at $QMP_SOCK; is the VM running?"
  python3 -I - "$QMP_SOCK" "$@" <<'PY'
import json, socket, sys

sock = socket.socket(socket.AF_UNIX)
sock.settimeout(10)
sock.connect(sys.argv[1])
reader = sock.makefile("r")

def reply():
    while True:
        msg = json.loads(reader.readline())
        if "event" not in msg:
            return msg

reader.readline()  # the greeting
sock.sendall(b'{"execute": "qmp_capabilities"}\n')
reply()
failed = False
for raw in sys.argv[2:]:
    sock.sendall(raw.encode() + b"\n")
    try:
        msg = reply()
    except (ValueError, OSError):  # "quit" closes the socket before or after replying
        break
    print(json.dumps(msg))
    failed |= "error" in msg
sys.exit(1 if failed else 0)
PY
}

ensure_key() {
  [[ -f $SSH_KEY ]] && return
  need ssh-keygen
  mkdir -p -- "$(dirname -- "$SSH_KEY")"
  ssh-keygen -q -t ed25519 -N '' -C hypruse-vm -f "$SSH_KEY"
  log "created the VM ssh key $SSH_KEY"
}

# The guest is rebuilt and reset often, so its host key changes; it is only
# ever reached on 127.0.0.1, so known_hosts is not consulted.
SSH_OPTS=(
  -i "$SSH_KEY" -p "$SSH_PORT"
  -o IdentitiesOnly=yes -o BatchMode=yes
  -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
  -o ConnectTimeout=5 -o LogLevel=ERROR
)

guest_ssh() {
  "$SSH_BIN" "${SSH_OPTS[@]}" "$GUEST_USER@127.0.0.1" "$@"
}
