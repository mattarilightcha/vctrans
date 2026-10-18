"""VC 翻訳: ゲームの VC の英語を日本語の字幕に、押している間にしゃべった日本語を英語の声にして VC へ。

  pythonw vctrans.py            字幕と操作の窓で動かす（「VC翻訳.pyw」・exe と同じ）
  python vctrans.py --devices   音の機器の名前を出す（config.toml に書く名前を探す）
  vctrans.exe --check [--cpu] [--model tiny]
                                窓も音の機器も使わずに、準備（取得）→ 読み上げ → 文字起こしを通して
                                logs/check-*.txt に結果を書く（exe が動くかの確かめ）
"""
import argparse
import ctypes
import datetime
import itertools
import logging
import queue
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

if not getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(__file__).resolve().parent))
else:  # exe: LGPL の部品（pynput）は差し替えられるよう、exe の横の lgpl/ にソースのまま置いてある
    sys.path.insert(0, str(Path(sys.executable).resolve().parent / "lgpl"))
from vc.config import ROOT  # noqa: E402  exe ならその exe のフォルダ

LOGS = ROOT / "logs"
LOGS.mkdir(exist_ok=True)
log = logging.getLogger("vctrans")


def setup_logging(console):
    day = datetime.date.today().strftime("%Y%m%d")
    path = LOGS / f"vctrans-{day}.log"
    handlers = [logging.FileHandler(path, encoding="utf-8")]
    if console and sys.stdout:
        sys.stdout.reconfigure(encoding="utf-8")
        handlers.append(logging.StreamHandler(sys.stdout))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
    if sys.stderr is None:  # pythonw では出す先が無いので記録へ
        sys.stderr = sys.stdout = open(path, "a", encoding="utf-8", buffering=1)


class App:
    def __init__(self, cfg, ui):
        from vc.pad import PadReader
        self.cfg = cfg
        self.ui = ui
        self.history = deque(maxlen=int(cfg["translate"]["context_lines"]))
        self.jobs = queue.PriorityQueue()
        self.seq = itertools.count()
        self.ids = itertools.count(1)
        self.them_pool = ThreadPoolExecutor(max_workers=3, thread_name_prefix="tr")
        self.me_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="me")  # 自分の声は順番どおりに
        self.router = None
        self.listener = None
        self.segmenter = None
        self.asr = None
        self.translator = None
        self.ready = False
        self.hearing = False
        self.capturing = False     # 操作の窓でボタンを決めている間は録らない
        self.rec_seq = 0           # 録るボタンを押した回数（短く押しただけのを取り消すため）
        self.rec_live = False      # 0.5 秒押しつづけて録るのが決まったか
        self.injecting = False     # ツールが送るキーを押している間（自分で押した分と分ける）
        self.send_lock = threading.Lock()
        self.buttons = []
        self.pending_t = 0.0
        self.pending_text = ""     # 送るキーを待っている英語
        self.last = None           # 最後の英語の声 (音, 周波数, 文)
        self.last_status = "起動中…"
        self.talk = deque(maxlen=60)  # 操作の窓に出す会話 (時刻, 誰, 原文, 訳)
        self.talk_count = 0
        self.them_recent = deque(maxlen=2)  # 直前の相手の英語（聞き取りのヒント）
        self.stop_ev = threading.Event()
        self.prep = dict(label="", done=0, total=0, device="", gpu="", model="", error="")  # 初回の準備の進み具合
        self.pad = PadReader()
        self.pad.start()
        day = datetime.date.today().strftime("%Y%m%d")
        self.talk_path = LOGS / f"talk-{day}.tsv"

    # ---- 画面
    def status(self, text, rec=False, idle=False):
        self.last_status = text
        self.ui.post(self.ui.set_status, text, rec, idle)

    def warn(self, text, secs=8):
        log.warning(text)
        self.last_status = "注意: " + text
        self.ui.post(self.ui.warn, text, secs)

    def idle_status(self):
        from vc.pad import label
        parts = ["聞き取り中"]
        if self.router:
            parts.append(f"英訳: {label(self.cfg['speak']['ptt'])} を押しながら話す")
        if self.translator and not self.translator.client:
            parts.append("Claude の API キーなし（訳さない）")
        self.status(" ／ ".join(parts), idle=True)

    # ---- 起動
    def start(self):
        threading.Thread(target=self._boot, daemon=True, name="boot").start()

    def _progress(self, label, done, total):
        self.prep.update(label=label, done=done, total=total)
        pct = 100 * done / total if total else 0
        self.status(f"初回の準備: {label} を取得中 {pct:.0f}%（{done / 1e6:,.0f} / {total / 1e6:,.0f} MB）")

    def _load_asr(self):
        """GPU があれば GPU の部品と大きいモデル、無ければ CPU の小さいモデル。GPU で読めなければ CPU に落とす。"""
        from vc import prepare
        cfg = self.cfg["asr"]
        device, model, compute, gpu = prepare.plan(cfg)
        self.prep.update(device=device, gpu=gpu, model=model)
        if device == "cuda":
            try:
                prepare.ensure_cuda(True, self._progress)
            except Exception as e:
                log.exception("GPU の部品の取得に失敗")
                self.warn(f"GPU の部品を取れなかったので CPU で動かす: {e}", 20)
                device, model, compute = "cpu", cfg["cpu_model"], cfg["cpu_compute_type"]
        if device == "cpu":
            prepare.ensure_cuda(False, self._progress)  # 読み込みに要る入口の DLL だけ
        self.status(f"読み込み中…（文字起こしのモデル {model}）")
        path = prepare.ensure_model(model, self._progress)
        from vc.asr import ASR
        try:
            asr = ASR(path, device, compute, cfg.get("beam_size", 5))
        except Exception as e:
            if device != "cuda":
                raise
            log.exception("GPU で読めなかった")
            self.warn(f"GPU で動かせなかったので CPU で動かす: {e}", 20)
            device, model = "cpu", cfg["cpu_model"]
            prepare.ensure_cuda(False, self._progress)
            asr = ASR(prepare.ensure_model(model, self._progress), "cpu", cfg["cpu_compute_type"],
                      cfg.get("beam_size", 5))
        self.prep.update(device=device, model=model, label="", done=0, total=0)
        return asr

    def _boot(self):
        from vc import config
        from vc.audio import com_init
        from vc.translate import Translator

        cfg = self.cfg
        com_init()
        try:
            self.status("読み込み中…")
            glossary = config.load_glossary(cfg)
            key = config.api_key()
            self.translator = Translator(key, cfg["translate"], cfg["game"], glossary,
                                         core_terms=cfg["listen"]["hotwords"])
            if not key:
                self.warn("Claude の API キーが入っていない: 操作の窓の「API キー」のタブで入れる", 20)
            t0 = time.time()
            self.asr = self._load_asr()
            log.info("ASR %s（%s）を %.1f 秒で読んだ。用語 %d 語", self.prep["model"], self.prep["device"],
                     time.time() - t0, len(glossary))
        except Exception as e:
            log.exception("起動に失敗")
            self.prep["error"] = str(e)
            self.warn(f"起動に失敗: {e}", 600)
            return

        from vc.vad import Segmenter
        if cfg["speak"]["enabled"]:
            self.open_router()
            self.bind_buttons()
            threading.Thread(target=self._warm_tts, daemon=True, name="tts-warm").start()
        ls = cfg["listen"]
        self.segmenter = Segmenter(self._on_segment, threshold=ls["vad_threshold"], silence_ms=ls["silence_ms"],
                                   max_segment_s=ls["max_segment_s"], min_speech_ms=ls["min_speech_ms"],
                                   on_state=self._on_hearing)
        self.open_listener()
        threading.Thread(target=self._asr_worker, daemon=True, name="asr").start()
        threading.Thread(target=self._watchdog, daemon=True, name="watchdog").start()
        self.ready = True
        self.idle_status()

    def apply_profile(self, pid):
        """ゲームのプロファイルを切り替える（訳の文脈・聞き取りのヒント・字幕を合わせる画面・送るキー）。"""
        from vc import config
        config.save_setting("", "profile", pid)
        config.save_setting("speak", "send_key", None)  # 送るキーはプロファイルの物に戻す
        new = config.load()
        cfg = self.cfg
        cfg["profile"], cfg["game"], cfg["profile_info"] = pid, new["game"], new["profile_info"]
        cfg["listen"]["hotwords"] = new["listen"]["hotwords"]
        cfg["overlay"]["follow_exe"] = new["overlay"]["follow_exe"]
        cfg["speak"]["send_key"] = new["speak"]["send_key"]
        if self.translator:
            self.translator.set_game(cfg["game"], config.load_glossary(cfg), cfg["listen"]["hotwords"])
        if self.ready:
            self.bind_buttons()
        log.info("ゲームを %s にした", cfg["game"] or "（決めない）")

    def set_api_key(self, key):
        """操作の窓で入れたキーを .env に残し、すぐ使う。"""
        from vc import config
        config.save_api_key(key)
        if self.translator:
            self.translator.set_key(key)
        log.info("API キーを入れ替えた（末尾 %s）", key[-4:])

    def check_api(self):
        """短い英語を 1 回訳して (できたか, 文) を返す。"""
        from vc.translate import TranslateError
        if not self.translator:
            return False, "まだ起動中"
        t0 = time.time()
        try:
            _, ja = self.translator("Can you hear me? Let's extract together.", "ja", [])
        except TranslateError as e:
            return False, str(e)
        except Exception as e:
            return False, f"{type(e).__name__}: {e}"
        return True, f"{time.time() - t0:.1f} 秒で訳せた: {ja}"

    def _warm_tts(self):
        """その起動で最初の読み上げはつなぎ始め・声の読み込みで遅れるので、起動のうちに 1 回作っておく。"""
        from vc.tts import synth
        try:
            synth("OK.", self.cfg["speak"])
        except Exception as e:
            log.warning("読み上げの準備に失敗: %s", e)
            self.warn(f"英語の声を作れない: {e}", 20)

    def open_router(self):
        """マイク→CABLE の中継を開く（操作の窓で機器を変えたときも）。"""
        from vc.audio import VoiceRouter, com_init
        com_init()
        old, self.router = self.router, None
        if old:
            old.close()
        try:
            self.router = VoiceRouter(self.cfg["speak"])
            log.info("マイク %s → %s", self.router.mic_label, self.router.out_label)
        except Exception as e:
            log.exception("英訳の声の準備に失敗")
            self.warn(f"英訳の声は使えない: {e}", 30)

    def open_listener(self):
        """ヘッドセットの音を拾い始める（操作の窓で機器を変えたときも）。"""
        from vc.audio import LoopbackListener, VCListener
        if self.listener:
            self.listener.stop()
        ls = self.cfg["listen"]
        if ls.get("vc_source") and self.router:
            from vc.audio import cable_id, find_device
            try:
                same = cable_id(find_device(ls["vc_source"], "input")["name"]) == cable_id(self.router.out_label)
            except Exception:
                same = False
            if same:
                self.warn("VC だけを聞く機器が、英語の声を出すケーブルと同じ。別のケーブル（CABLE-A など）を選ぶ", 30)
                self.listener = None
                return
        if ls.get("vc_source"):  # ゲームの VC の出力先にした仮想ケーブルだけを聞き、ヘッドセットへ中継
            self.listener = VCListener(ls["vc_source"], self.segmenter.feed, self.warn, ls)
        else:                    # ヘッドセットに流れる音をまるごと
            self.listener = LoopbackListener(ls["device"], self.segmenter.feed, self.warn)
        self.listener.start()
        log.info("相手の声: %s", f"VC だけ {ls['vc_source']}" if ls.get("vc_source") else "ヘッドセットの音をまるごと")

    def bind_buttons(self):
        """録るボタンと送るキーを見張る（操作の窓で変えたときも呼ぶ）。"""
        from vc.ptt import PushToTalk
        for b in self.buttons:
            b.stop()
        self.buttons = []
        sp = self.cfg["speak"]
        try:
            b = PushToTalk(sp["ptt"], self._rec_down, self._rec_up, pad=self.pad)
            b.start()
            self.buttons.append(b)
        except Exception as e:
            self.warn(f"録るボタンが使えない: {e}", 20)
        if sp["send_mode"] in ("auto", "hold") and sp["send_key"] and sp["send_key"] != sp["ptt"]:
            try:
                b = PushToTalk(sp["send_key"], self._send_down, self._send_up, pad=self.pad)
                b.start()
                self.buttons.append(b)
            except Exception as e:
                self.warn(f"送るキーが使えない: {e}", 20)

    # ---- 相手の声
    def _on_hearing(self, speaking):
        self.hearing = speaking

    def _on_segment(self, audio):
        if self.router and time.time() < self.router.monitor_until:
            return  # 自分の英語の声を聞き返している間は拾わない
        self.jobs.put((1, next(self.seq), "them", audio))

    # ---- 自分の声（フックの中なので軽く）
    def _rec_down(self):
        """押した瞬間から録り始め（話し始めを欠かさない）、ptt_hold_ms 押しつづけたら録るのを決める。"""
        if not self.router or self.capturing:
            return
        self.rec_seq += 1
        self.rec_live = False
        self.router.rec_start()
        hold = float(self.cfg["speak"].get("ptt_hold_ms", 500)) / 1000
        seq = self.rec_seq
        threading.Timer(hold, self._rec_commit, args=(seq,)).start()

    def _rec_commit(self, seq):
        r = self.router
        if seq != self.rec_seq or not r or not r.recording:
            return  # その前に離した
        self.rec_live = True
        r.rec_mute = True
        self.status("● 録音中（離すと英訳）", rec=True)

    def _rec_up(self):
        if not self.router or not self.router.recording:
            return
        live = self.rec_live
        self.rec_seq += 1   # まだ決まっていなければ取り消す
        self.rec_live = False
        if not live:
            self.router.rec_stop()  # 短く押しただけ。何もしない
            return
        threading.Thread(target=self._rec_finish, daemon=True).start()

    def _rec_finish(self):
        audio = self.router.rec_stop()
        if len(audio) < 16000 * 0.3:
            self.idle_status()
            return
        self.status("英訳中…")
        self.jobs.put((0, next(self.seq), "me", audio))

    # ---- 送るキー（ゲームの近接 VC の押して話すキー）を自分で押したとき
    def _send_down(self):
        r = self.router
        if self.injecting or not r or not r.has_pending():
            return
        if time.time() - self.pending_t > float(self.cfg["speak"]["pending_s"]):
            r.drop_pending()  # 古い英語は流さない
            self.pending_text = ""
            return
        if r.send_start():
            self.pending_text = ""
            self.status("▶ 英語の声を VC へ（離すと止まる）")
            threading.Thread(target=self._wait_played, daemon=True).start()

    def _send_up(self):
        r = self.router
        if self.injecting or not r:
            return
        if r.send_stop():
            self.pending_text = r.pending_text
            self.status("途中で離した。もう一度押すと頭から流れる")

    def _wait_played(self):
        while self.router and self.router.busy():
            time.sleep(0.05)
        if not (self.router and self.router.has_pending()):
            self.idle_status()

    def deliver(self, audio, sr, text):
        """英語の声を設定の流し方で VC へ。auto はこのスレッドで流し終わるまで待つ。"""
        from vc import gamewin
        from vc.pad import label
        from vc.ptt import key_is_down, key_vk, press
        sp = self.cfg["speak"]
        r = self.router
        if not r:
            self.warn("英語の声を流す先が開いていない")
            return
        self.last = (audio, sr, text)
        mode = sp["send_mode"]
        if mode == "now":
            r.drop_pending()
            self.pending_text = ""
            secs = r.play(audio, sr)
            self.status("▶ 英語の声を VC へ")
            time.sleep(secs)
            self.idle_status()
            return
        r.queue(audio, sr, text)
        self.pending_t = time.time()
        self.pending_text = text
        vk = key_vk(sp["send_key"])
        if not sp["send_key"]:  # 送るキーを決めていないゲーム: そのまま流す（オープンマイク向け）
            r.send_start()
            self.pending_text = ""
            self.status("▶ 英語の声を VC へ")
            self._wait_played()
            return
        if mode == "hold" or vk is None:
            if mode == "auto":
                self.warn(f"{label(sp['send_key'])} はツールから押せない。自分で押している間に流れる")
            self.status(f"英訳ができた: {label(sp['send_key'])} を押している間に流れる")
            return
        exe = (self.cfg["overlay"].get("follow_exe") or "").lower()
        in_game = not exe or gamewin.foreground_exe() == exe
        with self.send_lock:
            held = key_is_down(vk)  # 自分で押している最中ならそのまま流す
            pressed = False
            if in_game and not held:
                self.injecting = True
                pressed = press(vk, True)
                time.sleep(float(sp["lead_ms"]) / 1000)  # ゲームがマイクを開くのを待つ
            if not in_game:
                self.warn(f"ゲームが前に無いので {label(sp['send_key'])} を押さずに流した（パーティだけに届く）")
            secs = r.send_start()
            self.pending_text = ""
            self.status("▶ 英語の声を近接 VC へ")
            t_end = time.time() + secs + 2
            while r.busy() and time.time() < t_end:
                time.sleep(0.05)
            time.sleep(0.3)  # 語尾が切れないよう少し長く押す
            if pressed:
                press(vk, False)
                time.sleep(0.05)
            self.injecting = False
        self.idle_status()

    def replay(self):
        """最後の英語の声をもう一度流す。"""
        if self.last:
            self.me_pool.submit(self.deliver, *self.last)

    def say_typed(self, text):
        """操作の窓で打った日本語を英訳して流す。"""
        text = text.strip()
        if not text or not self.translator:
            return
        eid = next(self.ids)
        self.ui.post(self.ui.add, eid, "me", text)
        ctx = list(self.history)
        self.history.append(("自分", text))
        self.me_pool.submit(self._translate_me, eid, text, ctx)

    def test_voice(self, delay=3):
        """英語の声のテスト。delay 秒のうちにゲームの画面へ戻ってもらう。"""
        def go():
            from vc.tts import synth
            for i in range(delay, 0, -1):
                self.status(f"テスト: {i} 秒後に英語の声を流す（ゲームの画面へ戻る）")
                time.sleep(1)
            sp = self.cfg["speak"]
            try:
                audio, sr = synth("Hey, can you hear me? This is a voice translation test.", sp)
            except Exception as e:
                self.warn(f"英語の声を作れない: {e}", 20)
                return
            self.deliver(audio, sr, "(test)")
        self.me_pool.submit(go)

    # ---- 文字起こし
    def _asr_worker(self):
        ls = self.cfg["listen"]
        while not self.stop_ev.is_set():
            prio, _, kind, audio = self.jobs.get()
            try:
                if kind == "them":
                    t0 = time.time()
                    text, lang, prob = self.asr.listen(audio, ls["languages"], ls["min_lang_prob"], ls["hotwords"],
                                                       prompt=" ".join(self.them_recent))
                    log.info("聞き取り %.1f 秒の音 → %s %.2f %.2f 秒: %s", len(audio) / 16000, lang, prob,
                             time.time() - t0, text)
                    if not text:
                        continue
                    eid = next(self.ids)
                    self.ui.post(self.ui.add, eid, "them", text)
                    ctx = list(self.history)
                    self.history.append(("相手", text))
                    self.them_pool.submit(self._translate_them, eid, text, ctx)
                else:
                    from vc import myvoice
                    from vc.asr import strip_fillers
                    t0 = time.time()
                    cid = myvoice.save_clip(audio) if self.cfg["speak"].get("save_voice", True) else None
                    base = (self.cfg.get("profile_info") or {}).get("prompt_ja", "")
                    raw = self.asr.japanese(audio, prompt=myvoice.prompt_text(base))
                    text = strip_fillers(raw)
                    log.info("自分 %.1f 秒の音 → %.2f 秒: %s", len(audio) / 16000, time.time() - t0, raw)
                    if not text:
                        myvoice.record(cid, raw)
                        self.warn("聞き取れなかった")
                        self.idle_status()
                        continue
                    eid = next(self.ids)
                    self.ui.post(self.ui.add, eid, "me", text)
                    ctx = list(self.history)
                    self.history.append(("自分", text))
                    self.me_pool.submit(self._translate_me, eid, text, ctx, cid)
            except Exception as e:
                log.exception("文字起こしに失敗")
                self.warn(f"文字起こしに失敗: {e}")

    # ---- 訳す
    def _translate_them(self, eid, text, ctx):
        from vc.translate import TranslateError
        t0 = time.time()
        try:
            fixed, ja = self.translator(text, "ja", ctx)
        except TranslateError as e:
            self.ui.post(self.ui.update, eid, None, str(e))
            return
        except Exception as e:
            log.exception("訳に失敗")
            self.ui.post(self.ui.update, eid, None, f"訳に失敗: {e}")
            return
        if fixed != text:
            log.info("英語を直した: %s → %s", text, fixed)
        log.info("和訳 %.2f 秒: %s", time.time() - t0, ja)
        self.them_recent.append(fixed)
        self.ui.post(self.ui.update, eid, ja, None, fixed)
        self._talk_log("相手", fixed, ja)

    def _translate_me(self, eid, text, ctx, cid=None):
        from vc import myvoice
        from vc.translate import TranslateError
        from vc.tts import synth
        t0 = time.time()
        try:
            fixed, en = self.translator(text, "en", ctx, examples=myvoice.examples())
        except TranslateError as e:
            self.ui.post(self.ui.update, eid, None, str(e))
            self.idle_status()
            return
        except Exception as e:
            log.exception("英訳に失敗")
            self.ui.post(self.ui.update, eid, None, f"英訳に失敗: {e}")
            self.idle_status()
            return
        t1 = time.time()
        if fixed != text:
            log.info("日本語を直した: %s → %s", text, fixed)
        self.ui.post(self.ui.update, eid, en, None, fixed)
        myvoice.record(cid, text, fixed)
        self._talk_log("自分", fixed, en, dict(clip=cid, asr=text))
        try:
            audio, sr = synth(en, self.cfg["speak"])
            log.info("英訳 %.2f 秒＋読み上げ %.2f 秒（%.1f 秒の声）: %s", t1 - t0, time.time() - t1,
                     len(audio) / sr, en)
            self.deliver(audio, sr, en)
        except Exception as e:
            log.exception("読み上げに失敗")
            self.warn(f"読み上げに失敗: {e}")
            self.idle_status()

    def _talk_log(self, who, src, dst, meta=None):
        self.talk.append((f"{datetime.datetime.now():%H:%M}", who, src, dst, meta or {}))
        self.talk_count += 1
        try:
            with open(self.talk_path, "a", encoding="utf-8") as f:
                f.write(f"{datetime.datetime.now():%H:%M:%S}\t{who}\t{src}\t{dst}\n")
        except Exception:
            pass

    # ---- 機器が抜けたら開き直す
    def _watchdog(self):
        from vc.audio import com_init
        com_init()
        while not self.stop_ev.wait(3):
            if self.router and not self.router.healthy():
                self.warn("マイクか仮想ケーブルが止まった。開き直す")
                self.open_router()

    def shutdown(self):
        self.stop_ev.set()
        self.pad.stop()
        if self.injecting:  # 押したまま終わらない
            from vc.ptt import key_vk, press
            vk = key_vk(self.cfg["speak"]["send_key"])
            if vk:
                press(vk, False)
        if self.router:
            self.router.close()


def list_devices():
    from vc.audio import wasapi_devices, pa
    api, devs = wasapi_devices()
    p = pa()
    print("既定の出力:", p.get_device_info_by_index(api["defaultOutputDevice"])["name"])
    print("既定の入力:", p.get_device_info_by_index(api["defaultInputDevice"])["name"])
    for d in devs:
        kind = "聞き取り(loopback)" if d.get("isLoopbackDevice") else ("入力" if d["maxInputChannels"] else "出力")
        print(f"  {kind:18} {int(d['defaultSampleRate'])} Hz  {d['name']}")


def run_check(a):
    """窓も音の機器も使わない確かめ（exe が Python なしで動くか）。結果は logs/check-*.txt と終了コード。"""
    import platform
    import traceback
    from vc import config, prepare
    out = []

    def say(line):
        out.append(line)
        print(line, flush=True) if sys.stdout else None

    ok = True
    t_all = time.time()
    try:
        cfg = config.load()
        say(f"vctrans の確かめ  {datetime.datetime.now():%Y-%m-%d %H:%M:%S}  exe={getattr(sys, 'frozen', False)}  "
            f"Python {platform.python_version()}  場所 {ROOT}")
        say(f"ゲーム: {cfg['game'] or '（決めない）'}  プロファイル {len(config.list_profiles())} 個  "
            f"用語 {len(config.load_glossary(cfg))} 語")
        acfg = dict(cfg["asr"])
        if a.cpu:
            acfg["device"] = "cpu"
        if a.model:
            acfg["model"] = acfg["cpu_model"] = a.model
        device, model, compute, gpu = prepare.plan(acfg)
        say(f"GPU: {gpu or 'なし'} → {device} で {model}（{compute}）")
        last = [0.0]

        def prog(label, done, total):
            if time.time() - last[0] > 2 or done >= total:
                last[0] = time.time()
                say(f"  取得 {label} {100 * done / max(total, 1):.0f}%（{done / 1e6:,.1f} / {total / 1e6:,.1f} MB）")

        t0 = time.time()
        prepare.ensure_cuda(device == "cuda", prog)
        path = prepare.ensure_model(model, prog)
        say(f"準備 {time.time() - t0:.1f} 秒  モデル {path}")
        from vc.asr import ASR
        from vc.audio import resample
        from vc.tts import synth, windows_voices
        from vc.vad import Segmenter
        t0 = time.time()
        asr = ASR(path, device, compute, acfg.get("beam_size", 5))
        say(f"文字起こしのモデルを読んだ {time.time() - t0:.1f} 秒")
        say(f"Windows の英語の声: {windows_voices() or 'なし'}")
        text = "Hey, there is an enemy near the tower. Let's grab the loot and extract."
        audio, sr = synth(text, dict(cfg["speak"], engine="windows"))
        x = resample(audio, sr, 16000)
        say(f"読み上げ（Windows の声）{len(x) / 16000:.1f} 秒の音")
        segs = []
        seg = Segmenter(segs.append, threshold=0.5, silence_ms=500, max_segment_s=12, min_speech_ms=300)
        pad = np.zeros(16000, np.float32)
        for chunk in np.array_split(np.concatenate([pad, x, pad, pad]), 40):
            seg.feed(chunk)
        say(f"声の切り出し: {len(segs)} 個")
        t0 = time.time()
        heard, lang, prob = asr.listen(segs[0] if segs else x, ["en"], 0.5, cfg["listen"]["hotwords"])
        say(f"文字起こし {time.time() - t0:.1f} 秒: {lang} {prob:.2f} → {heard!r}")
        if not heard or "tower" not in heard.lower():
            ok = False
            say("× 文字起こしの結果が合わない")
        from vc import pad as padmod
        from vc import ptt  # noqa: F401  pynput（差し替えられる形で置いた物）が読めるか
        import anthropic  # noqa: F401
        say(f"コントローラーの部品: hidapi={'あり' if padmod.hid else 'なし'}  XInput={'あり' if padmod._xinput_dll() else 'なし'}")
        say("Claude の API キー: " + ("入っている（訳は試さない）" if config.api_key() else "なし"))
    except Exception:
        ok = False
        say(traceback.format_exc())
    say(f"{'○ 通った' if ok else '× 通らなかった'}（{time.time() - t_all:.1f} 秒）")
    p = LOGS / f"check-{datetime.datetime.now():%Y%m%d-%H%M%S}.txt"
    p.write_text("\n".join(out) + "\n", encoding="utf-8")
    return 0 if ok else 1


_mutex = None


def single_instance():
    """2 つ目を起こさない（マイクの中継が二重になって声が 2 回流れる）。起動済みなら False。"""
    global _mutex
    k32 = ctypes.windll.kernel32
    _mutex = k32.CreateMutexW(None, False, "Local\\vctrans-single-instance")
    return k32.GetLastError() != 183  # ERROR_ALREADY_EXISTS


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--devices", action="store_true")
    ap.add_argument("--console", action="store_true", help="記録を画面にも出す")
    ap.add_argument("--check", action="store_true", help="窓を出さずに動くかを確かめる")
    ap.add_argument("--cpu", action="store_true", help="--check で GPU を使わない")
    ap.add_argument("--model", default="", help="--check で使うモデル（tiny など）")
    a = ap.parse_args()
    if a.devices:
        sys.stdout.reconfigure(encoding="utf-8")
        list_devices()
        return
    if a.check:
        if sys.stdout:
            sys.stdout.reconfigure(encoding="utf-8")
        sys.exit(run_check(a))
    if not single_instance():
        ctypes.windll.user32.MessageBoxW(None, "VC翻訳はもう起動しています。Ctrl+Alt+P で操作の窓を出せます。",
                                         "VC翻訳", 0x40)
        return
    try:  # タスクバーで python の絵でなくこのツールの絵を出す
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("nettoge.vctrans")
    except Exception:
        pass
    setup_logging(a.console)
    from vc import config
    from vc.overlay import Overlay
    from vc.panel import Panel
    from vc.ptt import Hotkeys
    cfg = config.load()
    app = None
    ui = Overlay(cfg["overlay"], on_quit=lambda: app and app.shutdown())
    app = App(cfg, ui)
    panel = Panel(ui, app, cfg)
    Hotkeys({cfg["overlay"].get("panel_key", ""): lambda: ui.post(panel.show)}).start()
    app.start()
    ui.run()


if __name__ == "__main__":
    main()
