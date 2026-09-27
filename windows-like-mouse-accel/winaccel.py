#!/usr/bin/env python3
"""winaccel - make Linux mouse acceleration behave exactly like Windows 11.

Windows 11 pointer ballistics ("Enhance pointer precision", the pointer speed
slider and the SmoothMouseXCurve/SmoothMouseYCurve registry curves) are
re-implemented bit-for-bit as a libinput Lua plugin. The compositor is then
told to use libinput's flat (constant 1:1) profile for those mice so that
libinput does not accelerate a second time.

See README.md for usage and ANALYSIS.md for how both systems work.
"""
from __future__ import annotations

import argparse
import dataclasses
import fcntl
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(os.path.realpath(__file__)).parent
sys.path.insert(0, str(HERE))

import windows_ballistics as wb  # noqa: E402

PLUGIN_TEMPLATE = HERE / "plugin.lua.in"
PLUGIN_PATH = Path("/etc/libinput/plugins/50-winaccel.lua")
CONFIG_DIR = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "winaccel"
CONFIG_PATH = CONFIG_DIR / "config.json"

KWIN = "org.kde.KWin"
KWIN_DEVICES = "/org/kde/KWin/InputDevice"
KWIN_DEVICE_IFACE = "org.kde.KWin.InputDevice"


# --------------------------------------------------------------------------- config

@dataclasses.dataclass
class Config:
    epp: bool = True                       # "Enhance pointer precision" (MouseSpeed != 0)
    sensitivity: int = 10                  # pointer speed slider, MouseSensitivity 1..20
    scale: float | str = "auto"            # display scale factor, or "auto"
    x_curve: list[int] = dataclasses.field(default_factory=lambda: list(wb.DEFAULT_X_CURVE))
    y_curve: list[int] = dataclasses.field(default_factory=lambda: list(wb.DEFAULT_Y_CURVE))
    devices: list[str] = dataclasses.field(default_factory=list)   # "vvvv:pppp" hex; empty = all mice
    # compositor settings from before winaccel touched them, restored by `remove`
    saved_kde: dict = dataclasses.field(default_factory=dict)

    @classmethod
    def load(cls) -> "Config":
        try:
            data = json.loads(CONFIG_PATH.read_text())
        except FileNotFoundError:
            return cls()
        known = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self) -> None:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(json.dumps(dataclasses.asdict(self), indent=2) + "\n")

    def device_ids(self) -> list[tuple[int, int]]:
        return [parse_vid_pid(d) for d in self.devices]


def parse_vid_pid(text: str) -> tuple[int, int]:
    m = re.fullmatch(r"\s*([0-9a-fA-F]{1,4}):([0-9a-fA-F]{1,4})\s*", text)
    if not m:
        raise ValueError(f"device must be VID:PID in hex (e.g. 1532:0099), got {text!r}")
    return int(m.group(1), 16), int(m.group(2), 16)


def scale_to_dpi(scale: float) -> int:
    """Windows' effective monitor DPI for a scale factor: (scale% * 96 + 50) / 100."""
    return (round(scale * 100) * 96 + 50) // 100


def parse_curve(text: str) -> list[int]:
    """Five points as 16.16 fixed point. Accepts decimal numbers (0,0.43,1.25,3.86,40),
    raw 16.16 integers written as hex (0x0,0x6E15,...), or the 40 registry bytes
    (00,00,00,00,00,00,00,00,15,6e,...)."""
    parts = [p.strip() for p in text.replace("\\", "").split(",") if p.strip()]
    if len(parts) == 40 and all(re.fullmatch(r"[0-9a-fA-F]{2}", p) for p in parts):
        curve = wb.parse_curve_reg_binary(bytes(int(p, 16) for p in parts))
    elif len(parts) == 5:
        curve = [int(p, 16) if p.lower().startswith("0x") else round(float(p) * 65536) for p in parts]
    else:
        raise ValueError("a curve needs 5 points (or 40 registry bytes)")
    return curve


def fmt_curve(curve: list[int]) -> str:
    return ", ".join(f"{v / 65536:g}" for v in curve)


# --------------------------------------------------------------------------- .reg import

def parse_reg_file(path: Path) -> dict[str, str | bytes]:
    """Read HKCU\\Control Panel\\Mouse values from a `reg export` file."""
    raw = path.read_bytes()
    if raw.startswith(b"\xff\xfe") or raw.startswith(b"\xfe\xff"):
        text = raw.decode("utf-16")
    else:
        text = raw.decode("utf-8-sig", errors="replace")
    text = re.sub(r"\\\r?\n\s*", "", text)          # join continuation lines
    values: dict[str, str | bytes] = {}
    in_mouse = False
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("["):
            in_mouse = line.rstrip("]").lower().endswith("control panel\\mouse")
            continue
        m = re.fullmatch(r'"([^"]+)"\s*=\s*(.*)', line)
        if not in_mouse or not m:
            continue
        name, val = m.groups()
        if val.startswith('"'):
            values[name] = val.strip('"')
        elif val.lower().startswith("hex:"):
            values[name] = bytes(int(b, 16) for b in val[4:].split(",") if b.strip())
        elif val.lower().startswith("dword:"):
            values[name] = str(int(val[6:], 16))
    if not values:
        raise ValueError(f"{path}: no [HKEY_CURRENT_USER\\Control Panel\\Mouse] values found")
    return values


def apply_reg_values(cfg: Config, values: dict[str, str | bytes]) -> list[str]:
    """Copy what Windows itself reads (see ANALYSIS.md) into the config."""
    notes = []
    if "MouseSpeed" in values:
        cfg.epp = int(values["MouseSpeed"]) != 0
        notes.append(f"MouseSpeed={values['MouseSpeed']} -> Enhance pointer precision "
                     f"{'on' if cfg.epp else 'off'}")
    if "MouseSensitivity" in values:
        s = int(values["MouseSensitivity"])
        if 1 <= s <= 20:
            cfg.sensitivity = s
            notes.append(f"MouseSensitivity={s}")
        else:
            cfg.sensitivity = 10
            notes.append(f"MouseSensitivity={s} is invalid, Windows falls back to 10")
    for name, attr in (("SmoothMouseXCurve", "x_curve"), ("SmoothMouseYCurve", "y_curve")):
        v = values.get(name)
        curve = wb.parse_curve_reg_binary(v) if isinstance(v, bytes) else None
        if v is not None and curve is None:
            notes.append(f"{name} is not 40 bytes, Windows ignores it (keeping previous curve)")
        elif curve is not None:
            setattr(cfg, attr, curve)
            notes.append(f"{name} = {fmt_curve(curve)}")
    return notes


# --------------------------------------------------------------------------- environment

def run(cmd: list[str], check: bool = True, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=check, text=True, capture_output=True, **kw)


def desktop() -> str:
    d = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    if "KDE" in d:
        return "kde"
    if "GNOME" in d:
        return "gnome"
    return d.lower() or "unknown"


def detect_scale() -> tuple[float, str]:
    """Display scale factor of the (primary) monitor, via kscreen-doctor."""
    if not shutil.which("kscreen-doctor"):
        return 1.0, "kscreen-doctor not found, assuming 100% scaling"
    try:
        outputs = json.loads(run(["kscreen-doctor", "-j"]).stdout)["outputs"]
    except (subprocess.CalledProcessError, ValueError, KeyError):
        return 1.0, "could not query kscreen-doctor, assuming 100% scaling"
    enabled = [o for o in outputs if o.get("enabled")]
    if not enabled:
        return 1.0, "no enabled outputs reported, assuming 100% scaling"
    enabled.sort(key=lambda o: o.get("priority", 99))
    scales = sorted({float(o.get("scale", 1.0)) for o in enabled})
    note = f"{enabled[0]['name']} at {float(enabled[0].get('scale', 1.0)):g}x"
    if len(scales) > 1:
        note += (f" (monitors use different scales {scales}; Windows would switch curves per "
                 f"monitor, the plugin uses the primary one)")
    return float(enabled[0].get("scale", 1.0)), note


def resolve_scale(cfg: Config) -> tuple[float, str]:
    if cfg.scale == "auto":
        return detect_scale()
    return float(cfg.scale), f"{float(cfg.scale):g}x (set manually)"


# --------------------------------------------------------------------------- plugin generation

def lua_list(values: list[int]) -> str:
    return "{ " + ", ".join(str(v) for v in values) + " }"


def validate_curves(cfg: Config, b: wb.Ballistics) -> list[str]:
    problems = []
    if b.x[0] >= 65536:
        # The smallest possible input speed is one count (65536 in 16.16). At or below
        # x[0] Windows indexes the curve at -1, an out-of-bounds kernel read.
        problems.append("the first X-curve point must be below 1/3.5 (Windows reads out of "
                        "bounds for speeds at or below it)")
    if any(b.x[k] > b.x[k + 1] for k in range(4)):
        problems.append("X-curve points must be ascending")
    return problems


def generate_plugin(cfg: Config, scale: float) -> str:
    dpi = scale_to_dpi(scale)
    b = wb.build_ballistics(cfg.x_curve, cfg.y_curve, cfg.sensitivity, dpi)
    devices = ", ".join(f"{{ vid = 0x{v:04x}, pid = 0x{p:04x} }}" for v, p in cfg.device_ids())
    lua_cfg = "\n".join([
        "{",
        f"    epp = {'true' if cfg.epp else 'false'},",
        f"    -- MouseSensitivity={cfg.sensitivity}, display scale {scale:g}x = {dpi} DPI",
        f"    -- SmoothMouseXCurve = {fmt_curve(cfg.x_curve)}",
        f"    -- SmoothMouseYCurve = {fmt_curve(cfg.y_curve)}",
        "    -- internal curve (16.16) as built by CDeviceAcceleration::_BuildAccelerationCurve",
        f"    x = {lua_list(b.x)},",
        f"    slope = {lua_list(b.slope)},",
        f"    intercept = {lua_list(b.intercept)},",
        "    -- EPP off: MouseSensitivity factor in 1/256 units, normalized for DPI",
        f"    factor256 = {wb.normalized_sensitivity_factor(cfg.sensitivity, dpi)},",
        f"    devices = {{ {devices} }},",
        "}",
    ])
    return PLUGIN_TEMPLATE.read_text().replace("@CONFIG@", lua_cfg)


def installed_plugin() -> str | None:
    try:
        return PLUGIN_PATH.read_text()
    except FileNotFoundError:
        return None


def pkexec(args: list[str]) -> None:
    """Run a command as root. polkit shows its password dialog and we wait for it."""
    print(f"  requesting administrator rights to run: {' '.join(args)}")
    r = subprocess.run(["pkexec", *args])
    if r.returncode != 0:
        raise SystemExit(f"error: administrator command failed or was cancelled (exit {r.returncode})")


def install_plugin(content: str) -> bool:
    if installed_plugin() == content:
        print(f"  {PLUGIN_PATH} is already up to date")
        return False
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
        f.write(content)
        tmp = f.name
    try:
        os.chmod(tmp, 0o644)
        pkexec(["install", "-D", "-m", "0644", "-o", "root", "-g", "root", tmp, str(PLUGIN_PATH)])
    finally:
        os.unlink(tmp)
    print(f"  installed {PLUGIN_PATH}")
    return True


# --------------------------------------------------------------------------- KDE (KWin)

def busctl_json(*args: str):
    return json.loads(run(["busctl", "--user", "--json=short", *args]).stdout)


def kwin_devices() -> list[dict]:
    """All KWin input devices with their properties (plain values)."""
    names = busctl_json("get-property", KWIN, KWIN_DEVICES,
                        "org.kde.KWin.InputDeviceManager", "devicesSysNames")["data"]
    devices = []
    for sys_name in names:
        try:
            props = busctl_json("call", KWIN, f"{KWIN_DEVICES}/{sys_name}",
                                "org.freedesktop.DBus.Properties", "GetAll", "s",
                                KWIN_DEVICE_IFACE)["data"][0]
        except subprocess.CalledProcessError:
            continue
        devices.append({k: v["data"] for k, v in props.items()})
    return devices


def kwin_targets(cfg: Config, devices: list[dict] | None = None) -> list[dict]:
    """KWin devices the plugin applies to: mice and pointing sticks that libinput could
    accelerate, restricted to the configured VID:PIDs if any."""
    ids = cfg.device_ids()
    out = []
    for d in devices if devices is not None else kwin_devices():
        if not d.get("pointer") or d.get("touchpad") or d.get("tabletTool") or d.get("tabletPad"):
            continue
        if not d.get("supportsPointerAccelerationProfileFlat"):
            continue
        if ids and (d.get("vendor"), d.get("product")) not in ids:
            continue
        out.append(d)
    return out


def kde_key(d: dict) -> str:
    return f"{d['vendor']:04x}:{d['product']:04x}:{d['name']}"


def kwin_set(sys_name: str, prop: str, sig: str, value: str) -> None:
    run(["busctl", "--user", "set-property", KWIN, f"{KWIN_DEVICES}/{sys_name}",
         KWIN_DEVICE_IFACE, prop, sig, value])


def kwin_configure(d: dict, flat: bool, speed: float) -> None:
    """Set the profile and speed live over D-Bus; KWin stores them in kcminputrc itself."""
    if flat:
        kwin_set(d["sysName"], "pointerAccelerationProfileFlat", "b", "true")
    else:
        kwin_set(d["sysName"], "pointerAccelerationProfileAdaptive", "b", "true")
    kwin_set(d["sysName"], "pointerAcceleration", "d", repr(float(speed)))


def flat_speed_for_scale(scale: float) -> float:
    """libinput flat profile multiplies by (1 + speed). The plugin emits physical pixels like
    Windows does; KWin positions the cursor in logical pixels, so divide by the scale."""
    return max(-1.0, min(1.0, 1.0 / scale - 1.0))


def kde_apply(cfg: Config, scale: float) -> None:
    speed = flat_speed_for_scale(scale)
    targets = kwin_targets(cfg)
    if not targets:
        print("  no matching pointer devices found in KWin")
    for d in targets:
        key = kde_key(d)
        if key not in cfg.saved_kde:
            cfg.saved_kde[key] = {"flat": d["pointerAccelerationProfileFlat"],
                                  "speed": d["pointerAcceleration"]}
        kwin_configure(d, True, speed)
        print(f"  {d['name']} ({d['sysName']}): libinput profile flat, speed {speed:+.4f}")


def kde_restore(cfg: Config) -> None:
    devices = {kde_key(d): d for d in kwin_devices()}
    for key, saved in cfg.saved_kde.items():
        d = devices.get(key)
        if d is None:
            print(f"  {key.split(':', 2)[2]} is not connected; its KWin settings were left as they are")
            continue
        kwin_configure(d, saved["flat"], saved["speed"])
        print(f"  {d['name']}: restored {'flat' if saved['flat'] else 'adaptive'} profile, "
              f"speed {saved['speed']:+.3f}")
    cfg.saved_kde = {}


# --------------------------------------------------------------------------- GNOME

GNOME_MOUSE = "org.gnome.desktop.peripherals.mouse"


def gnome_apply(cfg: Config, scale: float) -> None:
    if not cfg.saved_kde.get("gnome"):
        cfg.saved_kde["gnome"] = {
            k: run(["gsettings", "get", GNOME_MOUSE, k]).stdout.strip() for k in ("accel-profile", "speed")}
    run(["gsettings", "set", GNOME_MOUSE, "accel-profile", "flat"])
    run(["gsettings", "set", GNOME_MOUSE, "speed", repr(flat_speed_for_scale(scale))])
    print("  GNOME: mouse accel-profile flat")
    print("  note: the plugin only runs if your mutter version loads libinput plugins")


def gnome_restore(cfg: Config) -> None:
    saved = cfg.saved_kde.pop("gnome", None)
    if saved:
        for k, v in saved.items():
            run(["gsettings", "set", GNOME_MOUSE, k, v])
        print("  GNOME: restored mouse acceleration settings")


# --------------------------------------------------------------------------- commands

def describe(cfg: Config, scale: float, scale_note: str) -> None:
    dpi = scale_to_dpi(scale)
    print(f"  Enhance pointer precision : {'on' if cfg.epp else 'off'}")
    print(f"  Pointer speed (1-20)      : {cfg.sensitivity}")
    print(f"  Display scale             : {scale_note} -> {dpi} DPI")
    if cfg.epp:
        default = cfg.x_curve == wb.DEFAULT_X_CURVE and cfg.y_curve == wb.DEFAULT_Y_CURVE
        print(f"  SmoothMouseXCurve         : {fmt_curve(cfg.x_curve)}{'  (Windows default)' if default else ''}")
        print(f"  SmoothMouseYCurve         : {fmt_curve(cfg.y_curve)}")
    else:
        f = wb.normalized_sensitivity_factor(cfg.sensitivity, dpi)
        print(f"  Speed multiplier          : {f}/256 = {f / 256:g}")
    print(f"  Devices                   : {', '.join(cfg.devices) or 'all mice and pointing sticks'}")


def cmd_apply(args) -> None:
    cfg = Config.load()
    if args.defaults:
        cfg = Config(saved_kde=cfg.saved_kde)
    if args.reg:
        for note in apply_reg_values(cfg, parse_reg_file(Path(args.reg))):
            print(f"  imported {note}")
    if args.epp is not None:
        cfg.epp = args.epp
    if args.sensitivity is not None:
        if not 1 <= args.sensitivity <= 20:
            raise SystemExit("error: --sensitivity must be 1..20 (the Windows pointer speed slider)")
        cfg.sensitivity = args.sensitivity
    if args.scale is not None:
        cfg.scale = args.scale if args.scale == "auto" else float(args.scale)
    if args.x_curve:
        cfg.x_curve = parse_curve(args.x_curve)
    if args.y_curve:
        cfg.y_curve = parse_curve(args.y_curve)
    if args.all_devices:
        cfg.devices = []
    if args.device:
        for d in args.device:
            parse_vid_pid(d)
        cfg.devices = [d.lower() for d in args.device]

    scale, scale_note = resolve_scale(cfg)
    b = wb.build_ballistics(cfg.x_curve, cfg.y_curve, cfg.sensitivity, scale_to_dpi(scale))
    problems = validate_curves(cfg, b) if cfg.epp else []
    if problems:
        raise SystemExit("error: " + "; ".join(problems))

    print("Windows pointer settings:")
    describe(cfg, scale, scale_note)
    content = generate_plugin(cfg, scale)
    if args.dry_run:
        print(content)
        return

    print("libinput plugin:")
    changed = install_plugin(content)
    cfg.save()

    print("Compositor:")
    de = desktop()
    if args.no_compositor:
        print("  skipped (--no-compositor)")
    elif de == "kde":
        kde_apply(cfg, scale)
    elif de == "gnome":
        gnome_apply(cfg, scale)
    else:
        print(f"  {de}: set every mouse to libinput's flat profile with speed "
              f"{flat_speed_for_scale(scale):+.4f} yourself")
    cfg.save()

    if changed:
        print("\nThe compositor loads libinput plugins when it starts: log out and back in "
              "for the new curve to take effect. Then run `winaccel test` to verify it.")


def cmd_remove(args) -> None:
    cfg = Config.load()
    print("libinput plugin:")
    if PLUGIN_PATH.exists():
        pkexec(["rm", "-f", str(PLUGIN_PATH)])
        print(f"  removed {PLUGIN_PATH}")
    else:
        print("  not installed")
    print("Compositor:")
    if desktop() == "kde":
        kde_restore(cfg)
    gnome_restore(cfg)
    cfg.save()
    print("\nLog out and back in to unload the plugin from the running compositor.")


def cmd_status(args) -> None:
    cfg = Config.load()
    scale, scale_note = resolve_scale(cfg)
    print("Configuration" + ("" if CONFIG_PATH.exists() else " (defaults, never applied)") + ":")
    describe(cfg, scale, scale_note)
    content = installed_plugin()
    print("libinput plugin:")
    if content is None:
        print(f"  not installed ({PLUGIN_PATH})")
    elif content == generate_plugin(cfg, scale):
        print(f"  {PLUGIN_PATH}: installed, matches the configuration")
    else:
        print(f"  {PLUGIN_PATH}: installed but OUT OF DATE (display scale or config changed?) "
              f"- run `winaccel apply`")
    if desktop() == "kde":
        print("KWin devices:")
        speed = flat_speed_for_scale(scale)
        for d in kwin_targets(cfg):
            ok = d["pointerAccelerationProfileFlat"] and abs(d["pointerAcceleration"] - speed) < 1e-3
            state = "ok" if ok else "NOT flat/1:1, libinput will accelerate on top - run `winaccel apply`"
            print(f"  {d['vendor']:04x}:{d['product']:04x} {d['name']} ({d['sysName']}): "
                  f"{'flat' if d['pointerAccelerationProfileFlat'] else 'adaptive'} "
                  f"{d['pointerAcceleration']:+.3f} - {state}")


def mouse_rate_hint() -> tuple[int, int] | None:
    """Default (DPI, polling rate) of the first mouse that has a udev MOUSE_DPI entry."""
    for dev in sorted(Path("/dev/input").glob("event*")):
        try:
            out = run(["udevadm", "info", "--query=property", str(dev)]).stdout
        except subprocess.CalledProcessError:
            continue
        m = re.search(r"^MOUSE_DPI=.*?\*(\d+)(?:@(\d+))?", out, re.M)
        if m:
            return int(m.group(1)), int(m.group(2) or 125)
    return None


def cmd_show(args) -> None:
    cfg = Config.load()
    scale, scale_note = resolve_scale(cfg)
    print("Configuration:")
    describe(cfg, scale, scale_note)
    dpi = scale_to_dpi(scale)
    settings = wb.Settings(epp=cfg.epp, sensitivity=cfg.sensitivity, monitor_dpi=dpi,
                           x_curve=cfg.x_curve, y_curve=cfg.y_curve)
    if cfg.epp:
        b = settings.ballistics()
        print("\nInternal curve (input speed = max(|dx|,|dy|) + min/2 counts per packet):")
        for k in range(5):
            print(f"  point {k}: speed {b.x[k] / 65536:9.4f} -> {b.y[k] / 65536:10.4f} px/packet")
    hint = mouse_rate_hint()
    mouse_dpi, rate = hint or (800, 1000)
    source = "udev MOUSE_DPI default" if hint else "assumed"
    if args.mouse_dpi or args.rate:
        mouse_dpi, rate, source = args.mouse_dpi or mouse_dpi, args.rate or rate, "given"
    print(f"\nSteady-state response to straight-line motion (hand speed for {mouse_dpi} DPI "
          f"at {rate} Hz, {source}):")
    print(f"  {'counts/packet':>13} {'hand cm/s':>10} {'px/packet':>10} {'gain':>7}")
    for d in (1, 2, 3, 4, 5, 6, 8, 10, 12, 15, 20, 25, 30, 40, 50, 64, 80, 100, 127):
        st = wb.BallisticsState()
        n = 400
        total = sum(wb.process_packet(d, 0, settings, st)[0] for _ in range(n))
        hand = d * rate / mouse_dpi * 2.54
        print(f"  {d:>13} {hand:>10.1f} {total / n / scale:>10.3f} {total / n / d / scale:>7.3f}")
    print("  (px = logical pixels; gain is applied per packet, so it depends on the polling "
          "rate exactly as on Windows)")


# --------------------------------------------------------------------------- end-to-end test

# <linux/uinput.h>, <linux/input-event-codes.h>
UI_SET_EVBIT, UI_SET_KEYBIT, UI_SET_RELBIT = 0x40045564, 0x40045565, 0x40045566
UI_DEV_SETUP, UI_DEV_CREATE, UI_DEV_DESTROY = 0x405C5503, 0x5501, 0x5502
EV_SYN, EV_KEY, EV_REL = 0, 1, 2
REL_X, REL_Y, BTN_LEFT, BTN_RIGHT = 0, 1, 0x110, 0x111
BUS_USB = 3
TEST_DEVICE_NAME = "winaccel test mouse"


class VirtualMouse:
    """A uinput relative mouse. Each packet is one evdev frame, like a USB HID report."""

    def __init__(self, vid: int, pid: int):
        self.fd = os.open("/dev/uinput", os.O_WRONLY | os.O_NONBLOCK)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(self.fd, UI_SET_EVBIT, EV_REL)
        for btn in (BTN_LEFT, BTN_RIGHT):
            fcntl.ioctl(self.fd, UI_SET_KEYBIT, btn)
        for rel in (REL_X, REL_Y):
            fcntl.ioctl(self.fd, UI_SET_RELBIT, rel)
        setup = struct.pack("<HHHH80sI", BUS_USB, vid, pid, 1, TEST_DEVICE_NAME.encode(), 0)
        fcntl.ioctl(self.fd, UI_DEV_SETUP, setup)
        fcntl.ioctl(self.fd, UI_DEV_CREATE)

    def packet(self, dx: int, dy: int) -> None:
        ev = b""
        if dx:
            ev += struct.pack("<qqHHi", 0, 0, EV_REL, REL_X, dx)
        if dy:
            ev += struct.pack("<qqHHi", 0, 0, EV_REL, REL_Y, dy)
        ev += struct.pack("<qqHHi", 0, 0, EV_SYN, 0, 0)
        os.write(self.fd, ev)

    def close(self) -> None:
        fcntl.ioctl(self.fd, UI_DEV_DESTROY)
        os.close(self.fd)


def kwin_cursor() -> tuple[float, float, dict]:
    """Cursor position (logical pixels) and screen geometry, via a KWin script."""
    token = f"winaccel{random.getrandbits(48):x}"
    js = (f"var g = workspace.virtualScreenGeometry;"
          f"print('{token} ' + workspace.cursorPos.x + ' ' + workspace.cursorPos.y + ' '"
          f" + g.x + ' ' + g.y + ' ' + g.width + ' ' + g.height);")
    with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as f:
        f.write(js)
    try:
        since = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 2))
        sid = run(["qdbus6", KWIN, "/Scripting", "org.kde.kwin.Scripting.loadScript", f.name, token]).stdout.strip()
        run(["qdbus6", KWIN, f"/Scripting/Script{sid}", "org.kde.kwin.Script.run"])
        for _ in range(50):
            out = run(["journalctl", "--user", "--since", since, "-o", "cat", "--grep", token], check=False).stdout
            m = re.search(token + r" (\S+) (\S+) (\S+) (\S+) (\S+) (\S+)", out)
            if m:
                x, y, gx, gy, gw, gh = map(float, m.groups())
                return x, y, {"x": gx, "y": gy, "w": gw, "h": gh}
            time.sleep(0.05)
        raise RuntimeError("could not read the cursor position from KWin")
    finally:
        run(["qdbus6", KWIN, "/Scripting", "org.kde.kwin.Scripting.unloadScript", token], check=False)
        os.unlink(f.name)


def make_test_sequence(rng: random.Random, settings: wb.Settings, room: tuple[float, float],
                       chunks: int = 16, per_chunk: int = 24) -> list[list[tuple[int, int]]]:
    """Mixed slow/fast/diagonal packets that stay inside `room` (physical px from the start)."""
    st = wb.BallisticsState()
    x = y = 0.0
    seq = []
    for _ in range(chunks):
        chunk = []
        tx, ty = rng.uniform(-room[0], room[0]), rng.uniform(-room[1], room[1])
        for _ in range(per_chunk):
            mag = rng.choice([1, 1, 2, 3, 4, 5, 6, 8, 10, 13, 17, 22, 30, 40])
            sx = 1 if tx > x else -1
            sy = 1 if ty > y else -1
            dx = sx * mag if rng.random() < 0.8 else 0
            dy = sy * rng.randint(0, mag) if rng.random() < 0.7 else 0
            probe = dataclasses.replace(st)
            px, py = wb.process_packet(dx, dy, settings, probe)
            if abs(x + px) > room[0] or abs(y + py) > room[1]:
                dx = dy = 0
                px = py = 0
            if dx or dy:
                wb.process_packet(dx, dy, settings, st)
                x += px
                y += py
                chunk.append((dx, dy))
        seq.append(chunk)
    return seq


def explain_axis(seq: list[list[tuple[int, int]]], settings: wb.Settings, axis: int,
                 measured: list[int]) -> tuple[int, int] | None:
    """Find an initial (prev_seg, remainder) that reproduces every measured checkpoint.
    The plugin's state before the test is unknown (it is shared with the real mouse), so
    we search it: remainders are 16.16 values in (-1, 1) px, the segment is 0..3.
    Every checkpoint position is nondecreasing in the initial remainder, so the matching
    remainders form an interval that two binary searches find."""
    def positions(prev: int, rem: int) -> list[int]:
        st = wb.BallisticsState(prev_seg=prev)
        if axis == 0:
            st.rem_x = rem
        else:
            st.rem_y = rem
        pos, out = 0, []
        for chunk in seq:
            for dx, dy in chunk:
                pos += wb.process_packet(dx, dy, settings, st)[axis]
            out.append(pos)
        return out

    def search(prev: int, too_low) -> int:
        lo, hi = -65535, 65536          # smallest rem in [lo, hi) for which too_low is false
        while lo < hi:
            mid = (lo + hi) // 2
            if too_low(positions(prev, mid)):
                lo = mid + 1
            else:
                hi = mid
        return lo

    for prev in range(4):
        rem = search(prev, lambda p: any(a < b for a, b in zip(p, measured)))
        if rem <= 65535 and positions(prev, rem) == measured:
            return prev, rem
    return None


def cmd_test(args) -> None:
    if desktop() != "kde":
        raise SystemExit("error: `winaccel test` reads the cursor through KWin and needs Plasma")
    cfg = Config.load()
    scale, _ = resolve_scale(cfg)
    settings = wb.Settings(epp=cfg.epp, sensitivity=cfg.sensitivity, monitor_dpi=scale_to_dpi(scale),
                           x_curve=cfg.x_curve, y_curve=cfg.y_curve)
    vid, pid = cfg.device_ids()[0] if cfg.devices else (0x1d6b, 0x7a11)

    print("Don't touch the mouse for ~15 seconds (the test moves the cursor itself).")
    mouse = VirtualMouse(vid, pid)
    try:
        dev = None
        for _ in range(100):
            dev = next((d for d in kwin_devices() if d.get("name") == TEST_DEVICE_NAME), None)
            if dev:
                break
            time.sleep(0.05)
        if dev is None:
            raise SystemExit("error: KWin did not pick up the virtual test mouse")
        kwin_configure(dev, True, flat_speed_for_scale(scale))
        time.sleep(0.2)

        x0, y0, geo = kwin_cursor()
        # Walk towards the middle of the screen so no movement gets clipped at an edge.
        for _ in range(8):
            cx, cy = geo["x"] + geo["w"] / 2, geo["y"] + geo["h"] / 2
            if abs(x0 - cx) < geo["w"] / 8 and abs(y0 - cy) < geo["h"] / 8:
                break
            for _ in range(40):
                mouse.packet(int(max(-10, min(10, (cx - x0) / 40))), int(max(-10, min(10, (cy - y0) / 40))))
                time.sleep(0.001)
            time.sleep(0.05)
            x0, y0, geo = kwin_cursor()

        room = (geo["w"] * scale * 0.35, geo["h"] * scale * 0.35)
        seq = make_test_sequence(random.Random(args.seed), settings, room)
        measured_x, measured_y, raw_x, raw_y = [], [], [], []
        rx = ry = 0
        for chunk in seq:
            for dx, dy in chunk:
                mouse.packet(dx, dy)
                rx += dx
                ry += dy
                time.sleep(0.002)
            time.sleep(0.05)
            x, y, _ = kwin_cursor()
            measured_x.append(round((x - x0) * scale))
            measured_y.append(round((y - y0) * scale))
            raw_x.append(rx)
            raw_y.append(ry)
    finally:
        mouse.close()

    n = sum(len(c) for c in seq)
    print(f"Sent {n} packets in {len(seq)} chunks; cursor travelled "
          f"{measured_x[-1]:+d},{measured_y[-1]:+d} px net for {raw_x[-1]:+d},{raw_y[-1]:+d} counts.")
    fx = explain_axis(seq, settings, 0, measured_x)
    fy = explain_axis(seq, settings, 1, measured_y)
    if fx and fy:
        print(f"PASS: all {len(seq)} checkpoints on both axes are reproduced exactly by the Windows 11 "
              f"model (initial plugin state: prev segment {fx[0]}/{fy[0]}).")
        return
    if measured_x == raw_x and measured_y == raw_y:
        print("FAIL: the cursor moved 1:1 with the counts - the plugin is not active. "
              "Log out and back in after `winaccel apply`.")
    else:
        st = wb.BallisticsState()
        px = py = 0
        worst = 0
        for chunk, mx, my in zip(seq, measured_x, measured_y):
            for dx, dy in chunk:
                ox, oy = wb.process_packet(dx, dy, settings, st)
                px += ox
                py += oy
            worst = max(worst, abs(mx - px), abs(my - py))
        print(f"FAIL: the cursor does not follow the Windows model (worst checkpoint is off by {worst} px). "
              f"Is the plugin out of date (`winaccel status`), or another tool changing the pointer?")
    raise SystemExit(1)


# --------------------------------------------------------------------------- main

def main() -> None:
    p = argparse.ArgumentParser(prog="winaccel", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("apply", help="install/update the Windows acceleration plugin and compositor settings")
    g = a.add_mutually_exclusive_group()
    g.add_argument("--epp", dest="epp", action="store_true", default=None,
                   help="turn 'Enhance pointer precision' on (Windows default)")
    g.add_argument("--no-epp", dest="epp", action="store_false", help="turn it off")
    a.add_argument("--sensitivity", type=int, help="Windows pointer speed slider, 1-20 (default 10)")
    a.add_argument("--scale", help="display scale factor, e.g. 1.25, or 'auto' (default)")
    a.add_argument("--x-curve", help="SmoothMouseXCurve: 5 numbers, 5 hex 16.16 values, or 40 registry bytes")
    a.add_argument("--y-curve", help="SmoothMouseYCurve, same formats")
    a.add_argument("--reg", metavar="FILE",
                   help="import a Windows `reg export \"HKCU\\Control Panel\\Mouse\" mouse.reg` file")
    a.add_argument("--device", action="append", metavar="VID:PID",
                   help="only affect this mouse (repeatable, hex ids from lsusb)")
    a.add_argument("--all-devices", action="store_true", help="affect all mice again")
    a.add_argument("--defaults", action="store_true", help="start from the Windows 11 defaults")
    a.add_argument("--no-compositor", action="store_true", help="don't change compositor settings")
    a.add_argument("--dry-run", action="store_true", help="print the generated plugin and exit")
    a.set_defaults(func=cmd_apply)

    r = sub.add_parser("remove", help="uninstall the plugin and restore the previous compositor settings")
    r.set_defaults(func=cmd_remove)

    s = sub.add_parser("status", help="show configuration, plugin and device state")
    s.set_defaults(func=cmd_status)

    sh = sub.add_parser("show", help="print the acceleration curve the configuration produces")
    sh.add_argument("--mouse-dpi", type=int, help="for the hand-speed column (default: from udev)")
    sh.add_argument("--rate", type=int, help="polling rate in Hz, for the hand-speed column")
    sh.set_defaults(func=cmd_show)

    t = sub.add_parser("test", help="verify the installed plugin end to end with a virtual mouse (KDE)")
    t.add_argument("--seed", type=int, default=1)
    t.set_defaults(func=cmd_test)

    args = p.parse_args()
    try:
        args.func(args)
    except (ValueError, OSError) as e:
        raise SystemExit(f"error: {e}")


if __name__ == "__main__":
    main()
