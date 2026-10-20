namespace PCMonitor;

public sealed record FrameStats(double? Fps = null, double? Milliseconds = null, double? Low1 = null, double? Low01 = null, bool Held = false, double Age = 0, double? GpuBusy = null);

public sealed record Sample
{
    public DateTimeOffset Time { get; init; } = DateTimeOffset.UtcNow;
    public double? CpuLoad { get; init; }
    public double? CpuTemp { get; init; }
    public double? CpuPeak { get; init; }
    public double? CpuAverage { get; init; }
    public double? CpuPower { get; init; }
    public double? GpuLoad { get; init; }
    public double? GpuTemp { get; init; }
    public double? GpuClock { get; init; }
    public double? GpuPower { get; init; }
    public double? Vram { get; init; }
    public double? RamUsed { get; init; }
    public double? RamTotal { get; init; }
    public double? DiskRead { get; init; }
    public double? DiskWrite { get; init; }
    public double? NetDown { get; init; }
    public double? NetUp { get; init; }
    public FrameStats Frames { get; init; } = new();
    public bool Game { get; init; }
    public string SensorState { get; init; } = "starting";
    public bool Held { get; init; }
}

public sealed record HostIntent(bool MonitorOn = true, bool Locked = false, bool Suspended = false, double IdleSeconds = 0, int ResumeGeneration = 0, int DisplayGeneration = 0, int ForegroundPid = 0, string ForegroundName = "", bool Fullscreen = false, bool ManualBlank = false);
public sealed record LightPlan(bool Blank, int Brightness, string Reason);
public static class LightPolicy
{
    public static LightPlan Decide(Settings c, HostIntent host, bool playing, bool night)
    {
        string? reason = host.Suspended ? "sleep" : host.ManualBlank ? "manual" : c.FollowLock && host.Locked ? "locked" : c.FollowDisplay && !host.MonitorOn ? "monitor-off" : null;
        var hold = playing && c.StayLitInGame;
        if (reason is null && !hold && c.BlankAfterMinutes > 0 && host.IdleSeconds >= c.BlankAfterMinutes * 60) reason = "idle";
        if (reason is not null) return new(true, c.BlankBrightness, reason);
        var brightness = playing ? c.GameBrightness : c.IdleBrightness;
        if (!hold && c.DimAfterSeconds > 0 && host.IdleSeconds >= c.DimAfterSeconds) brightness = c.DimBrightness;
        if (night) brightness = Math.Min(brightness, Math.Max(c.NightBrightnessFloor, (int)Math.Round(brightness * c.NightBrightnessScale)));
        return new(false, brightness, night ? "night" : "lit");
    }
}

public static class PowerModel
{
    public static (double? Watts, string Status) Estimate(Sample s, Settings c)
    {
        double? Model(double? power, double? load, double tdp, double idle) => power ?? (load is null ? null : idle + (tdp - idle) * Math.Pow(Math.Clamp(load.Value / 100, 0, 1), 1.25));
        var cpu = Model(s.CpuPower, s.CpuLoad, c.CpuTdp, 18); var gpu = Model(s.GpuPower, s.GpuLoad, c.GpuTdp, 28);
        if (cpu is null || gpu is null) return (null, cpu is null && gpu is null ? "unavailable" : "partial");
        var status = s.Held || DateTimeOffset.UtcNow - s.Time > TimeSpan.FromSeconds(8) ? "stale" : s.CpuPower is null || s.GpuPower is null ? "modelled input" : "measured input";
        return (c.BaseWatts + (cpu + gpu) * (1 + c.RailOverheadPercent / 100), status);
    }
}

public sealed record WorkerStatus(DateTimeOffset Time, string Panel, string Sensors, string Capture, string Light, string Mode, string? Error = null, double TickMilliseconds = 0, string Night = "unknown", long FramesPresented = 0, DateTimeOffset? LastFrame = null, string UpdateMode = "unknown", DateTimeOffset? SampleTime = null);
