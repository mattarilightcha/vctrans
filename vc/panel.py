"""操作の窓。いまの状態と会話を見せ、ボタン・流し方・声・機器を設定ファイルを書かずに変える。

タブ: はじめに（準備の点検・ゲーム側の設定）／ふだん（使い方・会話）／ボタン／英語の声／音の機器／API キー／声の学習。
変えた値は logs/settings.json に残り、次の起動でも効く（config.toml より優先）。
閉じるとツールも終わる。最小化しておけば字幕は出つづける。Ctrl+Alt+P で前に出す。
"""
import ctypes
import os
import re
import threading
import tkinter as tk
import webbrowser
from tkinter import filedialog, ttk

from . import config, tts
from .pad import label
from .ptt import capture, key_vk

VBCABLE_URL = "https://vb-audio.com/Cable/"
ANTHROPIC_URL = "https://console.anthropic.com/settings/keys"
AZURE_URL = "https://learn.microsoft.com/azure/ai-services/speech-service/get-started-text-to-speech"
ELEVEN_URL = "https://elevenlabs.io/app/settings/api-keys"
PIPER_URL = "https://github.com/rhasspy/piper/releases"
RATES = ["-10%", "+0%", "+10%", "+20%", "+30%"]
MODES = [("auto", "自動（おすすめ）", "英語ができたら、ツールが送るキーを押して流す。近くのプレイヤーとパーティに届く"),
         ("hold", "手で", "送るキーを自分で押している間だけ流す。途中で離すと止まり、次は頭から"),
         ("now", "すぐ流す", "できたらすぐ流す。パーティだけに届き、近くのプレイヤーには届かない")]
DEFAULT = "（既定の機器）"
WHOLE = "（使わない: ヘッドセットの音をまるごと聞く）"
F = "Yu Gothic UI"
FONT = (F, 10)

# 状態の札の色と見出し
STATES = {
    "boot": ("#5a6470", "起動中…"),
    "idle": ("#1f8a4c", "待機中（相手の英語を聞いています）"),
    "rec": ("#d43c3c", "● 録音中"),
    "busy": ("#8a6d1f", "… 英語にしています"),
    "play": ("#2a6fd6", "▶ 英語の声を流しています"),
    "wait": ("#2a6fd6", "英語の声が待っています"),
    "warn": ("#c76a12", "！ 注意"),
    "prep": ("#5a6470", "初回の準備中（終わるまで待つ）"),
}


def nb(text):
    """日本語の横の半角の空白を折り返さない空白に（Tk は空白でしか折り返さないので、「ARC は」の所で早く折れる）。"""
    return re.sub(r"(?<=[^\x00-\x7f]) | (?=[^\x00-\x7f])", "\u00a0", text or "")


class Scroll(ttk.Frame):
    """縦にスクロールする入れ物。中身は self.body に置く。"""

    def __init__(self, parent):
        super().__init__(parent)
        self.canvas = tk.Canvas(self, highlightthickness=0, bd=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = ttk.Frame(self.canvas, padding=12)
        self.item = self.canvas.create_window(0, 0, window=self.body, anchor="nw")
        self.body.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self.item, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _wheel(self, e):
        w = self.winfo_containing(e.x_root, e.y_root)
        while w is not None and w is not self:
            w = w.master
        if w is self:
            self.canvas.yview_scroll(-1 * (e.delta // 120), "units")


def _kind(app, text):
    if not app.ready and not text.startswith("注意"):
        return "prep" if text.startswith("初回の準備") else "boot"
    if text.startswith("●"):
        return "rec"
    if text.startswith("▶"):
        return "play"
    if text.startswith("注意"):
        return "warn"
    if text.startswith(("英訳中", "テスト")):
        return "busy"
    if text.startswith(("英訳ができた", "途中で離した")):
        return "wait"
    return "idle"


class Panel:
    def __init__(self, ui, app, cfg):
        self.ui, self.app, self.cfg = ui, app, cfg
        self.sp, self.ls = cfg["speak"], cfg["listen"]
        self.shown_talk = 0
        self.row_no = 0
        self.win = w = tk.Toplevel(ui.root)
        w.title("VC翻訳")
        w.minsize(640, 600)
        w.geometry("780x860+80+60")
        w.protocol("WM_DELETE_WINDOW", ui.quit)
        w.option_add("*Font", FONT)
        self.scale = max(1.0, w.winfo_fpixels("1i") / 96)  # 画面の拡大率（折り返しの幅は px なので合わせる）
        st = ttk.Style(w)
        try:
            st.theme_use("vista")
        except tk.TclError:
            pass
        for name in ("TLabel", "TButton", "TCheckbutton", "TRadiobutton", "TLabelframe.Label", "TCombobox",
                     "TNotebook.Tab"):
            st.configure(name, font=FONT)
        st.configure("TNotebook.Tab", padding=(9, 4))
        st.configure("Ok.TLabel", foreground="#1f8a4c", font=(F, 10, "bold"))
        st.configure("Ng.TLabel", foreground="#d43c3c", font=(F, 10, "bold"))
        st.configure("Wait.TLabel", foreground="#8a6d1f", font=(F, 10, "bold"))
        st.configure("Big.TLabel", font=(F, 11, "bold"))
        st.configure("Note.TLabel", foreground="#5a6470")
        st.configure("Step.TLabel", font=(F, 11))

        self._header(w)
        nb = ttk.Notebook(w)
        nb.pack(fill="both", expand=True, padx=10, pady=(8, 0))
        self.nb = nb
        self._tab_guide(nb)
        self._tab_main(nb)
        self._tab_buttons(nb)
        self._tab_voice(nb)
        self._tab_devices(nb)
        self._tab_key(nb)
        self._tab_learn(nb)
        self._footer(w)
        self._scale_wraps(w)
        self._poll()
        if not config.load_state().get("guided") or not config.api_key():
            nb.select(self.tab_guide)  # 初めての起動・キーが無いときは「はじめに」を開く
            config.save_state({"guided": True})
        else:
            nb.select(self.tab_main)
        from . import gamewin
        exe = (ui.cfg.get("follow_exe") or "").lower()
        if exe and gamewin.foreground_exe() == exe:
            # ゲーム中に起動したら前に出さない（Ctrl+Alt+P で出す）。Tk の iconify は一度前に出してから
            # 縮めるうえ、縮めたときに前面が字幕の窓へ移るので、前面を取らない最小化で出す
            game = ctypes.windll.user32.GetForegroundWindow()
            w.withdraw()
            w.after(30, lambda: self._show_min_quiet(game))

    def _scale_wraps(self, widget):
        """wraplength（px）を画面の拡大率に合わせる。あとから作った部品にも呼ぶ。"""
        stack = [widget]
        while stack:
            wdg = stack.pop()
            stack += wdg.winfo_children()
            if getattr(wdg, "_wl_scaled", False):
                continue
            try:
                wl = int(float(str(wdg.cget("wraplength")) or 0))
            except (tk.TclError, ValueError):
                continue
            if wl > 0:
                try:
                    if not str(wdg.cget("textvariable")):
                        wdg.configure(text=nb(str(wdg.cget("text"))))
                except tk.TclError:
                    pass
                wdg.configure(wraplength=int(wl * self.scale))
                wdg._wl_scaled = True

    def _show_min_quiet(self, game):
        u32 = ctypes.windll.user32
        self.win.update_idletasks()
        frame = int(self.win.wm_frame(), 16)
        u32.ShowWindow(frame, 7)  # SW_SHOWMINNOACTIVE
        if game and u32.GetForegroundWindow() != game:
            u32.SetForegroundWindow(game)  # それでも前面を取っていたらゲームへ返す

    # ---- タブ 0: はじめに
    def _tab_guide(self, nbk):
        sc = Scroll(nbk)
        nbk.add(sc, text="はじめに")
        self.tab_guide = sc
        f = sc.body
        chk = ttk.LabelFrame(f, text="準備の点検（○ がそろえば使える）", padding=(10, 6))
        chk.pack(fill="x")
        chk.columnconfigure(1, weight=1)
        self.g_rows = {}
        rows = (("asr", "文字起こし", None),
                ("cable", "VB-CABLE（英語の声をゲームのマイクへ）", ("VB-CABLE のページ", VBCABLE_URL)),
                ("cable_a", "VC だけを聞く（任意）", ("VB-CABLE A+B のページ", VBCABLE_URL)),
                ("key", "Claude の API キー（訳）", ("API キーのタブ", None)),
                ("voice", "英語の声", ("英語の声のタブ", None)))
        for i, (key, name, btn) in enumerate(rows):
            ttk.Label(chk, text=name, style="Big.TLabel").grid(row=i * 2, column=0, sticky="w", pady=(4, 0))
            v = tk.StringVar()
            lb = ttk.Label(chk, textvariable=v)
            lb.grid(row=i * 2, column=1, sticky="w", padx=10, pady=(4, 0))
            note = tk.StringVar()
            ttk.Label(chk, textvariable=note, style="Note.TLabel", wraplength=470).grid(
                row=i * 2 + 1, column=0, columnspan=2, sticky="w", padx=(12, 0))
            if btn:
                text, url = btn
                cmd = (lambda u=url: webbrowser.open(u)) if url else (lambda k=key: self._goto(k))
                ttk.Button(chk, text=text, command=cmd).grid(row=i * 2, column=2, sticky="e", pady=(4, 0))
            self.g_rows[key] = (v, lb, note)
        self.g_bar = ttk.Progressbar(chk, maximum=1.0)
        self.g_bar.grid(row=len(rows) * 2, column=0, columnspan=3, sticky="we", pady=(6, 0))
        ttk.Button(chk, text="もう一度確かめる", command=self._guide_refresh).grid(
            row=len(rows) * 2 + 1, column=2, sticky="e", pady=(6, 0))

        g = ttk.LabelFrame(f, text="ゲーム側の設定", padding=(10, 6))
        g.pack(fill="x", pady=(10, 0))
        top = ttk.Frame(g)
        top.pack(fill="x")
        ttk.Label(top, text="ゲーム").pack(side="left")
        self.profiles = config.list_profiles()
        names = [n for _, n in self.profiles]
        cur = next((n for pid, n in self.profiles if pid == self.cfg.get("profile")), "")
        self.v_prof = tk.StringVar(value=cur)
        cb = ttk.Combobox(top, textvariable=self.v_prof, values=names, state="readonly", width=24)
        cb.pack(side="left", padx=8)
        cb.bind("<<ComboboxSelected>>", lambda e: self._set_profile())
        ttk.Button(top, text="設定のフォルダ", command=lambda: os.startfile(config.PROFILES)).pack(side="right")
        self.g_setup = ttk.Frame(g)
        self.g_setup.pack(fill="x", pady=(6, 0))
        self._render_setup()

        n = ttk.LabelFrame(f, text="知っておくこと", padding=(10, 6))
        n.pack(fill="x", pady=(10, 0))
        self.v_caution = tk.StringVar()
        ttk.Label(n, textvariable=self.v_caution, wraplength=580, justify="left").pack(anchor="w")

    def _goto(self, key):
        tab = {"key": self.tab_key, "voice": self.tab_voice}.get(key)
        if tab is not None:
            self.nb.select(tab)

    def _render_setup(self):
        for c in self.g_setup.winfo_children():
            c.destroy()
        prof = self.cfg.get("profile_info") or {}
        for a, b in prof.get("setup") or []:
            r = ttk.Frame(self.g_setup)
            r.pack(fill="x", pady=1)
            ttk.Label(r, text=a, width=34, wraplength=250).pack(side="left", anchor="n")
            ttk.Label(r, text=b, style="Big.TLabel", wraplength=320).pack(side="left", anchor="n")
        for line in prof.get("notes") or []:
            ttk.Label(self.g_setup, text="・" + line, style="Note.TLabel", wraplength=580).pack(anchor="w", pady=(3, 0))
        self._scale_wraps(self.g_setup)

    def _set_profile(self):
        name = self.v_prof.get()
        pid = next((p for p, n in self.profiles if n == name), "")
        self.app.apply_profile(pid)
        self._render_setup()
        self._guide_refresh()

    def _mark(self, key, state, text, note=""):
        v, lb, nv = self.g_rows[key]
        mark = {"ok": "○ ", "ng": "× ", "wait": "… ", "info": "－ "}[state]
        v.set(mark + text)
        lb.configure(style={"ok": "Ok.TLabel", "ng": "Ng.TLabel", "wait": "Wait.TLabel", "info": "TLabel"}[state])
        nv.set(nb(note))

    def _guide_refresh(self):
        """準備の点検をやり直す（機器の一覧は別のスレッドで取る）。"""
        k = config.api_key()
        self._mark("key", "ok" if k else "ng", "入っている" if k else "まだ入っていない",
                   "" if k else "Anthropic のコンソールでキーを作り、「API キー」のタブに貼る。料金は 1 発言およそ 0.03 円")
        sp = self.sp
        eng = sp.get("engine") or "windows"
        name = next((n for key, n, _ in tts.ENGINES if key == eng), eng)
        if eng == "windows":
            voices = tts.windows_voices()
            self._mark("voice", "ok" if voices else "ng", name if voices else "Windows に英語の声が無い",
                       "" if voices else "Windows の設定 → 時刻と言語 → 音声 → 「音声の追加」で英語（米国）を足す。"
                                         "または「英語の声」のタブでほかの作り方を選ぶ")
        elif eng in ("azure", "elevenlabs"):
            has = bool(config.secret(eng))
            self._mark("voice", "ok" if has else "ng", name, "" if has else "キーを「英語の声」のタブで入れる")
        else:
            self._mark("voice", "info", name)

        def go():
            from . import prepare
            try:
                c = prepare.cables()
            except Exception:
                c = None
            self.ui.post(self._guide_cables, c)

        threading.Thread(target=go, daemon=True).start()
        self._caution_text()

    def _guide_cables(self, c):
        if c is None:
            self._mark("cable", "info", "確かめられなかった")
            return
        if c["cable"]:
            self._mark("cable", "ok", "入っている",
                       "ゲームのマイク（入力デバイス）を CABLE Output にする（下の「ゲーム側の設定」）")
        else:
            self._mark("cable", "ng", "入っていない",
                       "VB-Audio の VB-CABLE（無料）を入れて PC を再起動し、このツールを起動し直す。"
                       "入れると Windows の既定の音の機器が CABLE に変わることがあるので、サウンドの設定でヘッドセットに戻す")
        if c["cable_a"]:
            self._mark("cable_a", "ok", "CABLE-A がある",
                       "ゲームの VC の出力を CABLE-A Input にし、「音の機器」の「VC を聞く」を CABLE-A Output にすると、"
                       "効果音やゲームの英語のアナウンスを拾わない")
        else:
            self._mark("cable_a", "info", "無し: ヘッドセットの音をまるごと聞く",
                       "それでも使える（効果音・ゲームの英語のアナウンスも文字にしようとする）。"
                       "VC だけを聞くには 2 本目の仮想ケーブル VB-CABLE A+B（寄付制で有料）が要る")

    def _guide_progress(self):
        """文字起こしの準備の進み具合（_poll から 200 ms ごと）。"""
        if not hasattr(self, "g_rows"):
            return
        a, p = self.app, self.app.prep
        if p.get("error"):
            self._mark("asr", "ng", "起動できなかった", p["error"][:160])
            self.g_bar["value"] = 0
        elif p.get("total"):
            frac = p["done"] / p["total"]
            self._mark("asr", "wait", f"取得中 {frac * 100:.0f}%",
                       f"{p['label']}（{p['done'] / 1e6:,.0f} / {p['total'] / 1e6:,.0f} MB）。"
                       "初回だけ。GPU の部品とモデルで合わせて 2〜3 GB ほど")
            self.g_bar["value"] = frac
        elif a.ready:
            gpu = (p.get("gpu") or "").replace("NVIDIA ", "").replace("GeForce ", "")
            where = f"GPU（{gpu}）" if p.get("device") == "cuda" else "CPU（NVIDIA の GPU なし）"
            note = "" if p.get("device") == "cuda" else "CPU では小さいモデルで、聞き取りが遅く粗くなる"
            self._mark("asr", "ok", f"{where}・{p.get('model')}", note)
            self.g_bar["value"] = 1.0
        else:
            self._mark("asr", "wait", "読み込み中…")

    def _caution_text(self):
        sp = self.sp
        key = label(sp["send_key"]) if sp["send_key"] else ""
        lines = []
        if key and sp["send_mode"] == "auto":
            lines.append(f"・英語の声を流す間、ツールがキーボードの {key} を押す（Windows の SendInput。ゲームが前面のときだけ）。"
                         "アンチチートがこれをどう扱うかは確かめていない。自己責任で使い、気になるなら「ボタン」のタブで"
                         "流し方を「手で」にする（自分で押している間だけ流れる）")
        lines.append("・外へ送る物: 文字にした会話を Anthropic（Claude）へ。読み上げに Azure・ElevenLabs を選んだら"
                     "英訳の文をそこへ。音声そのものは PC の外へ出さない")
        lines.append("・自分の録音（声の学習）とキーは、この PC の中（logs・.env）だけに置く")
        lines.append("・ツールを閉じている間はゲームにマイクが届かない。使わない日はゲームのマイクをヘッドセットに戻す")
        self.v_caution.set(nb("\n".join(lines)))

    # ---- タブ: 英語の声
    def _tab_voice(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="英語の声")
        self.tab_voice = f
        e = ttk.LabelFrame(f, text="英語の声の作り方", padding=(10, 6))
        e.pack(fill="x")
        self.v_engine = tk.StringVar(value=self.sp.get("engine") or "windows")
        edge_ok = tts.edge_available()
        for key, name, desc in tts.ENGINES:
            if key == "edge" and not edge_ok:
                continue
            ttk.Radiobutton(e, text=name, value=key, variable=self.v_engine, command=self._set_engine).pack(anchor="w")
            ttk.Label(e, text=desc, style="Note.TLabel", wraplength=540).pack(anchor="w", padx=(24, 0), pady=(0, 3))
        self.voice_box = ttk.LabelFrame(f, text="", padding=(10, 6))
        self.voice_box.pack(fill="x", pady=(10, 0))
        c = ttk.Frame(f)
        c.pack(fill="x", pady=(10, 0))
        ttk.Label(c, text="速さ").pack(side="left")
        self.v_rate = tk.StringVar(value=self.sp.get("rate") or "+10%")
        cb = ttk.Combobox(c, textvariable=self.v_rate, values=RATES, width=6, state="readonly")
        cb.pack(side="left", padx=(6, 16))
        cb.bind("<<ComboboxSelected>>", lambda ev: self._save("speak", "rate", self.v_rate.get()))
        ttk.Label(c, text="VC へ流すとき自分にも聞かせる").pack(side="left")
        self.v_mon = tk.DoubleVar(value=float(self.sp["monitor_volume"]))
        ttk.Scale(c, from_=0.0, to=1.0, variable=self.v_mon, length=120,
                  command=lambda x: self._save("speak", "monitor_volume", round(float(x), 2))).pack(side="left", padx=6)
        t = ttk.Frame(f)
        t.pack(fill="x", pady=(10, 0))
        ttk.Button(t, text="試しに読む（自分にだけ鳴らす）", command=self._preview).pack(side="left")
        self.v_prev = tk.StringVar()
        ttk.Label(t, textvariable=self.v_prev, style="Note.TLabel", wraplength=380).pack(side="left", padx=8)
        self._voice_fields()

    def _set_engine(self):
        self._save("speak", "engine", self.v_engine.get())
        self._voice_fields()
        self._guide_refresh()

    def _voice_fields(self):
        box = self.voice_box
        for c in box.winfo_children():
            c.destroy()
        eng = self.v_engine.get()
        box.configure(text={"windows": "Windows の声", "azure": "Azure AI Speech", "elevenlabs": "ElevenLabs",
                            "piper": "Piper", "edge": "Edge の読み上げ（試し）"}.get(eng, eng))
        box.columnconfigure(1, weight=1)
        r = [0]

        def row(text, widget, button=None):
            ttk.Label(box, text=text).grid(row=r[0], column=0, sticky="w", pady=2)
            widget.grid(row=r[0], column=1, sticky="we", padx=8, pady=2)
            if button:
                button.grid(row=r[0], column=2, sticky="e", pady=2)
            r[0] += 1

        def note(text):
            ttk.Label(box, text=text, style="Note.TLabel", wraplength=560).grid(row=r[0], column=0, columnspan=3,
                                                                              sticky="w", pady=(4, 0))
            r[0] += 1

        def combo(key, values, width=34):
            v = tk.StringVar(value=self.sp.get(key) or "")
            cb = ttk.Combobox(box, textvariable=v, values=values, width=width)
            cb.bind("<<ComboboxSelected>>", lambda ev: self._save("speak", key, v.get()))
            cb.bind("<FocusOut>", lambda ev: self._save("speak", key, v.get().strip()))
            return cb

        def entry(key, show=""):
            v = tk.StringVar(value=self.sp.get(key) or "")
            en = ttk.Entry(box, textvariable=v, show=show)
            en.bind("<FocusOut>", lambda ev: self._save("speak", key, v.get().strip()))
            en.bind("<Return>", lambda ev: self._save("speak", key, v.get().strip()))
            return en

        def secret(kind):
            v = tk.StringVar()
            en = ttk.Entry(box, textvariable=v, show="●")
            have = config.secret(kind)

            def save(ev=None):
                val = v.get().strip()
                if val:
                    config.save_secret(kind, val)
                    v.set("")
                    lb.configure(text="入っている（…" + val[-4:] + "）")
                    self._guide_refresh()
            en.bind("<Return>", save)
            b = ttk.Button(box, text="保存", command=save)
            lb = ttk.Label(box, text=("入っている（…" + have[-4:] + "）") if have else "まだ入っていない",
                           style="Note.TLabel")
            return en, b, lb

        def browse(key, title, types):
            def go():
                p = filedialog.askopenfilename(parent=self.win, title=title, filetypes=types)
                if p:
                    self._save("speak", key, p)
                    self._voice_fields()
            return go

        if eng == "windows":
            voices = tts.windows_voices()
            row("声", combo("windows_voice", voices))
            note("空なら最初の英語の声。Windows の設定 → 時刻と言語 → 音声 → 「音声の追加」で英語（米国・英国）の声を足せる。"
                 if voices else "英語の声が入っていない。Windows の設定 → 時刻と言語 → 音声 → 「音声の追加」で英語（米国）を足す。")
        elif eng == "azure":
            en, b, lb = secret("azure")
            row("キー", en, b)
            lb.grid(row=r[0], column=1, sticky="w", padx=8)
            r[0] += 1
            row("地域", entry("azure_region"))
            row("声", combo("azure_voice", tts.AZURE_VOICES))
            note("Azure のポータルで「音声サービス（Speech）」のリソースを作り、キーと地域（japaneast など）を入れる。"
                 "無料の枠（F0）で月 50 万字まで。英訳の文を Microsoft へ送る。")
            ttk.Button(box, text="Azure の始め方", command=lambda: webbrowser.open(AZURE_URL)).grid(
                row=r[0], column=0, sticky="w", pady=(4, 0))
        elif eng == "elevenlabs":
            en, b, lb = secret("elevenlabs")
            row("キー", en, b)
            lb.grid(row=r[0], column=1, sticky="w", padx=8)
            r[0] += 1
            row("声の ID", entry("elevenlabs_voice"))
            row("モデル", combo("elevenlabs_model", ["eleven_flash_v2_5", "eleven_turbo_v2_5", "eleven_multilingual_v2"]))
            note("ElevenLabs の設定でキーを作る。声の ID は Voices の画面で写す。使った字数で課金される。"
                 "英訳の文を ElevenLabs へ送る。")
            ttk.Button(box, text="ElevenLabs のキーの画面", command=lambda: webbrowser.open(ELEVEN_URL)).grid(
                row=r[0], column=0, sticky="w", pady=(4, 0))
        elif eng == "piper":
            row("piper.exe", entry("piper_exe"),
                ttk.Button(box, text="選ぶ", command=browse("piper_exe", "piper.exe", [("piper", "*.exe")])))
            row("声のモデル", entry("piper_model"),
                ttk.Button(box, text="選ぶ", command=browse("piper_model", "声のモデル", [("Piper の声", "*.onnx")])))
            note("Piper の Windows 版（piper_windows_amd64.zip）と、英語の声のモデル（.onnx と .onnx.json）を自分で落として置く。"
                 "声のモデルごとにライセンスが違うので、配っている所の条件を確かめる。")
            ttk.Button(box, text="Piper のページ", command=lambda: webbrowser.open(PIPER_URL)).grid(
                row=r[0], column=0, sticky="w", pady=(4, 0))
        elif eng == "edge":
            row("声", combo("voice", tts.EDGE_VOICES))
            note("Microsoft Edge の「音声で読み上げる」と同じ所を借りる。Microsoft が外のツール向けに開いている方法ではないので、"
                 "急に使えなくなることがある。英訳の文を Microsoft へ送る。")
        self._scale_wraps(box)

    def _preview(self):
        self.v_prev.set("作っています…")

        def go():
            from .audio import play_once
            try:
                audio, sr = tts.synth("Hey, can you hear me? Let's extract together.", self.sp)
                play_once(audio, sr, self.ls.get("forward_device") or "", 0.8)
                msg = f"○ 鳴らした（{len(audio) / sr:.1f} 秒）"
            except Exception as e:
                msg = f"× {e}"
            self.ui.post(self.v_prev.set, msg)

        threading.Thread(target=go, daemon=True).start()

    # ---- タブ: API キー
    def _tab_key(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="API キー")
        self.tab_key = f
        box = ttk.LabelFrame(f, text="Claude の API キー（訳に使う）", padding=10)
        box.pack(fill="x")
        box.columnconfigure(1, weight=1)
        ttk.Label(box, text="いまのキー").grid(row=0, column=0, sticky="w")
        self.v_key_now = tk.StringVar()
        ttk.Label(box, textvariable=self.v_key_now, style="Big.TLabel").grid(row=0, column=1, sticky="w", padx=10)
        ttk.Label(box, text="新しいキー").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.v_key = tk.StringVar()
        self.e_key = ttk.Entry(box, textvariable=self.v_key, show="●", font=(F, 11))
        self.e_key.grid(row=1, column=1, sticky="we", padx=10, pady=(10, 0))
        self.e_key.bind("<Return>", lambda e: self._save_key())
        self.v_show = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="見せる", variable=self.v_show,
                        command=lambda: self.e_key.configure(show="" if self.v_show.get() else "●")).grid(
            row=1, column=2, sticky="w", pady=(10, 0))
        b = ttk.Frame(box)
        b.grid(row=2, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Button(b, text="保存して使う", command=self._save_key).pack(side="left")
        ttk.Button(b, text="いまのキーで訳せるか確かめる", command=self._check_key).pack(side="left", padx=6)
        self.v_key_msg = tk.StringVar()
        self.l_key_msg = tk.Label(box, textvariable=self.v_key_msg, font=FONT, anchor="w", justify="left",
                                  wraplength=560)
        self.l_key_msg.grid(row=3, column=0, columnspan=3, sticky="w", pady=(10, 0))
        ttk.Label(f, text="キーは Anthropic のコンソールの「API Keys」で作る（sk-ant- で始まる文字列）。"
                          f"保存先は {config.ENV}（この PC の中だけ）。",
                  style="Note.TLabel", wraplength=580).pack(anchor="w", pady=(10, 0))
        ttk.Button(f, text="Anthropic のコンソールを開く", command=lambda: webbrowser.open(ANTHROPIC_URL)).pack(
            anchor="w", pady=(4, 0))
        ttk.Label(f, text="料金は使う人の Anthropic の口座から。目安は Claude Haiku 5.5 で 1 発言およそ 0.03 円"
                          "（入力 約 1,700 トークン・出力 数十トークン。100 万トークンで入力 $0.10・出力 $0.50）。"
                          "1 時間に 300 発言で約 10 円。",
                  style="Note.TLabel", wraplength=580).pack(anchor="w", pady=(8, 0))
        ttk.Label(f, text="送る物: 文字にした会話（相手の英語・自分の日本語）と直前の数行・ゲームの用語。"
                          "音声そのものは送らない。",
                  style="Note.TLabel", wraplength=580).pack(anchor="w", pady=(4, 0))
        self._key_state()

    def _key_state(self):
        k = config.api_key()
        self.v_key_now.set(f"入っている（…{k[-4:]}）" if k else "まだ入っていない")
        if hasattr(self, "g_rows"):
            self._guide_refresh()

    def _key_msg(self, ok, text):
        self.v_key_msg.set(("○ " if ok else "× ") + text)
        self.l_key_msg.configure(fg="#1f8a4c" if ok else "#d43c3c")

    def _save_key(self):
        key = self.v_key.get().strip().strip("\"'")
        if not key:
            self._key_msg(False, "新しいキーが空")
            return
        if not key.startswith("sk-ant-"):
            self._key_msg(False, "sk-ant- で始まっていない。コピーしたキーを確かめる")
            return
        self.app.set_api_key(key)
        self.v_key.set("")
        self._key_state()
        self._key_msg(True, "保存した。確かめています…")
        self._check_key()

    def _check_key(self):
        self.v_key_msg.set("確かめています…")
        self.l_key_msg.configure(fg="#5a6470")

        def go():
            ok, text = self.app.check_api()
            self.ui.post(self._key_msg, ok, text)

        threading.Thread(target=go, daemon=True).start()

    # ---- いちばん上: 状態の札と 3 つのランプ
    def _header(self, w):
        self.card = tk.Frame(w, bg=STATES["boot"][0], padx=14, pady=10)
        self.card.pack(fill="x", padx=10, pady=(10, 0))
        self.v_head = tk.StringVar()
        self.v_sub = tk.StringVar()
        self.l_head = tk.Label(self.card, textvariable=self.v_head, font=(F, 15, "bold"), fg="white",
                               bg=STATES["boot"][0], anchor="w")
        self.l_head.pack(fill="x")
        self.l_sub = tk.Label(self.card, textvariable=self.v_sub, font=(F, 10), fg="white", bg=STATES["boot"][0],
                              anchor="w", justify="left", wraplength=580)
        self.l_sub.pack(fill="x")

        g = ttk.Frame(w, padding=(12, 8, 12, 0))
        g.pack(fill="x")
        self.lamps = {}
        for i, (key, text) in enumerate((("vc", "相手の声（VC）"), ("mic", "自分のマイク"), ("pad", "コントローラー"))):
            c = ttk.Frame(g)
            c.grid(row=0, column=i, sticky="we", padx=(0, 14))
            g.columnconfigure(i, weight=1)
            ttk.Label(c, text=text).pack(anchor="w")
            if key == "pad":
                v = tk.StringVar()
                lb = tk.Label(c, textvariable=v, font=(F, 10, "bold"), anchor="w")
                lb.pack(anchor="w", pady=(3, 0))
                self.lamps[key] = (v, lb)
            else:
                bar = ttk.Progressbar(c, length=170, maximum=1.0)
                bar.pack(anchor="w", fill="x", pady=(4, 0))
                self.lamps[key] = bar

    # ---- タブ 1: ふだん
    def _tab_main(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="ふだん")
        self.tab_main = f
        how = ttk.LabelFrame(f, text="使い方", padding=(10, 6))
        how.pack(fill="x")
        self.v_steps = [tk.StringVar() for _ in range(3)]
        for v in self.v_steps:
            ttk.Label(how, textvariable=v, style="Step.TLabel", wraplength=560).pack(anchor="w", pady=1)

        log = ttk.LabelFrame(f, text="会話（新しいものが下）", padding=6)
        log.pack(fill="both", expand=True, pady=(10, 0))
        self.talk = tk.Text(log, height=9, wrap="word", font=(F, 10), relief="flat", bg="#f7f8fa",
                            padx=8, pady=6, state="disabled", cursor="arrow")
        sb = ttk.Scrollbar(log, command=self.talk.yview)
        self.talk.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.talk.pack(fill="both", expand=True)
        self.talk.tag_configure("time", foreground="#8a939d", font=(F, 9))
        self.talk.tag_configure("them_src", foreground="#5a6470")
        self.talk.tag_configure("them_dst", foreground="#1b1f24", font=(F, 11, "bold"))
        self.talk.tag_configure("me_src", foreground="#5a6470")
        self.talk.tag_configure("me_dst", foreground="#1f5fbf", font=(F, 11, "bold"))
        self.talk.tag_configure("hint", foreground="#8a939d")
        self.talk.tag_configure("fix", foreground="#8a939d", font=(F, 9))
        self._talk_hint()

        t = ttk.Frame(f)
        t.pack(fill="x", pady=(10, 0))
        t.columnconfigure(1, weight=1)
        ttk.Label(t, text="日本語を打って流す").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.v_typed = tk.StringVar()
        e = ttk.Entry(t, textvariable=self.v_typed, font=(F, 11))
        e.grid(row=0, column=1, sticky="we")
        e.bind("<Return>", lambda ev: self._say())
        ttk.Button(t, text="英語で流す（Enter）", command=self._say).grid(row=0, column=2, padx=(6, 0))

        b = ttk.Frame(f)
        b.pack(fill="x", pady=(8, 0))
        ttk.Button(b, text="英語の声をテスト", command=self._test).pack(side="left")
        ttk.Button(b, text="最後の英語をもう一度", command=self.app.replay).pack(side="left", padx=6)
        ttk.Button(b, text="字幕を消す", command=self.ui._clear).pack(side="right")
        self.b_lock = ttk.Button(b, command=self.ui.toggle_edit, width=22)
        self.b_lock.pack(side="right", padx=6)
        self.ui.listeners.append(lambda locked: self._lock_text())
        self._lock_text()

    def _lock_text(self):
        self.b_lock.configure(text="字幕のロックを外す（動かす）" if self.ui.locked else "字幕をロックする")

    def _talk_hint(self):
        self.talk.configure(state="normal")
        self.talk.insert("end", "ここに会話が出ます。相手の英語は日本語に、自分の日本語は英語にして並べます。", "hint")
        self.talk.configure(state="disabled")

    def _test(self):
        self.app.test_voice()
        self.win.iconify()  # ゲームの画面へ戻りやすく

    # ---- タブ 2: ボタンと声
    def _tab_buttons(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="ボタン")
        k = ttk.LabelFrame(f, text="ボタン", padding=10)
        k.pack(fill="x")
        k.columnconfigure(1, weight=1)
        self.v_ptt, self.v_send = tk.StringVar(), tk.StringVar()
        self.b_ptt = self._button_row(k, 0, "録るボタン", "押している間の日本語を英語にする", self.v_ptt, "ptt")
        self.b_send = self._button_row(k, 2, "送るキー", "ゲームの「近接ボイスチャット（押して話す）」と同じキー",
                                       self.v_send, "send_key")

        m = ttk.LabelFrame(f, text="英語の声の流し方", padding=10)
        m.pack(fill="x", pady=(10, 0))
        self.v_mode = tk.StringVar(value=self.sp["send_mode"])
        for key, name, desc in MODES:
            ttk.Radiobutton(m, text=name, value=key, variable=self.v_mode, command=self._set_mode).pack(anchor="w")
            ttk.Label(m, text=desc, style="Note.TLabel", wraplength=540).pack(anchor="w", padx=(24, 0), pady=(0, 4))

        v = ttk.LabelFrame(f, text="声と字幕", padding=10)
        v.pack(fill="x", pady=(10, 0))
        self.v_pass = tk.BooleanVar(value=bool(self.sp["passthrough"]))
        ttk.Checkbutton(v, text="ふだんの声もそのまま VC へ（パーティに聞こえる）", variable=self.v_pass,
                        command=lambda: self._save("speak", "passthrough", self.v_pass.get())).grid(
            row=2, column=0, columnspan=2, sticky="w")
        self.v_mute = tk.BooleanVar(value=bool(self.sp["mute_while_recording"]))
        ttk.Checkbutton(v, text="録っている間の日本語は VC へ流さない", variable=self.v_mute,
                        command=lambda: self._save("speak", "mute_while_recording", self.v_mute.get())).grid(
            row=3, column=0, columnspan=2, sticky="w")
        self.v_src = tk.BooleanVar(value=bool(self.ui.cfg["show_source"]))
        ttk.Checkbutton(v, text="字幕に原文も小さく出す", variable=self.v_src, command=self._toggle_src).grid(
            row=4, column=0, columnspan=2, sticky="w")
        self.v_keep = tk.BooleanVar(value=not float(self.ui.cfg.get("hold_s", 0) or 0))
        ttk.Checkbutton(v, text="字幕を出しっぱなしにする（外すと 60 秒で消える）", variable=self.v_keep,
                        command=self._toggle_keep).grid(row=5, column=0, columnspan=2, sticky="w")

    def _button_row(self, parent, row, name, desc, var, key):
        ttk.Label(parent, text=name, style="Big.TLabel").grid(row=row, column=0, sticky="w")
        tk.Label(parent, textvariable=var, font=(F, 11, "bold"), fg="#1f5fbf").grid(row=row, column=1, sticky="w",
                                                                                   padx=10)
        b = ttk.Button(parent, text="押して決める", command=lambda: self._capture(key))
        b.grid(row=row, column=2, sticky="e")
        ttk.Label(parent, text=desc, style="Note.TLabel").grid(row=row + 1, column=0, columnspan=3, sticky="w",
                                                              pady=(0, 8))
        return b

    # ---- タブ 3: 音の機器
    def _tab_devices(self, nb):
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="音の機器")
        d = ttk.LabelFrame(f, text="このツールの機器", padding=10)
        d.pack(fill="x")
        d.columnconfigure(1, weight=1)
        self.devs = {"loopback": [], "input": [], "output": []}
        rows = (("マイク", "自分の声", "speak", "mic_device", "input"),
                ("VC へ出す先", "英語の声とふだんの声。ゲームのマイクはこれの出口（CABLE Output）にする", "speak",
                 "vc_device", "output"),
                ("VC を聞く", "ゲームのボイスチャットの出力先にしたケーブル（CABLE-A など。無ければ使わない）", "listen",
                 "vc_source", "vc"),
                ("VC の中継先", "自分が聞くヘッドセット", "listen", "forward_device", "output"),
                ("まるごと聞く機器", "「VC を聞く」を使わないとき", "listen", "device", "loopback"))
        self.dev_vars = []
        for i, (name, desc, sec, key, kind) in enumerate(rows):
            ttk.Label(d, text=name, style="Big.TLabel").grid(row=i * 2, column=0, sticky="w")
            v = tk.StringVar(value=self.cfg[sec][key] or (WHOLE if kind == "vc" else DEFAULT))
            cb = ttk.Combobox(d, textvariable=v, width=44, state="readonly", postcommand=self._load_devices)
            cb.grid(row=i * 2, column=1, sticky="we", padx=(10, 0))
            cb.bind("<<ComboboxSelected>>", lambda e, s=sec, k=key, v=v: self._set_device(s, k, v.get()))
            ttk.Label(d, text=desc, style="Note.TLabel").grid(row=i * 2 + 1, column=0, columnspan=2, sticky="w",
                                                             pady=(0, 6))
            self.dev_vars.append((cb, kind))

    def _load_devices(self):
        from .audio import cable_id, device_names
        try:
            self.devs, _, _ = device_names()
        except Exception:
            return
        for cb, kind in self.dev_vars:
            if kind == "vc":
                mine = [n for n in self.devs["output"] if self.sp["vc_device"] and self.sp["vc_device"] in n]
                used = {cable_id(n) for n in mine}  # 英語の声を出すケーブルは外す
                cb["values"] = [WHOLE] + [n for n in self.devs["input"]
                                          if "CABLE" in n.upper() and cable_id(n) not in used]
            else:
                cb["values"] = [DEFAULT] + self.devs[kind]

    def _set_device(self, sec, key, name):
        name = "" if name in (DEFAULT, WHOLE) else name
        self._save(sec, key, name)
        if not self.app.ready:
            return
        fn = self.app.open_listener if sec == "listen" else self.app.open_router
        threading.Thread(target=fn, daemon=True).start()

    # ---- いちばん下
    def _footer(self, w):
        f = ttk.Frame(w, padding=(10, 8, 10, 10))
        f.pack(fill="x")
        ttk.Label(f, text="閉じると終了。最小化しても字幕は出る。Ctrl+Alt+P でこの窓を出す。",
                  style="Note.TLabel").pack(side="left")
        ttk.Button(f, text="終了", command=self.ui.quit).pack(side="right")
        ttk.Button(f, text="記録のフォルダ", command=lambda: os.startfile(config.ROOT / "logs")).pack(side="right",
                                                                                             padx=6)
        ttk.Button(f, text="説明とライセンス", command=lambda: os.startfile(config.ROOT)).pack(side="right")

    def _toggle_keep(self):
        self.ui.cfg["hold_s"] = 0 if self.v_keep.get() else 60
        self.ui._save()

    def _toggle_src(self):
        self.ui.cfg["show_source"] = self.v_src.get()
        self.ui._save()
        self.ui._render()

    # ---- 変える
    def _save(self, sec, key, value):
        self.cfg[sec][key] = value
        config.save_setting(sec, key, value)

    def _set_mode(self):
        mode = self.v_mode.get()
        if mode == "auto" and key_vk(self.sp["send_key"]) is None:
            self.app.warn("自動で押せるのはキーボードのキーだけ。送るキーをキーボードのキーにする")
        self._save("speak", "send_mode", mode)
        self.app.bind_buttons()

    def _capture(self, key):
        btn = self.b_ptt if key == "ptt" else self.b_send
        btn.configure(text="押す…（Esc でやめる）", state="disabled")
        self.app.capturing = True

        def done(name):
            self.ui.post(self._captured, key, name)

        capture(self.app.pad, done)

    def _captured(self, key, name):
        self.app.capturing = False
        for b in (self.b_ptt, self.b_send):
            b.configure(text="押して決める", state="normal")
        if not name:
            return
        other = "send_key" if key == "ptt" else "ptt"
        if name == self.sp[other]:
            self.app.warn(f"{label(name)} はもう片方で使っている")
            return
        self._save("speak", key, name)
        if key == "send_key" and self.sp["send_mode"] == "auto" and key_vk(name) is None:
            self.v_mode.set("hold")
            self._save("speak", "send_mode", "hold")
            self.app.warn(f"{label(name)} はツールから押せないので、流し方を「手で」にした")
        self.app.bind_buttons()

    def _say(self):
        text = self.v_typed.get()
        if text.strip():
            self.v_typed.set("")
            self.app.say_typed(text)

    # ---- 見せる
    def show(self):
        self.win.deiconify()
        self.win.lift()
        self.win.focus_force()

    def _steps(self):
        ptt, send, mode = label(self.sp["ptt"]), label(self.sp["send_key"]), self.sp["send_mode"]
        if mode == "auto":
            two = f"② 離すと英語の声になり、ツールが {send} を押して近くのプレイヤーとパーティへ流す"
        elif mode == "hold":
            two = f"② 離すと英語の声ができる。{send} を押している間に近くのプレイヤーとパーティへ流れる"
        else:
            two = "② 離すと英語の声になり、すぐパーティへ流れる（近くのプレイヤーには届かない）"
        if not self.sp["send_key"]:
            two = "② 離すと英語の声になり、そのまま VC へ流れる（ゲームの VC がオープンマイクなら届く）"
        return (f"① {ptt} を押しながら日本語で話す", two, "③ 相手の英語は、ゲームの画面の左下に日本語の字幕で出る")

    def _poll(self):
        a = self.app
        text = a.last_status
        kind = _kind(a, text)
        if kind == "wait" and not a.pending_text:
            kind = "idle"
        color, head = STATES[kind]
        if kind == "idle":
            sub = f"英語にしたいときは {label(self.sp['ptt'])} を押しながら日本語で話す"
        elif kind == "prep":
            sub = text[len("初回の準備: "):]
        elif kind == "warn":
            sub = text[3:]
        elif kind == "wait":
            sub = f"{a.pending_text}（{label(self.sp['send_key'])} を押すと流れる）"
        else:
            sub = text
        for wdg in (self.card, self.l_head, self.l_sub):
            wdg.configure(bg=color)
        self.v_head.set(head)
        self.v_sub.set(nb(sub))

        li, r = a.listener, a.router
        self.lamps["vc"]["value"] = min(1.0, getattr(li, "level", 0.0) * 2) if li else 0
        self.lamps["mic"]["value"] = min(1.0, r.mic_level * 2) if r else 0
        v, lb = self.lamps["pad"]
        dev = a.pad.device
        need = self.sp["ptt"].startswith("pad:") or self.sp["send_key"].startswith("pad:")
        if dev:
            v.set(f"● {dev}")
            lb.configure(fg="#1f8a4c")
        else:
            v.set("● つながっていない" if need else "（使っていない）")
            lb.configure(fg="#d43c3c" if need else "#8a939d")

        for var, s in zip(self.v_steps, self._steps()):
            var.set(nb(s))
        self.v_ptt.set(label(self.sp["ptt"]))
        self.v_send.set(label(self.sp["send_key"]))
        self._update_talk()
        self._guide_progress()
        self.win.after(200, self._poll)

    def _update_talk(self):
        items = list(getattr(self.app, "talk", []))
        n = getattr(self.app, "talk_count", len(items))
        if n == self.shown_talk:
            return
        new = items[-(n - self.shown_talk):] if n - self.shown_talk <= len(items) else items
        t = self.talk
        t.configure(state="normal")
        if self.shown_talk == 0:
            t.delete("1.0", "end")
        for row in new:
            when, who, src, dst = row[:4]
            meta = row[4] if len(row) > 4 else {}
            k = "them" if who == "相手" else "me"
            if t.index("end-1c") != "1.0":
                t.insert("end", "\n\n")
            tag = f"row{self.row_no}"
            self.row_no += 1
            t.insert("end", f"{when}  {who}  ", ("time", tag))
            t.insert("end", src + "\n", (f"{k}_src", tag))
            t.insert("end", dst, (f"{k}_dst", tag))
            if k == "me" and meta.get("clip"):
                t.insert("end", "  ✎直す", ("fix", tag))
                t.tag_bind(tag, "<Double-Button-1>", lambda e, m=dict(meta, src=src): self._fix_dialog(m))
                t.tag_bind(tag, "<Enter>", lambda e: t.configure(cursor="hand2"))
                t.tag_bind(tag, "<Leave>", lambda e: t.configure(cursor="arrow"))
        t.configure(state="disabled")
        t.see("end")
        self.shown_talk = n

    def _fix_dialog(self, meta):
        """自分の発言を正しい日本語に直す。直した例は次の聞き取りと訳に生かす。"""
        from . import myvoice
        d = tk.Toplevel(self.win)
        d.title("聞き取りを直す")
        d.transient(self.win)
        d.option_add("*Font", FONT)
        f = ttk.Frame(d, padding=14)
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="聞き取った文", style="Note.TLabel").pack(anchor="w")
        ttk.Label(f, text=meta.get("asr") or "", wraplength=520).pack(anchor="w", pady=(0, 8))
        ttk.Label(f, text="正しい日本語（話したとおりに）", style="Big.TLabel").pack(anchor="w")
        v = tk.StringVar(value=meta.get("src") or meta.get("asr") or "")
        e = ttk.Entry(f, textvariable=v, width=60, font=(F, 11))
        e.pack(fill="x", pady=(2, 10))
        e.focus_set()
        e.select_range(0, "end")
        b = ttk.Frame(f)
        b.pack(fill="x")

        def save(resend):
            text = v.get().strip()
            if not text:
                return
            myvoice.correct(meta["clip"], text)
            d.destroy()
            self.app.status(f"覚えた: {meta.get('asr')} → {text}", idle=True)
            if resend:
                self.app.say_typed(text)
            self._learn_refresh()

        ttk.Button(b, text="覚える", command=lambda: save(False)).pack(side="left")
        ttk.Button(b, text="覚えて英語で流し直す", command=lambda: save(True)).pack(side="left", padx=6)
        ttk.Button(b, text="やめる", command=d.destroy).pack(side="right")
        e.bind("<Return>", lambda ev: save(False))

    # ---- タブ 5: 声の学習
    def _tab_learn(self, nb):
        from . import myvoice
        f = ttk.Frame(nb, padding=12)
        nb.add(f, text="声の学習")
        how = ttk.LabelFrame(f, text="しくみ", padding=10)
        how.pack(fill="x")
        for line in ("① 自分の録音と、聞き取った文を PC の中にためる（logs/voice/）",
                     "② 「ふだん」の会話で自分の発言をダブルクリック → 正しい日本語に直す",
                     "③ 直した例は、次から聞き取りのヒントと英訳の聞き違い直しに使う（同じ間違いが減る）",
                     "④ 直した録音が 30 分ほどたまったら、聞き取りのモデルを自分の声で追加学習できる"):
            ttk.Label(how, text=line, wraplength=560).pack(anchor="w", pady=1)
        st = ttk.LabelFrame(f, text="たまった分", padding=10)
        st.pack(fill="x", pady=(10, 0))
        self.v_learn = tk.StringVar()
        ttk.Label(st, textvariable=self.v_learn, style="Big.TLabel").pack(anchor="w")
        self.v_save_voice = tk.BooleanVar(value=bool(self.sp.get("save_voice", True)))
        ttk.Checkbutton(st, text="自分の録音を残す", variable=self.v_save_voice,
                        command=lambda: self._save("speak", "save_voice", self.v_save_voice.get())).pack(
            anchor="w", pady=(6, 0))
        ttk.Button(st, text="録音のフォルダ", command=lambda: (myvoice.CLIPS.mkdir(parents=True, exist_ok=True),
                                                         os.startfile(myvoice.DIR))).pack(anchor="w", pady=(6, 0))
        vb = ttk.LabelFrame(f, text="よく使う言葉（1 行に 1 つ。名前・あだ名・決まり文句など）", padding=10)
        vb.pack(fill="both", expand=True, pady=(10, 0))
        self.t_vocab = tk.Text(vb, height=5, font=(F, 10), wrap="word")
        self.t_vocab.pack(fill="both", expand=True)
        self.t_vocab.insert("1.0", "\n".join(myvoice.vocab()))
        ttk.Button(vb, text="保存", command=self._save_vocab).pack(anchor="e", pady=(6, 0))
        self._learn_refresh()

    def _save_vocab(self):
        from . import myvoice
        myvoice.save_vocab(self.t_vocab.get("1.0", "end").splitlines())
        self._learn_refresh()
        self.app.status("よく使う言葉を保存した", idle=True)

    def _learn_refresh(self):
        from . import myvoice
        try:
            s = myvoice.stats()
        except Exception:
            return
        self.v_learn.set(f"録音 {s['clips']} 件（{s['minutes']:.1f} 分）・直した文 {s['corrected']} 件・"
                         f"よく使う言葉 {len(myvoice.vocab())} 語")
