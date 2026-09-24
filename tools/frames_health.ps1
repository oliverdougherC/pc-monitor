<#
.SYNOPSIS
    Is the frame capture working right now, and if not, which kind of not?

.DESCRIPTION
    No admin, no assumptions: it reads the app's own log and says which of the
    four states the present-stream capture is in, because they all look the same
    on the panel (fps shows `--`):

      LIVE     rows are arriving - fps belongs on the panel
      STARVED  the ETW session is up and receives nothing - machine-level, the
               README's "When the present stream is silent" section is the trail
      DENIED   the child could not start a trace session - not elevated
      QUIET    the app has started but the capture has not said anything yet

    Also lists the presentmon processes: one per ETW session. A process whose CPU
    does not move while a game is rendering is a starved session, and leftovers
    from earlier runs are visible here too (they hold a session name each, and a
    reboot is the only thing that clears an elevated one).

.EXAMPLE
    powershell -File tools\frames_health.ps1
    powershell -File tools\frames_health.ps1 -Log log.log
#>
[CmdletBinding()]
param(
    [string]$Log = 'log.log',
    [int]$Bytes = 131072
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$path = Join-Path $root $Log
if (-not (Test-Path $path)) { throw "no log at $path - has the app run at all?" }

# The app appends to this file while it runs, so open it for shared read and
# look only at the end: the verdict is about the most recent start.
$fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
try {
    [void]$fs.Seek([Math]::Max(0, $fs.Length - $Bytes), 'Begin')
    $sr = New-Object System.IO.StreamReader($fs)
    $text = $sr.ReadToEnd()
    $sr.Dispose()
} finally { $fs.Dispose() }
$lines = @($text -split "`r?`n" | Where-Object { $_ })

function FindLast([string]$pattern) {
    $hits = $lines | Select-String -Pattern $pattern
    if ($hits) { $hits[-1] } else { $null }
}

$started = FindLast '\[start\]'
$live    = FindLast 'session live'
$starved = FindLast 'no rows from presentmon'
$denied  = FindLast 'access denied|failed to start trace session'
$shown   = FindLast 'frames=(\d|no)'

$verdict = 'QUIET'
$note    = 'the capture has not reported anything since the last start'
if (-not $started) {
    $verdict = 'QUIET'; $note = 'no [start] banner in the window read - the app is not running, or the log rolled'
} elseif ($denied -and (-not $live -or $denied.LineNumber -gt $live.LineNumber)) {
    $verdict = 'DENIED'; $note = 'presentmon could not start a trace session - run the app elevated'
} elseif ($starved -and (-not $live -or $starved.LineNumber -gt $live.LineNumber)) {
    $verdict = 'STARVED'; $note = 'session up, zero rows - the machine is not delivering graphics events (see README)'
} elseif ($live) {
    $verdict = 'LIVE'; $note = 'presents are flowing'
}

"frames verdict : $verdict  ($note)"
""
"last start     : " + $(if ($started) { $started.Line.Trim() } else { '(none)' })
"last frames    : " + $(if ($live)    { $live.Line.Trim() }    else { '(none)' })
"               " + $(if ($starved)  { $starved.Line.Trim() } else { '' })
"last state     : " + $(if ($shown)   { $shown.Line.Trim() }   else { '(none)' })
""
"--- capture processes (CPU should climb while a game renders) ---"
$procs = Get-Process presentmon -ErrorAction SilentlyContinue
if ($procs) {
    $procs | Select-Object Id, ParentProcessId, CPU, StartTime | Format-Table -AutoSize | Out-String | ForEach-Object { $_.TrimEnd() }
    if ($procs.Count -gt 1) {
        "  note: $($procs.Count) capture processes - more than one ETW session is alive."
    }
} else {
    "  none running - the capture is not even started (frames disabled, or the app is down)"
}
if ($verdict -eq 'STARVED') {
    ""
    "next: tools/frames_probe.py (elevated) for what a fresh session sees,"
    "      tools/frames_selftest.py (no admin) to prove the parser is fine."
}
if ($verdict -eq 'QUIET') {
    ""
    "note: silence only means something while something is rendering - open a"
    "      game, wait ~25 s, and run this again (the app stays quiet on a still"
    "      desktop on purpose, because DWM presents nothing then)."
}
