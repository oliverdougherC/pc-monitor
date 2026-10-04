<#
.SYNOPSIS
Restart the panel app when its own heartbeat stops: the observer it cannot be for itself.

.DESCRIPTION
The app's [beat] line is written by the very loop whose life it is meant to prove, so a
hung loop is invisible from the inside. The last line stays the last line forever and
log.log says nothing wrong, which is the same shape as a healthy quiet night. The task's
own restart-on-failure policy cannot see it either: a wedged process has not failed, it
has just stopped making progress, and ExecutionTimeLimit is deliberately unlimited
because this task is meant to run for weeks.

So this is a second process on its own schedule. It decides nothing on its own:
`python -m app.liveness decide` answers with one word (ok / hold / backoff / permanent /
restart / start) plus a reason, and this script only acts on that word. What counts as
stalled, how many restarts fit in an hour, how long to back off, and when to give up
entirely all live in app/liveness.py against files on disk, which is what makes the
policy testable offline instead of by waiting for a hang that never reproduces.

Two rules this script does keep to itself:

  * It touches the task, never a process. No image-name match, no command-line match:
    it stops and starts the PCMonitor task by name, which is the only thing this project
    owns here. (Issue #25 is about why killing python.exe because its command line
    mentions main.py is not acceptable; a watchdog must not become a second offender.)
  * `hold` and `permanent` both mean "do nothing", and both are still written down. A
    watchdog that quietly stops trying is indistinguishable from one that is not needed,
    and the difference is the whole reason to read the file.

Register it with tools/install_autostart.ps1, which also unregisters it again.
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'PCMonitor',
    [string]$LogName  = 'watchdog.log'
)

$ErrorActionPreference = 'Stop'

$Root = Split-Path -Parent $PSScriptRoot
$Py = Join-Path $Root '.venv\Scripts\python.exe'
$Log = Join-Path $Root $LogName

function Write-Watchdog([string]$line) {
    # Only incidents land in the file, so it stays short enough to read in one go.
    # Bounded the blunt way: one generation of history is enough to answer "when did
    # this start happening", and a log that grows forever is its own outage.
    try {
        if ((Test-Path $Log) -and ((Get-Item $Log).Length -gt 262144)) {
            Move-Item -Force $Log "$Log.old"
        }
        Add-Content -Path $Log -Value ("{0} {1}" -f (Get-Date -Format 's'), $line)
    } catch {
        # Nowhere left to report a logging failure to. Deliberately silent, and the
        # reason is on the line above: this is not allowed to be the thing that fails.
    }
}

# Fast path: the two branches of the app/liveness.decide() policy that are pure
# functions of the on-disk epochs, evaluated without starting Python. The task's
# action is a console program running in the user session, so Task Scheduler gives it
# a visible console, and the venv interpreter (about two seconds to start and answer)
# is what makes the flash last long enough to see. Both branches are mirrored exactly:
#   * a readable .stopped at least as new as the last readable .heartbeat (or no
#     readable beat at all) is `hold` - liveness._policy's first branch, which both
#     the stale-beat and no-beat paths face before the budget, the backoff and the
#     pid are even consulted;
#   * a beat at most STALL_S (600 s in app/liveness.py) old outranks a stop marker:
#     with no marker, or with a marker older than the beat, the verdict is `ok` -
#     the common case, which the observer serves with no line and no action.
# Everything else - a stale beat with no stop marker at least as new as it, the
# restart budget, the backoff, the pid liveness check - falls through to the full
# policy below rather than trusting itself. Any missing or unparseable file the same
# way liveness would: an unreadable beat counts as no beat, an unreadable marker as
# no marker, and the branch that needs the other side's exact value is simply not
# taken.
try {
    $stopEpoch = $null
    $stopPath = Join-Path $Root '.stopped'
    if (Test-Path $stopPath) {
        $stopTok = (Get-Content -Path $stopPath -First 1).Trim().Split(' ')[0]
        $stopCand = 0.0
        if ([double]::TryParse($stopTok, [ref]$stopCand)) { $stopEpoch = $stopCand }
    }
    $beatEpoch = $null
    $beatPath = Join-Path $Root '.heartbeat'
    if (Test-Path $beatPath) {
        $beatTok = (Get-Content -Path $beatPath -First 1).Trim().Split(' ')[0]
        $beatCand = 0.0
        if ([double]::TryParse($beatTok, [ref]$beatCand)) { $beatEpoch = $beatCand }
    }
    if ($beatEpoch -ne $null) {
        # liveness clamps the beat age to zero; a negative age (future timestamp)
        # still satisfies the same comparison, so the raw difference is used.
        # UtcNow is load-bearing: the epochs are time.time() values, and a
        # local-time subtraction is silently wrong by 2x the zone offset
        # (a hung app would keep looking "fresh" on a non-UTC machine).
        if ([DateTimeOffset]::UtcNow.ToUnixTimeSeconds() - $beatEpoch -le 600) {
            if ($stopEpoch -eq $null -or $stopEpoch -lt $beatEpoch) {
                return    # `ok` - the common case: no line, no action
            }
            Write-Watchdog "hold: fast path - .stopped is at least as new as the last beat; recovery must not undo that"
            return
        }
        if ($stopEpoch -ne $null -and $stopEpoch -ge $beatEpoch) {
            Write-Watchdog "hold: fast path - .stopped is at least as new as the last beat; recovery must not undo that"
            return
        }
        # A stale beat with no stop marker at least as new: the budget, the backoff
        # and the pid are Python's to judge.
    }
    elseif ($stopEpoch -ne $null) {
        Write-Watchdog "hold: fast path - .stopped with no readable heartbeat; recovery must not undo that"
        return
    }
} catch {
    # Unreadable state is no reason to trust the fast path; the full policy decides.
}

if (-not (Test-Path $Py)) {
    Write-Watchdog "no interpreter at $Py - doing nothing"
    return
}

if (-not (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue)) {
    # The app is not installed (or was removed). Its heartbeat going stale is the
    # expected answer, and restarting it would be the watchdog undoing an uninstall.
    return
}

try {
    $answer = @(& $Py -m app.liveness decide 2>&1 | ForEach-Object { "$_" })
} catch {
    Write-Watchdog "could not ask app.liveness ($($_.Exception.Message)) - doing nothing"
    return
}

if ($answer.Count -lt 1) {
    Write-Watchdog "app.liveness said nothing - doing nothing"
    return
}

$verdict = $answer[0].Trim().ToLower()
$reason = if ($answer.Count -gt 1) { ($answer | Select-Object -Skip 1) -join ' ' } else { '' }

switch ($verdict) {
    'ok' { return }                       # the common case: no line, no action
    'hold' {
        Write-Watchdog "hold: $reason"
        return
    }
    'backoff' {
        Write-Watchdog "backoff: $reason"
        return
    }
    'permanent' {
        Write-Watchdog "PERMANENT: $reason"
        return
    }
    { $_ -eq 'restart' -or $_ -eq 'start' } {
        Write-Watchdog "$verdict : $reason"
        # Stop first: 'restart' means something is wedged behind a stale beat, and
        # Start-ScheduledTask on a task Windows still believes is running would do
        # nothing at all (the task settings say IgnoreNew).
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
        Start-ScheduledTask -TaskName $TaskName
        Write-Watchdog "$verdict issued for task $TaskName"
        return
    }
    default {
        Write-Watchdog "unknown verdict '$verdict' - doing nothing"
    }
}
