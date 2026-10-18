"""ゲームの画面（窓）の位置を探す。字幕をその下の真ん中に置くため。"""
import ctypes
import ctypes.wintypes as w
import os

u = ctypes.windll.user32
k = ctypes.windll.kernel32
k.OpenProcess.restype = w.HANDLE
_ENUM = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
_names = {}


def _exe(pid):
    if pid in _names:
        return _names[pid]
    name = ""
    h = k.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if h:
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = w.DWORD(1024)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                name = os.path.basename(buf.value).lower()
        finally:
            k.CloseHandle(h)
    _names[pid] = name
    return name


def rect_of(hwnd):
    r = w.RECT()
    if not hwnd or not u.IsWindow(hwnd) or not u.IsWindowVisible(hwnd) or u.IsIconic(hwnd):
        return None
    u.GetWindowRect(hwnd, ctypes.byref(r))
    if r.right - r.left < 200 or r.bottom - r.top < 200:
        return None
    return (r.left, r.top, r.right, r.bottom)


def find(exe):
    """exe の見えている一番大きな窓 → (hwnd, (左, 上, 右, 下))。無ければ (None, None)。"""
    exe = (exe or "").lower()
    if not exe:
        return None, None
    best = [None, None, 0]

    def cb(h, _):
        rc = rect_of(h)
        if rc:
            pid = w.DWORD()
            u.GetWindowThreadProcessId(h, ctypes.byref(pid))
            if _exe(pid.value) == exe:
                area = (rc[2] - rc[0]) * (rc[3] - rc[1])
                if area > best[2]:
                    best[:] = [h, rc, area]
        return True

    u.EnumWindows(_ENUM(cb), 0)
    return best[0], best[1]


def primary():
    return (0, 0, u.GetSystemMetrics(0), u.GetSystemMetrics(1))


def foreground_exe():
    """いま前面にある窓の exe の名前（小文字）。"""
    h = u.GetForegroundWindow()
    if not h:
        return ""
    pid = w.DWORD()
    u.GetWindowThreadProcessId(h, ctypes.byref(pid))
    return _exe(pid.value)
