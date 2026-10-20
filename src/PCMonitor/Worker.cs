using System.IO.Pipes;
using System.Text.Json;

namespace PCMonitor;

public sealed record WorkerMessage(string Kind, HostIntent? Host = null);
public sealed class Worker
{
    private HostIntent host = new();
    private readonly AutoResetEvent changed = new(false);
    public async Task Run(string pipeName)
    {
        using var pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        using var stop = new CancellationTokenSource();
        await pipe.ConnectAsync(10000, stop.Token);
        using var reader = new StreamReader(pipe, leaveOpen: true); using var writer = new StreamWriter(pipe, leaveOpen: true) { AutoFlush = true };
        var receiver = Task.Run(async () =>
        {
            try
            {
                while (!stop.IsCancellationRequested)
                {
                    var line = await reader.ReadLineAsync(stop.Token); if (line is null) break;
                    var msg = JsonSerializer.Deserialize<WorkerMessage>(line);
                    if (msg?.Kind == "stop") break;
                    if (msg?.Host is { } h) { Volatile.Write(ref host, h); changed.Set(); }
                }
            }
            catch (Exception e) when (e is IOException or OperationCanceledException or JsonException) { }
            finally { stop.Cancel(); changed.Set(); }
        });
        using var panel = new Panel(); using var renderer = new Renderer(); using var capture = new FrameCapture(false);
        Telemetry? sensors = null;
        var night = new NightLight(); var watch = new GameDetector(); Sample? previous = null;
        Settings config = Settings.Load(AppPaths.Config); DateTime configTime = File.GetLastWriteTimeUtc(AppPaths.Config); long configCheck = 0, sensorRetry = 0;
        int resume = 0, display = 0; bool wasBlank = true; bool initial = true;
        Sample? hardwareSample = null; long hardwareReadAt = 0;
        string lastLight = "", lastHealth = "", lastNight = "";
        try
        {
            while (!stop.IsCancellationRequested)
            {
                long began = Environment.TickCount64; var h = Volatile.Read(ref host);
                if (initial) { await Report("starting"); initial = false; }
                if (began >= configCheck)
                {
                    configCheck = began + 3000;
                    var stamp = File.GetLastWriteTimeUtc(AppPaths.Config);
                    if (stamp != configTime)
                    {
                        try { config = Settings.Load(AppPaths.Config); configTime = stamp; panel.RetryNow(); Log.Write("Settings reloaded."); }
                        catch (Exception e) when (e is IOException or JsonException or InvalidDataException) { Log.Write("Rejected settings: " + e.Message); configTime = stamp; }
                    }
                }
                night.Refresh(config);
                if (night.State != lastNight) { lastNight = night.State; Log.Write("Night appearance: " + lastNight); }
                bool game = previous?.Game ?? false;
                var plan = LightPolicy.Decide(config, h, game && previous?.Frames is { Fps: > 0, Held: false }, night.On);
                if (h.ResumeGeneration != resume)
                {
                    resume = h.ResumeGeneration; capture.Restart(); watch.Reset(); sensors?.Dispose(); sensors = null; previous = null; hardwareSample = null; hardwareReadAt = 0; sensorRetry = 0; panel.NoteResume(); Log.Write("Resume: telemetry and panel handles retired.");
                }
                if (h.DisplayGeneration != display) { display = h.DisplayGeneration; panel.RetryNow(); }
                if (plan.Blank)
                {
                    capture.Enable(false);
                    // Existing USB is kept awake. No TURNOFF, reset, or driver cycle.
                    if (panel.Connected) panel.Present(null, plan);
                    wasBlank = true;
                    if (plan.Reason != lastLight) { lastLight = plan.Reason; Log.Write("Blank: " + plan.Reason); }
                    await Report(plan.Reason);
                    changed.WaitOne(1000); continue;
                }
                capture.Enable(config.CaptureFrames);
                if (wasBlank) { sensors?.ResetRates(); hardwareSample = null; hardwareReadAt = 0; }
                Sample sample;
                bool hardwareFresh = false;
                try
                {
                    if (sensors is null && began >= sensorRetry) { sensors = new Telemetry(); sensorRetry = began + 30000; }
                    if (hardwareSample is null || began - hardwareReadAt >= 1000)
                    {
                        hardwareSample = sensors?.Read() ?? new Sample { SensorState = "unavailable" }; hardwareReadAt = began; hardwareFresh = true;
                    }
                    sample = hardwareSample;
                    if (wasBlank) sample = sample with { Game = false }; // counters are re-primed below on a true resume
                }
                catch (Exception e)
                {
                    Log.Write("Sensor sample failed: " + e.Message);
                    sample = previous is not null && DateTimeOffset.UtcNow - previous.Time < TimeSpan.FromSeconds(30) ? previous with { Held = true, SensorState = "held: " + e.Message } : new Sample { SensorState = "unavailable: " + e.Message };
                    sensors?.Dispose(); sensors = null; hardwareSample = null; sensorRetry = began + 5000;
                }
                game = watch.Tick(h, sample, capture, config); sample = sample with { Game = game, Frames = game ? capture.Stats(watch.Target) : new() };
                capture.Observe(game || sample.GpuLoad >= 10);
                plan = LightPolicy.Decide(config, h, game && sample.Frames is { Fps: > 0, Held: false }, night.On);
                if (!sample.Held) { renderer.Observe(sample, hardwareFresh); previous = sample; }
                h = Volatile.Read(ref host); plan = LightPolicy.Decide(config, h, game && sample.Frames is { Fps: > 0, Held: false }, night.On);
                if (!plan.Blank) panel.Connect(config);
                if (panel.Connected)
                {
                    var image = renderer.Render(sample, config);
                    // Re-read intent after sensor acquisition: a display-off supersedes the frame just composed.
                    h = Volatile.Read(ref host); plan = LightPolicy.Decide(config, h, game && sample.Frames is { Fps: > 0, Held: false }, night.On);
                    var pixels = plan.Blank ? null : Renderer.Pixels(image, config.Rotation, night.On ? night.Gains : null);
                    panel.Present(pixels, plan);
                }
                wasBlank = plan.Blank;
                if (lastLight != plan.Reason) { lastLight = plan.Reason; Log.Write("Appearance: " + plan.Reason); }
                var health = sample.SensorState + " / " + capture.State;
                if (health != lastHealth) { lastHealth = health; Log.Write("Telemetry: " + health); }
                await Report(plan.Reason, sample, Environment.TickCount64 - began);
                double interval = game ? config.GameIntervalSeconds : config.IdleIntervalSeconds;
                int wait = Math.Max(0, (int)(interval * 1000 - (Environment.TickCount64 - began)));
                changed.WaitOne(wait);
            }
        }
        catch (OperationCanceledException) when (stop.IsCancellationRequested) { }
        catch (IOException) when (stop.IsCancellationRequested) { }
        finally
        {
            try { if (panel.Connected) panel.Present(null, new(true, config.BlankBrightness, "stopped")); } catch (Exception) { }
            sensors?.Dispose(); stop.Cancel(); changed.Set();
            try { await receiver.WaitAsync(TimeSpan.FromSeconds(1)); } catch (Exception) { }
            changed.Dispose();
        }
        async Task Report(string light, Sample? sample = null, double tick = 0)
        {
            var status = new WorkerStatus(DateTimeOffset.UtcNow, panel.State, sample?.SensorState ?? sensors?.State ?? "paused", capture.State, light, sample?.Game == true ? "game" : "idle", TickMilliseconds: tick, Night: night.State);
            await writer.WriteLineAsync(JsonSerializer.Serialize(status).AsMemory(), stop.Token);
        }
    }
}
