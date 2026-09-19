# pin the vendored turing-smart-screen-python copy (see vendor/README.md)
#
#   powershell -File tools/vendor_lock.ps1            # write vendor/LOCK.txt from the copy on disk
#   powershell -File tools/vendor_lock.ps1 -Verify    # fail if the copy drifted from the lock
#   powershell -File tools/vendor_lock.ps1 -Fetch     # clone upstream into vendor/, prune artwork, write lock
#
# Runs on Windows PowerShell 5.1 and pwsh 7. Tracked set: the library we import
# plus the three fonts the layout renders with.
param(
    [switch]$Verify,
    [switch]$Fetch,
    [switch]$NoPrune,
    [string]$Repo = "https://github.com/mathoudebine/turing-smart-screen-python",
    [string]$Ref = "main"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root "vendor"
$lib = Join-Path $vendor "turing-smart-screen-python"
$lock = Join-Path $vendor "LOCK.txt"

$fonts = @(
    "res/fonts/jetbrains-mono/JetBrainsMono-ExtraBold.ttf",
    "res/fonts/roboto/Roboto-Bold.ttf",
    "res/fonts/roboto/Roboto-Medium.ttf"
)

if ($Fetch) {
    if (Test-Path (Join-Path $lib ".git")) { throw "vendor/ already holds a git clone: $lib" }
    if ($NoPrune) {
        git clone --depth 1 --branch $Ref $Repo $lib
    } else {
        # ~1.1 GB of theme artwork/tests we never import - skip the download entirely
        git clone --depth 1 --filter=blob:none --sparse --branch $Ref $Repo $lib
        Push-Location $lib
        git sparse-checkout set library res/fonts/jetbrains-mono res/fonts/roboto
        Pop-Location
    }
}

if (-not (Test-Path $lib)) { throw "no vendored copy at $lib - run: tools/vendor_lock.ps1 -Fetch" }

$files = @(Get-ChildItem -Recurse -File (Join-Path $lib "library") -Filter *.py |
    ForEach-Object { $_.FullName.Substring($lib.Length + 1).Replace('\', '/') }) + $fonts
$entries = foreach ($rel in ($files | Sort-Object)) {
    $full = Join-Path $lib $rel
    if (-not (Test-Path $full)) { throw "expected vendored file is missing: $rel" }
    "{0}  {1}" -f (Get-FileHash $full -Algorithm SHA256).Hash.ToLower(), $rel
}

if ($Verify) {
    if (-not (Test-Path $lock)) { throw "no $lock - run: tools/vendor_lock.ps1" }
    $want = @{}
    Get-Content $lock | Where-Object { $_ -and $_ -notmatch '^#' } | ForEach-Object {
        $h, $f = $_ -split '\s+', 2; if ($f) { $want[$f.Trim()] = $h.Trim() }
    }
    $have = @{}
    foreach ($e in $entries) { $h, $f = $e -split '\s+', 2; $have[$f] = $h }
    $bad = @()
    foreach ($f in $have.Keys) {
        if (-not $want.ContainsKey($f)) { $bad += "new file not in lock: $f" }
        elseif ($want[$f] -ne $have[$f]) { $bad += "drift: $f" }
    }
    foreach ($f in $want.Keys) { if (-not $have.ContainsKey($f)) { $bad += "missing: $f" } }
    if ($bad) { $bad | Sort-Object | ForEach-Object { "FAIL $_" }; exit 1 }
    "ok - vendored library matches vendor/LOCK.txt ($($want.Count) files)"
    exit 0
}

Set-Content -Path $lock -Encoding utf8 -Value (
    @("# sha256 of the vendored turing-smart-screen-python files we import",
      "# regenerate: tools/vendor_lock.ps1    check: tools/vendor_lock.ps1 -Verify") + $entries)
"wrote $lock ($($entries.Count) files)"
