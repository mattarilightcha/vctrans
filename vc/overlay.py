"""字幕のオーバーレイ。ゲームのチャット欄のように、会話を左下に積んで出しつづける。

- 新しい発言は下から流れて入り、古い発言は上へ押し出される（消さない。hold_s を決めればその秒数で消す）
- ロック中: 背景は透明、クリックはゲームへ通る、手前に出てもゲームから操作を奪わない
- ロックを外すと枠が出る: ドラッグで移動、右下の角で大きさ、右クリックでメニュー、右上の「ロック」で固定
- ロックの切り替えは Ctrl+Alt+O・字幕の「ロック」・操作の窓のボタン
- ゲームの窓（follow_exe）を追いかけ、位置と大きさはゲームの画面に対する割合で持つ
- ほかのスレッドからは post() で頼む
"""
import ctypes
import queue
import time
import tkinter as tk

from . import gamewin
from .config import ROOT, save_overlay_state
from .ptt import Hotkeys

KEY = "#010101"      # この色が透明になる（文字の縁は黒なので、にじみが目立たない）
EDIT_BG = "#1c232b"
EDIT_LINE = "#6b7a89"
OUTLINE = "#000000"
FG_SRC = "#c4cbd2"
FG_JA = "#ffffff"
FG_EN = "#9fdcff"
FG_WHO = "#8f9aa5"
FG_STATUS = "#e8edf2"
FG_WARN = "#ffb35c"
FG_REC = "#ff7070"
FG_LOCK = "#ffd166"

GWL_EXSTYLE = -20
WS_EX_LAYERED, WS_EX_TRANSPARENT = 0x00080000, 0x00000020
WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE = 0x00000080, 0x08000000
HWND_TOPMOST = -1
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE = 0x2, 0x1, 0x10
SW_SHOWNOACTIVATE = 4
GRIP = 22            # 右下の角のつかむ所（px）
MAX_ENTRIES = 40

u32 = ctypes.windll.user32
ICON = ROOT / "assets" / "vctrans.ico"
STATE_KEYS = ("log_x", "log_bottom", "log_w", "log_h", "font_size", "src_size", "show_source", "locked", "hold_s")


class Overlay:
    def __init__(self, cfg, on_quit):
        self.cfg = cfg
        self.on_quit = on_quit
        self.q = queue.Queue()
        self.entries = []          # dict(id, kind, src, dst, err, t, h)
        self.status_text = ""
        self.status_color = FG_STATUS
        self.status_until = 0.0    # 0 なら出しっぱなし
        self.locked = bool(cfg.get("locked", True))
        self.game_rect = None
        self.game_hwnd = None
        self.next_scan = 0.0
        self.anim = 0              # 流れて入る残りの px
        self.anim_job = None
        self.grow_id = None        # 次に描くとき、流れて入らせる発言
        self.drag = None
        self.listeners = []        # ロックが変わったら呼ぶ（操作の窓の表示用）

        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
        prev_fg = u32.GetForegroundWindow()  # 起動の前に前面だった窓（ゲームなど）
        self.root = root = tk.Tk()
        root.withdraw()            # 作った瞬間に前へ出て操作を奪わないよう、隠したまま整える
        try:                       # このあと作る窓（操作の窓など）もこの絵になる
            root.iconbitmap(default=str(ICON))
        except tk.TclError:
            pass
        root.title("VC翻訳 字幕")
        root.overrideredirect(True)
        root.attributes("-topmost", True)
        root.attributes("-transparentcolor", KEY)
        root.configure(bg=KEY)
        self.canvas = tk.Canvas(root, bg=KEY, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        for ev, fn in (("<ButtonPress-1>", self._press), ("<B1-Motion>", self._motion),
                       ("<ButtonRelease-1>", self._release), ("<Button-3>", self._menu),
                       ("<Motion>", self._hover)):
            self.canvas.bind(ev, fn)

        self.menu = tk.Menu(root, tearoff=0)
        self.menu.add_command(label="ロックする（Ctrl+Alt+O）", command=self.toggle_edit)
        self.menu.add_separator()
        self.menu.add_command(label="文字を大きく", command=lambda: self._font(+0.0015))
        self.menu.add_command(label="文字を小さく", command=lambda: self._font(-0.0015))
        self.menu.add_command(label="原文を出す／隠す", command=self._toggle_source)
        self.menu.add_command(label="古い発言を消す／出しっぱなし", command=self._toggle_hold)
        self.menu.add_command(label="左下に戻す", command=self._reset_pos)
        self.menu.add_command(label="字幕を消す", command=self._clear)
        self.menu.add_separator()
        self.menu.add_command(label="終了", command=self.quit)

        self._place()
        root.update_idletasks()
        self.hwnd = u32.GetAncestor(self.canvas.winfo_id(), 2)
        self._apply_style()
        u32.ShowWindow(self.hwnd, SW_SHOWNOACTIVATE)
        root.deiconify()           # Tk の側にも見えていると知らせる（スタイルで前には出ない）
        self._apply_style()
        self._look()
        self._keep_top()
        if prev_fg and u32.GetForegroundWindow() == self.hwnd:
            u32.SetForegroundWindow(prev_fg)  # 字幕の窓が前面を取っていたら返す

        self.hotkeys = Hotkeys({cfg.get("edit_key", ""): lambda: self.post(self.toggle_edit),
                                cfg.get("quit_key", ""): lambda: self.post(self.quit)})
        self.hotkeys.start()
        root.after(50, self._poll)
        root.after(1000, self._tick)

    # ---- Windows の窓の性質
    def _apply_style(self):
        st = u32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        st |= WS_EX_LAYERED | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        if self.locked:
            st |= WS_EX_TRANSPARENT  # クリックを下のゲームへ通す
        else:
            st &= ~WS_EX_TRANSPARENT
        u32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, st)

    def _keep_top(self):
        u32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE)

    def _look(self):
        self.canvas.configure(bg=KEY if self.locked else EDIT_BG)
        self.root.attributes("-alpha", 1.0 if self.locked else 0.85)

    # ---- 置き場所（ゲームの画面に対する割合で持つ）
    def _base_rect(self):
        now = time.time()
        rc = gamewin.rect_of(self.game_hwnd) if self.game_hwnd else None
        if rc is None and now >= self.next_scan:
            self.next_scan = now + 5
            self.game_hwnd, rc = gamewin.find(self.cfg.get("follow_exe", ""))
        return rc or gamewin.primary()

    def _fonts(self, gh):
        c = self.cfg
        self.px_main = max(13, int(gh * c["font_size"]))
        self.px_src = max(11, int(gh * c["src_size"]))
        self.outline = max(2, self.px_main // 13)
        self.f_main = (c["font"], -self.px_main, "bold")
        self.f_src = (c["font"], -self.px_src, "bold")
        self.f_small = (c["font"], -self.px_src)

    def _place(self):
        rc = self._base_rect()
        self.game_rect = rc
        gw, gh = rc[2] - rc[0], rc[3] - rc[1]
        c = self.cfg
        self._fonts(gh)
        self.W = max(240, int(gw * c["log_w"]))
        self.H = max(120, int(gh * c["log_h"]))
        x = int(rc[0] + gw * c["log_x"])
        y = int(rc[1] + gh * (1 - c["log_bottom"]) - self.H)
        self.root.geometry(f"{self.W}x{self.H}+{x}+{y}")
        self._render()

    def _tick(self):
        """1 秒ごと: ゲームの窓が動いたら追う・時間の来た発言と状態を消す・手前に置き直す。"""
        if self.locked and self.drag is None:
            rc = self._base_rect()
            if rc != self.game_rect:
                self._place()
        now = time.time()
        hold = float(self.cfg.get("hold_s", 0) or 0)
        changed = False
        if hold > 0 and any(now - e["t"] > hold for e in self.entries):
            self.entries = [e for e in self.entries if now - e["t"] <= hold]
            changed = True
        if self.status_until and now > self.status_until:
            self.status_text, self.status_until = "", 0.0
            changed = True
        if changed:
            self._render()
        self._keep_top()
        self.root.after(1000, self._tick)

    # ---- ロック
    def toggle_edit(self):
        self.set_locked(not self.locked)

    def set_locked(self, locked):
        self.locked = bool(locked)
        self.cfg["locked"] = self.locked
        self._look()
        self._apply_style()
        self._render()
        self._save()
        for fn in self.listeners:
            fn(self.locked)

    # ---- マウス（ロックを外したときだけ届く）
    def _lock_box(self):
        return (self.W - 96, 6, self.W - 8, 6 + self.px_src + 12)

    def _in(self, box, e):
        return box[0] <= e.x <= box[2] and box[1] <= e.y <= box[3]

    def _hover(self, e):
        if self.locked:
            return
        corner = e.x > self.W - GRIP and e.y > self.H - GRIP
        self.canvas.configure(cursor="size_nw_se" if corner else ("hand2" if self._in(self._lock_box(), e)
                                                                  else "fleur"))

    def _press(self, e):
        if self.locked:
            return
        if self._in(self._lock_box(), e):
            self.set_locked(True)
            return
        mode = "size" if (e.x > self.W - GRIP and e.y > self.H - GRIP) else "move"
        self.drag = (mode, e.x_root, e.y_root, self.root.winfo_x(), self.root.winfo_y(), self.W, self.H)

    def _motion(self, e):
        if self.locked or not self.drag:
            return
        mode, sx, sy, wx, wy, w0, h0 = self.drag
        dx, dy = e.x_root - sx, e.y_root - sy
        if mode == "move":
            self.root.geometry(f"+{wx + dx}+{wy + dy}")
        else:
            self.W, self.H = max(240, w0 + dx), max(120, h0 + dy)
            self.root.geometry(f"{self.W}x{self.H}")
            self._render()

    def _release(self, e):
        if self.locked or not self.drag:
            return
        self.drag = None
        rc = self.game_rect
        gw, gh = rc[2] - rc[0], rc[3] - rc[1]
        c = self.cfg
        c["log_x"] = round((self.root.winfo_x() - rc[0]) / gw, 4)
        c["log_bottom"] = round(1 - (self.root.winfo_y() + self.H - rc[1]) / gh, 4)
        c["log_w"] = round(self.W / gw, 4)
        c["log_h"] = round(self.H / gh, 4)
        self._save()

    def _menu(self, e):
        if not self.locked:
            self.menu.tk_popup(e.x_root, e.y_root)

    def _font(self, d):
        c = self.cfg
        c["font_size"] = round(min(0.05, max(0.008, c["font_size"] + d)), 4)
        c["src_size"] = round(c["font_size"] * 0.74, 4)
        self._fonts(self.game_rect[3] - self.game_rect[1])
        self._render()
        self._save()

    def _toggle_source(self):
        self.cfg["show_source"] = not self.cfg["show_source"]
        self._render()
        self._save()

    def _toggle_hold(self):
        self.cfg["hold_s"] = 0 if float(self.cfg.get("hold_s", 0) or 0) > 0 else 60
        self._save()

    def _reset_pos(self):
        from .config import DEFAULTS
        for k in ("log_x", "log_bottom", "log_w", "log_h"):
            self.cfg[k] = DEFAULTS["overlay"][k]
        self._place()
        self._save()

    def _save(self):
        save_overlay_state({k: self.cfg.get(k) for k in STATE_KEYS})

    def _clear(self):
        self.entries.clear()
        self._render()

    def quit(self):
        try:
            self.on_quit()
        finally:
            self.root.destroy()

    # ---- ほかのスレッドから
    def post(self, fn, *args):
        self.q.put((fn, args))

    def _poll(self):
        changed = False
        try:
            while True:
                fn, args = self.q.get_nowait()
                fn(*args)
                changed = True
        except queue.Empty:
            pass
        if changed:
            self._render()
        self.root.after(50, self._poll)

    # 以下はメインスレッドで呼ばれる（post 経由）
    def set_status(self, text, rec=False, idle=False):
        """idle: ふだんの状態の案内。数秒だけ出して消す。録音中などは出しっぱなし。"""
        if self.status_color == FG_WARN and self.status_until and time.time() < self.status_until:
            return  # 注意を出している間は上書きしない
        self.status_text = text
        self.status_color = FG_REC if rec else FG_STATUS
        self.status_until = time.time() + 6 if idle else 0.0

    def warn(self, text, secs=8):
        self.status_text, self.status_color = text, FG_WARN
        self.status_until = time.time() + secs

    def add(self, eid, kind, src):
        self.entries.append(dict(id=eid, kind=kind, src=src, dst=None, err=None, t=time.time(), h=0))
        del self.entries[:-MAX_ENTRIES]
        self.grow_id = eid

    def update(self, eid, dst=None, err=None, src=None):
        for e in self.entries:
            if e["id"] == eid:
                e["dst"], e["err"], e["t"] = dst, err, time.time()
                if src:
                    e["src"] = src
                self.grow_id = eid

    # ---- 描く（下から上へ積む。新しい発言がいちばん下）
    def _text(self, x, y, text, font, fill, tag):
        """左寄せ・縁取りの文字を (x, 下端 y) に描いて、高さを返す。"""
        c = self.canvas
        o = self.outline
        wrap = max(80, self.W - x - 14 - o)
        if self.locked:  # 枠の無いときだけ縁取り（枠の上では地の色で読める）
            for dx, dy in ((-o, 0), (o, 0), (0, -o), (0, o), (-o, -o), (o, o), (-o, o), (o, -o)):
                c.create_text(x + dx, y + dy, text=text, font=font, fill=OUTLINE, anchor="sw",
                              width=wrap, justify="left", tags=tag)
        item = c.create_text(x, y, text=text, font=font, fill=fill, anchor="sw", width=wrap, justify="left",
                             tags=tag)
        x1, y1, x2, y2 = c.bbox(item)
        return y2 - y1

    def _render(self):
        c = self.canvas
        c.delete("all")
        if self.anim_job:
            self.root.after_cancel(self.anim_job)
            self.anim_job = None
        x = 12 + self.outline
        y = self.H - 8
        status_h = self.px_src + 8
        if self.status_text:
            self._text(x, y, self.status_text, self.f_small, self.status_color, "status")
        y -= status_h
        top = (self.px_src + 22) if not self.locked else 0
        if not self.locked:
            c.create_rectangle(2, 2, self.W - 3, self.H - 3, outline=EDIT_LINE, width=2, dash=(6, 4))
            c.create_text(12, 6 + (self.px_src + 12) / 2, text="ドラッグで移動・右下の角で大きさ・右クリックでメニュー",
                          font=self.f_small, fill=FG_STATUS, anchor="w")
            b = self._lock_box()
            c.create_rectangle(*b, outline=FG_LOCK, width=2, fill="#2b3440")
            c.create_text((b[0] + b[2]) / 2, (b[1] + b[3]) / 2, text="ロック", font=self.f_small, fill=FG_LOCK)
            for i in range(3):  # 右下のつかむ所
                k = 6 + i * 5
                c.create_line(self.W - k, self.H - 4, self.W - 4, self.H - k, fill=EDIT_LINE, width=2)
        sample = not self.locked and not self.entries
        entries = [dict(id=0, kind="them", src="(見本) Watch out, there's a Bastion near the extraction point.",
                        dst="気をつけて、脱出地点の近くにバスティオンがいる。", err=None, h=0),
                   dict(id=-1, kind="me", src="了解、ちょっと待とう", dst="Got it, let's wait a bit.", err=None, h=0)] \
            if sample else self.entries
        grow = 0
        for i, e in enumerate(reversed(entries)):
            y0 = y
            if e["kind"] == "them":
                main, color, src = e["dst"] or "…", FG_JA, e["src"]
            else:
                main, color, src = e["dst"] or "…", FG_EN, "自分: " + e["src"]
            if e["err"]:
                main, color = e["err"], FG_WARN
            tag = ("log", f"e{i}")
            y -= self._text(x, y, main, self.f_main, color, tag) + 1
            if self.cfg["show_source"]:
                y -= self._text(x, y, src, self.f_src, FG_SRC, tag)
            if y < top and i > 0:  # 収まりきらない古い発言は出さない（上の端で切れて見えないように）
                c.delete(f"e{i}")
                break
            y -= max(4, self.px_main // 3)
            h = y0 - y
            if i == 0 and e["id"] == self.grow_id:
                grow = max(0, h - e.get("h", 0))
            e["h"] = h
        if not self.locked:
            c.tag_raise("all")
        self.grow_id = None
        # 新しい発言は下から流れて入る（いったん下へずらしてから上げる）
        self.anim += grow
        if self.anim > 0 and self.locked:
            c.move("log", 0, self.anim)
            self.anim_job = self.root.after(16, self._anim_step)
        else:
            self.anim = 0

    def _anim_step(self):
        step = max(1, int(self.anim * 0.22 + 0.5))
        step = min(step, self.anim)
        self.canvas.move("log", 0, -step)
        self.anim -= step
        self.anim_job = self.root.after(16, self._anim_step) if self.anim > 0 else None

    def run(self):
        self.root.mainloop()
