"""RigControl: lighting, Scimitar DPI/buttons and Tartarus Pro analog keys in one app.

  RigControl.exe [--minimized] [--verbose]
Config: config.json next to the exe (previous version kept as config.backup.json).
"""
import colorsys, copy, ctypes, json, os, queue, shutil, subprocess, sys, threading, time
import tkinter as tk
from tkinter import ttk, messagebox

FROZEN = getattr(sys, 'frozen', False)
BASE = os.path.dirname(sys.executable if FROZEN else os.path.abspath(__file__))
if not FROZEN:
    sys.path.insert(0, BASE)
import actions, devices, sendkeys
from lighting import Lighting, TARGETS, EFFECTS, hex_rgb, uptime_seconds

CFG_PATH = os.path.join(BASE, 'config.json')
BACKUP_PATH = os.path.join(BASE, 'config.backup.json')
LOG_PATH = os.path.join(BASE, 'rigcontrol.log')
APP = 'RigControl'
# Lighting-only devices no reader watches: re-apply lighting when one reappears (OpenRGB only detects at start).
HOTPLUG = {'Keyboard': (devices.RAZER, {0x0243})}  # Huntsman Tournament Edition

_T = lambda a, p=4, r=0: {'action': a, 'press': p, 'release': r}
DEFAULT_CFG = {
    'version': 2,
    'lighting': {'color': '00ff00', 'effect': 'static', 'overrides': {}, 'uptime_tint': True, 'sparkle': True,
                 'sparkle_density': 1.0, 'sparkle_length': 0.5},
    'dpi': 1500,
    'openrgb_exe': 'OpenRGB\\OpenRGB.exe',
    'openrgb_port': 6742,
    'board_zone_sizes': {'D_LED1': 0, 'D_LED2': 12},
    'board_zone_targets': {'D_LED2': 'fans', 'I/O Cover': 'heatsink'},
    'aio': True,
    'aio_alert_temp': None,  # cooler logo red-alert temperature; None = no alert (liquidctl's own default is 45)
    'wheel_filter': True,
    'scimitar': dict({'G%d' % i: {'action': 'NUM%d' % i} for i in range(1, 10)},
                     G10={'action': 'NUMDIV'}, G11={'action': 'NUMMUL'}, G12={'action': 'NUMSUB'},
                     DPI={'action': 'MOUSE5'}, PROFILE={'action': 'MOUSE4'}),  # Bike: DPI=Forward(Auto-run), profile=Back(Walk)
    'tartarus': {'1': _T('ESC', 105, 98), '2': _T('F2', 105, 98), '3': _T('F3', 105, 98), '4': _T('F11', 105, 98),
                 '5': _T('F5'), '6': _T('TAB'), '7': _T('F7'), '8': _T('F8'), '9': _T('F9'), '10': _T('F10'),
                 '11': _T('LSHIFT'), '12': _T('2'), '13': _T('3'), '14': _T('4'), '15': _T('5'),
                 '16': _T('LCTRL'), '17': _T('7'), '18': _T('8'), '19': _T('9'), '20': _T('M'),
                 # wheel click = F13 (FFXIV Jump's 2nd key): End picked up Ctrl/Alt from keys 16 / Alt (Ctrl+End etc.)
                 'wheel_click': {'action': 'F13', 'tap_ms': 40},
                 'wheel_up': {'action': 'WHEELUP'}, 'wheel_down': {'action': 'WHEELDOWN'},
                 **{k: {'action': v} for k, v in devices.WheelFilter.DEFAULTS.items()}},
    'tartarus_profile': 2,   # profile indicator light: 1 red, 2 green, 3 blue
}


def load_cfg():
    cfg = copy.deepcopy(DEFAULT_CFG)
    if os.path.exists(CFG_PATH):
        with open(CFG_PATH, encoding='utf-8-sig') as f:  # tolerate a BOM from hand edits
            user = json.load(f)
        for k, v in user.items():
            if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                cfg[k].update(v)
            else:
                cfg[k] = v
    else:
        save_cfg(cfg)
    return cfg


def save_cfg(cfg):
    if os.path.exists(CFG_PATH):
        shutil.copyfile(CFG_PATH, BACKUP_PATH)
    tmp = CFG_PATH + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    os.replace(tmp, CFG_PATH)


# ---------------- start at login ----------------
def _startup_lnk():
    return os.path.join(os.environ['APPDATA'], r'Microsoft\Windows\Start Menu\Programs\Startup', APP + '.lnk')


def make_shortcut(path, target, args=''):
    ps = ("$s=(New-Object -ComObject WScript.Shell).CreateShortcut('%s');$s.TargetPath='%s';$s.Arguments='%s';"
          "$s.WorkingDirectory='%s';$s.Save()" % (path, target, args, os.path.dirname(target)))
    subprocess.run(['powershell', '-NoProfile', '-Command', ps], capture_output=True,
                   creationflags=subprocess.CREATE_NO_WINDOW)


def app_target():
    return (sys.executable, '--minimized') if FROZEN else (
        os.path.join(os.path.dirname(sys.executable), 'pythonw.exe'), '"%s" --minimized' % os.path.abspath(__file__))


# ---------------- colours / theme ----------------
BG, SIDE, CARD, CARD_HI, BORDER = '#121417', '#0c0e11', '#1a1d22', '#23282f', '#2a2f37'
FG, MUTED, ACCENT, ACCENT_HI, WARN = '#e8eaed', '#8b929b', '#3fb950', '#56d364', '#d29922'
PANEL = CARD
ICON_FONT = 'Segoe Fluent Icons'


def dark_theme(root):
    root.configure(bg=BG)
    s = ttk.Style(root); s.theme_use('clam')
    s.configure('.', background=BG, foreground=FG, fieldbackground=CARD_HI, bordercolor=BORDER,
                lightcolor=CARD_HI, darkcolor=CARD_HI, troughcolor=CARD_HI, arrowcolor=FG, insertcolor=FG,
                font=('Segoe UI', 10))
    for st, bg in (('TFrame', BG), ('Card.TFrame', CARD)):
        s.configure(st, background=bg)
    s.configure('TLabel', background=BG, foreground=FG)
    s.configure('Card.TLabel', background=CARD, foreground=FG)
    s.configure('Muted.TLabel', background=BG, foreground=MUTED)
    s.configure('CardMuted.TLabel', background=CARD, foreground=MUTED)
    s.configure('Head.TLabel', background=BG, font=('Segoe UI Semibold', 11))
    s.configure('CardHead.TLabel', background=CARD, foreground=FG, font=('Segoe UI Semibold', 11))
    s.configure('Title.TLabel', background=BG, foreground=FG, font=('Segoe UI Semibold', 18))
    s.configure('TButton', background=CARD_HI, foreground=FG, borderwidth=0, padding=(12, 6))
    s.map('TButton', background=[('active', BORDER), ('pressed', BORDER)])
    s.configure('TEntry', fieldbackground=CARD_HI, foreground=FG, borderwidth=0, padding=6)
    s.configure('TCombobox', fieldbackground=CARD_HI, background=CARD_HI, foreground=FG, arrowcolor=FG)
    s.map('TCombobox', fieldbackground=[('readonly', CARD_HI)], foreground=[('readonly', FG)])
    s.configure('TSpinbox', fieldbackground=CARD_HI, foreground=FG, arrowsize=12, padding=4)
    s.configure('TCheckbutton', background=CARD, foreground=FG, indicatorbackground=CARD_HI,
                indicatorforeground=ACCENT, padding=4)
    s.map('TCheckbutton', background=[('active', CARD)], indicatorbackground=[('selected', ACCENT)])
    s.configure('Vertical.TScrollbar', background=CARD_HI, troughcolor=CARD, borderwidth=0, arrowcolor=MUTED)
    root.option_add('*TCombobox*Listbox.background', CARD_HI)
    root.option_add('*TCombobox*Listbox.foreground', FG)
    root.option_add('*TCombobox*Listbox.selectBackground', ACCENT)
    root.option_add('*Font', ('Segoe UI', 10))


def _colorref(hexcol):
    r, g, b = hex_rgb(hexcol.lstrip('#'))
    return ctypes.c_int(r | (g << 8) | (b << 16))


def dark_title_bar(win, caption=SIDE):
    """Keep the native frame (snap, shake, taskbar all work) but paint it to match the app (Windows 11)."""
    try:
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id())
        dwm = ctypes.windll.dwmapi
        on = ctypes.c_int(1)
        dwm.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(on), 4)                    # immersive dark mode
        for attr, col in ((35, caption), (34, caption), (36, FG)):                  # caption, border, text
            c = _colorref(col)
            dwm.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(c), 4)
    except Exception:
        pass


def app_icon(size=64):
    from PIL import Image, ImageDraw
    img = Image.new('RGBA', (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((size * .08, size * .08, size * .92, size * .92), fill=(63, 185, 80, 255))
    d.ellipse((size * .32, size * .32, size * .68, size * .68), fill=(12, 14, 17, 255))
    return img


def flat_button(parent, text, command, primary=False, bg=CARD, width=None):
    """A flat label-button with hover state."""
    base = ACCENT if primary else CARD_HI
    hover = ACCENT_HI if primary else BORDER
    fg = '#0c0e11' if primary else FG
    b = tk.Label(parent, text=text, bg=base, fg=fg, padx=14, pady=6, cursor='hand2',
                 font=('Segoe UI Semibold', 10), width=width)
    b.bind('<Enter>', lambda e: b.configure(bg=hover))
    b.bind('<Leave>', lambda e: b.configure(bg=base))
    b.bind('<Button-1>', lambda e: command())
    return b


def card(parent, title=None, **grid):
    f = tk.Frame(parent, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=18, pady=16)
    if title:
        ttk.Label(f, text=title, style='CardHead.TLabel').pack(anchor='w', pady=(0, 10))
    return f


class Tile(tk.Frame):
    """Clickable key tile: small caption on top, bound action below."""

    def __init__(self, parent, caption, command, width=118):
        super().__init__(parent, bg=CARD_HI, width=width, height=58, cursor='hand2',
                         highlightthickness=1, highlightbackground=CARD_HI)
        self.pack_propagate(False)
        self.cap = tk.Label(self, text=caption, bg=CARD_HI, fg=MUTED, font=('Segoe UI', 8), anchor='w')
        self.val = tk.Label(self, text='', bg=CARD_HI, fg=FG, font=('Segoe UI Semibold', 11), anchor='w')
        self.cap.pack(fill='x', padx=10, pady=(7, 0)); self.val.pack(fill='x', padx=10)
        for w in (self, self.cap, self.val):
            w.bind('<Button-1>', lambda e: command())
            w.bind('<Enter>', lambda e: self.configure(highlightbackground=ACCENT))
            w.bind('<Leave>', lambda e: self.configure(highlightbackground=CARD_HI))

    FRIENDLY = {'MOUSE1': 'Left click', 'MOUSE2': 'Right click', 'MOUSE3': 'Middle click',
                'MOUSE4': 'Back (Mouse 4)', 'MOUSE5': 'Forward (Mouse 5)', 'WHEELUP': 'Scroll up', 'WHEELDOWN': 'Scroll down'}

    ARROW = {'UP': '↑', 'DOWN': '↓', 'LEFT': '←', 'RIGHT': '→'}

    def set(self, text, sub=None, tap_ms=0):
        text = self.FRIENDLY.get(text, text)
        if text and not text.lower().startswith('macro:') and any(k in self.ARROW for k in text.split('+')):
            text = '+'.join(self.ARROW.get(k, k) for k in text.split('+'))
        if text and tap_ms:
            text += ' · %dms' % tap_ms
        self.val.configure(text=text or '—', fg=FG if text else MUTED)
        if sub is not None:
            self.cap.configure(text=sub)


class ColorPicker(tk.Frame):
    """Saturation/brightness square plus hue bar (PIL-rendered). on_change(hex, final)."""
    W, H, HB = 380, 230, 16

    def __init__(self, master, on_change, bg=CARD):
        super().__init__(master, bg=bg)
        from PIL import Image
        self.on_change, self.h, self.s, self.v = on_change, 0.33, 1.0, 1.0
        self.sv = tk.Canvas(self, width=self.W, height=self.H, highlightthickness=0, bg=bg, cursor='crosshair')
        self.hue = tk.Canvas(self, width=self.W, height=self.HB, highlightthickness=0, bg=bg, cursor='sb_h_double_arrow')
        self.sv.pack(); self.hue.pack(pady=(12, 0))
        self._hmask = Image.linear_gradient('L').rotate(90).resize((self.W, self.H))      # 0 left .. 255 right
        self._vmask = Image.linear_gradient('L').resize((self.W, self.H)).transpose(Image.FLIP_TOP_BOTTOM)
        hue = Image.new('RGB', (self.W, 1))
        hue.putdata([tuple(int(c * 255) for c in colorsys.hsv_to_rgb(x / self.W, 1, 1)) for x in range(self.W)])
        from PIL import ImageTk
        self.hue_img = ImageTk.PhotoImage(hue.resize((self.W, self.HB)))
        self.hue.create_image(0, 0, image=self.hue_img, anchor='nw')
        self.hue_mark = self.hue.create_rectangle(0, 0, 4, self.HB, outline='white', width=2)
        self.sv_item = self.sv.create_image(0, 0, anchor='nw')
        self.sv_mark = self.sv.create_oval(0, 0, 14, 14, outline='white', width=2)
        for c, fn in ((self.sv, self._sv_evt), (self.hue, self._hue_evt)):
            c.bind('<Button-1>', fn); c.bind('<B1-Motion>', fn)
            c.bind('<ButtonRelease-1>', lambda e: self.on_change(self.get(), True))
        self._render_sv()

    def _render_sv(self):
        from PIL import Image, ImageTk
        pure = tuple(int(c * 255) for c in colorsys.hsv_to_rgb(self.h, 1, 1))
        img = Image.composite(Image.new('RGB', (self.W, self.H), pure), Image.new('RGB', (self.W, self.H), 'white'), self._hmask)
        img = Image.composite(img, Image.new('RGB', (self.W, self.H), 'black'), self._vmask)
        self.sv_img = ImageTk.PhotoImage(img)
        self.sv.itemconfigure(self.sv_item, image=self.sv_img)
        self._marks()

    def _marks(self):
        x, y = self.s * (self.W - 1), (1 - self.v) * (self.H - 1)
        self.sv.coords(self.sv_mark, x - 7, y - 7, x + 7, y + 7)
        hx = self.h * (self.W - 1)
        self.hue.coords(self.hue_mark, hx - 2, 1, hx + 2, self.HB - 1)

    def _sv_evt(self, e):
        self.s = min(max(e.x / (self.W - 1), 0), 1); self.v = min(max(1 - e.y / (self.H - 1), 0), 1)
        self._marks(); self.on_change(self.get(), False)

    def _hue_evt(self, e):
        self.h = min(max(e.x / (self.W - 1), 0), 0.999); self._render_sv(); self.on_change(self.get(), False)

    def get(self):
        return '%02x%02x%02x' % tuple(int(round(c * 255)) for c in colorsys.hsv_to_rgb(self.h, self.s, self.v))

    def set(self, hexcol):
        r, g, b = (c / 255 for c in hex_rgb(hexcol))
        self.h, self.s, self.v = colorsys.rgb_to_hsv(r, g, b)
        self._render_sv()


class BindDialog(tk.Toplevel):
    """Edit one button: key / combo / macro, plus actuation for analog keys."""
    MODS = ('LCTRL', 'RCTRL', 'LSHIFT', 'RSHIFT', 'LALT', 'RALT')

    def __init__(self, master, title, bind, analog):
        # Built hidden, then shown centred on the main window's monitor: Tk otherwise opens it at 0,0 of the
        # primary display. Normal title bar; it can also be dragged by its heading or background.
        super().__init__(master); self.withdraw(); self.configure(bg=CARD); self.title(title); self.resizable(False, False)
        self.result, self.capturing, self.mods = None, False, []
        f = tk.Frame(self, bg=CARD, padx=20, pady=18); f.pack(fill='both', expand=True)
        head = ttk.Label(f, text=title, style='CardHead.TLabel', cursor='fleur')
        head.grid(row=0, column=0, columnspan=4, sticky='w')
        for w in (f, head):
            w.bind('<ButtonPress-1>', self._drag_start); w.bind('<B1-Motion>', self._drag)
        ttk.Label(f, text='Action', style='CardMuted.TLabel').grid(row=1, column=0, sticky='w', pady=(14, 0))
        self.var = tk.StringVar(value=bind.get('action', ''))
        ttk.Entry(f, textvariable=self.var, width=38, font=('Segoe UI', 11)).grid(row=2, column=0, columnspan=4, sticky='we', pady=(4, 8))
        self.cap_btn = flat_button(f, 'Capture key', self.capture, primary=True)
        self.cap_btn.grid(row=3, column=0, sticky='w')
        flat_button(f, 'Disable', lambda: self.var.set('')).grid(row=3, column=1, sticky='w', padx=6)
        self.pick = ttk.Combobox(f, values=sendkeys.ALL_NAMES, state='readonly', width=12)
        self.pick.grid(row=3, column=2, columnspan=2, sticky='e')
        self.pick.set('Pick key…')
        self.pick.bind('<<ComboboxSelected>>', lambda e: self.var.set(self.pick.get()))
        ttk.Label(f, style='CardMuted.TLabel', justify='left', text=(
            'Combo  LCTRL+F2  (held while the button is held)\n'
            'Macro  macro: F2 50 F3 LCTRL+C   (numbers = wait ms)\n'
            'Mouse  MOUSE1-5, WHEELUP, WHEELDOWN')).grid(row=4, column=0, columnspan=4, sticky='w', pady=12)
        self.press = self.release = None
        if analog:   # actuation depth lives under "Advanced", closed by default
            self.press = tk.IntVar(value=bind.get('press', 4))
            self.release = tk.IntVar(value=bind.get('release', 0))
            adv = tk.Frame(f, bg=CARD)
            ttk.Label(adv, text='Press at', style='Card.TLabel').grid(row=0, column=0, sticky='w')
            ttk.Spinbox(adv, from_=1, to=255, textvariable=self.press, width=6).grid(row=0, column=1, sticky='w', padx=8)
            ttk.Label(adv, text='Release at', style='Card.TLabel').grid(row=1, column=0, sticky='w', pady=(6, 0))
            ttk.Spinbox(adv, from_=0, to=254, textvariable=self.release, width=6).grid(row=1, column=1, sticky='w', padx=8, pady=(6, 0))
            ttk.Label(adv, style='CardMuted.TLabel', text='How far the key must go down (0-255).  4 / 0 = fires on touch.'
                      ).grid(row=2, column=0, columnspan=3, sticky='w', pady=(6, 0))
            tog = tk.Label(f, text='▸  Advanced', bg=CARD, fg=MUTED, cursor='hand2', font=('Segoe UI', 10))
            tog.grid(row=5, column=0, columnspan=2, sticky='w')

            def toggle(e=None):
                if adv.winfo_ismapped():
                    adv.grid_remove(); tog.configure(text='▸  Advanced')
                else:
                    adv.grid(row=6, column=0, columnspan=4, sticky='w', pady=(6, 0)); tog.configure(text='▾  Advanced')
            tog.bind('<Button-1>', toggle)
        # Press length: FFXIV reads keys once per frame, so a very short press can be missed.
        tap = int(bind.get('tap_ms', 0) or 0)
        pl = tk.Frame(f, bg=CARD); pl.grid(row=7, column=0, columnspan=4, sticky='w', pady=(12, 0))
        ttk.Label(pl, text='Press length', style='Card.TLabel').pack(side='left')
        self.tap_mode = ttk.Combobox(pl, values=self.TAP_MODES, state='readonly', width=18)
        self.tap_mode.set(self.TAP_MODES[1 if tap else 0]); self.tap_mode.pack(side='left', padx=8)
        self.tap_ms = tk.IntVar(value=tap or 40)
        self.tap_spin = ttk.Spinbox(pl, from_=10, to=1000, increment=5, textvariable=self.tap_ms, width=5)
        self.tap_spin.pack(side='left')
        ttk.Label(pl, text='ms', style='CardMuted.TLabel').pack(side='left', padx=(4, 0))
        ttk.Label(f, style='CardMuted.TLabel', text='Hold: key stays down while the button is (at least 30 ms).\n'
                  'Tap: key is pressed for exactly this long, however briefly you click (30-50 ms suits FFXIV).'
                  ).grid(row=8, column=0, columnspan=4, sticky='w', pady=(4, 0))
        self.tap_mode.bind('<<ComboboxSelected>>', lambda e: self._tap_state())
        self._tap_state()
        b = tk.Frame(f, bg=CARD); b.grid(row=9, column=0, columnspan=4, sticky='e', pady=(16, 0))
        flat_button(b, 'Save', self.ok, primary=True).pack(side='right')
        flat_button(b, 'Cancel', self.destroy).pack(side='right', padx=8)
        self.bind('<KeyPress>', self._key); self.bind('<KeyRelease>', self._keyup)
        self._place(master)
        self.transient(master); self.deiconify(); dark_title_bar(self, CARD)
        self.lift(); self.focus_force(); self.grab_set()
        self.wait_window()

    TAP_MODES = ('Hold while pressed', 'Tap for')

    def _tap_state(self):
        self.tap_spin.configure(state='normal' if self.tap_mode.get() == self.TAP_MODES[1] else 'disabled')

    def _place(self, master):
        """Centre on the work area of the monitor the main window is on (fallback: over the main window)."""
        self.update_idletasks()
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        x = master.winfo_rootx() + (master.winfo_width() - w) // 2
        y = master.winfo_rooty() + (master.winfo_height() - h) // 2
        try:
            class MONITORINFO(ctypes.Structure):
                _fields_ = [('cbSize', ctypes.c_ulong), ('rcMonitor', ctypes.c_long * 4),
                            ('rcWork', ctypes.c_long * 4), ('dwFlags', ctypes.c_ulong)]
            u32 = ctypes.windll.user32
            u32.MonitorFromWindow.restype = ctypes.c_void_p
            hwnd = u32.GetParent(master.winfo_id()) or master.winfo_id()
            mon = u32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)   # MONITOR_DEFAULTTONEAREST
            mi = MONITORINFO(); mi.cbSize = ctypes.sizeof(MONITORINFO)
            if mon and u32.GetMonitorInfoW(ctypes.c_void_p(mon), ctypes.byref(mi)):
                l, t, r, b = mi.rcWork
                x, y = l + (r - l - w) // 2, t + (b - t - h) // 2
        except Exception:
            pass
        self.geometry('+%d+%d' % (x, y))

    def _drag_start(self, e):
        self._dx, self._dy = e.x_root - self.winfo_x(), e.y_root - self.winfo_y()

    def _drag(self, e):
        self.geometry('+%d+%d' % (e.x_root - self._dx, e.y_root - self._dy))

    def capture(self):
        self.capturing, self.mods = True, []
        self.cap_btn.configure(text='Press a key…'); self.focus_set()

    def _key(self, e):
        if not self.capturing:
            return
        name = sendkeys.VK_TO_NAME.get(e.keycode)
        if name is None:
            return 'break'
        if name in self.MODS:
            if name not in self.mods: self.mods.append(name)
            return 'break'
        self._done('+'.join(self.mods + [name])); return 'break'

    def _keyup(self, e):
        if self.capturing and self.mods and sendkeys.VK_TO_NAME.get(e.keycode) in self.MODS:
            self._done('+'.join(self.mods))

    def _done(self, combo):
        self.capturing = False; self.var.set(combo); self.cap_btn.configure(text='Capture key')

    def ok(self):
        try:
            actions.validate(self.var.get())
            r = {'action': self.var.get().strip()}
            if self.press is not None:
                p, rl = int(self.press.get()), int(self.release.get())
                if not (1 <= p <= 255 and 0 <= rl < p):
                    raise ValueError('press must be 1-255 and release lower than press')
                r.update(press=p, release=rl)
            if self.tap_mode.get() == self.TAP_MODES[1]:
                t = int(self.tap_ms.get())
                if not 10 <= t <= 1000:
                    raise ValueError('tap length must be 10-1000 ms')
                r['tap_ms'] = t
        except Exception as e:
            messagebox.showerror('Invalid', str(e), parent=self); return
        self.result = r; self.destroy()


class App:
    def __init__(self, verbose, minimized):
        self.cfg, self.q = load_cfg(), queue.Queue()
        self.logf = open(LOG_PATH, 'a', encoding='utf-8')
        self.light = Lighting(self.cfg, self.log, BASE)
        self.seen = set()
        self.readers = [devices.Tartarus(self.log, self.cfg['tartarus'], self.on_device, verbose),
                        devices.Scimitar(self.log, self.cfg['scimitar'], self.on_device, verbose)]
        self.wheel = None
        self.last_tint = None
        self.live_pending, self.live_busy = None, False
        self.root = tk.Tk(); dark_theme(self.root)
        self.build_ui()
        self.tray = None
        if minimized:
            self.root.withdraw()

    # ---------- logging / config ----------
    def log(self, msg):
        line = time.strftime('%H:%M:%S ') + msg
        try:
            self.logf.write(line + '\n'); self.logf.flush()
        except Exception:
            pass
        self.q.put(line)

    def save(self):
        try:
            save_cfg(self.cfg)
        except Exception as e:
            self.log('ERROR saving config: %s' % e)

    def bg(self, fn, *a):
        def run():
            try:
                fn(*a)
            except Exception as e:
                self.log('ERROR %s: %s' % (getattr(fn, '__name__', 'task'), e))
        threading.Thread(target=run, daemon=True).start()

    # ---------- hardware ----------
    def apply_lighting(self):
        self.light.apply()
        devices.set_tartarus_mode(0x03)  # OpenRGB's Razer init can drop the keypad to normal mode
        self.apply_profile_led()

    def apply_profile_led(self):
        try:
            devices.set_tartarus_profile_led(self.cfg.get('tartarus_profile', 2))
        except Exception as e:
            self.log('Tartarus profile light: %s' % e)

    def apply_dpi(self):
        rgb = hex_rgb(self.light.resolve('mouse_dpi')[0])
        self.cfg['dpi'] = devices.set_scimitar_dpi(self.cfg['dpi'], rgb)
        self.log('Scimitar DPI %d' % self.cfg['dpi'])

    def on_device(self, name):
        """Reader connected: first time at startup, again after a replug."""
        replug = name in self.seen
        self.seen.add(name)
        try:
            if name == 'Tartarus':
                devices.set_tartarus_mode(0x03); self.log('Tartarus: driver mode on')
                self.apply_profile_led()
            elif name == 'Scimitar':
                self.apply_dpi()
                if replug and self.wheel:
                    self.wheel.restart()
            if replug:
                self.log('%s replugged: restarting OpenRGB and re-applying lighting' % name)
                self.light.restart_server(); self.apply_lighting()
        except Exception as e:
            self.log('ERROR %s setup: %s' % (name, e))

    def startup_lighting(self):
        """A leftover OpenRGB keeps handles to devices that were replugged since it started (they stay dark)."""
        self.light.restart_server(); self.apply_lighting()

    def reapply_all(self):
        self.apply_lighting(); self.apply_dpi()

    def watcher(self):
        """Detect resume from sleep (wall clock jump), replugged lighting-only devices and uptime tint steps."""
        last = time.time()
        present = self.hotplug_present()
        tick = 0
        while True:
            time.sleep(5); tick += 1
            now_present = self.hotplug_present()
            for name in now_present - present:
                self.log('%s replugged: restarting OpenRGB and re-applying lighting' % name)
                # Applied right away, the Huntsman was still booting and went back to its own colour cycle:
                # wait for its firmware, then apply again in case it reset after the first write.
                time.sleep(6)
                try:
                    self.light.restart_server(); self.apply_lighting()
                    time.sleep(8); self.apply_lighting()
                except Exception as e:
                    self.log('ERROR re-apply after %s replug: %s' % (name, e))
                last = time.time()  # the OpenRGB restart is not a sleep/resume gap
            present = now_present
            if tick % 12 == 0:
                self.check_uptime_tint()
            now = time.time()
            if now - last > 30:
                self.log('Resume from sleep detected: re-applying')
                time.sleep(5)
                try:
                    self.light.restart_server(); self.reapply_all()
                except Exception as e:
                    self.log('ERROR re-apply after resume: %s' % e)
                now = time.time()
            last = now
            self.check_tartarus_mode()

    @staticmethod
    def hotplug_present():
        try:
            return {name for name, (vid, pids) in HOTPLUG.items() if devices.find(vid, pids)}
        except Exception:
            return set()

    def check_uptime_tint(self):
        """Re-apply when the uptime tint has moved the colour far enough to change the hex value."""
        if not self.cfg['lighting'].get('uptime_tint'):
            return
        color = self.light.resolve('fans')[0]
        if color != self.last_tint:
            if self.last_tint is not None:
                self.log('Uptime tint: %.1f days up -> #%s' % (uptime_seconds() / 86400, color))
                self.bg(self.apply_lighting)
            self.last_tint = color

    def toggle_uptime_tint(self):
        self.cfg['lighting']['uptime_tint'] = self.tint_var.get(); self.save()
        self.log('Uptime tint: %s' % ('on' if self.tint_var.get() else 'off'))
        self.last_tint = None
        self.bg(self.apply_lighting)

    def set_sparkle(self, key, value):
        self.cfg['lighting'][key] = round(float(value), 2)
        self.save_soon()

    def save_soon(self):
        """Slider drags: save once they stop."""
        if getattr(self, '_save_job', None):
            self.root.after_cancel(self._save_job)
        self._save_job = self.root.after(800, self.save)

    def toggle_sparkle(self):
        self.cfg['lighting']['sparkle'] = self.sparkle_var.get(); self.save()
        self.log('Sparkle: %s' % ('on' if self.sparkle_var.get() else 'off'))

    def cooler_poller(self):
        """Also re-applies the cooler colour when it comes back (replug / power cycle loses it)."""
        time.sleep(8)
        was = True  # startup already applied it
        while True:
            ok = self.light.poll_cooler()
            if ok and not was:
                self.log('CPU cooler reconnected: re-applying its colour')
                self.light.apply_cooler()
            was = ok
            time.sleep(15)

    def check_tartarus_mode(self):
        """The keypad must stay in driver mode; outside it, its keys type natively and can stick."""
        tart = self.readers[0]
        if not tart.connected:
            return
        try:
            mode = devices.get_tartarus_mode()
            if mode is not None and mode != 0x03:
                self.log('Tartarus: found in mode %d, switching back to driver mode' % mode)
                devices.set_tartarus_mode(0x03)
        except Exception as e:
            self.log('Tartarus mode check: %s' % e)

    # ---------- UI ----------
    NAV = [('lighting', 'Lighting', ''), ('mouse', 'Mouse', ''), ('tartarus', 'Tartarus', ''),
           ('status', 'Status', ''), ('settings', 'Settings', ''), ('log', 'Log', '')]

    def build_ui(self):
        r = self.root; r.title(APP); r.geometry('1140x720'); r.minsize(1100, 660)
        from PIL import ImageTk
        self._icon = ImageTk.PhotoImage(app_icon(64)); r.iconphoto(True, self._icon)
        side = tk.Frame(r, bg=SIDE, width=210); side.pack(side='left', fill='y'); side.pack_propagate(False)
        head = tk.Frame(side, bg=SIDE); head.pack(fill='x', padx=18, pady=(20, 26))
        self._logo = ImageTk.PhotoImage(app_icon(26))
        tk.Label(head, image=self._logo, bg=SIDE).pack(side='left')
        tk.Label(head, text=APP, bg=SIDE, fg=FG, font=('Segoe UI Semibold', 14)).pack(side='left', padx=10)
        self.nav_items, self.pages = {}, {}
        for key, label, glyph in self.NAV:
            self.nav_items[key] = self._nav_item(side, key, label, glyph)
        main = tk.Frame(r, bg=BG); main.pack(side='left', fill='both', expand=True)
        self.footer = tk.Frame(main, bg=BG); self.footer.pack(side='bottom', fill='x', padx=28, pady=(0, 14))
        self.status_dots = {}
        for name in ('Scimitar', 'Tartarus', 'Lighting', 'Wheel remap'):
            lbl = tk.Label(self.footer, text='●  ' + name, bg=BG, fg=MUTED, font=('Segoe UI', 9))
            lbl.pack(side='left', padx=(0, 18)); self.status_dots[name] = lbl
        self.cooler_lbl = tk.Label(self.footer, text='Cooler —', bg=BG, fg=MUTED, font=('Segoe UI', 9))
        self.cooler_lbl.pack(side='right')
        holder = tk.Frame(main, bg=BG); holder.pack(fill='both', expand=True, padx=28, pady=(22, 10))
        holder.grid_rowconfigure(0, weight=1); holder.grid_columnconfigure(0, weight=1)
        for key, build in (('lighting', self.lighting_page), ('mouse', self.mouse_page), ('tartarus', self.tartarus_page),
                           ('status', self.status_page), ('settings', self.settings_page), ('log', self.log_page)):
            p = tk.Frame(holder, bg=BG); p.grid(row=0, column=0, sticky='nsew')
            build(p); self.pages[key] = p
        self.show_page('lighting')
        r.protocol('WM_DELETE_WINDOW', self.hide)
        dark_title_bar(r)
        r.after(100, self.pump); r.after(500, self.refresh_status)

    def _nav_item(self, side, key, label, glyph):
        f = tk.Frame(side, bg=SIDE, cursor='hand2'); f.pack(fill='x', padx=10, pady=2)
        bar = tk.Frame(f, bg=SIDE, width=3); bar.pack(side='left', fill='y')
        ic = tk.Label(f, text=glyph, bg=SIDE, fg=MUTED, font=(ICON_FONT, 13), width=2)
        ic.pack(side='left', padx=(12, 8), pady=10)
        tx = tk.Label(f, text=label, bg=SIDE, fg=MUTED, font=('Segoe UI', 11), anchor='w')
        tx.pack(side='left', fill='x')
        parts = (f, ic, tx)

        def paint(state):
            sel = self.page == key
            bg = CARD if sel else (CARD_HI if state == 'hover' else SIDE)
            for w in parts: w.configure(bg=bg)
            ic.configure(fg=ACCENT if sel else (FG if state == 'hover' else MUTED))
            tx.configure(fg=FG if sel or state == 'hover' else MUTED)
            bar.configure(bg=ACCENT if sel else bg)
        for w in parts:
            w.bind('<Button-1>', lambda e: self.show_page(key))
            w.bind('<Enter>', lambda e: paint('hover')); w.bind('<Leave>', lambda e: paint(''))
        return paint

    page = None

    def show_page(self, key):
        self.page = key
        self.pages[key].tkraise()
        for paint in self.nav_items.values():
            paint('')

    def page_title(self, p, title, sub):
        tk.Label(p, text=title, bg=BG, fg=FG, font=('Segoe UI Semibold', 20)).pack(anchor='w')
        tk.Label(p, text=sub, bg=BG, fg=MUTED, font=('Segoe UI', 10)).pack(anchor='w', pady=(0, 18))

    # ----- Lighting -----
    def lighting_page(self, p):
        self.page_title(p, 'Lighting', 'Pick a colour for everything, or select a device to give it its own.')
        body = tk.Frame(p, bg=BG); body.pack(fill='both', expand=True)
        left = card(body, 'Colour'); left.pack(side='left', fill='y', anchor='n')
        self.picker = ColorPicker(left, self.on_color); self.picker.pack()
        row = tk.Frame(left, bg=CARD); row.pack(fill='x', pady=(14, 0))
        self.swatch = tk.Frame(row, width=40, height=40, bg='#' + self.cfg['lighting']['color'],
                               highlightthickness=1, highlightbackground=BORDER)
        self.swatch.pack(side='left')
        self.hex_var = tk.StringVar(value='#' + self.cfg['lighting']['color'])
        hx = ttk.Entry(row, textvariable=self.hex_var, width=10, font=('Consolas', 12))
        hx.pack(side='left', padx=12); hx.bind('<Return>', lambda e: self.on_hex())
        ttk.Label(left, text='Effect', style='CardMuted.TLabel').pack(anchor='w', pady=(16, 6))
        seg = tk.Frame(left, bg=CARD); seg.pack(anchor='w')
        self.effect_btns = {}
        for eff in EFFECTS:
            b = tk.Label(seg, text=eff.title(), bg=CARD_HI, fg=FG, padx=14, pady=6, cursor='hand2', font=('Segoe UI', 10))
            b.pack(side='left', padx=(0, 4)); b.bind('<Button-1>', lambda e, eff=eff: self.on_effect(eff))
            self.effect_btns[eff] = b

        right = card(body, 'Devices'); right.pack(side='left', fill='both', expand=True, padx=(18, 0), anchor='n')
        self.target_names = [('all', 'Everything')] + TARGETS
        self.sel_target = 'all'
        self.dev_rows = {}
        for key, name in self.target_names:
            self.dev_rows[key] = self._device_row(right, key, name)
        self.load_target()

    def _device_row(self, parent, key, name):
        f = tk.Frame(parent, bg=CARD, cursor='hand2'); f.pack(fill='x', pady=1)
        sw = tk.Canvas(f, width=22, height=22, bg=CARD, highlightthickness=0)
        dot = sw.create_oval(2, 2, 20, 20, outline=BORDER)
        sw.pack(side='left', padx=(10, 12), pady=8)
        tx = tk.Label(f, text=name, bg=CARD, fg=FG, font=('Segoe UI Semibold' if key == 'all' else 'Segoe UI', 10), anchor='w')
        tx.pack(side='left', fill='x', expand=True)
        info = tk.Label(f, text='', bg=CARD, fg=MUTED, font=('Segoe UI', 9))
        info.pack(side='left', padx=8)
        rst = tk.Label(f, text='', bg=CARD, fg=MUTED, font=(ICON_FONT, 9), cursor='hand2')   # reset to Everything
        rst.pack(side='left', padx=(0, 10))
        rst.bind('<Button-1>', lambda e: self.reset_override(key))
        parts = (f, sw, tx, info, rst)

        def paint(hover=False):
            sel = self.sel_target == key
            bg = CARD_HI if sel or hover else CARD
            for w in parts: w.configure(bg=bg)
            L = self.cfg['lighting']
            color, effect = (L['color'], L['effect']) if key == 'all' else self.light.resolve(key, tint=False)
            own = key != 'all' and key in L['overrides']
            sw.itemconfigure(dot, fill='#000000' if effect == 'off' else '#' + color)
            info.configure(text=effect if key == 'all' else ('custom · ' + effect if own else 'follows Everything'),
                           fg=ACCENT if own else MUTED)
            rst.configure(text='' if own else '', fg=MUTED)
        for w in (f, sw, tx, info):
            w.bind('<Button-1>', lambda e: self.select_target(key))
            w.bind('<Enter>', lambda e: paint(True)); w.bind('<Leave>', lambda e: paint(False))
        return paint

    def select_target(self, key):
        self.sel_target = key; self.load_target()

    def cur_target(self):
        return self.sel_target

    def load_target(self):
        t = self.cur_target(); L = self.cfg['lighting']
        color, effect = (L['color'], L['effect']) if t == 'all' else self.light.resolve(t, tint=False)
        self.picker.set(color); self.swatch.configure(bg='#' + color); self.hex_var.set('#' + color)
        self._paint_effects(effect)
        for paint in self.dev_rows.values():
            paint()

    def _paint_effects(self, effect):
        for eff, b in self.effect_btns.items():
            b.configure(bg=ACCENT if eff == effect else CARD_HI, fg='#0c0e11' if eff == effect else FG)

    def _set(self, key, value):
        t = self.cur_target(); L = self.cfg['lighting']
        if t == 'all':
            L[key] = value
        else:
            L['overrides'].setdefault(t, {})[key] = value

    def on_color(self, hexcol, final):
        self._set('color', hexcol); self.swatch.configure(bg='#' + hexcol); self.hex_var.set('#' + hexcol)
        if final:
            self.live_pending = None
            self.save(); self.load_target()
            self.bg(self.apply_final)
        else:
            self.live_pending = True
            if not self.live_busy:
                self.live_busy = True; self.root.after(80, self.live_tick)

    def on_hex(self):
        v = self.hex_var.get().strip().lstrip('#').lower()
        if len(v) == 6 and all(c in '0123456789abcdef' for c in v):
            self.picker.set(v); self.on_color(v, True)
        else:
            self.hex_var.set('#' + self.picker.get())

    def live_tick(self):
        """Throttled live preview: at most one OpenRGB update in flight, ~12/s."""
        if not self.live_pending:
            self.live_busy = False; return
        self.live_pending = None

        def run():
            try: self.light.apply(live=True)
            except Exception as e: self.log('live preview: %s' % e)
            self.root.after(80, self.live_tick)
        threading.Thread(target=run, daemon=True).start()

    def apply_final(self):
        self.apply_lighting()
        if self.cur_target() in ('all', 'mouse_dpi'):
            self.apply_dpi()  # DPI stage colour = DPI light colour

    def on_effect(self, eff):
        self._set('effect', eff); self.save(); self.load_target(); self.bg(self.apply_final)

    def reset_override(self, key=None):
        self.cfg['lighting']['overrides'].pop(key or self.cur_target(), None)
        self.save(); self.load_target(); self.bg(self.apply_final)

    # ----- Mouse -----
    def mouse_page(self, p):
        self.page_title(p, 'Scimitar', 'DPI is fixed in RigControl; the DPI button never changes it.')
        c = card(p, 'DPI'); c.pack(fill='x')
        row = tk.Frame(c, bg=CARD); row.pack(fill='x')
        self.dpi_var = tk.IntVar(value=self.cfg['dpi'])
        self.dpi_scale = tk.Scale(row, from_=100, to=18000, resolution=50, orient='horizontal', length=460,
                                  variable=self.dpi_var, showvalue=False, bg=ACCENT, fg=FG, troughcolor=CARD_HI,
                                  highlightthickness=0, activebackground=ACCENT_HI, sliderrelief='flat', bd=0,
                                  sliderlength=18, width=10)
        self.dpi_scale.pack(side='left'); self.dpi_scale.bind('<ButtonRelease-1>', lambda e: self.set_dpi())
        ent = ttk.Entry(row, textvariable=self.dpi_var, width=7, font=('Consolas', 12)); ent.pack(side='left', padx=14)
        ent.bind('<Return>', lambda e: self.set_dpi())
        flat_button(row, 'Apply', self.set_dpi, primary=True).pack(side='left')
        row2 = tk.Frame(p, bg=BG); row2.pack(fill='x', pady=(18, 0))
        b = card(row2, 'Side keypad'); b.pack(side='left', anchor='n')
        ttk.Label(b, text='Laid out as on the mouse. Click a key to change it.', style='CardMuted.TLabel').pack(anchor='w', pady=(0, 10))
        grid = tk.Frame(b, bg=CARD); grid.pack(anchor='w')
        self.mouse_btns = {}
        for i in range(12):
            n = 'G%d' % (i + 1)
            t = Tile(grid, n, lambda n=n: self.edit_mouse(n)); t.grid(row=i // 3, column=i % 3, padx=4, pady=4)
            self.mouse_btns[n] = t
        col = tk.Frame(row2, bg=BG); col.pack(side='left', fill='x', expand=True, anchor='n', padx=(18, 0))
        s = card(col, 'Buttons 4 and 5'); s.pack(fill='x')
        sg = tk.Frame(s, bg=CARD); sg.pack(anchor='w')
        for i, (n, cap) in enumerate((('DPI', 'Button 4  ·  DPI button'), ('PROFILE', 'Button 5  ·  profile button'))):
            t = Tile(sg, cap, lambda n=n: self.edit_mouse(n), width=200); t.grid(row=i, column=0, padx=4, pady=4)
            self.mouse_btns[n] = t
        w = card(col, 'Scroll wheel'); w.pack(fill='x', pady=(18, 0))
        wg = tk.Frame(w, bg=CARD); wg.pack(anchor='w')
        for i, (cap, val) in enumerate((('Wheel click', 'Middle click'), ('Scroll', 'Normal scrolling'))):
            t = Tile(wg, cap, lambda: None, width=200); t.grid(row=i, column=0, padx=4, pady=4); t.set(val)
            t.configure(cursor='arrow')
        self.refresh_mouse()

    def set_dpi(self):
        try:
            v = int(self.dpi_var.get())
        except (tk.TclError, ValueError):
            self.log('DPI must be a number'); return
        self.cfg['dpi'] = max(100, min(18000, v)); self.dpi_var.set(self.cfg['dpi']); self.save()
        self.bg(self.apply_dpi)

    def refresh_mouse(self):
        for n, t in self.mouse_btns.items():
            bd = self.cfg['scimitar'].get(n, {})
            t.set(bd.get('action'), tap_ms=bd.get('tap_ms', 0))

    def edit_mouse(self, n):
        d = BindDialog(self.root, 'Scimitar %s' % n, self.cfg['scimitar'].get(n, {}), analog=False)
        if d.result is not None:
            self.cfg['scimitar'][n] = d.result; self.save(); self.refresh_mouse()

    # ----- Tartarus -----
    # Legend printed on each key (position -> label)
    TARTARUS_LABELS = {1: '1', 2: '2', 3: '3', 4: '4', 5: '5', 6: 'Tab', 7: 'Q', 8: 'W', 9: 'E', 10: 'R',
                       11: 'Caps', 12: 'A', 13: 'S', 14: 'D', 15: 'F', 16: 'Shift', 17: 'Z', 18: 'X', 19: 'C',
                       20: 'Thumb'}

    def tartarus_page(self, p):
        self.page_title(p, 'Tartarus Pro', 'Click any key, button or direction to change what it does.')
        top = tk.Frame(p, bg=BG); top.pack(fill='x')
        c = card(top, 'Keys'); c.pack(side='left', anchor='n')
        grid = tk.Frame(c, bg=CARD); grid.pack(anchor='w')
        self.tart_btns = {}
        for k in range(1, 20):
            t = Tile(grid, str(k), lambda k=k: self.edit_tartarus(k), width=100)
            t.grid(row=(k - 1) // 5, column=(k - 1) % 5, padx=3, pady=3); self.tart_btns[k] = t
        t = Tile(grid, '20  ·  scroll', lambda: self.edit_wheel('wheel_click', 'Scroll click'), width=100)
        t.grid(row=3, column=4, padx=3, pady=3); self.tart_btns['wheel_click'] = t
        j = card(top, 'Joystick'); j.pack(side='left', anchor='n', padx=(18, 0))
        jg = tk.Frame(j, bg=CARD); jg.pack()
        compass = [('joy_upleft', '↖', 0, 0), ('joy_up', '↑', 0, 1), ('joy_upright', '↗', 0, 2),
                   ('joy_left', '←', 1, 0), ('joy_right', '→', 1, 2),
                   ('joy_downleft', '↙', 2, 0), ('joy_down', '↓', 2, 1), ('joy_downright', '↘', 2, 2)]
        for key, arrow, r_, c_ in compass:
            t = Tile(jg, arrow, lambda k=key, a=arrow: self.edit_wheel(k, 'joystick ' + a), width=80)
            t.grid(row=r_, column=c_, padx=3, pady=3); self.tart_btns[key] = t
        tk.Label(jg, text='', bg=CARD, fg=MUTED, font=(ICON_FONT, 18)).grid(row=1, column=1)
        b = card(p, 'Thumb, modifier and scroll'); b.pack(fill='x', pady=(18, 0))
        bg_ = tk.Frame(b, bg=CARD); bg_.pack(anchor='w')
        for i, (k, label) in enumerate(((20, 'Thumb key'), ('modifier', 'Modifier (Alt) button'),
                                        ('wheel_up', 'Scroll up'), ('wheel_down', 'Scroll down'))):
            cmd = (lambda: self.edit_tartarus(20)) if k == 20 else (lambda k=k, l=label: self.edit_wheel(k, l))
            t = Tile(bg_, label, cmd, width=150); t.grid(row=0, column=i, padx=3, pady=3); self.tart_btns[k] = t
        ttk.Label(b, style='CardMuted.TLabel', text='Scroll, joystick and modifier binds affect the Tartarus only; '
                  'your mouse and keyboard keep their own.').pack(anchor='w', pady=(10, 0))
        self.refresh_tartarus()

    def refresh_tartarus(self):
        for k, t in self.tart_btns.items():
            bd = self.cfg['tartarus'].get(str(k), {})
            t.set(bd.get('action'), tap_ms=bd.get('tap_ms', 0))

    def set_wheel_filter(self, on):
        self.cfg['wheel_filter'] = on; self.save()
        if hasattr(self, 'wheel_var'):
            self.wheel_var.set(on)
        if self.wheel:
            self.wheel.stop(); self.wheel = None
        if on:
            self.wheel = devices.WheelFilter(self.log, self.cfg['tartarus'], os.path.join(BASE, 'RigInput.exe'))
            self.wheel.start()
        else:
            self.log('Wheel filter: off')

    def edit_wheel(self, k, label):
        d = BindDialog(self.root, 'Tartarus %s' % label.lower(), self.cfg['tartarus'].get(k, {}), analog=False)
        if d.result is not None:
            self.cfg['tartarus'][k] = d.result; self.save(); self.refresh_tartarus()
            if (k.startswith(('joy_', 'wheel_up', 'wheel_down')) or k == 'modifier') and self.wheel:
                self.wheel.restart()   # the helper only filters keys/scroll whose binds differ from native

    def edit_tartarus(self, k):
        title = 'Tartarus thumb key' if k == 20 else 'Tartarus key %d' % k
        d = BindDialog(self.root, title, self.cfg['tartarus'].get(str(k), {}), analog=True)
        if d.result is not None:
            self.cfg['tartarus'][str(k)] = d.result; self.save(); self.refresh_tartarus()

    # ----- Status -----
    def status_page(self, p):
        self.page_title(p, 'Status', 'What RigControl is talking to right now.')
        grid = tk.Frame(p, bg=BG); grid.pack(fill='x')
        self.stat = {}
        cards = [('scimitar', 'Corsair Scimitar', ''), ('tartarus', 'Razer Tartarus Pro', ''),
                 ('lighting', 'Lighting (OpenRGB)', ''), ('cooler', 'EVGA CLC cooler', ''),
                 ('wheel', 'Tartarus wheel remap', ''), ('headset', 'Corsair Virtuoso XT', '')]
        for i, (key, title, glyph) in enumerate(cards):
            c = tk.Frame(grid, bg=CARD, highlightthickness=1, highlightbackground=BORDER, padx=16, pady=14)
            c.grid(row=i // 2, column=i % 2, sticky='nsew', padx=(0, 14), pady=(0, 14))
            grid.grid_columnconfigure(i % 2, weight=1, uniform='s')
            top = tk.Frame(c, bg=CARD); top.pack(fill='x')
            tk.Label(top, text=glyph, bg=CARD, fg=ACCENT, font=(ICON_FONT, 15)).pack(side='left')
            tk.Label(top, text=title, bg=CARD, fg=FG, font=('Segoe UI Semibold', 11)).pack(side='left', padx=10)
            st = tk.Label(top, text='', bg=CARD, fg=MUTED, font=('Segoe UI', 9)); st.pack(side='right')
            detail = tk.Label(c, text='', bg=CARD, fg=MUTED, font=('Segoe UI', 9), justify='left', anchor='w',
                              wraplength=330)
            detail.pack(fill='x', pady=(8, 0))
            self.stat[key] = (st, detail)

    def _stat(self, key, ok, state, detail):
        st, d = self.stat[key]
        st.configure(text=('●  ' + state) if state else '', fg=ACCENT if ok else (WARN if ok is None else MUTED))
        d.configure(text=detail)

    def refresh_status(self):
        tart, scim = self.readers
        cs = self.light.cooler_state
        self._stat('scimitar', scim.connected, 'Connected' if scim.connected else 'Not found',
                   'DPI %d  ·  Button 4: %s  ·  Button 5: %s' % (
                       self.cfg['dpi'], Tile.FRIENDLY.get(self.cfg['scimitar']['DPI']['action'], self.cfg['scimitar']['DPI']['action'] or '—'),
                       Tile.FRIENDLY.get(self.cfg['scimitar']['PROFILE']['action'], self.cfg['scimitar']['PROFILE']['action'] or '—')))
        self._stat('tartarus', tart.connected, 'Connected' if tart.connected else 'Not found',
                   'Driver mode, analog keys handled by RigControl' if tart.connected else 'Plug in the keypad')
        n = self.light.device_count
        tint = ''
        if self.cfg['lighting'].get('uptime_tint'):
            tint = '  ·  uptime %.1f days -> #%s' % (uptime_seconds() / 86400, self.light.resolve('fans')[0])
        self._stat('lighting', n > 0, '%d devices' % n if n else 'Starting', 'Effect %s  ·  #%s%s' % (
            self.cfg['lighting']['effect'], self.cfg['lighting']['color'], tint))
        if cs:
            self._stat('cooler', True, '%.1f °C' % cs.get('Liquid temperature', 0),
                       'Fan %s rpm  ·  pump %s rpm' % (cs.get('Fan speed', '—'), cs.get('Pump speed', '—')))
            self.cooler_lbl.configure(text='Cooler  %.1f °C' % cs.get('Liquid temperature', 0), fg=FG)
        else:
            self._stat('cooler', None, 'No reading', 'liquidctl has not read the cooler yet')
        on = bool(self.wheel)
        scroll = ('normal' if on and self.wheel.native_scroll() else
                  'disabled' if not self.cfg['tartarus'].get('wheel_up', {}).get('action') else 'remapped')
        self._stat('wheel', on, 'On' if on else 'Off', 'Wheel click %s  ·  scroll %s' % (
            self.cfg['tartarus'].get('wheel_click', {}).get('action') or 'disabled', scroll))
        self._stat('headset', False, 'Set on headset', 'Mic light (green live / red muted) and earcup colour are saved '
                   'on the headset itself, so RigControl does not need to manage them. Nothing to update.')
        for name, ok in (('Scimitar', scim.connected), ('Tartarus', tart.connected), ('Lighting', n > 0), ('Wheel remap', on)):
            self.status_dots[name].configure(fg=ACCENT if ok else MUTED)
        self.root.after(2000, self.refresh_status)

    # ----- Settings -----
    def settings_page(self, p):
        self.page_title(p, 'Settings', 'Startup and safety switches.')
        c = card(p, 'General'); c.pack(fill='x')
        self.startup_var = tk.BooleanVar(value=os.path.exists(_startup_lnk()))
        ttk.Checkbutton(c, text='Start RigControl when I sign in', variable=self.startup_var,
                        command=self.toggle_startup).pack(anchor='w')
        self.wheel_var = tk.BooleanVar(value=self.cfg.get('wheel_filter', True))
        ttk.Checkbutton(c, text='Remap the Tartarus scroll wheel', variable=self.wheel_var,
                        command=lambda: self.set_wheel_filter(self.wheel_var.get())).pack(anchor='w', pady=(6, 0))
        ttk.Label(c, style='CardMuted.TLabel', text='Untick if mouse clicks or scrolling ever misbehave '
                  '(also in the tray menu).').pack(anchor='w', padx=(26, 0))
        self.tint_var = tk.BooleanVar(value=self.cfg['lighting'].get('uptime_tint', False))
        ttk.Checkbutton(c, text='Shift all lights toward red the longer the PC is on', variable=self.tint_var,
                        command=self.toggle_uptime_tint).pack(anchor='w', pady=(6, 0))
        ttk.Label(c, style='CardMuted.TLabel', text='Full red after 7 days on; back to your colour after any shut down '
                  'or restart.').pack(anchor='w', padx=(26, 0))
        self.sparkle_var = tk.BooleanVar(value=self.cfg['lighting'].get('sparkle', False))
        ttk.Checkbutton(c, text='White sparkle on peripherals (and now and then in the case)', variable=self.sparkle_var,
                        command=self.toggle_sparkle).pack(anchor='w', pady=(6, 0))
        for key, label, lo, hi, default in (('sparkle_density', 'Sparkle amount', 0.2, 4.0, 1.0),
                                            ('sparkle_length', 'Sparkle length', 0.1, 1.5, 0.5)):
            row = tk.Frame(c, bg=CARD); row.pack(anchor='w', padx=(26, 0), pady=(4, 0))
            ttk.Label(row, style='CardMuted.TLabel', text=label, width=16).pack(side='left')
            var = tk.DoubleVar(value=self.cfg['lighting'].get(key, default))
            ttk.Scale(row, from_=lo, to=hi, variable=var, length=220,
                      command=lambda v, k=key: self.set_sparkle(k, v)).pack(side='left')
        a = card(p, 'Hardware'); a.pack(fill='x', pady=(18, 0))
        row = tk.Frame(a, bg=CARD); row.pack(anchor='w')
        flat_button(row, 'Re-apply everything', lambda: self.bg(self.reapply_all), primary=True).pack(side='left')
        flat_button(row, 'Open config folder', lambda: os.startfile(BASE)).pack(side='left', padx=8)
        ttk.Label(a, style='CardMuted.TLabel', text='Settings are saved in config.json next to RigControl.exe '
                  '(previous version kept as config.backup.json).').pack(anchor='w', pady=(10, 0))

    def toggle_startup(self):
        lnk = _startup_lnk()
        if self.startup_var.get():
            make_shortcut(lnk, *app_target()); self.log('Start at login: on')
        elif os.path.exists(lnk):
            os.remove(lnk); self.log('Start at login: off')

    # ----- Log -----
    def log_page(self, p):
        self.page_title(p, 'Log', 'Recent activity (also saved in rigcontrol.log).')
        c = tk.Frame(p, bg=CARD, highlightthickness=1, highlightbackground=BORDER); c.pack(fill='both', expand=True)
        self.txt = tk.Text(c, bg=CARD, fg=FG, insertbackground=FG, relief='flat', state='disabled',
                           font=('Consolas', 9), padx=14, pady=10, bd=0)
        sb = ttk.Scrollbar(c, orient='vertical', command=self.txt.yview); self.txt.configure(yscrollcommand=sb.set)
        sb.pack(side='right', fill='y'); self.txt.pack(fill='both', expand=True)

    def pump(self):
        n = 0
        while not self.q.empty() and n < 200:
            self.txt.configure(state='normal'); self.txt.insert('end', self.q.get() + '\n'); n += 1
        if n:
            if int(self.txt.index('end-1c').split('.')[0]) > 2000:
                self.txt.delete('1.0', '500.0')
            self.txt.see('end'); self.txt.configure(state='disabled')
        self.root.after(150, self.pump)

    # ---------- tray ----------
    def start_tray(self):
        import pystray
        from PIL import Image, ImageDraw
        img = app_icon(64)
        self.tray = pystray.Icon(APP, img, APP, pystray.Menu(
            pystray.MenuItem('Show', lambda: self.root.after(0, self.show), default=True),
            pystray.MenuItem('Re-apply all', lambda: self.bg(self.reapply_all)),
            pystray.MenuItem('Remap Tartarus wheel', lambda: self.root.after(0, self.set_wheel_filter, not self.cfg.get('wheel_filter', True)),
                             checked=lambda item: self.cfg.get('wheel_filter', True)),
            pystray.MenuItem('Exit', lambda: self.root.after(0, self.exit))))
        self.tray.run_detached()

    def hide(self):
        self.root.withdraw()

    def show(self):
        self.root.deiconify(); self.root.lift(); self.root.focus_force()

    def exit(self):
        self.root.quit()

    # ---------- lifecycle ----------
    def start(self):
        self.log('RigControl starting (%s)' % BASE)
        self.start_tray()
        for rd in self.readers:
            rd.start()
        if self.cfg.get('wheel_filter', True):
            self.set_wheel_filter(True)
        self.bg(self.startup_lighting)
        threading.Thread(target=self.watcher, daemon=True).start()
        threading.Thread(target=self.cooler_poller, daemon=True).start()
        try:
            self.root.mainloop()
        finally:
            self.shutdown()

    def shutdown(self):
        self.save()  # in case a slider save was still pending
        if self.tray:
            self.tray.stop()
        if self.wheel:
            self.wheel.stop()
        for rd in self.readers:
            rd.stop_flag.set()
        for rd in self.readers:
            rd.join(timeout=1)
        try:
            devices.set_tartarus_mode(0x00)  # plain keyboard again, so the keypad still types without RigControl
        except Exception:
            pass
        self.light.stop_server()
        self.log('RigControl stopped')


def single_instance():
    k32 = ctypes.windll.kernel32
    k32.CreateMutexW(None, False, 'Local\\RigControlSingleton')
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


if __name__ == '__main__':
    if not single_instance():
        sys.exit(0)
    app = App('--verbose' in sys.argv, '--minimized' in sys.argv)
    if '--page' in sys.argv:   # open on a given page (used for screenshots)
        app.show_page(sys.argv[sys.argv.index('--page') + 1])
    app.start()

