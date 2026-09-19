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

Everything is imported as a library and **never modified** — see
`vendor/turing-smart-screen-python/AGENTS.md` for the upstream change policy.

## Provenance (verified July 2026)

The copy is upstream `main` with CRLF line endings, unmodified otherwise. That is
provable from sizes alone, because blob hashes differ only by those line endings:

| file | local bytes | CRLFs | local − CRLFs | upstream `main` |
|---|---|---|---|---|
| `library/lcd/lcd_comm.py` | 33309 | 756 | **32553** | 32553 |
| `library/lcd/lcd_comm_turing_usb.py` | 38003 | 1003 | **37000** | 37000 |
| `library/lcd/serialize.py` | 2414 | 74 | **2340** | 2340 |

(Upstream sizes from the GitHub contents API for `library/lcd` on `main`.)

## Re-fetching after a fresh clone

    powershell -File tools\vendor_lock.ps1 -Fetch    # sparse clone of just what we import + write LOCK.txt
    powershell -File tools\vendor_lock.ps1 -Verify   # confirm the copy still matches the pin

Runs on Windows PowerShell 5.1 and pwsh 7. `-Fetch` uses a `--filter=blob:none
--sparse` clone limited to `library/` and the two font families, so the 1.05 GB
of theme artwork is never downloaded (`-NoPrune` gets you the full tree instead).
Upstream: <https://github.com/mathoudebine/turing-smart-screen-python>
(GPL-3.0-or-later — our app links to it as a library, keep that in mind before
relicensing this repo).
