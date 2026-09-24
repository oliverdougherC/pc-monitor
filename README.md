# PC Monitor — desk telemetry display

Telemetry → 5" USB-C "Turing-family" LCD via
[turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)
(vendored in `vendor/`, used as a library, unmodified). The connected unit
identifies itself as `chs_5inch.dev1_rom1.88`, i.e. the classic 5" **revision C**
(CDC-ACM COM port), not a `TUR_USB` TURZX board — see "The panel on this desk".

## Run now (no hardware needed)

```
.venv\Scripts\python main.py --backend demo          # simulated screen at http://localhost:5678
.venv\Scripts\python tools\liveview.py               # LIVE two-up layout editor preview → http://localhost:5680
.venv\Scripts\python tools\layout_check.py --no-trends  # geometry guard (collisions / panel overflow)
.venv\Scripts\python main.py                         # real sensors (psutil+NVML), simulated screen
.venv\Scripts\python main.py --dump x.png --force-state game   # headless frame preview
```

Real fps/frametime (game pane, `--backend auto|lhm|fallback`) additionally needs
the pinned PresentMon binary and Administrator — everything else degrades to
honest `--`, never fake numbers:

```
powershell -File tools\fetch_presentmon.ps1          # download + sha256-lock vendor/presentmon/
.venv\Scripts\python tools\frames_probe.py 10        # what the present stream sees (elevated)
```

`tools/liveview.py` renders **both** states from the same telemetry and
hot-reloads `app/layout.py` / `app/power.py` / `config.yaml` on save — edit the
layout and the browser updates within a tick (no restart). It also reports, per
state, how many bands a partial panel update would need (same math as
`app/output.py`), and has zoom / brightness / grid / burn-in-shift overlays.

## Architecture

| File | Role |
|---|---|
| `main.py` | entry: 1 Hz loop, wiring |
| `app/display.py` | builds the panel object for `display.revision` (the only module that imports the vendored `library.*`) |
| `app/sensors/` | backends: `lhm` (LibreHardwareMonitorLib via pythonnet, full fidelity incl. package power & per-core clocks), `fallback` (psutil+NVML, no admin), `demo` |
| `app/frames.py` | real fps/frametime/1%–0.1% low per process: spawns PresentMon (ETW, admin), parses the present stream, answers "who is rendering" and "at what frame rate" |
| `app/steamid.py` | Steam identity: `SteamAppId`/`SteamGameId` env vars → which pids are Steam games + AppID (enrichment, never the detection core) |
| `app/power.py` | total-watt estimate (base+cpu+gpu) & green→red 50–1000 W gradient |
| `app/gamewatch.py` | idle/game state, layered: foreground PID actively presenting (sticky across alt-tabs) → Steam-flagged pick → window-style fallback; hysteresis |
| `app/layout.py` | 800×480 rendering: permanent top half + mode strip, trend bands, worst-case-fit type |
| `app/history.py` | bounded ring buffers feeding the trend bands |
| `app/output.py` | numpy diff → only changed bands are sent to the panel |
| `app/burnin.py` | brightness schedule, screen-off on idle, 3-px layout shift, periodic color sweep |
| `tools/liveview.py` | dev server: live idle+game two-up, hot-reload, push-cost stats, real present-stream fps |
| `tools/layout_check.py` | geometry guard: value extremes → collisions / panel overflow |
| `tools/frames_probe.py` | what the PresentMon stream sees right now: presenters, AppIDs, fps/frametime |
| `tools/frames_health.ps1` | no admin: which of LIVE / STARVED / DENIED / QUIET the capture is in, straight from `log.log` |
| `tools/frames_selftest.py` | no admin: replays synthetic present streams through the real parser, and checks the silence/column guards fire |
| `tools/presentmon_matrix.py` | elevated: A/Bs the capture invocations (drop filter, tracking, `--v1_metrics`, name reuse) |
| `tools/frames_diag.py` | elevated: the child's header/rows verbatim, beside what the app would pick |
| `tools/ab_capture.ps1` | drive one capture through the elevated task by editing `config.yaml`; `-Restore` puts it back |
| `tools/isolated_capture_probe.py` | elevated: does the app's own session starve a second one? |
| `tools/usb_probe.py` | what is on USB: libusb view (what `TUR_USB` can reach) + the Windows/registry view; `--watch` for plug events |
| `tools/plug_watch.ps1` | run beside it: every device Windows enumerates or drops, timestamped |
| `tools/screen_link_probe.py` | map each COM port to the USB device behind it, dump the raw descriptors + any unsolicited bytes |
| `tools/screen_bench.py` | first contact with a real panel: HELLO ID, sub-revision, ROM, and *measured* full-frame / band push times |
| `tools/vendor_lock.ps1` | pin + verify the vendored library (see `vendor/README.md`) |
| `tools/fetch_presentmon.ps1` | download + sha256-pin `vendor/presentmon/presentmon.exe` (GameTechDev, MIT) |

## Screen design: permanent furniture

Both states share one skeleton — a legibility decision and, as it turns out, a
bandwidth one:

| region | idle | in-game | moves? |
|---|---|---|---|
| top left | CPU panel, 394×212, 58 px digits | identical | **never** |
| top right | GPU panel | identical | **never** |
| bottom strip, 3 slots | RAM · DISK · NETWORK | FRAMES · RAM · DISK | content only |
| power strip | total W + clock | identical | **never** |

Measured on one snapshot (`tools/liveview.py` prints it live under the frames):
the state change touches **0 pixels above y=228** — 10.2 % of the frame, which as
partial-push bands is 3 bands / 220 KB raw / 23 KB PNG. On a serial revision that
turns a ~68 s mode switch into a ~15 s one; on TUR_USB it is a 23 KB push.

Type sizes are fixed per slot from the **worst-case** string (`100°` beside `100%`,
`999.9 MB/s` under a `WRITE` caption), so digits never collide and never resize
while they update. `tools/layout_check.py` re-proves that — both states, both
trend modes, the value extremes *and* the all-`None` sensor case.

A missing sensor renders as a small dim `--` on the same baseline rather than a
giant bright dash, and a metric with no history draws a dashed empty axis instead
of a blank box. That is not hypothetical: the non-elevated fallback backend
genuinely has no CPU temp and no package power.

### Frame stats & how "game" is decided

fps / frametime / 1%–0.1% low come from the **OS present stream** (GameTechDev
PresentMon over ETW, spawned as a child, CSV on stdout). Zero injection into
games — the same mechanism CapFrameX/G-Helper use — so it works for
DX9/11/12/Vulkan/GL/UWP day-one, and it measures *displayed* frames. It needs
Administrator (ETW kernel session; "Performance Log Users" also suffices),
otherwise the child exits with a captured error and the panel shows `--`.
Definitions live in the `app/frames.py` docstring: last-1s displayed present
rate, frametime = MsBetweenPresents, lows = 1000/P99 and P99.9 over 60 s.

Game mode reads that stream instead of guessing from window chrome: **GAME when
the foreground process is actually presenting** at ≥ `frames.min_present_fps`
(= "watch for a graphics process", made truthful by ETW), with the picked pid
*sticky* so alt-tabs and launcher overlays don't drop the state, and multi-monitor
correct (a window counts against the monitor it is on, not the primary). The
window-style heuristic (covers monitor + no caption + GPU busy) stays as the
fallback for light games under the threshold or when ETW is unavailable.
`SteamAppId`/`SteamGameId` env vars (`app/steamid.py`) then say *which Steam game
it is* — Steam's own launch contract, inherited through launcher chains — used
for preference and (later) per-game profiles, never as the detection core:
protected processes block the env read and direct-launched exes skip Steam, so
both cases are still caught by present + foreground.

### When the present stream is silent

Three failures look identical from the panel — fps stays `--` — and are nothing
alike underneath, so the app distinguishes them in `log.log`:

1. **The child cannot start.** No Administrator/"Performance Log Users" ⇒
   PresentMon exits at once with `failed to start trace session: access denied`
   (exit 6) and the captured error is logged.
2. **The child starts and receives nothing.** Session up, process alive, not one
   row forever — not even the CSV header. `frames.observe()` notices silence *while
   something is visibly rendering* and logs once:
   `[frames] no rows from presentmon for 20+ s of rendering (header=no, args: …);
   child said: …`. The args and the child's first line are quoted because they are
   the whole diagnosis: `warning: a trace session named "…" is already running`
   means an earlier run still owns the session, `printed nothing at all` means the
   machine is not delivering graphics events to anyone.
3. **Rows arrive and nothing is parsed.** The stream is fine; the parser is reading
   a column that is not there, so every row is dropped and fps stays `--` with a
   perfectly healthy-looking log. This is the one that actually bit: `--qpc_time_ms`
   emits **`CPUStartQPCTimeInMs`** (column 15 of 28), the parser asked for
   `CPUStartQPCTime` — a name lifted from the documentation — and the version before
   that read `row[-1]`, which on this header is `MsClickToPhotonLatency`, a mostly
   empty column. Do not reach for `TimeInSeconds` instead: measured on a live
   stream, consecutive rows at 48 fps advanced it by +20.8 *seconds* per frame and
   its origin differs per process, so it cannot bucket a mixed-activity stream. When
   this fires the guard prints **every** column name, because a truncated list is
   what hid the correct one the first time around.

Frame detection falls back to the window heuristic in cases 2 and 3, so game mode
still appears while fps stays `--` — which is why `[state] … frames=on` used to be a
lie (it only asked whether the object existed). It now prints the number, or `no`.

A run that takes the session over from a previous run of itself stops the old
session first, in a process of its own, then waits (`_SESSION_SETTLE_S`) before
starting the real one. Doing both inside one invocation is what
`--stop_existing_session` is for, and on some machines it yields a session that
is registered for the graphics providers and receives nothing at all.

To tell *which* of the three culprits — our args, our parsing, the machine — is
responsible, capture the raw stream and replay it:

| command | what it answers |
| --- | --- |
| `tools/frames_selftest.py` | replays synthetic 1.x/2.x streams through the real parser, no admin: is *our* reading of the CSV correct? |
| `tools/frames_probe.py 10` | elevated: presenters, AppIDs, fps — what the live stream sees |
| `tools/presentmon_matrix.py` | elevated: A/Bs the invocations (drop filter, tracking, `--v1_metrics`, session-name reuse) side by side |
| `tools/frames_diag.py` | elevated: echoes the child's header/rows verbatim next to what the app would pick |
| `tools/ab_capture.ps1 -Out … -Role … -Exe … -ExcludeDropped false -ExtraArgs '[…]'` | drives one such capture through the *elevated scheduled task* by editing `config.yaml`, then restores it — the way to test on a machine where UAC prompts do not reach the screen |
| `tools/isolated_capture_probe.py` | elevated: is the running app itself starving the session? |

`frames.exclude_dropped`, `frames.role`, `frames.extra_args` and
`frames.output_file` are the config-side surface for that: `role` picks the ETW
session name (same = take over, different = capture alongside), `output_file`
writes the raw CSV to a file and turns frame stats off by design. They change
what a frame *is*, so they stay off in a normal config.

**Measured on this desk** (Windows 11 build 26200, RTX 5090 + AMD iGPU): on the
session where this was investigated, *nothing* arrived under **any** combination —
pinned 2.5.1 and 2.6.0, with and without the drop filter, with and without display
tracking, on a fresh session name, on a cleanly reclaimed one, and with a
guaranteed 60 fps presenter on screen. The NVIDIA overlay was reporting wrong
frame/GPU telemetry at the same time. A **reboot fixed it**: the first start on
the fresh boot logged `presentmon session live` in the same second and the capture
started actually burning CPU, with one `presentmon` and no orphans. So the fault
was a wedged machine state below PresentMon — one that also starves NVIDIA's own
overlay — not the args, the binary, the parser or the session name. What this repo
contributes is that the failure is now *visible*: one log line instead of a panel
that quietly reads "`--`" forever while the log says nothing. If it returns, look
at the overlay first; if that is lying too, reboot before digging further.

Once the stream came back, case 3 was what remained: 7907 rows in 20 s, not one
parsed. The captured file (`vendor/presentmon/cap-live.csv`, gitignored) is where
the real header came from and what `tools/frames_selftest.py` is now pinned to.
Frame stats were confirmed live the same day: `[state] game pid=32024 frames=170`
for `re9.exe` — the first real number that line ever printed.

### The panel's boot, and why it does not reboot itself any more

Revision C enumerates two serial faces, and the vendored examples `Reset()` the
panel before talking to it: write a RESTART command, close the port, wait for it to
come back, shake hands. That write goes out with pyserial's `write_timeout` unset,
so on a screen whose CDC endpoint has stopped draining it blocks in `WriteFile`
**forever** — dark panel, a log ending at `Display reset (COM port may change)...`,
and nothing to act on. Twice on this desk, and only pulling the USB cable cleared
it. `InitializeComm()` has the opposite problem: it retries HELLO once a second
without end.

`app/display.py` therefore handshakes *first* and only reaches for a reboot if the
screen does not answer, with a deadline on both (`_HELLO_WAIT_S`, `_RESET_WAIT_S`)
and three tries across a window wide enough for a USB replug. Skipping the reboot is
safe because the loop's first push is a full frame (`DiffPusher` starts with no
previous frame), so whatever was on the panel is painted over within a second — and
it takes ~15 s off every start. A screen that will not talk at all now ends with a
line naming the cable and the restart command, and exit code 2, instead of hanging:
the one thing the old behaviour could not do was tell you which of the two it was.
`display.reset_on_start: true` restores the vendor's order.

### The idle ↔ game transition

Detection debounces asymmetrically already — game after `enter_after_s: 4`, back
to idle after `exit_after_s: 25`, so alt-tabs and loading screens do not flip the
panel. Since the top half never moves, the switch is a strip-level event, and
`layout.transition: wipe` covers even that: one dark frame (near-black PNG ≈ 1 KB),
hold `transition_hold_s: 0.12`, then the new layout. The panel has no framebuffer
and slow pixels, so without the gap the outgoing layout ghosts through the
incoming one and reads as a glitch. Wipes are skipped automatically on the serial
revisions, where an extra frame cannot be afforded; `transition: none` disables
them everywhere. The hold's visible effect can only be judged on the real panel.

## Vendored dependency (not in git)

`vendor/` holds an **unmodified** copy of turing-smart-screen-python and is
git-ignored: the upstream tree is 1.1 GB, 1.05 GB of which is theme artwork this
app never loads. Only `vendor/README.md` (provenance: byte-for-byte upstream
`main`, CRLF endings) and `vendor/LOCK.txt` (sha256 of the 20 files we import)
are tracked. On a fresh clone:

```
powershell -File tools\vendor_lock.ps1 -Fetch     # sparse clone + write LOCK.txt
powershell -File tools\vendor_lock.ps1 -Verify    # prove the copy did not drift
```

## Bandwidth budget (it decides what the layout may animate)

A push is **not** `w*h*2` bytes on every revision:

| revision | how a push is encoded | full 800×480 frame | one trend strip (382×40) |
|---|---|---|---|
| `TUR_USB` (TURZX) | driver PNG-encodes each push (`send_pil_image_auto`, 1 MB cap) | **~50 KB** | ~3.4 KB |
| `C` (this panel) | raw BGRA, one `0x00` per 249 payload bytes, 115200-baud CDC port | **1.51 MB ≈ 0.82 s** | ~46 KB ≈ **25 ms** |
| `A/B/D`, WeAct | raw pixels over the same kind of CDC port | ~750 KB–1.5 MB (same order) | ~30–46 KB |

The `TUR_USB` row is computed against the vendored encoder (`_encode_png`,
`compress_level=9`) and `tools/liveview.py` recomputes it on every tick. The `C`
row is **measured on the real panel** with `tools/screen_bench.py`. Note what it
says: the "115200-baud serial" label describes the CDC signalling rate, not the
pipe — the panel's USB bridge takes the bytes as fast as the host writes them
(~1.8 MB/s sustained, more in short bursts), so the old 68-second full-frame
estimate was off by two orders of magnitude.

Consequence: **the whole screen may redraw every second on this panel**, so the
60 s trend bands stay on (`layout.trend_bands: true`). A full frame is 82 % of a
1 Hz tick, so the diff transport in `app/output.py` is still what keeps the loop
comfortable: a typical tick pushes a few bands (~25 ms each), and only the
burn-in shift, the exercise sweep and the first frame pay the full 0.82 s.
`layout.trend_bands: false` remains the knob for a genuinely slow link.

## The panel on this desk (connected 2026-09-23)

Plug it in and the host sees a **hub inside the screen** with two CDC-ACM
gadgets behind it:

| what | where | role |
|---|---|---|
| `1A40:0101` "USB2.0 HUB" | on the root-hub port | the screen's internal hub |
| `1A86:CA21` "UsbMonitor", serial `CT21INCH` | COM3 | sleeping side: opening it **wakes** the panel |
| `1D6B:0106` "Android", serial `20080411` | COM4 | the live link — this is the port to talk to |

That pair is exactly what `library/lcd/lcd_comm_rev_c.py` auto-detects, so
`display.revision: C` with `com_port: AUTO`. The HELLO handshake answers
`chs_5inch.dev1_rom1.88` → 5" 480×800 portrait (800×480 landscape), ROM 88, so
`portrait_width/height` stay at 480/800 and the vendored map picks
`SubRevision.REV_5INCH`. Keep `com_port: AUTO`: the awake COM port is *not*
stable — a `Reset()` reboots the panel and its port changes, which is why the
driver re-detects on every open. Startup therefore spends ~15 s on
wake → reset → re-detect → HELLO (the panel also shows its boot logo).

If a screen ever appears to be dead, check the chain in this order —
`tools/usb_probe.py --verbose` (does libusb/Windows see anything at all?),
`tools/plug_watch.ps1` (timestamped plug events), then
`tools/screen_link_probe.py` (which COM port is which gadget). A panel that
lights up but shows no device anywhere is a power-only cable/port, not a driver
problem; the awake gadget only exists once the panel has enumerated properly.

1. ~~Device Manager → note the hardware ID~~ done: revision **C**, see above.
2. ~~Confirm 800×480~~ done: `chs_5inch`, ROM 1.88.
3. `pip install pyusb` is only needed for a `TUR_USB` panel; this one is pure
   pyserial (already in the venv).
4. For full sensor fidelity: `pip install pythonnet`, set `sensors.backend: lhm`
   (or `auto`), run as Administrator. **Verified on this machine** (7950X + 5090):
   CPU Tctl temp, socket power (`Total Power`), per-core clocks (peak/avg), GPU
   all via LHM; NVML as fallback. Ring0 driver works under HVCI; the earlier
   `None`s were sensor-matching bugs, now fixed.

   Elevated autostart (one UAC prompt, from anywhere — it registers the task and
   starts it). `Interactive + Highest` keeps it inside your desktop session so
   foreground-window game detection keeps working, and it stops any running
   instance first, because the panel has exactly one COM port:

   ```powershell
   Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-Command',
       "& { & 'C:\Users\ofhd\Documents\Projects\PC Monitor\tools\install_autostart.ps1' } "
   ```

   Remove it again with `tools\install_autostart.ps1 -Remove` (elevated). To watch
   what it did: `schtasks /query /tn PCMonitor /v /fo LIST`, and the app's own
   start-up trace lands in `log.log` next to `main.py`.

   A healthy start looks like this (the app logs its own banner, because
   `pythonw.exe` has no stdout and every `print()` would otherwise vanish):

   ```
   [start] pid=10840 ppid=28040 backend=LhmBackend revision=C port=AUTO panel=800x480 frames=starting
   [frames] presentmon session live — present-based detection on
   [state] game pid=12345 steam=1245620 frames=118      # only on idle <-> game changes
   ```

   A live-but-empty capture says so instead of going quiet:

   ```
   [frames] no rows from presentmon for 20+ s of rendering (header=no, args: --no_console_stats …);
   child said: printed nothing at all — frame stats stay -- and present-based detection is off
   ```

   Expect **two** `pythonw.exe` PIDs for one app: the venv launcher and the
   interpreter it re-execs. The banner is printed by the real one (`ppid` matches
   the launcher), which is also the one holding COM4 — so `Get-Process` counts are
   not a sign of a double start. `Stop-ScheduledTask`/`Start-ScheduledTask` work
   without elevation, which makes restarting the panel loop cheap.

## Roadmap

- [x] PresentMon frame stats (fps, frametime, 1%/0.1% low) + present-based game
      detection + Steam AppID identity — 2026-09; FRAME TIME is the displayed
      frame interval, not click-to-photon (PresentMon's input-latency providers
      could add the real one later). Confirmed live on this desk 2026-09-24
      (`[state] game … frames=170`), after two separate causes were cleared: a
      machine-level telemetry wedge that no combination of args, binary or session
      name could work around, and a time column read by the wrong name
      (`CPUStartQPCTimeInMs` is what `--qpc_time_ms` emits) — both written up in
      "When the present stream is silent", with the parser pinned to the real
      header by `tools/frames_selftest.py`.
- [ ] Per-game profiles keyed by Steam AppID (frametime target line, accents)
- [x] Power model: sensor-based (CPU socket + GPU board power) + researched
      base constant for this build + 9% VRM/PSU overhead — see config.yaml comments
- [ ] Optional night schedule for brightness
- [ ] App-specific themes (per-game accents) later
