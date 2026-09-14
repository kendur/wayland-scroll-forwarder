#!/usr/bin/env bash
# Build the self-contained executable: dist/wayland-scroll-forwarder
# python-evdev has a C extension and Fedora Atomic ships no Python headers, so the
# build venv inherits the host's python3-evdev / python3-xlib packages and
# PyInstaller bundles them. Result needs no Python modules on the target machine.
set -euo pipefail
cd "$(dirname "$0")"
python3 -c 'import evdev, Xlib' 2>/dev/null || {
  echo "Install python3-evdev and python3-xlib on the host first (Fedora/Bazzite ship them)." >&2
  exit 1
}
if [ ! -x .venv/bin/python ]; then
  echo "Creating build virtualenv..."
  python3 -m venv --system-site-packages .venv
fi
./.venv/bin/pip install --quiet --upgrade pip pyinstaller
./.venv/bin/python -m unittest discover -s tests
./.venv/bin/pyinstaller --clean --noconfirm wayland-scroll-forwarder.spec
echo
echo "Built: $(pwd)/dist/wayland-scroll-forwarder  ($(du -h dist/wayland-scroll-forwarder | cut -f1))"
echo "Install/update: ./install-persistent.sh \"EXACT INPUT DEVICE NAME\""
