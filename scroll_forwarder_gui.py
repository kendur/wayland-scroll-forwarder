#!/usr/bin/env python3
"""Desktop front end for the hardened Wayland scroll forwarder.

The forwarder itself is a small daemon that reads wheel events from explicitly
selected input devices and replays them into a focused Xwayland window.  Doing
that persistently needs three things kept in sync: a udev rule naming the
devices (root), a user service carrying the runtime flags (no root), and the
devices actually being connected (neither).  Getting one of the three wrong is
the usual cause of "scrolling stopped working", and none of them are visible
from a desktop.

This window makes all three visible, lets any wheel-bearing peripheral be
picked - not just mice, because gaming keypads and unifying receivers put the
wheel on an interface udev classifies as a keyboard - and keeps the privileged
half to a single authenticated call, exactly as install-persistent.sh does.

Runs on the host's python3-gobject (GTK 4 + libadwaita); it is deliberately not
bundled by PyInstaller, which would mean shipping all of GTK.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, GLib, Gtk  # noqa: E402


APP_ID = "io.github.kendur.WaylandScrollForwarder"
SERVICE = "wayland-scroll-forwarder.service"
REFRESH_SERVICE = "wayland-scroll-forwarder-udev-refresh.service"
RULE_PATH = Path("/etc/udev/rules.d/70-wayland-scroll-forwarder.rules")
DEVICE_DIR = Path("/dev/input/wsf")
PROC_DEVICES = Path("/proc/bus/input/devices")
ENV_FILE = Path.home() / ".config/wayland-scroll-forwarder/forwarder.env"
SHARE_DIR = Path.home() / ".local/share/wayland-scroll-forwarder"
BIN_TARGET = Path.home() / ".local/bin/wayland-scroll-forwarder"
DEFAULT_WINDOW_CLASS = "GeForceNOW"

# Keyring attributes; these identify our one secret in libsecret/KWallet.
SECRET_ATTRS = ["service", "wayland-scroll-forwarder", "user", os.environ.get("USER", "")]

# REL_* codes that mean "this interface reports a wheel".  Checked against the
# capability bitmask in /proc/bus/input/devices, which is world-readable - so
# devices we have no permission to open are still classified correctly.
REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 6, 8, 11, 12
WHEEL_BITS = (REL_HWHEEL, REL_WHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES)

KIND_LABELS = {
    "mouse": "mouse interface",
    "key": "keyboard/keypad interface",
    "any": "every interface with this name",
    "other": "other interface",
}


# --------------------------------------------------------------------------
# Device discovery
# --------------------------------------------------------------------------


def _mask_value(text: str) -> int:
    """Turn a /proc/bus/input/devices capability mask into an integer.

    The kernel prints the bitmask as space-separated hex longs, most
    significant word first, so the words are recombined in reverse.
    """
    value = 0
    for index, word in enumerate(reversed(text.split())):
        try:
            value |= int(word, 16) << (64 * index)
        except ValueError:
            continue
    return value


@dataclass
class InputInterface:
    """One kernel input device (one /dev/input/eventN node)."""

    name: str
    node: str
    wheel: bool
    kind: str = "other"
    bus: str = ""

    @property
    def readable(self) -> bool:
        return os.access(self.node, os.R_OK)

    @property
    def linked(self) -> bool:
        return (DEVICE_DIR / Path(self.node).name).exists()


def parse_proc_devices(text: str) -> list[InputInterface]:
    """Parse /proc/bus/input/devices into one entry per event node.

    Reading this instead of opening every device keeps the GUI unprivileged:
    it needs to describe devices it has no access to yet, which is exactly the
    state a device is in before the user grants it access here.
    """
    interfaces: list[InputInterface] = []
    name = ""
    handlers: list[str] = []
    rel_mask = 0
    for line in text.splitlines() + [""]:
        if line.startswith("N: Name="):
            name = line.partition("=")[2].strip().strip('"')
        elif line.startswith("H: Handlers="):
            handlers = [word for word in line.partition("=")[2].split() if word.startswith("event")]
        elif line.startswith("B: REL="):
            rel_mask = _mask_value(line.partition("=")[2])
        elif not line.strip():
            wheel = any(rel_mask >> bit & 1 for bit in WHEEL_BITS)
            for handler in handlers:
                interfaces.append(InputInterface(name=name, node=f"/dev/input/{handler}", wheel=wheel))
            name, handlers, rel_mask = "", [], 0
    return interfaces


def udev_info(node: str) -> tuple[str, str]:
    """Classify an event node the way the udev rule will match it, plus its bus.

    The bus is only used to keep the "other interfaces" list down to things a
    person could plausibly scroll with, instead of every lid switch and HDMI
    audio jack the kernel exposes.
    """
    try:
        output = subprocess.run(
            ["udevadm", "info", "--query=property", "--name", node],
            capture_output=True, text=True, timeout=5, check=False,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return "other", ""
    properties = dict(
        line.split("=", 1) for line in output.splitlines() if "=" in line
    )
    bus = properties.get("ID_BUS", "")
    if not bus and properties.get("DEVPATH", "").find("/bluetooth/") >= 0:
        bus = "bluetooth"
    # Mouse first: a node that is both (the MX Master 3 reports keys too) is
    # matched more tightly, and more safely, by ID_INPUT_MOUSE.
    if properties.get("ID_INPUT_MOUSE") == "1":
        return "mouse", bus
    if properties.get("ID_INPUT_KEY") == "1":
        return "key", bus
    return "other", bus


def parse_rule(text: str) -> list[tuple[str, str]]:
    """Read back the (kind, name) pairs from an installed udev rule."""
    selections: list[tuple[str, str]] = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "ATTRS{name}==" not in line:
            continue
        match = re.search(r'ATTRS\{name\}=="([^"]*)"', line)
        if not match:
            continue
        if 'ID_INPUT_MOUSE}=="1"' in line:
            kind = "mouse"
        elif 'ID_INPUT_KEY}=="1"' in line:
            kind = "key"
        else:
            kind = "any"
        selections.append((kind, match.group(1)))
    return selections


@dataclass
class DeviceGroup:
    """What the udev rule can actually address: an exact name plus a class.

    The rule matches ATTRS{name} and an interface class, never an event number,
    because event numbers move on every reconnect.  So this, not the event
    node, is the unit the user selects.
    """

    name: str
    kind: str
    nodes: list[str] = field(default_factory=list)
    wheel: bool = False
    selected: bool = False
    present: bool = False
    bus: str = ""

    @property
    def plausible(self) -> bool:
        """Could a person conceivably scroll with this?

        Anything reporting a wheel qualifies, as does anything already chosen.
        Otherwise it has to be a mouse- or keyboard-class interface on a bus
        peripherals actually use, which drops lid switches, power buttons,
        HDMI audio jacks and the built-in keyboard controller from the list.
        """
        if self.wheel or self.selected:
            return True
        return self.kind in {"mouse", "key"} and self.bus in {"usb", "bluetooth"}

    @property
    def spec(self) -> str:
        return f"{self.kind}:{self.name}"

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)


def discover_devices() -> list[DeviceGroup]:
    """Merge connected interfaces with whatever the installed rule covers."""
    try:
        interfaces = parse_proc_devices(PROC_DEVICES.read_text())
    except OSError:
        interfaces = []
    for interface in interfaces:
        interface.kind, interface.bus = udev_info(interface.node)

    groups: dict[tuple[str, str], DeviceGroup] = {}
    for interface in interfaces:
        group = groups.setdefault(
            (interface.kind, interface.name),
            DeviceGroup(name=interface.name, kind=interface.kind),
        )
        group.nodes.append(interface.node)
        group.wheel = group.wheel or interface.wheel
        group.bus = group.bus or interface.bus
        group.present = True

    try:
        selections = parse_rule(RULE_PATH.read_text())
    except OSError:
        selections = []
    for kind, name in selections:
        group = groups.get((kind, name))
        if group is None:
            # Configured but not connected right now: a sleeping Bluetooth
            # mouse, or a dongle that is currently unplugged.  Keep it listed,
            # otherwise applying any change would silently drop its coverage.
            group = groups.setdefault((kind, name), DeviceGroup(name=name, kind=kind))
        group.selected = True

    def sort_key(group: DeviceGroup) -> tuple:
        return (not group.selected, not group.wheel, not group.present, group.name.lower())

    return sorted(groups.values(), key=sort_key)


# --------------------------------------------------------------------------
# Runtime options (user service environment file - no privileges needed)
# --------------------------------------------------------------------------


@dataclass
class Options:
    window_class: str = DEFAULT_WINDOW_CLASS
    allow_unfocused: bool = True
    wait_for_device: bool = True

    @classmethod
    def load(cls) -> "Options":
        options = cls()
        try:
            text = ENV_FILE.read_text()
        except OSError:
            return options
        values = {}
        for line in text.splitlines():
            if "=" in line and not line.strip().startswith("#"):
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        flags = values.get("WSF_OPTIONS", "")
        options.window_class = values.get("WSF_WINDOW_CLASS", DEFAULT_WINDOW_CLASS) or DEFAULT_WINDOW_CLASS
        options.allow_unfocused = "--allow-unfocused" in flags.split()
        options.wait_for_device = "--wait-for-device" in flags.split()
        return options

    def save(self) -> None:
        flags = []
        if self.allow_unfocused:
            flags.append("--allow-unfocused")
        if self.wait_for_device:
            flags.append("--wait-for-device")
        ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
        ENV_FILE.write_text(
            "# Written by the Scroll Forwarder GUI; read by the user service.\n"
            f"WSF_OPTIONS={' '.join(flags)}\n"
            f"WSF_WINDOW_CLASS={self.window_class}\n"
        )


def valid_window_class(text: str) -> bool:
    """WM_CLASS values are a single token; reject anything that would word-split.

    The value is expanded unquoted in the unit's ExecStart, so whitespace there
    would turn into extra arguments rather than a longer class name.
    """
    return bool(text) and bool(re.fullmatch(r"[A-Za-z0-9._:+-]{1,127}", text))


# --------------------------------------------------------------------------
# systemd, keyring and the privileged apply step
# --------------------------------------------------------------------------


def systemctl(*args: str, user: bool = True) -> subprocess.CompletedProcess:
    command = ["systemctl"] + (["--user"] if user else []) + list(args)
    return subprocess.run(command, capture_output=True, text=True, check=False)


def service_state() -> tuple[str, bool]:
    active = systemctl("is-active", SERVICE).stdout.strip() or "unknown"
    enabled = systemctl("is-enabled", SERVICE).stdout.strip() == "enabled"
    return active, enabled


def journal_tail(lines: int = 200) -> str:
    result = subprocess.run(
        ["journalctl", "--user", "-u", SERVICE, "-n", str(lines), "--no-pager", "-o", "short-iso"],
        capture_output=True, text=True, check=False,
    )
    return result.stdout or result.stderr or "(no log output)"


def keyring_available() -> bool:
    return shutil.which("secret-tool") is not None


def keyring_lookup() -> str:
    """Read a password an *older* build of this GUI may have stored.

    Saving the login password was dropped: while the keyring is unlocked,
    anything running as this user can read it back, which is close to giving the
    account passwordless root. Nothing writes a secret any more - this exists
    only so an already-saved one can be found and offered for deletion.
    """
    if not keyring_available():
        return ""
    result = subprocess.run(
        ["secret-tool", "lookup", *SECRET_ATTRS],
        capture_output=True, text=True, check=False,
    )
    return result.stdout if result.returncode == 0 else ""


def keyring_clear() -> None:
    if keyring_available():
        subprocess.run(["secret-tool", "clear", *SECRET_ATTRS], capture_output=True, check=False)


def installer_path() -> Path | None:
    """Prefer the checkout this file lives in, fall back to the installed copy."""
    for candidate in (Path(__file__).resolve().parent / "install-persistent.sh",
                      SHARE_DIR / "install-persistent.sh"):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


ROOT_NEEDED = 3  # install-persistent.sh --privesc none: system files differ


def run_installer(specs: list[str], privesc: str, password: str = "") -> tuple[int, str]:
    """Run the one installer that owns every privileged step.

    Duplicating its logic here would mean two places that can generate a udev
    rule; instead the GUI supplies the device list and, for the sudo path, the
    password on standard input - never on the command line, where any process on
    the machine could read it back out of /proc.
    """
    installer = installer_path()
    if installer is None:
        return 127, "install-persistent.sh was not found next to the GUI or in ~/.local/share."
    command = [str(installer), "--privesc", privesc, "--", *specs]
    try:
        result = subprocess.run(
            command,
            input=(password + "\n") if privesc == "sudo" else "",
            capture_output=True, text=True, check=False, timeout=180,
        )
    except subprocess.TimeoutExpired:
        return 124, (
            "The installer timed out after 3 minutes.\n"
            "The authentication dialog is probably hidden behind a fullscreen window - "
            "Alt+Tab to it, or switch to the password method in Options."
        )
    except OSError as exc:
        return 1, str(exc)
    return result.returncode, (result.stdout + result.stderr).strip()


def root_step_needed(specs: list[str]) -> tuple[bool, int, str]:
    """Do the unprivileged half, and report whether root is needed at all.

    Most applies change nothing under /etc - a rebuild, or a selection that
    already matches the installed rule - so asking for credentials first would
    collect a password only to throw it away. The installer's no-privilege mode
    answers the question without one ever being held.
    """
    code, output = run_installer(specs, "none")
    return code == ROOT_NEEDED, code, output


# --------------------------------------------------------------------------
# Diagnostics
# --------------------------------------------------------------------------


def xwayland_portal_prompts() -> bool | None:
    """True when Xwayland routes XTest through the remote-desktop portal.

    KWin then pops a "remote control" approval dialog on the first injected
    wheel event after every forwarder restart, and under a fullscreen game that
    dialog is invisible - scrolling just appears dead.
    """
    try:
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            except OSError:
                continue
            if cmdline and cmdline[0].endswith(b"Xwayland"):
                return b"-enable-ei-portal" in cmdline
    except OSError:
        pass
    return None


def find_target_window(window_class: str) -> bool | None:
    """Whether a window with this WM_CLASS exists right now (None: no X11)."""
    try:
        from Xlib import display, error  # noqa: PLC0415 - optional, only for the check
    except ImportError:
        return None
    try:
        connection = display.Display()
    except Exception:  # noqa: BLE001 - Xlib raises several unrelated types here
        return None
    wanted = window_class.casefold()
    try:
        pending = [connection.screen().root]
        while pending:
            window = pending.pop()
            try:
                values = window.get_wm_class() or ()
                if any(value.casefold() == wanted for value in values):
                    return True
                pending.extend(window.query_tree().children)
            except error.XError:
                continue
        return False
    finally:
        connection.close()


@dataclass
class Check:
    title: str
    ok: bool | None  # None: advisory, nothing is broken
    detail: str


def run_diagnostics(groups: list[DeviceGroup], options: Options) -> list[Check]:
    checks: list[Check] = []

    active, enabled = service_state()
    checks.append(Check(
        "Forwarder service",
        active == "active",
        f"{active}; {'starts at login' if enabled else 'NOT enabled at login'}",
    ))

    checks.append(Check(
        "Forwarder program",
        BIN_TARGET.is_file(),
        str(BIN_TARGET) if BIN_TARGET.is_file() else f"{BIN_TARGET} is missing - run ./build.sh && ./install-persistent.sh",
    ))

    selected = [group for group in groups if group.selected]
    checks.append(Check(
        "Device rule",
        bool(selected) and RULE_PATH.is_file(),
        f"{len(selected)} device(s) covered by {RULE_PATH}" if RULE_PATH.is_file()
        else "no udev rule installed - nothing will be readable",
    ))

    links = sorted(path.name for path in DEVICE_DIR.iterdir()) if DEVICE_DIR.is_dir() else []
    connected = [group for group in selected if group.present]
    expected = sum(len(group.nodes) for group in connected)
    checks.append(Check(
        "Stable device links",
        bool(links) and len(links) >= expected,
        f"{DEVICE_DIR}: {', '.join(links) or 'empty'} (expected {expected})"
        if DEVICE_DIR.is_dir() else
        f"{DEVICE_DIR} does not exist - udev did not run the rule for the current events",
    ))

    unreadable = [
        node for group in connected for node in group.nodes if not os.access(node, os.R_OK)
    ]
    checks.append(Check(
        "Read access",
        not unreadable,
        "every selected device is readable" if not unreadable
        else f"no access yet: {', '.join(unreadable)} - apply the device list, or reconnect them",
    ))

    portal = xwayland_portal_prompts()
    if portal is None:
        checks.append(Check("Xwayland input injection", None, "no Xwayland process found"))
    elif portal:
        checks.append(Check(
            "Xwayland input injection", False,
            "Xwayland runs with -enable-ei-portal, so KWin asks for 'remote control' "
            "approval on the first scroll after every restart, and the dialog hides "
            "behind fullscreen games. Fix: set XwaylandEisNoPrompt=true in the "
            "[Xwayland] group of ~/.config/kwinrc, then log out and back in.",
        ))
    else:
        checks.append(Check("Xwayland input injection", True, "direct XTest, no approval prompt"))

    found = find_target_window(options.window_class)
    checks.append(Check(
        f"Target window ({options.window_class})",
        None,
        "open right now" if found else "not open - normal unless you are playing",
    ))

    return checks


# --------------------------------------------------------------------------
# Live wheel monitor
# --------------------------------------------------------------------------


class WheelMonitor:
    """Count wheel events per device so the user can see which one is alive.

    Several processes can read the same evdev node independently, so watching
    here does not disturb the running service - it answers "is this peripheral
    sending a wheel at all", which is the question a scroll problem starts with.
    """

    def __init__(self, on_change) -> None:
        self.on_change = on_change
        self.counts: dict[str, int] = {}
        self._devices: dict[int, object] = {}
        self._sources: list[int] = []
        self._dirty = False
        self._tick: int | None = None

    @property
    def running(self) -> bool:
        return bool(self._sources)

    def start(self, nodes: list[str]) -> list[str]:
        """Watch every readable node; return the ones that could not be opened."""
        from evdev import InputDevice  # noqa: PLC0415 - optional at import time

        self.stop()
        skipped = []
        for node in nodes:
            try:
                device = InputDevice(node)
            except (OSError, PermissionError):
                skipped.append(node)
                continue
            self.counts[node] = 0
            self._devices[device.fileno()] = device
            self._sources.append(
                GLib.unix_fd_add_full(
                    GLib.PRIORITY_DEFAULT, device.fileno(), GLib.IOCondition.IN,
                    self._readable, node,
                )
            )
        if self._sources:
            self._tick = GLib.timeout_add(250, self._flush)
        return skipped

    def _readable(self, fd: int, _condition, node: str) -> bool:
        from evdev import ecodes  # noqa: PLC0415

        device = self._devices.get(fd)
        if device is None:
            return False
        try:
            for event in device.read():
                if event.type == ecodes.EV_REL and event.code in WHEEL_BITS:
                    self.counts[node] = self.counts.get(node, 0) + 1
                    self._dirty = True
        except BlockingIOError:
            pass
        except OSError:
            # Unplugged mid-watch: drop it rather than spinning on a dead fd.
            self._devices.pop(fd, None)
            return False
        return True

    def _flush(self) -> bool:
        if self._dirty:
            self._dirty = False
            self.on_change()
        return True

    def stop(self) -> None:
        for source in self._sources:
            GLib.source_remove(source)
        self._sources.clear()
        if self._tick is not None:
            GLib.source_remove(self._tick)
            self._tick = None
        for device in self._devices.values():
            try:
                device.close()
            except OSError:
                pass
        self._devices.clear()
        self.counts.clear()


# --------------------------------------------------------------------------
# Window
# --------------------------------------------------------------------------


WHY_TEXT = """\
<b>GeForce NOW's Linux client is an X11 program.</b> On a Wayland desktop it runs \
through Xwayland, and there the client never receives wheel events from the \
physical mouse. The pointer moves and the buttons click, but nothing scrolls: \
inventories, menus, crafting lists and map zoom stop working inside the stream, \
while every other application on the desktop scrolls normally.

<b>What this program does.</b> A small background service reads the wheel \
directly from the peripherals you pick below and replays those movements into \
the GeForce NOW window using the X11 XTest extension. The stream sees ordinary \
scroll input again. Nothing is injected while the target window does not exist, \
and high-resolution wheels are normalised so one physical notch stays one step.

<b>Why it needs permission to a device.</b> Input devices under /dev/input are \
readable only by root. Rather than running anything as root, a udev rule grants \
<i>your session</i> read access to exactly the interfaces you select here and \
gives each one a stable name under /dev/input/wsf, so reconnecting a wireless \
mouse or unplugging a dongle does not break the setup. The service itself runs \
as you, with no privileges at all.

<b>Why it asks for authentication.</b> Only writing that rule needs root, \
because it lives in /etc/udev/rules.d. Everything else - the target window, the \
options, starting and stopping - is yours to change freely. If the rule already \
matches your selection, applying asks for nothing.

<b>Why peripherals other than mice appear here.</b> A wheel is not always on a \
mouse interface. Gaming keypads such as the Razer Tartarus, and unifying \
receivers that expose one shared endpoint, report their wheel on an interface \
the kernel classifies as a keyboard. Those are listed too, with a warning: \
granting access to a keyboard interface also lets programs running as you read \
what that device types.
"""


class ForwarderWindow(Adw.ApplicationWindow):
    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="Scroll Forwarder")
        self.set_default_size(820, 760)

        self.groups: list[DeviceGroup] = []
        self.options = Options.load()
        self.device_rows: dict[tuple[str, str], Adw.SwitchRow] = {}
        self.pending: set[tuple[str, str]] = set()
        # Only ever an in-memory copy, and only while "keep until closed" is on.
        self._session_password = ""
        self.monitor = WheelMonitor(self._refresh_device_subtitles)
        self._loading = False

        self.toasts = Adw.ToastOverlay()
        self.set_content(self.toasts)

        view = Adw.ToolbarView()
        self.toasts.set_child(view)

        self.stack = Adw.ViewStack()
        header = Adw.HeaderBar(title_widget=Adw.ViewSwitcher(stack=self.stack, policy=Adw.ViewSwitcherPolicy.WIDE))
        header.pack_start(self._build_service_controls())
        header.pack_end(self._build_menu())
        view.add_top_bar(header)

        self.banner = Adw.Banner(title="Device selection changed", button_label="Apply")
        self.banner.set_tooltip_text(
            "Write the new device list to the system udev rule. This is the only "
            "step that needs administrator access."
        )
        self.banner.connect("button-clicked", lambda *_: self.apply_devices())
        view.add_top_bar(self.banner)

        view.set_content(self.stack)
        view.add_bottom_bar(Adw.ViewSwitcherBar(stack=self.stack, reveal=False))

        self.stack.add_titled_with_icon(self._build_devices_page(), "devices", "Devices", "input-mouse-symbolic")
        self.stack.add_titled_with_icon(self._build_options_page(), "options", "Options", "preferences-system-symbolic")
        self.stack.add_titled_with_icon(self._build_diagnostics_page(), "checks", "Diagnostics", "dialog-information-symbolic")
        self.stack.add_titled_with_icon(self._build_why_page(), "why", "Why this exists", "help-about-symbolic")

        self.connect("close-request", self._on_close)
        self.reload()
        GLib.timeout_add_seconds(5, self._poll_service)

    # -- header ------------------------------------------------------------

    def _build_service_controls(self) -> Gtk.Widget:
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        self.status_label = Gtk.Label(label="checking...")
        self.status_label.add_css_class("dim-label")
        self.status_label.set_tooltip_text(
            "State of the background service that does the forwarding.\n"
            "'active' means it is running and watching your devices."
        )
        box.append(self.status_label)

        restart = Gtk.Button(icon_name="view-refresh-symbolic")
        restart.set_tooltip_text(
            "Restart the forwarder service.\n"
            "Worth trying first if scrolling stopped mid-session."
        )
        restart.connect("clicked", lambda *_: self.restart_service())
        box.append(restart)
        return box

    def _build_menu(self) -> Gtk.Widget:
        menu = Gtk.MenuButton(icon_name="open-menu-symbolic")
        menu.set_tooltip_text("More actions")

        popover = Gtk.Popover()
        contents = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4, margin_top=6,
                           margin_bottom=6, margin_start=6, margin_end=6)

        for label, tooltip, callback in (
            ("Stop service", "Stop forwarding until you start it again or log in next time.",
             lambda *_: self.service_action("stop")),
            ("Start service", "Start forwarding now.", lambda *_: self.service_action("start")),
            ("Copy diagnostics", "Copy the full diagnostics report to the clipboard, for pasting into a bug report.",
             lambda *_: self.copy_diagnostics()),
            ("About", "Version and licence information.", lambda *_: self.show_about()),
        ):
            button = Gtk.Button(label=label)
            button.add_css_class("flat")
            button.set_tooltip_text(tooltip)
            button.connect("clicked", callback)
            button.connect("clicked", lambda *_: popover.popdown())
            contents.append(button)

        popover.set_child(contents)
        menu.set_popover(popover)
        return menu

    # -- devices page ------------------------------------------------------

    def _build_devices_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        intro = Adw.PreferencesGroup(
            title="Peripherals that may scroll",
            description=(
                "Turn on every peripheral you might scroll with during a session - a "
                "second mouse, a keypad, a dongle and its Bluetooth link all count. "
                "The forwarder follows whichever of them are connected, so swapping "
                "devices mid-game keeps working. Interfaces are listed per device: a "
                "receiver's mouse endpoint and its keyboard endpoint are separate "
                "entries, and the wheel may be on either."
            ),
        )
        self.watch_row = Adw.SwitchRow(
            title="Watch wheel activity",
            subtitle="Counts wheel movements live, so you can see which device is actually sending them",
        )
        self.watch_row.set_tooltip_text(
            "Opens the devices you already have access to and counts wheel events as "
            "you scroll. Use it to identify an unlabelled peripheral, or to prove a "
            "device is dead before blaming the forwarder. It does not interfere with "
            "the running service."
        )
        self.watch_row.connect("notify::active", self._on_watch_toggled)
        intro.add(self.watch_row)

        self.show_all_row = Adw.SwitchRow(
            title="Show every interface",
            subtitle="Including lid switches, audio jacks and other things that cannot scroll",
        )
        self.show_all_row.set_tooltip_text(
            "Normally only plausible peripherals are listed: anything reporting a wheel, "
            "anything already selected, and USB or Bluetooth mouse/keyboard interfaces.\n\n"
            "Turn this on if a peripheral you know is connected does not appear - some "
            "devices only report their wheel once a vendor driver or a profile switch has "
            "configured them."
        )
        self.show_all_row.connect("notify::active", lambda *_: self._populate_devices())
        intro.add(self.show_all_row)
        page.add(intro)

        self.wheel_group = Adw.PreferencesGroup(title="Wheel-capable interfaces")
        self.wheel_group.rows = []
        page.add(self.wheel_group)

        self.other_group = Adw.PreferencesGroup(
            title="Other interfaces",
            description=(
                "These report no wheel right now. A peripheral that is asleep, in a "
                "different profile, or switched to another mode can still gain one, so "
                "they can be selected anyway."
            ),
        )
        self.other_group.rows = []
        page.add(self.other_group)

        actions = Adw.PreferencesGroup()
        rescan = Adw.ButtonRow(title="Rescan devices")
        rescan.set_tooltip_text("Re-read the connected peripherals. Use after plugging something in.")
        rescan.connect("activated", lambda *_: self.reload())
        actions.add(rescan)

        self.apply_row = Adw.ButtonRow(title="Apply device list")
        self.apply_row.add_css_class("suggested-action")
        self.apply_row.set_tooltip_text(
            "Write the selection to the system udev rule and restart the forwarder.\n"
            "Asks for administrator access only when the rule actually changes."
        )
        self.apply_row.connect("activated", lambda *_: self.apply_devices())
        actions.add(self.apply_row)
        page.add(actions)

        return page

    def _device_subtitle(self, group: DeviceGroup) -> str:
        parts = [KIND_LABELS.get(group.kind, group.kind)]
        if group.bus:
            parts.append(group.bus)
        if group.present:
            parts.append(", ".join(Path(node).name for node in group.nodes))
            readable = all(os.access(node, os.R_OK) for node in group.nodes)
            parts.append("access granted" if readable else "no access yet")
        else:
            parts.append("not connected")
        if self.monitor.running and group.present:
            total = sum(self.monitor.counts.get(node, 0) for node in group.nodes)
            watched = any(node in self.monitor.counts for node in group.nodes)
            parts.append(f"{total} wheel events" if watched else "not watchable")
        return " · ".join(parts)

    def _refresh_device_subtitles(self) -> None:
        for group in self.groups:
            row = self.device_rows.get(group.key)
            if row is not None:
                row.set_subtitle(self._device_subtitle(group))

    def _populate_devices(self) -> None:
        """Rebuild both device lists from scratch.

        Devices come and go, and a stale row would keep showing access state
        that is no longer true, which is the one thing this window must not do.
        """
        for container in (self.wheel_group, self.other_group):
            for row in container.rows:
                container.remove(row)
            container.rows = []
        self.device_rows.clear()

        show_all = self.show_all_row.get_active() if hasattr(self, "show_all_row") else False
        self._loading = True
        for group in self.groups:
            if not show_all and not group.plausible:
                continue
            row = Adw.SwitchRow(title=GLib.markup_escape_text(group.name))
            row.set_subtitle(self._device_subtitle(group))
            row.set_active(group.selected)
            tooltip = (
                f"Cover the {KIND_LABELS.get(group.kind, group.kind)} named "
                f"\u201c{group.name}\u201d.\nThe udev rule matches the device name and "
                "interface class, never an event number, so it survives reconnects."
            )
            if group.kind == "key":
                tooltip += (
                    "\n\nThis interface reports key presses as well. Granting access lets "
                    "programs running as you read what this device types - only enable it "
                    "for a peripheral whose wheel you actually need."
                )
            if not group.present:
                tooltip += "\n\nNot connected right now; turning it off removes its coverage."
            row.set_tooltip_text(tooltip)
            row.connect("notify::active", self._on_device_toggled, group)
            self.device_rows[group.key] = row
            container = self.wheel_group if group.wheel else self.other_group
            container.add(row)
            container.rows.append(row)
        self._loading = False

        self.wheel_group.set_visible(bool(self.wheel_group.rows))
        self.other_group.set_visible(bool(self.other_group.rows))

    def _on_device_toggled(self, row: Adw.SwitchRow, _param, group: DeviceGroup) -> None:
        if self._loading:
            return
        group.selected = row.get_active()
        self._update_banner()
        if group.selected and group.kind == "key":
            self.toast(
                f"“{group.name}” also reports key presses - access covers those too."
            )

    def _current_specs(self) -> list[str]:
        return [group.spec for group in self.groups if group.selected]

    def _update_banner(self) -> None:
        installed = set()
        try:
            installed = {(kind, name) for kind, name in parse_rule(RULE_PATH.read_text())}
        except OSError:
            pass
        chosen = {group.key for group in self.groups if group.selected}
        self.banner.set_revealed(chosen != installed)

    def _on_watch_toggled(self, row: Adw.SwitchRow, _param) -> None:
        if row.get_active():
            nodes = [node for group in self.groups if group.present for node in group.nodes]
            try:
                skipped = self.monitor.start(nodes)
            except ImportError:
                row.set_active(False)
                self.toast("python3-evdev is not installed, so live watching is unavailable.")
                return
            if skipped:
                self.toast(f"{len(skipped)} device(s) cannot be read yet; apply the list first.")
        else:
            self.monitor.stop()
        self._refresh_device_subtitles()
    # -- options page ------------------------------------------------------

    def _build_options_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        runtime = Adw.PreferencesGroup(
            title="Forwarding",
            description=(
                "These take effect as soon as you change them; they live in a user "
                "file and need no administrator access."
            ),
        )

        self.class_row = Adw.EntryRow(title="Target window class (WM_CLASS)")
        self.class_row.set_text(self.options.window_class)
        self.class_row.set_tooltip_text(
            "Wheel events are replayed only while a window with exactly this WM_CLASS "
            "exists. GeForce NOW uses “GeForceNOW”. To find another program's "
            "value, run  xprop WM_CLASS  in a terminal and click its window; use the "
            "second string it prints. One token, no spaces."
        )
        self.class_row.connect("apply", lambda *_: self.save_options())
        self.class_row.connect("changed", lambda *_: self._mark_options_dirty())
        runtime.add(self.class_row)

        self.unfocused_row = Adw.SwitchRow(
            title="Forward even when focus is unclear",
            subtitle="Needed for fullscreen streaming, where the compositor hides X11 focus",
        )
        self.unfocused_row.set_active(self.options.allow_unfocused)
        self.unfocused_row.set_tooltip_text(
            "Normally the forwarder checks that the target window is focused before "
            "injecting anything. Once a game grabs the pointer, the compositor can "
            "report an unclassified proxy as focused instead, and that check rejects "
            "every event - which looks exactly like the bug this tool fixes.\n\n"
            "On (recommended for GeForce NOW): forward whenever the target window "
            "exists. Wheel events then reach it even while another window is focused."
        )
        self.unfocused_row.connect("notify::active", lambda *_: self.save_options())
        runtime.add(self.unfocused_row)

        self.wait_row = Adw.SwitchRow(
            title="Wait for devices instead of giving up",
            subtitle="Keeps the service alive while every peripheral is asleep or unplugged",
        )
        self.wait_row.set_active(self.options.wait_for_device)
        self.wait_row.set_tooltip_text(
            "On (recommended): the service starts at login, waits quietly until one of "
            "your devices appears, and picks up each one as it reconnects.\n\n"
            "Off: it exits immediately when no selected device is connected, which "
            "means a Bluetooth mouse that sleeps takes scrolling down with it."
        )
        self.wait_row.connect("notify::active", lambda *_: self.save_options())
        runtime.add(self.wait_row)

        self.autostart_row = Adw.SwitchRow(
            title="Start automatically at login",
            subtitle="Runs as part of your graphical session",
        )
        self.autostart_row.set_tooltip_text(
            "Enables the user service so forwarding is there before you launch a game. "
            "Turning it off leaves the service installed but dormant until you start it."
        )
        self.autostart_row.connect("notify::active", self._on_autostart_toggled)
        runtime.add(self.autostart_row)
        page.add(runtime)

        # -- administrator access -----------------------------------------
        admin = Adw.PreferencesGroup(
            title="Administrator access",
            description=(
                "Changing which devices are covered writes a file under /etc, so that "
                "one step needs root. Everything else here does not, and a selection "
                "that already matches the installed rule needs nothing at all - the "
                "unprivileged half runs first, and you are only asked when something "
                "under /etc genuinely has to change."
            ),
        )

        self.method_row = Adw.ComboRow(
            title="How to authenticate",
            model=Gtk.StringList.new([
                "System dialog (recommended)",
                "Password entered here",
            ]),
        )
        self.method_row.set_tooltip_text(
            "System dialog: the desktop's own polkit prompt. Your password never "
            "passes through this program at all, which is why it is the default.\n\n"
            "Password entered here: for the one case the dialog cannot handle - it "
            "opens behind a fullscreen game, so applying appears to hang. The password "
            "is written straight to sudo's standard input, never to the command line, "
            "a file, or the keyring."
        )
        self.method_row.connect("notify::selected", lambda *_: self._sync_password_rows())
        admin.add(self.method_row)

        self.password_row = Adw.PasswordEntryRow(title="System password")
        self.password_row.set_tooltip_text(
            "Your own login password, the one sudo asks for. It is used for a single "
            "command and then cleared from this window."
        )
        admin.add(self.password_row)

        self.remember_row = Adw.SwitchRow(
            title="Keep it until this window is closed",
            subtitle="Held in memory only - never written to disk, a file or the keyring",
        )
        self.remember_row.set_tooltip_text(
            "Lets you apply several changes in a row without retyping. The password "
            "is discarded when this window closes, when you switch back to the system "
            "dialog, or as soon as you turn this off.\n\n"
            "Saving it permanently was deliberately not built: while the desktop "
            "keyring is unlocked, anything running as you could read it back, which is "
            "close to granting your account passwordless root. Device changes are rare "
            "enough that it is not worth that."
        )
        self.remember_row.connect("notify::active", self._on_remember_toggled)
        admin.add(self.remember_row)

        # Only shown if a build that did save the password left one behind.
        self.legacy_secret_row = Adw.ButtonRow(title="Delete password saved by an earlier version")
        self.legacy_secret_row.add_css_class("destructive-action")
        self.legacy_secret_row.set_tooltip_text(
            "An earlier build of this window could store your login password in the "
            "desktop keyring. That is no longer done; this removes the stored one."
        )
        self.legacy_secret_row.connect("activated", lambda *_: self.forget_password())
        admin.add(self.legacy_secret_row)
        page.add(admin)

        self._sync_password_rows()
        return page

    def _sync_password_rows(self) -> None:
        use_password = self.method_row.get_selected() == 1
        self.password_row.set_visible(use_password)
        self.remember_row.set_visible(use_password)
        # A stale secret is worth surfacing whichever method is selected.
        self.legacy_secret_row.set_visible(bool(keyring_lookup()))
        if not use_password:
            self._forget_session_password()

    def _forget_session_password(self) -> None:
        """Drop the in-memory copy and clear the entry.

        Python cannot scrub a str in place, so the only real protection is not
        keeping one: no persistence, and no reference held once it is not needed.
        """
        self._session_password = ""
        self.password_row.set_text("")

    def _on_remember_toggled(self, row: Adw.SwitchRow, _param) -> None:
        if not row.get_active():
            self._session_password = ""

    def _mark_options_dirty(self) -> None:
        text = self.class_row.get_text().strip()
        if text and not valid_window_class(text):
            self.class_row.add_css_class("error")
        else:
            self.class_row.remove_css_class("error")

    # -- diagnostics page --------------------------------------------------

    def _build_diagnostics_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()

        self.checks_group = Adw.PreferencesGroup(
            title="Checks",
            description=(
                "Each line is one thing that has to be true for scrolling to reach the "
                "game. They are checked in the order a problem usually travels: is the "
                "service running, is the rule installed, did udev apply it, can the "
                "devices be read, will the injection be accepted."
            ),
        )
        self.checks_group.rows = []
        page.add(self.checks_group)

        recheck_group = Adw.PreferencesGroup()
        refresh = Adw.ButtonRow(title="Run checks again")
        refresh.set_tooltip_text("Re-run every check. Cheap and read-only.")
        refresh.connect("activated", lambda *_: self.refresh_diagnostics())
        recheck_group.add(refresh)
        page.add(recheck_group)

        log_group = Adw.PreferencesGroup(
            title="Service log",
            description="The last 200 lines from the forwarder service.",
        )
        self.log_view = Gtk.TextView(editable=False, monospace=True, cursor_visible=False)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        scroller = Gtk.ScrolledWindow(min_content_height=240, vexpand=True)
        scroller.set_child(self.log_view)
        scroller.add_css_class("card")
        scroller.set_tooltip_text(
            "“Monitoring …” lines list the devices actually being read. "
            "“Waiting for input device” means the udev rule has not produced a "
            "link for it yet."
        )
        log_group.add(scroller)
        reload_log = Adw.ButtonRow(title="Reload log")
        reload_log.set_tooltip_text("Fetch the latest service output.")
        reload_log.connect("activated", lambda *_: self.refresh_log())
        log_group.add(reload_log)
        page.add(log_group)
        return page

    def _build_why_page(self) -> Gtk.Widget:
        page = Adw.PreferencesPage()
        group = Adw.PreferencesGroup(title="Why this program is necessary")
        label = Gtk.Label(
            label=WHY_TEXT, use_markup=True, wrap=True, xalign=0.0, selectable=True,
            margin_top=12, margin_bottom=12, margin_start=12, margin_end=12,
        )
        # Keep the paragraph column readable; without a cap the label reports a
        # single-line natural width and drags the whole page wide.
        label.set_max_width_chars(60)
        label.set_can_focus(False)  # selectable, but no caret parked in the text
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("card")
        box.append(label)
        group.add(box)
        page.add(group)

        limits = Adw.PreferencesGroup(title="Known limits")
        for title, subtitle, tooltip in (
            ("Injection is global to Xwayland",
             "XTest has no per-window delivery; the focus checks narrow it as far as the protocol allows.",
             "Wheel steps are synthesised into the X server, not into one specific window. "
             "The target-window and focus checks are the only filters available."),
            ("Duplicates after an upstream fix",
             "The original device is never grabbed, so if the client starts receiving wheel events natively you may see both.",
             "Stop or disable the service once GeForce NOW handles the wheel by itself."),
            ("Not endorsed by NVIDIA",
             "This is a local workaround and has not been evaluated against individual games' anti-cheat systems.",
             "It injects input into your own X server; it does not modify or interact with the game client."),
        ):
            row = Adw.ActionRow(title=title, subtitle=subtitle)
            row.set_subtitle_lines(3)
            row.set_tooltip_text(tooltip)
            limits.add(row)
        page.add(limits)
        return page

    # -- actions -----------------------------------------------------------

    def toast(self, message: str) -> None:
        self.toasts.add_toast(Adw.Toast(title=message, timeout=5))

    def reload(self) -> None:
        self.groups = discover_devices()
        self._populate_devices()
        self._update_banner()
        self._poll_service()
        self.refresh_diagnostics()
        self.refresh_log()

    def _poll_service(self) -> bool:
        active, enabled = service_state()
        self.status_label.set_label(active)
        self.status_label.remove_css_class("success")
        self.status_label.remove_css_class("error")
        self.status_label.add_css_class("success" if active == "active" else "error")
        self._loading = True
        if hasattr(self, "autostart_row"):
            self.autostart_row.set_active(enabled)
        self._loading = False
        return True

    def save_options(self) -> None:
        text = self.class_row.get_text().strip()
        if not valid_window_class(text):
            self.toast("Window class must be a single token such as GeForceNOW.")
            return
        self.options.window_class = text
        self.options.allow_unfocused = self.unfocused_row.get_active()
        self.options.wait_for_device = self.wait_row.get_active()
        try:
            self.options.save()
        except OSError as exc:
            self.toast(f"Could not save options: {exc}")
            return
        systemctl("daemon-reload")
        if service_state()[0] == "active":
            systemctl("restart", SERVICE)
        self.toast("Options saved and forwarder restarted.")
        self.refresh_diagnostics()

    def _on_autostart_toggled(self, row: Adw.SwitchRow, _param) -> None:
        if self._loading:
            return
        result = systemctl("enable" if row.get_active() else "disable", SERVICE)
        if result.returncode != 0:
            self.toast(result.stderr.strip() or "Could not change autostart.")
        else:
            self.toast("Starts at login." if row.get_active() else "Will not start at login.")

    def service_action(self, action: str) -> None:
        result = systemctl(action, SERVICE)
        self.toast(result.stderr.strip() or f"Service {action} requested.")
        self._poll_service()
        GLib.timeout_add_seconds(1, lambda: (self.refresh_diagnostics(), self.refresh_log(), False)[-1])

    def restart_service(self) -> None:
        self.service_action("restart")

    def apply_devices(self) -> None:
        """Apply the selection, asking for credentials only if /etc must change.

        The unprivileged half runs first and reports whether the system files
        already match. Most applies stop there, so no password is requested,
        entered or held for them at all.
        """
        specs = self._current_specs()
        if not specs:
            self._confirm(
                "Remove every device?",
                "No peripheral would be covered, so nothing will scroll inside the target "
                "window until you select one again.",
                "Remove all", lambda: self._begin_apply(specs),
            )
            return
        self._begin_apply(specs)

    def _confirm(self, heading: str, body: str, confirm_label: str, action) -> None:
        dialog = Adw.AlertDialog(heading=heading, body=body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("go", confirm_label)
        dialog.set_response_appearance("go", Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect("response", lambda _dialog, response: action() if response == "go" else None)
        dialog.present(self)

    def _begin_apply(self, specs: list[str]) -> None:
        self._set_applying(True, "Checking\u2026")
        self.banner.set_revealed(False)

        def worker() -> None:
            needs_root, code, output = root_step_needed(specs)
            GLib.idle_add(self._after_preflight, specs, needs_root, code, output)

        threading.Thread(target=worker, daemon=True).start()

    def _after_preflight(self, specs: list[str], needs_root: bool, code: int, output: str) -> bool:
        if not needs_root:
            # Either it is done (0) or it failed for a reason root would not fix.
            return self._finish_apply(code, output, used_password=False)

        if self.method_row.get_selected() == 1:
            password = self.password_row.get_text() or self._session_password
            if not password:
                self._set_applying(False)
                self.stack.set_visible_child_name("options")
                self.password_row.grab_focus()
                self.toast("Administrator access is needed for this change - enter your password.")
                return False
            if self.remember_row.get_active():
                self._session_password = password
            self._run_privileged(specs, "sudo", password)
            return False

        if find_target_window(self.options.window_class):
            self._set_applying(False)
            self._confirm(
                f"{self.options.window_class} is running",
                "This change needs administrator access, and the system dialog opens "
                "behind a fullscreen window - which makes it look as though nothing is "
                "happening. Alt+Tab to the dialog after applying, or cancel and switch "
                "to the password method in Options.",
                "Apply anyway", lambda: self._run_privileged(specs, "pkexec", ""),
            )
            return False

        self._run_privileged(specs, "pkexec", "")
        return False

    def _run_privileged(self, specs: list[str], privesc: str, password: str) -> None:
        self._set_applying(True, "Authenticating\u2026")

        def worker() -> None:
            code, output = run_installer(specs, privesc, password)
            # Drop this thread's reference as soon as the pipe has taken it.
            GLib.idle_add(self._finish_apply, code, output, privesc == "sudo")

        threading.Thread(target=worker, daemon=True).start()

    def _set_applying(self, busy: bool, label: str = "") -> None:
        self.apply_row.set_sensitive(not busy)
        self.apply_row.set_title(label if busy else "Apply device list")

    def _finish_apply(self, code: int, output: str, used_password: bool) -> bool:
        self._set_applying(False)
        if used_password and not self.remember_row.get_active():
            self._forget_session_password()
        elif used_password:
            self.password_row.set_text("")  # kept in memory, not on screen
        if code == 0:
            self.toast("Device list applied and forwarder restarted.")
        else:
            body = output or "The installer reported no output."
            if used_password and ("password" in body.lower() or "sorry" in body.lower()):
                body += "\n\nCheck the password in Options."
            dialog = Adw.AlertDialog(heading="Could not apply the device list", body=body[-1500:])
            dialog.add_response("close", "Close")
            dialog.present(self)
        self.reload()
        return False

    def forget_password(self) -> None:
        keyring_clear()
        self._forget_session_password()
        self.legacy_secret_row.set_visible(False)
        self.toast("Stored password removed from the keyring.")

    def refresh_diagnostics(self) -> None:
        checks = run_diagnostics(self.groups, self.options)
        for row in self.checks_group.rows:
            self.checks_group.remove(row)
        self.checks_group.rows = []
        for check in checks:
            row = Adw.ActionRow(title=check.title, subtitle=check.detail)
            row.set_subtitle_lines(4)
            row.set_tooltip_text(check.detail)
            if check.ok is None:
                icon = Gtk.Image(icon_name="dialog-information-symbolic")
            elif check.ok:
                icon = Gtk.Image(icon_name="emblem-ok-symbolic")
                icon.add_css_class("success")
            else:
                icon = Gtk.Image(icon_name="dialog-warning-symbolic")
                icon.add_css_class("error")
            row.add_prefix(icon)
            self.checks_group.add(row)
            self.checks_group.rows.append(row)
        self._last_checks = checks

    def refresh_log(self) -> None:
        self.log_view.get_buffer().set_text(journal_tail())

    def copy_diagnostics(self) -> None:
        lines = ["Scroll Forwarder diagnostics", ""]
        for check in getattr(self, "_last_checks", []):
            mark = {True: "OK", False: "FAIL", None: "info"}[check.ok]
            lines.append(f"[{mark}] {check.title}: {check.detail}")
        lines += ["", "Selected devices:"]
        lines += [f"  {group.spec} ({'connected' if group.present else 'absent'})"
                  for group in self.groups if group.selected]
        lines += ["", f"Options: class={self.options.window_class} "
                      f"allow_unfocused={self.options.allow_unfocused} "
                      f"wait_for_device={self.options.wait_for_device}", "", "Log:", journal_tail(80)]
        self.get_clipboard().set("\n".join(lines))
        self.toast("Diagnostics copied to the clipboard.")

    def show_about(self) -> None:
        about = Adw.AboutDialog(
            application_name="Scroll Forwarder",
            application_icon="input-mouse",
            developer_name="Hardened Wayland Scroll Forwarder",
            comments=(
                "Replays wheel events from the peripherals you choose into an "
                "X11/Xwayland window that would otherwise never receive them."
            ),
            website="https://github.com/kendur/wayland-scroll-forwarder",
            license_type=Gtk.License.GPL_3_0,
        )
        about.present(self)

    def _on_close(self, *_args) -> bool:
        self.monitor.stop()
        self._session_password = ""
        return False


class ForwarderApplication(Adw.Application):
    def __init__(self) -> None:
        super().__init__(application_id=APP_ID)

    def do_activate(self) -> None:
        window = self.props.active_window or ForwarderWindow(self)
        window.present()


def main(argv: list[str] | None = None) -> int:
    return ForwarderApplication().run(argv if argv is not None else sys.argv)


if __name__ == "__main__":
    raise SystemExit(main())
