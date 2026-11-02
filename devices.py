"""Raw HID readers for the Tartarus and Scimitar, Scimitar DPI writes, Tartarus driver mode."""
import threading, time
import hid
import sendkeys
from actions import Runner

# Keys the Tartarus types in normal keyboard mode (keys 01-20, Alt, d-pad).
TARTARUS_NATIVE_KEYS = ['1', '2', '3', '4', '5', 'TAB', 'Q', 'W', 'E', 'R', 'CAPSLOCK', 'A', 'S', 'D', 'F',
                        'LSHIFT', 'Z', 'X', 'C', 'SPACE', 'LALT', 'UP', 'DOWN', 'LEFT', 'RIGHT']

RAZER = 0x1532
CORSAIR = 0x1B1C
# Tartarus Pro (analog, report 0x06). The V2 (022B) is recognised but its driver-mode
# report is not mapped yet, so it is left in normal keyboard mode.
TARTARUS_ANALOG = {0x0244}
TARTARUS_ALL = {0x0244, 0x022B}
# Legacy-protocol Scimitars: RGB, Pro RGB, RGB Elite. (The Bragi Elite 1BE3 uses another protocol.)
SCIMITAR = {0x1B1E, 0x1B3E, 0x1B8B}


def find(vid, pids, pred=lambda d: True):
    return [d for d in hid.enumerate(vid, 0) if d['product_id'] in pids and pred(d)]


class Reader(threading.Thread):
    """Open matching HID collections, reopen after unplug, dispatch reports."""
    name = 'device'

    def __init__(self, log, on_connect=None):
        super().__init__(daemon=True)
        self.log, self.on_connect = log, on_connect
        self.stop_flag, self.handles, self.runner = threading.Event(), [], Runner(log)
        self.connected = False

    def devices(self):
        raise NotImplementedError

    def on_report(self, r):
        raise NotImplementedError

    def open_all(self):
        for d in self.devices():
            try:
                h = hid.device(); h.open_path(d['path']); h.set_nonblocking(True)
                self.handles.append(h)
            except OSError as e:
                self.log('%s: cannot open collection: %s' % (self.name, e))
        if self.handles:
            self.connected = True
            self.log('%s: connected' % self.name)
            if self.on_connect:
                threading.Thread(target=self.on_connect, args=(self.name,), daemon=True).start()

    def run(self):
        while not self.stop_flag.is_set():
            if not self.handles:
                self.open_all()
                if not self.handles:
                    self.stop_flag.wait(2); continue
            idle = True
            for h in list(self.handles):
                try:
                    r = h.read(64)
                except OSError:
                    self.log('%s: disconnected' % self.name)
                    self.close_all(); break
                if r:
                    idle = False; self.on_report(r)
            if idle:
                time.sleep(0.001)
        self.close_all()

    def close_all(self):
        self.release_all()
        for h in self.handles:
            try: h.close()
            except Exception: pass
        self.handles, self.connected = [], False

    def release_all(self):
        pass


class Tartarus(Reader):
    """Report 0x06: byte n = analog depth (0-255) of key n (1-19 keys, 20 thumb key)."""
    name = 'Tartarus'

    def __init__(self, log, binds, on_connect=None, verbose=False):
        super().__init__(log, on_connect)
        self.binds, self.down, self.verbose = binds, {}, verbose

    def devices(self):
        return find(RAZER, TARTARUS_ANALOG,
                    lambda d: d['interface_number'] == 1 and d['usage_page'] == 0x0001 and d['usage'] == 0x0000)

    def on_report(self, r):
        if r[0] != 0x06:
            return
        for pos in range(1, min(len(r), 21)):
            v = r[pos]
            if pos in self.down:
                if v <= self.down[pos][1]:
                    action, _, tap = self.down.pop(pos)
                    self.runner.release(action, tap)
                    if self.verbose: self.log('Tartarus %02d up (%d)' % (pos, v))
            elif v:
                b = self.binds.get(str(pos))
                if b and b.get('action') and v >= max(1, b.get('press', 4)):
                    tap = b.get('tap_ms', 0)
                    self.down[pos] = (b['action'], b.get('release', 0), tap)  # keep the action held even if edited
                    self.runner.press(b['action'], tap)
                    if self.verbose: self.log('Tartarus %02d down (%d) -> %s' % (pos, v, b['action']))

    def release_all(self):
        for action, _, tap in self.down.values():
            self.runner.release(action, tap)
        self.down.clear()


class Scimitar(Reader):
    """Report 0x03 on MI_00 page 0xFFC1: byte1 bit5 DPI, bit6 profile; byte2 = G1-G8, byte3 = G9-G12."""
    name = 'Scimitar'
    BUTTONS = [('DPI', 1, 0x20), ('PROFILE', 1, 0x40)] + \
              [('G%d' % (i + 1), 2, 1 << i) for i in range(8)] + \
              [('G%d' % (i + 9), 3, 1 << i) for i in range(4)]

    def __init__(self, log, binds, on_connect=None, verbose=False):
        super().__init__(log, on_connect)
        self.binds, self.down, self.verbose = binds, {}, verbose

    def devices(self):
        return find(CORSAIR, SCIMITAR, lambda d: d['interface_number'] == 0 and d['usage_page'] == 0xFFC1)

    def on_report(self, r):
        if r[0] != 0x03 or len(r) < 4:
            return
        for btn, byte, bit in self.BUTTONS:
            pressed = bool(r[byte] & bit)
            if pressed and btn not in self.down:
                b = self.binds.get(btn, {})
                action, tap = b.get('action', ''), b.get('tap_ms', 0)
                self.down[btn] = (action, tap)
                self.runner.press(action, tap)
                if self.verbose: self.log('Scimitar %s down -> %s' % (btn, action))
            elif not pressed and btn in self.down:
                self.runner.release(*self.down.pop(btn))
                if self.verbose: self.log('Scimitar %s up' % btn)

    def release_all(self):
        for action, tap in self.down.values():
            self.runner.release(action, tap)
        self.down.clear()


class WheelFilter(threading.Thread):
    """Runs RigInput.exe, which holds middle-click/wheel events, keeps the ones from the Tartarus
    (reported as MDOWN/MUP/WUP/WDOWN) and replays everyone else's. Restarts it if it exits."""
    name = 'Wheel filter'
    EVENTS = {'MDOWN': ('wheel_click', True), 'MUP': ('wheel_click', False),
              'WUP': ('wheel_up', None), 'WDOWN': ('wheel_down', None)}

    # Joystick: the Tartarus sends arrow keys natively; 8 directions come from which arrows are held.
    ARROWS = {'26': 'up', '28': 'down', '25': 'left', '27': 'right'}
    DIRS = {frozenset(['up']): 'joy_up', frozenset(['up', 'right']): 'joy_upright', frozenset(['right']): 'joy_right',
            frozenset(['down', 'right']): 'joy_downright', frozenset(['down']): 'joy_down',
            frozenset(['down', 'left']): 'joy_downleft', frozenset(['left']): 'joy_left',
            frozenset(['up', 'left']): 'joy_upleft'}
    DEFAULTS = {'joy_up': 'UP', 'joy_upright': 'UP+RIGHT', 'joy_right': 'RIGHT', 'joy_downright': 'DOWN+RIGHT',
                'joy_down': 'DOWN', 'joy_downleft': 'DOWN+LEFT', 'joy_left': 'LEFT', 'joy_upleft': 'UP+LEFT',
                'modifier': 'LALT'}

    def __init__(self, log, binds, exe):
        super().__init__(daemon=True)
        self.log, self.binds, self.exe = log, binds, exe
        self.runner, self.proc, self.stop_flag, self.held = Runner(log), None, threading.Event(), None
        self.arrows, self.joy_action, self.mod_action = set(), None, None

    def key_args(self):
        """Only filter the keyboard when a joystick/modifier bind differs from what the key does natively."""
        changed = lambda k: self.binds.get(k, {}).get('action', self.DEFAULTS[k]) != self.DEFAULTS[k]
        vks = []
        if any(changed(k) for k in self.DEFAULTS if k.startswith('joy_')):
            vks += list(self.ARROWS)
        if changed('modifier'):
            vks.append('A4')
        return vks

    def native_scroll(self):
        """Tartarus scroll bound to plain scrolling: leave every wheel event alone (no hold, no remap)."""
        return (self.binds.get('wheel_up', {}).get('action', '').upper() == 'WHEELUP' and
                self.binds.get('wheel_down', {}).get('action', '').upper() == 'WHEELDOWN' and
                not self.binds['wheel_up'].get('tap_ms') and not self.binds['wheel_down'].get('tap_ms'))

    def on_key(self, vk, down):
        if vk == 'A4':
            if down:
                if self.mod_action is None:
                    self.mod_action = self.binds.get('modifier', {}).get('action', 'LALT'); self.runner.press(self.mod_action)
            elif self.mod_action is not None:
                self.runner.release(self.mod_action); self.mod_action = None
            return
        d = self.ARROWS.get(vk)
        if not d:
            return
        (self.arrows.add if down else self.arrows.discard)(d)
        key = self.DIRS.get(frozenset(self.arrows))
        action = self.binds.get(key, {}).get('action', self.DEFAULTS.get(key, '')) if key else None
        if action != self.joy_action:
            if self.joy_action:
                self.runner.release(self.joy_action)
            self.joy_action = action
            if action:
                self.runner.press(action)

    def run(self):
        import subprocess
        while not self.stop_flag.is_set():
            args = [self.exe, '--device', 'VID_1532&PID_0244']
            if self.key_args():
                args += ['--keys', ','.join(self.key_args())]
            if self.native_scroll():
                args.append('--pass-wheel')
            scim = find(CORSAIR, SCIMITAR, lambda d: d['interface_number'] == 0 and d['usage_page'] == 0xFFC1)
            if scim:
                # the Scimitar's own button report attributes middle clicks when Raw Input doesn't arrive
                args += ['--scimitar', scim[0]['path'].decode() if isinstance(scim[0]['path'], bytes) else scim[0]['path']]
            try:
                self.proc = subprocess.Popen(args, stdin=subprocess.PIPE,
                                             stdout=subprocess.PIPE, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
            except OSError as e:
                self.log('%s: cannot start %s: %s' % (self.name, self.exe, e)); return
            for line in self.proc.stdout:
                if line.startswith('LOG '):
                    self.log('%s: %s' % (self.name, line[4:].strip())); continue
                if line.startswith(('KDOWN ', 'KUP ')):
                    kind, vk = line.split()
                    self.on_key(vk.upper(), kind == 'KDOWN'); continue
                ev = self.EVENTS.get(line.strip())
                if line.strip() == 'READY':
                    self.log('%s: running' % self.name)
                if not ev:
                    continue
                key, down = ev
                b = self.binds.get(key, {})
                action, tap = b.get('action', ''), b.get('tap_ms', 0)
                if down is True:
                    self.held = (action, tap); self.runner.press(action, tap)
                elif down is False:
                    self.runner.release(*(self.held or (action, tap))); self.held = None
                else:
                    self.runner.press(action, tap); self.runner.release(action, tap)
            for a in filter(None, (self.joy_action, self.mod_action)):   # never leave a remapped key held
                self.runner.release(a)
            self.arrows.clear(); self.joy_action = self.mod_action = None
            if not self.stop_flag.is_set():
                self.log('%s: helper exited, restarting' % self.name); self.stop_flag.wait(2)

    def restart(self):
        """Pick up a replugged Scimitar (its report path changes): the run loop starts a fresh helper."""
        if self.proc and self.proc.poll() is None:
            try: self.proc.stdin.close()
            except Exception: pass

    def stop(self):
        self.stop_flag.set()
        if self.proc and self.proc.poll() is None:
            try: self.proc.stdin.close()
            except Exception: pass
            try: self.proc.wait(2)
            except Exception: self.proc.kill()


def _razer_pkt(cmd_id, args, cls=0x00):
    """90-byte Razer report: size at 5, class at 6, id at 7, args from 8, XOR crc of 2..87 at 88."""
    pkt = bytearray(90)
    pkt[5], pkt[6], pkt[7] = len(args), cls, cmd_id
    pkt[8:8 + len(args)] = bytes(args)
    crc = 0
    for b in pkt[2:88]:
        crc ^= b
    pkt[88] = crc
    return bytes([0x00]) + bytes(pkt)


def get_tartarus_mode():
    """Razer 'get device mode' (class 00, id 84). Returns the mode byte, or None if unreadable."""
    for d in find(RAZER, TARTARUS_ANALOG, lambda d: d['interface_number'] == 2):
        h = hid.device()
        try:
            h.open_path(d['path'])
            if h.send_feature_report(_razer_pkt(0x84, [0, 0])) <= 0:
                continue
            time.sleep(0.03)
            r = h.get_feature_report(0x00, 91)
            if len(r) > 9 and r[1] == 0x02 and r[8] == 0x84:   # status OK, echo of the command id
                return r[9]
        except OSError:
            pass
        finally:
            h.close()
    return None


PROFILE_LEDS = {1: 0x0C, 2: 0x0D, 3: 0x0E}   # red, green, blue indicator (openrazer LED ids)


def set_tartarus_profile_led(profile):
    """Light one profile indicator: class 03 id 00 [varstore 01, led, on]. 0 = all off."""
    for d in find(RAZER, TARTARUS_ANALOG, lambda d: d['interface_number'] == 2):
        h = hid.device()
        try:
            h.open_path(d['path'])
            for p, led in PROFILE_LEDS.items():
                h.send_feature_report(_razer_pkt(0x00, [0x01, led, 1 if p == profile else 0], cls=0x03))
                time.sleep(0.03)
            return True
        finally:
            h.close()
    raise RuntimeError('Tartarus not found')


def set_tartarus_mode(mode):
    """Razer 'set device mode' (class 00, id 04) as a 90-byte feature report on MI_02.
    mode 0x03 = driver mode (keys silent, analog report 0x06 on); 0x00 = normal keyboard.
    Format from Synapse's own restore packet (Devices/Restore/5426_580.json)."""
    errs = []
    for d in find(RAZER, TARTARUS_ANALOG, lambda d: d['interface_number'] == 2):
        h = hid.device()
        try:
            h.open_path(d['path'])
            if h.send_feature_report(_razer_pkt(0x04, [mode, 0x00])) > 0:
                # A key held during the switch never gets its native key-up (seen: Space stuck down).
                time.sleep(0.05)
                sendkeys.release_if_held(TARTARUS_NATIVE_KEYS)
                return True
        except OSError as e:
            errs.append(str(e))
        finally:
            h.close()
    raise RuntimeError('Tartarus mode write failed: %s' % '; '.join(errs or ['keypad not found']))


def set_scimitar_dpi(dpi, rgb):
    """Write every DPI stage as `dpi` (indicator `rgb`) and select stage 1, so the DPI
    button cannot change it. Live settings only; onboard memory is not written.
    Bytes from ckb-next (nxp_proto.h / dpi.c): CMD_SET 07, FIELD_MOUSE 13, DPIPROF D0|stage, DPI 02."""
    dpi = max(100, min(18000, int(dpi)))
    devs = find(CORSAIR, SCIMITAR, lambda d: d['interface_number'] == 1 and d['usage_page'] == 0xFFC2)
    if not devs:
        raise RuntimeError('Scimitar not found')
    h = hid.device(); h.open_path(devs[0]['path'])
    try:
        def cmd(*b):
            h.write(bytes([0x00]) + bytes(b) + bytes(64 - len(b))); time.sleep(0.02)
        for stage in range(6):
            cmd(0x07, 0x13, 0xD0 | stage, 0x00, 0x01, dpi & 0xFF, dpi >> 8, dpi & 0xFF, dpi >> 8, *rgb)
        cmd(0x07, 0x13, 0x05, 0x00, 0x3F)   # all stages enabled (all identical)
        cmd(0x07, 0x13, 0x02, 0x00, 0x01)   # current stage 1
    finally:
        h.close()
    return dpi
