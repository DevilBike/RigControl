// RigInput: per-device filter for mouse buttons Windows owns (middle click, wheel).
// A WH_MOUSE_LL hook holds every physical middle/wheel event. Raw Input then says which device sent it
// (WM_INPUT arrives right after the hook, even for blocked events). Events from a device whose name
// contains the --device string are reported on stdout (MDOWN, MUP, WUP, WDOWN) and swallowed; events
// from any other device are replayed with SendInput. Injected events always pass straight through.
// Usage: RigInput.exe --device "VID_1532&PID_0244" [--pass-wheel] [--keys 26,28] [--scimitar <hid path>]
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
using System.Text;
using System.Windows.Forms;

public class RigInput : Form
{
    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id, HookProc cb, IntPtr hMod, uint tid);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr h, int n, IntPtr w, IntPtr l);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr h);
    [DllImport("kernel32.dll")] static extern IntPtr GetModuleHandle(string n);
    [DllImport("user32.dll")] static extern bool RegisterRawInputDevices(RAWINPUTDEVICE[] d, uint n, uint sz);
    [DllImport("user32.dll")] static extern uint GetRawInputData(IntPtr h, uint cmd, byte[] data, ref uint size, uint hdr);
    [DllImport("user32.dll", CharSet = CharSet.Unicode)] static extern uint GetRawInputDeviceInfo(IntPtr dev, uint cmd, StringBuilder data, ref uint size);
    [DllImport("user32.dll")] static extern uint SendInput(uint n, INPUT[] i, int sz);
    [DllImport("user32.dll")] static extern short GetAsyncKeyState(int vk);
    [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
    static extern Microsoft.Win32.SafeHandles.SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr sa, uint disp, uint flags, IntPtr tmpl);
    delegate IntPtr HookProc(int n, IntPtr w, IntPtr l);
    [StructLayout(LayoutKind.Sequential)] struct RAWINPUTDEVICE { public ushort page, usage; public uint flags; public IntPtr target; }
    [StructLayout(LayoutKind.Sequential)] struct MOUSEINPUT { public int dx, dy; public uint data, flags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct INPUT { public uint type; public MOUSEINPUT mi; }
    [StructLayout(LayoutKind.Sequential)] struct KEYBDINPUT { public ushort vk, scan; public uint flags, time; public IntPtr extra; }
    [StructLayout(LayoutKind.Sequential)] struct KINPUT { public uint type; public KEYBDINPUT ki; public long pad; }
    [StructLayout(LayoutKind.Sequential)] struct KBLL { public uint vk, scan, flags, time; public IntPtr extra; }
    [DllImport("user32.dll", EntryPoint = "SendInput")] static extern uint SendKeyInput(uint n, KINPUT[] i, int sz);
    [StructLayout(LayoutKind.Sequential)] struct MSLL { public int x, y; public uint data, flags, time; public IntPtr extra; }

    const int WM_MBUTTONDOWN = 0x207, WM_MBUTTONUP = 0x208, WM_MOUSEWHEEL = 0x20A;
    const ushort RI_MDOWN = 0x10, RI_MUP = 0x20, RI_WHEEL = 0x400;
    static readonly IntPtr TAG = (IntPtr)0x52474354;   // marks our own replays

    class Pending { public int msg; public short delta; public DateTime at; }
    readonly LinkedList<Pending> pending = new LinkedList<Pending>();
    readonly Dictionary<IntPtr, bool> isTarget = new Dictionary<IntPtr, bool>();
    readonly string device;
    // How the current middle press was handled; its release must be handled the same way.
    bool midSwallowed, midPassed, injectedMid;
    readonly Dictionary<IntPtr, bool> rawMid = new Dictionary<IntPtr, bool>();   // per-device physical middle state
    DateTime straySince = DateTime.MaxValue;
    // Scimitar vendor report (MI_00 COL03): byte1 holds its button state. A middle press the Scimitar does
    // not report is the Tartarus's, even when Raw Input never arrives (it often doesn't while FFXIV has focus).
    readonly string scimitarPath; const int SCIM_MID = 0x04;
    volatile bool scimOk, scimMid; DateTime scimMidAt = DateTime.MinValue;
    HookProc proc, kproc; IntPtr hook, khook;
    // Keyboard keys the Tartarus sends natively (joystick arrows, Alt) that have non-default binds.
    readonly HashSet<uint> keys = new HashSet<uint>();
    class KPending { public uint vk, scan, flags; public DateTime at; }
    readonly LinkedList<KPending> kpending = new LinkedList<KPending>();
    readonly Dictionary<uint, bool> kstate = new Dictionary<uint, bool>();   // vk -> swallowed (true) / passed (false) while held

    static string keyArg = "";
    static bool passWheel;   // --pass-wheel: the Tartarus scroll is plain scrolling, so wheel events are not held

    public static void Main(string[] args)
    {
        string dev = "VID_1532&PID_0244", scim = null; keyArg = "";
        for (int i = 0; i + 1 < args.Length; i++)
        {
            if (args[i] == "--device") dev = args[i + 1];
            if (args[i] == "--scimitar") scim = args[i + 1];
            if (args[i] == "--keys") keyArg = args[i + 1];
        }
        passWheel = Array.IndexOf(args, "--pass-wheel") >= 0;
        Application.Run(new RigInput(dev.ToUpperInvariant(), scim));
    }

    RigInput(string dev, string scim)
    {
        device = dev; scimitarPath = scim; ShowInTaskbar = false; WindowState = FormWindowState.Minimized; Opacity = 0;
        Load += (s, e) => {
            Hide();
            var rid = new[] { new RAWINPUTDEVICE { page = 1, usage = 2, flags = 0x100, target = Handle } }; // RIDEV_INPUTSINK
            RegisterRawInputDevices(rid, 1, (uint)Marshal.SizeOf(typeof(RAWINPUTDEVICE)));
            proc = Hook;
            hook = SetWindowsHookEx(14, proc, GetModuleHandle(null), 0);
            foreach (var k in keyArg.Split(new[] { ',' }, StringSplitOptions.RemoveEmptyEntries)) keys.Add(Convert.ToUInt32(k, 16));
            if (keys.Count > 0)
            {
                var kr = new[] { new RAWINPUTDEVICE { page = 1, usage = 2, flags = 0x100, target = Handle },
                                 new RAWINPUTDEVICE { page = 1, usage = 6, flags = 0x100, target = Handle } };
                RegisterRawInputDevices(kr, 2, (uint)Marshal.SizeOf(typeof(RAWINPUTDEVICE)));
                kproc = KeyHook;
                khook = SetWindowsHookEx(13, kproc, GetModuleHandle(null), 0);
                Out("LOG keyboard filter on for " + keyArg);
            }
            System.Diagnostics.Process.GetCurrentProcess().PriorityClass = System.Diagnostics.ProcessPriorityClass.High;
            var t = new Timer { Interval = 10 }; t.Tick += (a, b) => { Expire(); KExpire(); Watchdog(); }; t.Start();
            if (scimitarPath != null) new System.Threading.Thread(ReadScimitar) { IsBackground = true }.Start();
            Out("READY");
            // Exit when RigControl closes our stdin.
            new System.Threading.Thread(() => { try { while (Console.In.ReadLine() != null) { } } catch { } BeginInvoke((Action)Close); }) { IsBackground = true }.Start();
        };
        FormClosed += (s, e) => { UnhookWindowsHookEx(hook); if (khook != IntPtr.Zero) UnhookWindowsHookEx(khook); };
    }

    static void Out(string s) { try { Console.Out.WriteLine(s); Console.Out.Flush(); } catch { } }

    IntPtr Hook(int n, IntPtr w, IntPtr l)
    {
        int msg = w.ToInt32();
        if (n >= 0 && (msg == WM_MBUTTONDOWN || msg == WM_MBUTTONUP || (msg == WM_MOUSEWHEEL && !passWheel)))
        {
            var info = (MSLL)Marshal.PtrToStructure(l, typeof(MSLL));
            if ((info.flags & 1) != 0)
            {
                if (info.extra != TAG) { if (msg == WM_MBUTTONDOWN) injectedMid = true; else if (msg == WM_MBUTTONUP) injectedMid = false; }
            }
            else if (msg == WM_MBUTTONUP && !HasPending(WM_MBUTTONDOWN))
            {   // a release needs no attribution: it follows its press
                Decide(new Pending { msg = msg }, false);
                return (IntPtr)1;
            }
            else   // physical: hold it until Raw Input (or the Scimitar report) names the device
            {
                pending.AddLast(new Pending { msg = msg, delta = (short)(info.data >> 16), at = DateTime.UtcNow });
                return (IntPtr)1;
            }
        }
        return CallNextHookEx(hook, n, w, l);
    }

    protected override void WndProc(ref Message m)
    {
        if (m.Msg == 0xFF) OnRaw(m.LParam);
        base.WndProc(ref m);
    }

    // ---------- keyboard ----------
    static uint Norm(uint vk) { return vk == 0xA4 || vk == 0xA5 ? 0x12u : vk; }   // raw input reports VK_MENU for Alt

    IntPtr KeyHook(int n, IntPtr w, IntPtr l)
    {
        if (n >= 0)
        {
            var k = (KBLL)Marshal.PtrToStructure(l, typeof(KBLL));
            if ((k.flags & 0x10) == 0 && keys.Contains(k.vk))   // physical key we filter
            {
                bool up = (k.flags & 0x80) != 0; bool st;
                if (kstate.TryGetValue(k.vk, out st))
                {   // held already: repeats and the release follow the first decision
                    if (up) kstate.Remove(k.vk);
                    if (st) { Out((up ? "KUP " : "KDOWN ") + k.vk.ToString("X2")); return (IntPtr)1; }
                    return CallNextHookEx(khook, n, w, l);
                }
                if (up) return CallNextHookEx(khook, n, w, l);
                kpending.AddLast(new KPending { vk = k.vk, scan = k.scan, flags = k.flags, at = DateTime.UtcNow });
                return (IntPtr)1;
            }
        }
        return CallNextHookEx(khook, n, w, l);
    }

    void KResolve(uint rawVk, bool up, bool tartarus)
    {
        if (up) return;
        for (var node = kpending.First; node != null; node = node.Next)
        {
            if (Norm(node.Value.vk) != rawVk) continue;
            kpending.Remove(node); KDecide(node.Value, tartarus); return;
        }
    }

    void KDecide(KPending p, bool tartarus)
    {
        kstate[p.vk] = tartarus;
        if (tartarus) Out("KDOWN " + p.vk.ToString("X2"));
        else KReplay(p, false);
    }

    void KExpire()
    {   // fail-open: an unattributed press is passed through unchanged (it is the key it always was)
        var now = DateTime.UtcNow;
        while (kpending.First != null && (now - kpending.First.Value.at).TotalMilliseconds > 40)
        {
            var p = kpending.First.Value; kpending.RemoveFirst(); KDecide(p, false);
        }
    }

    void KReplay(KPending p, bool up)
    {
        var i = new KINPUT[1]; i[0].type = 1;
        i[0].ki.vk = (ushort)p.vk; i[0].ki.scan = (ushort)p.scan; i[0].ki.extra = TAG;
        i[0].ki.flags = ((p.flags & 1) != 0 ? 1u : 0u) | (up ? 2u : 0u);
        SendKeyInput(1, i, Marshal.SizeOf(typeof(KINPUT)));
    }

    void OnRaw(IntPtr h)
    {
        uint hdr = (uint)(8 + IntPtr.Size * 2), size = 0;
        GetRawInputData(h, 0x10000003, null, ref size, hdr);
        var b = new byte[size];
        if (GetRawInputData(h, 0x10000003, b, ref size, hdr) != size) return;
        IntPtr dev = IntPtr.Size == 8 ? (IntPtr)BitConverter.ToInt64(b, 8) : (IntPtr)BitConverter.ToInt32(b, 8);
        if (dev == IntPtr.Zero) return;   // injected
        if (BitConverter.ToUInt32(b, 0) == 1)   // RIM_TYPEKEYBOARD: MakeCode, Flags, Reserved, VKey
        {
            ushort kflags = BitConverter.ToUInt16(b, (int)hdr + 2), vkey = BitConverter.ToUInt16(b, (int)hdr + 6);
            KResolve(vkey, (kflags & 1) != 0, IsTarget(dev));
            return;
        }
        ushort flags = BitConverter.ToUInt16(b, (int)hdr + 4);
        if ((flags & RI_MDOWN) != 0) rawMid[dev] = true;
        if ((flags & RI_MUP) != 0) rawMid[dev] = false;
        if ((flags & RI_MDOWN) != 0) Resolve(WM_MBUTTONDOWN, dev);
        if ((flags & RI_MUP) != 0) Resolve(WM_MBUTTONUP, dev);
        if ((flags & RI_WHEEL) != 0) Resolve(WM_MOUSEWHEEL, dev);
    }

    void Resolve(int msg, IntPtr dev)
    {
        for (var node = pending.First; node != null; node = node.Next)
        {
            if (node.Value.msg != msg) continue;
            pending.Remove(node);
            Decide(node.Value, IsTarget(dev));
            return;
        }
    }

    bool IsTarget(IntPtr dev)
    {
        bool t;
        if (isTarget.TryGetValue(dev, out t)) return t;
        uint size = 512; var sb = new StringBuilder(512);
        GetRawInputDeviceInfo(dev, 0x20000007, sb, ref size);   // RIDI_DEVICENAME
        t = sb.ToString().ToUpperInvariant().Contains(device);
        isTarget[dev] = t;
        return t;
    }

    void Decide(Pending p, bool tartarus)
    {
        if (p.msg == WM_MBUTTONUP)
        {   // A release follows its press, whatever the attribution says.
            if (midSwallowed) { midSwallowed = false; Out("MUP"); }
            else { midPassed = false; Replay(p); }
        }
        else if (p.msg == WM_MBUTTONDOWN)
        {
            if (midSwallowed || midPassed)
            {   // previous release was lost: close it first
                Out("LOG middle press while previous still open, closing previous");
                Decide(new Pending { msg = WM_MBUTTONUP }, false);
            }
            if (tartarus) { midSwallowed = true; Out("MDOWN"); }
            else { midPassed = true; Replay(p); }
        }
        else if (tartarus) Out(p.delta > 0 ? "WUP" : "WDOWN");
        else Replay(p);
    }

    bool HasPending(int msg)
    {
        foreach (var p in pending) if (p.msg == msg) return true;
        return false;
    }

    void Expire()
    {
        var now = DateTime.UtcNow;
        while (pending.First != null)
        {
            var p = pending.First.Value;
            double age = (now - p.at).TotalMilliseconds;
            if (p.msg == WM_MBUTTONDOWN)
            {   // No Raw Input: the Scimitar report decides. Reported by the Scimitar -> pass; otherwise it is the
                // Tartarus once 40 ms have passed. Never passed blind: a leaked wheel click is FFXIV Auto-run (A1).
                bool scim = scimMid || (now - scimMidAt).TotalMilliseconds < 150;
                if (scim) { pending.RemoveFirst(); Out("LOG middle press attributed by Scimitar report"); Decide(p, false); continue; }
                if (age < 40) break;
                pending.RemoveFirst();
                Out(scimOk ? "LOG middle press not from Scimitar, treating as Tartarus" : "LOG no Scimitar report, treating middle press as Tartarus");
                Decide(p, true);
            }
            else if (p.msg == WM_MBUTTONUP)
            {
                if (age < 40) break;
                pending.RemoveFirst(); Decide(p, false);   // follows its press
            }
            else
            {   // wheel: harmless if it leaks, so pass after 40 ms
                if (age < 40) break;
                pending.RemoveFirst(); Decide(p, false);
            }
        }
    }

    void ReadScimitar()
    {
        while (true)
        {
            try
            {
                using (var h = CreateFile(scimitarPath, 0x80000000, 3, IntPtr.Zero, 3, 0, IntPtr.Zero))
                {
                    if (h.IsInvalid) { Out("LOG cannot open Scimitar report (" + Marshal.GetLastWin32Error() + ")"); scimOk = false; System.Threading.Thread.Sleep(5000); continue; }
                    using (var fs = new System.IO.FileStream(h, System.IO.FileAccess.Read, 1, false))
                    {
                        scimOk = true; Out("LOG reading Scimitar report");
                        var buf = new byte[512];
                        while (true)
                        {
                            int n = fs.Read(buf, 0, buf.Length);
                            if (n <= 0) break;
                            if (buf[0] != 0x03 || n < 2) continue;
                            bool mid = (buf[1] & SCIM_MID) != 0;
                            if (mid && !scimMid) scimMidAt = DateTime.UtcNow;
                            if (mid != scimMid) Out("LOG Scimitar middle " + (mid ? "down" : "up") + " (byte1 0x" + buf[1].ToString("X2") + ")");
                            scimMid = mid;
                        }
                    }
                }
            }
            catch (Exception e) { Out("LOG Scimitar report: " + e.Message); }
            scimOk = false; scimMid = false;
            System.Threading.Thread.Sleep(2000);
        }
    }

    void Watchdog()
    {   // Windows shows middle held, but no device reports it, nothing is pending and nothing injected holds it.
        bool held = (GetAsyncKeyState(0x04) & 0x8000) != 0, physical = false;
        foreach (var v in rawMid.Values) physical |= v;
        physical |= scimMid;
        if (held && !physical && !injectedMid && pending.Count == 0)
        {
            if (straySince == DateTime.MaxValue) straySince = DateTime.UtcNow;
            else if ((DateTime.UtcNow - straySince).TotalMilliseconds > 250)
            {
                Out("LOG watchdog: middle button stuck down with no device holding it, releasing");
                midPassed = false;
                Replay(new Pending { msg = WM_MBUTTONUP }); straySince = DateTime.MaxValue;
            }
        }
        else straySince = DateTime.MaxValue;
    }

    void Replay(Pending p)
    {
        var i = new INPUT[1]; i[0].type = 0; i[0].mi.extra = TAG;
        if (p.msg == WM_MBUTTONDOWN) i[0].mi.flags = 0x20;
        else if (p.msg == WM_MBUTTONUP) i[0].mi.flags = 0x40;
        else { i[0].mi.flags = 0x800; i[0].mi.data = (uint)(int)p.delta; }
        SendInput(1, i, Marshal.SizeOf(typeof(INPUT)));
    }
}






