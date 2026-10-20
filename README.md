# PC Monitor

A native C#/.NET 10 Windows tray application for the Turing Smart Screen 5-inch
revision-C panel (800 × 480). No Python runtime, browser, or development checkout
is needed to run the installed app.

The CPU/GPU panels, RAM, disk/network trends, total-power estimate and automatic
game layout follow the original display. Game mode includes PresentMon FPS,
frame time, 1% low and 0.1% low statistics. Missing readings show `--`; held frame
measurements are labelled and expire after 12 seconds. Total power remains an
estimate, with measured/modelled input provenance.

## Everyday use

The tray menu provides Start, Stop, Restart, Blank screen, Show screen, Settings,
logs and status. Start monitoring begins at Windows sign-in. Locking or turning
the monitors off blanks the panel while the application remains running.

Normal blanking sends one black framebuffer and lowers brightness. It never
sends the panel's firmware power-off/restart command. The USB connection remains
open, and returning to the desktop repaints current statistics before restoring
brightness. Actual loss of USB power during sleep can still require the panel
firmware to boot and enumerate again.

The default rotation is **180 degrees** for the internally connected, inverted
mount. Set `Rotation` to `0` for the original mounting. COM discovery uses the
panel's hardware IDs, checks the awake interface first, and opens the wake
interface only when necessary. COM numbers can change.

Windows Night light is read from its effective CloudStore state, including
manual overrides. External gamma warmth is followed too. A transient unreadable
Windows state retains the last confirmed appearance. Manual `NightMode` values
`on` and `off` remain available if a Windows update changes the private encoding.

## Build and install

Requirements: Windows 10/11 x64, PowerShell, and the .NET **10 SDK** for building.
The published installation is self-contained and does not require a separately
installed .NET runtime.

```powershell
./scripts/Build.ps1 -Publish
# Run the following in Administrator PowerShell:
./scripts/Install.ps1
```

The build fetches the pinned PresentMon 2.5.1 executable and verifies its SHA-256,
restores the locked NuGet dependency graph, builds with warnings as errors, runs
the native regression harness, and publishes to `.artifacts/publish`.

Installation copies the published build to `%ProgramFiles%\PCMonitor` and
registers `PCMonitorNative` as an interactive, elevated, sign-in scheduled task.
Elevation is needed for CPU temperature/package-power driver access and reliable
ETW frame capture. Task Scheduler restarts the tray after unexpected failure;
the tray independently replaces a worker that stops making progress. Installation
exports and retires the legacy `PCMonitor` and `PCMonitorWatchdog` Python tasks.

Configuration, bounded logs, and status live in `%LOCALAPPDATA%\PCMonitor`:

- `settings.json`: validated settings, reloaded transactionally every three seconds.
- `Logs/tray.log`, `Logs/worker.log`: 2 MiB each plus two previous generations.
- `status.json`: periodic status and component health, also refreshed by status commands.

A deliberate Stop leaves the tray available without restarting monitoring.
Exit closes the application until the next sign-in or scheduled-task start.

```powershell
& "$env:ProgramFiles\PCMonitor\PCMonitor.exe" --command status
& "$env:ProgramFiles\PCMonitor\PCMonitor.exe" --command restart
& "$env:ProgramFiles\PCMonitor\PCMonitor.exe" --command blank
& "$env:ProgramFiles\PCMonitor\PCMonitor.exe" --command unblank
```

Use `scripts/Uninstall.ps1` from Administrator PowerShell to unregister autostart.
It preserves the installed files, configuration and logs for inspection/rollback.

## Configuration

The first run creates settings with the previous brightness/power-model values.
`IdleIntervalSeconds` defaults to 1; `GameIntervalSeconds` defaults to 0.25.
Dark screens suspend sensor/frame collection and rendering. Idle timers set to
zero are disabled. `GameProcesses` can explicitly list executable names.
`Trends`, `PixelShift`, lock/display following, brightness and night warmth are
configurable. Unknown keys or invalid values are rejected; live edits that fail
validation leave the last working settings active and write the reason to the log.

## Architecture

- `TrayApp.cs`, `WindowsHost.cs`: tray controls, Windows power/session/device
  notifications, foreground-window facts, watchdog and user-scoped pipe control.
- `Worker.cs`: latest-intent control loop; owns sensors, game detection, rendering
  and the serial panel. Host events supersede a frame composed before them.
- `Panel.cs`: revision-C framing, timed serial I/O, acknowledged diff updates,
  warm blanking, reconnect and physical 180-degree rotation.
- `Telemetry.cs`, `FrameCapture.cs`: LibreHardwareMonitor CPU/GPU sensors, native
  host counters, PresentMon lifecycle/parsing and bounded per-swapchain history.
- `Renderer.cs`, `NightLight.cs`: fixed 800 × 480 layout and night appearance.

The tray and worker communicate through current-user-only named pipes. Windows
job objects close owned children when their parent exits. A stalled native call
is contained by replacing the worker process, with bounded restart backoff.
After persistent failure or stale USB following resume, recovery can restart the
panel interface or its verified composite parent. Shared hubs and unrelated USB
devices are refused. No per-frame threads, heartbeat-file writes, device disable operations,
or upstream theme checkout are required.

## Verification

```powershell
./scripts/Build.ps1
# Optional visual previews, visibly marked SIMULATED:
.artifacts/publish/PCMonitor.exe --render idle.png
.artifacts/publish/PCMonitor.exe --render game.png --game
# Read real sensors and discover devices without opening the display:
.artifacts/publish/PCMonitor.exe --diagnose diagnostics.json
```

Regression fixtures pin full/partial packets from the reviewed Python driver for
ROM 88 and 89. Tests cover rotation, diff regions, acknowledgement failure,
black-frame blanking, light precedence, stale/missing power, CSV faults, FPS
arithmetic, percentile calculations, and captured Windows Night light blobs.
CI builds and publishes on Windows. Physical acceptance results and outstanding
long-duration checks are recorded in [docs/validation.md](docs/validation.md).

The original Python history and reliability fixes remain on the
`fix/airtight-all-open-issues` branch. Local retired files are archived outside
the checkout during migration. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md)
and [LICENSE](LICENSE) for component provenance and licensing.
