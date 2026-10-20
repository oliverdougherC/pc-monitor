using System.Diagnostics;
using System.Runtime.InteropServices;
using Microsoft.Win32;

namespace PCMonitor;

// A top-level hidden window receives power/session events even without an open UI.
public sealed class WindowsHost : Form
{
    private readonly List<nint> notifications = [];
    private bool monitor = true, locked, suspended;
    private int resumes, displays;
    private nint suspendNotification;
    public event Action? Changed;
    public WindowsHost()
    {
        ShowInTaskbar = false; FormBorderStyle = FormBorderStyle.FixedToolWindow;
        var handle = Handle;
        foreach (var id in new[] { "2B84C20E-AD23-4DDF-93DB-05FFBD7EFCA5", "6FE69556-704A-47A0-8F24-C28D936FDA47" })
        {
            var guid = new Guid(id); var token = RegisterPowerSettingNotification(handle, ref guid, 0);
            if (token != 0) notifications.Add(token); else Log.Write("Power notification registration failed: " + Marshal.GetLastWin32Error());
        }
        if (!WTSRegisterSessionNotification(handle, 0)) Log.Write("Session notification registration failed.");
        suspendNotification = RegisterSuspendResumeNotification(handle, 0);
        locked = IsDesktopLocked();
    }
    protected override void SetVisibleCore(bool value) => base.SetVisibleCore(false);
    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0x218)
        {
            int kind = (int)m.WParam;
            if (kind == 4) suspended = true;
            else if (kind is 6 or 7 or 0x12) { suspended = false; resumes++; }
            else if (kind == 0x8013 && m.LParam != 0)
            {
                var size = Marshal.ReadInt32(m.LParam, 16);
                if (size == 4) { monitor = Marshal.ReadInt32(m.LParam, 20) != 0; displays++; }
            }
            Changed?.Invoke();
        }
        else if (m.Msg == 0x2b1)
        {
            int kind = (int)m.WParam;
            if (kind is 2 or 4 or 6 or 7) locked = true;
            else if (kind is 1 or 3 or 5 or 8) { locked = false; displays++; }
            Changed?.Invoke();
        }
        else if (m.Msg is 0x219 or 0x7e) { displays++; Changed?.Invoke(); }
        base.WndProc(ref m);
    }
    public HostIntent Snapshot()
    {
        var input = new LastInput { Size = (uint)Marshal.SizeOf<LastInput>() };
        double idle = GetLastInputInfo(ref input) ? unchecked((uint)Environment.TickCount - input.Tick) / 1000.0 : 0;
        var hwnd = GetForegroundWindow(); GetWindowThreadProcessId(hwnd, out var pid);
        string name = ""; try { using var p = Process.GetProcessById((int)pid); name = p.ProcessName.ToLowerInvariant() + ".exe"; } catch (ArgumentException) { } catch (System.ComponentModel.Win32Exception) { }
        bool fullscreen = false;
        var mi = new MonitorInfo { Size = Marshal.SizeOf<MonitorInfo>() };
        if (GetWindowRect(hwnd, out var r) && GetMonitorInfo(MonitorFromWindow(hwnd, 2), ref mi))
            fullscreen = (GetWindowLong(hwnd, -16) & 0x00c00000) == 0 && r.Left <= mi.Monitor.Left + 2 && r.Top <= mi.Monitor.Top + 2 && r.Right >= mi.Monitor.Right - 2 && r.Bottom >= mi.Monitor.Bottom - 2;
        return new(monitor, locked, suspended, idle, resumes, displays, (int)pid, name, fullscreen);
    }
    private static bool IsDesktopLocked()
    {
        var desktop = OpenInputDesktop(0, false, 1); if (desktop == 0) return true;
        CloseDesktop(desktop); return false;
    }
    protected override void Dispose(bool disposing)
    {
        if (disposing) { foreach (var n in notifications) UnregisterPowerSettingNotification(n); if (suspendNotification != 0) UnregisterSuspendResumeNotification(suspendNotification); WTSUnRegisterSessionNotification(Handle); }
        base.Dispose(disposing);
    }
    [StructLayout(LayoutKind.Sequential)] private struct LastInput { public uint Size, Tick; }
    [StructLayout(LayoutKind.Sequential)] private struct Rect { public int Left, Top, Right, Bottom; }
    [StructLayout(LayoutKind.Sequential)] private struct MonitorInfo { public int Size; public Rect Monitor, Work; public uint Flags; }
    [DllImport("user32.dll", SetLastError = true)] private static extern nint RegisterPowerSettingNotification(nint hwnd, ref Guid guid, uint flags);
    [DllImport("user32.dll")] private static extern bool UnregisterPowerSettingNotification(nint token);
    [DllImport("user32.dll")] private static extern nint RegisterSuspendResumeNotification(nint hwnd, uint flags);
    [DllImport("user32.dll")] private static extern bool UnregisterSuspendResumeNotification(nint token);
    [DllImport("wtsapi32.dll")] private static extern bool WTSRegisterSessionNotification(nint hwnd, uint flags);
    [DllImport("wtsapi32.dll")] private static extern void WTSUnRegisterSessionNotification(nint hwnd);
    [DllImport("user32.dll")] private static extern bool GetLastInputInfo(ref LastInput info);
    [DllImport("user32.dll")] private static extern nint GetForegroundWindow();
    [DllImport("user32.dll")] private static extern uint GetWindowThreadProcessId(nint hwnd, out uint pid);
    [DllImport("user32.dll")] private static extern bool GetWindowRect(nint hwnd, out Rect rect);
    [DllImport("user32.dll", EntryPoint = "GetWindowLongW")] private static extern int GetWindowLong(nint hwnd, int index);
    [DllImport("user32.dll")] private static extern nint MonitorFromWindow(nint hwnd, uint flags);
    [DllImport("user32.dll", EntryPoint = "GetMonitorInfoW")] private static extern bool GetMonitorInfo(nint monitor, ref MonitorInfo info);
    [DllImport("user32.dll")] private static extern nint OpenInputDesktop(uint flags, bool inherit, uint access);
    [DllImport("user32.dll")] private static extern bool CloseDesktop(nint desktop);
}

public sealed class ProcessJob : IDisposable
{
    private nint handle;
    public ProcessJob()
    {
        handle = CreateJobObject(0, null);
        var info = new ExtendedLimits { Basic = new BasicLimits { Flags = 0x2000 } };
        if (handle == 0 || !SetInformationJobObject(handle, 9, ref info, (uint)Marshal.SizeOf<ExtendedLimits>()))
            throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Cannot create process lifetime job.");
    }
    public void Add(Process process)
    {
        if (!AssignProcessToJobObject(handle, process.Handle)) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "Cannot contain monitoring child.");
    }
    public void Dispose() { if (handle != 0) { CloseHandle(handle); handle = 0; } }
    [StructLayout(LayoutKind.Sequential)] private struct BasicLimits { public long ProcessTime, JobTime; public uint Flags; public nuint MinWorking, MaxWorking; public uint Active; public nuint Affinity; public uint Priority, Scheduling; }
    [StructLayout(LayoutKind.Sequential)] private struct Io { public ulong A, B, C, D, E, F; }
    [StructLayout(LayoutKind.Sequential)] private struct ExtendedLimits { public BasicLimits Basic; public Io Io; public nuint ProcessMemory, JobMemory, PeakProcess, PeakJob; }
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)] private static extern nint CreateJobObject(nint attributes, string? name);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool SetInformationJobObject(nint job, int cls, ref ExtendedLimits info, uint size);
    [DllImport("kernel32.dll", SetLastError = true)] private static extern bool AssignProcessToJobObject(nint job, nint process);
    [DllImport("kernel32.dll")] private static extern bool CloseHandle(nint handle);
}
