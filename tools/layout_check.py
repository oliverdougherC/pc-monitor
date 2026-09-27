#!/usr/bin/env python
"""Layout self-check: render every state with pathological values and fail on
any text collision, panel overflow, or off-screen draw.

The layouts are hand-placed pixel geometry, so the failure mode of a tweak is
always the same: a value grows a digit and either crosses a panel border or
lands on its neighbour. That is invisible in a pretty screenshot and shows up on
the panel the day a sensor spikes. This renders the pathological cases and
asserts the geometry holds.

    .venv\\Scripts\\python tools\\layout_check.py            # both states, trends on
    .venv\\Scripts\\python tools\\layout_check.py --no-trends  # serial budget mode too

Exit code 1 with a list of offending element pairs on failure.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
# rendered strings include ° and the console may be cp1252
for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - non-reconfigurable pipes are fine
        pass

from app import config as cfgmod  # noqa: E402
from app import layout as layout_mod  # noqa: E402
from app.snapshot import Snapshot  # noqa: E402

# worst-case strings per slot: 3-digit temps, 100% load, 4-digit rates, 4-digit fps
EXTREMES = {
    "idle": dict(temp=100.0, load=100.0, clock_max=5950.0, clock_avg=5950.0,
                 cpu_w=170.0, g_temp=100.0, g_load=100.0, core=2950.0, gpu_w=999.0,
                 vram_used=32_768.0, vram_total=32_768.0, ram_used=65_536.0,
                 ram_total=65_536.0, read=999.9e6, write=999.9e6,
                 down=999.9e6, up=999.9e6, total_w=1000.0),
    "game": dict(temp=100.0, load=100.0, clock_max=5950.0, clock_avg=5950.0,
                 cpu_w=170.0, g_temp=100.0, g_load=100.0, core=2950.0, gpu_w=999.0,
                 vram_used=32_768.0, vram_total=32_768.0, ram_used=65_536.0,
                 ram_total=65_536.0, read=999.9e6, write=999.9e6,
                 down=999.9e6, up=999.9e6, total_w=1000.0,
                 fps=999.0, low1=999.0, low01=999.0),
}
BLANK = {k: None for k in EXTREMES["idle"]}
BLANK.update(fps=None, low1=None, low01=None)


def build(case: dict, state: str, simulated: bool = False) -> Snapshot:
    s = Snapshot()
    s.cpu.load_pct, s.cpu.temp_c = case["load"], case["temp"]
    s.cpu.clock_max_mhz, s.cpu.clock_avg_mhz = case["clock_max"], case["clock_avg"]
    s.cpu.power_w = case["cpu_w"]
    s.gpu.load_pct, s.gpu.temp_c = case["g_load"], case["g_temp"]
    s.gpu.core_mhz, s.gpu.power_w = case["core"], case["gpu_w"]
    s.gpu.vram_used_mb, s.gpu.vram_total_mb = case["vram_used"], case["vram_total"]
    s.ram_used_mb, s.ram_total_mb = case["ram_used"], case["ram_total"]
    s.disk_read_bps, s.disk_write_bps = case["read"], case["write"]
    s.net_down_bps, s.net_up_bps = case["down"], case["up"]
    s.frames.fps, s.frames.low1_pct = case.get("fps"), case.get("low1")
    s.frames.low01_pct, s.frames.latency_ms = case.get("low01"), None
    # The simulated pane earns its own pass: the badge is a new run in a panel that
    # is already full, and it only appears over invented numbers — which is exactly
    # when nobody thinks to look for a collision.
    s.frames.simulated = simulated
    s.power_total_w = case["total_w"]
    return s


def panels_for(state: str):
    return {"cpu": layout_mod.TOP_BOX["cpu"], "gpu": layout_mod.TOP_BOX["gpu"],
            **{f"bot:{k}": v for k, v in layout_mod.BOT_BOX[state].items()},
            "power": layout_mod.POWER_BOX}


def check_state(layout, state: str, case: dict, label: str, simulated: bool = False):
    """Render and audit: every text run must sit inside its own panel and must
    not touch any other text run."""
    drawn: list[tuple[str, tuple, str]] = []
    orig_txt = layout_mod.Layout._txt

    def spy(self, d, xy, text, font, fill, anchor="la"):
        orig_txt(self, d, xy, text, font, fill, anchor)
        try:
            box = d.textbbox(xy, text, font=self._font(font[0], font[1]), anchor=anchor)
        except Exception:  # noqa: BLE001 - anchor/emoji edge cases
            return
        drawn.append((str(text), box, f"{font[0].split('/')[-1]}@{font[1]}"))

    layout_mod.Layout._txt = spy
    try:
        layout.render(build(case, state, simulated), state, (3, 3))   # worst case: shifted
    finally:
        layout_mod.Layout._txt = orig_txt

    problems: list[str] = []
    panels = panels_for(state)

    for text, (x0, y0, x1, y1), font in drawn:
        if (x0, y0, x1, y1) == (0, 0, 0, 0) or text.strip() in ("", "--"):
            continue
        if x0 < 0 or y0 < 0 or x1 > layout.w or y1 > layout.h:
            problems.append(f"'{text}' [{font}] off-canvas at {x0},{y0},{x1},{y1}")
            continue
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        home = [n for n, (a, b, c, e) in panels.items() if a <= cx <= c and b <= cy <= e]
        if not home:
            problems.append(f"'{text}' [{font}] at {x0},{y0},{x1},{y1} sits outside every panel")
            continue
        for n in home:
            a, b, c, e = panels[n]
            if x0 < a - 1 or x1 > c + 1 or y0 < b - 1 or y1 > e + 1:
                problems.append(f"'{text}' [{font}] escapes panel {n} "
                                f"({x0}..{x1} vs {a}..{c}, y {y0}..{y1} vs {b}..{e})")

    for i in range(len(drawn)):
        for j in range(i + 1, len(drawn)):
            t1, a, f1 = drawn[i]
            t2, b, f2 = drawn[j]
            if a == (0, 0, 0, 0) or b == (0, 0, 0, 0):
                continue
            ox = min(a[2], b[2]) - max(a[0], b[0])
            oy = min(a[3], b[3]) - max(a[1], b[1])
            if ox > 1 and oy > 1:
                problems.append(f"overlap {ox}x{oy}px: '{t1}' [{f1}] {a} vs '{t2}' "
                                f"[{f2}] {b}")
    return [f"[{state}/{label}] {p}" for p in problems]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=None)
    ap.add_argument("--no-trends", action="store_true", help="also check serial budget mode")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    modes = [("trends", True)] + ([("no-trends", False)] if args.no_trends else [])
    problems: list[str] = []
    for state in ("idle", "game"):
        for mode_name, bands in modes:
            cfg["layout"]["trend_bands"] = bands
            layout = layout_mod.Layout(cfg, rate_hz=1.0)
            problems += check_state(layout, state, EXTREMES[state],
                                    f"{mode_name}/extreme")
            problems += check_state(layout, state, BLANK, f"{mode_name}/blank")
            if state == "game":
                # Same worst-case digits, with the values marked as invented: the
                # SIMULATED badge lands on the frames panel's own title row, and a
                # preview is precisely where a bad fit would never be noticed.
                problems += check_state(layout, state, EXTREMES[state],
                                        f"{mode_name}/simulated", simulated=True)

    if problems:
        print(f"FAIL — {len(problems)} layout problem(s):")
        for p in problems:
            print("  -", p)
        print("SELFTEST FAILED: layout")
        return 1
    print("ok — no collisions, no panel overflow, both states, all value extremes, "
          "invented frame values included")
    # The gate reads this line rather than trusting the exit code: a check that
    # stops early can still exit 0, and that looks exactly like a check that ran.
    print("SELFTEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
