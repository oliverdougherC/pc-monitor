# Live USB plug test: prints every device that appears/disappears, with timestamps.
#
#   powershell -File tools\plug_watch.ps1                 # 300 s
#   powershell -File tools\plug_watch.ps1 -Seconds 120
#   (run it as .\tools\plug_watch.ps1 from a shell, or via powershell.exe - the
#    script needs no PowerShell 7, and pwsh.exe is not installed here)
#
# Use this together with tools/usb_probe.py --watch: the Python side sees what
# libusb can talk to (i.e. what our TUR_USB driver can find), this side sees what
# Windows enumerated at all -- including a device that failed enumeration, which
# shows up as "Unknown USB Device" and never reaches libusb. Together they tell
# apart: nothing on the wire (charge-only cable/port), enumeration failure
# (bad/under-powered device), and driver-binding failure (Windows sees it, libusb
# cannot open it).
param(
    [int]$Seconds = 300,
    [int]$IntervalMs = 1000
)

function Get-Snapshot {
    $all = @{}
    foreach ($d in Get-PnpDevice -PresentOnly -ErrorAction SilentlyContinue) {
        if ($d.InstanceId -notmatch '^USB(\\|4\\)') { continue }
        $all[$d.InstanceId] = "$($d.Status) $($d.Class) $($d.FriendlyName)"
    }
    return $all
}

$stamp = { (Get-Date).ToString('HH:mm:ss.fff') }
Write-Output "[$(& $stamp)] watching USB for $Seconds s - unplug/replug the device now"
$before = Get-Snapshot
$deadline = (Get-Date).AddSeconds($Seconds)

while ((Get-Date) -lt $deadline) {
    Start-Sleep -Milliseconds $IntervalMs
    $now = Get-Snapshot
    foreach ($k in $now.Keys) {
        if (-not $before.ContainsKey($k)) {
            Write-Output "[$(& $stamp)] + $($now[$k])  $k"
        }
    }
    foreach ($k in $before.Keys) {
        if (-not $now.ContainsKey($k)) {
            Write-Output "[$(& $stamp)] - $($before[$k])  $k"
        }
    }
    $before = $now
}
Write-Output "[$(& $stamp)] done"
