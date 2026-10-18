"""押している間だけ動くボタン（コントローラー "pad:right"・マウス "x2"・キー "f8" など）。"""
import threading

from pynput import keyboard, mouse

_MOUSE = {"x1": mouse.Button.x1, "x2": mouse.Button.x2, "middle": mouse.Button.middle}


def _vk(key):
    vk = getattr(key, "vk", None)
    if vk is None and hasattr(key, "value"):
        vk = getattr(key.value, "vk", None)
    return vk


class PushToTalk:
    def __init__(self, name, on_down, on_up, pad=None):
        self.on_down, self.on_up = on_down, on_up
        self.held = False
        name = (name or "").strip().lower()
        self.listener = None
        self.pad, self.pad_handle = pad, None
        if name.startswith("pad:"):
            if pad is None:
                raise ValueError("コントローラーを読めない（hidapi）")
            self.pad_button = name[4:]
        elif name in _MOUSE:
            self.button = _MOUSE[name]
            self.listener = mouse.Listener(on_click=self._click)
        else:
            if hasattr(keyboard.Key, name):
                target = getattr(keyboard.Key, name)
            elif len(name) == 1:
                target = keyboard.KeyCode.from_vk(ord(name.upper()))
            else:
                raise ValueError(f"ボタンの名前がわからない: {name}")
            self.vk = _vk(target)
            self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)
        self.label = name

    def start(self):
        if self.pad is not None and self.listener is None:
            self.pad_handle = self.pad.watch(self.pad_button, self._set)
        else:
            self.listener.start()

    def stop(self):
        if self.pad_handle:
            self.pad.unwatch(self.pad_handle)
            self.pad_handle = None
        if self.listener:
            self.listener.stop()
        if self.held:
            self._set(False)

    def _set(self, down):
        if down and not self.held:
            self.held = True
            self.on_down()
        elif not down and self.held:
            self.held = False
            self.on_up()

    def _click(self, x, y, button, pressed):
        if button == self.button:
            self._set(pressed)

    def _press(self, key):
        if _vk(key) == self.vk:
            self._set(True)

    def _release(self, key):
        if _vk(key) == self.vk:
            self._set(False)


_MODS = {"ctrl": {0x11, 0xA2, 0xA3}, "alt": {0x12, 0xA4, 0xA5}, "shift": {0x10, 0xA0, 0xA1}}


def _parse_combo(text):
    """"ctrl+alt+o" → ({"ctrl","alt"}, vk)。Ctrl を押していると文字が制御文字に化けるので仮想キーで見る。"""
    parts = [p.strip().lower() for p in text.split("+") if p.strip()]
    mods = {p for p in parts if p in _MODS}
    keys = [p for p in parts if p not in _MODS]
    if len(keys) != 1:
        raise ValueError(f"ホットキーの形がわからない: {text}")
    k = keys[0]
    if len(k) == 1:
        vk = ord(k.upper())
    elif hasattr(keyboard.Key, k):
        vk = _vk(getattr(keyboard.Key, k))
    else:
        raise ValueError(f"ホットキーのキー名がわからない: {text}")
    return mods, vk


class Hotkeys:
    """{"ctrl+alt+o": 関数} を押したら呼ぶ（フックの中で呼ぶので関数は軽く）。"""

    def __init__(self, table):
        self.table = [(_parse_combo(k), fn) for k, fn in table.items() if k]
        self.down = set()
        self.listener = keyboard.Listener(on_press=self._press, on_release=self._release)

    def start(self):
        self.listener.start()

    def _press(self, key):
        vk = _vk(key)
        if vk is None:
            return
        first = vk not in self.down
        self.down.add(vk)
        if not first:
            return  # 押しっぱなしの繰り返し
        for (mods, kvk), fn in self.table:
            if vk == kvk and all(self.down & _MODS[m] for m in mods):
                fn()

    def _release(self, key):
        self.down.discard(_vk(key))


def capture(pad, on_result, timeout=10):
    """次に押したボタン（コントローラー・マウスの x1/x2/中・キー）の名前を on_result に渡す。Esc か時間切れで None。"""
    done = threading.Event()
    listeners = []

    def finish(name):
        if done.is_set():
            return
        done.set()
        if pad is not None:
            pad.any_press = None
        for l in listeners:
            l.stop()
        on_result(name)

    def on_key(key):
        if key == keyboard.Key.esc:
            finish(None)
        elif isinstance(key, keyboard.Key):
            finish(key.name)
        elif getattr(key, "vk", None) and (0x30 <= key.vk <= 0x39 or 0x41 <= key.vk <= 0x5A):
            finish(chr(key.vk).lower())

    def on_click(x, y, button, pressed):
        for name, b in _MOUSE.items():
            if pressed and button == b:
                finish(name)

    if pad is not None:
        pad.any_press = finish
    listeners += [keyboard.Listener(on_press=on_key), mouse.Listener(on_click=on_click)]
    for l in listeners:
        l.start()
    threading.Timer(timeout, lambda: finish(None)).start()


# ---- キーをツールから押す（近接 VC の押して話すキーを、英語の声が流れる間だけ押しておく）
import ctypes  # noqa: E402
import ctypes.wintypes as _w  # noqa: E402


class _KI(ctypes.Structure):
    _fields_ = [("wVk", _w.WORD), ("wScan", _w.WORD), ("dwFlags", _w.DWORD), ("time", _w.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class _MI(ctypes.Structure):  # 共用体の大きさを合わせるためだけ
    _fields_ = [("dx", _w.LONG), ("dy", _w.LONG), ("mouseData", _w.DWORD), ("dwFlags", _w.DWORD),
                ("time", _w.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class _IU(ctypes.Union):
    _fields_ = [("ki", _KI), ("mi", _MI)]


class _INPUT(ctypes.Structure):
    _fields_ = [("type", _w.DWORD), ("u", _IU)]


_EXTENDED = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, 0xA3, 0xA5, 0x5B, 0x5C, 0x6F}


def key_vk(name):
    """キーの名前 → 仮想キー。マウス・コントローラーのボタンは None（ツールからは押せない）。"""
    name = (name or "").strip().lower()
    if not name or name.startswith("pad:") or name in _MOUSE:
        return None
    if len(name) == 1:
        return ord(name.upper())
    if hasattr(keyboard.Key, name):
        return _vk(getattr(keyboard.Key, name))
    return None


def press(vk, down):
    """ゲームが読む形（スキャンコード）でキーを押す・離す。"""
    u32 = ctypes.windll.user32
    scan = u32.MapVirtualKeyW(vk, 0)
    flags = 0x0008 | (0 if down else 0x0002) | (0x0001 if vk in _EXTENDED else 0)
    inp = _INPUT(type=1, u=_IU(ki=_KI(wVk=0, wScan=scan, dwFlags=flags, time=0, dwExtraInfo=0x56435452)))
    return u32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(_INPUT)) == 1


def key_is_down(vk):
    return bool(ctypes.windll.user32.GetAsyncKeyState(vk) & 0x8000)
