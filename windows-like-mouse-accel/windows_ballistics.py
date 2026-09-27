#!/usr/bin/env python3
"""
Reference implementation of Windows 11 (build 10.0.26100.9444, win32kbase.sys)
mouse pointer ballistics ("Enhance pointer precision" on/off).

Every arithmetic step mirrors the x64 code of:
  CDeviceAcceleration::_BuildAccelerationCurve        win32kbase!0x1401b31b4
  CDeviceAcceleration::Accelerate                     win32kbase!0x1400e8448
  CMouseProcessor::ApplyAccelerationToDelta           win32kbase!0x1400e8174
  GetNormalizedMouseSensitivityFactor                 win32kbase!0x1400e8858
  CMouseAcceleration::MOUSE_SENSITIVITY_INFO::UpdateMouseSensitivity  win32kbase!0x1401a7840
  CDeviceAcceleration::CreateDefaultAcceleratorCurve  win32kbase!0x1400cf160

All intermediate values are wrapped to the C integer width used by the machine
code (int64 for the EPP path, int32 for the non-EPP path) so the results are
bit-exact, including for pathological registry curves.
"""
from __future__ import annotations
from dataclasses import dataclass, field
import struct

# ---------------------------------------------------------------- integer helpers
M64 = (1 << 64) - 1
M32 = (1 << 32) - 1


def s64(v: int) -> int:
    v &= M64
    return v - (1 << 64) if v >> 63 else v


def s32(v: int) -> int:
    v &= M32
    return v - (1 << 32) if v >> 31 else v


def sar(v: int, n: int) -> int:          # arithmetic shift right (x86 SAR), v already signed
    return v >> n


def idiv(a: int, b: int) -> int:        # x86 IDIV / C '/': truncation toward zero
    q = abs(a) // abs(b)
    return q if (a >= 0) == (b >= 0) else -q


def irem(a: int, b: int) -> int:        # C '%': remainder has the sign of the dividend
    return a - idiv(a, b) * b


# ---------------------------------------------------------------- defaults
# Built-in fallback curves (CreateDefaultAcceleratorCurve), identical to the
# registry defaults in HKCU\Control Panel\Mouse (DEFAULT hive and Default NTUSER.DAT).
DEFAULT_X_CURVE = [0x0, 0x6E15, 0x14000, 0x3DC29, 0x280000]       # 16.16: 0, 0.43, 1.25, 3.86, 40
DEFAULT_Y_CURVE = [0x0, 0x111FD, 0x42400, 0x12FC00, 0x1BBC000]    # 16.16: 0, 1.07, 4.14, 18.98, 443.75


def parse_curve_reg_binary(raw: bytes) -> list[int] | None:
    """REG_BINARY SmoothMouse?Curve -> 5 signed int64 (16.16). Windows accepts the value
    only if it is exactly 40 bytes, otherwise it keeps the previous/built-in curve."""
    if len(raw) != 40:
        return None
    return list(struct.unpack('<5q', raw))


def sensitivity_factor_256(sens: int) -> int:
    """MOUSE_SENSITIVITY_INFO::UpdateMouseSensitivity: slider 1..20 -> factor in 1/256 units
    (used only when EPP is OFF)."""
    if not 1 <= sens <= 20:
        raise ValueError("MouseSensitivity must be 1..20 (Windows falls back to the default, 10)")
    if sens < 3:
        return (sens & 0xFFFFFF) << 3                 # 1->8, 2->16
    if sens < 11:
        return ((sens * 256 - 0x200) & M32) >> 3       # (s-2)*32 : 3->32 ... 10->256
    return ((sens * 256 - 0x600) & M32) >> 2           # (s-6)*64 : 11->320 ... 20->896


def normalized_sensitivity_factor(sens: int, monitor_dpi: int | None) -> int:
    """GetNormalizedMouseSensitivityFactor: factor scaled by monitor effective DPI/96 when DPI > 96
    (MulDiv-style rounding: (f*dpi + 48) / 96)."""
    f = sensitivity_factor_256(sens)
    if monitor_dpi is not None and monitor_dpi > 96:
        q = (abs(f) * monitor_dpi + 0x30) // 96
        if q > 0x7FFFFFFF:
            return 0x7FFFFFFF if f >= 0 else -0x80000000
        return -q if f < 0 else q
    return f


# ---------------------------------------------------------------- curve construction
@dataclass
class Ballistics:
    x: list[int]          # 5 thresholds, speed units (16.16 "mickeys per packet", max+min/2 metric)
    y: list[int]          # 5 outputs (16.16 pixels per packet)
    slope: list[int]      # 4 segment slopes (16.16)
    intercept: list[int]  # 4 segment intercepts (16.16)


def build_ballistics(x_curve=DEFAULT_X_CURVE, y_curve=DEFAULT_Y_CURVE, sens: int = 10,
                     monitor_dpi: int = 96) -> Ballistics:
    """CDeviceAcceleration::_BuildAccelerationCurve(curve, dpi=region->effective DPI, sens=slider).
    Called per monitor (input-space region) at logon, on MouseSensitivity change (SPI_SETMOUSESPEED)
    and on display-configuration changes (ResetAccelerationCurves)."""
    dpi = monitor_dpi & 0xFFFF
    if dpi < 0x60:
        dpi = 0x60                                           # clamp to >= 96
    dpi_f = (dpi << 16) // 120                               # unsigned, floor   (96 -> 52428 = 0.79999)
    sens_f = (sens << 16) // 10                              # unsigned, floor   (10 -> 65536 = 1.0)
    x, y = [], []
    for k in range(5):
        yy = sar(s64(dpi_f * s64(y_curve[k])), 16)
        yy = sar(s64(yy * sens_f), 16)
        y.append(yy)
        x.append(sar(s64(s64(x_curve[k]) * 0x38000), 16))   # * 3.5
    slope, icpt = [], []
    for k in range(1, 5):
        dx = s64(x[k] - x[k - 1])
        if dx == 0:
            m, b = 0, 0
        else:
            m = s64(idiv(s64((y[k] - y[k - 1]) << 16), dx))
            b = s64(y[k - 1] - sar(s64(m * x[k - 1]), 16))
        slope.append(m)
        icpt.append(b)
    return Ballistics(x, y, slope, icpt)


# ---------------------------------------------------------------- state
@dataclass
class BallisticsState:
    # EPP path (session-global in Windows: UserSessionState+0x4fa0/+0x4fa8/+0x4fb0)
    rem_x: int = 0          # 16.16 sub-pixel remainder, sign follows the last result
    rem_y: int = 0
    prev_seg: int = 0       # uint32, segment index used by the previous packet
    # EPP-off path (CMouseProcessor+0x24/+0x28), units of 1/256 pixel
    rem256_x: int = 0
    rem256_y: int = 0


@dataclass
class Settings:
    epp: bool = True                    # MouseSpeed != 0  (1 and 2 behave identically)
    sensitivity: int = 10               # MouseSensitivity 1..20
    monitor_dpi: int = 96               # effective DPI of the monitor under the cursor (96*scale%/100)
    x_curve: list = field(default_factory=lambda: list(DEFAULT_X_CURVE))
    y_curve: list = field(default_factory=lambda: list(DEFAULT_Y_CURVE))
    _cache: dict = field(default_factory=dict, repr=False)

    def ballistics(self) -> Ballistics:
        key = (tuple(self.x_curve), tuple(self.y_curve), self.sensitivity, self.monitor_dpi)
        b = self._cache.get(key)
        if b is None:
            b = self._cache[key] = build_ballistics(self.x_curve, self.y_curve,
                                                    self.sensitivity, self.monitor_dpi)
        return b


# ---------------------------------------------------------------- per-packet processing
def _truncate_16(v: int) -> tuple[int, int]:
    """Split a signed 16.16 value into (integer part toward zero, remainder)."""
    if v < 0:
        ip = -((-v) & ~0xFFFF)
    else:
        ip = v & ~0xFFFF
    ip = s64(ip)
    return ip >> 16, s64(v - ip)


def accelerate(dx: int, dy: int, b: Ballistics, st: BallisticsState) -> tuple[int, int]:
    """CDeviceAcceleration::Accelerate (EPP on)."""
    X = s64(s32(dx) << 16)
    Y = s64(s32(dy) << 16)
    ax, ay = abs(X), abs(Y)
    mn, mx = (ay, ax) if ay <= ax else (ax, ay)
    speed = s64(idiv(mn, 2) + mx)                       # max + min/2 in 16.16
    if speed == 0:
        return dx, dy                                   # nothing happens, state untouched
    n = 5
    i = 0
    while i < n - 1:
        if speed <= b.x[i]:
            break
        i += 1
    seg = (i - 1) & M32
    if seg >= 4:
        # speed <= x[0]: Windows would index intercept[0xFFFFFFFF] (out-of-bounds kernel read).
        raise ValueError("speed <= first X-curve point: undefined behaviour in Windows")
    gain = s64(idiv(s64(b.intercept[seg] << 16), speed) + b.slope[seg])
    if st.prev_seg < seg:                               # unsigned compare
        g_prev = s64(idiv(s64(b.intercept[st.prev_seg] << 16), speed) + b.slope[st.prev_seg])
        gain = sar(s64(gain + g_prev), 1)
    st.prev_seg = seg
    vx = s64(sar(s64(X * gain), 16) + st.rem_x)
    vy = s64(sar(s64(Y * gain), 16) + st.rem_y)
    ox, st.rem_x = _truncate_16(vx)
    oy, st.rem_y = _truncate_16(vy)
    return s32(ox), s32(oy)


def scale_no_accel(dx: int, dy: int, factor: int, st: BallisticsState) -> tuple[int, int]:
    """ApplyAccelerationToDelta, EPP off branch."""
    if factor == 0x100:
        return dx, dy        # exact 1:1; Windows only randomises the (display-only) sub-pixel field
    if dx != 0:
        v = s32(factor * dx + st.rem256_x)
        dx, st.rem256_x = idiv(v, 256), irem(v, 256)
    if dy != 0:
        v = s32(factor * dy + st.rem256_y)
        dy, st.rem256_y = idiv(v, 256), irem(v, 256)
    return dx, dy


def process_packet(dx: int, dy: int, settings: Settings, state: BallisticsState) -> tuple[int, int]:
    """One MOUSE_INPUT_DATA relative packet (mickeys) -> integer pixel delta added to the cursor.
    Rotation (NtSetShellCursorState, default identity) and cursor clipping are applied afterwards
    by Windows and do not feed back into the state."""
    if settings.epp:
        return accelerate(dx, dy, settings.ballistics(), state)
    f = normalized_sensitivity_factor(settings.sensitivity, settings.monitor_dpi)
    return scale_no_accel(dx, dy, f, state)


# ---------------------------------------------------------------- demo / tables
def _fmt16(v: int) -> str:
    return f"{v:>10d} ({v / 65536:10.5f})"


def dump_curve(sens=10, dpi=96):
    b = build_ballistics(sens=sens, monitor_dpi=dpi)
    print(f"internal curve  sens={sens} dpi={dpi}")
    for k in range(5):
        print(f"  x[{k}]={_fmt16(b.x[k])}  y[{k}]={_fmt16(b.y[k])}")
    for k in range(4):
        print(f"  seg{k}: slope={_fmt16(b.slope[k])} intercept={_fmt16(b.intercept[k])}")


def example_table(dpi=96, sens=10, epp=True, deltas=(1, 2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 40, 50, 64, 100, 127)):
    s = Settings(epp=epp, sensitivity=sens, monitor_dpi=dpi)
    print(f"\nEPP={'on' if epp else 'off'}  MouseSensitivity={sens}  monitor DPI={dpi}"
          f"  (refresh rate is not used by the code)")
    print(f"  {'dx':>5} | {'1st pkt':>7} | {'avg px/pkt over 1000 pkts':>26} | {'gain':>7} | first 8 outputs (fresh state)")
    for d in deltas:
        st = BallisticsState()
        outs = [process_packet(d, 0, s, st)[0] for _ in range(1000)]
        avg = sum(outs) / len(outs)
        print(f"  {d:>5} | {outs[0]:>7} | {avg:>26.4f} | {avg / d:7.4f} | {outs[:8]}")


if __name__ == "__main__":
    dump_curve(10, 96)
    example_table(96, 10, True)
    example_table(120, 10, True, deltas=(1, 2, 3, 5, 10, 20, 50))
    example_table(144, 10, True, deltas=(1, 2, 3, 5, 10, 20, 50))
    example_table(96, 6, True, deltas=(1, 2, 3, 5, 10, 20, 50))
    example_table(96, 10, False, deltas=(1, 2, 3, 5, 10, 20, 50))
    example_table(96, 6, False, deltas=(1, 2, 3, 5, 10, 20, 50))
    example_table(144, 10, False, deltas=(1, 2, 3, 5, 10, 20, 50))
    print("\nsensitivity table (EPP off, 1/256 units):",
          {s: sensitivity_factor_256(s) for s in range(1, 21)})
    # diagonal example
    st = BallisticsState(); s = Settings()
    print("\ndiagonal (3,4) x5 at defaults:", [process_packet(3, 4, s, st) for _ in range(5)], st)
