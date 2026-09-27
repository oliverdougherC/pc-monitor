#!/usr/bin/env python
"""Live two-up preview of the idle and in-game layouts.

Renders BOTH states side by side from live-or-simulated telemetry and serves an
auto-refreshing browser page:

    .venv\\Scripts\\python tools\\liveview.py                    # simulated data
    .venv\\Scripts\\python tools\\liveview.py --backend auto     # this PC's sensors
    .venv\\Scripts\\python tools\\liveview.py --port 5681 --hz 2

Then open http://localhost:5680. `app/layout.py`, `app/power.py` and
`config.yaml` are hot-reloaded when they change on disk, so editing the layout
shows up in the browser within a tick — no restart, no refresh needed. The
reload is transactional: a broken edit is refused and the previous working
generation keeps rendering, with the error on screen until a complete valid
one is running — and the config watched is the one actually requested
(`--config`), not always the repo default.

The game pane's frame numbers come from the real present stream. When it is off,
denied, quiet or has no usable target, the pane shows `--` — the same honest hole
the panel shows. `--synth-fps on` is there for designing the layout without a game
running, and it pays for that: the pane is painted SIMULATED, in the frame itself,
so an exported PNG cannot outlive its context as a plausible measurement. The
status line reports the provenance of the numbers on screen, not of the capture
underneath them, so the two views of a tick always describe the same thing.

Per-state "push" stats (changed-pixel fraction + how many bands a partial
update would need) are reported the same way app/output.py would do it, so the
design can be judged against what the panel actually has to redraw. A third
number, "swap", is the cost of the idle→game transition itself: one snapshot and
one history rendered in both shapes.

Bytes are reported twice, because they mean different things per revision: PNG at
full size is what TUR_USB puts on the wire (the driver encodes every push), while
width*height*2 is what a 115200-baud serial revision receives.
"""
from __future__ import annotations

import argparse
import copy
import io
import json
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "vendor" / "turing-smart-screen-python"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)  # ROOT last => ROOT wins name collisions (vendor has its own main.py)

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw, ImageEnhance  # noqa: E402

from app import config as cfgmod  # noqa: E402
from app import history as history_mod  # noqa: E402
from app import layout as layout_mod  # noqa: E402
from app import power as power_mod  # noqa: E402
from app.frames import FrameMonitor  # noqa: E402  (role="liveview": its own ETW session, so it coexists with main.py)
from app.output import _runs  # noqa: E402  (same band-merge logic as the real pusher)
from app.sensors import demo as demo_mod  # noqa: E402
from app.sensors import make_hub  # noqa: E402
from app.steamid import SteamIdentity  # noqa: E402

STATES = ("idle", "game")
# hot-reload order matters: each module keeps references to the ones before it
HOT_MODULES = [power_mod, history_mod, demo_mod, layout_mod]
# the watched file list is per-Engine, not a module constant: the config to
# watch is the one --config actually requested

PAGE = """<!doctype html>
<html><head><meta charset="utf-8"><title>PC Monitor — live layouts</title>
<style>
  :root { --scale: 1; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 18px 20px 40px; background: #05060a; color: #cfd6e4;
         font: 13px/1.45 ui-monospace, "Cascadia Mono", Consolas, monospace; }
  h1 { font-size: 14px; font-weight: 600; letter-spacing: .12em; margin: 0 0 4px;
       text-transform: uppercase; color: #8b96ab; }
  #status { font-size: 12px; color: #6f7a8f; margin-bottom: 14px; }
  #status b { color: #b9c4d6; font-weight: 600; }
  .bar { display: flex; flex-wrap: wrap; gap: 10px 18px; align-items: center;
         margin-bottom: 16px; padding: 10px 12px; background: #0b0e14;
         border: 1px solid #1c2230; border-radius: 8px; }
  .grp { display: flex; gap: 6px; align-items: center; }
  .grp > span { color: #6f7a8f; font-size: 11px; text-transform: uppercase;
                letter-spacing: .08em; }
  button { background: #161b26; color: #cfd6e4; border: 1px solid #2a3143;
           border-radius: 5px; padding: 4px 9px; font: inherit; font-size: 12px;
           cursor: pointer; }
  button.on { background: #22406b; border-color: #3f6ea8; color: #eaf2ff; }
  input[type=range] { width: 130px; accent-color: #5b8fd0; }
  #err { display: none; white-space: pre-wrap; margin-bottom: 14px; padding: 10px;
         background: #2a1114; border: 1px solid #7d2b32; border-radius: 8px;
         color: #ffb3bb; font-size: 12px; }
  .stage { display: flex; flex-wrap: wrap; gap: 26px; align-items: flex-start; }
  figure { margin: 0; }
  .bezel { background: #0c0e12; border: 10px solid #15181d; border-radius: 6px;
           box-shadow: 0 8px 26px rgba(0,0,0,.6); line-height: 0; overflow: hidden;
           width: calc(800px * var(--scale)); }
  .bezel img { width: 100%; display: block; image-rendering: auto; }
  figcaption { display: flex; justify-content: space-between; gap: 14px;
               padding: 8px 2px 0; color: #8b96ab; font-size: 12px; }
  figcaption b { letter-spacing: .14em; text-transform: uppercase; color: #d7dfec; }
  figcaption i { font-style: normal; color: #6f7a8f; }
</style></head><body>
<h1>PC Monitor — live layouts (800×480 panel)</h1>
<div id="status">connecting… · <span id="link" style="color:#6f7a8f"></span></div>
<div id="err"></div>
<div class="bar">
  <div class="grp"><span>zoom</span>
    <button data-scale="1" class="on">1×</button><button data-scale="1.5">1.5×</button>
    <button data-scale="2">2×</button></div>
  <div class="grp"><span>brightness</span>
    <input id="dim" type="range" min="0.1" max="1" step="0.05" value="1">
    <i id="dimv" style="color:#6f7a8f">100%</i></div>
  <div class="grp"><span>overlay</span>
    <button id="grid">50px grid</button><button id="shift">burn-in shift</button>
    <button id="trends" class="on" title="layout.trend_bands — off = serial budget mode">trend bands</button></div>
  <div class="grp"><button id="pause">pause</button>
    <button id="dl">save PNGs</button></div>
</div>
<div class="stage">
  <figure>
    <div class="bezel"><img id="img-idle" alt="idle"></div>
    <figcaption><b>Idle</b><i id="d-idle"></i></figcaption>
  </figure>
  <figure>
    <div class="bezel"><img id="img-game" alt="game"></div>
    <figcaption><b>In-game</b><i id="d-game"></i></figcaption>
  </figure>
</div>
<script>
const S = {scale:1, dim:1, grid:0, shift:0, paused:false, n:0, last:{}};
function q(s){ return `/frame.png?state=${s}&dim=${S.dim}&grid=${S.grid}` +
                     `&shift=${S.shift}&n=${S.n}`; }
function tick(){
  if (S.paused) return;
  S.n++;
  for (const s of ['idle','game']) document.getElementById('img-'+s).src = q(s);
  fetch('/api/status?n='+S.n).then(r=>r.json()).then(j=>{
    const ago = j.reloaded ? Math.round((j.now - j.reloaded)) : null;
    const f = j.frames || {};
    let fseg;
    if (f.simulated) {
      // The same fact the image carries. The pane is showing invented numbers, so
      // this line describes those instead of the capture sitting underneath them —
      // before #31 they could disagree, which made the tool unreadable.
      fseg = ` · frames <span style="color:#ff8a4c"><b>SIMULATED</b> ` +
             `${Math.round(f.fps || 0)} fps — invented, not measured</span>`;
    }
    else if (f.source === 'demo') fseg = '';
    else if (f.source === 'off') fseg = ' · frames <b>off</b>';
    else if (f.name) fseg = ` · frames <b>${f.name}</b>` + (f.appid ? ` [${f.appid}]` : '') +
                            ` <b>${Math.round(f.fps || 0)} fps</b>`;
    else if (f.ok) fseg = ` · frames <i>live, nothing presenting</i>`;
    else fseg = ` · frames <span style="color:#c98b93">${f.error || 'starting'}</span>`;
    document.getElementById('status').innerHTML =
      `backend <b>${j.backend}</b> · tick <b>${j.tick}</b> · ` +
      `hot-reloads <b>${j.reloads}</b>` + (ago===null?'':` (${ago}s ago)`) +
      ` · render <b>${j.render_ms} ms</b> · trend <b>${j.trend_s}s</b>` +
      ` · watching <b>${j.watched}</b> files` + fseg;
    for (const s of ['idle','game']) {
      const d = j.push[s] || {};
      document.getElementById('d-'+s).textContent =
        `${d.mode||'?'} · ${((d.changed||0)*100).toFixed(1)}% px · ${d.bands||0} band(s) · ` +
        `USB ${(d.bytes_partial_png/1024||0).toFixed(1)} KB / serial ${(d.bytes_partial/1024||0).toFixed(0)} KB`;
    }
    if (j.push.idle) {
      const p = j.push.idle.bytes_partial_png + j.push.game.bytes_partial_png;
      const r = j.push.idle.bytes_partial + j.push.game.bytes_partial;
      const sw = j.swap || {};
      const swap = sw.bytes_partial ?
        ` · idle→game swap (${sw.mode}, ${sw.bands} band(s)): ` +
        `${(sw.bytes_partial_png/1024).toFixed(1)} KB PNG / ` +
        `${(sw.bytes_partial/1024).toFixed(0)} KB raw = ${(sw.bytes_partial/14400).toFixed(1)} s serial` : '';
      document.getElementById('link').textContent =
        `wire: ~${(p/1024).toFixed(0)} KB/s → ${(p/1e6*1000).toFixed(0)} ms/frame on TUR_USB · ` +
        `raw ${(r/1024).toFixed(0)} KB/s → ${(r/14400).toFixed(1)} s/frame on 115200 serial` + swap;
      const b = document.getElementById('trends');
      b.classList.toggle('on', !!j.trends);
    }
    document.getElementById('err').style.display = j.error ? 'block' : 'none';
    document.getElementById('err').textContent = j.error || '';
  }).catch(()=>{ document.getElementById('status').textContent = 'server not responding'; });
}
setInterval(tick, 600); tick();
document.querySelectorAll('[data-scale]').forEach(b => b.onclick = () => {
  document.querySelectorAll('[data-scale]').forEach(x => x.classList.remove('on'));
  b.classList.add('on'); S.scale = parseFloat(b.dataset.scale);
  document.documentElement.style.setProperty('--scale', S.scale);
});
dim.oninput = () => { S.dim = parseFloat(dim.value); dimv.textContent = Math.round(S.dim*100)+'%'; };
grid.onclick = () => { S.grid ^= 1; grid.classList.toggle('on', !!S.grid); };
trends.onclick = () => {
  const want = !document.getElementById('trends').classList.contains('on');
  fetch('/ctrl?trends=' + (want ? 1 : 0));   // next status confirms the state
};
shift.onclick = () => { S.shift = S.shift ? 0 : 1; shift.classList.toggle('on', !!S.shift); };
pause.onclick = () => { S.paused = !S.paused; pause.classList.toggle('on', S.paused);
                        pause.textContent = S.paused ? 'resume' : 'pause'; };
dl.onclick = () => { for (const s of ['idle','game'])
  window.open(`/frame.png?state=${s}&dim=1&raw=1`, '_blank'); };
</script></body></html>
"""


def _merge_bands(runs, gap: int = 6):
    """Mirror DiffPusher's band merging so the reported cost is the real one."""
    if not runs:
        return []
    merged = [list(runs[0])]
    for s, e in runs[1:]:
        if s - merged[-1][1] <= gap:
            merged[-1][1] = e
        else:
            merged.append([s, e])
    return [(s, e) for s, e in merged]


def _bytes_png(img) -> int:
    """What the TUR_USB driver would actually put on the wire for this image: it
    PNG-encodes each push (`_encode_png`, compress_level=9, `send_pil_image_auto`),
    so compressed bytes — not width*height*2 — decide whether a design is
    affordable. JPEG fallback only kicks in above the 1 MB payload cap."""
    buf = io.BytesIO()
    img.save(buf, "PNG", compress_level=9)
    return len(buf.getvalue())


def diff_report(prev, new, merge_gap: int = 6, max_bands: int = 6,
                full_fraction: float = 0.35):
    """What app/output.py would do with this frame, plus the real cost of each
    option: raw RGB565 bytes (what the 115200-baud serial revisions receive) and
    PNG bytes scaled to full size (what TUR_USB actually puts on the wire)."""
    if prev is None or prev.shape != new.shape:
        return {"mode": "full", "changed": 1.0, "bands": 1,
                "bytes_partial": int(new.size * 2), "bytes_full": int(new.size * 2)}
    changed = np.any(new != prev, axis=2)
    frac = float(changed.mean())
    h, w = new.shape[0], new.shape[1]
    full_raw = int(w * h * 2)
    full_png = _bytes_png(Image.fromarray(new))
    if frac == 0.0:
        return {"mode": "none", "changed": 0.0, "bands": 0,
                "bytes_partial": 0, "bytes_full": full_raw,
                "bytes_partial_png": 0, "bytes_full_png": full_png}
    rows = np.flatnonzero(changed.any(axis=1))
    merged = _merge_bands(_runs(rows), gap=merge_gap)
    usable = frac <= full_fraction and 0 < len(merged) <= max_bands
    area = png_area = 0
    if usable:
        for y0, y1 in merged:
            cols = np.flatnonzero(changed[y0:y1].any(axis=0))
            x0, x1 = int(cols[0]), int(cols[-1]) + 1
            area += (y1 - y0) * (x1 - x0)
            png_area += _bytes_png(Image.fromarray(new[y0:y1, x0:x1]))
    return {"mode": "bands" if usable else "full", "changed": frac,
            "bands": (len(merged) if usable else 1),
            "bytes_partial": (area * 2 if usable else full_raw),
            "bytes_full": full_raw,
            "bytes_partial_png": (png_area if usable else full_png),
            "bytes_full_png": full_png}


def synth_frames(snap, t: float) -> None:
    """Invented frame stats — opt-in only (`--synth-fps on`).

    They exist so the game layout can be designed with no game running. What they
    invented used to be indistinguishable from a measurement: the values are now
    marked `simulated` on the snapshot, which is what makes the pane draw its
    SIMULATED badge. Before #31 this ran by default for every real backend, so a
    preview whose capture was down showed a plausible ~118 fps and read as a
    working present stream — the one thing this tool is supposed to let you check.
    """
    f = snap.frames
    if f.fps is None:
        fps = 118 + 14 * np.sin(t * 0.31) + 5 * np.sin(t * 1.7)
        f.fps = fps
        f.low1_pct = fps * 0.74
        f.low01_pct = fps * 0.46
        f.latency_ms = 1000.0 / fps * 0.86
        f.simulated = True


class Engine:
    """Ticks telemetry, hot-reloads the layout code, keeps both frames."""

    def __init__(self, cfg, backend: str, hz: float, synth: bool,
                 frames_source: str = "auto", cfg_path: str | None = None):
        self.cfg = cfg
        self.hz = hz
        self.backend = backend
        self.synth = synth
        self.demo = backend == "demo"
        # real present-stream frame stats for the game pane (needs admin);
        # the game pane follows the busiest presenting process, preferring a
        # Steam-identified one — that IS the point of running it with --backend auto
        self.frames_mon = None
        self.steam = SteamIdentity()
        if not self.demo and frames_source != "off":
            self.frames_mon = FrameMonitor(cfg, role="liveview")
        # `displayed`/`simulated` are recomputed from the snapshot on every tick
        # (`_publish_frames`); these are the values for the tick before the first one.
        self.frames_info: dict = {"source": "demo" if self.demo else frames_source,
                                  "displayed": "none", "simulated": False}
        self.lock = threading.Lock()
        self.base: dict[tuple[str, int], Image.Image] = {}
        self.prev: dict[str, np.ndarray | None] = {s: None for s in STATES}
        self.diff: dict[str, dict] = {s: {"mode": "init", "changed": 1.0, "bands": 1}
                                     for s in STATES}
        # cost of the idle->game swap itself: the two frames differ only in the
        # bottom strip now, and that is the number that decides whether a serial
        # board can survive a mode change at all
        self.swap: dict | None = None
        self.tick_n = 0
        self.reloads = 0
        self.reloaded_at: float | None = None
        self.render_ms = 0.0
        self.error: str | None = None
        self.started = time.time()
        # watch the config that was actually requested: cfg["_root"] is always
        # the repo, so a custom --config file would otherwise load once and
        # then never be seen again
        self._cfg_path = Path(cfg_path).resolve() if cfg_path else ROOT / "config.yaml"
        self.watch = [Path(m.__file__) for m in HOT_MODULES] + [self._cfg_path]
        self._stamps = {p: self._stamp(p) for p in self.watch}
        self.layouts = {s: layout_mod.Layout(cfg, rate_hz=hz) for s in STATES}
        self.samples = self.layouts["idle"].samples

        if self.demo:
            self._make_hubs()
        else:
            self.hub = make_hub(cfg, force=backend)
            self.hub_idle = self.hub_game = self.hub
        if self.demo:
            self._backfill()

    def _backfill(self) -> None:
        """Fill the trend rings before the first paint by running the same demo
        generator backwards in time, so a restart never shows empty graphs; live
        samples then continue seamlessly from the last backfilled one. Real
        sensors cannot be backfilled, so there the bands fill honestly."""
        for _ in range(self.samples):
            for hub in (self.hub_idle, self.hub_game):
                hub.backend._t0 -= 1.0
            si, sg = self._snapshots()
            for state, snap in (("idle", si), ("game", sg)):
                self.layouts[state].observe(snap, state)

    def _new_demo_hubs(self):
        """Two independent demo streams, built but not yet published: idle-shaped
        data on the left screen, game-shaped data on the right, both animating
        from the same clock. The class check is module-qualified on purpose:
        after a hot reload the once-imported `DemoBackend` names the class of
        whichever generation imported it, while `demo_mod.DemoBackend` is the
        one `make_hub` just built against — comparing against the stale name
        rejected every legitimate reload and left the game hub un-flagged."""
        idle = make_hub(self.cfg, force="demo")
        game = make_hub(self.cfg, force="demo")
        assert isinstance(idle.backend, demo_mod.DemoBackend)
        idle.backend.game = False
        game.backend.game = True
        return idle, game

    def _make_hubs(self) -> None:
        self.hub_idle, self.hub_game = self._new_demo_hubs()

    @staticmethod
    def _stamp(p: Path):
        try:
            st = p.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    # ---- hot reload ----------------------------------------------------------
    def _maybe_reload(self) -> None:
        changed = [p for p in self.watch if self._stamp(p) != self._stamps.get(p)]
        if not changed:
            return
        cfg_changed = self._cfg_path in changed
        demo_changed = Path(demo_mod.__file__) in changed

        # stage the candidate config before anything is touched: invalid YAML
        # must not churn the module chain, let alone half-apply itself
        fresh = None
        if cfg_changed:
            try:
                fresh = cfgmod.load(str(self._cfg_path))
            except Exception as e:  # noqa: BLE001
                raise RuntimeError(
                    f"config reload failed ({e.__class__.__name__}: {e}); "
                    f"keeping the previous config") from None

        # reload the whole chain in dependency order whenever anything changed:
        # each module keeps direct references to the ones before it, so a partial
        # reload would leave the new module wired to the old classes. And
        # importlib re-executes modules *in place*: the dict snapshots below are
        # what make the reload transactional — if an edit dies mid-module, the
        # preview keeps running the last complete generation instead of a
        # half-new, half-old one.
        saved = [(m, dict(m.__dict__)) for m in HOT_MODULES]
        saved_cfg = copy.deepcopy(self.cfg)
        try:
            if fresh is not None:
                # publish into the live dict, never replace it: the layouts and
                # the /ctrl handler all hold this same reference
                self.cfg.clear()
                self.cfg.update(fresh)
            for mod in HOT_MODULES:
                importlib_reload(mod)
            if demo_changed and self.demo:
                # existing DemoBackend objects are bound to the old class, so
                # the streams have to be rebuilt for edited data shapes to show
                # up — built first, published only once the whole generation is
                # valid, flags and all
                hubs = self._new_demo_hubs()
            # rebuild the layouts, carrying the trend history across the reload
            # so an edit never blanks the graphs out from under us
            old = {s: l.history for s, l in self.layouts.items()}
            layouts = {s: layout_mod.Layout(self.cfg, rate_hz=self.hz)
                       for s in STATES}
            for s, hist in old.items():
                # a History instance is just rings + a length, so it survives the
                # reload even though the Layout class around it was replaced
                if hist.samples == layouts[s].history.samples:
                    layouts[s].history = hist
        except Exception:
            for mod, snap in saved:
                mod.__dict__.clear()
                mod.__dict__.update(snap)
            self.cfg.clear()
            self.cfg.update(saved_cfg)
            raise
        # commit: publish the candidate generation, and only then advance the
        # stamps of the files that loaded — a failed reload stays pending, so
        # its error is re-surfaced every tick until a complete valid generation
        # is running (and the repair is picked up without a restart)
        if demo_changed and self.demo:
            self.hub_idle, self.hub_game = hubs
        self.layouts = layouts
        self.samples = layouts["idle"].samples
        for p in changed:
            self._stamps[p] = self._stamp(p)
        self.reloads += 1
        self.reloaded_at = time.time()
        names = ", ".join(p.name for p in changed)
        print(f"[liveview] hot-reloaded ({names})")

    # ---- one tick ------------------------------------------------------------
    def _snapshots(self):
        t = time.monotonic() - self.started
        if self.demo:
            # two independent demo streams: the idle panel shows idle-shaped
            # data, the game panel shows game-shaped data, both animating
            si = self.hub_idle.tick()
            sg = self.hub_game.tick()
        else:
            sg = self.hub_game.tick()
            si = sg
            self._fill_frames(sg, t)
        # whatever ended up in the game pane — measured, invented, or nothing — is
        # reported from here, after both paths, so the page cannot describe a
        # different tick than the one it is about to show
        self._publish_frames(sg)
        for s in {id(si): si, id(sg): sg}.values():
            s.power_total_w = power_mod.estimate(s, self.cfg).total_w
        return si, sg

    def _fill_frames(self, sg, t: float) -> None:
        """Game pane frame stats: the live ETW present stream when it is up —
        following the busiest presenting process, preferring a Steam-identified one
        — else nothing at all, which the pane renders as `--`; the invented
        placeholder only with `--synth-fps on`, and then it is marked as invented.

        What is left on `sg.frames` is the whole truth of the game pane. The status
        line is read back off that snapshot afterwards (`_publish_frames`) instead of
        being decided here, so the two views of one tick cannot drift apart.
        """
        info: dict = {"source": "off"}
        mon = self.frames_mon
        if mon is not None:
            info = {"source": "auto", "ok": mon.ok, "error": mon.error}
            if mon.ok:
                pres = mon.presenters()
                info["n_presenters"] = len(pres)
                if pres:
                    pid, pr = max(pres.items(),
                                  key=lambda kv: (self.steam.is_steam_game(kv[0]),
                                                  kv[1].fps))
                    fs = mon.stats(pid)
                    if fs is not None:
                        sg.frames = fs
                        info.update(pid=pid, name=pr.name, appid=self.steam.appid(pid),
                                    fps=round(float(fs.fps or 0.0), 1))
                        self.frames_info = info
                        return
        self.frames_info = info
        if self.synth:
            synth_frames(sg, t)

    def _publish_frames(self, sg) -> None:
        """Say what the game pane is showing, derived from the snapshot being drawn.

        This used to be implicit: `frames_info` described the *capture* ("auto" /
        "off") while the pane below it could be showing invented numbers, so the
        status line and the image were two separate claims and only one of them was
        ever looked at (issue #31). Deriving the answer from the snapshot means a
        new source of frame numbers cannot be added here without being accounted for
        there, and the honest `--` of a dead capture now has a name in the status too.
        """
        sim = bool(getattr(sg.frames, "simulated", False))
        shown = "simulated" if sim else ("real" if sg.frames.fps is not None else "none")
        info = {**self.frames_info, "simulated": sim, "displayed": shown}
        if sim and sg.frames.fps is not None:
            info["fps"] = round(float(sg.frames.fps), 1)
        # Rebind rather than mutate: `status()` hands this dict straight to the
        # browser, and a reader should see the old provenance or the new one.
        self.frames_info = info

    def step(self) -> None:
        self._maybe_reload()
        t0 = time.perf_counter()
        si, sg = self._snapshots()
        shots = {"idle": si, "game": sg}
        frames: dict[tuple[str, int], Image.Image] = {}
        for state, snap in shots.items():
            layout = self.layouts[state]
            layout.observe(snap, state)
            for sh, shift in ((0, (0, 0)), (1, (3, 3))):
                frames[(state, sh)] = layout.render(snap, state, shift)
        dt = (time.perf_counter() - t0) * 1000.0
        diffs = {s: diff_report(self.prev[s], np.asarray(frames[(s, 0)], np.uint8))
                 for s in STATES}
        # What a state change actually costs: the SAME telemetry and the SAME
        # history rendered in both shapes (the demo feeds its two panes from
        # separate streams, so diffing those would measure the data, not the
        # layout). One extra render, ~8 ms.
        shared = self.layouts["game"].render(sg, "idle", (0, 0))
        swap = diff_report(np.asarray(shared, np.uint8),
                           np.asarray(frames[("game", 0)], np.uint8))
        with self.lock:
            self.base = frames
            self.diff = diffs
            self.swap = swap
            self.render_ms = round(dt, 1)
            self.tick_n += 1
            self.error = None
            for s in STATES:
                self.prev[s] = np.asarray(frames[(s, 0)], np.uint8)

    def frame(self, state: str, shift: int, dim: float, grid: bool) -> bytes:
        with self.lock:
            img = self.base.get((state, 1 if shift else 0))
        if img is None:
            img = Image.new("RGB", (800, 480), (6, 8, 12))
            ImageDraw.Draw(img).text((20, 20), "no frame yet", fill=(120, 130, 150))
        if dim < 0.999:
            img = ImageEnhance.Brightness(img).enhance(max(0.02, dim))
        if grid:
            rgba = img.convert("RGBA")
            ov = Image.new("RGBA", rgba.size, (0, 0, 0, 0))
            d = ImageDraw.Draw(ov)
            for x in range(0, rgba.size[0], 50):
                d.line([(x, 0), (x, rgba.size[1])], fill=(150, 190, 255, 34))
            for y in range(0, rgba.size[1], 50):
                d.line([(0, y), (rgba.size[0], y)], fill=(150, 190, 255, 34))
            img = Image.alpha_composite(rgba, ov).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, "PNG", optimize=False, compress_level=2)
        return buf.getvalue()

    def status(self) -> dict:
        with self.lock:
            return {
                "backend": self.backend + (" (sim)" if self.demo else ""),
                "tick": self.tick_n, "reloads": self.reloads,
                "reloaded": self.reloaded_at, "now": time.time(),
                "render_ms": self.render_ms, "push": self.diff, "swap": self.swap,
                "trend_s": round(self.samples / max(self.hz, 0.001)),
                "trends": bool(self.cfg["layout"].get("trend_bands", True)),
                "frames": self.frames_info,
                "error": self.error, "uptime_s": round(time.time() - self.started, 1),
                "watched": len(self.watch),
            }


def importlib_reload(mod):
    import importlib
    try:
        return importlib.reload(mod)
    except Exception:  # broken edit: _maybe_reload rolls the chain back
        tb = traceback.format_exc()
        print("[liveview] reload failed:\n" + tb)
        raise RuntimeError(tb) from None


def run(cfg, backend, hz, port, synth, frames_source="auto", cfg_path=None) -> None:
    engine = Engine(cfg, backend, hz, synth, frames_source, cfg_path)

    # first tick on the main thread so the page always has something to show
    try:
        engine.step()
    except Exception:
        engine.error = traceback.format_exc()
        print(engine.error)

    def loop():
        while True:
            t0 = time.monotonic()
            try:
                engine.step()
            except Exception as e:  # a bad edit must not kill the server
                engine.error = traceback.format_exc()
                print(f"[liveview] render failed: {e.__class__.__name__}: {e}")
            left = 1.0 / hz - (time.monotonic() - t0)
            if left > 0:
                time.sleep(left)

    threading.Thread(target=loop, daemon=True).start()

    cache: dict[tuple, tuple[float, bytes]] = {}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # quiet
            pass

        def _send(self, code, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            qs = {k: v[0] for k, v in parse_qs(u.query).items()}
            try:
                if u.path in ("/", "/index.html"):
                    return self._send(200, PAGE.encode(), "text/html; charset=utf-8")
                if u.path == "/api/status":
                    return self._send(200, json.dumps(engine.status()).encode(),
                                      "application/json")
                if u.path == "/ctrl":
                    # live config switches, written straight into the cfg dict the
                    # layouts read on every render (no restart, no state race)
                    if "trends" in qs:
                        engine.cfg["layout"]["trend_bands"] = \
                            qs["trends"] not in ("0", "", "false", "off")
                    return self._send(200, json.dumps(
                        {"trends": bool(engine.cfg["layout"].get("trend_bands", True))}
                    ).encode(), "application/json")
                if u.path == "/frame.png":
                    state = qs.get("state", "idle")
                    state = state if state in STATES else "idle"
                    shift = 1 if qs.get("shift", "0") not in ("0", "", "false") else 0
                    if qs.get("raw") in ("1", "true"):
                        dim, grid = 1.0, False
                    else:
                        dim = float(qs.get("dim", "1"))
                        grid = qs.get("grid", "0") not in ("0", "", "false")
                    key = (state, shift, round(dim, 3), grid)
                    hit = cache.get(key)
                    now = time.monotonic()
                    if hit and now - hit[0] < 0.45:
                        png = hit[1]
                    else:
                        png = engine.frame(state, shift, dim, grid)
                        cache.clear() if len(cache) > 12 else None
                        cache[key] = (now, png)
                    return self._send(200, png, "image/png",
                                      {"Content-Disposition":
                                       f'inline; filename="pcmonitor-{state}.png"'})
                return self._send(404, b"not found", "text/plain")
            except Exception:
                return self._send(500, traceback.format_exc().encode(), "text/plain")

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    srv.daemon_threads = True
    print(f"[liveview] http://localhost:{port}  backend={backend}  hz={hz}  "
          f"synth_fps={'on (game pane marked SIMULATED)' if synth else 'off'}")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=None)
    ap.add_argument("--backend", default="demo", help="demo|auto|lhm|fallback")
    ap.add_argument("--port", type=int, default=5680)
    ap.add_argument("--hz", type=float, default=1.0, help="render ticks/second")
    ap.add_argument("--synth-fps", default="off", choices=["off", "on", "auto"],
                    help="invented frame stats when the real stream has none: off "
                         "(default) leaves them unavailable, on shows them and marks "
                         "the pane SIMULATED. 'auto' is accepted as 'off' — it used "
                         "to mean 'invent them for any real backend'")
    ap.add_argument("--frames-source", default="auto", choices=["auto", "off"],
                    help="auto = PresentMon ETW for real per-process frame stats (needs admin)")
    args = ap.parse_args()

    cfg = cfgmod.load(args.config)
    backend = args.backend.lower()
    # `auto` is kept so an old command line still runs, but it no longer means what
    # it meant: inventing fps whenever the backend was real is how a preview came to
    # show a steady ~118 fps while the capture was dead (issue #31). Say it out loud
    # rather than letting the flag quietly change meaning under someone's fingers.
    if args.synth_fps == "auto":
        print("[liveview] --synth-fps=auto is off now: missing frame stats stay "
              "unavailable. Pass --synth-fps on for a simulation, which the game "
              "pane marks SIMULATED on the image.")
    synth = args.synth_fps == "on"
    run(cfg, backend, args.hz, args.port, synth, args.frames_source, args.config)


if __name__ == "__main__":
    main()
