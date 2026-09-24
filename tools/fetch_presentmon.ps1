# Pin + fetch GameTechDev PresentMon (the ETW present-stream collector used by
# app/frames.py). Mirrors tools/vendor_lock.ps1's pin-and-verify pattern.
#
#   powershell -File tools/fetch_presentmon.ps1          # download + write vendor/presentmon/LOCK.txt
#   powershell -File tools/fetch_presentmon.ps1 -Verify  # fail if the binary drifted
#
# Provenance: official GitHub release, single self-contained x64 binary.
# License: MIT (https://github.com/GameTechDev/PresentMon/blob/main/LICENSE.txt)
param(
    [switch]$Verify,
    [string]$Version = "2.5.1",
    [string]$Sha256  = "9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191"  # 2.5.1 x64, from the release API digest
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$dir  = Join-Path $root "vendor\presentmon"
$exe  = Join-Path $dir "presentmon.exe"
$lock = Join-Path $dir "LOCK.txt"

if ($Verify) {
    if (-not (Test-Path $exe))  { throw "no $exe - run: tools/fetch_presentmon.ps1" }
    if (-not (Test-Path $lock)) { throw "no $lock - run: tools/fetch_presentmon.ps1" }
    $line = Get-Content $lock | Where-Object { $_.Trim() -and $_ -notmatch '^\s*#' } |
            Select-Object -First 1
    if (-not $line) { throw "no hash line in $lock" }
    $want = ($line.Trim() -split '\s+')[0]
    $have = (Get-FileHash $exe -Algorithm SHA256).Hash.ToLower()
    if ($want.ToLower() -ne $have) { "FAIL drift: $exe"; exit 1 }
    "ok - presentmon matches vendor/presentmon/LOCK.txt"
    exit 0
}

$tag  = "v$Version"
$name = "PresentMon-$Version-x64.exe"
$url  = "https://github.com/GameTechDev/PresentMon/releases/download/$tag/$name"

New-Item -ItemType Directory -Force $dir | Out-Null
$tmp = "$exe.download"
Write-Host "fetching $url"
Invoke-WebRequest -Uri $url -OutFile $tmp -UseBasicParsing
$got = (Get-FileHash $tmp -Algorithm SHA256).Hash.ToLower()
if ($got -ne $Sha256.ToLower()) {
    Remove-Item $tmp -Force
    throw "sha256 mismatch: got $got want $Sha256 (release tampered or version changed? pass -Sha256)"
}
Move-Item -Force $tmp $exe
$lockLines = @(
    "# sha256 of vendor/presentmon/presentmon.exe (GameTechDev PresentMon $tag, MIT)",
    "# provenance: $url",
    "# regenerate: tools/fetch_presentmon.ps1    check: tools/fetch_presentmon.ps1 -Verify",
    ("{0}  presentmon.exe" -f $got)
)
($lockLines -join "`r`n") + "`r`n" | Set-Content -Path $lock -Encoding utf8 -NoNewline
"wrote $exe + $lock"
