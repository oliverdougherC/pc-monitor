using System.Diagnostics;
using System.Net.NetworkInformation;
using System.Runtime.InteropServices;
using LibreHardwareMonitor.Hardware;

namespace PCMonitor;

public sealed class Telemetry : IDisposable
{
    private Computer? computer;
    private PerformanceCounter? diskRead, diskWrite;
    private ulong idle, kernel, user;
    private long previousTime;
    private readonly Dictionary<string, (long Read, long Write)> netPrevious = [];
    private long retryHardware;
    private long nextReacquire;
    private int incompleteReads;
    public string State { get; private set; } = "starting";
    public Telemetry() { Open(); }
    private void Open()
    {
        try { computer = new Computer { IsCpuEnabled = true, IsGpuEnabled = true }; computer.Open(); }
        catch (Exception e) { Log.Write("Hardware sensors: " + e.Message); computer?.Close(); computer = null; retryHardware = Environment.TickCount64 + 30000; }
        if (diskRead is null && diskWrite is null) try
        {
            diskRead = new("PhysicalDisk", "Disk Read Bytes/sec", "_Total", true);
            diskWrite = new("PhysicalDisk", "Disk Write Bytes/sec", "_Total", true);
            diskRead.NextValue(); diskWrite.NextValue();
        }
        catch (Exception e) { Log.Write("Disk counters: " + e.Message); diskRead?.Dispose(); diskWrite?.Dispose(); diskRead = diskWrite = null; }
    }
    private static double? Finite(float? value, bool positive = false) => value is float f && float.IsFinite(f) && (!positive || f > 0) ? f : null;
    public Sample Read()
    {
        if (computer is null && Environment.TickCount64 >= retryHardware) { Open(); retryHardware = Environment.TickCount64 + 30000; }
        var sensors = new List<(HardwareType Type, ISensor Sensor)>();
        int hardwareFailed = 0;
        if (computer is not null)
        {
            foreach (var hw in computer.Hardware)
            {
                try
                {
                    hw.Update(); sensors.AddRange(hw.Sensors.Select(s => (hw.HardwareType, s)));
                    foreach (var child in hw.SubHardware) { child.Update(); sensors.AddRange(child.Sensors.Select(s => (hw.HardwareType, s))); }
                }
                catch (Exception e) { hardwareFailed++; Log.Write("Hardware update failed: " + hw.Name + ": " + e.Message); }
            }
        }
        double? Pick(HardwareType type, SensorType kind, params string[] names) => sensors.Where(x => x.Type == type && x.Sensor.SensorType == kind && names.Any(n => x.Sensor.Name.Contains(n, StringComparison.OrdinalIgnoreCase))).Select(x => Finite(x.Sensor.Value, kind is SensorType.Temperature or SensorType.Clock or SensorType.Power)).FirstOrDefault(v => v is not null);
        var gpuType = new[] { HardwareType.GpuNvidia, HardwareType.GpuAmd, HardwareType.GpuIntel }.FirstOrDefault(t => sensors.Any(s => s.Type == t));
        var clocks = sensors.Where(s => s.Type == HardwareType.Cpu && s.Sensor.SensorType == SensorType.Clock && s.Sensor.Name.Contains("Core #") && !s.Sensor.Name.Contains("Effective")).Select(s => Finite(s.Sensor.Value, true)).Where(v => v.HasValue).Select(v => v!.Value).ToArray();
        double? cpuLoad = null;
        if (GetSystemTimes(out ulong nextIdle, out ulong nextKernel, out ulong nextUser))
        {
            var total = nextKernel - kernel + nextUser - user;
            if (previousTime > 0 && total > 0) cpuLoad = Math.Clamp((1 - (double)(nextIdle - idle) / total) * 100, 0, 100);
            idle = nextIdle; kernel = nextKernel; user = nextUser;
        }
        var memory = new MemoryStatus { Length = (uint)Marshal.SizeOf<MemoryStatus>() }; bool memoryOk = GlobalMemoryStatusEx(ref memory);
        double? read = null, write = null;
        try { read = diskRead?.NextValue(); write = diskWrite?.NextValue(); }
        catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception) { Log.Write("Disk counters unavailable: " + e.Message); diskRead?.Dispose(); diskWrite?.Dispose(); diskRead = diskWrite = null; }
        double? down = null, up = null; long now = Environment.TickCount64; double dt = (now - previousTime) / 1000.0;
        var next = new Dictionary<string, (long Read, long Write)>();
        try
        {
            foreach (var nic in NetworkInterface.GetAllNetworkInterfaces().Where(n => n.OperationalStatus == OperationalStatus.Up && n.NetworkInterfaceType is not (NetworkInterfaceType.Loopback or NetworkInterfaceType.Tunnel)))
            {
                var s = nic.GetIPStatistics(); next[nic.Id] = (s.BytesReceived, s.BytesSent);
            }
            if (dt is > 0 and < 30 && next.Count > 0 && next.Keys.Order().SequenceEqual(netPrevious.Keys.Order()) && next.All(x => x.Value.Read >= netPrevious[x.Key].Read && x.Value.Write >= netPrevious[x.Key].Write))
            {
                down = next.Sum(x => (double)(x.Value.Read - netPrevious[x.Key].Read)) * 8 / dt;
                up = next.Sum(x => (double)(x.Value.Write - netPrevious[x.Key].Write)) * 8 / dt;
            }
        }
        catch (NetworkInformationException) { next.Clear(); }
        netPrevious.Clear(); foreach (var item in next) netPrevious[item.Key] = item.Value;
        previousTime = now;
        var cpuTemp = Pick(HardwareType.Cpu, SensorType.Temperature, "Tctl", "CPU Package");
        var gpuLoad = Pick(gpuType, SensorType.Load, "GPU Core");
        State = cpuTemp is null ? "partial: CPU temperature/power unavailable (driver or elevation)" : hardwareFailed > 0 ? "partial: hardware update failed" : gpuLoad is null ? "partial: GPU sensors unavailable" : "live";
        incompleteReads = State == "live" ? 0 : incompleteReads + 1;
        var result = new Sample
        {
            CpuLoad = cpuLoad, CpuTemp = cpuTemp,
            CpuPeak = clocks.Length == 0 ? null : clocks.Max(), CpuAverage = clocks.Length == 0 ? null : clocks.Average(),
            CpuPower = Pick(HardwareType.Cpu, SensorType.Power, "Total Power", "Package"),
            GpuLoad = gpuLoad, GpuTemp = Pick(gpuType, SensorType.Temperature, "GPU Core"),
            GpuClock = Pick(gpuType, SensorType.Clock, "GPU Core"), GpuPower = Pick(gpuType, SensorType.Power, "GPU Package", "GPU Board", "GPU Power"),
            Vram = Pick(gpuType, SensorType.SmallData, "GPU Memory Used"),
            RamTotal = memoryOk ? memory.TotalPhysical / 1e6 : null, RamUsed = memoryOk ? (memory.TotalPhysical - memory.AvailablePhysical) / 1e6 : null,
            DiskRead = read, DiskWrite = write, NetDown = down, NetUp = up, SensorState = State
        };
        if (incompleteReads >= 3 && now >= nextReacquire)
        {
            // Reacquire native sensor handles after driver resets; progress alone
            // must not keep an unusable backend alive indefinitely.
            computer?.Close(); computer = null; retryHardware = now + 5000;
            nextReacquire = now + 30000; incompleteReads = 0;
        }
        return result;
    }
    public void Dispose() { computer?.Close(); computer = null; diskRead?.Dispose(); diskWrite?.Dispose(); }
    [StructLayout(LayoutKind.Sequential)] private struct MemoryStatus { public uint Length, Load; public ulong TotalPhysical, AvailablePhysical, TotalPage, AvailablePage, TotalVirtual, AvailableVirtual, Extended; }
    [DllImport("kernel32.dll")] private static extern bool GlobalMemoryStatusEx(ref MemoryStatus status);
    [DllImport("kernel32.dll")] private static extern bool GetSystemTimes(out ulong idle, out ulong kernel, out ulong user);
}

public sealed class GameDetector
{
    private int target, candidate;
    private long targetStart, candidateSince, lastActive;
    private static readonly HashSet<string> NonGames = new(StringComparer.OrdinalIgnoreCase)
    { "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "spotify.exe", "discord.exe", "teams.exe", "explorer.exe", "dwm.exe", "obs64.exe", "taskmgr.exe", "code.exe", "devenv.exe", "applicationframehost.exe", "systemsettings.exe", "searchhost.exe", "steamwebhelper.exe" };
    public int Target => target;
    public bool Tick(HostIntent h, Sample sample, FrameCapture capture, Settings config)
    {
        long now = Environment.TickCount64;
        if (target != 0)
        {
            try { using var p = Process.GetProcessById(target); if (p.StartTime.ToUniversalTime().Ticks != targetStart) Reset(); }
            catch (ArgumentException) { Reset(); } catch (System.ComponentModel.Win32Exception) { Reset(); }
        }
        bool named = config.GameProcesses.Contains(h.ForegroundName, StringComparer.OrdinalIgnoreCase);
        var incoming = capture.Stats(h.ForegroundPid);
        bool strong = named || !NonGames.Contains(h.ForegroundName) && h.Fullscreen && incoming.Fps >= 24 && !incoming.Held;
        bool medium = !NonGames.Contains(h.ForegroundName) && (incoming.GpuBusy >= 55 && incoming.Fps >= 24 || h.Fullscreen && sample.GpuLoad >= 10);
        if (strong || medium)
        {
            if (candidate != h.ForegroundPid) { candidate = h.ForegroundPid; candidateSince = now; }
            if (candidate != 0 && candidate != target && now - candidateSince >= (strong ? 1200 : 4000) && (target == 0 || now - lastActive > 3000))
            {
                try { using var p = Process.GetProcessById(candidate); targetStart = p.StartTime.ToUniversalTime().Ticks; target = candidate; lastActive = now; }
                catch (ArgumentException) { } catch (System.ComponentModel.Win32Exception) { }
            }
        }
        else candidate = 0;
        if (target != 0)
        {
            var active = capture.Stats(target);
            if (active.Fps > 0 && !active.Held || h.ForegroundPid == target && (named || h.Fullscreen && sample.GpuLoad >= 10)) lastActive = now;
            if (now - lastActive > 25000) Reset();
        }
        return target != 0;
    }
    public void Reset() { target = candidate = 0; lastActive = 0; }
}
