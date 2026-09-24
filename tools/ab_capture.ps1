<#
.SYNOPSIS
    One raw-capture cycle through the already-elevated scheduled task.

.DESCRIPTION
    Reading the present stream needs admin. The PCMonitor task already runs
    elevated, and Stop/Start-ScheduledTask work *without* elevation, so this tool
    can A/B capture setups with no UAC prompt at all: it rewrites the frames:
    block of config.yaml, restarts the task, lets it capture for -Seconds, then
    reports what the raw CSV actually contains.

    "Zero rows after N seconds of a game visibly rendering" is the answer to the
    question that matters -- the session receives no graphics events -- and it is
    not a buffering artifact: a row is ~200 bytes and the file buffer ~8 KB, so
    ~40 presents (about a second of gameplay) would already have landed.

.PARAMETER Out
    Raw capture file (written by presentmon itself), relative to the repo.

.PARAMETER Seconds
    Capture window before reporting.

.PARAMETER Exe
    presentmon binary name inside vendor\presentmon (default: the pinned one).

.PARAMETER Role
    ETW session role. Give it a value to get a brand-new session name instead of
    the take-over path, which is itself a suspect.

.PARAMETER ExcludeDropped
    false drops --exclude_dropped, i.e. counts presents even when the OS did not
    attribute a flip to the display. Accepts true/false/1/0: through -File,
    PowerShell passes arguments as strings, so a [bool] parameter type cannot be
    used here (it rejects "False" before the script ever sees it).

.PARAMETER Restore
    Put config.yaml back from the pristine backup and restart the app.

.EXAMPLE
    powershell -File tools\ab_capture.ps1 -Out vendor\presentmon\cap-1.csv -Role ab1
    powershell -File tools\ab_capture.ps1 -Out vendor\presentmon\cap-2.csv -Role ab2 -ExcludeDropped false
    powershell -File tools\ab_capture.ps1 -Out vendor\presentmon\cap-3.csv -Role ab3 -Exe presentmon-2.6.0.exe
    powershell -File tools\ab_capture.ps1 -Restore
#>
[CmdletBinding()]
param(
    [string]$Out,
    [int]$Seconds = 25,
    [string]$Exe,
    [string]$Role,
    [string]$ExcludeDropped,
    [string]$ExtraArgs,
    [switch]$Restore
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$cfgPath = Join-Path $root 'config.yaml'
$backup  = "$cfgPath.ab_bak"
$task    = 'PCMonitor'
$py      = Join-Path $root '.venv\Scripts\python.exe'
Set-Location $root
# This file is UTF-8 with arrows/em-dashes in the comments, and Windows
# PowerShell's default encoding would rewrite them as mojibake.
$utf8 = if ($PSVersionTable.PSVersion.Major -ge 6) { 'utf8NoBOM' } else { 'UTF8' }

function Restart-Task {
    Stop-ScheduledTask -TaskName $task -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 3
    Start-ScheduledTask -TaskName $task
}

function Read-Shared([string]$path) {
    if (-not (Test-Path $path)) { return $null }
    try {
        $fs = [System.IO.File]::Open($path, 'Open', 'Read', 'ReadWrite')
        try { $sr = New-Object System.IO.StreamReader($fs, [Text.Encoding]::UTF8)
              $t = $sr.ReadToEnd(); $sr.Dispose() }
        finally { $fs.Dispose() }
        return $t
    } catch { return ("unreadable: " + $_.Exception.Message) }
}

function Set-FramesKey([string[]]$lines, [string]$key, [string]$value) {
    # Rewrite (or append) one key inside the frames: block. The block ends at the
    # first line that is not indented -- never trust a key-name scan for that.
    $i = -1
    for ($k = 0; $k -lt $lines.Count; $k++) {
        if ($lines[$k] -match '^frames:\s*$') { $i = $k; break }
    }
    if ($i -lt 0) { throw "no 'frames:' block in $cfgPath" }
    $j = $i + 1
    while ($j -lt $lines.Count -and $lines[$j] -match '^\s+\S') {
        if ($lines[$j] -match ('^\s+' + [regex]::Escape($key) + '\s*:')) {
            $lines[$j] = "  {0}: {1}" -f $key, $value
            return , $lines
        }
        $j++
    }
    $out = New-Object System.Collections.Generic.List[string]
    for ($k = 0; $k -lt $j; $k++) { $out.Add($lines[$k]) }
    $out.Add("  {0}: {1}" -f $key, $value)
    for ($k = $j; $k -lt $lines.Count; $k++) { $out.Add($lines[$k]) }
    return , $out.ToArray()
}

if ($Restore) {
    if (-not (Test-Path $backup)) { throw "no backup at $backup" }
    Copy-Item $backup $cfgPath -Force
    Restart-Task
    "restored $cfgPath from $backup and restarted $task"
    exit 0
}

if (-not $Out) { throw "-Out is required (or use -Restore)" }
$outPath = if ([System.IO.Path]::IsPathRooted($Out)) { $Out } else { Join-Path $root $Out }
if (-not (Test-Path $backup)) { Copy-Item $cfgPath $backup }   # pristine, once
Remove-Item $outPath -Force -ErrorAction SilentlyContinue

# --- config for this run ------------------------------------------------------
$lines = @(Get-Content $cfgPath -Encoding $utf8)
$lines = Set-FramesKey $lines 'output_file' ('"{0}"' -f ($outPath -replace '\\', '/'))
if ($Exe)  { $lines = Set-FramesKey $lines 'path' ('"vendor/presentmon/{0}"' -f $Exe) }
if ($Role) { $lines = Set-FramesKey $lines 'role' $Role }
if ($PSBoundParameters.ContainsKey('ExtraArgs')) {
    $lines = Set-FramesKey $lines 'extra_args' $ExtraArgs
}
if ($PSBoundParameters.ContainsKey('ExcludeDropped')) {
    switch -regex ($ExcludeDropped.Trim().ToLower()) {
        '^(1|true|yes|on)$'  { $drop = 'true';  break }
        '^(0|false|no|off)$' { $drop = 'false'; break }
        default { throw "-ExcludeDropped wants true/false (or 1/0), got '$ExcludeDropped'" }
    }
    $lines = Set-FramesKey $lines 'exclude_dropped' $drop
}
Set-Content -Path $cfgPath -Value $lines -Encoding $utf8
try {
    & $py -c "from app import config as c; c.load()"
    if ($LASTEXITCODE -ne 0) { throw "config.yaml does not parse" }
} catch {
    Copy-Item $backup $cfgPath -Force
    throw "bad config.yaml - restored from $backup"
}
"this run:"
$block = $false
foreach ($l in $lines) {
    if ($l -match '^frames:\s*$') { $block = $true; continue }
    if ($block) {
        if ($l -notmatch '^\s+\S') { break }
        if ($l -match 'path:|role:|exclude_dropped:|output_file:') { "  " + $l.Trim() }
    }
}
"  capture -> $outPath"

# --- capture ------------------------------------------------------------------
Restart-Task
"capturing $Seconds s -- keep a game rendering..."
for ($t = 5; $t -le $Seconds; $t += 5) {
    Start-Sleep -Seconds 5
    $txt = Read-Shared $outPath
    $bytes = if (Test-Path $outPath) { (Get-Item $outPath).Length } else { 0 }
    $rows = 0
    if ($txt -and $txt.Length -gt 0 -and -not $txt.StartsWith('unreadable')) {
        $rows = @($txt -split "`r?`n" | Where-Object { $_ }).Count - 1
    }
    "  t=$t s  bytes=$bytes  rows=$rows"
}

# --- report -------------------------------------------------------------------
"=" * 70
$txt = Read-Shared $outPath
if (-not $txt) { "NO FILE at $outPath -- presentmon never created it"; exit 2 }
if ($txt.StartsWith('unreadable')) { "FILE LOCKED -- $txt"; exit 4 }
$rows = @($txt -split "`r?`n" | Where-Object { $_ -and $_.Contains(',') })
if ($rows.Count -eq 0) { "EMPTY FILE after $Seconds s of rendering -- session up, zero events"; exit 3 }
"HEADER: $($rows[0])"
"data  : $($rows.Count - 1) rows"
$cols = $rows[0].Split(',')
$appIdx = [Array]::IndexOf($cols, 'Application'); if ($appIdx -lt 0) { $appIdx = 0 }
$pidIdx = [Array]::IndexOf($cols, 'ProcessID');   if ($pidIdx -lt 0) { $pidIdx = 1 }
if ($rows.Count -gt 1) {
    "FIRST : $($rows[1])"
    $tally = @{}
    foreach ($r in $rows[1..($rows.Count - 1)]) {
        $p = $r.Split(',')
        if ($appIdx -lt $p.Count -and $pidIdx -lt $p.Count) {
            $k = "{0} pid={1}" -f $p[$appIdx], $p[$pidIdx]
            $tally[$k] = 1 + $tally[$k]
        }
    }
    "SOURCES:"
    $tally.GetEnumerator() | Sort-Object Value -Descending | Select-Object -First 8 |
        ForEach-Object { "  {0,-32} x{1}" -f $_.Key, $_.Value }
}
"---- app log tail ----"
Get-Content (Join-Path $root 'log.log') -Tail 8 -ErrorAction SilentlyContinue |
    ForEach-Object { "  " + $_ }
"restore with: powershell -File tools\ab_capture.ps1 -Restore"
