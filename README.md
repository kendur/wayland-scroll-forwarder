# Hardened Wayland Scroll Forwarder

This is a security-focused fork of
[`enexam/wayland-scroll-forwarder`](https://github.com/enexam/wayland-scroll-forwarder).
It forwards wheel events from one explicitly selected physical device only while
an exact X11/Xwayland `WM_CLASS` is focused.

## Security changes

- Requires an explicit input-device path; it never reads every input device.
- Permanently drops root and supplementary groups immediately after opening that device.
- Refuses direct root execution; privileged fallback must come from a desktop user via `sudo`.
- Requires an exact, case-insensitive `WM_CLASS` match.
- Checks `_NET_ACTIVE_WINDOW`; a merely visible target receives nothing.
- De-duplicates paired legacy/high-resolution wheel events.
- Caps synthetic events from a single input report.
- Downloads or executes no remote content and creates no persistence.

The safest configuration is to give the logged-in user read access to one stable
mouse event device and run the forwarder without `sudo`. The sudo fallback still
drops privileges, but opening the device through a privileged Python process has
a larger attack surface than a device ACL.

## Dependencies

Running the self-contained build (`./build.sh`, see below) needs nothing beyond
glibc, udev and Xwayland. Running the plain script needs:

- Python 3
- `python-evdev`
- `python-xlib`

Fedora/Bazzite currently provides the required Python modules on the host. Other
distributions can use their native packages listed by the upstream project.

## Self-contained build

```bash
./build.sh          # -> dist/wayland-scroll-forwarder (PyInstaller, ~11 MB)
```

The build virtualenv inherits the host's `python3-evdev` / `python3-xlib`
(Fedora Atomic ships no Python headers, so `pip` cannot compile evdev) and
PyInstaller bundles them. The installer below prefers `dist/` when it exists, so
the user service no longer depends on host Python modules surviving an image
update.

## Usage

Discover the mouse event device:

```bash
sudo ./scroll_forwarder.py --list-devices
```

Prefer a stable `/dev/input/by-id/...-event-mouse` path from the output. Then run:

```bash
sudo --preserve-env=DISPLAY,XAUTHORITY ./scroll_forwarder.py \
  --device /dev/input/by-id/YOUR-MOUSE-event-mouse GeForceNOW
```

The script opens only that device, drops to `SUDO_UID`, and then connects to X11.
It waits for GFN, forwards only while GFN is focused, and exits after the target
window closes.

Some compositors report an unclassified Xwayland proxy as active after a game
grabs the pointer. If wheel events are detected but rejected by focus checking,
use the explicit compatibility mode:

```bash
./scroll_forwarder.py --allow-unfocused \
  --device /dev/input/by-id/YOUR-MOUSE-event-mouse GeForceNOW
```

This forwards whenever the matching GFN window exists, including when another
application is focused. Use it only for the duration of the GFN session.

## Persistent installation

Input event numbers such as `/dev/input/event19` are not stable: they can change
after rebooting or reconnecting a Bluetooth mouse. The included installer creates:

- a udev rule matching the exact kernel device name and mouse interface
- the stable symlink `/dev/input/wayland-scroll-forwarder-mouse`
- a `uaccess` grant for the active desktop user only
- a system one-shot service that refreshes input-device rules after udev and
  Bluetooth start during boot
- a persistent user service that waits quietly while the mouse is absent and
  resumes automatically after it reconnects

First identify the exact mouse name with `--list-devices`, then install. For example:

```bash
sudo ./scroll_forwarder.py --list-devices
./build.sh
./install-persistent.sh "Naga V2 Pro Mouse"
```

A single authentication dialog covers the udev rule, the boot-refresh service
and the udev reload; everything else runs as the desktop user. Re-running the
installer after a rebuild (`./install-persistent.sh`, the device name is read
from the installed rule) shows no dialog at all when the system files are
already correct. Check or stop the forwarder with:

```bash
systemctl --user status wayland-scroll-forwarder
systemctl --user stop wayland-scroll-forwarder
```

Disable persistent startup with:

```bash
systemctl --user disable --now wayland-scroll-forwarder
```

To remove the system setup as well, disable and delete
`wayland-scroll-forwarder-udev-refresh.service`, delete
`/etc/udev/rules.d/70-wayland-scroll-forwarder.rules`, then reload systemd and
udev.

For completely unprivileged operation, grant the active desktop user a device ACL
(temporary until reconnect/reboot):

```bash
sudo setfacl -m "u:$USER:r" /dev/input/by-id/YOUR-MOUSE-event-mouse
./scroll_forwarder.py --device /dev/input/by-id/YOUR-MOUSE-event-mouse GeForceNOW
```

Do not grant access to keyboard event devices or add the user broadly to the
`input` group.

## Tests

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile scroll_forwarder.py
```

## Limitations

XTest synthesizes wheel buttons into Xwayland globally. Focus checking minimizes
misdelivery but cannot make XTest a true per-window injection API. The program
does not grab or suppress the original device, so an application that starts
receiving native wheel events may see duplicates; stop the forwarder after an
upstream fix.

This workaround is not endorsed by NVIDIA and has not been evaluated against
individual games' anti-cheat systems.
