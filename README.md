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

Everything behavioural has an offline proof that needs neither admin nor hardware —
run them all before trusting a change. One command runs all of them and returns one
exit code (this is also what CI runs; a case that cannot run for want of the vendored
library reports SKIP, which is never counted as coverage):

```
.venv\Scripts\python tools\run_offline_tests.py       # all of the below, one exit code
.venv\Scripts\python tools\frames_selftest.py         # fps/GPU/mode parsing, held values, stream guards
.venv\Scripts\python tools\gamewatch_selftest.py      # entry speed, alt-tab keeps the game's numbers
.venv\Scripts\python tools\hoststate_selftest.py      # sleep / displays-off / lock / frozen loop
.venv\Scripts\python tools\idle_clock_selftest.py     # idle arithmetic at the 25- and 50-day tick boundaries
.venv\Scripts\python tools\lights_selftest.py         # what the panel does about each of those
.venv\Scripts\python tools\nightlight_probe.py --selftest   # the CloudStore decode, pinned to real blobs
.venv\Scripts\python tools\panel_link_selftest.py     # link survives raise/hang, rebuilds, walks its device ladder
.venv\Scripts\python tools\fault_selftest.py          # nothing outside the per-tick guard (AST-checked)
.venv\Scripts\python tools\layout_check.py            # geometry at the value extremes, both states
```

`tools/liveview.py` renders **both** states from the same telemetry and
hot-reloads `app/layout.py` / `app/power.py` / `config.yaml` on save — edit the
layout and the browser updates within a tick (no restart). It also reports, per
state, how many bands a partial panel update would need (same math as
`app/output.py`), and has zoom / brightness / grid / burn-in-shift overlays.

## Architecture

| File | Role |
|---|---|
| `main.py` | entry: 1 Hz loop, wiring, and the per-tick guard that keeps one subsystem's raise from ending the app |
| `app/display.py` | builds the panel object for `display.revision` (the only module that imports the vendored `library.*`) |
| `app/panel.py` | the panel as a **link that can fail**: guarded/timed device calls, rebuild on its own clock, replays orientation+brightness, and asks Windows to restart the USB device when reopening the port cannot help |
| `app/hoststate.py` | what the machine is doing: asleep / monitors off / locked, from `WM_POWERBROADCAST` + the console-display power setting + session notifications, with a tick-gap fallback |
| `app/nightlight.py` | is the user's night mode on, and how warm: Windows Night light read out of CloudStore (CompactBinary), plus the gamma ramp as a second opinion for f.lux-style tools |
| `app/lights.py` | the one light decision: dark? how bright? which warmth LUT? (asleep → locked → monitor-off → idle → game/idle/dim, night applied on top) |
| `app/sensors/` | backends: `lhm` (LibreHardwareMonitorLib via pythonnet, full fidelity incl. package power & per-core clocks), `fallback` (psutil+NVML, no admin), `demo` |
| `app/frames.py` | real fps/frametime/1%–0.1% low per process: spawns PresentMon (ETW, admin), parses the present stream, answers "who is rendering" and "at what frame rate", and *holds* a stopped game's last number (marked stale) instead of inventing one |
| `app/steamid.py` | Steam identity: `SteamAppId`/`SteamGameId` env vars → which pids are Steam games + AppID (enrichment, never the detection core) |
| `app/power.py` | total-watt estimate (base+cpu+gpu) & green→red 50–1000 W gradient |
| `app/gamewatch.py` | idle/game state, scored by confidence (STRONG/MED/LEGACY/NONE) with a **locked frame target** an alt-tab cannot steal; hysteresis per tier |
| `app/layout.py` | 800×480 rendering: permanent top half + mode strip, trend bands, worst-case-fit type, night-mode LUT applied to the finished frame |
| `app/history.py` | bounded ring buffers feeding the trend bands |
| `app/output.py` | numpy diff → only changed bands are sent to the panel |
| `app/burnin.py` | 3-px layout shift and the periodic color sweep (brightness/screen-off moved out to `app/lights.py`) |
| `tools/liveview.py` | dev server: live idle+game two-up, hot-reload, push-cost stats, real present-stream fps |
| `tools/layout_check.py` | geometry guard: value extremes → collisions / panel overflow |
| `tools/frames_probe.py` | what the PresentMon stream sees right now: presenters, AppIDs, fps/frametime |
| `tools/frames_health.ps1` | no admin: which of LIVE / STARVED / DENIED / QUIET the capture is in, straight from `log.log` |
| `tools/frames_selftest.py` | no admin: replays synthetic present streams through the real parser; fps math, GPU-busy/mode parsing, the held-value window, and the silence/column guards |
| `tools/gamewatch_selftest.py` | no admin: fake present streams + fake focus through the real detector — fast entry, alt-tab holds the game's numbers, quick exit on quit, video is not a game, target handover |
| `tools/hoststate_selftest.py` | no admin: replays power/session broadcasts through the real state machine (sleep, monitor timeout, lock, frozen-loop fallback, slow start) |
| `tools/idle_clock_selftest.py` | no admin: replays 25 and 50 days of uptime at the idle clock against a scripted boot clock - the signed tick boundary, the 32-bit wrap, and a clock that cannot be read |
| `tools/nightlight_probe.py` | prints what Windows actually stores; `--selftest` replays the captured blobs, `--watch 30` follows a manual toggle |
| `tools/panel_link_selftest.py` | the link against the simulated panel and against deliberately bad devices: raise, hang, rebuild — plus the three-rung device ladder, its order and cadence, and the disable/enable journaling (including "a refused disable is never followed by an enable") |
| `tools/lights_selftest.py` | the light decision end to end: sleep, displays-off, idle, dim, precedence, the night cap and LUT, and a rendered frame measured before/after the warmth |
| `tools/fault_selftest.py` | the per-tick guard: fallback + one log line per fault, `SystemExit` contained, Ctrl-C not — plus an AST pass that fails if any subsystem call in the loop has escaped the guard |
| `tools/hoststate_probe.py` | live watcher at 2 Hz: what each power/session event did to the state (`--trace` for raw window messages) |
| `tools/screen_wake_probe.py` | raw serial HELLO / TURNON / RESTART: is the panel deaf, and does anything bring it back |
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

Game mode is decided by **evidence quality, not a single timer**. Each presenting
process is scored every tick (`app/gamewatch.py`):

| tier | evidence | enters after |
|---|---|---|
| STRONG | the foreground process is presenting, over its whole monitor — or a name in `game.processes` | `detection.enter_strong_s` (1.2 s) |
| MED | foreground presenter without full coverage · a Steam-flagged presenter · a presenter whose `MsGPUBusy` is ≥ `detection.min_gpu_score` % of the frame · a hardware-flip presenter | `game.enter_after_s` (4 s) |
| LEGACY | no present stream at all: foreground window covers its monitor, no caption, GPU busy | `game.enter_after_s` |
| NONE | a browser, a chat app, the shell, a capture tool — however full-screen and however busy | never |

The NONE row is what stops a 60 fps YouTube tab from being a game: a frame counter
alone cannot tell them apart, a process name and a GPU-busy figure can. That split is
measured on this desk, not guessed — a scrolling browser presents `Composed: Flip`
with `MsGPUBusy` ≈ 19 % of the frame, `re9.exe` at 48 fps presents `Hardware Composed:
Independent Flip` at ≈ 100 %. The flip-mode classification is therefore never the
gate on its own (the real game here is *not* in exclusive flip); it is one signal
beside GPU busyness and foreground ownership.

**Once in game mode the frame target is locked, and nothing steals it.** Other
presenters are ignored while the target lives, foreground or not — so tabbing out to
a browser, an overlay, or a video on the second monitor cannot move the numbers,
which is what the panel used to do. The lock is released when the process disappears
(`detection.dead_exit_s`, i.e. ~3 s after the 2 s stream-liveness window, instead of
the old 25 s) or when it stops presenting altogether (`exit_after_s`, so an alt-tab
keeps the last measurement, dimmed and labelled `HELD`, while a quit does not). A new
STRONG candidate that has been alone for `detection.switch_silence_s` takes the lock,
which is how starting game B from game A's desktop works. `SteamAppId`/`SteamGameId`
(`app/steamid.py`) then say *which* Steam game it is — Steam's own launch contract,
inherited through launcher chains — used for preference and (later) per-game
profiles, never as the detection core: protected processes block the env read and
direct-launched exes skip Steam, so both cases are still caught by present +
foreground.

`tools/gamewatch_selftest.py` proves all of it offline: real parser, real state
machine, synthetic streams and a stubbed foreground window. Those behaviours are
exactly the ones no live spot-check catches, because they only happen at the moment
you tab out.

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

The autostarted loop does not use that fatal ending any more: `app/panel.py` calls
the same bring-up and turns "no screen" into a retryable False (see *When the link
itself is the problem*). `make_lcd` keeps the exit for the manual tools — for a
command you ran yourself, stopping is the right answer.

### The idle ↔ game transition

Detection debounces asymmetrically — game after 1.2 s when the evidence is
STRONG (`detection.enter_strong_s`), after `enter_after_s: 4` when it is weaker,
back to idle after `exit_after_s: 25` (or `detection.dead_exit_s: 3` when the
process is gone) — so alt-tabs and loading screens do not flip the panel while a
game that starts in your face is on the panel inside a second and a quit one stops
being reported almost at once. Since the top half never moves, the switch is a
strip-level event, and `layout.transition: wipe` covers even that: one dark frame
(near-black PNG ≈ 1 KB), hold `transition_hold_s: 0.12`, then the new layout. The
panel has no framebuffer and slow pixels, so without the gap the outgoing layout
ghosts through the incoming one and reads as a glitch. Wipes are skipped
automatically on the serial revisions, where an extra frame cannot be afforded;
`transition: none` disables them everywhere. The hold's visible effect can only be
judged on the real panel.

## What the panel does when the machine changes state

The panel is a second screen on a machine that sleeps, locks and dims. Five inputs,
one owner (`app/lights.py`), evaluated in this order — and the reason that lost the
panel before is that the inputs did not exist as data anywhere:

| reason | where it comes from | panel |
|---|---|---|
| `asleep` | `WM_POWERBROADCAST` `QUERYSUSPEND`/`SUSPEND` → back on `RESUME`/`RESUMEAUTOMATIC` | off, and the loop stops pushing |
| `locked` | `WTSRegisterSessionNotification` → `WTS_SESSION_LOCK`/`_UNLOCK`, reconciled against `WTSGetActiveConsoleSessionId` on every tick | off — nobody is at the desk |
| `console-lost` | our session is still running but another one took the console (fast user switching) | off |
| `monitor-off` | the `GUID_CONSOLE_DISPLAY_STATE` power setting (+ `GUID_MONITOR_POWER_ON`) — Windows' own display timeout, screensaver blank, or `nircmd`-style sleep | off |
| `idle` | `GetLastInputInfo` past `display.screen_off_after_min` — held while a game is presenting live frames | off (as before) |
| `dim` | quiet past `display.dim_after_s` (also held during live frames) | `brightness_dim` |
| `lit` | otherwise | `brightness_game` in game mode, `brightness_idle` not |
| `night` | Windows Night light (see below) | brightness × `night.brightness_scale`, warm LUT |

Each reason has a switch, because "the machine is asleep" and "nobody is looking" are
different opinions on someone's desk: `power.follow_sleep`, `power.follow_lock`,
`power.follow_display` and `power.seed_monitor` (the derived start-up answer, below)
each turn one line of the table off. Whatever is acting gets named in the log, so
`[light] panel off (asleep)` always says which rule you are fighting.

**A live game holds the two idle timers** (`display.stay_lit_in_game`, on by default).
`GetLastInputInfo` counts keyboard and mouse only, so somebody playing on a controller
is indistinguishable from an empty desk: the panel would dim at five minutes and go
dark at forty-five, mid-game. The honest evidence that the desk is in use is the thing
the frame view already measures — the locked target presenting frames *now* — so that
holds both timers, and an alt-tabbed or loading game (`stale` numbers, the same flag the
panel renders dimmed) releases them at once. `asleep`, `locked` and `monitor-off` are
never held: a machine that is asleep or handed to another session is not being played,
whatever the last frame counter said. When the hold is what keeps the panel lit, the
plan says so (`light=lit 70+play` in `[beat]`, and one `[light] idle timer held` line),
because "lit" and "lit despite the timer" are different things to read at 7 a.m.

Dark means `ScreenOff` **and** no rendering: on revision C a full frame is ~0.82 s of
the link, so a dark panel is also the one setting that costs the bus nothing. The
diff is invalidated on the way back, so the first frame after a wake is whole rather
than a few bands of a picture the panel no longer has.

**Why events, and not polling.** The loop is frozen while the PC sleeps, so "turn the
panel off when the PC sleeps" cannot be a rule evaluated after the fact — nothing is
evaluating. `app/hoststate.py` therefore owns a hidden top-level window (`WS_POPUP`,
its own thread, `PeekMessageW` on a 50 ms poll so `close()` is deterministic) that
registers the power-setting notifications and a session notification, and
`main.py` parks on that event flag (`host.wait()`) instead of `time.sleep()` — an
event lands in milliseconds instead of up to a whole tick later. Two details that
cost an hour each: a *message-only* window (`HWND_MESSAGE`) does **not** receive
broadcasts, it has to be a real hidden top-level window; and `lParam` for a power
setting is a pointer to a `POWERBROADCAST_SETTING`, so a null one must be ignored
before dereferencing (`from_address(0)` is an access violation, not an exception —
the selftest crashed with a bare `0xC0000005` status and no output).

When no event window can exist at all — no window station, a service-ish context —
the fallback is the loop's own tick gap: a 1 Hz loop that suddenly reports a 400 s
tick was frozen, and that is treated as a wake (`gap:400s` in the log) because it
means the panel link, the ETW child and the game lock all have to be re-made. The
first tick is exempt: a deaf panel takes ~35 s to declare itself deaf between
start-up and the first tick, which used to be announced as a resume.

On a resume the app re-makes everything that cannot survive a suspend: `panel.relink`
(re-open, re-assert orientation, demand a full repaint), `frames.restart` (the ETW
child is a husk after a suspend — and it restarts **forever** now, with backoff up to
60 s, because "give up after three tries" left one desk frameless until a reboot),
and `watch.reset` (the game we locked onto was frozen mid-frame).

### Night mode is the user's, not ours

The panel follows the night mode already configured on the machine, rather than
inventing a schedule of its own. `app/nightlight.py` reads Windows Night light from
its CloudStore blob — the payload is a Microsoft Bond **CompactBinary v1** structure,
so the module carries a ~100-line reader for it rather than a registry guess — and
takes `enabled`, the colour temperature, and the schedule (including the
sunset/sunrise variant). Because that store is the OS's own, the Quick Settings
toggle *and* the scheduled transitions both land within `night.refresh_s` (3 s), and
`log.log` says which of the two fired:

```
[night] night=on src=windows temp=2525K (state enabled=True schedule-now=False, changed 21:00:03; schedule=True set-hours 21:00-07:00 temp=2525K)
```

Night light writes nothing into the gamma ramp on this build (`GetDeviceGammaRamp`
returns identity with it on), but third-party warmers do, so the ramp is read as a
second opinion that may only ever *add* an on — a neutral ramp never overrides a
store that says warm, because that is the ordinary Windows case. The warmth is a
per-channel LUT (`img.point(lut)`) applied to the finished frame in
`app/layout.py`: one place to be wrong, everything shifts together including the
graphs, and near-black stays near-black because a LUT maps 0 to 0. `night.strength`
blends it toward neutral for when 2500 K reads as "the panel is broken", and
`night.mode: on|off` overrides the whole thing.

The state blob is deleted and rewritten on transitions and its `last_change` is a
file time, so a wrong decode is invisible unless it is pinned: `tools/nightlight_probe.py
--selftest` replays the two blobs captured from this machine (43 B with the enabled
field = on, 41 B without = off) and `--watch 30` follows a manual toggle live. What
that cannot settle from here is which night mode *you* use — if it is not Windows
Night light, `night.mode: on` or the gamma-ramp path covers it, and the `[night]`
line says which source it acted on.

### When the link itself is the problem

`app/panel.py` owns the device object; nothing else touches it. Every call is
serialised, bounded and survivable: a raise, a `SystemExit` (the vendor driver's own
"cannot open COM port" path calls `sys.exit(0)`, and `os._exit(0)` if that raises — a
missing screen has therefore been able to terminate the app), or a write that never
returns all mark the link **down** instead of escaping, and every later call
short-circuits until a rebuild. Rebuilds happen on a background thread, because a
bring-up against a dead device takes tens of seconds and the render loop cannot wait
for it — measured, and it also used to fake the tick-gap wake condition.

If reopening the port has not helped for two minutes, the app asks Windows to restart
the panel's USB device (`pnputil /restart-device`, needs the elevation the scheduled
task already has; `Restart-PnpDevice` no longer exists on this build). That is not
paranoia about a hypothetical: probed directly on this desk
(`tools/screen_wake_probe.py`), the COM port **opens fine and the first write
blocks** — which means no command can ever be delivered, including the vendor's own
RESTART. `pnputil` reports `Access is denied` while still exiting 0, which is why the
verdict is read from its text, not its code.

It escalates, because the layers fail differently — measured in the same session:

1. restart the **CDC interface** (`…&MI_00`) — re-initialises the serial pipe;
2. restart the **composite device** above it — the software unplug/replug;
3. **disable and re-enable** that device — takes it off the bus, which is as near to
   pulling the plug as Windows lets a program get.

The three rungs are a minute apart, so the whole ladder is walked about four minutes
into a real outage and then repeats every five: escalation is pointless if the strongest
rung takes a quarter of an hour to arrive, and a device that survives all three is not
going to be persuaded by a fourth every minute. The interface restart on this desk did
bring the pipe back (a read returned where the write had blocked) but HELLO came back
**empty**: pipe alive, MCU not.

Rung 3 has a real hazard: a device left **disabled** stays disabled through a reboot,
which would turn a dark panel into an absent one. So the intent is written to
`.panel_reset_pending` before the disable, the enable is retried three times, the
marker is deleted on success, and any start that finds the marker enables the device
before it does anything else. If even the retries fail, the log says so in capitals and
names the click that undoes it — because at that point the app has made the desk worse
and must not pretend otherwise. `tools/panel_link_selftest.py` pins all four of those
paths, including "a refused disable is never followed by an enable".

Honest limit, measured the same night: both restarts were run against this wedged panel
and it stayed deaf — pnputil reported success, the device re-enumerated, HELLO still
timed out. Then rung 3 was run too, elevated, on the real device: disable and enable
both reported success, the marker file was cleaned up, and the panel *still* would not
answer HELLO. A device restart re-initialises the port and a disable/enable takes the
device off the bus, but neither removes VBUS from the hub port, and a hung CH55x needs
exactly that. So the ladder is what recovers the panel without a hand on the cable
*when the fault is in the port*, and what proves — by running out — that the fault is
not. When it does run out the app says so in one line, every fifteen minutes, with the
action ("unplug the screen's USB, wait five seconds, plug it back") and the reassurance
that nothing has to be restarted: the loop takes the panel back within seconds of a
replug.

The loop also says whether it is alive at all: one `[beat]` line every
`display.heartbeat_s` (15 min, first one a minute after start) with uptime, state,
light reason, link, fps and host state. Everything else in the log is conditional, and
a loop that stopped at 4 a.m. otherwise looks exactly like a quiet one — which is how a
frozen panel got mistaken for a sleeping desk once already.

### Nothing may die quietly

The app runs under `pythonw.exe`: no console, no traceback on screen, no window to
notice. In that setup the worst failure is not a crash, it is a crash nobody sees — the
panel simply stops updating and the log says nothing wrong. So the tick body has three
layers of noise:

| what | line | scope |
|---|---|---|
| a subsystem raised | `[fault] render raised (3 total): … — the tick continues without it` | `main.Guard` around every external call in the loop; the tick is abandoned, the next one tries again |
| a thread died | `[fault] thread panel-bring-up raised: …` | `threading.excepthook` — otherwise a crashed rebuild thread just looks like a panel that stopped being retried |
| start-up failed | `[fatal] Traceback…` | the only death that is still final (unreadable config, missing sensor library, port denied) |

`Guard` catches `BaseException` and re-raises `KeyboardInterrupt`: the vendored driver
raises `SystemExit` for a COM port that will not open, and a missing screen must not be
able to stop the sensor loop. Repeating the same fault is counted rather than
re-reported (300 raises → one line, so a per-second fault cannot bury the log), but a
*different* subsystem is new information and is printed immediately. Fallbacks are
chosen per call, not globally: a detector that raises keeps the previous state instead
of falling to `idle`, and the light planner keeps the previous plan instead of jumping
to a default — the two fallbacks that would themselves look like the bugs this pass was
about.

The guard is only worth as much as its coverage, and coverage is the kind of thing an
unrelated edit quietly removes. `tools/fault_selftest.py` therefore parses `main.py`
and fails if any call into the panel link, the ETW child, the event window, Pillow, the
sensor hub or the diff pusher sits outside a `g.run(...)` argument or a `try` with a
handler. Add a bare `layout.render(...)` to the loop and the test says so.


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
   [start] pid=40092 ppid=50412 backend=LhmBackend revision=C port=AUTO panel=800x480 frames=starting
   [host] asleep=False(-) monitor=? locked=False console-lost=False idle=- events=console-display,monitor-power,session
   [monitor] off: idle 13424s vs 300s display timeout
   [night] night=off src=windows temp=2525K (state enabled=False schedule-now=False, changed 06:25:12; schedule=False sunset-sunrise 21:00-07:00 temp=2525K)
   [frames] presentmon session live — present-based detection on
   [state] game pid=12345 steam=1245620 frames=118 | strong:foreground-presenter (88% of present rows)   # only on idle <-> game changes
   [beat] up=1m00s game light=lit 70 panel=up frames=118 | asleep=False(-) monitor=True locked=False …
   ```

   `[start] panel=800x480` is the configured geometry, not a claim that the panel
   answered — `panel=up/down` in `[beat]` is the live link, and `[beat]` is the proof
   the loop is still running (one line per `display.heartbeat_s`, the first a minute
   after start). The seed line deserves a word: a process that starts
   while the screens are already asleep is told nothing — the display setting reports
   *changes* — so the state is derived once from the idle clock against the power
   scheme's own timeout, and marked `(seed)` until Windows says otherwise. It is a
   derivation because nothing can answer the question directly:
   `GetDevicePowerState(\\.\DISPLAY1)` fails on this machine (measured: it returns 0),
   and `CallNtPowerInformation(SystemPowerState)` reports the machine's sleep state, not
   the screen's. The lines that only appear when something happens:

   ```
   [light] panel off (asleep) — asleep=True(query-suspend) monitor=? locked=False …
   [resume] gap:412s (auto-suspend) — relinking panel, restarting capture, clearing the game lock
   [panel] link rebuilt (auto-suspend; rebuild #2) — full repaint
   [panel] no screen on start (bring-up said deaf) — continuing anyway, the loop keeps retrying every 10 s
   [panel] restarted the panel's USB port (USB\VID_1D6B&PID_0106&MI_00\8&…) — waiting for it to come back
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
- [x] Sleep / display-off / lock awareness — 2026-09; the panel goes dark when the PC
      sleeps or Windows turns the displays off, and comes back by itself
      (`app/hoststate.py` + `app/lights.py`, proven offline by
      `tools/hoststate_selftest.py`; the physical suspend test is still owed by a
      human, because putting this machine to sleep would take the session with it).
- [x] Night mode follows the user's — 2026-09; Windows Night light read from
      CloudStore (CompactBinary) + the gamma ramp as a second opinion, applied as a
      LUT on the finished frame with a brightness cap (`app/nightlight.py`,
      `tools/nightlight_probe.py`). Supersedes "optional night schedule": the
      schedule now comes from Windows, and `night.schedule` is only the fallback for
      a store that cannot be read.
- [x] Reliable frame view — 2026-09; locked frame target that an alt-tab cannot steal,
      confidence-tiered entry (1.2 s for STRONG, 4 s otherwise), quick exit when the
      game process dies, held-and-marked numbers instead of a live-looking freeze,
      per-process GPU-busy scoring, and a present-stream supervisor that retries
      forever (`tools/gamewatch_selftest.py`, extended `tools/frames_selftest.py`).
- [x] The panel link survives its own failures — 2026-09; `app/panel.py`
      (`tools/panel_link_selftest.py`), plus the measured fact that a wedged CDC
      endpoint still opens its COM port and blocks the first write
      (`tools/screen_wake_probe.py`), which is why a device ladder exists: restart
       the interface, restart the device, disable+re-enable it and name the cable as
       the answer. Run for real on this desk at 02:40 — all three rungs executed and
       journalled cleanly, and the panel stayed deaf: the fault is VBUS power, which no
       software can take away.
- [x] Nothing dies quietly — 2026-09; every subsystem call in the loop goes through a
       guard that logs `[fault] … the tick continues without it`, thread deaths are
       caught by `threading.excepthook`, start-up failures leave `[fatal]`, and a loop
       that stopped is visible as a missing `[beat]` line (`main.Guard`,
       `tools/fault_selftest.py`, which fails the suite if a subsystem call escapes).
 - [x] The panel knows when the desk is away — 2026-09; `locked` and `console-lost`
       joined `asleep` as dark reasons (`app/lights.py`, `power.follow_lock`), and a
       start-up while the displays are already off now derives that once from the
       power scheme's own timeout (`HostState.seed_monitor`, marked `(seed)` wherever it
       appears, and overridden by the first real notification or by fresh input).
       Measured live at 3:14: `[monitor] off: idle 13724s vs 300s display timeout` →
       `[light] panel off (monitor-off)`. It is a labelled derivation because
       `GetDevicePowerState(\\.\DISPLAY1)` fails on this machine (returns 0).
 - [x] A game on a controller is not an empty desk — 2026-09; the two idle timers are
       held while the locked target is presenting live frames
       (`display.stay_lit_in_game`), because `GetLastInputInfo` never sees a gamepad
       and the panel would otherwise dim at 5 minutes and go dark at 45 mid-game.
       `asleep` / `locked` / `monitor-off` are never held, `stale` numbers release at
       once, and a dark plan cannot claim a hold (`tools/lights_selftest.py`,
       case "a game on a controller is not an empty desk").
 - [x] Back off the rebuild cadence once the device ladder has been walked — 2026-09;
       10 s between attempts is right for a replug and loud for a wedge that lasts all
       night, so attempts after the last rung drop to 60 s (`_retry_interval`). Measured live:
       the last "retrying every 10s" at 3:19:09, the first "retrying every 60s" at 3:20:15.
- [ ] Per-game profiles keyed by Steam AppID (frametime target line, accents)
- [x] Power model: sensor-based (CPU socket + GPU board power) + researched
      base constant for this build + 9% VRM/PSU overhead — see config.yaml comments
- [ ] Confirm which night mode is wanted: if it is not Windows Night light, set
      `night.mode: on` (or rely on the gamma-ramp path) — the `[night]` line names
      the source it acted on
- [ ] Physical sleep/hibernate walk-through on this desk: watch `[light] panel off
      (asleep)` → `[resume] …` → `[panel] link rebuilt`, and the monitors-off case
- [ ] App-specific themes (per-game accents) later
