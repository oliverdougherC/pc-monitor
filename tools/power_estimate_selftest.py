"""The power total never turns missing telemetry into an apparently-measured watt figure.

    .venv\\Scripts\\python tools\\power_estimate_selftest.py

`app/power.py::estimate` used to assign zero watts to a CPU or GPU it could
not read and still return a numeric total, which main.py wrote into the
snapshot - so with every sensor unavailable the shipped base value could stand
on the panel as "TOTAL POWER 34 W", and with one component missing the total
silently excluded it. It also never said whether its inputs came from sensors
or from the load model, or how old they were.

The cases below drive the real model and the real power strip (the layout is
rendered with every text call captured, so the assertions are about what the
panel would draw, not only about what the model returned). Expected watts are
written out from the documented formula, never imported from the
implementation, so a change to a constant fails here instead of being echoed.
"""
from __future__ import annotations

import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from app.layout import Layout               # noqa: E402
from app.power import estimate, power_watts_for_display   # noqa: E402
from app.snapshot import Snapshot           # noqa: E402

sys.stdout.reconfigure(errors="replace")
from PIL import ImageFont                   # noqa: E402

fails: list[str] = []

# A real wall-clock stamp taken once, up front. The model ages snapshots
# against a `now` we pass (deterministic), and the rendered strip ages against
# the real clock - both sit within milliseconds of this one, so "fresh" cases
# are fresh and the stale cases (stamped NOW - 30) are stale in both paths.
NOW = time.time()


def check(name: str, got, want) -> None:
    ok = got == want
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def check_close(name: str, got, want) -> None:
    ok = got is not None and want is not None and abs(got - want) < 1e-6
    print(f"  {'ok  ' if ok else 'FAIL'} {name}: got {got!r}" + ("" if ok else f" want {want!r}"))
    if not ok:
        fails.append(name)


def cfg(**power_over) -> dict:
    """A config carrying only what the model and the strip read. The power
    values are literals on purpose: base 34, +9% rails, 170/300 W TDPs."""
    p = {"base_w": 34, "rail_overhead_pct": 9, "cpu_tdp": 170, "gpu_tdp": 300,
         "gradient_min_w": 50, "gradient_max_w": 1000, "gradient_gamma": 0.75}
    p.update(power_over)
    return {"power": p,
            "sensors": {"interval_s": 1.0},
            "layout": {"font_value": "a.ttf", "font_label": "b.ttf",
                       "font_small": "c.ttf", "trend_window_s": 60,
                       "trend_bands": True}}


def snap(ts: float, cpu_w=None, cpu_load=None, gpu_w=None, gpu_load=None) -> Snapshot:
    s = Snapshot(ts=ts)
    s.cpu.power_w, s.cpu.load_pct = cpu_w, cpu_load
    s.gpu.power_w, s.gpu.load_pct = gpu_w, gpu_load
    return s


def modelled(load_pct: float, tdp: float, idle_w: float) -> float:
    """The documented curve, re-derived here: idle + (tdp-idle) * load^1.25."""
    return idle_w + (tdp - idle_w) * (load_pct / 100.0) ** 1.25


# --------------------------------------------------------------- strip capture
_default_font_cache: dict = {}


def _stub_font(path: str, size: int):
    # The assertions are about which strings and units the strip draws, not
    # about font metrics, so the vendored theme fonts are not needed here:
    # Pillow's own default stands in (metrics are layout_check's job).
    if size not in _default_font_cache:
        try:
            _default_font_cache[size] = ImageFont.load_default(size=size)
        except TypeError:            # older Pillow: no size argument
            _default_font_cache[size] = ImageFont.load_default()
    return _default_font_cache[size]


def strip_texts(layout: Layout, s: Snapshot) -> dict:
    """Render one idle frame with every text call captured; return the power
    strip's parts. The strip is the only drawing below y=430 (with shift 0),
    and within it the big number is the sole size-34 right-aligned run, the
    unit the sole size-18 one, and the provenance word the sole baseline-
    centred size-11 run (the gradient-end captions are left/right-aligned)."""
    calls: list[tuple] = []
    orig_font, orig_txt = Layout._font, Layout._txt
    Layout._font = lambda self, path, size: _stub_font(path, size)

    def spy(self, d, xy, text, font, fill, anchor="la"):
        calls.append((xy, str(text), font[1], anchor))

    Layout._txt = spy
    try:
        layout.render(s, "idle", (0, 0))
    finally:
        Layout._font, Layout._txt = orig_font, orig_txt

    strip = [(xy, t, size, a) for (xy, t, size, a) in calls if xy[1] >= 430]
    out = {"number": None, "unit": None, "status": None, "dash": False}
    for _xy, t, size, a in strip:
        if size == 34 and a == "rs":
            out["number"] = t
        elif size == 18 and a == "ls":
            out["unit"] = t
        elif size == 14 and a == "ls" and t == "-- W":
            out["dash"] = True
        elif size == 11 and a == "ms":
            out["status"] = t
    return out


def strip_for(s: Snapshot, c: dict) -> dict:
    return strip_texts(Layout(c, rate_hz=1.0), s)


# ------------------------------------------------------------------- the cases
def case_all_missing() -> None:
    print("case: every CPU/GPU reading is missing - there is no total, and it says so")
    c = cfg()
    s = snap(NOW)
    est = estimate(s, c, now=NOW)
    check("total withheld", est.total_w, None)
    check("status", est.status, "unavailable")
    check("sources", est.sources, {"cpu": "unknown", "gpu": "unknown"})
    check("display total", power_watts_for_display(s, c), None)
    st = strip_for(s, c)
    check("strip shows the unit-bearing dash", st["dash"], True)
    check("strip shows no watt figure", st["number"], None)
    check("strip says unavailable", st["status"], "unavailable")


def case_all_missing_policy_off() -> None:
    print("case: policy relaxed does not rescue base-only - a machine we cannot read is not 34 W")
    c = cfg(require_complete=False)
    s = snap(NOW)
    est = estimate(s, c, now=NOW)
    check("total still withheld", est.total_w, None)
    check("status", est.status, "unavailable")


def case_cpu_only() -> None:
    print("case: only the CPU is known - the default policy will not name a total")
    c = cfg()
    s = snap(NOW, cpu_w=40.0)
    est = estimate(s, c, now=NOW)
    check("total withheld (would have been 77.6 excluding the GPU)", est.total_w, None)
    check("status", est.status, "partial")
    check("sources", est.sources, {"cpu": "measured", "gpu": "unknown"})
    st = strip_for(s, c)
    check("strip shows no apparently-measured figure", st["number"], None)
    check("strip says partial", st["status"], "partial")


def case_gpu_only() -> None:
    print("case: only the GPU is known - same story, other side")
    c = cfg()
    s = snap(NOW, gpu_w=50.0)
    est = estimate(s, c, now=NOW)
    check("total withheld", est.total_w, None)
    check("status", est.status, "partial")
    check("sources", est.sources, {"cpu": "unknown", "gpu": "measured"})
    check("strip says partial", strip_for(s, c)["status"], "partial")


def case_cpu_only_policy_off() -> None:
    print("case: require_complete false - the one-sided figure is drawn, as a labelled floor")
    c = cfg(require_complete=False)
    s = snap(NOW, cpu_w=40.0)
    est = estimate(s, c, now=NOW)
    check_close("total is the floor 34 + 40*1.09", est.total_w, 34 + 40 * 1.09)
    check("status still says partial", est.status, "partial")
    st = strip_for(s, c)
    check("number drawn", st["number"], "78")
    check("unit drawn", st["unit"], "W")
    check("status drawn", st["status"], "partial")


def case_both_measured() -> None:
    print("case: complete measured inputs - the total is a model over measured parts")
    c = cfg()
    s = snap(NOW, cpu_w=40.0, gpu_w=50.0)
    est = estimate(s, c, now=NOW)
    check_close("total 34 + (40+50)*1.09", est.total_w, 34 + 90 * 1.09)
    check("sources", est.sources, {"cpu": "measured", "gpu": "measured"})
    check("status", est.status, "measured input")
    st = strip_for(s, c)
    check("number drawn", st["number"], "132")
    check("unit drawn", st["unit"], "W")
    check("status drawn", st["status"], "measured input")


def case_load_only() -> None:
    print("case: no power sensors at all - the load model answers, and says modelled input")
    c = cfg()
    s = snap(NOW, cpu_load=50.0, gpu_load=75.0)
    cpu_w = modelled(50.0, 170.0, 18.0)      # the documented curve, re-derived
    gpu_w = modelled(75.0, 300.0, 28.0)
    est = estimate(s, c, now=NOW)
    check_close("cpu part", est.parts["cpu"], cpu_w)
    check_close("gpu part", est.parts["gpu"], gpu_w)
    check_close("total", est.total_w, 34 + (cpu_w + gpu_w) * 1.09)
    check("sources", est.sources, {"cpu": "modelled", "gpu": "modelled"})
    check("status", est.status, "modelled input")
    st = strip_for(s, c)
    check("number drawn", st["number"], f"{34 + (cpu_w + gpu_w) * 1.09:.0f}")
    check("status drawn", st["status"], "modelled input")


def case_mixed_inputs() -> None:
    print("case: one measured one modelled - the weaker provenance wins the label")
    c = cfg()
    s = snap(NOW, cpu_w=40.0, gpu_load=75.0)
    gpu_w = modelled(75.0, 300.0, 28.0)
    est = estimate(s, c, now=NOW)
    check_close("total", est.total_w, 34 + (40 + gpu_w) * 1.09)
    check("sources", est.sources, {"cpu": "measured", "gpu": "modelled"})
    check("status", est.status, "modelled input")
    check("strip agrees", strip_for(s, c)["status"], "modelled input")


def case_stale() -> None:
    print("case: the sensors died a tick ago and the loop is reusing the last snapshot")
    c = cfg()
    s = snap(NOW - 30.0, cpu_w=40.0, gpu_w=50.0)   # last good read, 30 s ago
    est = estimate(s, c, now=NOW)                  # asked 30 s later
    check("age carried", round(est.age_s, 1), 30.0)
    check("marked stale", est.stale, True)
    check("number still there", est.total_w is not None, True)
    check("status", est.status, "stale")
    # Rendered at the real clock (which is ~NOW), the strip must say stale too.
    st = strip_for(s, c)
    check("number drawn is not presented as live", st["status"], "stale")


def case_untimestamped() -> None:
    print("case: a snapshot with no timestamp has unknown age - never invented fresh, never aged out")
    c = cfg()
    s = snap(0.0, cpu_w=40.0, gpu_w=50.0)
    est = estimate(s, c, now=NOW)
    check("age unknown", est.age_s, None)
    check("not stale", est.stale, False)
    check("status", est.status, "measured input")


def case_recovery() -> None:
    print("case: sensors come back - the total returns, with its provenance")
    c = cfg()
    gone = estimate(snap(NOW), c, now=NOW)
    held = estimate(snap(NOW, cpu_w=40.0, gpu_w=50.0), c, now=NOW + 30.0)
    back = estimate(snap(NOW + 31.0, cpu_w=40.0, gpu_w=50.0), c, now=NOW + 31.0)
    check("dark stretch was unavailable", gone.status, "unavailable")
    check("reuse aged to stale", held.status, "stale")
    check("recovered measured input", back.status, "measured input")
    check_close("recovered total", back.total_w, 34 + 90 * 1.09)
    check("recovered strip figure", strip_for(snap(NOW + 31.0, cpu_w=40.0, gpu_w=50.0),
                                              c)["number"], "132")


def main() -> int:
    for fn in (case_all_missing, case_all_missing_policy_off, case_cpu_only,
               case_gpu_only, case_cpu_only_policy_off, case_both_measured,
               case_load_only, case_mixed_inputs, case_stale, case_untimestamped,
               case_recovery):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 - a case that raises is a failed case
            print(f"  FAIL {fn.__name__} raised {type(e).__name__}: {e}")
            fails.append(fn.__name__)
        print()
    print("SELFTEST PASSED" if not fails else f"SELFTEST FAILED: {fails}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())