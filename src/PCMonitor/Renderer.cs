using System.Drawing.Drawing2D;
using System.Drawing.Imaging;
using System.Drawing.Text;
using System.Globalization;
using System.Runtime.InteropServices;

namespace PCMonitor;

public sealed class Renderer : IDisposable
{
    private static readonly Color Bg = Color.FromArgb(9, 11, 15), PanelColor = Color.FromArgb(19, 23, 32), Border = Color.FromArgb(55, 64, 82), Track = Color.FromArgb(37, 43, 56), Dim = Color.FromArgb(138, 148, 165), Dimmer = Color.FromArgb(120, 130, 148), Blue = Color.FromArgb(96, 165, 250), Green = Color.FromArgb(72, 222, 128), White = Color.FromArgb(240, 243, 246), Pink = Color.FromArgb(255, 106, 182), Tan = Color.FromArgb(224, 208, 178);
    private readonly PrivateFontCollection families = new();
    private readonly Dictionary<(int Size, bool Value, bool Bold), Font> fonts = [];
    private readonly Dictionary<string, Queue<(long Time, double? Value)>> history = [];
    private readonly StringFormat format = (StringFormat)StringFormat.GenericTypographic.Clone();
    private readonly Bitmap bitmap = new(800, 480, PixelFormat.Format32bppArgb);
    private Graphics g = null!;
    private float sx, sy;
    public Renderer()
    {
        foreach (var file in new[] { "JetBrainsMono-ExtraBold.ttf", "Roboto-Bold.ttf", "Roboto-Medium.ttf" }) families.AddFontFile(Path.Combine(AppContext.BaseDirectory, "Assets", "Fonts", file));
        format.FormatFlags |= StringFormatFlags.NoWrap | StringFormatFlags.MeasureTrailingSpaces;
    }
    public void Observe(Sample s, bool hardwareFresh = true)
    {
        if (hardwareFresh) {
        Add("cpu", s.CpuTemp); Add("gpu", s.GpuTemp); Add("ram", s.RamUsed);
        Add("read", s.DiskRead); Add("write", s.DiskWrite); Add("down", s.NetDown); Add("up", s.NetUp);
        }
        Add("frames", s.Frames.Held ? null : s.Frames.Milliseconds);
    }
    private void Add(string key, double? value)
    {
        if (!history.TryGetValue(key, out var q)) history[key] = q = new();
        long now = Environment.TickCount64; q.Enqueue((now, value));
        while (q.Count > 0 && (now - q.Peek().Time > 60000 || q.Count > 260)) q.Dequeue();
    }
    private Font Font(int size, bool value = false, bool bold = false)
    {
        var key = (size, value, bold);
        if (!fonts.TryGetValue(key, out var font))
        {
            var family = families.Families.First(f => f.Name.Contains(value ? "JetBrains" : "Roboto"));
            var style = value || bold ? FontStyle.Bold : FontStyle.Regular;
            if (!family.IsStyleAvailable(style)) style = family.IsStyleAvailable(FontStyle.Regular) ? FontStyle.Regular : FontStyle.Bold;
            fonts[key] = font = new Font(family, size, style, GraphicsUnit.Pixel);
        }
        return font;
    }
    private void Text(float x, float baseline, string text, int size, Color color, bool value = false, string align = "left", bool bold = false)
    {
        var f = Font(size, value, bold); float width = g.MeasureString(text, f, 2000, format).Width;
        float ascent = f.Size * f.FontFamily.GetCellAscent(f.Style) / f.FontFamily.GetEmHeight(f.Style);
        using var brush = new SolidBrush(color);
        g.DrawString(text, f, brush, x + sx - (align == "right" ? width : align == "center" ? width / 2 : 0), baseline + sy - ascent, format);
    }
    private void Box(Rectangle r) { using var b = new SolidBrush(PanelColor); using var p = new Pen(Border); g.FillRectangle(b, r.X + sx, r.Y + sy, r.Width, r.Height); g.DrawRectangle(p, r.X + sx, r.Y + sy, r.Width, r.Height); }
    private void Fill(float x, float y, float w, float h, Color color) { using var b = new SolidBrush(color); g.FillRectangle(b, x + sx, y + sy, w, h); }
    private void Line(float x, float y, float x1, float y1, Color color) { using var p = new Pen(color); g.DrawLine(p, x + sx, y + sy, x1 + sx, y1 + sy); }
    public static string Number(double? value, string spec = "0") => value.HasValue && double.IsFinite(value.Value) ? value.Value.ToString(spec, CultureInfo.InvariantCulture) : "--";
    public static string Rate(double? v, bool bits = false)
    {
        if (v is null) return "--";
        foreach (var (unit, scale) in new[] { (bits ? "Gbps" : "GB/s", 1e9), (bits ? "Mbps" : "MB/s", 1e6), (bits ? "Kbps" : "KB/s", 1e3) }) if (v >= scale) return Number(v / scale, "0.0") + " " + unit;
        return Number(v) + (bits ? " bps" : " B/s");
    }
    private void Big(float x, float baseline, double? value, string suffix, int size, Color color, string align = "left", string spec = "0") => Text(x, baseline, value.HasValue ? Number(value, spec) + suffix : "--", value.HasValue ? size : Math.Max(15, size / 2), value.HasValue ? color : Dimmer, value.HasValue, align);
    private void Row(float left, float right, float y, string label, string val, int size, Color color)
    { Text(left, y, label, 13, Dimmer); Text(right, y, val, val == "--" ? 14 : size, val == "--" ? Dimmer : color, val != "--", "right"); }
    private void Graph(string key, float x, float y, float w, float h, Color color, bool trends, double? level = null, double spanMin = 1)
    {
        Fill(x, y, w, h, Track);
        if (trends && history.TryGetValue(key, out var q))
        {
            var values = q.Where(p => p.Value.HasValue).Select(p => p.Value!.Value).ToArray();
            if (values.Length > 1)
            {
                double lo = values.Min(), hi = values.Max(); if (hi - lo < spanMin) { double mid = (lo + hi) / 2; lo = mid - spanMin / 2; hi = mid + spanMin / 2; }
                PointF? previous = null; long now = Environment.TickCount64;
                foreach (var point in q)
                {
                    if (point.Value is not double v) { previous = null; continue; }
                    var p = new PointF(x + w - 1 - (float)Math.Clamp((now - point.Time) / 60000.0, 0, 1) * (w - 2), y + h - 7 - (float)((v - lo) / (hi - lo)) * (h - 10));
                    if (previous.HasValue) Line(previous.Value.X, previous.Value.Y, p.X, p.Y, Blend(Track, color)); previous = p;
                }
                if (previous.HasValue) Fill(previous.Value.X - 1, previous.Value.Y - 1, 3, 3, color);
            }
        }
        if (level is double l && l > 0) Fill(x, trends ? y + h - 4 : y, (float)(w * Math.Clamp(l, 0, 1)), trends ? 4 : h, color);
    }
    private static Color Blend(Color a, Color b) => Color.FromArgb((a.R + b.R) / 2, (a.G + b.G) / 2, (a.B + b.B) / 2);
    private void Chip(int x, string name, Color color, double? temp, double? load, string sub, double? watts, string powerLabel, Settings c)
    {
        Box(new(x, 12, 382, 212)); int left = x + 12, right = x + 370;
        Text(left, 36, name, 15, color, bold: true);
        Big(left, 100, temp, "°", 58, color); Big(right, 100, load, "%", 58, color, "right");
        Text(left, 122, "TEMP", 12, Dimmer); Text(right, 122, "LOAD", 12, Dimmer, align: "right");
        Graph(name.ToLowerInvariant(), left, 130, 358, 22, color, c.Trends, load / 100, 4);
        Text(left, 176, sub, 18, color); Row(left, right, 204, powerLabel, watts.HasValue ? Number(watts) + " W" : "--", 24, color);
    }
    private void Ram(Rectangle r, Sample s, Settings c)
    {
        Box(r); int left = r.Left + 12, right = r.Right - 12, y = r.Top;
        Text(left, y + 24, "RAM", 15, Blue, bold: true);
        Big(left, y + 78, s.RamUsed / 1000, " GB", 40, Blue, spec: "0.0");
        double? pct = s.RamTotal > 0 ? s.RamUsed / s.RamTotal : null;
        Text(right, y + 78, pct.HasValue ? Number(pct * 100) + "%" : "--", 17, Dim, align: "right");
        Text(left, y + 104, s.RamTotal.HasValue ? "of " + Number(s.RamTotal / 1000) + " GB" : "--", 15, Dim);
        Graph("ram", left, y + 114, right - left, 36, Blue, c.Trends, pct);
        Row(left, right, y + 170, "FREE", s.RamTotal.HasValue ? Number((s.RamTotal - s.RamUsed) / 1000, "0.0") + " GB" : "--", 16, Dim);
    }
    private void Rates(Rectangle r, bool net, Sample s, Settings c)
    {
        Box(r); int left = r.Left + 12, right = r.Right - 12, y = r.Top; var color = net ? Pink : White;
        Text(left, y + 24, net ? "NETWORK" : "DISK", 15, color, bold: true);
        Row(left, right, y + 58, net ? "DOWN" : "READ", Rate(net ? s.NetDown : s.DiskRead, net), 26, color);
        Graph(net ? "down" : "read", left, y + 66, right - left, 36, color, c.Trends);
        Row(left, right, y + 138, net ? "UP" : "WRITE", Rate(net ? s.NetUp : s.DiskWrite, net), 26, color);
        Graph(net ? "up" : "write", left, y + 146, right - left, 36, color, c.Trends);
        Line(left, y + 100, right, y + 100, Border);
    }
    private void Frames(Rectangle r, Sample s, Settings c)
    {
        Box(r); int left = r.Left + 12, right = r.Right - 12, y = r.Top; var f = s.Frames; var color = f.Held ? Color.FromArgb(140, 130, 112) : Tan;
        Text(left, y + 22, "FRAMES", 15, color, bold: true); Big(left, y + 76, f.Fps, "", 56, color);
        Text(right, y + 76, f.Held ? $"FPS  HELD {f.Age:0}s" : "FPS", 14, Dim, align: "right", bold: true);
        Row(left, right, y + 104, "FRAME TIME", f.Milliseconds.HasValue ? Number(f.Milliseconds, "0.0") + " ms" : "--", 20, color);
        Graph("frames", left, y + 112, right - left, 30, Tan, c.Trends, spanMin: 2);
        Text(left, y + 168, "1% LOW", 13, Dimmer); Text(left + 57, y + 168, Number(f.Low1), 22, color, true);
        string low01 = Number(f.Low01);
        float lowWidth = g.MeasureString(low01, Font(22, true), 2000, format).Width;
        Text(right, y + 168, low01, 22, color, true, "right"); Text(right - lowWidth - 7, y + 168, "0.1% LOW", 13, Dimmer, align: "right");
    }
    public Bitmap Render(Sample s, Settings c, bool demo = false)
    {
        using (g = Graphics.FromImage(bitmap))
        {
            g.Clear(Bg); g.TextRenderingHint = TextRenderingHint.AntiAliasGridFit; g.SmoothingMode = SmoothingMode.None;
            int shift = c.PixelShift ? (int)(DateTimeOffset.UtcNow.ToUnixTimeSeconds() / 300 % 4) : 0;
            sx = shift is 1 or 2 ? 3 : 0; sy = shift is 2 or 3 ? 3 : 0;
            Chip(12, "CPU", Blue, s.CpuTemp, s.CpuLoad, $"PEAK {Number(s.CpuPeak / 1000, "0.00")}  AVG {Number(s.CpuAverage / 1000, "0.00")} GHz", s.CpuPower, "PKG POWER", c);
            Chip(406, "GPU", Green, s.GpuTemp, s.GpuLoad, $"CORE {Number(s.GpuClock)} MHz   VRAM {Number(s.Vram / 1000, "0.0")} GB", s.GpuPower, "BOARD POWER", c);
            if (s.Game) { Frames(new(12, 234, 282, 190), s, c); Ram(new(306, 234, 234, 190), s, c); Rates(new(552, 234, 236, 190), false, s, c); }
            else { Ram(new(12, 234, 282, 190), s, c); Rates(new(306, 234, 234, 190), false, s, c); Rates(new(552, 234, 236, 190), true, s, c); }
            Box(new(12, 436, 776, 36)); Text(24, 460, "TOTAL POWER", 14, Dim, bold: true);
            var power = PowerModel.Estimate(s, c);
            if (power.Watts is double watts)
            {
                double t = Math.Clamp((watts - 50) / 950, 0, 1); var color = Color.FromArgb((int)(72 + 183 * t), (int)(222 - 170 * t), 80);
                Fill(172, 448, 384, 9, Track); Fill(172, 448, (float)(384 * t), 9, color);
                foreach (float tick in new[] { .25f, .5f, .75f }) Line(172 + 384 * tick, 447, 172 + 384 * tick, 458, PanelColor);
                Text(688, 466, Number(watts), 34, color, true, "right"); Text(696, 466, "W", 18, color);
                Text(172, 467, "50", 11, Dimmer); Text(556, 467, "1000", 11, Dimmer, align: "right");
            }
            else Text(172, 460, "-- W", 14, Dimmer);
            Text(364, 467, power.Status, 11, Dimmer, align: "center"); Text(776, 462, DateTime.Now.ToString("HH:mm"), 17, Dim, align: "right");
            if (s.Held) Text(400, 230, "SENSORS HELD", 11, Dimmer, align: "center");
            if (demo) { Fill(590, 12, 198, 24, Color.Orange); Text(689, 30, "SIMULATED PREVIEW", 14, Color.Black, align: "center", bold: true); }
        }
        return bitmap;
    }
    public static byte[] Pixels(Bitmap image, int rotation = 0, (double R, double G, double B)? warmth = null)
    {
        var locked = image.LockBits(new(0, 0, image.Width, image.Height), ImageLockMode.ReadOnly, PixelFormat.Format32bppArgb);
        var data = new byte[800 * 480 * 4];
        try { for (int y = 0; y < 480; y++) Marshal.Copy(locked.Scan0 + y * locked.Stride, data, y * 800 * 4, 800 * 4); }
        finally { image.UnlockBits(locked); }
        if (warmth is { } gain)
        {
            for (int p = 0; p < data.Length; p += 4) { data[p] = (byte)(data[p] * gain.B); data[p + 1] = (byte)(data[p + 1] * gain.G); data[p + 2] = (byte)(data[p + 2] * gain.R); }
        }
        return rotation == 180 ? PanelProtocol.Rotate180(data) : data;
    }
    public void Dispose() { bitmap.Dispose(); foreach (var font in fonts.Values) font.Dispose(); families.Dispose(); format.Dispose(); }
}
