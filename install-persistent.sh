#!/usr/bin/env bash
# Install or update the persistent forwarder for the current desktop user.
#
# - Installs dist/wayland-scroll-forwarder (built by ./build.sh) or, if no build
#   exists, the plain script, to ~/.local/bin/wayland-scroll-forwarder.
# - Installs and (re)starts the user service.
# - Installs the udev rule and boot-refresh service. This is the only part that
#   needs root, and it is done in ONE pkexec call (one authentication dialog).
#   If those system files are already correct, no dialog appears at all, so
#   re-running after a rebuild is prompt-free.
set -euo pipefail

readonly SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
readonly RULE_TARGET="/etc/udev/rules.d/70-wayland-scroll-forwarder.rules"
readonly REFRESH_SERVICE_NAME="wayland-scroll-forwarder-udev-refresh.service"
readonly REFRESH_SERVICE_TARGET="/etc/systemd/system/${REFRESH_SERVICE_NAME}"
readonly SERVICE_TARGET="${HOME}/.config/systemd/user/wayland-scroll-forwarder.service"
readonly BIN_TARGET="${HOME}/.local/bin/wayland-scroll-forwarder"

usage() {
  printf 'Usage: %s "EXACT INPUT DEVICE NAME" ["ANOTHER NAME" ...]\n' "${0##*/}"
  printf 'Example: %s "Naga V2 Pro Mouse" "Logitech Wireless Mouse MX Master 3"\n' "${0##*/}"
  printf 'Every named mouse is covered; the forwarder follows whichever ones are connected.\n'
  printf 'Omit the names to reuse the ones in the installed udev rule.\n'
}

if [[ ${EUID} -eq 0 ]]; then
  printf 'Run this installer as the desktop user, not as root.\n' >&2
  exit 1
fi

DEVICE_NAMES=("$@")
if (( ${#DEVICE_NAMES[@]} == 0 )) && [[ -r ${RULE_TARGET} ]]; then
  # Reuse every mouse already covered by the installed rule, in rule order.
  mapfile -t DEVICE_NAMES < <(sed -n 's/.*ATTRS{name}=="\([^"]*\)".*/\1/p' "${RULE_TARGET}")
fi
if (( ${#DEVICE_NAMES[@]} == 0 )); then
  usage >&2
  exit 2
fi

for name in "${DEVICE_NAMES[@]}"; do
  if (( ${#name} > 127 )) || \
    ! printf '%s\n' "${name}" | LC_ALL=C grep -Eq '^[[:alnum:]][[:alnum:] ._:+-]*$'; then
    printf 'Device name %q contains unsupported characters.\n' "${name}" >&2
    exit 2
  fi
done
readonly DEVICE_NAMES

readonly TEMP_DIR="$(mktemp -d)"
trap 'rm -rf -- "${TEMP_DIR}"' EXIT

# The template is a comment header plus one rule line carrying @DEVICE_NAME@;
# emit the header once and the rule line once per mouse. The validation above
# excludes sed replacement metacharacters except spaces, so substituting each
# exact kernel-reported device name is deterministic.
readonly RULE_TEMPLATE="${SCRIPT_DIR}/config/70-wayland-scroll-forwarder.rules.in"
readonly GENERATED_RULE="${TEMP_DIR}/70-wayland-scroll-forwarder.rules"
grep '^#' "${RULE_TEMPLATE}" > "${GENERATED_RULE}"
for name in "${DEVICE_NAMES[@]}"; do
  grep -v '^#' "${RULE_TEMPLATE}" | sed "s/@DEVICE_NAME@/${name}/g" >> "${GENERATED_RULE}"
done

# --- user-level files (no privileges needed) --------------------------------
if [[ -x ${SCRIPT_DIR}/dist/wayland-scroll-forwarder ]]; then
  SOURCE_BIN="${SCRIPT_DIR}/dist/wayland-scroll-forwarder"
  printf 'Installing self-contained build.\n'
else
  SOURCE_BIN="${SCRIPT_DIR}/scroll_forwarder.py"
  printf 'No dist/ build found; installing the script (needs python3-evdev and python3-xlib).\n'
fi
install -Dm755 "${SOURCE_BIN}" "${BIN_TARGET}"
install -Dm644 "${SCRIPT_DIR}/config/wayland-scroll-forwarder.service" "${SERVICE_TARGET}"

# --- system files: skip entirely when already correct -----------------------
system_ok=1
cmp -s "${GENERATED_RULE}" "${RULE_TARGET}" 2>/dev/null || system_ok=0
cmp -s "${SCRIPT_DIR}/config/${REFRESH_SERVICE_NAME}" "${REFRESH_SERVICE_TARGET}" 2>/dev/null || system_ok=0
systemctl is-enabled --quiet "${REFRESH_SERVICE_NAME}" 2>/dev/null || system_ok=0

if (( system_ok )); then
  printf 'udev rule and boot-refresh service already installed; no authentication needed.\n'
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
  chmod 755 "${TEMP_DIR}"   # pkexec runs as root; let it read the staged files
  printf 'Authentication is required once to install the narrowly scoped udev rule.\n'
  pkexec "${TEMP_DIR}/root-steps.sh"
fi

# --- (re)start the user service ---------------------------------------------
systemctl --user daemon-reload
systemctl --user enable wayland-scroll-forwarder.service >/dev/null 2>&1 || true
systemctl --user restart wayland-scroll-forwarder.service

printf '\nInstalled persistent forwarder for: %s\n' "$(printf '%s, ' "${DEVICE_NAMES[@]}" | sed 's/, $//')"
printf 'Binary: %s\n' "${BIN_TARGET}"
printf 'Stable devices: /dev/input/wsf/ (one entry per connected mouse above)\n'
printf 'Service: wayland-scroll-forwarder.service (systemctl --user status wayland-scroll-forwarder)\n'
printf 'Boot refresh: %s\n' "${REFRESH_SERVICE_NAME}"
