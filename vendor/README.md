# Vendored: turing-smart-screen-python (upstream copy, NOT tracked by git)

This directory is ignored by git on purpose. The upstream tree is **1.1 GB** —
1.05 GB of it is `res/themes/--Theme examples/` (full-size theme artwork, emoji
and CJK fonts) that this project never loads. Committing it would make the repo
unusable to save 20 files we cannot edit anyway.

## What we use from it

| path | why |
|---|---|
| `library/lcd/lcd_comm.py` | `Orientation`, base driver API (`main.py: make_lcd`) |
| `library/lcd/lcd_simulated.py` | the `SIMU` preview panel |
| `library/lcd/lcd_comm_rev_{a,b,c,d}.py`, `lcd_comm_turing_usb.py`, `lcd_comm_weact_{a,b}.py` | real hardware, selected by `display.revision` |
| `library/lcd/{color,serialize}.py` | pulled in by those drivers |
| `res/fonts/jetbrains-mono/JetBrainsMono-ExtraBold.ttf` | `layout.font_value` (tabular digits) |
| `res/fonts/roboto/Roboto-Bold.ttf`, `Roboto-Medium.ttf` | `layout.font_label`, `layout.font_small` |
| `external/LibreHardwareMonitor/*.dll` | loaded by `app/sensors/lhm.py` through pythonnet; `LibreHardwareMonitorLib.dll` is the one it names, the rest are its own dependencies |

Everything is imported as a library and **never modified** — see
`vendor/turing-smart-screen-python/AGENTS.md` for the upstream change policy.

## Provenance

`LOCK.txt` is the dependency: one upstream commit, and a sha256 for every file above.

```
# commit:   2b33ab4f00a096916dd6a1174441a53a7ec33b03
```

That revision was picked because it is what this desk already had, and the pin was
verified against it rather than asserted: every locked file hashes to the same git blob
as `2b33ab4`'s tree, once CRLF is folded to LF in the text files (the copy on a Windows
desk has CRLF line endings; upstream does not, and `git check-attr text` treats them as
the same content — which is the same rule this repository's own `.gitattributes`
applies, and the reason the hashes in `LOCK.txt` do not depend on whoever checked the
tree out, or on their `core.autocrlf`).

## Re-fetching after a fresh clone

    powershell -File tools\vendor_lock.ps1 -Fetch    # install the revision LOCK.txt names
    powershell -File tools\vendor_lock.ps1 -Verify   # confirm the copy still matches it

Runs on Windows PowerShell 5.1 and pwsh 7. `-Fetch` stages a sparse checkout of the
pinned commit — `library/`, the two font families, and `external/LibreHardwareMonitor`,
so the 1.05 GB of theme artwork is never downloaded — verifies it against `LOCK.txt`,
and only then moves it into place. A failure anywhere in that leaves the previous tree
and lock untouched. `tools\bootstrap.ps1` runs it as one step of a whole install.

`-Fetch` cannot write `LOCK.txt`. The only thing that does is

    powershell -File tools\vendor_lock.ps1 -Update -Sha <40 hex>

which is a maintainer deciding to move a dependency, prints every file that moved, and
refuses a branch name: a lock that says `main` describes whatever upstream heads to the
next time someone installs, and the `-Verify` that follows it stops meaning anything.

Upstream: <https://github.com/mathoudebine/turing-smart-screen-python>
(GPL-3.0-or-later — our app links to it as a library, keep that in mind before
relicensing this repo).
