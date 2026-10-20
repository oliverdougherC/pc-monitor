using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json;

namespace PCMonitor;

internal static class Program
{
    [STAThread]
    private static int Main(string[] args)
    {
        try
        {
            if (args is ["--worker", var pipe]) { new Worker().Run(pipe).GetAwaiter().GetResult(); return 0; }
            if (args is ["--command", var command]) return Command(command);
            if (args.Length >= 2 && args[0] == "--render")
            {
                using var renderer = new Renderer(); var config = new Settings { PixelShift = false };
                var sample = Demo(args.Contains("--game")); for (int i = 0; i < 120; i++) renderer.Observe(sample);
                renderer.Render(sample, config, true).Save(Path.GetFullPath(args[1])); return 0;
            }
            if (args is ["--diagnose", var output])
            {
                using var telemetry = new Telemetry(); telemetry.Read(); Thread.Sleep(1000); var sample = telemetry.Read();
                File.WriteAllText(output, JsonSerializer.Serialize(new { Sample = sample, PanelDevices = Panel.Devices().Select(p => new { p.Port, p.Awake }), ProcessId = Environment.ProcessId }, Settings.Json)); return 0;
            }
            AppPaths.Initialize();
            if (args.Length > 0) throw new ArgumentException("Usage: PCMonitor.exe [--command start|stop|restart|status|blank|unblank|exit] [--render image.png [--game]] [--diagnose report.json]");
            using var mutex = new Mutex(true, @"Global\PCMonitor-native-main", out bool created);
            if (!created) return Command("status");
            ApplicationConfiguration.Initialize();
            Application.SetUnhandledExceptionMode(UnhandledExceptionMode.ThrowException);
            AppDomain.CurrentDomain.UnhandledException += (_, e) => Log.Write("Fatal: " + e.ExceptionObject);
            Application.Run(new TrayApp()); return 0;
        }
        catch (Exception e)
        {
            Log.Write("Fatal: " + e); Console.Error.WriteLine(e); return 1;
        }
    }
    private static int Command(string command)
    {
        using var pipe = new NamedPipeClientStream(".", TrayApp.ControlPipe, PipeDirection.InOut, PipeOptions.Asynchronous);
        pipe.Connect(3000); using var reader = new StreamReader(pipe, leaveOpen: true); using var writer = new StreamWriter(pipe, leaveOpen: true) { AutoFlush = true };
        writer.WriteLine(command); string? response = reader.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(12)).GetAwaiter().GetResult();
        Console.WriteLine(response); return response is null ? 1 : 0;
    }
    private static Sample Demo(bool game) => new()
    {
        CpuLoad = 37, CpuTemp = 62, CpuPeak = 5450, CpuAverage = 4280, CpuPower = 95, GpuLoad = 82, GpuTemp = 67, GpuClock = 2610, GpuPower = 390, Vram = 17300,
        RamUsed = 27100, RamTotal = 68719, DiskRead = 356e6, DiskWrite = 18e6, NetDown = 173e6, NetUp = 4.2e6, Game = game, Frames = new(144, 6.9, 112, 93), SensorState = "simulated preview"
    };
}
