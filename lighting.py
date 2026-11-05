"""Lighting: one global colour/effect with per-target overrides.

Targets: fans (board D_LED2), heatsink (board I/O cover), board (other board zones), gpu,
cooler (EVGA CLC via liquidctl), mouse, mouse_dpi (Scimitar LED 3), tartarus, keyboard.
"""
import ctypes, math, os, random, socket, subprocess, threading, time
from openrgb import OpenRGBClient
from openrgb.utils import RGBColor, DeviceType

TARGETS = [('fans', 'Case fans'), ('heatsink', 'Board heatsink'), ('board', 'Board other'), ('gpu', 'GPU'),
           ('cooler', 'CPU cooler'), ('mouse', 'Mouse'), ('mouse_dpi', 'Mouse DPI light'),
           ('tartarus', 'Tartarus'), ('keyboard', 'Keyboard')]
EFFECTS = ['static', 'breathing', 'cycle', 'off']
MODE_NAMES = {'breathing': ['Breathing'], 'cycle': ['Spectrum Cycle', 'Color Cycle', 'Rainbow Wave']}
SCIMITAR_DPI_LED = 3
SCIMITAR_VISIBLE = [2, 4, 5, 6, 7]  # logo, wheel/side keypad (LED 3 = DPI light keeps its colour)
SPARKLE_TARGETS = ('keyboard', 'mouse', 'tartarus', 'fans', 'heatsink', 'gpu')
SPARKLE_CASE = ('fans', 'heatsink', 'gpu')
SPARKLE_WHOLE = 5  # single-LED devices (GPU, AIO logo) flash as a whole: one every ~8 s
SPARKLE_CASE_RATE = 0.4  # case lights sparkle only now and then (all 9 fans mirror one LED strip)
SPARKLE_RATE = 0.025   # sparkles per LED per second at density 1 (keyboard ~2.7/s)
SPARKLE_PEAK = (0.45, 1.0)     # random peak whiteness
# Settings sliders: lighting.sparkle_density (x SPARKLE_RATE) and lighting.sparkle_length (seconds, fade
# randomised 0.5x-1.5x).
# Uptime breathing: every static light in its set colour, solid through day 1, then breathing that gets faster and
# deeper until it is rapid blinking at the end of day 3. Back to solid after any shutdown/restart.
BREATH_START, BREATH_FULL = 24 * 3600, 3 * 24 * 3600
BREATH_SLOW, BREATH_FAST = 8.0, 0.5   # seconds per breath when it starts and at BREATH_FULL
BREATH_FLOOR = 0.03           # brightness at the bottom of a breath at full depth
BREATH_TICKS, BREATH_TICKS_SLOW = 2, 4   # write every Nth 30 Hz tick; single-LED GPU/AIO writes are the costly ones


def hex_rgb(h):
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


class _WTSINFOW(ctypes.Structure):
    _fields_ = ([('State', ctypes.c_int), ('SessionId', ctypes.c_uint32)]
                + [('_counter%d' % i, ctypes.c_uint32) for i in range(6)]
                + [('WinStationName', ctypes.c_wchar * 32), ('Domain', ctypes.c_wchar * 17),
                   ('UserName', ctypes.c_wchar * 21)]
                + [(n, ctypes.c_int64) for n in ('ConnectTime', 'DisconnectTime', 'LastInputTime',
                                                  'LogonTime', 'CurrentTime')])


def _since_logon():
    """Seconds since this Windows sign-in, or None. Fast Startup 'Shut down' signs out, so this resets on it."""
    wts, buf, n = ctypes.windll.wtsapi32, ctypes.c_void_p(), ctypes.c_uint32()
    if not wts.WTSQuerySessionInformationW(None, ctypes.c_uint32(0xFFFFFFFF), 24, ctypes.byref(buf), ctypes.byref(n)):
        return None
    try:
        i = _WTSINFOW.from_address(buf.value)
        return (i.CurrentTime - i.LogonTime) / 1e7 if i.LogonTime else None
    finally:
        wts.WTSFreeMemory(buf)


def uptime_seconds():
    """Time since the last startup: Windows uptime, or the sign-in time when Fast Startup kept uptime running."""
    ctypes.windll.kernel32.GetTickCount64.restype = ctypes.c_uint64
    up = ctypes.windll.kernel32.GetTickCount64() / 1000.0
    try:
        logon = _since_logon()
    except Exception:
        logon = None
    return min(up, logon) if logon is not None else up


def breath_params(up):
    """(seconds per breath, depth 0..1) after `up` seconds of uptime. Depth 0 = solid."""
    frac = min(max(up - BREATH_START, 0) / (BREATH_FULL - BREATH_START), 1.0)
    return BREATH_SLOW * (BREATH_FAST / BREATH_SLOW) ** frac, min(frac * 2, 1.0)   # speed grows evenly; depth is full by day 2


def breath_level(phase, depth):
    """Brightness at a point 0..1 of one breath: 1 at the top, down to BREATH_FLOOR at full depth."""
    return 1 - depth * (1 - BREATH_FLOOR) * math.cos(math.pi * phase) ** 2


def _port_open(port):
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


class Lighting:
    def __init__(self, cfg, log, base):
        self.cfg, self.log, self.base = cfg, log, base
        self.client, self.proc, self.lock = None, None, threading.Lock()
        self.device_count, self.cooler_state = 0, {}
        self.sparkle_base = {}  # device index -> (device, base colours, LEDs that may sparkle)
        threading.Thread(target=self._sparkle_loop, daemon=True).start()

    # ---- settings ----
    def resolve(self, target):
        L = self.cfg['lighting']
        o = L.get('overrides', {}).get(target) or {}
        return o.get('color', L['color']), o.get('effect', L['effect'])

    # ---- OpenRGB server ----
    def exe(self):
        p = self.cfg['openrgb_exe']
        return p if os.path.isabs(p) else os.path.join(self.base, p)

    def ensure_server(self):
        port = self.cfg['openrgb_port']
        if _port_open(port):
            return
        self.log('Starting OpenRGB')
        exe = self.exe()
        # No --startminimized/--gui: those open OpenRGB's window (and its dialogs). --server alone is headless.
        self.proc = subprocess.Popen([exe, '--server', '--server-host', '127.0.0.1', '--server-port', str(port),
                                      '--noautoconnect'], cwd=os.path.dirname(exe),
                                     creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(60):
            if _port_open(port):
                time.sleep(3)  # let device detection finish
                return
            time.sleep(0.5)
        raise RuntimeError('OpenRGB server did not start')

    def restart_server(self):
        """Used after a device replug: OpenRGB only detects devices at start."""
        with self.lock:
            self._drop_client()
            subprocess.run(['taskkill', '/IM', 'OpenRGB.exe', '/F'], capture_output=True,
                           creationflags=subprocess.CREATE_NO_WINDOW)
            time.sleep(1)
            self.ensure_server()

    def stop_server(self):
        self._drop_client()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _drop_client(self):
        self.sparkle_base = {}
        if self.client:
            try: self.client.disconnect()
            except Exception: pass
        self.client = None

    def _client(self):
        if self.client is None:
            self.ensure_server()
            self.client = OpenRGBClient('127.0.0.1', self.cfg['openrgb_port'], 'RigControl')
        return self.client

    # ---- apply ----
    def apply(self, live=False):
        """live=True: fast colour-only update while dragging (static Direct colours, no cooler)."""
        with self.lock:
            try:
                self._apply_openrgb(live)
            except (ConnectionError, OSError, BrokenPipeError) as e:
                self.log('OpenRGB connection lost (%s), reconnecting' % e)
                self._drop_client()
                self._apply_openrgb(live)
            if not live:
                self.apply_cooler()

    def _target_of(self, d):
        if d.type == DeviceType.GPU: return 'gpu'
        if d.type == DeviceType.MOUSE: return 'mouse'
        if 'tartarus' in d.name.lower(): return 'tartarus'
        if d.type == DeviceType.KEYBOARD: return 'keyboard'
        return None

    def _apply_openrgb(self, live):
        c = self._client()
        self.device_count = len(c.devices)
        sparkle = {k: v for k, v in self.sparkle_base.items() if k == 'aio'}  # set by apply_cooler
        zt = self.cfg.get('board_zone_targets', {})
        for d in c.devices:
            if d.type == DeviceType.MOTHERBOARD:
                if not live:
                    for z in d.zones:
                        n = self.cfg['board_zone_sizes'].get(z.name)
                        if n is not None and len(z.leds) != n:
                            z.resize(n)
                    d.update()
                zone_targets = [zt.get(z.name, 'board') for z in d.zones]
                primary = 'fans'
            else:
                t = self._target_of(d)
                zone_targets, primary = None, t
            color, effect = self.resolve(primary) if primary else (self.cfg['lighting']['color'], self.cfg['lighting']['effect'])
            if effect in MODE_NAMES and not live and self._set_hw_mode(d, effect, color):
                continue
            if not live or effect in MODE_NAMES:
                self._direct(d)
            if d.type == DeviceType.MOTHERBOARD:
                cols, leds = [], []
                for z, t in zip(d.zones, zone_targets):
                    zc = [self._rgb(t)] * len(z.leds)
                    if zc:
                        z.set_colors(zc, fast=live)
                    if t in SPARKLE_TARGETS and self.resolve(t)[1] == 'static':
                        leds += range(len(cols), len(cols) + len(zc))
                    cols += zc
                if leds and len(cols) == len(d.leds):
                    sparkle[d.id] = (d, cols, leds, SPARKLE_CASE_RATE, list(range(len(cols))))
            elif d.type == DeviceType.MOUSE:
                cols = [self._rgb('mouse')] * len(d.leds)
                if len(cols) > SCIMITAR_DPI_LED:
                    cols[SCIMITAR_DPI_LED] = self._rgb('mouse_dpi')
                d.set_colors(cols, fast=live)
                if effect == 'static':
                    leds = [i for i in SCIMITAR_VISIBLE if i < len(cols)] or list(range(len(cols)))
                    sparkle[d.id] = (d, cols, leds, 1, leds)
            else:
                cols = [self._rgb(primary)] * len(d.leds)
                d.set_colors(cols, fast=live)
                if primary in SPARKLE_TARGETS and effect == 'static':
                    rate = SPARKLE_WHOLE if len(cols) == 1 else SPARKLE_CASE_RATE if primary in SPARKLE_CASE else 1
                    sparkle[d.id] = (d, cols, list(range(len(cols))), rate, list(range(len(cols))))
            if not live:
                self.log('Lighting: %s -> %s #%s' % (d.name, effect, color))
        self.sparkle_base = sparkle

    # ---- animation: uptime breathing, with white sparkle glints over it ----
    def _sparkle_loop(self):
        active = {}  # (device id, led) -> (start time, fade, peak)
        dirty = set()
        phase, period, depth, tick, last, breathing = 0.0, BREATH_SLOW, 0.0, 0, time.time(), False
        while True:
            time.sleep(1 / 30)
            now = time.time()
            L = self.cfg['lighting']
            if tick % 300 == 0:
                period, depth = breath_params(uptime_seconds())
            if L.get('uptime_breath') and depth > 0:
                phase = (phase + (now - last) / period) % 1
                breathing = True
            elif breathing:  # switched off: repaint the base once
                dirty |= set(self.sparkle_base); breathing = False
            last, tick = now, tick + 1
            if L.get('sparkle'):
                p = SPARKLE_RATE * L.get('sparkle_density', 1.0) / 30
                length = L.get('sparkle_length', 0.5)
                for dev_id, (_, _, leds, rate, _) in list(self.sparkle_base.items()):
                    for i in leds:
                        if random.random() < p * rate and (dev_id, i) not in active:
                            active[(dev_id, i)] = (now + random.random() / 30, length * random.uniform(0.5, 1.5),
                                                   random.uniform(*SPARKLE_PEAK))
            elif active:  # switched off: repaint the base once
                dirty |= {k[0] for k in active}; active = {}
            done = [k for k, (t, fade, _) in active.items() if now - t > fade * 1.15]
            for k in done:
                del active[k]
            devs = {k[0] for k in active} | {k[0] for k in done} | dirty
            if breathing:
                devs |= {i for i, e in list(self.sparkle_base.items())
                         if tick % (BREATH_TICKS_SLOW if len(e[1]) == 1 else BREATH_TICKS) == 0}
            if devs:
                # a skipped frame (lock busy) is redrawn next time, so no LED is left half white
                level = breath_level(phase, depth) if breathing else 1.0
                dirty = set() if self._sparkle_frame(active, devs, now, level) else devs - {k[0] for k in active}

    def _sparkle_frame(self, active, devs, now=None, level=1.0):
        if not self.lock.acquire(timeout=0.02):
            return False  # a full apply or cooler read is running
        try:
            for dev_id in devs:
                entry = self.sparkle_base.get(dev_id)
                if not entry:
                    continue
                d, base = entry[:2]
                cols = list(base)
                if level < 1:
                    for i in entry[4]:
                        b = cols[i]
                        cols[i] = RGBColor(round(b.red * level), round(b.green * level), round(b.blue * level))
                for (did, i), (t, fade, peak) in active.items():
                    if did != dev_id or i >= len(cols):
                        continue
                    age, rise = max(0.0, now - t), fade * 0.15
                    k = age / rise if age < rise else max(0.0, 1 - (age - rise) / fade) ** 2
                    k *= peak
                    b = cols[i]
                    cols[i] = RGBColor(*(round(c + (255 - c) * k) for c in (b.red, b.green, b.blue)))
                if dev_id == 'aio':
                    rgb = [[c.red, c.green, c.blue] for c in cols]
                    with d.connect():
                        d.set_color('logo', 'fixed', rgb, **self._alert(rgb[0]))
                else:
                    d.set_colors(cols, fast=True)
        except Exception:
            pass  # OpenRGB restarting: the next apply rebuilds sparkle_base
        finally:
            self.lock.release()
        return True

    def _rgb(self, target):
        color, effect = self.resolve(target) if target else (self.cfg['lighting']['color'], 'static')
        return RGBColor(0, 0, 0) if effect == 'off' else RGBColor(*hex_rgb(color))

    @staticmethod
    def _direct(d):
        if d.modes[d.active_mode].name != 'Direct' and any(m.name == 'Direct' for m in d.modes):
            d.set_mode('Direct')

    def _set_hw_mode(self, d, effect, color):
        for name in MODE_NAMES[effect]:
            m = next((m for m in d.modes if m.name == name), None)
            if m is None:
                continue
            if m.colors is not None and len(m.colors):
                m.colors = [RGBColor(*hex_rgb(color))] * len(m.colors)
            d.set_mode(m)
            self.log('Lighting: %s -> %s (%s)' % (d.name, effect, name))
            return True
        return False

    def poll_cooler(self):
        """Read liquid temperature / fan / pump for the status page (same lock as colour writes)."""
        try:
            from liquidctl import find_liquidctl_devices
            with self.lock:
                for dev in find_liquidctl_devices(match='690LC'):
                    with dev.connect():
                        self.cooler_state = {k: v for k, v, _ in dev.get_status()}
                    return True
            self.cooler_state = {}
        except Exception:
            self.cooler_state = {}
        return False

    def _alert(self, rgb):
        """Cooler temperature alert. aio_alert_temp None (Bike's choice) = off: liquidctl cannot disable it,
        so it is set to 100 C in the logo's own colour."""
        t = self.cfg.get('aio_alert_temp')
        if t is None:
            return {'alert_threshold': 100, 'alert_color': list(rgb)}
        return {'alert_threshold': t}

    def apply_cooler(self):
        if not self.cfg.get('aio', True):
            return
        color, effect = self.resolve('cooler')
        rgb = list(hex_rgb(color))
        mode, colors = {'static': ('fixed', [rgb]), 'breathing': ('fading', [rgb, [0, 0, 0]]),
                        'cycle': ('rainbow', []), 'off': ('blackout', [])}[effect]
        try:
            from liquidctl import find_liquidctl_devices
            found = False
            self.sparkle_base.pop('aio', None)
            for dev in find_liquidctl_devices(match='690LC'):
                found = True
                with dev.connect():
                    # liquidctl's default alert is 45 C, which normal gaming load reaches: the logo then shows red
                    dev.set_color('logo', mode, colors, **self._alert(rgb))
                if effect == 'static':
                    self.sparkle_base['aio'] = (dev, [RGBColor(*rgb)], [0], SPARKLE_WHOLE, [0])
            self.log('Lighting: CPU cooler -> %s #%s' % (effect, color) if found else 'Lighting: CPU cooler not found')
        except Exception as e:
            self.log('Lighting: CPU cooler failed: %s' % e)
