#!/usr/bin/env python3
"""Checks the generated libinput Lua plugin against the Windows 11 reference model.

The plugin is executed by a real Lua 5.4 interpreter inside a stub of libinput's
plugin API (tests/harness.lua); every output packet must match
windows_ballistics.process_packet exactly.

    python3 tests/test_plugin.py
"""
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import windows_ballistics as wb  # noqa: E402
import winaccel  # noqa: E402

HARNESS = ROOT / "tests" / "harness.lua"
MOUSE = "ID_INPUT,ID_INPUT_MOUSE"

# MarkC's Windows 10 "1:1" fix curve for 100% scaling (a well known custom curve).
MARKC_X = [0, 0xCCCC0, 0x199980, 0x266640, 0x333300]
MARKC_Y = [0, 0x380000, 0x700000, 0xA80000, 0xE00000]


def random_packets(rng, n):
    packets = []
    for _ in range(n):
        kind = rng.random()
        if kind < 0.1:
            packets.append((0, 0))
        elif kind < 0.4:          # slow
            packets.append((rng.randint(-3, 3), rng.randint(-3, 3)))
        elif kind < 0.8:          # medium
            packets.append((rng.randint(-25, 25), rng.randint(-25, 25)))
        elif kind < 0.95:         # fast flicks
            packets.append((rng.randint(-200, 200), rng.randint(-200, 200)))
        else:                     # single axis
            v = rng.randint(-60, 60)
            packets.append((v, 0) if rng.random() < 0.5 else (0, v))
    return packets


def run_plugin(cfg, scale, packets, vid=0x1532, pid=0x0099, props=MOUSE):
    with tempfile.NamedTemporaryFile("w", suffix=".lua", delete=False) as f:
        f.write(winaccel.generate_plugin(cfg, scale))
    try:
        stdin = "".join(f"{dx} {dy}\n" for dx, dy in packets)
        r = subprocess.run(["lua5.4", str(HARNESS), f.name, str(vid), str(pid), props],
                           input=stdin, text=True, capture_output=True)
        if r.returncode != 0:
            raise AssertionError(r.stderr)
        lines = r.stdout.split("\n")
        if lines[0] != "attached":
            return None
        return [tuple(map(int, line.split())) for line in lines[1:1 + len(packets)]]
    finally:
        Path(f.name).unlink()


def expected(cfg, scale, packets):
    s = wb.Settings(epp=cfg.epp, sensitivity=cfg.sensitivity, monitor_dpi=winaccel.scale_to_dpi(scale),
                    x_curve=cfg.x_curve, y_curve=cfg.y_curve)
    st = wb.BallisticsState()
    return [wb.process_packet(dx, dy, s, st) for dx, dy in packets]


class PluginMatchesWindows(unittest.TestCase):
    def check(self, cfg, scale, seed, n=6000):
        packets = random_packets(random.Random(seed), n)
        got = run_plugin(cfg, scale, packets)
        want = expected(cfg, scale, packets)
        for i, (g, w) in enumerate(zip(got, want)):
            if g != w:
                self.fail(f"packet {i} {packets[i]}: plugin {g}, Windows {w}")
        self.assertEqual(len(got), len(want))

    def test_windows_defaults(self):
        self.check(winaccel.Config(), 1.0, 1)

    def test_every_sensitivity_and_scale(self):
        for sens in range(1, 21):
            for scale in (1.0, 1.25, 1.5, 1.75, 2.0):
                with self.subTest(sens=sens, scale=scale):
                    self.check(winaccel.Config(sensitivity=sens), scale, sens * 100 + int(scale * 4), n=800)

    def test_epp_off(self):
        for sens in range(1, 21):
            for scale in (1.0, 1.5):
                with self.subTest(sens=sens, scale=scale):
                    self.check(winaccel.Config(epp=False, sensitivity=sens), scale, sens, n=800)

    def test_custom_curves(self):
        self.check(winaccel.Config(x_curve=MARKC_X, y_curve=MARKC_Y), 1.0, 7)
        rng = random.Random(3)
        for i in range(20):
            xs = sorted(rng.randint(1, 60 << 16) for _ in range(4))
            ys = sorted(rng.randint(0, 600 << 16) for _ in range(4))
            with self.subTest(curve=i):
                self.check(winaccel.Config(x_curve=[0] + xs, y_curve=[0] + ys,
                                           sensitivity=rng.randint(1, 20)), rng.choice([1.0, 1.25, 2.0]), i, n=800)

    def test_speed_exactly_on_curve_points(self):
        # x = 1,2,4,8 * 3.5 = 3.5, 7, 14, 28 counts/packet: speeds max+min/2 reach them exactly,
        # which pins down the segment tie rule (a speed equal to x[i] uses the lower segment).
        cfg = winaccel.Config(x_curve=[0, 0x10000, 0x20000, 0x40000, 0x80000],
                              y_curve=[0, 0x30000, 0x80000, 0x180000, 0x500000])
        on_points = [(3, 1), (-3, 1), (7, 0), (0, -7), (6, 2), (14, 0), (12, 4), (28, 0), (-24, 8)]
        packets = [p for _ in range(4) for q in on_points for p in (q, (1, 0), q, q, (40, 3))]
        self.assertEqual(run_plugin(cfg, 1.0, packets), expected(cfg, 1.0, packets))

    def test_markc_curve_is_one_to_one(self):
        # Sanity check of the model itself: MarkC's 1:1 curve at 100% maps counts to pixels 1:1.
        packets = [(d, 0) for d in range(1, 128)] + [(-d, d) for d in range(1, 64)]
        got = run_plugin(winaccel.Config(x_curve=MARKC_X, y_curve=MARKC_Y), 1.0, packets)
        self.assertEqual(got, packets)

    def test_device_filter(self):
        cfg = winaccel.Config(devices=["1532:0099"])
        self.assertIsNotNone(run_plugin(cfg, 1.0, [(1, 1)], vid=0x1532, pid=0x0099))
        self.assertIsNone(run_plugin(cfg, 1.0, [(1, 1)], vid=0x046d, pid=0xc077))

    def test_touchpads_and_non_mice_are_left_alone(self):
        cfg = winaccel.Config()
        self.assertIsNone(run_plugin(cfg, 1.0, [(1, 1)], props="ID_INPUT,ID_INPUT_MOUSE,ID_INPUT_TOUCHPAD"))
        self.assertIsNone(run_plugin(cfg, 1.0, [(1, 1)], props="ID_INPUT,ID_INPUT_KEYBOARD"))
        self.assertIsNotNone(run_plugin(cfg, 1.0, [(1, 1)], props="ID_INPUT,ID_INPUT_POINTINGSTICK"))


class RegImport(unittest.TestCase):
    REG = (
        'Windows Registry Editor Version 5.00\r\n\r\n'
        '[HKEY_CURRENT_USER\\Control Panel\\Mouse]\r\n'
        '"MouseSensitivity"="14"\r\n'
        '"MouseSpeed"="1"\r\n'
        '"MouseThreshold1"="6"\r\n'
        '"SmoothMouseXCurve"=hex:00,00,00,00,00,00,00,00,c0,cc,0c,00,00,00,00,00,80,99,\\\r\n'
        '  19,00,00,00,00,00,40,66,26,00,00,00,00,00,00,33,33,00,00,00,00,00\r\n'
        '"SmoothMouseYCurve"=hex:00,00,00,00,00,00,00,00,00,00,38,00,00,00,00,00,00,00,\\\r\n'
        '  70,00,00,00,00,00,00,00,a8,00,00,00,00,00,00,00,e0,00,00,00,00,00\r\n'
    )

    def test_utf16_reg_export(self):
        with tempfile.NamedTemporaryFile("wb", suffix=".reg", delete=False) as f:
            f.write(b"\xff\xfe" + self.REG.encode("utf-16-le"))
        try:
            cfg = winaccel.Config()
            winaccel.apply_reg_values(cfg, winaccel.parse_reg_file(Path(f.name)))
        finally:
            Path(f.name).unlink()
        self.assertEqual(cfg.sensitivity, 14)
        self.assertTrue(cfg.epp)
        self.assertEqual(cfg.x_curve, [0, 0xCCCC0, 0x199980, 0x266640, 0x333300])
        self.assertEqual(cfg.y_curve, [0, 0x380000, 0x700000, 0xA80000, 0xE00000])

    def test_curve_formats(self):
        self.assertEqual(winaccel.parse_curve("0,0.43,1.25,3.86,40"),
                         [0, round(0.43 * 65536), 0x14000, round(3.86 * 65536), 0x280000])
        self.assertEqual(winaccel.parse_curve("0x0,0x6E15,0x14000,0x3DC29,0x280000"), wb.DEFAULT_X_CURVE)
        reg = ("00,00,00,00,00,00,00,00,15,6e,00,00,00,00,00,00,00,40,01,00,00,00,00,00,"
               "29,dc,03,00,00,00,00,00,00,00,28,00,00,00,00,00")
        self.assertEqual(winaccel.parse_curve(reg), wb.DEFAULT_X_CURVE)

    def test_scale_to_dpi(self):
        self.assertEqual([winaccel.scale_to_dpi(s) for s in (1, 1.25, 1.5, 1.75, 2)], [96, 120, 144, 168, 192])


if __name__ == "__main__":
    unittest.main(verbosity=2)
