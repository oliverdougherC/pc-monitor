// SPDX-License-Identifier: GPL-3.0-or-later
// Revision-C wire format adapted from turing-smart-screen-python, commit
// 2b33ab4f00a096916dd6a1174441a53a7ec33b03. See THIRD-PARTY-NOTICES.md.
using System.Buffers.Binary;
using System.IO.Ports;
using System.Text;
using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace PCMonitor;

public interface IPanelWire : IDisposable { void Write(byte[] bytes); byte[] Read(int count); void FlushInput(); }
public sealed class SerialWire : IPanelWire
{
    private readonly SerialPort serial;
    public SerialWire(string port, bool wake = false)
    {
        serial = new(port, 115200) { ReadTimeout = 1500, WriteTimeout = 1500, DtrEnable = true, Handshake = wake ? Handshake.RequestToSend : Handshake.None };
        if (!wake) serial.RtsEnable = true;
        try { serial.Open(); } catch { serial.Dispose(); throw; }
    }
    public void Write(byte[] data) => serial.Write(data, 0, data.Length);
    public byte[] Read(int count)
    {
        var data = new byte[count]; int offset = 0;
        while (offset < count)
        {
            try { int n = serial.Read(data, offset, count - offset); if (n == 0) break; offset += n; }
            catch (TimeoutException) { break; }
        }
        return data[..offset];
    }
    public void FlushInput() => serial.DiscardInBuffer();
    public void Dispose() => serial.Dispose();
}

public static class PanelProtocol
{
    public const int Width = 800, Height = 480;
    public static string Reply(byte[] bytes) => Encoding.ASCII.GetString(bytes).TrimEnd('\0', '\r', '\n', ' ');
    public static (bool Resend, uint RenderCount) Status(byte[] bytes)
    {
        string reply = Reply(bytes);
        var fields = reply.Split('|');
        if (fields.Length != 2 || !fields[0].StartsWith("needReSend:", StringComparison.OrdinalIgnoreCase) || !fields[1].StartsWith("renderCnt:", StringComparison.Ordinal)
            || !uint.TryParse(fields[0].AsSpan(11), out uint resend) || resend > 1 || !uint.TryParse(fields[1].AsSpan(10), out uint rendered))
            throw new IOException("Unrecognized panel status: " + reply);
        return (resend != 0, rendered);
    }
    public static byte[] Pad(ReadOnlySpan<byte> bytes, byte padding = 0)
    {
        var data = new byte[((bytes.Length + 249) / 250) * 250];
        if (padding != 0) Array.Fill(data, padding); bytes.CopyTo(data); return data;
    }
    public static byte[] Escape(ReadOnlySpan<byte> bytes)
    {
        var data = new byte[bytes.Length + Math.Max(0, (bytes.Length - 1) / 249)]; int dest = 0;
        for (int i = 0; i < bytes.Length; i++) { if (i > 0 && i % 249 == 0) data[dest++] = 0; data[dest++] = bytes[i]; }
        return data;
    }
    public static (byte[] Header, byte[] Data) Partial(byte[] bgra, Rectangle region, int rom, uint sequence)
    {
        if (bgra.Length != Width * Height * 4 || region.Width <= 0 || region.Height <= 0 || region.Left < 0 || region.Top < 0 || region.Right > Width || region.Bottom > Height) throw new ArgumentOutOfRangeException(nameof(region));
        int channels = rom > 88 ? 4 : 3;
        var raw = new byte[region.Height * (5 + region.Width * channels)]; int p = 0;
        for (int y = region.Top; y < region.Bottom; y++)
        {
            int address = y * Width + region.Left;
            raw[p++] = (byte)(address >> 16); raw[p++] = (byte)(address >> 8); raw[p++] = (byte)address;
            raw[p++] = (byte)(region.Width >> 8); raw[p++] = (byte)region.Width;
            for (int x = region.Left; x < region.Right; x++)
            {
                int offset = (y * Width + x) * 4;
                raw[p++] = bgra[offset]; raw[p++] = bgra[offset + 1]; raw[p++] = bgra[offset + 2]; if (channels == 4) raw[p++] = 255;
            }
        }
        var header = new byte[14]; new byte[] { 0xcc, 0xef, 0x69, 0 }.CopyTo(header, 0);
        int size = raw.Length + 2; header[4] = (byte)(size >> 16); header[5] = (byte)(size >> 8); header[6] = (byte)size;
        BinaryPrimitives.WriteUInt32BigEndian(header.AsSpan(10), sequence);
        var encoded = raw.Length > 250 ? Escape(raw) : raw;
        return (Pad(header), Pad([.. encoded, 0xef, 0x69]));
    }
    public static byte[] Rotate180(byte[] bgra)
    {
        if (bgra.Length != Width * Height * 4) throw new ArgumentException("Invalid framebuffer.");
        var result = new byte[bgra.Length];
        for (int p = 0; p < bgra.Length; p += 4) Buffer.BlockCopy(bgra, p, result, bgra.Length - p - 4, 4);
        return result;
    }
    public static List<Rectangle> Changes(byte[]? previous, byte[] frame)
    {
        if (frame.Length != Width * Height * 4) throw new ArgumentException("Invalid framebuffer.");
        if (previous is null) return [new(0, 0, Width, Height)];
        var bands = new List<Rectangle>(); int start = -1, last = -1, minX = Width, maxX = -1, changedPixels = 0;
        for (int y = 0; y < Height; y++)
        {
            int left = Width, right = -1;
            for (int x = 0; x < Width; x++)
            {
                int p = (y * Width + x) * 4;
                if (frame[p] != previous[p] || frame[p + 1] != previous[p + 1] || frame[p + 2] != previous[p + 2]) { left = Math.Min(left, x); right = x; changedPixels++; }
            }
            if (right < 0) continue;
            if (start >= 0 && y - last > 7) { bands.Add(Rectangle.FromLTRB(minX, start, maxX + 1, last + 1)); start = -1; minX = Width; maxX = -1; }
            if (start < 0) start = y; last = y; minX = Math.Min(minX, left); maxX = Math.Max(maxX, right);
        }
        if (start >= 0) bands.Add(Rectangle.FromLTRB(minX, start, maxX + 1, last + 1));
        if (bands.Count > 12 || changedPixels > Width * Height * .35) return [new(0, 0, Width, Height)];
        return bands;
    }
}

public sealed class Panel : IDisposable
{
    private IPanelWire? wire; private byte[]? previous; private int brightness = -1; private uint sequence; private int rom = 88;
    private long nextAttempt; private bool blanked;
    private long failedSince, lastUsbRestart;
    private string? confirmedInstance;
    private bool wakeBeforeRetry;
    private bool resumeRecovery;
    private bool partialUpdates = true;
    public long FramesPresented { get; private set; }
    public DateTimeOffset? LastFrame { get; private set; }
    public string UpdateMode => partialUpdates ? "partial" : "full (partial updates unverified)";
    public string State { get; private set; } = "disconnected";
    public bool Connected => wire != null;
    public Panel()
    {
        try
        {
            var file = Path.Combine(AppPaths.Data, "panel-identity.txt");
            if (File.Exists(file) && new FileInfo(file).Length < 1024) confirmedInstance = File.ReadAllText(file).Trim();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException) { }
    }
    public Panel(IPanelWire transport, int firmware = 88) { wire = transport; rom = firmware; State = "connected test transport"; }
    public static List<(string Port, bool Awake, string InstanceId)> Devices()
    {
        var found = new List<(string, bool, string)>();
        using var usb = Registry.LocalMachine.OpenSubKey(@"SYSTEM\CurrentControlSet\Enum\USB");
        if (usb is null) return found;
        foreach (var id in usb.GetSubKeyNames())
        {
            var u = id.ToUpperInvariant(); bool awake = u.StartsWith("VID_1D6B&PID_0106") || u.StartsWith("VID_1D6B&PID_0121") || u.StartsWith("VID_0525&PID_A4A7");
            if (!awake && !u.StartsWith("VID_1A86&PID_CA21")) continue;
            using var device = usb.OpenSubKey(id); if (device is null) continue;
            foreach (var instance in device.GetSubKeyNames())
            {
                using var parameters = device.OpenSubKey(instance + @"\Device Parameters");
                string instanceId = @"USB\" + id + @"\" + instance;
                if (parameters?.GetValue("PortName") is string port && SerialPort.GetPortNames().Contains(port) && Present(instanceId)) found.Add((port, awake, instanceId));
            }
        }
        return found.Distinct().ToList();
    }
    public void RetryNow() => nextAttempt = 0;
    public void NoteResume() { Dispose(); resumeRecovery = true; lastUsbRestart = 0; RetryNow(); }
    public static string? VerifiedComposite(string child, string parent)
    {
        string upper = child.ToUpperInvariant(); int mi = upper.IndexOf("&MI_", StringComparison.Ordinal);
        if (mi < 0 || !new[] { @"USB\VID_1D6B&PID_0106&MI_", @"USB\VID_1D6B&PID_0121&MI_", @"USB\VID_0525&PID_A4A7&MI_" }.Any(upper.StartsWith)) return null;
        string prefix = upper[..mi] + @"\";
        return parent.StartsWith(prefix, StringComparison.OrdinalIgnoreCase) && parent.Split('\\').Length == 3 ? parent : null;
    }
    public bool Connect(Settings config)
    {
        if (Connected) return true;
        if (Environment.TickCount64 < nextAttempt) return false;
        nextAttempt = Environment.TickCount64 + 5000;
        try
        {
            var devices = Devices();
            if (wakeBeforeRetry)
            {
                wakeBeforeRetry = false;
                var wakingPort = devices.FirstOrDefault(x => !x.Awake).Port;
                if (wakingPort is not null) { try { using var waking = new SerialWire(wakingPort, true); Log.Write("Opened panel wake interface after failed I/O."); } catch (Exception e) when (e is IOException or UnauthorizedAccessException or InvalidOperationException) { Log.Write("Panel wake interface: " + e.Message); } }
            }
            if (failedSince > 0 && (resumeRecovery || Environment.TickCount64 - failedSince > 15000) && (lastUsbRestart == 0 || Environment.TickCount64 - lastUsbRestart > 60000) && confirmedInstance is not null && devices.Any(d => d.Awake && d.InstanceId.Equals(confirmedInstance, StringComparison.OrdinalIgnoreCase)))
            {
                lastUsbRestart = Environment.TickCount64;
                resumeRecovery = false;
                RestartInterface(confirmedInstance);
                nextAttempt = Environment.TickCount64 + 2000; return false;
            }
            string? port = config.Port == "AUTO" ? devices.FirstOrDefault(x => x.Awake).Port : config.Port;
            if (port is null)
            {
                var wake = devices.FirstOrDefault(x => !x.Awake).Port;
                if (wake != null) { using var waking = new SerialWire(wake, true); }
                State = "waiting for panel USB"; return false;
            }
            wire = new SerialWire(port); wire.FlushInput();
            Command([0x01, 0xef, 0x69, 0, 0, 0, 1, 0, 0, 0, 0xc5, 0xd3]);
            var identity = PanelProtocol.Reply(wire.Read(23));
            if (!identity.StartsWith("chs_5inch")) throw new IOException("Panel identity was not recognized: " + identity.Replace('\0', ' '));
            if (int.TryParse(identity.Split('.').LastOrDefault(), out var version) && version is >= 80 and <= 100) rom = version;
            wire.FlushInput();
            Command([0x79, 0xef, 0x69, 0, 0, 0, 1]);
            Command([0x96, 0xef, 0x69, 0, 0, 0, 1], read: true);
            Command([0x7d, 0xef, 0x69, 0, 0, 0, 5, 0, 0, 0, 0x2d, 0, 0, 0, 0]);
            previous = null; brightness = -1; blanked = false; failedSince = 0; resumeRecovery = false; sequence = 0; partialUpdates = true;
            confirmedInstance = devices.FirstOrDefault(d => d.Port == port && d.Awake).InstanceId;
            if (confirmedInstance is not null)
            {
                try { File.WriteAllText(Path.Combine(AppPaths.Data, "panel-identity.txt"), confirmedInstance); } catch (Exception e) when (e is IOException or UnauthorizedAccessException) { Log.Write("Could not persist panel identity: " + e.Message); }
            }
            SetBrightness(config.BlankBrightness); State = $"connected {port} ROM {rom}"; Log.Write(State); return true;
        }
        catch (Exception e) when (e is IOException or TimeoutException or UnauthorizedAccessException or InvalidOperationException)
        { Fail(e); return false; }
    }
    private byte[] Command(byte[] command, bool read = false, byte padding = 0)
    {
        wire!.Write(PanelProtocol.Pad(command, padding));
        if (!read) return [];
        var response = wire.Read(1024);
        if (response.Length == 0)
        {
            // ROM 88 occasionally misses a status query while committing pixels.
            // Retry only the idempotent query, never a partially written image.
            if (command[0] != 0xcf) throw new TimeoutException("Panel did not acknowledge command.");
            wire.Write(PanelProtocol.Pad(command));
            response = wire.Read(1024);
            if (response.Length == 0) throw new TimeoutException("Panel did not acknowledge repeated status query.");
        }
        return response;
    }
    public void SetBrightness(int percent)
    {
        if (brightness == percent) return;
        Command([0x7b, 0xef, 0x69, 0, 0, 0, 1, 0, 0, 0, (byte)(percent * 255 / 100)]); brightness = percent;
    }
    public void Push(byte[] frame, bool force = false)
    {
        if (force) previous = null;
        var changes = PanelProtocol.Changes(previous, frame);
        if (changes.Count == 0) return;
        if (!partialUpdates || changes.Any(r => r.Width == 800 && r.Height == 480)) SendFull(frame);
        else foreach (var region in changes)
        {
            uint sent = sequence++;
            var packet = PanelProtocol.Partial(frame, region, rom, sent); wire!.Write(packet.Header); wire.Write(packet.Data);
            var status = PanelProtocol.Status(Command([0xcf, 0xef, 0x69, 0, 0, 0, 1], read: true));
            if (status.Resend || status.RenderCount != sent)
            {
                // A reply with an unexpected counter cannot confirm this update.
                // Some firmware draws pixels without advancing its counter, so
                // conservatively use explicitly acknowledged complete frames.
                partialUpdates = false;
                Log.Write($"Panel could not confirm partial update {sent} (resend={status.Resend}, rendered={status.RenderCount}); switching to complete frames.");
                SendFull(frame); break;
            }
        }
        previous = frame;
        FramesPresented++; LastFrame = DateTimeOffset.UtcNow;
    }
    private void SendFull(byte[] frame)
    {
        Command([0x86, 0xef, 0x69, 0, 0, 0, 1]); Command([0x2c], padding: 0x2c);
        Command([0xc8, 0xef, 0x69, 0, 0x17, 0x70, 0x0e, 0x10]);
        wire!.Write(PanelProtocol.Pad(PanelProtocol.Escape(frame)));
        string reply = PanelProtocol.Reply(wire.Read(1024));
        if (reply is not ("full_png_sucess" or "full_png_success")) throw new IOException("Panel rejected full frame: " + reply);
        var status = PanelProtocol.Status(Command([0xcf, 0xef, 0x69, 0, 0, 0, 1], read: true));
        if (status.Resend) throw new IOException("Panel requested a full-frame resend.");
        sequence = 0;
    }
    public void Present(byte[]? frame, LightPlan plan)
    {
        if (!Connected) return;
        try
        {
            if (plan.Blank)
            {
                if (!blanked) { SetBrightness(plan.Brightness); var black = new byte[800 * 480 * 4]; for (int i = 3; i < black.Length; i += 4) black[i] = 255; Push(black, true); blanked = true; }
            }
            else if (frame is not null)
            {
                Push(frame, blanked); SetBrightness(plan.Brightness); blanked = false;
            }
        }
        catch (Exception e) when (e is IOException or TimeoutException or UnauthorizedAccessException or InvalidOperationException) { Fail(e); }
    }
    private void Fail(Exception e) { State = "disconnected: " + e.Message; Log.Write(State); if (failedSince == 0) failedSince = Environment.TickCount64; wakeBeforeRetry = true; Dispose(); if (resumeRecovery) RetryNow(); }
    private static bool Present(string instance)
    {
        return CM_Locate_DevNode(out uint dev, instance, 0) == 0 && CM_Get_DevNode_Status(out uint status, out uint problem, dev, 0) == 0 && problem == 0 && (status & 8) != 0;
    }
    private static void RestartInterface(string instance)
    {
        // An identity learned from a successful HELLO, checked against the live
        // device list immediately before restart. Never ascend to a hub or disable.
        if (!instance.StartsWith(@"USB\VID_1D6B&PID_0106", StringComparison.OrdinalIgnoreCase) && !instance.StartsWith(@"USB\VID_1D6B&PID_0121", StringComparison.OrdinalIgnoreCase) && !instance.StartsWith(@"USB\VID_0525&PID_A4A7", StringComparison.OrdinalIgnoreCase)) return;
        try
        {
            string target = instance;
            if (CM_Locate_DevNode(out uint dev, instance, 0) == 0 && CM_Get_Parent(out uint parent, dev, 0) == 0)
            {
                var id = new StringBuilder(1024);
                if (CM_Get_Device_ID(parent, id, id.Capacity, 0) == 0 && VerifiedComposite(instance, id.ToString()) is string composite && Present(composite)) target = composite;
            }
            var start = new ProcessStartInfo(Path.Combine(Environment.SystemDirectory, "pnputil.exe")) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardOutput = true, RedirectStandardError = true };
            start.ArgumentList.Add("/restart-device"); start.ArgumentList.Add(target);
            using var process = Process.Start(start)!;
            var output = process.StandardOutput.ReadToEndAsync(); var error = process.StandardError.ReadToEndAsync();
            if (!process.WaitForExit(5000)) { process.Kill(true); Log.Write("Panel interface restart timed out."); return; }
            Log.Write($"Restarted verified panel USB device {target}: exit {process.ExitCode}; {output.GetAwaiter().GetResult().Trim()} {error.GetAwaiter().GetResult().Trim()}");
        }
        catch (Exception e) when (e is IOException or System.ComponentModel.Win32Exception or InvalidOperationException) { Log.Write("Panel interface restart refused: " + e.Message); }
    }
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode, EntryPoint = "CM_Locate_DevNodeW")] private static extern uint CM_Locate_DevNode(out uint dev, string instance, uint flags);
    [DllImport("cfgmgr32.dll")] private static extern uint CM_Get_DevNode_Status(out uint status, out uint problem, uint dev, uint flags);
    [DllImport("cfgmgr32.dll")] private static extern uint CM_Get_Parent(out uint parent, uint dev, uint flags);
    [DllImport("cfgmgr32.dll", CharSet = CharSet.Unicode, EntryPoint = "CM_Get_Device_IDW")] private static extern uint CM_Get_Device_ID(uint dev, StringBuilder id, int size, uint flags);
    public void Dispose() { try { wire?.Dispose(); } catch (IOException) { } wire = null; previous = null; brightness = -1; }
}
