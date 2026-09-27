# Shared plumbing for the setup scripts (`vendor_lock.ps1`, `bootstrap.ps1`).
#
#   . (Join-Path $PSScriptRoot 'ps_common.ps1')
#
# The one thing worth sharing is how a native command is allowed to fail. PowerShell
# does not honour $ErrorActionPreference for `git`, `pip` or `python`: a non-zero exit
# sets $LASTEXITCODE and execution continues, and with the preference at Stop, `2>&1`
# on Windows PowerShell 5.1 turns the first stderr line into a terminating
# NativeCommandError before the exit code is ever read. Both halves of that behaved
# badly in `vendor_lock.ps1`, where a failed `git fetch` walked on into the step that
# wrote vendor/LOCK.txt — so the script that was supposed to record a known-good
# dependency recorded a half-fetched one instead. Every native call in this repository
# goes through Invoke-Step, which relaxes the preference around the call, reads the
# exit code, and raises the failure itself.

$ErrorActionPreference = 'Stop'
$script:PsCommonRoot = Split-Path -Parent $PSScriptRoot
$script:PythonExe = $null

function Resolve-Python {
    <#
      The venv if there is one, the interpreter on PATH otherwise. The setup scripts are
      what create the venv, so PATH has to be an acceptable answer at least once.
    #>
    param([switch]$Quiet)
    if ($script:PythonExe) { return $script:PythonExe }
    $venv = Join-Path $script:PsCommonRoot '.venv\Scripts\python.exe'
    if (Test-Path $venv) { $script:PythonExe = $venv; return $script:PythonExe }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd) { $script:PythonExe = $cmd.Source; return $script:PythonExe }
    if ($Quiet) { return $null }
    throw 'no python.exe found: create the venv with tools\bootstrap.ps1, or put python on PATH'
}

function Invoke-Step {
    <#
      Run a native command, echo what it said, and turn a non-zero exit into a real
      terminating error. Arguments arrive as one array rather than as loose parameters
      because they are things like `--filter=blob:none`, and PowerShell reads a bare
      token starting with `-` as a parameter name of *this* function.
    #>
    param(
        [Parameter(Mandatory = $true)][string]$Exe,
        [string[]]$ToolArgs = @(),
        [switch]$Quiet
    )
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = & $Exe @ToolArgs 2>&1
        $code = $LASTEXITCODE
    } finally { $ErrorActionPreference = $prev }
    $text = ($out | ForEach-Object { "$_" }) -join "`n"
    if ($text -and -not $Quiet) { Write-Host $text }
    if ($code -ne 0) {
        if ($text) { Write-Host $text }
        throw "$Exe $($ToolArgs -join ' ') exited $code"
    }
    # A step reports to the console; only a quiet step hands its text back. Returning it
    # always means every line is printed twice - once here, once again when the caller
    # that has no use for it lets the value escape into the script's output.
    if ($Quiet) { $out }
}

function Test-Step {
    <#
      The same call, but a non-zero exit is an answer instead of a failure: "does the
      tree already verify?" is a question you ask before deciding to fetch.
    #>
    param([Parameter(Mandatory = $true)][string]$Exe, [string[]]$ToolArgs = @())
    try {
        Invoke-Step -Exe $Exe -ToolArgs $ToolArgs -Quiet | Out-Null
        $true
    } catch {
        $false
    }
}

function Write-Step {
    param([string]$Text)
    Write-Host "`n== $Text" -ForegroundColor Cyan
}