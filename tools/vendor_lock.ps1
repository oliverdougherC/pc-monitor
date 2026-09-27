<#
.SYNOPSIS
Install, check, or deliberately re-pin the vendored turing-smart-screen-python copy.

.DESCRIPTION
The vendored library is not in this repository — upstream is 1.1 GB, nearly all of it
theme artwork this app never loads. What *is* here is `vendor/LOCK.txt`: one upstream
commit SHA and a sha256 for every file the app imports. That pair is the dependency,
and the three verbs below are the only ways to interact with it.

  -Fetch    install the revision the lock already names. It stages a checkout, verifies
            it against the committed lock, and only then moves it into place. It cannot
            write the lock, so it cannot decide that whatever it just downloaded is
            correct. If anything fails — the clone, the checkout, one byte of one file —
            the staging directory is deleted and the working copy and the lock are
            exactly as they were.

  -Verify   the same check, on the tree that is already there. Delegates to
            tools\env_check.py, so "is it right" has one answer in this repository.

  -Update   the only thing that writes vendor/LOCK.txt, and it needs -Sha <40 hex>.
            This is a maintainer moving a dependency, not a step in an install, and it
            prints every file that moved so the change is visible while it is being
            made. The new lock is written beside the real one and verified against the
            staged tree before either the tree or the lock is replaced, so a bad
            -Update cannot leave a newly accepted lock behind.

            There is deliberately no -Ref: a branch name cannot be recorded in a lock,
            because by the time the next person installs it points somewhere else. That
            was the old default, and the old -Fetch wrote the lock from whatever the
            branch happened to hold — which made the -Verify that followed a formality
            rather than a check.

The fetch is sparse: it takes the directories named by `tools\env_check.py
--print-scope` — the Python library, the theme fonts, and
`external/LibreHardwareMonitor`, whose DLL the LHM backend loads and which the old
sparse set left out, so a "clean" install could not run the backend it documented.

Runs on Windows PowerShell 5.1 and pwsh 7.

.EXAMPLE
  powershell -File tools\vendor_lock.ps1 -Fetch

.EXAMPLE
  powershell -File tools\vendor_lock.ps1 -Verify

.EXAMPLE
  # after deciding to move to a new upstream revision
  powershell -File tools\vendor_lock.ps1 -Update -Sha 2b33ab4f00a096916dd6a1174441a53a7ec33b03
#>
[CmdletBinding()]
param(
    [switch]$Verify,
    [switch]$Fetch,
    [switch]$Update,
    [string]$Sha = '',
    [switch]$NoPrune,
    [switch]$Force,
    [string]$Repo = 'https://github.com/mathoudebine/turing-smart-screen-python'
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$vendor = Join-Path $root 'vendor'
$lib = Join-Path $vendor 'turing-smart-screen-python'
$lock = Join-Path $vendor 'LOCK.txt'
$check = Join-Path $PSScriptRoot 'env_check.py'
. (Join-Path $PSScriptRoot 'ps_common.ps1')

function Invoke-Check {
    param([Parameter(Mandatory = $true)][string[]]$CheckArgs, [switch]$Quiet)
    Invoke-Step -Exe (Resolve-Python) -Quiet:$Quiet -ToolArgs (@($check) + $CheckArgs)
}

function Invoke-Git {
    param([string]$Dir, [Parameter(Mandatory = $true)][string[]]$GitArgs)
    if ($Dir) { $GitArgs = @('-C', $Dir) + $GitArgs }
    Invoke-Step -Exe 'git' -ToolArgs $GitArgs
}

function Get-PinnedCommit {
    if (-not (Test-Path $lock)) {
        throw "no $lock - this checkout does not name a dependency set to install"
    }
    $m = Select-String -Path $lock -Pattern '^\s*#\s*commit\s*:\s*(\S+)' |
        Select-Object -First 1
    if (-not $m) {
        throw "$lock names no upstream commit ('# commit: <40 hex>') - nothing to fetch"
    }
    $sha = $m.Matches[0].Groups[1].Value
    if ($sha -notmatch '^[0-9a-f]{40}$') {
        throw "$lock pins '$sha', which is not a commit SHA - re-record it with: " +
              'tools\vendor_lock.ps1 -Update -Sha <40 hex>'
    }
    $sha
}

function Test-IsLink($path) {
    # This development tree reaches the vendored library through a junction. Deleting
    # one recursively is not a guaranteed no-op across PowerShell versions, and what
    # sits on the other side is somebody's working copy, so refuse instead.
    $item = Get-Item $path -Force -ErrorAction SilentlyContinue
    if (-not $item) { return $false }
    [bool]($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint)
}

function Get-StagedTree($sha) {
    $stage = Join-Path $vendor (".staging-$PID")
    if (Test-Path $stage) { Remove-Item -Recurse -Force $stage }
    Write-Host "fetching $sha into $stage"
    Invoke-Git -GitArgs @('init', '-q', $stage) | Out-Null
    Invoke-Git -Dir $stage -GitArgs @('remote', 'add', 'origin', $Repo) | Out-Null
    # Line endings are pinned too, by switching the setting off: checkout then writes
    # the blob bytes, so the tree a fresh clone produces is the tree the lock hashes.
    # The old fetch left that to whatever core.autocrlf the machine happened to have,
    # which is precisely why it had to rewrite the lock afterwards.
    Invoke-Git -Dir $stage -GitArgs @('config', 'core.autocrlf', 'false') | Out-Null
    Invoke-Git -Dir $stage -GitArgs @('fetch', '-q', '--depth', '1',
                                      '--filter=blob:none', 'origin', $sha) | Out-Null
    if (-not $NoPrune) {
        # One definition of what the fetch takes, shared with what the lock is complete
        # about: a directory fetched but not locked is an unverified file in the runtime.
        $scope = (Invoke-Check -Quiet -CheckArgs @('--print-scope')) -join ' '
        Invoke-Git -Dir $stage -GitArgs ((@('sparse-checkout', 'set') +
                                          ($scope -split ' '))) | Out-Null
    }
    Invoke-Git -Dir $stage -GitArgs @('checkout', '-q', 'FETCH_HEAD') | Out-Null
    $stage
}

function Install-StagedTree($stage, $newlock) {
    if (Test-Path $lib) {
        if (Test-IsLink $lib) {
            throw "$lib is a link, not a copy - run this against a real checkout"
        }
        if (-not $Force) {
            throw "$lib already exists, and nothing was changed. Re-run with -Force " +
                  'now that the new revision has verified.'
        }
        Remove-Item -Recurse -Force $lib
    }
    # The installed tree is content, pinned by hash — not a nested git repository that
    # this project would then have to ignore twice.
    Remove-Item -Recurse -Force (Join-Path $stage '.git') -ErrorAction SilentlyContinue
    Move-Item -Path $stage -Destination $lib
    if ($newlock) { Move-Item -Force -Path $newlock -Destination $lock }
    Write-Host "installed $lib$(if ($newlock) { " and $lock" })"
}

try {
    if ($Verify) {
        Invoke-Check -CheckArgs @('--root', $root, '--only-vendor')
        exit 0
    }
    if (-not ($Fetch -or $Update)) {
        # The old default wrote the lock from whatever was already on disk. There is no
        # longer a verb for "decide the dependency by not naming one".
        throw ('nothing to do: -Fetch installs the pinned revision, -Verify checks the ' +
               'working copy, -Update -Sha <40 hex> re-pins (maintainers). See ' +
               'tools\vendor_lock.ps1 -?')
    }
    if ($Update) {
        if ($Sha -notmatch '^[0-9a-f]{40}$') {
            throw "-Update needs -Sha <40 hex>, got '$Sha'. Find one with: " +
                  "git ls-remote $Repo HEAD"
        }
        $sha = $Sha
    } else {
        $sha = Get-PinnedCommit
    }

    $stage = $null
    $newlock = $null
    $installed = $false
    try {
        $stage = Get-StagedTree $sha
        if ($Update) {
            # A candidate, beside the real one. The committed lock is not touched until
            # the tree and the record have both passed.
            $newlock = Join-Path $vendor (".LOCK-$PID.txt")
            Write-Host "recording $sha in a candidate lock"
            Invoke-Check -CheckArgs @('--root', $root, '--tree', $stage, '--lock',
                                      $newlock, '--write-lock', '--sha', $sha)
        }
        # Verify the staged tree before anything is replaced. On -Update the lock it is
        # verified against was just written from this same tree, and that is still worth
        # doing: env_check re-reads the file from disk and re-hashes the files, so a
        # hash that does not reproduce is caught here rather than on the next clone.
        $against = if ($newlock) { $newlock } else { $lock }
        Invoke-Check -CheckArgs @('--root', $root, '--tree', $stage,
                                  '--only-vendor', '--lock', $against)
        Install-StagedTree $stage $newlock
        $installed = $true
    } finally {
        foreach ($tmp in @($stage, $newlock)) {
            if ($tmp -and (Test-Path $tmp)) { Remove-Item -Recurse -Force $tmp }
        }
        if (-not $installed) {
            Write-Host 'nothing was installed: the vendored tree and lock are as they were'
        }
    }
} catch {
    Write-Host "FAIL $($_.Exception.Message)"
    exit 1
}