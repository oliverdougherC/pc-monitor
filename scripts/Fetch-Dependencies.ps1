[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$folder = Join-Path $root 'dependencies'
$target = Join-Path $folder 'presentmon.exe'
$expected = '9bec3083069f58f911e6a512f4806db51a27bd096103087bc1d05ef54c80a191'
New-Item -ItemType Directory -Path $folder -Force | Out-Null
if ((Test-Path -LiteralPath $target) -and (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash.ToLowerInvariant() -eq $expected) { Write-Output 'PresentMon 2.5.1 verified.'; return }
$staged = Join-Path $folder 'presentmon.download'
Invoke-WebRequest 'https://github.com/GameTechDev/PresentMon/releases/download/v2.5.1/PresentMon-2.5.1-x64.exe' -OutFile $staged
if ((Get-FileHash -LiteralPath $staged -Algorithm SHA256).Hash.ToLowerInvariant() -ne $expected) { throw 'PresentMon checksum mismatch; installation refused.' }
Move-Item -LiteralPath $staged -Destination $target -Force
Write-Output 'PresentMon 2.5.1 fetched and verified.'
