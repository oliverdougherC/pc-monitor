using System.Drawing;
using System.Security.Cryptography;
using System.Text.Json;
using PCMonitor;

Environment.SetEnvironmentVariable("PCMON_DATA_DIR", Path.Combine(AppContext.BaseDirectory, "TestData"));
AppPaths.Initialize();
int passed = 0;
void Check(string title, bool condition) { if (!condition) throw new Exception("FAIL: " + title); passed++; Console.WriteLine("PASS " + title); }
void Throws(string title, Action action) { try { action(); } catch (Exception e) when (e is InvalidDataException or ArgumentException or JsonException) { Check(title, true); return; } throw new Exception("FAIL: " + title); }

new Settings().Validate();
Throws("rotation is explicit", () => new Settings { Rotation = 90 }.Validate());
Throws("NaN interval refused", () => new Settings { IdleIntervalSeconds = double.NaN }.Validate());
Throws("invalid port refused", () => new Settings { Port = "COM4;evil" }.Validate());
Throws("bad brightness refused", () => new Settings { BlankBrightness = -1 }.Validate());
Throws("unknown settings refused", () => JsonSerializer.Deserialize<Settings>("{\"Unknown\":1}", Settings.Json));
var c = new Settings();
Check("suspend outranks playing", LightPolicy.Decide(c, new(Suspended: true), true, false).Blank);
Check("monitor off stays blank", LightPolicy.Decide(c, new(MonitorOn: false), true, false).Reason == "monitor-off");
Check("manual blank works even when display following is disabled", LightPolicy.Decide(c with { FollowDisplay = false }, new(ManualBlank: true), true, false).Reason == "manual");
Check("locked app stays running and blank", LightPolicy.Decide(c, new(Locked: true), true, false).Reason == "locked");
Check("controller game holds idle timeout", !LightPolicy.Decide(c, new(IdleSeconds: 9000), true, false).Blank);
Check("zero idle timer disables blanking", !LightPolicy.Decide(c with { BlankAfterMinutes = 0 }, new(IdleSeconds: 9000), false, false).Blank);
Check("night dims brightness", LightPolicy.Decide(c, new(), false, true).Brightness == 25);
Check("black-frame brightness remains minimal", LightPolicy.Decide(c, new(MonitorOn: false), false, false).Brightness == 1);
Check("missing power is unavailable", PowerModel.Estimate(new(), c).Watts is null);
Check("one missing component is partial", PowerModel.Estimate(new Sample { CpuPower = 50 }, c).Status == "partial");
Check("modelled inputs are labelled", PowerModel.Estimate(new Sample { CpuLoad = 30, GpuPower = 50 }, c).Status == "modelled input");
Check("held inputs are stale", PowerModel.Estimate(new Sample { CpuPower = 30, GpuPower = 50, Held = true }, c).Status == "stale");
Check("network uses bits", Renderer.Rate(125e6 * 8, true) == "1.0 Gbps");
Check("disk uses bytes", Renderer.Rate(125e6) == "125.0 MB/s");

var frame = new byte[800 * 480 * 4];
for (int y = 0; y < 480; y++) for (int x = 0; x < 800; x++) { int p = (y * 800 + x) * 4; frame[p] = (byte)(x + y); frame[p + 1] = (byte)y; frame[p + 2] = (byte)x; frame[p + 3] = 255; }
var rotated = PanelProtocol.Rotate180(frame);
Check("rotation moves top-left to bottom-right", frame.AsSpan(0, 4).SequenceEqual(rotated.AsSpan(rotated.Length - 4)));
Check("rotation is reversible", PanelProtocol.Rotate180(rotated).SequenceEqual(frame));
Check("unchanged pixels send nothing", PanelProtocol.Changes(frame, frame).Count == 0);
var one = (byte[])frame.Clone(); one[(25 * 800 + 27) * 4] ^= 1;
Check("one changed pixel sends its region", PanelProtocol.Changes(frame, one).SequenceEqual(new[] { new Rectangle(27, 25, 1, 1) }));
Check("first frame is full", PanelProtocol.Changes(null, frame).Single() == new Rectangle(0, 0, 800, 480));
Throws("out-of-bounds update refused", () => PanelProtocol.Partial(frame, new(799, 479, 2, 1), 88, 0));
Check("249-byte escape boundary", PanelProtocol.Escape(new byte[249]).Length == 249 && PanelProtocol.Escape(new byte[250]).Length == 251);
Check("padded identity preserves firmware version", PanelProtocol.Reply(System.Text.Encoding.ASCII.GetBytes("chs_5inch.dev1_rom1.89\0")).EndsWith(".89"));
Check("captured firmware status decoded", PanelProtocol.Status(Convert.FromHexString("6E656564526553656E643A307C72656E646572436E743A3000000000000000000000")) == (false, 0u));
Check("firmware resend request decoded", PanelProtocol.Status(System.Text.Encoding.ASCII.GetBytes("needResend:1|renderCnt:0")) == (true, 0u));
try { PanelProtocol.Status([0, 0, 0]); throw new Exception("Malformed status accepted"); } catch (IOException) { Check("nonempty garbage is not an acknowledgement", true); }
Check("panel composite parent accepted", PCMonitor.Panel.VerifiedComposite(@"USB\VID_1D6B&PID_0106&MI_00\node", @"USB\VID_1D6B&PID_0106\20080411") is not null);
Check("shared USB hub is never a restart target", PCMonitor.Panel.VerifiedComposite(@"USB\VID_1D6B&PID_0106&MI_00\node", @"USB\VID_1A40&PID_0101\hub") is null);
Check("unrelated parent is never a restart target", PCMonitor.Panel.VerifiedComposite(@"USB\VID_1D6B&PID_0106&MI_00\node", @"USB\VID_0525&PID_A4A7\other") is null);

string Hash(byte[] b) => Convert.ToHexStringLower(SHA256.HashData(b));
var fixtures = JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "revision-c.json"))).RootElement;
Check("full image matches pinned Python driver", Hash(PanelProtocol.Pad(PanelProtocol.Escape(frame))) == fixtures.GetProperty("FullHash").GetString());
foreach (var item in fixtures.GetProperty("Partial").EnumerateArray())
{
    var r = item.GetProperty("Region").EnumerateArray().Select(n => n.GetInt32()).ToArray(); int rom = item.GetProperty("Rom").GetInt32();
    var packet = PanelProtocol.Partial(frame, new(r[0], r[1], r[2], r[3]), rom, 17);
    Check($"ROM {rom} header {string.Join(',', r)}", Hash(packet.Header) == item.GetProperty("HeaderHash").GetString());
    Check($"ROM {rom} data {string.Join(',', r)}", Hash(packet.Data) == item.GetProperty("DataHash").GetString());
}

var columns = FrameCapture.Csv("\"game, example.exe\",123,1000,16.7,1,12");
Check("quoted CSV names retained", columns[0] == "game, example.exe" && columns.Count == 6);
var header = new Dictionary<string, int> { ["Application"] = 0, ["ProcessID"] = 1, ["CPUStartQPCTimeInMs"] = 2, ["MsBetweenPresents"] = 3, ["SwapChainAddress"] = 4, ["MsGPUBusy"] = 5 };
Check("valid present row parsed", FrameCapture.Parse(columns, header)?.Pid == 123);
Check("malformed timestamp rejected", FrameCapture.Parse(["game", "123", "NaN"], header) is null);
Check("truncated row rejected", FrameCapture.Parse(["game", "123", "1000"], header) is null);
var rows = Enumerable.Range(0, 601).Select(i => new PresentRow(123, "game.exe", "1", i * (1000.0 / 60), 1000.0 / 60, 80)).ToArray();
Check("60 FPS has no extra counted frame", Math.Abs(FrameCapture.Summarize(rows).Fps!.Value - 60) < .001);
Check("percentiles preserve constant frametimes", Math.Abs(FrameCapture.Summarize(rows).Low01!.Value - 60) < .001);
Check("held present measurements labelled", FrameCapture.Summarize(rows, 3).Held);
Throws("malformed CloudStore rejected", () => NightLight.DecodePayload([1, 2, 3]));
var warm = NightLight.TemperatureGains(2700); Check("night warmth reduces blue", warm.R > warm.B && warm.R <= 1 && warm.B >= 0);
foreach (var item in JsonDocument.Parse(File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "Fixtures", "night.json"))).RootElement.EnumerateArray())
{
    var fields = NightLight.DecodePayload(Convert.FromBase64String(item.GetProperty("Blob").GetString()!));
    Check("captured Windows night state " + item.GetProperty("Name").GetString(), fields.ContainsKey(0) == item.GetProperty("On").GetBoolean());
}
Console.WriteLine($"\n{passed} assertions passed.");

var fake = new FakeWire();
using (var panel = new PCMonitor.Panel(fake))
{
    panel.Present(null, new(true, 1, "monitor-off")); int writes = fake.Writes.Count;
    Check("blank sends black frame and minimal brightness", writes > 0 && fake.Writes.Any(b => b[0] == 0x7b && b[10] == 2));
    panel.Present(null, new(true, 1, "locked"));
    Check("remaining blank sends no repeated traffic", fake.Writes.Count == writes);
    Check("blank never powers off or resets firmware", !fake.Writes.Any(b => b.Length > 3 && b[1] == 0xef && b[2] == 0x69 && b[0] is 0x83 or 0x84));
    panel.Present(frame, new(false, 45, "lit"));
    Check("wake restores current image then brightness", fake.Writes[^1][0] == 0x7b && fake.Writes[^1][10] == 114);
    int before = fake.Writes.Count; panel.Present(frame, new(false, 45, "lit"));
    Check("unchanged image and brightness send no bytes", fake.Writes.Count == before);
    Check("only confirmed changed frames counted", panel.FramesPresented == 2 && panel.LastFrame.HasValue);
    fake.EmptyQueries = 1; panel.Present(one, new(false, 45, "lit"));
    Check("missed status query is retried without resending pixels", panel.Connected && fake.Writes[^1][0] == 0xcf && fake.Writes[^2][0] == 0xcf);
    fake.FailRead = true; panel.Present(frame, new(false, 45, "lit"));
    Check("failed acknowledgement retires connection", !panel.Connected && fake.Disposed);
}
foreach (bool resend in new[] { false, true })
{
    var rejected = new FakeWire { IgnorePartials = !resend, ResendPartials = resend };
    using var panel = new PCMonitor.Panel(rejected);
    panel.Present(frame, new(false, 45, "lit"));
    panel.Present(one, new(false, 45, "lit"));
    panel.Present(frame, new(false, 45, "lit"));
    Check(resend ? "resend request recovers current full frame" : "stalled firmware counter recovers current full frame", panel.Connected && panel.UpdateMode.StartsWith("full") && rejected.FullFrames >= 2 && panel.FramesPresented == 3);
    int partials = rejected.PartialFrames;
    panel.Present(one, new(false, 45, "lit"));
    Check("rejected partial path stays disabled on connection", rejected.PartialFrames == partials && rejected.FullFrames >= 3);
}
var badFull = new FakeWire { RejectFull = true };
using (var panel = new PCMonitor.Panel(badFull))
{
    panel.Present(frame, new(false, 45, "lit"));
    Check("nonempty full-frame failure is never cached as success", !panel.Connected && panel.FramesPresented == 0 && panel.LastFrame is null);
}
Console.WriteLine($"Final: {passed} assertions passed.");

sealed class FakeWire : IPanelWire
{
    public List<byte[]> Writes { get; } = [];
    public bool FailRead { get; set; }
    public bool Disposed { get; private set; }
    public int EmptyQueries { get; set; }
    public bool IgnorePartials { get; init; }
    public bool ResendPartials { get; init; }
    public bool RejectFull { get; init; }
    public int FullFrames { get; private set; }
    public int PartialFrames { get; private set; }
    private uint rendered;
    private bool partial;
    public void Write(byte[] bytes)
    {
        if (Disposed) throw new IOException("closed"); Writes.Add(bytes);
        if (bytes.Length == 250 && bytes[0] == 0x86 && bytes[1] == 0xef) { FullFrames++; rendered = 0; partial = false; }
        if (bytes.Length == 250 && bytes[0] == 0xcc && bytes[1] == 0xef) { PartialFrames++; partial = true; if (!IgnorePartials && !ResendPartials) rendered = System.Buffers.Binary.BinaryPrimitives.ReadUInt32BigEndian(bytes.AsSpan(10)); }
    }
    public byte[] Read(int count)
    {
        if (FailRead) throw new IOException("ack lost");
        if (Writes[^1][0] == 0xcf)
        {
            if (EmptyQueries > 0) { EmptyQueries--; return []; }
            return System.Text.Encoding.ASCII.GetBytes($"needResend:{(partial && ResendPartials ? 1 : 0)}|renderCnt:{rendered}\0");
        }
        return System.Text.Encoding.ASCII.GetBytes(RejectFull ? "full_png_failed\0" : "full_png_sucess\0");
    }
    public void FlushInput() { }
    public void Dispose() => Disposed = true;
}
