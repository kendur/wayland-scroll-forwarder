#!/usr/bin/env bash
# Install or update the persistent forwarder for the current desktop user.
#
# - Installs dist/wayland-scroll-forwarder (built by ./build.sh) or, if no build
#   exists, the plain script, to ~/.local/bin/wayland-scroll-forwarder.
# - Installs the GUI, its desktop entry, and a copy of this installer plus the
#   config templates under ~/.local/share/wayland-scroll-forwarder, so the GUI
#   keeps working if the checkout moves.
# - Installs and (re)starts the user service.
# - Installs the udev rule and boot-refresh service. This is the only part that
#   needs root, and it is done in ONE privileged call (one authentication).
#   If those system files are already correct, nothing is asked at all, so
#   re-running after a rebuild is prompt-free.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly RULE_TARGET="/etc/udev/rules.d/70-wayland-scroll-forwarder.rules"
readonly REFRESH_SERVICE_NAME="wayland-scroll-forwarder-udev-refresh.service"
readonly REFRESH_SERVICE_TARGET="/etc/systemd/system/${REFRESH_SERVICE_NAME}"
readonly SERVICE_TARGET="${HOME}/.config/systemd/user/wayland-scroll-forwarder.service"
readonly BIN_TARGET="${HOME}/.local/bin/wayland-scroll-forwarder"
readonly GUI_TARGET="${HOME}/.local/bin/wayland-scroll-forwarder-gui"
readonly DESKTOP_TARGET="${HOME}/.local/share/applications/wayland-scroll-forwarder-gui.desktop"
readonly SHARE_DIR="${HOME}/.local/share/wayland-scroll-forwarder"

usage() {
  cat <<'USAGE'
Usage: install-persistent.sh [--privesc pkexec|sudo|none] [--print-rule]
                             ["[KIND:]EXACT DEVICE NAME" ...]

KIND selects which interface of the device is covered, and defaults to mouse:
  mouse:NAME   the node udev classifies as a mouse (ID_INPUT_MOUSE)
  key:NAME     the node udev classifies as a keyboard/keypad (ID_INPUT_KEY) -
               gaming keypads and unifying receivers put the wheel there
  any:NAME     every node reporting that exact kernel name

Example:
  install-persistent.sh "Naga V2 Pro Mouse" "key:Razer Razer Tartarus V2"

Every named device is covered; the forwarder follows whichever ones are
connected. Omit the names to reuse the ones in the installed udev rule.

--privesc controls how the one privileged step authenticates:
  pkexec  the desktop's polkit dialog (default)
  sudo    reads the password from stdin (used by the GUI when a polkit dialog
          would be hidden behind a fullscreen game)
  none    do the user-level half only; exit 3 if the system files need root

--print-rule writes the udev rule that would be installed to stdout and exits,
changing nothing.
USAGE
}

if [[ ${EUID} -eq 0 ]]; then
  printf 'Run this installer as the desktop user, not as root.\n' >&2
  exit 1
fi

PRIVESC=pkexec
PRINT_RULE=0
DEVICE_SPECS=()
while (( $# )); do
  case "$1" in
    --privesc)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      PRIVESC="$2"
      shift 2
      ;;
    --privesc=*)
      PRIVESC="${1#--privesc=}"
      shift
      ;;
    --print-rule)
      PRINT_RULE=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    --)
      shift
      DEVICE_SPECS+=("$@")
      break
      ;;
    *)
      DEVICE_SPECS+=("$1")
      shift
      ;;
  esac
done
case "${PRIVESC}" in
  pkexec|sudo|none) ;;
  *) printf 'Unknown --privesc value %q (use pkexec, sudo or none).\n' "${PRIVESC}" >&2; exit 2 ;;
esac
readonly PRIVESC

# The installed rule is the only place the device list persists, so reusing it
# is the right default for a plain rebuild - but it silently narrows coverage
# after a failed install, which is why the GUI always passes every device.
if (( ${#DEVICE_SPECS[@]} == 0 )) && [[ -r ${RULE_TARGET} ]]; then
  mapfile -t DEVICE_SPECS < <(
    grep -v '^#' "${RULE_TARGET}" |
      while IFS= read -r line; do
        [[ ${line} == *'ATTRS{name}=='* ]] || continue
        name="${line#*ATTRS\{name\}==\"}"
        name="${name%%\"*}"
        if [[ ${line} == *'ID_INPUT_MOUSE}=="1"'* ]]; then
          printf 'mouse:%s\n' "${name}"
        elif [[ ${line} == *'ID_INPUT_KEY}=="1"'* ]]; then
          printf 'key:%s\n' "${name}"
        else
          printf 'any:%s\n' "${name}"
        fi
      done
  )
fi
if (( ${#DEVICE_SPECS[@]} == 0 )); then
  usage >&2
  exit 2
fi

# Split each spec into kind + exact kernel name, and validate the name hard:
# it is interpolated into a udev rule, so anything that could add a match key
# or a second rule line has to be rejected rather than escaped.
VALIDATED=()
for spec in "${DEVICE_SPECS[@]}"; do
  kind=mouse
  name="${spec}"
  case "${spec}" in
    mouse:*) kind=mouse; name="${spec#mouse:}" ;;
    key:*)   kind=key;   name="${spec#key:}" ;;
    any:*)   kind=any;   name="${spec#any:}" ;;
  esac
  if (( ${#name} > 127 )) || \
    ! printf '%s\n' "${name}" | LC_ALL=C grep -Eq '^[[:alnum:]][[:alnum:] ._:+-]*$'; then
    printf 'Device name %q contains unsupported characters.\n' "${name}" >&2
    exit 2
  fi
  VALIDATED+=("${kind}:${name}")
done

# Emit the rule in a canonical order, so the same set of devices given in a
# different order produces a byte-identical file. Without this, merely
# reordering the selection would rewrite /etc and demand authentication for a
# change that is not one. Names are validated above to a set that excludes
# newlines, so sorting them line-wise is safe.
mapfile -t VALIDATED < <(printf '%s\n' "${VALIDATED[@]}" | LC_ALL=C sort -u)
DEVICE_KINDS=()
DEVICE_NAMES=()
for spec in "${VALIDATED[@]}"; do
  DEVICE_KINDS+=("${spec%%:*}")
  DEVICE_NAMES+=("${spec#*:}")
done
readonly DEVICE_KINDS DEVICE_NAMES

readonly TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEMP_DIR}"' EXIT

# The template is a comment header plus one rule line carrying @DEVICE_MATCH@;
# emit the header once and the rule line once per device, substituting the
# interface-class match keys and the validated name.
readonly RULE_TEMPLATE="${SCRIPT_DIR}/config/70-wayland-scroll-forwarder.rules.in"
readonly GENERATED_RULE="${TEMP_DIR}/70-wayland-scroll-forwarder.rules"
rule_line="$(grep -v '^#' "${RULE_TEMPLATE}")"
grep '^#' "${RULE_TEMPLATE}" > "${GENERATED_RULE}"
for index in "${!DEVICE_NAMES[@]}"; do
  case "${DEVICE_KINDS[index]}" in
    mouse) match="ENV{ID_INPUT_MOUSE}==\"1\", ATTRS{name}==\"${DEVICE_NAMES[index]}\"" ;;
    key)   match="ENV{ID_INPUT_KEY}==\"1\", ATTRS{name}==\"${DEVICE_NAMES[index]}\"" ;;
    any)   match="ATTRS{name}==\"${DEVICE_NAMES[index]}\"" ;;
  esac
  printf '%s\n' "${rule_line//@DEVICE_MATCH@/${match}}" >> "${GENERATED_RULE}"
done

if (( PRINT_RULE )); then
  cat "${GENERATED_RULE}"
  exit 0
fi

# --- user-level files (no privileges needed) --------------------------------
# The GUI runs this installer from ${SHARE_DIR}, where there is no build and no
# source script; in that case the already-installed binary is kept as it is.
if [[ -x ${SCRIPT_DIR}/dist/wayland-scroll-forwarder ]]; then
  install -Dm755 "${SCRIPT_DIR}/dist/wayland-scroll-forwarder" "${BIN_TARGET}"
  printf 'Installing self-contained build.\n'
elif [[ -f ${SCRIPT_DIR}/scroll_forwarder.py ]]; then
  install -Dm755 "${SCRIPT_DIR}/scroll_forwarder.py" "${BIN_TARGET}"
  printf 'No dist/ build found; installing the script (needs python3-evdev and python3-xlib).\n'
elif [[ -x ${BIN_TARGET} ]]; then
  printf 'Keeping the installed forwarder binary.\n'
else
  printf 'No forwarder to install: run ./build.sh in a checkout first.\n' >&2
  exit 1
fi
install -Dm644 "${SCRIPT_DIR}/config/wayland-scroll-forwarder.service" "${SERVICE_TARGET}"

# Keep a self-sufficient copy so the GUI can re-run the privileged half even if
# this checkout is moved or deleted.
install -Dm755 "${SCRIPT_DIR}/install-persistent.sh" "${SHARE_DIR}/install-persistent.sh"
install -Dm644 "${RULE_TEMPLATE}" "${SHARE_DIR}/config/70-wayland-scroll-forwarder.rules.in"
install -Dm644 "${SCRIPT_DIR}/config/wayland-scroll-forwarder.service" "${SHARE_DIR}/config/wayland-scroll-forwarder.service"
install -Dm644 "${SCRIPT_DIR}/config/${REFRESH_SERVICE_NAME}" "${SHARE_DIR}/config/${REFRESH_SERVICE_NAME}"
if [[ -f ${SCRIPT_DIR}/scroll_forwarder_gui.py ]]; then
  install -Dm755 "${SCRIPT_DIR}/scroll_forwarder_gui.py" "${GUI_TARGET}"
  install -Dm644 "${SCRIPT_DIR}/config/wayland-scroll-forwarder-gui.desktop" "${DESKTOP_TARGET}"
fi

# --- system files: skip entirely when already correct -----------------------
system_ok=1
cmp -s "${GENERATED_RULE}" "${RULE_TARGET}" 2>/dev/null || system_ok=0
cmp -s "${SCRIPT_DIR}/config/${REFRESH_SERVICE_NAME}" "${REFRESH_SERVICE_TARGET}" 2>/dev/null || system_ok=0
systemctl is-enabled --quiet "${REFRESH_SERVICE_NAME}" 2>/dev/null || system_ok=0

if (( system_ok )); then
  printf 'udev rule and boot-refresh service already installed; no authentication needed.\n'
elif [[ ${PRIVESC} == none ]]; then
  # Exit 3 means exactly "the system files differ and root is needed to fix
  # them". The GUI runs this first so it only ever asks for a password when
  # that is true; everything user-level above has already been done.
  printf 'System files need updating; rerun with --privesc pkexec or sudo.\n' >&2
  exit 3
else
  cp "${SCRIPT_DIR}/config/${REFRESH_SERVICE_NAME}" "${TEMP_DIR}/"
  cat > "${TEMP_DIR}/root-steps.sh" <<ROOT
#!/usr/bin/env bash
set -euo pipefail
install -Dm644 "${GENERATED_RULE}" "${RULE_TARGET}"
install -Dm644 "${TEMP_DIR}/${REFRESH_SERVICE_NAME}" "${REFRESH_SERVICE_TARGET}"
udevadm control --reload-rules
udevadm trigger --subsystem-match=input --action=add
systemctl daemon-reload
systemctl enable "${REFRESH_SERVICE_NAME}"
ROOT
  chmod 755 "${TEMP_DIR}/root-steps.sh"
  chmod 755 "${TEMP_DIR}"   # the privileged step runs as root; let it read the staged files
  if [[ ${PRIVESC} == sudo ]]; then
    # Password arrives on stdin from the caller; -k ignores any cached ticket so
    # a wrong password fails here instead of silently succeeding from a cache.
    printf 'Authenticating with sudo to install the narrowly scoped udev rule.\n'
    sudo -S -k -p '' -- "${TEMP_DIR}/root-steps.sh"
  else
    printf 'Authentication is required once to install the narrowly scoped udev rule.\n'
    pkexec "${TEMP_DIR}/root-steps.sh"
  fi
fi

# --- (re)start the user service ---------------------------------------------
systemctl --user daemon-reload
systemctl --user enable wayland-scroll-forwarder.service >/dev/null 2>&1 || true
systemctl --user restart wayland-scroll-forwarder.service

printf '\nInstalled persistent forwarder for:\n'
for index in "${!DEVICE_NAMES[@]}"; do
  printf '  %-5s %s\n' "${DEVICE_KINDS[index]}" "${DEVICE_NAMES[index]}"
done
printf 'Binary: %s\n' "${BIN_TARGET}"
printf 'Stable devices: /dev/input/wsf/ (one entry per connected device above)\n'
printf 'Service: wayland-scroll-forwarder.service (systemctl --user status wayland-scroll-forwarder)\n'
printf 'Boot refresh: %s\n' "${REFRESH_SERVICE_NAME}"
