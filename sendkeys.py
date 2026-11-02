"""Synthesize keyboard and mouse input with SendInput (VK + scan code)."""
import ctypes
from ctypes import wintypes

user32 = ctypes.WinDLL('user32', use_last_error=True)
ULONG_PTR = ctypes.c_size_t

KEYEVENTF_EXTENDEDKEY, KEYEVENTF_KEYUP = 0x1, 0x2


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [('dx', wintypes.LONG), ('dy', wintypes.LONG), ('mouseData', wintypes.DWORD),
                ('dwFlags', wintypes.DWORD), ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [('wVk', wintypes.WORD), ('wScan', wintypes.WORD), ('dwFlags', wintypes.DWORD),
                ('time', wintypes.DWORD), ('dwExtraInfo', ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [('uMsg', wintypes.DWORD), ('wParamL', wintypes.WORD), ('wParamH', wintypes.WORD)]


class _U(ctypes.Union):
    _fields_ = [('mi', MOUSEINPUT), ('ki', KEYBDINPUT), ('hi', HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [('type', wintypes.DWORD), ('u', _U)]


# name -> (vk, scan, extended)
KEYS = {
    'ESC': (0x1B, 0x01, 0), 'TAB': (0x09, 0x0F, 0), 'SPACE': (0x20, 0x39, 0), 'ENTER': (0x0D, 0x1C, 0),
    'BACKSPACE': (0x08, 0x0E, 0), 'CAPSLOCK': (0x14, 0x3A, 0),
    'LSHIFT': (0xA0, 0x2A, 0), 'RSHIFT': (0xA1, 0x36, 0), 'LCTRL': (0xA2, 0x1D, 0), 'RCTRL': (0xA3, 0x1D, 1),
    'LALT': (0xA4, 0x38, 0), 'RALT': (0xA5, 0x38, 1),
    'UP': (0x26, 0x48, 1), 'DOWN': (0x28, 0x50, 1), 'LEFT': (0x25, 0x4B, 1), 'RIGHT': (0x27, 0x4D, 1),
    'HOME': (0x24, 0x47, 1), 'END': (0x23, 0x4F, 1), 'PGUP': (0x21, 0x49, 1), 'PGDN': (0x22, 0x51, 1),
    'INSERT': (0x2D, 0x52, 1), 'DELETE': (0x2E, 0x53, 1),
    'NUM0': (0x60, 0x52, 0), 'NUM1': (0x61, 0x4F, 0), 'NUM2': (0x62, 0x50, 0), 'NUM3': (0x63, 0x51, 0),
    'NUM4': (0x64, 0x4B, 0), 'NUM5': (0x65, 0x4C, 0), 'NUM6': (0x66, 0x4D, 0), 'NUM7': (0x67, 0x47, 0),
    'NUM8': (0x68, 0x48, 0), 'NUM9': (0x69, 0x49, 0), 'NUMDOT': (0x6E, 0x53, 0), 'NUMENTER': (0x0D, 0x1C, 1),
    'NUMDIV': (0x6F, 0x35, 1), 'NUMMUL': (0x6A, 0x37, 0), 'NUMSUB': (0x6D, 0x4A, 0), 'NUMADD': (0x6B, 0x4E, 0),
    'MINUS': (0xBD, 0x0C, 0), 'EQUALS': (0xBB, 0x0D, 0), 'GRAVE': (0xC0, 0x29, 0), 'LBRACKET': (0xDB, 0x1A, 0),
    'RBRACKET': (0xDD, 0x1B, 0), 'BACKSLASH': (0xDC, 0x2B, 0), 'SEMICOLON': (0xBA, 0x27, 0),
    'QUOTE': (0xDE, 0x28, 0), 'COMMA': (0xBC, 0x33, 0), 'PERIOD': (0xBE, 0x34, 0), 'SLASH': (0xBF, 0x35, 0),
}
for _i, _sc in enumerate([0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x43, 0x44, 0x57, 0x58]):
    KEYS['F%d' % (_i + 1)] = (0x70 + _i, _sc, 0)
# F13-F24: no physical keys, so nothing else types them (FFXIV accepts them as binds)
for _i, _sc in enumerate([0x64, 0x65, 0x66, 0x67, 0x68, 0x69, 0x6A, 0x6B, 0x6C, 0x6D, 0x6E, 0x76]):
    KEYS['F%d' % (_i + 13)] = (0x7C + _i, _sc, 0)
for _c, _sc in zip('1234567890', range(0x02, 0x0C)):
    KEYS[_c] = (ord(_c), _sc, 0)
for _row, _start in (('QWERTYUIOP', 0x10), ('ASDFGHJKL', 0x1E), ('ZXCVBNM', 0x2C)):
    for _n, _c in enumerate(_row):
        KEYS[_c] = (ord(_c), _start + _n, 0)

# name -> (down flag, up flag, mouseData)
MOUSE = {'MOUSE1': (0x2, 0x4, 0), 'MOUSE2': (0x8, 0x10, 0), 'MOUSE3': (0x20, 0x40, 0),
         'MOUSE4': (0x80, 0x100, 1), 'MOUSE5': (0x80, 0x100, 2),
         'WHEELUP': (0x800, 0, 120), 'WHEELDOWN': (0x800, 0, -120)}

ALL_NAMES = sorted(KEYS) + sorted(MOUSE)

# Windows VK -> name, for capturing keys in the editor (generic modifiers map to the left key)
VK_TO_NAME = {vk: n for n, (vk, _, _) in KEYS.items() if n != 'NUMENTER'}
VK_TO_NAME.update({0x10: 'LSHIFT', 0x11: 'LCTRL', 0x12: 'LALT'})


def release_if_held(names):
    """Send key-up only for keys Windows currently reports as down; returns the ones released."""
    out = []
    for n in names:
        if user32.GetAsyncKeyState(KEYS[n][0]) & 0x8000:
            send(n, False); out.append(n)
    return out


def _send(inp):
    if user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT)) != 1:
        raise ctypes.WinError(ctypes.get_last_error())


def send(name, down):
    """Press (down=True) or release a named key or mouse button."""
    name = name.upper()
    inp = INPUT()
    if name in MOUSE:
        dflag, uflag, data = MOUSE[name]
        flag = dflag if down else uflag
        if not flag:
            return  # wheel: only acts on press
        inp.type = 0
        inp.u.mi = MOUSEINPUT(0, 0, ctypes.c_uint32(data).value, flag, 0, 0)
    else:
        vk, scan, ext = KEYS[name]
        inp.type = 1
        inp.u.ki = KEYBDINPUT(vk, scan, (KEYEVENTF_EXTENDEDKEY if ext else 0) | (0 if down else KEYEVENTF_KEYUP), 0, 0)
    _send(inp)
