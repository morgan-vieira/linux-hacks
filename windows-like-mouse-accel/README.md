# winaccel

winaccel makes Linux mouse acceleration behave exactly like Windows 11's **Enhance pointer precision**, including the pointer-speed slider and the `SmoothMouseXCurve`/`SmoothMouseYCurve` registry curves.

The algorithm was reverse-engineered from `win32kbase.sys` on the Windows 11 24H2 ISO. It runs as a libinput Lua plugin with bit-exact integer math, and libinput's own acceleration is switched to `flat` so nothing is applied twice. See [ANALYSIS.md](ANALYSIS.md) for how both systems work.

## Requirements
- libinput ≥ 1.30 built with Lua (Arch/CachyOS: yes).
- A compositor that loads libinput plugins. KWin (Plasma 6) does; this is what winaccel is tested with.
- Python ≥ 3.10 (standard library only), `busctl`, `pkexec`, and `kscreen-doctor` on KDE.
- `lua5.4` for the tests.

## Usage
Commands:

| Command | What it does |
|---|---|
| `winaccel apply` | Uses the Windows 11 defaults (EPP on, slider 10). Asks for your password once to install the plugin. |
| `winaccel apply --sensitivity 12` | Sets the pointer-speed slider (1–20, 10 = middle notch). |
| `winaccel apply --no-epp` | Turns "Enhance pointer precision" off (linear, slider-scaled). |
| `winaccel apply --reg mouse.reg` | Copies your settings from a Windows install. Create the file on Windows with `reg export "HKCU\Control Panel\Mouse" mouse.reg`. |
| `winaccel apply --x-curve 0,0.43,1.25,3.86,40 --y-curve ...` | Uses custom SmoothMouse curves (numbers, `0x` 16.16 values, or registry bytes). |
| `winaccel apply --device 1532:0099` | Limits winaccel to one mouse (`--all-devices` undoes it). |
| `winaccel apply --scale 1.25` | Overrides the auto-detected display scale. |
| `winaccel apply --dry-run` | Prints the generated plugin without installing anything. |
| `winaccel show` | Prints the resulting curve (counts/packet → pixels and gain). |
| `winaccel status` | Checks that the plugin is current and every mouse is on flat 1:1. |
| `winaccel test` | End-to-end check through the real libinput and KWin using a virtual mouse. |
| `winaccel remove` | Uninstalls the plugin and restores the previous KWin settings. |

After `apply` changes the plugin, **log out and back in**, because KWin loads plugins when it starts. Then run `winaccel test`.

Settings are stored in `~/.config/winaccel/config.json`. The generated plugin is `/etc/libinput/plugins/50-winaccel.lua`, and it applies to all users.

## Behaviour that matches Windows
- **Polling rate:** acceleration is computed per hardware packet with no time involved. A higher polling rate therefore means less acceleration at the same hand speed, exactly as on Windows.
- **Display scale:** it changes the curve (×dpi/120), as it does on Windows.

## Tests
```
python3 tests/test_plugin.py
```
This runs the generated plugin in Lua 5.4 inside a stub of libinput's plugin sandbox and compares every output packet with the Windows reference model ([windows_ballistics.py](windows_ballistics.py)).
