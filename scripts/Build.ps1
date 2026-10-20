[CmdletBinding()]
param([string]$Dotnet = 'dotnet', [switch]$Publish)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if ($Dotnet -eq 'dotnet') {
    $privateSdk = Join-Path $env:LOCALAPPDATA 'PCMonitorBuild/dotnet/dotnet.exe'
    if (Test-Path -LiteralPath $privateSdk) { $Dotnet = $privateSdk }
}
Push-Location $root
try {
    & (Join-Path $PSScriptRoot 'Fetch-Dependencies.ps1')
    & $Dotnet restore PCMonitor.slnx --locked-mode
    if ($LASTEXITCODE -ne 0) { throw 'Restore failed.' }
    & $Dotnet build PCMonitor.slnx -c Release --no-restore -warnaserror
    if ($LASTEXITCODE -ne 0) { throw 'Build failed.' }
    & $Dotnet (Join-Path $root 'tests/PCMonitor.Tests/bin/Release/net10.0-windows/PCMonitor.Tests.dll')
    if ($LASTEXITCODE -ne 0) { throw 'Regression tests failed.' }
    if ($Publish) {
        & $Dotnet publish src/PCMonitor/PCMonitor.csproj -c Release -r win-x64 --self-contained true -p:PublishSingleFile=false -o .artifacts/publish
        if ($LASTEXITCODE -ne 0) { throw 'Publish failed.' }
        Copy-Item -LiteralPath (Join-Path $root 'docs') -Destination (Join-Path $root '.artifacts/publish') -Recurse -Force
        Copy-Item -LiteralPath (Join-Path $root 'LICENSE'),(Join-Path $root 'THIRD-PARTY-NOTICES.md'),(Join-Path $root 'README.md') -Destination (Join-Path $root '.artifacts/publish')
    }
} finally { Pop-Location }
