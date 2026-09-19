# PC Monitor — desk telemetry display

Telemetry → 5" USB-C "Turing-family" LCD (TURZX) via
[turing-smart-screen-python](https://github.com/mathoudebine/turing-smart-screen-python)
(vendored in `vendor/`, used as a library, unmodified).

## Run now (no hardware needed)

```
.venv\Scripts\python main.py --backend demo          # simulated screen at http://localhost:5678
.venv\Scripts\python tools\liveview.py               # LIVE two-up layout editor preview → http://localhost:5680
.venv\Scripts\python tools\layout_check.py --no-trends  # geometry guard (collisions / panel overflow)
.venv\Scripts\python main.py                         # real sensors (psutil+NVML), simulated screen
.venv\Scripts\python main.py --dump x.png --force-state game   # headless frame preview
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
| `app/sensors/` | backends: `lhm` (LibreHardwareMonitorLib via pythonnet, full fidelity incl. package power & per-core clocks), `fallback` (psutil+NVML, no admin), `demo` |
| `app/power.py` | total-watt estimate (base+cpu+gpu) & green→red 50–1000 W gradient |
| `app/gamewatch.py` | idle/game state: fullscreen foreground window + GPU busy, with hysteresis |
| `app/layout.py` | 800×480 rendering: permanent top half + mode strip, trend bands, worst-case-fit type |
| `app/history.py` | bounded ring buffers feeding the trend bands |
| `app/output.py` | numpy diff → only changed bands are sent to the panel |
| `app/burnin.py` | brightness schedule, screen-off on idle, 3-px layout shift, periodic color sweep |
| `tools/liveview.py` | dev server: live idle+game two-up, hot-reload, push-cost stats |
| `tools/layout_check.py` | geometry guard: value extremes → collisions / panel overflow |
| `tools/vendor_lock.ps1` | pin + verify the vendored library (see `vendor/README.md`) |

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
| `A/B/C/D`, WeAct | raw RGB565 over 115200-baud serial | ~750 KB ≈ **68 s** | ~30 KB ≈ 2 s |

Measured against the vendored encoder (`_encode_png`, `compress_level=9`) in
July 2026; `tools/liveview.py` recomputes both numbers on every tick.

Consequence: on **USB** the whole screen may redraw every second, so the 60s trend
bands cost nothing. On the **serial** revisions only a few hundred pixels per tick
are affordable, which is what `layout.trend_bands: false` is for (bands collapse
to plain level bars, only digits change) — and why `app/output.py` diffs at all.
`tools/liveview.py` prints both numbers live under the screenshots.

## When the screen arrives

1. Device Manager → note the hardware ID; set `display.revision` in `config.yaml`
   (`C` = classic Turing 5", `TUR_USB` = newer TURZX; it may enumerate as a COM port)
2. Confirm it is 800×480 landscape; adjust `portrait_width/height` if not
3. `pip install pyusb` if revision is `TUR_USB`
4. For full sensor fidelity: `pip install pythonnet`, set `sensors.backend: lhm`
   (or `auto`), run as Administrator. **Verified on this machine** (7950X + 5090):
   CPU Tctl temp, socket power (`Total Power`), per-core clocks (peak/avg), GPU
   all via LHM; NVML as fallback. Ring0 driver works under HVCI; the earlier
   `None`s were sensor-matching bugs, now fixed.

   Elevated autostart (run once from an elevated PowerShell; `Interactive +
   Highest` keeps it inside your desktop session so game detection works):

   ```powershell
   $action    = New-ScheduledTaskAction -Execute "C:\Users\ofhd\Documents\Projects\PC Monitor\.venv\Scripts\pythonw.exe" `
                -Argument '"C:\Users\ofhd\Documents\Projects\PC Monitor\main.py"' `
                -WorkingDirectory "C:\Users\ofhd\Documents\Projects\PC Monitor"
   $principal = New-ScheduledTaskPrincipal -UserId "$env:USERNAME" -LogonType Interactive -RunLevel Highest
   Register-ScheduledTask -TaskName "PCMonitor" -Action $action -Principal $principal `
                -Trigger (New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME) `
                -Settings (New-ScheduledTaskSettingsSet -StartWhenAvailable -ExecutionTimeLimit 0)
   ```

   Then stop my non-elevated demo instance so it doesn't hold port 5678:
   `Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'main\.py' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }`

## Roadmap

- [ ] PresentMon frame stats (fps, 1%/0.1% low, frame latency) + per-game profiles
- [x] Power model: sensor-based (CPU socket + GPU board power) + researched
      base constant for this build + 9% VRM/PSU overhead — see config.yaml comments
- [ ] Optional night schedule for brightness
- [ ] App-specific themes (per-game accents) later
