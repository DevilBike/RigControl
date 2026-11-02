"""Bind actions. An action is a string:
  ""                    disabled
  "F2" / "LCTRL+F2"     key or combo, held while the button is held
  "macro: LCTRL+C 50 F2" tap each step in order on press; a number waits that many ms
"""
import threading, time
import sendkeys


def parse(action):
    a = (action or '').strip()
    if not a:
        return ('none', None)
    if a.lower().startswith('macro:'):
        steps = []
        for tok in a[6:].split():
            steps.append(('wait', int(tok)) if tok.isdigit() else ('tap', _keys(tok)))
        return ('macro', steps)
    return ('combo', _keys(a))


def _keys(tok):
    keys = [k.strip().upper() for k in tok.split('+') if k.strip()]
    for k in keys:
        if k not in sendkeys.KEYS and k not in sendkeys.MOUSE:
            raise ValueError('unknown key %r' % k)
    return keys


def validate(action):
    parse(action)


class Runner:
    """Plays one button's action: down/up for combos, a background thread for macros.
    tap_ms > 0: the combo is pressed for exactly that long on button-down and the button-up is ignored.
    tap_ms 0: held while the button is held, but for at least MIN_HOLD (FFXIV reads keys once per frame,
    so a quicker press can be missed or leave the key looking held)."""
    MIN_HOLD = 0.03

    def __init__(self, log):
        self.log = log
        self.lock, self.since, self.timers = threading.Lock(), {}, {}

    def press(self, action, tap_ms=0):
        try:
            kind, data = parse(action)
            if kind == 'combo':
                self._flush(action, data)   # a pending release of the same keys goes out first
                for k in data:
                    sendkeys.send(k, True)
                self.since[action] = time.monotonic()
                if tap_ms:
                    self._later(action, data, tap_ms / 1000)
            elif kind == 'macro':
                threading.Thread(target=self._macro, args=(data,), daemon=True).start()
        except Exception as e:
            self.log('action %r failed: %s' % (action, e))

    def release(self, action, tap_ms=0):
        if tap_ms:
            return   # the tap timer releases it
        try:
            kind, data = parse(action)
            if kind == 'combo':
                wait = self.MIN_HOLD - (time.monotonic() - self.since.get(action, 0))
                if wait > 0:
                    self._later(action, data, wait)
                else:
                    self._up(data)
        except Exception as e:
            self.log('action %r failed: %s' % (action, e))

    @staticmethod
    def _up(data):
        for k in reversed(data):
            sendkeys.send(k, False)

    def _later(self, action, data, delay):
        t = threading.Timer(delay, self._fire, (action, data))
        t.daemon = True
        with self.lock:
            old = self.timers.pop(action, None)
            if old: old.cancel()
            self.timers[action] = t
        t.start()

    def _fire(self, action, data):
        with self.lock:
            if self.timers.get(action) is not threading.current_thread():
                return   # cancelled or replaced
            del self.timers[action]
        try:
            self._up(data)
        except Exception as e:
            self.log('action %r failed: %s' % (action, e))

    def _flush(self, action, data):
        with self.lock:
            t = self.timers.pop(action, None)
        if t:
            t.cancel(); self._up(data)

    def _macro(self, steps):
        for kind, v in steps:
            if kind == 'wait':
                time.sleep(v / 1000)
            else:
                for k in v:
                    sendkeys.send(k, True)
                time.sleep(0.02)
                for k in reversed(v):
                    sendkeys.send(k, False)
                time.sleep(0.02)
