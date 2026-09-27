<#
.SYNOPSIS
Build the environment this repository was reviewed against, then prove it works.

.DESCRIPTION
One command for a fresh clone: the venv, the pinned Python dependencies, the vendored
panel library at the revision vendor/LOCK.txt names, the PresentMon binary, and then
three proofs that the result is the thing that was reviewed — `tools\env_check.py
--strict`, the offline selftests, and a headless render.

Each step verifies before it installs, so running this twice is free, and every step
that fails stops the script with the failing command's own output above it. Nothing here
installs a scheduled task, starts the app, or touches a device: the elevated autostart is
`tools\install_autostart.ps1`, deliberately separate and deliberately elevated, and this
script ends by saying so rather than by doing it.

The offline gate will report three SKIPs on a machine that has not vendored the library
and the fonts. That is the honest answer, not a pass, and the summary at the end says
which cases were skipped and why.

.EXAMPLE
  powershell -File tools\bootstrap.ps1                 # what the app needs to run

.EXAMPLE
  powershell -File tools\bootstrap.ps1 -WithLhm        # plus the LibreHardwareMonitor backend

.EXAMPLE
  powershell -File tools\bootstrap.ps1 -SkipTests      # install only
#>
[CmdletBinding()]
param(
    [switch]$WithLhm,
    [switch]$SkipVendor,
    [switch]$SkipPresentMon,
    [switch]$SkipTests
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
. (Join-Path $PSScriptRoot 'ps_common.ps1')

$venvDir = Join-Path $root '.venv'
$venvPy = Join-Path $venvDir 'Scripts\python.exe'
$manifest = if ($WithLhm) { 'requirements-lhm.txt' } else { 'requirements.txt' }
$notes = @()
# The host that is running this script, by absolute path: `powershell` is not on every
# PATH, and on pwsh 7 there is no powershell.exe at all.
$hostExe = (Get-Process -Id $PID).Path

function Invoke-ToolScript {
    param([string]$Script, [string[]]$ScriptArgs = @(), [switch]$Test)
    $argv = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
              (Join-Path $PSScriptRoot $Script)) + $ScriptArgs
    if ($Test) { return Test-Step -Exe $hostExe -ToolArgs $argv }
    Invoke-Step -Exe $hostExe -ToolArgs $argv
}

try {
    Write-Step 'interpreter'
    # The venv is what we are building, so PATH is the only place to look this once.
    $boot = Get-Command python -ErrorAction SilentlyContinue
    if (-not $boot) {
        throw ('no python on PATH. Install Python 3.10 or newer (3.12 is what the ' +
               'offline gate runs) and re-run this script.')
    }
    # The floor is real, not caution: several modules write `str | None` without
    # `from __future__ import annotations`, so the union is evaluated at import time.
    # The number lives in env_check.py, and asking that is also the first thing this
    # script does with the interpreter it just found - if it cannot even run a stdlib
    # script, everything below would only have produced a broken venv.
    Invoke-Step -Exe $boot.Source -ToolArgs @(
        (Join-Path $PSScriptRoot 'env_check.py'), '--check-python')
    Write-Host "using $($boot.Source)"
    if (-not (Test-Path $venvPy)) {
        Write-Step 'venv'
        Invoke-Step -Exe $boot.Source -ToolArgs @('-m', 'venv', $venvDir)
    } else {
        Write-Host "`n== venv already present: $venvDir" -ForegroundColor Cyan
    }

    Write-Step "python dependencies ($manifest)"
    Invoke-Step -Exe $venvPy -ToolArgs @('-m', 'pip', 'install', '--quiet',
                                         '--upgrade', 'pip')
    Invoke-Step -Exe $venvPy -ToolArgs @('-m', 'pip', 'install', '-r', $manifest)

    if (-not $SkipVendor) {
        Write-Step 'vendored panel library'
        # Verify first: a tree that already matches the pin is left completely alone,
        # and `-Fetch` would refuse to replace it anyway.
        if (Invoke-ToolScript -Script 'vendor_lock.ps1' -ScriptArgs @('-Verify') -Test) {
            Write-Host 'vendor/LOCK.txt already satisfied; nothing fetched'
        } else {
            Invoke-ToolScript -Script 'vendor_lock.ps1' -ScriptArgs @('-Fetch')
        }
    } else {
        $notes += 'vendor fetch skipped (-SkipVendor): the library and theme fonts are ' +
                  'absent, so three offline cases will report SKIP'
    }

    if (-not $SkipPresentMon) {
        Write-Step 'PresentMon (frame stats)'
        $pm = Join-Path $root 'vendor\presentmon\presentmon.exe'
        if (Test-Path $pm) {
            Invoke-ToolScript -Script 'fetch_presentmon.ps1' -ScriptArgs @('-Verify')
        } else {
            Invoke-ToolScript -Script 'fetch_presentmon.ps1'
        }
    } else {
        $notes += 'PresentMon skipped (-SkipPresentMon): frame stats stay off'
    }

    Write-Step 'environment'
    # --strict says "everything the elevated task exists for is here". That is only a
    # fair question when -WithLhm was asked for: the default install is the app plus the
    # offline tests, and a missing pythonnet there is a warning about a backend that
    # will not load, not a failed install.
    $strict = if ($WithLhm) { @('--strict') } else { @() }
    Invoke-Step -Exe $venvPy -ToolArgs (@('tools\env_check.py', '--root', $root,
                                          '--manifest') + $strict)

    if (-not $SkipTests) {
        Write-Step 'offline selftests'
        Invoke-Step -Exe $venvPy -ToolArgs @('tools\run_offline_tests.py')

        Write-Step 'headless render (no panel, no sensors)'
        # `--dump` never opens a COM port and `--backend demo` never reads a sensor, so
        # this is the one end-to-end run that is safe to do unasked: it proves the
        # vendored library imports, the fonts load, and a frame is produced.
        $png = Join-Path ([IO.Path]::GetTempPath()) 'pcmonitor-bootstrap.png'
        Remove-Item $png -ErrorAction SilentlyContinue
        Invoke-Step -Exe $venvPy -ToolArgs @('main.py', '--backend', 'demo', '--dump',
                                             $png, '--frames', '2')
        if (-not (Test-Path $png)) { throw "the smoke render did not write $png" }
        Write-Host "wrote a rendered frame to $png ($((Get-Item $png).Length) bytes)"
        Remove-Item $png -Force
    }

    Write-Step 'done'
    foreach ($n in $notes) { Write-Host "  note: $n" }
    Write-Host @'
  Installed and verified. Not done, on purpose:
    * the scheduled task that runs the app at logon with Administrator - that is
      tools\install_autostart.ps1, it needs elevation, and it stops any running
      instance because the panel owns one COM port;
    * starting the app itself. Run `python main.py --dump frame.png` to look at a
      frame without touching the panel, or `python main.py` to drive it.
'@
} catch {
    Write-Host "`nBOOTSTRAP FAILED: $($_.Exception.Message)" -ForegroundColor Red
    foreach ($n in $notes) { Write-Host "  note: $n" }
    Write-Host ('Nothing above this line that already succeeded needs redoing: every ' +
                'step verifies before it installs.')
    exit 1
}