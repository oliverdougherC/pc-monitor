using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace PCMonitor;

public sealed class NightLight
{
    private const string Root = @"Software\Microsoft\Windows\CurrentVersion\CloudStore\Store\DefaultAccount\Current\default$windows.data.bluelightreduction.";
    private long next;
    private bool established;
    public bool On { get; private set; }
    public int Temperature { get; private set; } = 2700;
    public string State { get; private set; } = "unknown";
    public (double R, double G, double B) Gains { get; private set; } = (1, 1, 1);
    public void Refresh(Settings c)
    {
        if (Environment.TickCount64 < next) return; next = Environment.TickCount64 + 3000;
        if (c.NightMode == "off") { On = false; Gains = (1, 1, 1); State = "off (manual)"; return; }
        if (c.NightMode == "on") { On = true; Temperature = c.NightTemperature == 0 ? 2700 : c.NightTemperature; Gains = TemperatureGains(Temperature); State = "on (manual)"; return; }
        try
        {
            var state = Read("bluelightreductionstate");
            if (state is null || !state.ContainsKey(10))
            {
                if (established) { State = "held: Windows night state unavailable"; return; }
                var rampOnly = Gamma();
                if (rampOnly is { } g) { On = g.R - g.B > .12; Gains = On ? g : (1, 1, 1); State = On ? "on (gamma)" : "off (gamma)"; }
                return;
            }
            bool windowsOn = state.ContainsKey(0);
            // Reading settings cannot undo a confirmed OFF or lose the previous warmth.
            if (windowsOn)
            {
                try { var settings = Read("settings"); if (settings?.GetValueOrDefault(40) is long k && k is >= 1000 and <= 10000) Temperature = (int)k; } catch (Exception e) when (e is IOException or InvalidDataException or UnauthorizedAccessException) { }
            }
            if (c.NightTemperature > 0) Temperature = c.NightTemperature;
            var ramp = Gamma();
            On = windowsOn || ramp is { } gains && gains.R - gains.B > .12;
            Gains = windowsOn ? TemperatureGains(Temperature) : On && ramp.HasValue ? ramp.Value : (1, 1, 1);
            established = true; State = windowsOn ? "on (Windows)" : On ? "on (gamma)" : "off (Windows)";
        }
        catch (Exception e) when (e is IOException or InvalidDataException or UnauthorizedAccessException or System.Security.SecurityException)
        { State = "held: " + e.Message; }
    }
    private static Dictionary<int, object>? Read(string suffix)
    {
        using var key = Registry.CurrentUser.OpenSubKey(Root + suffix + @"\windows.data.bluelightreduction." + suffix);
        if (key?.GetValue("Data") is not byte[] bytes) return null;
        return DecodePayload(bytes);
    }
    public static Dictionary<int, object> DecodePayload(byte[] bytes)
    {
        if (bytes.Length < 4 || !bytes.AsSpan(0, 4).SequenceEqual(new byte[] { 0x43, 0x42, 1, 0 })) throw new InvalidDataException("Unknown CloudStore format.");
        var top = new Bond(bytes, 4).Fields(0, true);
        if (top.GetValueOrDefault(1) is not Dictionary<int, object> container) throw new InvalidDataException("CloudStore payload missing.");
        object? node = container.GetValueOrDefault(1);
        for (int i = 0; i < 2 && node is Dictionary<int, object> d; i++) node = d.GetValueOrDefault(1);
        if (node is not List<object> list) throw new InvalidDataException("CloudStore bytes missing.");
        var inner = list.Select(v => unchecked((byte)Convert.ToInt64(v))).ToArray();
        if (inner.Length < 4 || !inner.AsSpan(0, 4).SequenceEqual(new byte[] { 0x43, 0x42, 1, 0 })) throw new InvalidDataException("Unknown night payload.");
        return new Bond(inner, 4).Fields(0);
    }
    private sealed class Bond(byte[] bytes, int position)
    {
        private int pos = position;
        private byte Byte() => pos < bytes.Length ? bytes[pos++] : throw new InvalidDataException("Truncated night state.");
        private ulong Varint()
        {
            ulong value = 0;
            for (int bits = 0; bits < 64; bits += 7) { byte b = Byte(); value |= (ulong)(b & 127) << bits; if (b < 128) return value; }
            throw new InvalidDataException("Oversized night state integer.");
        }
        public Dictionary<int, object> Fields(int depth, bool tolerant = false)
        {
            if (depth > 16) throw new InvalidDataException("Nested night state exceeds limit.");
            var result = new Dictionary<int, object>();
            while (pos < bytes.Length)
            {
                byte h = Byte(); int type = h & 31; if (type == 0) return result; if (type == 1) continue;
                int id = h >> 5; if (id == 6) id = Byte(); else if (id == 7) { int lo = Byte(); id = lo | Byte() << 8; }
                result[id] = Value(type, depth);
            }
            if (tolerant) return result; throw new InvalidDataException("Unterminated night state.");
        }
        private object Value(int type, int depth)
        {
            if (depth > 16) throw new InvalidDataException("Nested night state exceeds limit.");
            if (type == 2) return Byte() != 0;
            if (type == 3) return (long)Byte();
            if (type == 14) return (long)unchecked((sbyte)Byte());
            if (type is 4 or 5 or 6) return unchecked((long)Varint());
            if (type is 15 or 16 or 17) { var n = Varint(); return (long)(n >> 1) ^ -((long)n & 1); }
            if (type == 10) return Fields(depth + 1);
            if (type is 11 or 12)
            {
                int elem = Byte(); ulong count = Varint(); if (count > 4096) throw new InvalidDataException("Oversized night state list.");
                var list = new List<object>(); for (ulong i = 0; i < count; i++) list.Add(Value(elem, depth + 1)); return list;
            }
            if (type is 7 or 8) { int count = type == 7 ? 4 : 8; var b = new byte[count]; for (int i = 0; i < count; i++) b[i] = Byte(); return b; }
            if (type is 9 or 18) { var n = Varint(); if (n > 4096) throw new InvalidDataException("Oversized string."); for (ulong i = 0; i < n; i++) Byte(); return ""; }
            throw new InvalidDataException("Unknown night state field.");
        }
    }
    public static (double R, double G, double B) TemperatureGains(int kelvin)
    {
        double t = Math.Clamp(kelvin, 1000, 10000) / 100.0;
        double r = t <= 66 ? 255 : 329.698727446 * Math.Pow(t - 60, -.1332047592);
        double g = t <= 66 ? 99.4708025861 * Math.Log(t) - 161.1195681661 : 288.1221695283 * Math.Pow(t - 60, -.0755148492);
        double b = t >= 66 ? 255 : t <= 19 ? 0 : 138.5177312231 * Math.Log(t - 10) - 305.0447927307;
        return (Math.Clamp(r / 255, 0, 1), Math.Clamp(g / 255, 0, 1), Math.Clamp(b / 255, 0, 1));
    }
    private static (double R, double G, double B)? Gamma()
    {
        foreach (var screen in Screen.AllScreens)
        {
            var dc = CreateDC("DISPLAY", screen.DeviceName, null, 0); if (dc == 0) continue;
            try
            {
                var data = new ushort[768]; if (!GetDeviceGammaRamp(dc, data) || data[128] == 0) continue;
                var g = (R: Math.Clamp(data[128] / 32768.0, 0, 1), G: Math.Clamp(data[384] / 32768.0, 0, 1), B: Math.Clamp(data[640] / 32768.0, 0, 1));
                if (g.R - g.B > .12) return g;
            }
            finally { DeleteDC(dc); }
        }
        return (1, 1, 1);
    }
    [DllImport("gdi32.dll", CharSet = CharSet.Unicode)] private static extern nint CreateDC(string driver, string device, string? output, nint init);
    [DllImport("gdi32.dll")] private static extern bool DeleteDC(nint dc);
    [DllImport("gdi32.dll")] private static extern bool GetDeviceGammaRamp(nint dc, [Out, MarshalAs(UnmanagedType.LPArray, SizeConst = 768)] ushort[] ramp);
}
