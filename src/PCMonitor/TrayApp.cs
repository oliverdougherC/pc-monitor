using System.Diagnostics;
using System.IO.Pipes;
using System.Security.Principal;
using System.Security.AccessControl;
using System.Text.Json;

namespace PCMonitor;

public sealed class TrayApp : ApplicationContext
{
    private static readonly SecurityIdentifier UserSid = GetSid();
    public static string ControlPipe { get; } = "PCMonitor-control-" + UserSid.Value.Replace('-', '_');
    private static SecurityIdentifier GetSid() { using var identity = WindowsIdentity.GetCurrent(); return identity.User!; }
    private readonly bool elevated = IsElevated();
    private static bool IsElevated() { using var identity = WindowsIdentity.GetCurrent(); return new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator); }
    private readonly NotifyIcon icon;
    private readonly WindowsHost host;
    private readonly System.Windows.Forms.Timer timer;
    private readonly ProcessJob job = new();
    private readonly CancellationTokenSource closing = new();
    private Process? worker;
    private NamedPipeServerStream? pipe;
    private StreamWriter? writer;
    private WorkerStatus? status;
    private long lastProgress, nextStart, lastStatusWrite;
    private int failures;
    private bool desired = true, starting, sending;
    private bool stopping, ticking;
    private bool manualBlank;
    private HostIntent? lastHost;
    private int observedResume;
    private readonly SemaphoreSlim lifecycle = new(1, 1);
    private readonly ToolStripMenuItem stateItem = new("Starting…") { Enabled = false };
    private readonly ToolStripMenuItem detailItem = new("Waiting for telemetry") { Enabled = false };
    public TrayApp()
    {
        host = new WindowsHost(); host.Changed += HostChanged;
        var menu = new ContextMenuStrip(); menu.Items.Add(stateItem); menu.Items.Add(detailItem); menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Start monitoring", null, (_, _) => StartMonitoring());
        menu.Items.Add("Stop monitoring", null, async (_, _) => await StopMonitoring());
        menu.Items.Add("Restart monitoring", null, async (_, _) => { await StopMonitoring(); StartMonitoring(); });
        menu.Items.Add("Blank screen", null, (_, _) => { manualBlank = true; HostChanged(); });
        menu.Items.Add("Show screen", null, (_, _) => { manualBlank = false; HostChanged(); });
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Settings", null, (_, _) => Open(AppPaths.Config));
        menu.Items.Add("Open logs", null, (_, _) => Open(AppPaths.Logs));
        menu.Items.Add("Show status", null, (_, _) => { SaveStatus(); Open(AppPaths.Status); });
        menu.Items.Add("Exit", null, async (_, _) => { desired = false; await StopWorker(); ExitThread(); });
        icon = new NotifyIcon { Text = "PC Monitor — starting", Icon = SystemIcons.Application, Visible = true, ContextMenuStrip = menu };
        icon.DoubleClick += (_, _) => { SaveStatus(); Open(AppPaths.Status); };
        timer = new() { Interval = 500 }; timer.Tick += async (_, _) => await Tick(); timer.Start();
        _ = ControlLoop(); Log.Write("Native tray started.");
    }
    private static void Open(string path) => Process.Start(new ProcessStartInfo(path) { UseShellExecute = true });
    private async Task Tick()
    {
        if (closing.IsCancellationRequested || ticking) return;
        ticking = true;
        try {
        var h = host.Snapshot();
        h = h with { ManualBlank = manualBlank };
        h = h with { IdleSeconds = Math.Floor(h.IdleSeconds / 2) * 2 };
        if (h.ResumeGeneration != observedResume) { observedResume = h.ResumeGeneration; lastProgress = Environment.TickCount64; }
        if (worker is { HasExited: true } && !starting && !stopping)
        {
            Log.Write("Worker exited unexpectedly: " + worker.ExitCode);
            await StopWorker(); failures++;
            nextStart = Environment.TickCount64 + Math.Min(60000, 2000 * (1 << Math.Min(failures - 1, 5)));
        }
        if (desired && !starting && !stopping && (worker is null || worker.HasExited) && Environment.TickCount64 >= nextStart) _ = StartWorker();
        if (worker is { HasExited: false } && !h.Suspended && Environment.TickCount64 - lastProgress > 30000)
        {
            Log.Write("Monitoring worker stopped making progress; replacing it."); await StopWorker(); failures++; nextStart = Environment.TickCount64 + Math.Min(60000, 2000 * (1 << Math.Min(failures, 5)));
        }
        if (writer is not null && !sending && h != lastHost)
        {
            sending = true;
            try { await writer.WriteLineAsync(JsonSerializer.Serialize(new WorkerMessage("host", h))).WaitAsync(TimeSpan.FromSeconds(1)); lastHost = h; }
            catch (Exception e) when (e is IOException or TimeoutException or ObjectDisposedException) { Log.Write("Host delivery failed: " + e.Message); }
            finally { sending = false; }
        }
        string state = !desired ? "Stopped" : status is null || worker is null ? "Starting" : status.Panel.StartsWith("connected") ? "Running" : "Reconnecting";
        stateItem.Text = "PC Monitor · " + state;
        detailItem.Text = status is null ? "Waiting for telemetry" : $"{status.Mode} · {status.Light} · {status.Sensors}";
        string tip = $"PC Monitor · {state}"; icon.Text = tip[..Math.Min(63, tip.Length)];
        if (Environment.TickCount64 - lastStatusWrite > 15000) SaveStatus();
        } finally { ticking = false; }
    }
    private void HostChanged()
    {
        if (!sending) _ = Tick();
    }
    private async Task StartWorker()
    {
        if (starting || !desired) return; starting = true;
        await lifecycle.WaitAsync();
        try
        {
            if (!desired || closing.IsCancellationRequested) return;
            worker?.Dispose(); worker = null; pipe?.Dispose();
            string name = "PCMonitor-worker-" + Guid.NewGuid().ToString("N");
            pipe = new(name, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
            var connected = pipe.WaitForConnectionAsync(closing.Token);
            var start = new ProcessStartInfo(Path.Combine(AppContext.BaseDirectory, "PCMonitor.exe")) { UseShellExecute = false, CreateNoWindow = true };
            start.ArgumentList.Add("--worker"); start.ArgumentList.Add(name);
            worker = Process.Start(start) ?? throw new IOException("Could not start monitoring worker."); job.Add(worker);
            lastProgress = Environment.TickCount64; status = null; lastHost = null;
            await connected.WaitAsync(TimeSpan.FromSeconds(10));
            writer = new StreamWriter(pipe, leaveOpen: true) { AutoFlush = true };
            _ = ReadWorker(pipe); Log.Write("Worker started: " + worker.Id);
        }
        catch (Exception e) { Log.Write("Worker start failed: " + e.Message); await StopWorkerCore(); failures++; nextStart = Environment.TickCount64 + 5000; }
        finally { starting = false; lifecycle.Release(); }
    }
    private async Task ReadWorker(NamedPipeServerStream connection)
    {
        try
        {
            using var reader = new StreamReader(connection, leaveOpen: true);
            while (!closing.IsCancellationRequested)
            {
                var line = await reader.ReadLineAsync(closing.Token); if (line is null) break;
                if (!ReferenceEquals(pipe, connection)) break;
                status = JsonSerializer.Deserialize<WorkerStatus>(line); lastProgress = Environment.TickCount64;
                if (status?.Panel.StartsWith("connected") == true) failures = 0;
            }
        }
        catch (Exception e) when (e is IOException or ObjectDisposedException or OperationCanceledException or JsonException) { }
    }
    private async Task StopWorker()
    {
        stopping = true;
        await lifecycle.WaitAsync();
        try { await StopWorkerCore(); }
        finally { stopping = false; lifecycle.Release(); }
    }
    private async Task StopWorkerCore()
    {
        var old = worker; var oldWriter = writer; worker = null; writer = null;
        if (old is not null)
        {
            try
            {
                if (!old.HasExited)
                {
                    if (oldWriter is not null) try { await oldWriter.WriteLineAsync(JsonSerializer.Serialize(new WorkerMessage("stop"))).WaitAsync(TimeSpan.FromSeconds(1)); } catch (Exception) { }
                    try { await old.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5)); } catch (TimeoutException) { old.Kill(true); await old.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(3)); }
                }
            }
            catch (Exception e) when (e is InvalidOperationException or System.ComponentModel.Win32Exception or TimeoutException) { Log.Write("Worker shutdown: " + e.Message); }
            finally { old.Dispose(); }
        }
        try { oldWriter?.Dispose(); } catch (IOException) { }
        pipe?.Dispose(); pipe = null; status = null; lastHost = null;
    }
    private void StartMonitoring() { desired = true; nextStart = 0; failures = 0; _ = Tick(); }
    private async Task StopMonitoring() { desired = false; await StopWorker(); SaveStatus(); }
    private object StatusObject() => new { Version = "2.0.0", ProcessId = Environment.ProcessId, Monitoring = desired, WorkerPid = worker is { HasExited: false } ? worker.Id : (int?)null, Elevated = elevated, LastProgress = status, Settings = AppPaths.Config };
    private void SaveStatus()
    {
        try { var temp = AppPaths.Status + ".tmp"; File.WriteAllText(temp, JsonSerializer.Serialize(StatusObject(), Settings.Json)); File.Move(temp, AppPaths.Status, true); lastStatusWrite = Environment.TickCount64; } catch (IOException) { } catch (UnauthorizedAccessException) { }
    }
    private async Task ControlLoop()
    {
        while (!closing.IsCancellationRequested)
        {
            try
            {
                var security = new PipeSecurity();
                security.SetOwner(UserSid);
                security.AddAccessRule(new PipeAccessRule(UserSid, PipeAccessRights.FullControl, AccessControlType.Allow));
                // Explicit account ACL permits this user's ordinary CLI to control its
                // elevated tray. CurrentUserOnly also demands identical elevation.
                using var server = NamedPipeServerStreamAcl.Create(ControlPipe, PipeDirection.InOut, 1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous, 4096, 4096, security);
                await server.WaitForConnectionAsync(closing.Token);
                using var reader = new StreamReader(server, leaveOpen: true); using var response = new StreamWriter(server, leaveOpen: true) { AutoFlush = true };
                string? command = await reader.ReadLineAsync(closing.Token).AsTask().WaitAsync(TimeSpan.FromSeconds(2));
                switch (command)
                {
                    case "start": StartMonitoring(); break;
                    case "stop": await StopMonitoring(); break;
                    case "restart": await StopMonitoring(); StartMonitoring(); break;
                    case "blank": manualBlank = true; HostChanged(); break;
                    case "unblank": manualBlank = false; HostChanged(); break;
                    case "exit": await StopMonitoring(); break;
                    case "status": break;
                    default: await response.WriteLineAsync("{\"error\":\"unknown command\"}"); continue;
                }
                SaveStatus(); await response.WriteLineAsync(JsonSerializer.Serialize(StatusObject()));
                if (command == "exit") { ExitThread(); return; }
            }
            catch (Exception e) when (e is IOException or TimeoutException or OperationCanceledException or ObjectDisposedException) { }
        }
    }
    protected override void ExitThreadCore()
    {
        closing.Cancel(); timer.Stop(); timer.Dispose(); icon.Visible = false; icon.Dispose(); job.Dispose(); pipe?.Dispose(); host.Dispose(); Log.Write("Native tray exited."); base.ExitThreadCore();
    }
}
