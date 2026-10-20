# Native migration validation

Recorded on 2026-10-20, Windows 11 build 26200, Ryzen 9 7950X / RTX 5090,
Turing 5-inch revision C, ROM 88, internal USB header. Configuration uses
180-degree rotation. Results describe this machine and these runs.

## Automated and source checks

- Original Python baseline: 37 offline suites passed before migration, including
  120 simulated fault cycles. Retired sources and notes were archived outside
  the checkout; their history remains on `fix/airtight-all-open-issues`.
- Native Release build: zero warnings and errors; self-contained win-x64 publish.
- Native regression harness: **76 assertions passed**. Full-frame and partial
  packet hashes match the pinned Python driver for ROM 88 and 89, including
  edge coordinates, payload escaping and packet padding.
- Tests cover reversible rotation, diff regions, missing acknowledgements,
  retrying only an idempotent status query, no power-off/reset on blanking,
  light-policy precedence, disabled idle timers, manual blanking, stale/partial
  power provenance, malformed CSV, correct FPS/percentiles, captured Night light
  blobs, and rejection of shared hubs/unrelated devices as recovery targets.
- Idle and game preview PNGs rendered and visually inspected. Synthetic data is
  marked SIMULATED PREVIEW and is available only through the preview command.

## Physical / installed checks

| Check | Result |
|---|---|
| Inverted mounting | User confirmed upright orientation and correct layout |
| Deployment | Runs from Program Files through elevated interactive sign-in task |
| Real CPU/GPU sensors | Live CPU temperature, power, clocks, GPU metrics and host counters verified |
| PresentMon | Real capture reports live after installation/elevation |
| Start / Stop / Restart | Exercised; stopping keeps the tray and stops its worker |
| Worker crash | Forced exit of the verified worker recovered automatically |
| Invalid live config | Rotation 90 rejected; running 180-degree configuration preserved |
| Warm black frame | Same worker and connected COM4 retained while blanked |
| Warm unblank | 0.731 s in the first installed control-cycle check |
| Windows monitor off/on | Real Windows power messages; black then current statistics |
| Lock/unlock | Real session lock; panel blanked and returned after unlock |
| Sleep/resume | Passed after correcting the stale composite-device recovery |
| Manual night/day | Both appearance plans applied to the physical panel, then auto restored |

The first sleep test exposed a real failure: COM4 remained enumerated and healthy
in Device Manager, but I/O returned `A device attached to the system is not
functioning`. Restarting the CDC interface alone did not fix it. Restarting its
verified parent `USB\VID_1D6B&PID_0106\20080411` restored communication. Native
recovery now learns the interface from a successful HELLO, persists that identity,
checks live device state, and verifies the composite parent's matching VID/PID
before restarting it. It never selects the shared hub and never disables a device.
The sleep retest recovered automatically; measured time from the suspend call
returning to live statistics was **17.319 s, including the user's unlock delay**.
The control loop now retries that resume failure sooner than in that measurement.

Ordinary monitor blanking performs none of that USB recovery: it retains the
connection, sends black once, and restores a complete current frame before normal
brightness. Firmware boot following actual electrical USB power loss remains a
hardware constraint.

## Frozen-display follow-up

After initial installation the user reported frozen statistics despite connected,
live status. A direct serial trace reproduced partial-update replies with an
unchanging `renderCnt:0`; the old code accepted any nonempty reply and advanced
its diff cache. The user confirmed that both full and partial diagnostic counters
could visibly advance, so the static firmware counter alone does **not** establish
the original freeze's cause. The initial checks were insufficient evidence of
sustained physical statistics updates. Complete uploads returned `full_png_sucess`.

The driver now checks the actual full-frame reply, parses `needReSend` and
`renderCnt`, and repaints the current complete image if a partial update requests
a resend or reports the wrong counter. It keeps complete uploads for that
connection, still skips unchanged frames and sends black only once. This is a
conservative compatibility fallback and uses more USB bandwidth; its game refresh
rate is limited by complete-frame transfer time. Padded device identities are also
trimmed before reading the firmware version. Status exposes confirmed frame
counts and frame/sample timestamps; the installed check now requires display
progress, fresh samples, and a new confirmed frame after unblanking.

Regression coverage includes captured reply bytes, silently ignored partial
updates, explicit resend requests, nonempty failure replies, fallback retention,
and refusing to cache failed uploads. A hardware probe exercised the fallback
and received successful complete-frame replies. After installing the fallback,
the user confirmed that real statistics were updating. The installed 30-second
check recorded 31 additional frames, fresh sensor samples, no repeated uploads
while blank, and a current frame after unblanking in **0.842 s**. Combined CPU use
over that sample was **0.198% of the machine** (6.347% of one logical core); this
is a short idle sample, not a long-term performance guarantee.

## Remaining normal-use acceptance

Normal display and hardware polling now target **2 updates per second** via
`IdleIntervalSeconds: 0.5` and `SensorIntervalSeconds: 0.5`. Game rendering retains
its 0.25-second target and reuses hardware samples between polls. The worker
performs sensor acquisition and panel writes sequentially, so the complete-frame
compatibility fallback may limit actual cadence below the configured target.
Blanking still pauses collection and rendering. Sensor interval validation
rejects nonfinite values and intervals below 0.25 seconds.

A 48–72 hour soak, a full OS reboot/sign-in cycle, physical unplug/replug, repeated
hibernation, and gameplay across multiple graphics APIs have not been completed
in this session. Game capture mathematics and parsing are tested, and the real
collector is live; this does not claim an end-to-end gameplay certification.
Windows Night light decoding uses a private CloudStore format: captured ON/OFF
fixtures and live registry reads are verified, with last-confirmed appearance on
read failure and a manual override if the OS format changes.

For repeatable installed resource and warm-blank checks:

```powershell
./scripts/Test-Installed.ps1 -ExerciseControls
```

This briefly blanks the physical screen, samples the installed processes, restores
normal display, and writes `.artifacts/installed-checks.json`. It does not lock or
sleep the PC. The installed logs are bounded and report connection, capture,
settings, night appearance and recovery transitions.
