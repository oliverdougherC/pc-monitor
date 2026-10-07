# Reliability plan — the road to an unattended panel

Issue #33 is the tracker for the findings raised against review baseline
`e6745a6` (#1–#32) and the later reviews against `451d58e` (#67–#69). This file
is its deliverable: which issue tracks which failure, **where the repair lives
now**, which gate case proves it, and — just as plainly — what no offline run can
claim.

Earlier revisions of this file described a stack of one-issue PRs. Those were
consolidated: every repair below is in this branch, and the repository has one
gate that runs them all. So the "PR" column is gone; what replaces it is the
column that matters to a reader deciding whether to trust the panel, which is
**the case that would fail if the repair were reverted**.

## What a green bar does and does not mean

```
.venv\Scripts\python tools\run_offline_tests.py            # everything, one exit code
.venv\Scripts\python tools\run_offline_tests.py --strict   # what CI runs
```

On a bare clone `vendor/` is a pin, not a checkout, and the theme fonts are not
there either. Cases that genuinely need them report **SKIP**, are listed as *not
covered*, and are never counted as coverage; `--strict` (the CI setting) turns
such a case into a FAIL, and the workflow fetches the pinned tree first so the
three vendor-boundary cases really run.

A green bar therefore means: *the non-hardware cases passed, on this diff, with
the pinned dependency installed.* It does not mean the app has been near a
screen, a USB reset, an ETW session, a lock, or a suspend. See the last section.

## The map

**P1** = a reliability/lifecycle/safety defect to fix before treating the app as
unattended. Every repair is implemented and carries a gate case; the gate's
output is the evidence, and `tools/run_offline_tests.py --list` names each case.

### 1. Prevent harmful recovery and competing owners

| Issue | P | The failure | Where the repair lives | Case that fails if reverted |
|---|---|---|---|---|
| #7 | P1 | USB device disabled with no journal; a crash between disable and re-enable leaves the panel dead | `app/panel.py` (`_disable_and_enable`, `_write_record`, `_adopt_legacy_mark`) | `panel_recovery` |
| #8 | P1 | USB reset can target a name-matched or unrelated device | `app/panel.py` (`_verified_target`, `_parent_of`, `_usb_restart`) | `usb_reset` |
| #25 | P1 | install/remove kills *every* `python main.py` | `app/owned.py`, `tools/install_autostart.ps1` | `owned` |
| #24 | P1 | single-instance/device ownership acquired too late | `app/instance.py`, `main.py` start-up order | `instance` |
| #12 | P1 | PresentMon job-object rights wrong; child shutdown races the reaper | `app/frames.py` (`_KillJob.adopt`, `close`) | `frames_shutdown` |
| #6 | P1 | serial I/O has no single owner: a build's lease was not truthful, so escalation could reset the device under a live bring-up | `app/panel.py` (`building`, `_claim_lease`, `_usb_restart`, `openSerial` re-check) | `panel_owner` |

The #7/#8 rule stands: non-destructive retry beats escalating to an unverified
device disable, and both are only *offline*-verified. `panel.py` refuses rung 3
when it cannot write the journal, and refuses any rung while a bring-up holds the
port.

### 2. Correct Windows state and an interruptible output lifecycle

| Issue | P | The failure | Where the repair lives | Case that fails if reverted |
|---|---|---|---|---|
| #1 | P1 | display-power GUIDs and session constants differed from the documented ones | `app/hoststate.py` constants | `hoststate`, `eventwindow` |
| #2 | P1 | event window: non-pointer-sized handles under x64 ABI, fake notification handles, dead pump | `app/hoststate.py` (`Native`, `EventWindow`) | `eventwindow` |
| #3 | P1 | wake-gap detection compared against its own elapsed dt | `main.py` (`elapsed_dt`), `app/hoststate.py` (`tick`) | `wake_gap` |
| #4 | P1 | the input that *requested* Sleep cleared the suspend's idle accounting | `app/hoststate.py` (`_request_suspend`, `_note_input`) | `hoststate`, `e2e_loop` |
| #19 | P2 | idle arithmetic breaks at the signed and DWORD tick boundaries | `app/hoststate.py` (`input_age_ms`, `idle_seconds`) | `idle_clock`, `lights` |
| #5 | P1 | recovery ran on the event loop and rebuilt everything for a display notification | `app/recovery.py`, `main.py` | `recovery`, `e2e_loop` |
| #9 | P1 | the diff cache commits on send, not on acknowledged push | `app/output.py`, and the vendor write path in `app/display.py` | `diff_cache` |
| #23 | P1 | a bad morning (start-up failure, dead capture) ends the run until the next logon | `app/liveness.py`, `app/bootlog.py`, `main.py` retry clocks | `recovery_startup`, `control_plane` |

#9 is worth reading twice: its residual defect was **below** the diff cache. The
pinned vendor's `WriteLine` swallows `serial.SerialTimeoutException` and returns,
and `serial_write` discards the byte count, so a frame that never left the host
looked like a successful call. `app/display.py::harden_vendor` now patches that in
the running process — the hash-verified `vendor/` tree stays byte-identical — and
`diff_cache` drives the real `LcdComm` with a timing-out pyserial to prove a push
is not acknowledged on a frame that did not land.

### 3. Recover telemetry and preserve truthful game state

| Issue | P | The failure | Where the repair lives | Case that fails if reverted |
|---|---|---|---|---|
| #10 | P1 | PresentMon alive-but-stalled is never recovered; capture health latched on header receipt | `app/frames.py` (`_stall_reason`, `_read_stream`, `state`) | `frames_recovery` |
| #11 | P1 | malformed present rows poison parsing; indefinite waits after reader failure | `app/frames.py` (`_ingest`, `_reap`) | `frames_rows` |
| #13 | P1 | an obsolete swapchain shown forever; generation not fenced at commit; measurement age taken from pipe activity | `app/frames.py` (`_best`, `_sweep`, `_ingest`, `stats`, `presenters`) | `frames`, `frames_recovery` |
| #17 | P1 | sensor acquisition unbounded; a retained snapshot presented as live telemetry | `app/sensors/__init__.py` (the supervisor, `make_hub`), `app/sensors/lhm.py` | `sensors` |
| #18 | P2 | disk/network counters not re-primed after absence, topology change or resume | `app/sensors/fallback.py` (`_Rate`) | `sensor_counters` |
| #21 | P2 | game mode oscillates below the entry FPS threshold; pid reuse not verified without a capture | `app/gamewatch.py` (`_held`, `_window_holds`) | `gamewatch` |
| #29 | P2 | Steam identity read environment keys with the wrong normalization | `app/steamid.py` | `steamid` |
| #27 | P2 | network throughput displayed in bytes while the pipeline keeps bits | `app/layout.py` (`bitrate`), `app/snapshot.py` | `units` |
| #28 | P2 | missing CPU/GPU telemetry becomes a reassuring "measured" watt total | `app/power.py`, `app/layout.py` | `power_estimate` |
| #69 | P2 | NVML initializations taken on every reacquisition and never balanced | `app/sensors/fallback.py` (`_nvml_open`, `_nvml_release`, `close`) | `sensors` |

#69 is a native reference-lifetime defect, and the counting fake is the point:
the case asserts `inits - shutdowns` equals the initializations currently owned,
and reaches zero after cleanup, for init-success-with-no-device, failed handle
lookup, repeated query errors, recovery, repeated close and backend replacement.

### 4. Make appearance follow the latest confirmed intent

| Issue | P | The failure | Where the repair lives | Case that fails if reverted |
|---|---|---|---|---|
| #14 | P2 | a manual Night light OFF is OR-ed with schedule inference | `app/nightlight.py` (`_read_windows`) | `nightlight` |
| #15 | P2 | a transient state-read failure loses the last confirmed night appearance | `app/nightlight.py` (`StateUnreadable`, `_blob`, `_hold`) | `nightlight` |
| #16 | P2 | the gamma-ramp second opinion called `EnumDisplaySettingsW` from the wrong DLL | `app/nightlight.py` (`_display_api`) | `gamma_ramp` |
| #68 | P2 | a neutral gamma ramp never clears a ramp-owned look, so the panel stays amber | `app/nightlight.py` (`_read`) | `nightlight` |
| #22 | P2 | the burn-in sweep ran uninterruptibly and could override sleep/off/game | `main.py` (`sweep_step`), `app/burnin.py` | `sweep` |

#68 preserves source authority exactly: the ramp may only take back the look it
gave. A measured neutral clears a ramp-owned ON; an unreadable ramp holds; and a
confirmed Windows ON is never cleared by a neutral ramp, because Windows' own
night light does not go through the ramp at all.

### 5. Make installation and future changes verifiable

| Issue | P | The failure | Where the repair lives | Case that fails if reverted |
|---|---|---|---|---|
| #20 | P2 | documented config keys unwired; unsafe values accepted at load | `app/config.py` (`SCHEMA`, `load`) | `config_schema` |
| #26 | P2 | clean install relocks upstream `main` instead of the reviewed pin | `tools/vendor_lock.ps1`, `tools/bootstrap.ps1`, `vendor/LOCK.txt` | `env_check` |
| #30 | P2 | the gate itself was unverified, and a hung case could strand it | `tools/run_offline_tests.py`, `tools/gate_selftest.py`, CI workflow | `gate` |
| #31 | P2 | real-sensor liveview silently substitutes synthetic FPS | `tools/liveview.py`, `app/layout.py` (the `SIMULATED` badge) | `liveview` |
| #32 | P2 | liveview hot reload non-transactional; stale `DemoBackend` references | `tools/liveview.py` | `liveview_reload` |
| #67 | P1 | `--dump` shared the production owner record, stop request, heartbeat and stop marker | `main.py` (`dump_role_requested`, `note_deliberate_stop`), `app/owned.py`, `app/liveness.py` | `control_plane` |
| #33 | P1 | the tracker itself: no honest map of what is verified | this file | — |

#67 is the one where two roles exist, so it is the one proved with two processes:
`control_plane` seeds a production control plane, runs a real `main.py --dump`
against it, and compares every production file byte for byte. Its Ctrl-C clause
calls the app's own decision function for both roles rather than trying to deliver
a console Ctrl-C to a console-less child — `send_signal(SIGINT)` raises on
Windows, `CTRL_BREAK` did not reach the handler through `runpy`, and
`PyThreadState_SetAsyncExc` reported success while the exception was never
observed. The static half of that case fails if a future edit adds a shared-state
write outside the role gate.

## What remains unverified even with every case green

This is the part the reviews said out loud, and nothing here changes it.

* **Offline ≠ hardware.** Every case exercises simulated panels, captured
  CloudStore blobs, synthetic present streams and fake clocks. None of it has
  touched USB enumeration, a real device disable/re-enable, ETW, NVML, LHM, or a
  monitor that actually goes dark.
* **The hardware acceptance run has never been executed**: monitor off/on,
  lock/unlock, sleep/hibernate/resume, an unattended automatic resume (the
  no-flash contract), clustered notifications, unplug/replug, start-up with the
  panel absent, delayed USB readiness, game launch/quit/alt-tab/display-mode
  change, low-FPS games, capture stalls, sensor/driver failures, and
  multi-monitor/session transitions.
* **The soak has never been executed**: the 48–72 h run with normal use plus
  repeated fault cycles, where the gate is no unexplained manual recovery, no
  stale values shown as current, no unwanted flashes, no orphaned collectors, and
  CPU/memory/threads/handles/logs that **plateau** rather than grow with uptime.
  Resource plateaus are asserted offline for the gate runner only
  (`tools/gate_selftest.py`); nothing measures the app.
* **No latency budget exists yet.** Event → decision → dispatch → first correct
  visible frame has never been measured end to end; until a healthy hardware
  baseline is recorded, any numerical recovery guarantee would be fabricated.
* **Installer/remove and clean-install behaviour** (#24, #25, #26) needs real
  machines and real installs; no offline case can cover what they do to unrelated
  processes or a pristine environment.
* **#13's follow-up generation fence** is asserted for the commit-under-lock
  barrier by injection (`frames_selftest`), not against a genuinely preempted
  reader thread mid-`_ingest` — the barrier is what the fix adds, and the case
  drives it deterministically rather than hoping for the interleaving.
* **Two deliberate behaviour changes from the review round**, both product calls
  rather than defect fixes, and both easy to reverse if the desk disagrees:

  * a sensor sample that is collected on a *later* tick than it was acquired is
    published `held=True` with its true age, and expires on its acquisition
    tick's grace. A driver that consistently answers just past `tick_timeout_s`
    therefore reads as held/dimmed rather than fresh — which is what "must not
    enter history as a current measurement" requires, but it is one tick of
    latency that the panel did not show before;
  * `close()`'s early-return path (shutdown while a reopen is still inside the
    driver) still leaks the backend the hub was holding, because the code
    deliberately does not join a native call. `MAX_LIVE_WORKERS` still counts
    live threads rather than owned handles — now an honest proxy, since a
    generation releases its ledger before its thread exits, but the cap
    semantics were left alone.
* **The data rate is one number, and it was quietly capped by the transport.**
  `sensors.interval_s` is the panel's whole notion of a refresh rate — it is a
  framebuffer with no scan-out, so it lights whatever arrives. Raising it from
  1 s to 0.25 s exposed a latent defect rather than a hardware limit: measured
  against this panel a full frame is 0.77 s and bands run at ~24 MB/s bursts,
  but a real tick changes a median of **7–8 bands** while `DiffPusher` gave up
  at **6**, so every tick fell through to a full frame and the loop sat near
  2 Hz whatever the config said. With the cap as a named parameter (12) the
  same desk measures **3.52 Hz** at 0.284 s/tick, ~133 ms of that on the link,
  and 7% of one core. The numbers here are from this desk's panel; a different
  revision's transport changes them, which is what the README's bandwidth table
  is for.

When the hardware run and the soak have been executed and recorded — response
latency, maximum outage, missed recoveries, process/thread/handle counts, memory,
CPU, log growth — this file gets its final section: the numbers. Until then the
honest status of "unattended" is: **designed for, offline-verified in parts,
hardware-unverified entirely.**
