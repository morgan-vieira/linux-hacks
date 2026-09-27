# Mouse acceleration: Windows 11 vs. Linux

## 1. Windows 11

### Source
- **ISO:** `/var/lib/libvirt/images/Win11.iso` (`CCCOMA_X64FRE_EN-GB_DV9`), image 6 of `sources/install.wim` (Windows 11 Pro).
- **Binaries:**
  - `win32kbase.sys` 10.0.26100.9444, sha256 `a78ed89e…5443d`, PDB GUID `D22DFDA3D54B87E6BD0567A57877904C`.
  - `win32kfull.sys`, sha256 `569634de…fc109b`.
- **Method:**
  - Decompiled with Ghidra 12.1.4 using Microsoft's public PDB symbols.
  - Arithmetic checked against the x64 instructions.
  - The relevant functions are in [`docs/decomp/`](docs/decomp).
- **Code location:** all pointer ballistics are in `win32kbase.sys`. `win32kfull.sys` only loads settings (logon and `SystemParametersInfo`).

### Settings (`HKCU\Control Panel\Mouse`)
Values are the defaults in both the `DEFAULT` hive and `Users\Default\NTUSER.DAT`. Full dump: [`docs/registry_dump.txt`](docs/registry_dump.txt).

| Value | Default | What the code does with it |
|---|---|---|
| `MouseSpeed` | `"1"` | On/off only: `EnableMouseAcceleration(MouseSpeed != 0)`. `2` behaves the same as `1`. This is the **Enhance pointer precision** checkbox. |
| `MouseSensitivity` | `"10"` | The pointer-speed slider, 1–20. Invalid values fall back to 10. |
| `SmoothMouseXCurve` | 0, 0.43, 1.25, 3.86, 40 | Five int64 16.16 values (40 bytes, otherwise ignored). These are the **input-speed** points. |
| `SmoothMouseYCurve` | 0, 1.07, 4.14, 18.98, 443.75 | Five **output** points. Despite the names, X and Y are not per-axis curves. |
| `MouseThreshold1/2` | `"6"` / `"10"` | **Unused** for motion. Only `SPI_GETMOUSE` reads them back. |

The same default curves are hard-coded in `CDeviceAcceleration::CreateDefaultAcceleratorCurve`.

### Building the curve (`CDeviceAcceleration::_BuildAccelerationCurve`)
- **When:** once per monitor, at logon, when the slider changes, and on every display-configuration change.
- **Inputs:** the monitor's *effective DPI*, 96 × scale% (96, 120, 144, …).

```
dpi   = max(dpi, 96)
dpiF  = (dpi << 16) / 120                  // 0.8 at 100%, 1.0 at 125%, 1.2 at 150%
sensF = (MouseSensitivity << 16) / 10      // 1.0 at the default slider position
x[k]  = (Xreg[k] * 3.5)                    // 16.16
y[k]  = ((dpiF * Yreg[k]) >> 16) * sensF >> 16
slope[k]     = ((y[k+1] - y[k]) << 16) / (x[k+1] - x[k])    // 0 if the x's are equal
intercept[k] = y[k] - (slope[k] * x[k] >> 16)
```

At the defaults (100% scale, slider 10), the curve maps input speed to output speed per packet as follows:

| Input (counts/packet) | Output (px/packet) |
|---|---|
| 0 | 0 |
| 1.505 | 0.856 |
| 4.375 | 3.312 |
| 13.51 | 15.19 |
| 140 | 355.0 (extrapolated beyond this) |

### Per packet with EPP on (`CDeviceAcceleration::Accelerate`)
This runs once for every hardware report (`MOUSE_INPUT_DATA`), before packets are coalesced. All arithmetic is int64 16.16. `>>` is an arithmetic shift and `/` truncates toward zero.

```
X, Y  = dx << 16, dy << 16
speed = max(|X|,|Y|) + min(|X|,|Y|)/2     // not Euclidean
if speed == 0: return
seg   = first i in 0..3 with speed <= x[i+1], else 3    // beyond the last point: extrapolate
gain  = (intercept[seg] << 16) / speed + slope[seg]
if prevSeg < seg:                                         // first packet in a higher segment
    gain = (gain + (intercept[prevSeg] << 16)/speed + slope[prevSeg]) >> 1
prevSeg = seg
vx = (X*gain >> 16) + remX ; out_x = trunc_toward_zero(vx) ; remX = vx - out_x   // same for y
```

- **Joint axes:** one gain is applied to both axes.
- **Remainders:** kept per axis and never reset, not even on a direction change, an idle period, or switching mouse.
- **Shared state:** the remainders and `prevSeg` are session-global, so all mice share them.

### Per packet with EPP off (`CMouseProcessor::ApplyAccelerationToDelta`)
- The slider selects a factor in units of 1/256:
  - Slider 1–2: s·8.
  - Slider 3–10: (s−2)·32.
  - Slider 11–20: (s−6)·64.
  - That gives 1/32, 1/16, 1/8, 1/4, 3/8 … 7/8, 1, 1.25 … 3.5.
- Above 96 DPI the factor becomes `(f·dpi+48)/96`.
- Each axis becomes `v = f·d + rem; d = v/256; rem = v%256`.
- A factor of exactly 256 passes counts through untouched.

### What is *not* involved
- **Time, polling rate and refresh rate.** Speed is simply counts per packet, so a 1000 Hz mouse gets less acceleration than a 125 Hz mouse at the same hand speed. That is how Windows behaves.
- **Mouse DPI.** Not used.
- **MouseThreshold1/2.** Not used for motion.
- **Raw input.** Games using `WM_INPUT` receive unaccelerated counts. This is inferred, not traced.

### What is commonly believed but wrong for this build
- **Refresh rate does not scale the curve.** The MarkC-fix era documentation says it does; this build doesn't use it.
- **EPP-off speed does scale with display DPI** above 100%.
- **Averaging on segment entry:** the first packet in a higher segment gets the average of the two segments' gains. It is rarely documented.

## 2. Linux (libinput 1.32, KWin 6.7, Wayland)

### The pipeline
```
kernel evdev (REL_X/REL_Y per SYN_REPORT) → libinput [plugins → profile filter] → compositor → cursor
```

libinput offers three pointer acceleration profiles:

- **`flat`** (`filter-flat.c`): `out = in × (1 + speed)`, where `speed` is in [−1, 1].
  - No DPI normalization and no time dependence, so speed 0 is exactly 1:1.
- **`adaptive`** (default, `filter-mouse.c`): the factor depends on *velocity over time*.
  - Deltas are normalized to 1000 DPI (`MOUSE_DPI` from udev hwdb).
  - Velocity is measured in units/µs using a tracker history averaged with Simpson's rule.
  - The profile has three regions:
    - Below 0.07 units/ms: deceleration down to 0.3×.
    - Up to the threshold (0.4 units/ms at speed 0): a flat 1:1 plateau.
    - Above the threshold: a linear incline of 1.1 per unit/ms, capped at a maximum factor of 2.0.
  - The speed setting changes the threshold (0.4 − 0.25·speed units/ms, never below 0.2), the cap (2 + 1.5·speed) and the incline (1.1 + 0.75·speed).
- **`custom`** (`filter-custom.c`, libinput ≥ 1.23): a user-supplied table of points.
  - It maps speed to output speed in device units per millisecond, using the time since the last event, with linear interpolation.
  - It is also time-based, so it cannot reproduce Windows' per-packet behaviour at every polling rate.

Differences from Windows:

- libinput is time-based, so the same packets give different results when the timing differs. Windows ignores time entirely.
- libinput uses Euclidean distance; Windows uses max + min/2.
- libinput computes floating-point motion and the compositor keeps sub-pixel positions. Windows moves whole pixels and carries a remainder forward.
- Windows' pixel units depend on the monitor's DPI and scale. libinput has no concept of the display.

### What the compositor exposes
- **KWin (Plasma 6.7):** only `flat` and `adaptive`, plus the speed setting, through `org.kde.KWin.InputDevice` on D-Bus and `kcminputrc`. It never calls `libinput_config_accel_*`, so no custom curve can be configured.
- **Plugins:** KWin *does* call `libinput_plugin_system_load_plugins()`. This libinput build is linked against Lua 5.4.
  - Plugins in `/etc/libinput/plugins/*.lua` can rewrite each evdev frame before libinput processes it.
  - They are loaded once, when the compositor starts.
  - They run sandboxed: no `io`/`os`, and event values must be integers.

## 3. The design of winaccel
1. **The plugin:** a generated Lua plugin (`/etc/libinput/plugins/50-winaccel.lua`) runs Windows' algorithm on every evdev frame of every mouse and pointing stick.
   - A frame corresponds to one USB HID report, the same unit Windows accelerates.
   - It uses 64-bit integer 16.16 math with the same truncation and remainder rules as Windows. Its state is shared across mice, as on Windows.
   - The curve is precomputed by the same code as the reference model ([`windows_ballistics.py`](windows_ballistics.py)), with the display scale baked in.
2. **The compositor:** those mice are set to libinput's `flat` profile with speed `1/scale − 1`.
   - libinput therefore adds no acceleration of its own.
   - The plugin's output is in physical pixels, as on Windows, and it becomes logical pixels at exactly the right ratio.
3. **Verification:**
   - [`tests/test_plugin.py`](tests/test_plugin.py) runs the generated plugin in Lua 5.4 inside a stub of libinput's plugin API, using the same sandbox.
   - Its output must match the reference model **bit for bit** for:
     - the default settings;
     - all 20 slider positions at 5 display scales;
     - EPP off at every slider position and two scales;
     - random custom curves;
     - speeds landing exactly on curve points.
   - Deliberately introduced bugs are detected.
   - `winaccel test` drives a virtual uinput mouse through the real libinput and KWin and reads the cursor position back.

### Known differences that remain
- **Games (raw input):** on Windows, games reading raw input bypass EPP. Here the plugin changes the counts *before* libinput, so Wayland's relative-pointer protocol (games) sees accelerated motion too. Turn EPP off (`winaccel apply --no-epp`) or `winaccel remove` if a game needs raw counts.
- **Multiple monitors with different scales:** Windows switches curves according to the monitor under the cursor. The plugin cannot see the cursor, so it uses the primary monitor's scale.
- **Changes need a new session:** the compositor only loads plugins when it starts, so new settings need a log-out/log-in. A change of display scale also means running `winaccel apply` again; `winaccel status` detects it.
- **New mice:** a mouse KWin has never seen starts with `adaptive`. Run `winaccel apply` again after plugging it in (`winaccel status` flags it).
- **Touchpads:** not handled. Windows uses a separate `CTouchpadAcceleration` curve for them, and the plugin leaves touchpads alone.
