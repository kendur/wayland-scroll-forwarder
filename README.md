# Hardened Wayland Scroll Forwarder

This is a security-focused fork of
[`enexam/wayland-scroll-forwarder`](https://github.com/enexam/wayland-scroll-forwarder).
It forwards wheel events from explicitly selected physical devices only while
an exact X11/Xwayland `WM_CLASS` is focused. A GTK4 desktop front end
(`scroll_forwarder_gui.py`) manages the device list, the runtime options and the
diagnostics.

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

## Graphical front end

```bash
wayland-scroll-forwarder-gui     # or: ./scroll_forwarder_gui.py
```

Installed by `install-persistent.sh` together with a **Scroll Forwarder** entry
in the desktop menu. It gives you:

- **Devices** - every peripheral that could plausibly scroll, one row per
  *interface*, with its bus, its current event node and whether it is readable
  yet. Wheel-capable interfaces are listed first; "Show every interface" reveals
  the rest. "Watch wheel activity" counts wheel events live, which identifies an
  unlabelled device and proves whether one is sending anything at all.
- **Options** - target `WM_CLASS`, the two runtime flags and autostart. These
  are written to an environment file read by the user service, so changing them
  needs no root and no reinstall.
- **Diagnostics** - the chain that has to hold for scrolling to arrive: service
  running, rule installed, udev applied it, devices readable, injection accepted
  by Xwayland. Plus the last 200 service log lines, and a "Copy diagnostics"
  action.
- **Why this exists** - a plain explanation of the underlying problem and of
  every permission the tool asks for.

It needs the host's `python3-gobject` (GTK 4 and libadwaita); it is deliberately
not bundled by PyInstaller, which would mean shipping all of GTK.

### Peripherals other than mice

A wheel is not always on a mouse interface. Gaming keypads (Razer Tartarus) and
unifying receivers report theirs on an interface udev classifies as a *keyboard*,
which `ENV{ID_INPUT_MOUSE}=="1"` can never match. Device selections therefore
carry an interface class, on the command line as a prefix:

| spec | matches |
| --- | --- |
| `"Naga V2 Pro Mouse"` or `mouse:...` | `ENV{ID_INPUT_MOUSE}=="1"` (default) |
| `key:"Razer Razer Tartarus V2"` | `ENV{ID_INPUT_KEY}=="1"` |
| `any:...` | every interface with that exact name |

Granting access to a keyboard-class interface also lets programs running as you
read what that device *types*. The GUI says so on the row and again when you
enable one; only do it for a peripheral whose wheel you actually need.

### Administrator access

Only the udev rule needs root, and the GUI is built so that it asks as rarely
and holds as little as possible:

1. **The unprivileged half runs first.** Every apply starts with
   `install-persistent.sh --privesc none`, which does the user-level work and
   exits 3 only if the files under `/etc` genuinely differ. A rebuild, or a
   selection that already matches the installed rule, never reaches an
   authentication step - so no password is requested, entered or held for it.
2. **System dialog (default).** polkit's own prompt; the password never passes
   through this program.
3. **Password entered in the window.** For the one case the dialog cannot
   handle: it opens *behind* a fullscreen game, so applying appears to hang.
   The password goes straight to `sudo` on standard input - never on a command
   line, where any process could read it from `/proc`. It is cleared from the
   window as soon as the command returns, and optionally kept **in memory only**
   until the window closes, for applying several changes in a row.

Writing the password to the desktop keyring was deliberately **not** built.
While the keyring is unlocked, anything running as you can read it back, which
is close to giving the account passwordless root; a scoped `NOPASSWD` sudoers
entry or a permissive polkit rule has the same problem in a different place,
since either would let any process of yours install arbitrary udev rules - root
code execution. Device changes are rare enough that none of that is worth it.
If an earlier build saved one, the Options page offers to delete it.

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

- a udev rule per named device, matching the exact kernel device name and the
  chosen interface class
- a stable symlink for each one under `/dev/input/wsf/`
- a `uaccess` grant for the active desktop user only
- a system one-shot service that refreshes input-device rules after udev and
  Bluetooth start during boot
- a persistent user service that monitors every mouse in `/dev/input/wsf`, waits
  quietly while they are absent, and picks each one up again as it reconnects

Name every mouse you use, not just one: the forwarder follows whichever of them
are connected, so switching mice mid-session keeps scrolling working. First
identify the exact names with `--list-devices`, then install. For example:

```bash
sudo ./scroll_forwarder.py --list-devices
./build.sh
./install-persistent.sh "Naga V2 Pro Mouse" "Logitech Wireless Mouse MX Master 3" \
  "key:Razer Razer Tartarus V2"
```

The runtime flags and the target window class live in
`~/.config/wayland-scroll-forwarder/forwarder.env`, read by the user service:

```ini
WSF_OPTIONS=--allow-unfocused --wait-for-device
WSF_WINDOW_CLASS=GeForceNOW
```

Changing them needs no root; `systemctl --user restart wayland-scroll-forwarder`
applies them (the GUI does both for you).

A single authentication dialog covers the udev rule, the boot-refresh service
and the udev reload; everything else runs as the desktop user. Re-running the
installer after a rebuild (`./install-persistent.sh`, the device names are read
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
python3 -m py_compile scroll_forwarder.py scroll_forwarder_gui.py
```

The GUI tests are skipped automatically on a host without `python3-gobject`.

## Limitations

XTest synthesizes wheel buttons into Xwayland globally. Focus checking minimizes
misdelivery but cannot make XTest a true per-window injection API. The program
does not grab or suppress the original device, so an application that starts
receiving native wheel events may see duplicates; stop the forwarder after an
upstream fix.

This workaround is not endorsed by NVIDIA and has not been evaluated against
individual games' anti-cheat systems.
