"""コントローラーのボタンを読む。

- PlayStation（DualSense / DualSense Edge / DUALSHOCK 4）: HID の入力レポートを読むだけ（共有で開く・何も送らない）
- Xbox 系（XInput。Xbox のコントローラー、Steam が Xbox 形にした物など）: XInputGetState で状態を見るだけ
どちらもゲームの操作とぶつからない。両方つながっていれば、どちらで押しても効く。
ボタン名は "pad:down" "pad:right" "pad:cross" "pad:l1" など（BUTTONS の鍵）。Xbox の A は cross、B は circle のように
同じ場所のボタンを同じ名前で呼ぶ。
"""
import ctypes
import threading
import time

try:
    import hid
except ImportError:  # hidapi が無ければコントローラーは使えない
    hid = None

SONY = 0x054C
MODELS = {0x0CE6: ("DualSense", "ds"), 0x0DF2: ("DualSense Edge", "ds"),
          0x05C4: ("DUALSHOCK 4", "ds4"), 0x09CC: ("DUALSHOCK 4", "ds4")}

BUTTONS = {  # 鍵: 画面に出す名前（PlayStation / Xbox）
    "down": "方向キー下", "up": "方向キー上", "left": "方向キー左", "right": "方向キー右",
    "cross": "× / A", "circle": "○ / B", "square": "□ / X", "triangle": "△ / Y",
    "l1": "L1 / LB", "r1": "R1 / RB", "l2": "L2 / LT", "r2": "R2 / RT", "l3": "L3 / LS", "r3": "R3 / RS",
    "create": "クリエイト / ビュー", "options": "オプション / メニュー", "ps": "PS", "touchpad": "タッチパッド",
    "mute": "ミュート", "fn_l": "左 Fn", "fn_r": "右 Fn", "paddle_l": "左背面", "paddle_r": "右背面",
}
_HAT = {"up": (7, 0, 1), "right": (1, 2, 3), "down": (3, 4, 5), "left": (5, 6, 7)}
_B0 = {"square": 0x10, "cross": 0x20, "circle": 0x40, "triangle": 0x80}
_B1 = {"l1": 0x01, "r1": 0x02, "l2": 0x04, "r2": 0x08, "create": 0x10, "options": 0x20, "l3": 0x40, "r3": 0x80}
_B2 = {"ps": 0x01, "touchpad": 0x02, "mute": 0x04, "fn_l": 0x10, "fn_r": 0x20, "paddle_l": 0x40, "paddle_r": 0x80}


def label(name):
    """"pad:right" → "方向キー右"、"x2" → "マウス x2"、"b" → "キー B"。"""
    if not name:
        return "（なし）"
    if name.startswith("pad:"):
        return BUTTONS.get(name[4:], name)
    if name in ("x1", "x2", "middle"):
        return f"マウス {name}"
    names = {"right": "→", "left": "←", "up": "↑", "down": "↓", "space": "スペース", "caps_lock": "CapsLock"}
    return f"キー {names.get(name, name.upper() if len(name) == 1 else name)}"


def parse(report, kind):
    """入力レポート → 押されているボタンの集合。"""
    if not report:
        return None
    rid = report[0]
    if kind == "ds":
        if rid == 0x01 and len(report) >= 11:      # USB（と BT の簡易レポート）
            o = 8
        elif rid == 0x31 and len(report) >= 12:    # Bluetooth
            o = 9
        else:
            return None
        b0, b1, b2 = report[o], report[o + 1], report[o + 2]
    else:
        if rid == 0x01 and len(report) >= 8:
            o = 5
        elif rid == 0x11 and len(report) >= 10:
            o = 7
        else:
            return None
        b0, b1, b2 = report[o], report[o + 1], report[o + 2] & 0x03
    hat = b0 & 0x0F
    on = {d for d, vals in _HAT.items() if hat in vals}
    on |= {k for k, m in _B0.items() if b0 & m}
    on |= {k for k, m in _B1.items() if b1 & m}
    on |= {k for k, m in _B2.items() if b2 & m}
    return on


# ---- XInput（Xbox 系）
_XI_BITS = {0x0001: "up", 0x0002: "down", 0x0004: "left", 0x0008: "right", 0x0010: "options", 0x0020: "create",
            0x0040: "l3", 0x0080: "r3", 0x0100: "l1", 0x0200: "r1", 0x1000: "cross", 0x2000: "circle",
            0x4000: "square", 0x8000: "triangle"}
XI_TRIGGER = 30  # LT・RT をこれより深く引いたら押したとみなす（0〜255）


class _XGAMEPAD(ctypes.Structure):
    _fields_ = [("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte), ("bRightTrigger", ctypes.c_ubyte),
                ("sThumbLX", ctypes.c_short), ("sThumbLY", ctypes.c_short),
                ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short)]


class _XSTATE(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XGAMEPAD)]


def xinput_parse(buttons, lt, rt):
    """XInput の状態 → 押されているボタンの集合（PlayStation と同じ名前）。"""
    on = {name for bit, name in _XI_BITS.items() if buttons & bit}
    if lt > XI_TRIGGER:
        on.add("l2")
    if rt > XI_TRIGGER:
        on.add("r2")
    return on


def _xinput_dll():
    for name in ("xinput1_4", "xinput1_3", "xinput9_1_0"):
        try:
            return getattr(ctypes.windll, name)
        except OSError:
            continue
    return None


class PadReader(threading.Thread):
    """つながっているコントローラーを読み続け、ボタンの押す・離すを知らせる。抜けたら待って探し直す。
    PlayStation（HID）はこのスレッド、Xbox 系（XInput）は別のスレッドで読み、押されている物を合わせる。"""

    def __init__(self):
        super().__init__(daemon=True, name="pad")
        self.pressed = set()
        self.hid_on, self.xi_on = set(), set()
        self.hid_name, self.xi_name = "", ""
        self.watchers = []        # (ボタン, 関数(押した True/離した False))
        self.any_press = None     # ボタンを押して決めるとき用（1 回だけ呼ぶ）
        self.lock = threading.Lock()
        self.fire = threading.Lock()   # 2 つのスレッドから知らせる順番をそろえる
        self.stop_ev = threading.Event()

    @property
    def device(self):
        """画面の表示用（「DualSense Edge（USB）」「Xbox 系（XInput 1）」など）。"""
        return "・".join(n for n in (self.hid_name, self.xi_name) if n)

    def start(self):
        super().start()
        threading.Thread(target=self._xinput, daemon=True, name="pad-xinput").start()

    def watch(self, button, fn):
        with self.lock:
            self.watchers.append((button, fn))
        return (button, fn)

    def unwatch(self, handle):
        with self.lock:
            if handle in self.watchers:
                self.watchers.remove(handle)

    def run(self):
        if hid is None:
            return
        while not self.stop_ev.is_set():
            try:
                info = next((d for d in hid.enumerate(SONY, 0) if d["product_id"] in MODELS), None)
            except Exception:
                info = None
            if not info:
                self.hid_name = ""
                self.stop_ev.wait(2)
                continue
            name, kind = MODELS[info["product_id"]]
            dev = hid.device()
            try:
                dev.open_path(info["path"])
                self._loop(dev, name, kind)
            except Exception:
                pass
            finally:
                try:
                    dev.close()
                except Exception:
                    pass
            self.hid_name = ""
            self._set_hid(set())
            self.stop_ev.wait(2)

    def _loop(self, dev, name, kind):
        conn = None
        while not self.stop_ev.is_set():
            r = dev.read(128, 500)
            if not r:
                continue
            if conn is None:
                conn = "Bluetooth" if r[0] in (0x31, 0x11) else "USB"
                self.hid_name = f"{name}（{conn}）"
            on = parse(r, kind)
            if on is not None and on != self.hid_on:
                self._set_hid(on)

    def _set_hid(self, on):
        self.hid_on = on
        self._update(self.hid_on | self.xi_on)

    def _xinput(self):
        """XInput を 8 ms ごとに見る。つながっていない口は 2 秒ごとにだけ見る（見るのが遅いので）。"""
        dll = _xinput_dll()
        if dll is None:
            return
        st = _XSTATE()
        live = set()
        next_scan = 0.0
        while not self.stop_ev.is_set():
            now = time.monotonic()
            slots = range(4) if now >= next_scan else sorted(live)
            if now >= next_scan:
                next_scan = now + 2.0
            on = set()
            for i in slots:
                try:
                    ok = dll.XInputGetState(i, ctypes.byref(st)) == 0
                except Exception:
                    ok = False
                if ok:
                    live.add(i)
                    g = st.Gamepad
                    on |= xinput_parse(g.wButtons, g.bLeftTrigger, g.bRightTrigger)
                else:
                    live.discard(i)
            name = f"Xbox 系（XInput {min(live) + 1}）" if live else ""
            if name != self.xi_name:
                self.xi_name = name
            if not live:
                on = set()
            if on != self.xi_on:
                self.xi_on = on
                self._update(self.hid_on | self.xi_on)
            self.stop_ev.wait(0.008 if live else 0.25)

    def _update(self, on):
        with self.fire:
            before, self.pressed = self.pressed, on
            with self.lock:
                watchers = list(self.watchers)
                grab = self.any_press
            for b in on - before:
                if grab:
                    self.any_press = None
                    grab("pad:" + b)
                    grab = None
            for button, fn in watchers:
                if (button in on) != (button in before):
                    fn(button in on)

    def stop(self):
        self.stop_ev.set()
