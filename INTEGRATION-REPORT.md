# Integration report — reliability-hardening union

**Branch:** `integrate/reliability-hardening` (based on `main` @ `4707915628ace3443f60aa4b8edb1a49f5ad6483`)
**Tested candidate SHA:** `25c3cdbdbb96a88942cd3919d2d77791e868323b` (gate run 09; supersedes `523de44` from gate run 08 — the delta is one deployment-found installer fix, §6)
**Tree state at candidate:** clean (`git status --porcelain` empty).
**Deployment:** installed and running on this machine (§6) — the scheduled tasks and the live app execute exactly this tree.
**Not done, deliberately:** no merge into `main`, master tracker #33 untouched.

---

## 1. Exact commands and results

```powershell
# full offline gate, strict (missing vendor assets would FAIL instead of SKIP)
.\.venv\Scripts\python.exe tools\run_offline_tests.py --strict
# -> .integ/gate/gate-run-08.txt
#    offline gate (strict) — .venv\Scripts\python.exe
#    passed 35   failed 0   advisory-fail 0   skipped 0        exit 0

# the gate's own honesty suite runs first inside every gate run (selector `gate`)
.\.venv\Scripts\python.exe tools\run_offline_tests.py --list      # 35 selectors, exit 0

# the combined control-loop suite on its own (~3 min, 120 fault cycles)
.\.venv\Scripts\python.exe tools\e2e_control_loop_selftest.py     # SELFTEST PASSED, exit 0

# the corrected-input-sample regression (finding PR #43) and its neighbours
.\.venv\Scripts\python.exe tools\hoststate_selftest.py            # SELFTEST PASSED
.\.venv\Scripts\python.exe tools\wake_gap_selftest.py             # SELFTEST PASSED
.\.venv\Scripts\python.exe tools\idle_clock_selftest.py           # SELFTEST PASSED
```

Failures: none in run 08. Skips: none (the vendored Turing library, `res/fonts`
and the PresentMon pin are present in this workspace, so `--strict` had nothing
to excuse). One **honest advisory** line is printed by the `owned` suite —
`cannot check which processes are ours: … OSError: no powershell here` — in this
sandbox; it is non-gating by design and the suite still passes.

Prior transcripts kept for comparison: `gate-run-05/06` (registry bring-up),
`gate-run-07` (34/0/0/0 before the #43 fix and the E2E suite existed).

---

## 2. Recommended review / merge order

Each commit is one reviewable area; later commits assume earlier ones (they
share the resolved files). Merge in this order:

| # | Commit | Area |
|---|--------|------|
| 1 | `c123d9b` | gate registry + `--strict` + env/vendor pin integrity |
| 2 | `7a0fbc3` | host state union (#1,#2,#3,#4,#5,#19) incl. input-interval fix |
| 3 | `5e0bf09` | recovery coordinator + start-up containment/observer |
| 4 | `1466c7f` | panel link: one owner, journal, verified reset, diff generations |
| 5 | `9c6eca3` | capture: parse, recover, ETW/session shutdown |
| 6 | `faf4890` | appearance: night, warm ramp, sweep/hold veto, diff cache |
| 7 | `e825b0a` | sensors/power/units/game identity |
| 8 | `38e4ce5` | config schema + transactional preview reload |
| 9 | `e9a92be` | ownership (fail-closed lock) + installer/observer scripts |
| 10 | `30c02d2` | `main.py` — the combined loop |
| 11 | `cda6113` | `tools/e2e_control_loop_selftest.py` (selector `e2e_loop`) |
| 12 | `dc14761` | README/docs union |
| 13 | `523de44` | nightlight probe follows the fallback contract |
| 14 | `25c3cdb` | installer: `app.owned` gets PYTHONPATH (deployment-found, §6) — **= tested candidate HEAD** |

`main.py` was deliberately **not** committed per-area: it is one resolved
control flow (5←3←4←17←22←23←24←25←28, 14 conflict regions) and reviewing it as
one diff against those nine branches is the honest unit.

---

## 3. Issue → code → test matrix (#1–#32)

Gate selectors are what `run_offline_tests.py --list` runs. "PR" is listed
where the branch↔PR pairing was established during integration; the branch
name itself encodes the issue for every row.

| Issue | Branch (`origin/fix/…`) | PR | Primary code | Gate selector(s) |
|---|---|---|---|---|
| 1 | issue-1-correct-power-guids | #35 | `app/hoststate.py` (GUID/PBT literals, dispatch) | `hoststate`, `eventwindow` |
| 2 | issue-2-abi-correct-event-window | #36 | `app/hoststate.py` `EventWindow` (instance-bound atom, WNDCLASS lifetime) | `eventwindow`, `hoststate` |
| 3 | issue-3-fix-wake-gap-detection | #44 | `app/hoststate.py` gap watchdog boundaries | `wake_gap`, `hoststate` |
| 4 | issue-4-pending-suspend-input | #34/#47 | `app/hoststate.py` `_request_suspend`/`_note_input`; `main.py` resume-edge hold | `hoststate` (`case_aborted_sleep_resolves_on_new_input`, `case_stale_sample_does_not_answer`) |
| 5 | issue-5-nonblocking-recovery-coordinator | #47 | `app/recovery.py`; `app/panel.py` `tick(reconnect_only)`; `main.py` | `recovery`, `panel_link`, `sweep` (hold veto) |
| 6 | issue-6-serial-io-single-owner | #64 | `app/panel.py` generations/`_gate`; `app/display.py` `_abandon` | `panel_owner` |
| 7 | issue-7-durable-recovery-journal | #40 | `app/panel.py` journal reconcile + legacy-marker adoption | `panel_recovery` |
| 8 | issue-8-verified-panel-usb-reset | #50 | `app/panel.py` ladder + pnputil verification | `usb_reset`, `panel_recovery` |
| 9 | issue-9-transactional-diff-cache | #37/#62 | `app/output.py` + link `on_relink` invalidation | `diff_cache`, `e2e_loop` |
| 10 | issue-10-recover-stalled-presentmon | #46 | `app/frames.py` stall/exit detection, restart | `frames_recovery`, `e2e_loop` |
| 11 | issue-11-isolate-malformed-rows | #48 | `app/frames.py` row parser isolation | `frames_rows`, `frames` |
| 12 | issue-12-job-object-child-shutdown | #52 | `app/frames.py` job object + bounded child kill | `frames_shutdown` |
| 13 | issue-13-fresh-swapchain-selection | #60 | `app/frames.py` swapchain/session selection; deque slice fix | `frames`, `frames_recovery` |
| 14 | issue-14-nightlight-manual-override | #39 | `app/nightlight.py` override precedence | `nightlight` |
| 15 | issue-15-preserve-night-appearance | #53 | `app/nightlight.py`/`app/lights.py` fallback = honest untinted, not temperature pixels | `lights`, `nightlight` |
| 16 | issue-16-gamma-ramp-dll-fix | #49 | `app/nightlight.py` ramp build/apply via ctypes (ABI-correct) | `gamma_ramp` |
| 17 | issue-17-bounded-sensor-recovery | #56 | `app/sensors/__init__.py` bounded sample, backoff reopen, marked-held/blank | `sensors`, `sensor_counters`, `e2e_loop` |
| 18 | issue-18-reprime-counters | #41 | `app/sensors/` disk/net counter re-prime after wrap/replug | `sensor_counters` |
| 19 | issue-19-idle-arithmetic-boundaries | #43 | `app/hoststate.py` idle input arithmetic over the sampling interval | `idle_clock`, `hoststate` |
| 20 | issue-20-config-schema-validation | #45 | `app/config.py` schema, NaN/Inf/malformed refusal | `config_schema` |
| 21 | issue-21-game-mode-stability | #42 | `app/gamewatch.py` held-path liveness (below-floor games) | `gamewatch`, `e2e_loop` |
| 22 | issue-22-interruptible-sweep | #55 | `app/burnin.py` + `main.sweep_step` interruption, hold veto | `sweep` |
| 23 | issue-23-startup-restart-recovery | #54 | `main.py` pre-heartbeat stop intent + budget; `app/liveness.py`; installer | `recovery_startup` |
| 24 | issue-24-single-instance-ownership | #63 | `app/instance.py` fail-closed `acquire_main_role` | `instance` |
| 25 | issue-25-owned-process-stop | #58 | `app/owned.py` `.owner` record; installer stops only its own | `owned` |
| 26 | issue-26-reproducible-install | #55/#66 | `tools/env_check.py`, `vendor_lock.ps1`, `bootstrap.ps1`, requirements pins | `env_check`, `gate` |
| 27 | issue-27-network-unit-contract | #65 | `app/snapshot.py`/panel units (net bit/s→Mbps, disk bytes/s) | `units` |
| 28 | issue-28-power-estimate-provenance | #42 | `app/power.py` measured/derived/none provenance to panel+probe | `power_estimate` |
| 29 | issue-29-steam-env-key-normalize | #42 | `app/steamid.py` case-folded env keys | `steamid` |
| 30 | issue-30-independent-regression-gate | #55 | `tools/run_offline_tests.py`, `gate_selftest.py`, CI job | `gate` |
| 31 | issue-31-synthetic-fps-labeling | #57 | `tools/liveview.py` `--synth-fps=auto`, SIMULATED marking | `liveview` |
| 32 | issue-32-transactional-hot-reload | #62 | `tools/liveview.py` generation-keeping reload | `liveview_reload` |

**Cross-cutting corrections applied during integration** (all with the
regressions above): #60 deque slice; #64/#56 fenced late native work, bounded
backend construction, no reopen while prior ownership unresolved; #47
coordinator/panel retry bypass; #53 fallback temperature→pixels; #63
fail-closed lock; #54 pre-heartbeat stop intent + restart budget; #43 input
samples compared over their own interval (verbatim t=.90/.91, ages .20/.21,
loop dt .91 case now pinned in `hoststate_selftest`).

---

## 4. Status split — what is proven where

**Offline gate (this workspace, any machine, no admin, no hardware):** all 35
selectors green under `--strict`, including the new `e2e_loop` control-loop
suite (120 fault cycles, barrier-observed mid-rebuild suspend, persistent
hang, late completion, capture exit/stall, sensor fault→hold→blank→reopen,
4 fps game + expiry, dark intent within two ticks, clean shutdown, and the
one-owner port ledger as a high-water assertion).

**Native Windows (needs the real OS objects, not this sandbox):**
- `instance` runs its multi-process claim/crash-release legs for real on
  Windows; in this run the reduced-privilege/advisory legs reported honestly.
- `owned` printed its advisory (no PowerShell here) — rerun on the target
  desk to exercise the actual stop path against a live main + collector.
- The event-window/display-GUID suites fake the SDK boundary; the window
  class, atom lifetime and broadcast receipt need a real desktop session.
- `install_autostart.ps1` / `watchdog_autostart.ps1` / `bootstrap.ps1` are
  syntax- and logic-gated offline only; the scheduled-task registration and
  bounded restart loop must be exercised on the target machine.

**Physical panel / hardware (unverified by anything in this gate — do not
accept these from the green run):**
- Real USB re-enumeration (pnputil disable/enable, the journal surviving a
  pull-mid-reset) — `usb_reset` gates the *decision*, not the device.
- Actual HELLO/ack behaviour on a real Turing panel under a live hub (the
  E2E bench fakes the vendored driver class, faithfully to its observed
  habits — including `openSerial()` on write failure — but not to its
  electrical behavior).
- Real monitor power / console events (WM_WTSSESSION_CHANGE, real
  PBT_* streams), real presentmon children and ETW session contention,
  real LHM/NVML handles and driver resets, real sleep/hibernate cycles and
  the watchdog restarting a crashed main on the target desk.

**Known remaining caveats on the candidate:**
- The abandoned-native-work leak is *bounded and counted* (`abandoned`,
  `sensor-sample` backoff), not eliminated — Python cannot cancel a blocked
  driver call; the suites pin that it cannot corrupt ownership or the loop.
- `vendor/presentmon` binary itself is a download; `env_check --strict`
  verifies its pin, not that a fresh clone has run `fetch_presentmon.ps1`.

Master tracker #33 stays open; it lists the acceptance criteria this branch
now lets you verify in one command (`tools/run_offline_tests.py --strict`)
plus the hardware checklist above.

---

## 6. Deployment to this machine (done after the gate)

`tools\install_autostart.ps1` was run elevated from the candidate tree. What
it did, from its own transcript (`.integ/deploy/install-elevated.log`):

1. `env_check.py --strict` inside the elevated run: **ENVIRONMENT OK**
   (vendor tree at `2b33ab4`, fonts, LHM dll, PresentMon pin, 7 pinned dists).
2. Stopped the one real leftover: `pid 32192: our collector, still owning an
   ETW session - stopping` — an orphaned `presentmon.exe` from an older run,
   stopped by ownership, not by name matching.
3. Re-registered `PCMonitor` (Interactive+Highest, logon trigger, restart 3×
   per 5 min, unlimited execution time, IgnoreNew) and registered the new
   `PCMonitorWatchdog` (every 5 min, `app/liveness.py` policy).
4. Started the task and verified it through `app.owned live`.

Post-deploy verification (all from the live tree):

- `PCMonitor: Running`, last result `0x41301`; watchdog `Ready`, first pass
  `result=0x0`.
- `.owner` names this checkout's `main.py`; `.heartbeat` advances
  (`tick 69 → 75 → 98`, ~1/s); `python -m app.liveness decide` → **`ok`,
  beat 1s old, state idle`**.
- `boot.log`: panel identified on the bus — `USB_VID_1D6B&PID_0106&MI_00
  (COM4, matches a known panel)`, `revision=C`, `backend=LhmBackend`,
  host events registered (`session-display, console-display,
  monitor-power`).
- `log.log`: `sensors=ok backend=LhmBackend fails=0`, `presentmon session
  live — present-based detection on`, night state reported honestly,
  `night=off src=windows`, and the 60s beat line
  `up=1m00s idle light=lit … capture=live panel=up`.

**Deployment-found fix (`25c3cdb`):** the elevated relaunch did not carry the
working directory, so `python -m app.owned` died with `No module named 'app'`
before registering anything. `Invoke-Owned` now sets `PYTHONPATH` to the
install root for the call. Gate re-run after the fix: **run 09 — passed 35,
failed 0, advisory-fail 0, skipped 0, exit 0** (with the app running
throughout; no suite touches the real panel or device store).

**Still physical-only:** what the panel actually *shows* (brightness, night
tint, game layout) is for eyes on the desk; everything the software can
observe about it is green above.