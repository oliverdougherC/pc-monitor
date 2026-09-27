# Reliability plan — the road to an unattended panel

Issue #33 is the tracker for the 32 finding issues (#1–#32) raised against
review baseline `e6745a6`. It is not itself a defect, and this file is its
deliverable: an honest map of **which issue tracks which failure, which PR
repairs it, and what is still unverified** — because a green CI bar here means
less than it looks, and pretending otherwise is how the panel ends up trusted
before it has earned it.

Nothing from #1–#32 is fixed on `main` yet: every repair PR below is still
open. What *is* on `main` is the measuring stick — the one-command offline gate
(`tools/run_offline_tests.py`, 423aa8b, with the CI job) and its verdict
contract ("a case has to say it passed, not just exit 0", 4707915).

## What a green bar does and does not mean

The `offline gate` job runs on a Windows runner against a **fresh clone**, which
has no vendored panel library and no theme fonts (`vendor/` is a pin, not a
checkout). So on every PR right now:

* `panel_link`, `lights` and `layout` **SKIP** — their cases import
  `library.lcd.*` or load theme fonts. The runner says so out loud
  (`skipped 3`, listed as *not covered* in the job summary), and the case list
  in the README names what each one would have proven.
* `hoststate` reads the live `GetLastInputInfo` clock, so it can report
  `ADVISORY-FAIL` whenever a human is at the keyboard. Advisory is never a
  pass; #43's PR removes the flag once the clock is pinned.
* A green bar therefore means: *the non-hardware cases passed on this diff*.
  On the development desk, where the vendored library and fonts are present,
  the same command runs everything with `skipped 0` — that is the only run
  where "all cases passed" is literally true, and it is offline-only: it
  exercises simulated panels and captured blobs, never USB, ETW or sleep.

As of 2026-09-26 every open PR listed below shows `offline pass`. That is the
weakened statement described above, and it is the only automated evidence any
of them has.

## The map

**P1** = a reliability/lifecycle/safety defect to fix before treating the app
as unattended. **State** is as of this file's commit; each PR's own page has
its current CI verdict.

### 1. Prevent harmful recovery and competing owners

| Issue | P | The failure | PR | State |
|---|---|---|---|---|
| #7 | P1 | USB device disabled with no journal; a crash between disable and re-enable leaves the panel dead | #40 | open, gate green |
| #8 | P1 | USB reset can target a name-matched or unrelated device — destructive action on the wrong hardware | #50 | open, gate green |
| #25 | P1 | install/remove scripts kill *every* `python main.py`, not just PC Monitor's | — | **no PR yet** |
| #24 | P1 | single-instance/device ownership acquired too late; two copies can fight over the panel and the ETW session | — | **no PR yet** |
| #12 | P1 | PresentMon job-object rights wrong; child shutdown races the reaper | #52 | open, gate green |

Until #7 and #8 are merged *and hardware-verified*, non-destructive retry beats
escalating to an unverified device disable. That is a behaviour constraint on
`app/panel.py`, not a test result.

### 2. Correct Windows state and an interruptible output lifecycle

| Issue | P | The failure | PR | State |
|---|---|---|---|---|
| #1 | P1 | both display-power GUIDs and the session logon/logoff constants differ from the documented ones — registering an unrelated GUID proves no events will arrive, and reserved codes are read as logon/logoff | #35 | open, gate green |
| #2 | P1 | event window: non-pointer-sized handles under x64 ABI, fake notification handles, dead pump thread | #36 | open, gate green |
| #3 | P1 | wake-gap detection compares against its own elapsed dt, so a frozen loop's missed resume is invisible | #34 | open, gate green |
| #4 | P1 | the input that *requested* Sleep clears the suspend's idle accounting; also pins the input clock and drops the gate's advisory flag | #43 | open, gate green |
| #19 | P2 | idle arithmetic breaks at the signed and DWORD tick boundaries (25/50 days) | #44 | open, gate green |
| #6 | P1 | serial I/O has no single owner: timed-out workers survive and compete with rebuilds | — | **no PR yet** (shares ground with #40/#47/#50) |
| #5 | P1 | recovery runs on the event loop and rebuilds healthy subsystems for ordinary display notifications | #47 | open, gate green |
| #9 | P1 | the framebuffer diff cache commits on send, not on acknowledged push — a failed push leaves the panel believed-up-to-date | #37 | open, gate green |
| #23 | P1 | a bad morning (startup failure, dead capture) ends the run until the next logon | #54 | open, gate green |

### 3. Recover telemetry and preserve truthful game state

| Issue | P | The failure | PR | State |
|---|---|---|---|---|
| #10 | P1 | PresentMon alive-but-stalled is never recovered; legitimate idle not distinguished | #46 | open, gate green |
| #11 | P1 | malformed present rows poison parsing; indefinite waits after reader failure | #48 | open, gate green |
| #13 | P1 | an obsolete swapchain is shown forever instead of a fresh selection with bounded obsolete state | — | **no PR yet** |
| #17 | P1 | sensor acquisition is unbounded and a retained snapshot keeps presenting as live telemetry | #56 | open, gate green |
| #18 | P2 | disk/network counters not re-primed after absence, topology changes or resume — deltas across a gap are fiction | — | **no PR yet** (discussed in #56's body, not fixed there) |
| #21 | P2 | game mode oscillates when a game sits below the entry FPS threshold; process identity not verified | — | **no PR yet** |
| #29 | P2 | Steam identity reads environment keys with the wrong normalization for the keys Windows delivers | #38 | open, gate green |
| #27 | P2 | network throughput displayed in bytes while the pipeline keeps bits | #41 | open, gate green |
| #28 | P2 | missing CPU/GPU telemetry becomes a reassuring "measured" watt total | #42 | open, gate green |

### 4. Make appearance follow the latest confirmed intent

| Issue | P | The failure | PR | State |
|---|---|---|---|---|
| #14 | P2 | a manual Night light OFF is OR-ed with schedule inference and flips back on | #49 | open, gate green |
| #15 | P2 | a transient state-read failure loses the last confirmed night appearance | #53 | open, gate green |
| #16 | P2 | the gamma-ramp second opinion calls `EnumDisplaySettingsW` from the wrong DLL | #39 | open, gate green |
| #22 | P2 | the burn-in sweep runs uninterruptibly and can override sleep/off/game with an animation | #51 | open, gate green |

### 5. Make installation and future changes verifiable

| Issue | P | The failure | PR | State |
|---|---|---|---|---|
| #20 | P2 | documented config keys unwired; unsafe values accepted at load | #45 | open, gate green |
| #26 | P2 | clean install relocks upstream `main` instead of the reviewed pinned dependencies | — | **no PR yet** |
| #30 | P2 | the gate itself was unverified, and a hung case could strand it | #55 | open, gate green (gate + CI itself already on `main`) |
| #31 | P2 | real-sensor liveview silently substitutes synthetic FPS | — | **no PR yet** |
| #32 | P2 | liveview hot reload was non-transactional and checked against stale classes | #57 | open, gate green |

## Suggested merge order

Every open PR except #49 and #53 registers a new gating case, so all of them
edit the same `CASES` list in `tools/run_offline_tests.py` (and the README
list). That list is the repo-wide choke point: any two of them can merge in
any order, but **each merge makes the next branch conflict until it is
rebased** and its gate re-run. Beyond that, the clusters below touch the same
source files and want a deliberate order inside the cluster; across clusters
the tracker's phase order is fine.

1. **#55 (#30) first** — it is the measuring stick the rest are judged by, and
   it anchors the top of the case list. (#57 then rebases onto it: its "nine
   commands" becomes "ten", its `liveview` entry keeps its place at the end.)
2. **hoststate cluster** — #35 (correct GUIDs/constants) → #34 (wake-gap) →
   #43 (suspend-request input, drops the gate's advisory flag) → #44 (idle
   boundaries) → #47 (recovery coordinator, adds `app/recovery.py`). All five
   edit `app/hoststate.py`.
3. **panel-link cluster** — #37 (diff cache commits on ack) → #40 (journal) →
   #50 (verified USB reset) → #51 (interruptible sweep). All touch
   `app/panel.py` or the panel's callers; #40/#50 also touch
   `tools/panel_link_selftest.py`.
4. **config** — #45 (`app/config.py` schema; #42, #51, #53, #56 also touch
   `app/config.py`, so land it early in the second half). After it merges,
   #57's liveview staging should adopt its `load_or_keep()` in a follow-up.
5. **capture cluster** — #46 → #48 → #52, all in `app/frames.py`.
6. **night cluster** — #39 → #49 → #53 (`app/nightlight.py`).
7. **telemetry/units** — #56, #41, #42 (#42's one-line `tools/liveview.py`
   hunk and #57's reload-region edits do not overlap; either order merges).
8. The no-PR issues (#6, #13, #18, #21, #24, #25, #26, #31) keep their issues
   open; #6 in particular should land with, or right after, the panel-link
   cluster, since its single-owner contract is what those PRs assume.

## What remains unverified even when every PR above is merged

This is the part the review already said out loud and nothing here changes:

* **Offline ≠ hardware.** Every case in the gate exercises simulated panels,
  captured CloudStore blobs, synthetic present streams and fake clocks. None
  of it has touched USB enumeration, a real device disable/re-enable, ETW,
  NVML, LHM, or a monitor that actually goes dark. The review's 15
  source-derived counterexamples were likewise reproductions, not the app.
* **The hardware acceptance run has never been executed**: monitor off/on,
  lock/unlock, sleep/hibernate/resume, an *unattended automatic* resume (the
  no-flash contract), clustered notifications, unplug/replug, startup with the
  panel absent, delayed USB readiness, game launch/quit/alt-tab/display-mode
  change, low-FPS games, capture stalls, sensor/driver failures, and
  multi-monitor/session transitions.
* **The soak has never been executed**: the proposed 48–72 h run with normal
  use plus repeated fault cycles, where the gate is no unexplained manual
  recovery, no stale values shown as current, no unwanted flashes, no orphaned
  collectors, and CPU/memory/threads/handles/logs that **plateau** with fault
  cycles rather than grow with uptime. Resource plateaus are asserted offline
  for the gate runner only (`tools/gate_selftest.py`); nothing measures the app.
* **No latency budget exists yet.** Event → decision → dispatch → first
  correct visible frame has never been measured end to end; until the healthy
  hardware baseline is recorded, any numerical recovery guarantee would be
  fabricated.
* **Installer/remove and clean-install behaviour** (#24, #25, #26) needs real
  machines and real installs; no offline test can cover what they do to
  unrelated processes or a pristine environment.
* Until #43 merges, a `hoststate` `ADVISORY-FAIL` on the desk simply means
  *someone was typing*; it must never be reported as a pass.

When the last PR merges and the hardware gate + soak have been run and
recorded (response latency, maximum outage, missed recoveries,
process/thread/handle counts, memory, CPU, log growth), this file gets its
final section: the numbers. Until then, the honest status of "unattended" is:
**designed for, offline-verified in parts, hardware-unverified entirely.**