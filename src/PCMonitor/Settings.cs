using System.Text.Json;
using System.Text.Json.Serialization;

namespace PCMonitor;

public sealed record Settings
{
    public int Rotation { get; init; } = 180;
    public string Port { get; init; } = "AUTO";
    public int IdleBrightness { get; init; } = 45;
    public int GameBrightness { get; init; } = 70;
    public int DimBrightness { get; init; } = 12;
    public int BlankBrightness { get; init; } = 1;
    public double IdleIntervalSeconds { get; init; } = 0.5;
    public double GameIntervalSeconds { get; init; } = 0.25;
    public double SensorIntervalSeconds { get; init; } = 0.5;
    public int DimAfterSeconds { get; init; } = 300;
    public int BlankAfterMinutes { get; init; } = 45;
    public bool FollowLock { get; init; } = true;
    public bool FollowDisplay { get; init; } = true;
    public bool StayLitInGame { get; init; } = true;
    public bool CaptureFrames { get; init; } = true;
    public string NightMode { get; init; } = "auto";
    public int NightTemperature { get; init; } = 0;
    public double NightBrightnessScale { get; init; } = 0.55;
    public int NightBrightnessFloor { get; init; } = 8;
    public double BaseWatts { get; init; } = 34;
    public double RailOverheadPercent { get; init; } = 9;
    public double CpuTdp { get; init; } = 170;
    public double GpuTdp { get; init; } = 575;
    public string[] GameProcesses { get; init; } = [];
    public bool Trends { get; init; } = true;
    public bool PixelShift { get; init; } = true;

    public void Validate()
    {
        if (Rotation is not (0 or 180)) throw new InvalidDataException("Rotation must be 0 or 180.");
        if (string.IsNullOrWhiteSpace(Port) || (Port != "AUTO" && !System.Text.RegularExpressions.Regex.IsMatch(Port, "^COM[0-9]+$"))) throw new InvalidDataException("Port must be AUTO or COM followed by digits.");
        foreach (var n in new[] { IdleBrightness, GameBrightness, DimBrightness, BlankBrightness, NightBrightnessFloor })
            if (n is < 0 or > 100) throw new InvalidDataException("Brightness must be between 0 and 100.");
        if (!double.IsFinite(IdleIntervalSeconds) || IdleIntervalSeconds is < 0.25 or > 60 || !double.IsFinite(GameIntervalSeconds) || GameIntervalSeconds is < 0.25 or > 60 || !double.IsFinite(SensorIntervalSeconds) || SensorIntervalSeconds is < 0.25 or > 60)
            throw new InvalidDataException("Refresh intervals must be between 0.25 and 60 seconds.");
        if (DimAfterSeconds < 0 || BlankAfterMinutes < 0 || NightMode is not ("auto" or "on" or "off") || (NightTemperature != 0 && NightTemperature is < 1000 or > 10000))
            throw new InvalidDataException("Invalid idle timer or night mode.");
        if (!double.IsFinite(NightBrightnessScale) || NightBrightnessScale is < 0 or > 1) throw new InvalidDataException("NightBrightnessScale must be between 0 and 1.");
        foreach (var n in new[] { BaseWatts, RailOverheadPercent, CpuTdp, GpuTdp })
            if (!double.IsFinite(n) || n < 0 || n > 10000) throw new InvalidDataException("Power model values must be finite and between 0 and 10000.");
        if (GameProcesses is null || GameProcesses.Any(string.IsNullOrWhiteSpace)) throw new InvalidDataException("GameProcesses must be an array of process names.");
    }

    public static readonly JsonSerializerOptions Json = new()
    {
        WriteIndented = true, PropertyNameCaseInsensitive = true, UnmappedMemberHandling = JsonUnmappedMemberHandling.Disallow
    };
    public static Settings Load(string path)
    {
        var value = JsonSerializer.Deserialize<Settings>(File.ReadAllText(path), Json) ?? throw new InvalidDataException("Empty settings.");
        value.Validate(); return value;
    }
}

public static class AppPaths
{
    public static string Data { get; } = Environment.GetEnvironmentVariable("PCMON_DATA_DIR") ?? Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PCMonitor");
    public static string Config => Path.Combine(Data, "settings.json");
    public static string Logs => Path.Combine(Data, "Logs");
    public static string Status => Path.Combine(Data, "status.json");
    public static void Initialize()
    {
        Directory.CreateDirectory(Data); Directory.CreateDirectory(Logs);
        if (!File.Exists(Config)) File.WriteAllText(Config, JsonSerializer.Serialize(new Settings(), Settings.Json));
    }
}

public static class Log
{
    private static readonly object Gate = new();
    public static void Write(string message)
    {
        lock (Gate)
        {
            try
            {
                var path = Path.Combine(AppPaths.Logs, Environment.GetCommandLineArgs().Contains("--worker") ? "worker.log" : "tray.log");
                if (File.Exists(path) && new FileInfo(path).Length > 2 * 1024 * 1024)
                {
                    File.Delete(path + ".2"); if (File.Exists(path + ".1")) File.Move(path + ".1", path + ".2"); File.Move(path, path + ".1");
                }
                File.AppendAllText(path, $"{DateTimeOffset.Now:O} [{Environment.ProcessId}] {message}{Environment.NewLine}");
            }
            catch (IOException) { }
            catch (UnauthorizedAccessException) { }
        }
    }
}
