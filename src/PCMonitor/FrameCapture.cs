using System.Diagnostics;
using System.Globalization;
using System.Text;

namespace PCMonitor;

public sealed record PresentRow(int Pid, string Name, string Swap, double Time, double? Between, double? Gpu);
public sealed class FrameCapture : IDisposable
{
    private readonly object gate = new();
    private sealed class Ring : Queue<(PresentRow Row, long Arrival)>
    {
        public (PresentRow Row, long Arrival) Newest { get; set; }
    }
    private readonly Dictionary<(int, string), Ring> rings = [];
    private readonly CancellationTokenSource stop = new();
    private readonly SemaphoreSlim wake = new(0, 1);
    private Process? child;
    private int generation;
    private long lastParsed, lastSweep;
    private int storedRows;
    private long busySince;
    private readonly Task supervisor;
    private volatile bool enabled;
    private string state = "starting";
    public string State => state;
    public void Observe(bool busy) { if (!busy) Interlocked.Exchange(ref busySince, 0); else Interlocked.CompareExchange(ref busySince, Environment.TickCount64, 0); }
    public FrameCapture(bool enabled = true) { this.enabled = enabled; supervisor = Task.Run(Supervise); }
    public void Enable(bool value) { if (enabled != value) { enabled = value; Restart(); } }
    public void Restart()
    {
        lock (gate) { generation++; rings.Clear(); storedRows = 0; lastParsed = 0; try { if (child is { HasExited: false }) child.Kill(true); } catch (InvalidOperationException) { } }
        try { wake.Release(); } catch (SemaphoreFullException) { }
    }
    private async Task Supervise()
    {
        int retry = 0;
        while (!stop.IsCancellationRequested)
        {
            if (!enabled) { state = "paused"; await Delay(1000); continue; }
            string exe = Path.Combine(AppContext.BaseDirectory, "presentmon.exe");
            if (!File.Exists(exe)) { state = "unavailable: presentmon.exe missing"; await Delay(30000); continue; }
            Process? process = null;
            try
            {
                while (wake.Wait(0)) { }
                var reclaim = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                foreach (string arg in new[] { "--no_console_stats", "--no_csv", "--session_name", "PCMonitor-native", "--terminate_existing_session" }) reclaim.ArgumentList.Add(arg);
                using (var previous = Process.Start(reclaim))
                {
                    if (previous is not null)
                    {
                        var output = previous.StandardOutput.ReadToEndAsync(stop.Token); var reclaimError = previous.StandardError.ReadToEndAsync(stop.Token);
                        try { await previous.WaitForExitAsync(stop.Token).WaitAsync(TimeSpan.FromSeconds(5)); } catch (TimeoutException) { previous.Kill(true); }
                        await output; await reclaimError;
                    }
                }
                await Delay(1000);
                var start = new ProcessStartInfo(exe) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
                foreach (string arg in new[] { "--no_console_stats", "--qpc_time_ms", "--session_name", "PCMonitor-native", "--stop_existing_session", "--output_stdout", "--exclude_dropped" }) start.ArgumentList.Add(arg);
                process = new Process { StartInfo = start };
                int current;
                lock (gate) { if (stop.IsCancellationRequested || !enabled) continue; current = ++generation; process.Start(); child = process; }
                using var job = new ProcessJob(); job.Add(process);
                state = "starting"; long began = Environment.TickCount64;
                var errors = process.StandardError.ReadToEndAsync(stop.Token);
                var reader = Read(process, current);
                while (!process.HasExited && !reader.IsCompleted && !stop.IsCancellationRequested)
                {
                    if (Environment.TickCount64 - began > 20000 && state == "starting") throw new IOException("PresentMon produced no header within 20 seconds.");
                    long busy = Interlocked.Read(ref busySince), parsed = Interlocked.Read(ref lastParsed);
                    if (busy > 0 && Environment.TickCount64 - Math.Max(began, Math.Max(parsed, busy)) > 20000) throw new IOException("PresentMon stopped producing frames while the GPU was busy.");
                    await Delay(500);
                }
                if (!process.HasExited) process.Kill(true);
                await process.WaitForExitAsync(stop.Token); await reader;
                string error = await errors; if (!stop.IsCancellationRequested && enabled) state = "unavailable: " + (error.Length > 0 ? error[..Math.Min(error.Length, 180)].Trim() : "collector exited");
            }
            catch (Exception e) when (e is IOException or InvalidOperationException or System.ComponentModel.Win32Exception or OperationCanceledException) { if (!stop.IsCancellationRequested) { state = "unavailable: " + e.Message; Log.Write("Capture: " + e.Message); } }
            finally
            {
                lock (gate) { if (ReferenceEquals(child, process)) child = null; }
                if (process is not null) { try { if (!process.HasExited) process.Kill(true); } catch (InvalidOperationException) { } process.Dispose(); }
            }
            if (!stop.IsCancellationRequested) await Delay(Math.Min(60000, 2000 * (1 << Math.Min(retry++, 5))));
        }
    }
    private async Task Delay(int ms) { try { await wake.WaitAsync(ms, stop.Token); } catch (OperationCanceledException) { } }
    private async Task Read(Process p, int gen)
    {
        Dictionary<string, int>? header = null;
        while (!stop.IsCancellationRequested)
        {
            var line = await p.StandardOutput.ReadLineAsync(stop.Token); if (line is null) break;
            if (line.Length > 16384) continue;
            var columns = Csv(line);
            if (columns.Contains("ProcessID") && columns.Contains("CPUStartQPCTimeInMs"))
            {
                header = columns.Select((name, i) => (name, i)).ToDictionary(x => x.name, x => x.i);
                state = "ready"; continue;
            }
            if (header is null) continue;
            var row = Parse(columns, header); if (row is null) continue;
            lock (gate)
            {
                if (generation != gen) continue;
                long now = Environment.TickCount64; lastParsed = now; state = "live";
                var key = (row.Pid, row.Swap);
                if (!rings.TryGetValue(key, out var q)) { if (rings.Count >= 128) continue; rings[key] = q = new(); }
                if (q.Count > 0 && (q.Peek().Row.Name != row.Name || row.Time < q.Newest.Row.Time)) { storedRows -= q.Count; q.Clear(); }
                q.Enqueue((row, now)); q.Newest = (row, now); storedRows++;
                while (q.Count > 0 && (q.Count > 60000 || row.Time - q.Peek().Row.Time > 60000)) { q.Dequeue(); storedRows--; }
                if (now - lastSweep > 5000 || storedRows > 180000)
                {
                    lastSweep = now;
                    foreach (var old in rings.Where(x => x.Value.Count == 0 || now - x.Value.Newest.Arrival > 65000).Select(x => x.Key).ToArray()) { storedRows -= rings[old].Count; rings.Remove(old); }
                    while (storedRows > 180000) { var oldest = rings.MinBy(x => x.Value.Peek().Arrival); oldest.Value.Dequeue(); storedRows--; if (oldest.Value.Count == 0) rings.Remove(oldest.Key); }
                }
            }
        }
    }
    public static List<string> Csv(string line)
    {
        List<string> result = []; var s = new StringBuilder(); bool quoted = false;
        for (int i = 0; i < line.Length; i++)
        {
            if (line[i] == '"') { if (quoted && i + 1 < line.Length && line[i + 1] == '"') { s.Append('"'); i++; } else quoted = !quoted; }
            else if (line[i] == ',' && !quoted) { result.Add(s.ToString()); s.Clear(); } else s.Append(line[i]);
        }
        result.Add(s.ToString()); return result;
    }
    public static PresentRow? Parse(IReadOnlyList<string> values, Dictionary<string, int> header)
    {
        string? Get(string key) => header.TryGetValue(key, out int index) && index < values.Count ? values[index] : null;
        double? Number(string key) => double.TryParse(Get(key), NumberStyles.Float, CultureInfo.InvariantCulture, out var value) && double.IsFinite(value) ? value : null;
        if (!int.TryParse(Get("ProcessID"), out int pid) || pid <= 0 || Number("CPUStartQPCTimeInMs") is not double time) return null;
        foreach (var key in new[] { "Application", "SwapChainAddress", "PresentMode" }) if (header.TryGetValue(key, out int index) && index >= values.Count) return null;
        double? between = Number("MsBetweenPresents"); if (between <= 0) between = null;
        var busy = Number("MsGPUBusy");
        return new(pid, Get("Application") ?? "?", Get("SwapChainAddress") ?? "0", time, between, between > 0 && busy >= 0 ? Math.Clamp(100 * busy.Value / between.Value, 0, 100) : null);
    }
    public FrameStats Stats(int pid)
    {
        (PresentRow Row, long Arrival)[] rows;
        lock (gate)
        {
            var choices = rings.Where(x => x.Key.Item1 == pid && x.Value.Count > 0).Select(x => x.Value).ToArray();
            if (choices.Length == 0) return new();
            var newest = choices.Max(q => q.Newest.Arrival);
            var pick = choices.Where(q => newest - q.Newest.Arrival < 1500).OrderByDescending(q => q.Count(r => r.Row.Time >= q.Newest.Row.Time - 1000)).First();
            rows = pick.ToArray();
        }
        double age = (Environment.TickCount64 - rows[^1].Arrival) / 1000.0; if (age > 12) return new();
        return Summarize(rows.Select(x => x.Row).ToArray(), age);
    }
    public static FrameStats Summarize(PresentRow[] rows, double age = 0)
    {
        if (rows.Length == 0) return new();
        var recent = rows.Where(r => r.Time >= rows[^1].Time - 1000).ToArray();
        var intervals = rows.Where(r => r.Between > 0).Select(r => r.Between!.Value).Order().ToArray();
        var span = recent.Length > 1 ? recent[^1].Time - recent[0].Time : 0;
        double? fps = span > 0 ? (recent.Length - 1) * 1000 / span : intervals.Length > 0 ? 1000 / Percentile(intervals, .5) : null;
        var ms = recent.Where(r => r.Between > 0).Select(r => r.Between!.Value).Order().ToArray();
        var gpu = recent.Where(r => r.Gpu.HasValue).Select(r => r.Gpu!.Value).ToArray();
        return new(fps, ms.Length > 0 ? Percentile(ms, .5) : null, intervals.Length >= 30 ? 1000 / Percentile(intervals, .99) : null, intervals.Length >= 100 ? 1000 / Percentile(intervals, .999) : null, age > 2, age, gpu.Length > 0 ? gpu.Average() : null);
    }
    private static double Percentile(double[] sorted, double q) { var f = (sorted.Length - 1) * q; int i = (int)f; return sorted[i] + (sorted[Math.Min(i + 1, sorted.Length - 1)] - sorted[i]) * (f - i); }
    public void Dispose() { stop.Cancel(); Restart(); try { supervisor.Wait(3000); } catch (AggregateException) { } stop.Dispose(); }
}
