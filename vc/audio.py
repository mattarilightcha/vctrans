"""音の出入り。

- LoopbackListener: ヘッドセットに流れている音（ゲームの VC）を拾って 16 kHz のモノラルにして渡す
- VoiceRouter: マイク→仮想ケーブル（CABLE Input）の中継、英語の声の差し込み、話すボタンの間の録音
- play_once: テスト用にヘッドセットなどへ 1 回鳴らす
"""
import collections
import ctypes
import queue
import threading
import time
from math import gcd

import numpy as np
import pyaudiowpatch as pyaudio
from scipy.signal import firwin, lfilter, resample_poly

SR = 16000
_pa = None
_pa_lock = threading.Lock()
_init_lock = threading.Lock()


def pa():
    """PortAudio は 1 つだけ作る。2 つのスレッドが同時に作ると落ちる（起動の機器の準備と、操作の窓の点検など）。"""
    global _pa
    if _pa is None:
        with _init_lock:
            if _pa is None:
                _pa = pyaudio.PyAudio()
    return _pa


def com_init():
    """PortAudio の WASAPI は COM を初期化したスレッドでないと開けない（-9999 になる）。"""
    try:
        ctypes.windll.ole32.CoInitializeEx(None, 0)  # COINIT_MULTITHREADED
    except Exception:
        pass


def resample(x, sr_from, sr_to):
    """まとまった音を一度に変換する（録音・合成音声用）。"""
    x = np.asarray(x, dtype=np.float32)
    if sr_from == sr_to or len(x) == 0:
        return x
    g = gcd(int(sr_from), int(sr_to))
    return resample_poly(x, int(sr_to) // g, int(sr_from) // g).astype(np.float32)


class Downsampler:
    """細切れで届く音を 16 kHz へ（48k→16k などの整数比は状態つきの FIR で継ぎ目なし）。"""

    def __init__(self, sr_in, sr_out=SR):
        self.sr_in, self.sr_out = sr_in, sr_out
        self.m = sr_in // sr_out if sr_in % sr_out == 0 else None
        if self.m and self.m > 1:
            self.taps = firwin(24 * self.m + 1, 0.45 * sr_out, fs=sr_in)
            self.zi = np.zeros(len(self.taps) - 1)
            self.offset = 0

    def __call__(self, x):
        if self.m == 1:
            return x.astype(np.float32)
        if self.m:
            y, self.zi = lfilter(self.taps, 1.0, x, zi=self.zi)
            out = y[(-self.offset) % self.m :: self.m]
            self.offset = (self.offset + len(x)) % self.m
            return out.astype(np.float32)
        return resample(x, self.sr_in, self.sr_out)


def wasapi_devices():
    com_init()
    p = pa()
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    devs = [p.get_device_info_by_index(i) for i in range(p.get_device_count())]
    return api, [d for d in devs if d["hostApi"] == api["index"]]


def device_names():
    """GUI の選択肢 {"loopback": [...], "input": [...], "output": [...]}、既定の出力名、既定の入力名。"""
    api, devs = wasapi_devices()
    p = pa()
    out = {"loopback": [], "input": [], "output": []}
    for d in devs:
        if d.get("isLoopbackDevice"):
            out["loopback"].append(d["name"])
        elif d["maxInputChannels"] > 0:
            out["input"].append(d["name"])
        elif d["maxOutputChannels"] > 0:
            out["output"].append(d["name"])
    return (out, p.get_device_info_by_index(api["defaultOutputDevice"])["name"],
            p.get_device_info_by_index(api["defaultInputDevice"])["name"])


def find_device(name, kind):
    """kind: loopback / input / output。name が空なら既定の機器。名前は部分一致。"""
    p = pa()
    api, devs = wasapi_devices()
    if kind == "loopback":
        if not name:
            name = p.get_device_info_by_index(api["defaultOutputDevice"])["name"]
        for d in devs:
            if d.get("isLoopbackDevice") and name in d["name"]:
                return d
    elif kind == "input":
        if not name:
            return p.get_device_info_by_index(api["defaultInputDevice"])
        for d in devs:
            if d["maxInputChannels"] > 0 and not d.get("isLoopbackDevice") and name in d["name"]:
                return d
    else:
        if not name:
            return p.get_device_info_by_index(api["defaultOutputDevice"])
        for d in devs:
            if d["maxOutputChannels"] > 0 and not d.get("isLoopbackDevice") and name in d["name"]:
                return d
    raise RuntimeError(f"音の機器が見つからない: {name or '既定'}（{kind}）")


def cable_id(name):
    """"CABLE-A Output (VB-Audio Cable A)" → "vb-audio cable a"。同じケーブルの入口と出口を見分ける。"""
    a, b = name.find("("), name.rfind(")")
    return name[a + 1:b].strip().lower() if 0 <= a < b else name.strip().lower()


def default_output_name():
    p = pa()
    api = p.get_host_api_info_by_type(pyaudio.paWASAPI)
    return p.get_device_info_by_index(api["defaultOutputDevice"])["name"]


def play_once(samples, sr, device_name="", volume=1.0):
    """テスト用に 1 回鳴らして、終わるまで待つ（呼んだスレッドで）。"""
    com_init()
    dev = find_device(device_name, "output")
    osr = int(dev["defaultSampleRate"])
    ch = max(1, min(2, int(dev["maxOutputChannels"])))
    y = resample(np.asarray(samples, np.float32) * volume, sr, osr)
    y = np.concatenate([y, np.zeros(osr // 5, np.float32)])
    if ch > 1:
        y = np.repeat(y[:, None], ch, axis=1)
    with _pa_lock:
        s = pa().open(format=pyaudio.paFloat32, channels=ch, rate=osr, output=True,
                      output_device_index=dev["index"], frames_per_buffer=osr // 50)
    try:
        s.write(y.astype(np.float32).tobytes())
    finally:
        with _pa_lock:
            s.stop_stream()
            s.close()


class LoopbackListener(threading.Thread):
    """出力の音を拾う。何も鳴っていない間は届かないので、その間は無音を渡す。"""

    def __init__(self, device_name, on_audio, on_error):
        super().__init__(daemon=True, name="loopback")
        self.device_name = device_name
        self.on_audio = on_audio  # (16 kHz の float32) を受け取る
        self.on_error = on_error
        self.stop_ev = threading.Event()
        self.device_label = ""

    def run(self):
        com_init()
        while not self.stop_ev.is_set():
            try:
                self._run_once()
            except Exception as e:  # 機器が抜けた等。少し待って開き直す
                self.on_error(f"聞き取りの機器: {e}")
                self.stop_ev.wait(3)

    def stop(self):
        self.stop_ev.set()
        self.join(timeout=4)

    def _run_once(self):
        dev = find_device(self.device_name, "loopback")
        sr = int(dev["defaultSampleRate"])
        ch = int(dev["maxInputChannels"])
        self.device_label = dev["name"]
        q = queue.Queue()

        def cb(in_data, frame_count, time_info, status):
            q.put(in_data)
            return (None, pyaudio.paContinue)

        with _pa_lock:
            stream = pa().open(format=pyaudio.paFloat32, channels=ch, rate=sr, input=True,
                               input_device_index=dev["index"], frames_per_buffer=int(sr * 0.03),
                               stream_callback=cb)
        down = Downsampler(sr)
        default_name = default_output_name()
        last_check = time.monotonic()
        try:
            while not self.stop_ev.is_set():
                try:
                    data = q.get(timeout=0.1)
                    x = np.frombuffer(data, np.float32).reshape(-1, ch).mean(axis=1)
                    self.on_audio(down(x))
                except queue.Empty:
                    self.on_audio(np.zeros(int(SR * 0.1), np.float32))
                now = time.monotonic()
                if now - last_check > 3:
                    last_check = now
                    if not stream.is_active():
                        raise RuntimeError("音が止まった")
                    if not self.device_name and default_output_name() != default_name:
                        return  # 既定の出力が変わった。開き直す
        finally:
            with _pa_lock:
                stream.stop_stream()
                stream.close()


class VoiceRouter:
    """マイク→CABLE Input の中継に英語の声を混ぜる。

    cfg（[speak] の辞書）はそのまま持つので、中継・音量などは書き換えればすぐ効く。
    英語の声は queue() で預かり、send_start()（送るボタン）で流す。送るボタンが無い設定なら play() ですぐ流す。
    """

    def __init__(self, cfg):
        com_init()
        self.cfg = cfg
        mic = find_device(cfg["mic_device"], "input")
        out = find_device(cfg["vc_device"], "output")
        self.mic_label, self.out_label = mic["name"], out["name"]
        self.mic_sr = int(mic["defaultSampleRate"])
        self.mic_ch = max(1, min(2, int(mic["maxInputChannels"])))
        self.out_sr = int(out["defaultSampleRate"])
        self.out_ch = max(1, min(2, int(out["maxOutputChannels"])))
        self.block = self.out_sr // 100  # 10 ms

        self.lock = threading.Lock()
        self.ring = collections.deque()
        self.ring_len = 0
        self.ring_ready = False
        self.tts = collections.deque()
        self.recording = False
        self.rec_mute = False   # 録るのが決まったら日本語の声を VC へ流さない
        self.rec = []
        self.gain = 0.0
        self.level = 0.0      # VC へ出している音の大きさ（画面の表示用）
        self.mic_level = 0.0  # マイクの音の大きさ
        self.pending = None   # 送るボタンを待っている英語の声（out_sr）
        self.pending_src = None
        self.pending_text = ""
        self.playing = None   # いま流している英語の声（途中で離されたら pending に戻す）
        self.monitor_until = 0.0
        self.monitor = None

        with _pa_lock:
            self.mic_stream = pa().open(format=pyaudio.paFloat32, channels=self.mic_ch, rate=self.mic_sr,
                                        input=True, input_device_index=mic["index"],
                                        frames_per_buffer=self.mic_sr // 100, stream_callback=self._mic_cb)
            self.out_stream = pa().open(format=pyaudio.paFloat32, channels=self.out_ch, rate=self.out_sr,
                                        output=True, output_device_index=out["index"],
                                        frames_per_buffer=self.block, stream_callback=self._out_cb)

    # ---- マイク
    def _mic_cb(self, in_data, frame_count, time_info, status):
        x = np.frombuffer(in_data, np.float32).reshape(-1, self.mic_ch).mean(axis=1)
        self.mic_level = float(np.abs(x).max()) if len(x) else 0.0
        with self.lock:
            if self.recording:
                self.rec.append(x.copy())
            if self.cfg["passthrough"]:
                y = x if self.mic_sr == self.out_sr else resample(x, self.mic_sr, self.out_sr)
                self.ring.append(y)
                self.ring_len += len(y)
                if self.ring_len > self.out_sr * 0.12:  # 機器の時計のずれでたまった分を捨てる
                    while self.ring_len > self.out_sr * 0.04:
                        self.ring_len -= len(self.ring.popleft())
        return (None, pyaudio.paContinue)

    def _take(self, dq, n):
        out = np.zeros(n, np.float32)
        i = 0
        while i < n and dq:
            a = dq[0]
            k = min(n - i, len(a))
            out[i:i + k] = a[:k]
            if k == len(a):
                dq.popleft()
            else:
                dq[0] = a[k:]
            i += k
        return out, i

    # ---- CABLE へ出す
    def _out_cb(self, in_data, frame_count, time_info, status):
        n = frame_count
        with self.lock:
            if not self.ring_ready and self.ring_len >= self.out_sr * 0.03:
                self.ring_ready = True
            if self.ring_ready:
                mic, got = self._take(self.ring, n)
                self.ring_len -= got
                if got < n:
                    self.ring_ready = False  # 足りなくなったら少しためてから再開
            else:
                mic = np.zeros(n, np.float32)
            tts, tgot = self._take(self.tts, n)
            busy = tgot > 0 or bool(self.tts)
            if not busy:
                self.playing = None
            mute = ((not self.cfg["passthrough"]) or (busy and self.cfg["mute_mic_while_tts"])
                    or (self.recording and self.rec_mute and self.cfg.get("mute_while_recording")))
        target = 0.0 if mute else float(self.cfg["mic_gain"])
        ramp = np.linspace(self.gain, target, n, dtype=np.float32)  # 切り替えの「プツッ」を防ぐ
        self.gain = target
        y = mic * ramp + tts * float(self.cfg["tts_gain"])
        np.clip(y, -1.0, 1.0, out=y)
        self.level = float(np.abs(y).max()) if n else 0.0
        if self.out_ch > 1:
            y = np.repeat(y[:, None], self.out_ch, axis=1)
        return (y.astype(np.float32).tobytes(), pyaudio.paContinue)

    # ---- 話すボタンの間の録音
    def rec_start(self):
        with self.lock:
            self.rec = []
            self.recording = True
            self.rec_mute = False

    def rec_stop(self):
        with self.lock:
            self.recording = False
            self.rec_mute = False
            rec, self.rec = self.rec, []
        x = np.concatenate(rec) if rec else np.zeros(0, np.float32)
        return resample(x, self.mic_sr, SR)

    # ---- 英語の声
    def queue(self, samples, sr, text=""):
        """送るボタンまで預かる（前に預かっていた物は置き換える）。"""
        with self.lock:
            self.pending = resample(samples, sr, self.out_sr)
            self.pending_src = (np.asarray(samples, np.float32), sr)
            self.pending_text = text

    def has_pending(self):
        return self.pending is not None

    def drop_pending(self):
        with self.lock:
            self.pending = self.pending_src = None
            self.pending_text = ""

    def busy(self):
        """英語の声を流している最中か。"""
        with self.lock:
            return bool(self.tts)

    def send_start(self):
        """送るボタンを押した: 預かっていた英語を流す。流す物が無ければ 0。"""
        with self.lock:
            if self.pending is None:
                return 0.0
            clip, src = self.pending, self.pending_src
            self.pending = None
            self.tts.clear()
            self.tts.append(clip)
            self.playing = (clip, src)
        self._monitor(*src)
        return len(clip) / self.out_sr

    def send_stop(self):
        """送るボタンを離した: 流している途中なら止めて、次に押したとき頭から流せるよう戻す。"""
        with self.lock:
            if self.playing and self.tts:
                clip, src = self.playing
                self.tts.clear()
                self.pending, self.pending_src = clip, src
                self.playing = None
                stopped = True
            else:
                stopped = False
        if stopped and self.monitor:
            self.monitor.clear()
            self.monitor_until = time.time() + 0.3
        return stopped

    def play(self, samples, sr):
        """すぐ流す（送るボタンを使わない設定）。"""
        y = resample(samples, sr, self.out_sr)
        with self.lock:
            self.tts.append(y)
        self._monitor(samples, sr)
        return len(samples) / sr

    def _monitor(self, samples, sr):
        """自分にも小さく聞かせる（その間はヘッドセットの音を拾わない）。"""
        vol = float(self.cfg.get("monitor_volume", 0))
        if vol <= 0:
            return
        if self.monitor is None:
            self.monitor = _Monitor()
        self.monitor.play(np.asarray(samples, np.float32) * vol, sr)
        self.monitor_until = time.time() + len(samples) / sr + 0.4

    def healthy(self):
        return self.mic_stream.is_active() and self.out_stream.is_active()

    def close(self):
        with _pa_lock:
            for s in (self.mic_stream, self.out_stream):
                try:
                    s.stop_stream()
                    s.close()
                except Exception:
                    pass
        if self.monitor:
            self.monitor.close()


class _Monitor:
    """自分でも英語の声を聞くとき用（既定の出力へ）。"""

    def __init__(self):
        com_init()
        dev = find_device("", "output")
        self.sr = int(dev["defaultSampleRate"])
        self.ch = max(1, min(2, int(dev["maxOutputChannels"])))
        self.q = collections.deque()
        self.lock = threading.Lock()
        with _pa_lock:
            self.stream = pa().open(format=pyaudio.paFloat32, channels=self.ch, rate=self.sr, output=True,
                                    output_device_index=dev["index"], frames_per_buffer=self.sr // 50,
                                    stream_callback=self._cb)

    def _cb(self, in_data, frame_count, time_info, status):
        out = np.zeros(frame_count, np.float32)
        i = 0
        with self.lock:
            while i < frame_count and self.q:
                a = self.q[0]
                k = min(frame_count - i, len(a))
                out[i:i + k] = a[:k]
                if k == len(a):
                    self.q.popleft()
                else:
                    self.q[0] = a[k:]
                i += k
        if self.ch > 1:
            out = np.repeat(out[:, None], self.ch, axis=1)
        return (out.tobytes(), pyaudio.paContinue)

    def play(self, samples, sr):
        y = resample(samples, sr, self.sr)
        with self.lock:
            self.q.append(y)

    def clear(self):
        with self.lock:
            self.q.clear()

    def close(self):
        with _pa_lock:
            self.stream.stop_stream()
            self.stream.close()


class VCListener(threading.Thread):
    """ゲームの VC の出力先にした仮想ケーブル（例 CABLE-A Output）だけを聞き、同じ音をヘッドセットへ中継する。

    ゲームの効果音・アナウンスはヘッドセットへ直に行くので、ここには VC の声だけが来る。
    on_audio には 16 kHz のモノラル、ヘッドセットへは元の音（チャンネルもそのまま）を出す。
    """

    def __init__(self, source, on_audio, on_error, cfg):
        super().__init__(daemon=True, name="vc-listen")
        self.source = source
        self.on_audio = on_audio
        self.on_error = on_error
        self.cfg = cfg            # [listen] の辞書（中継の音量は書き換えればすぐ効く）
        self.stop_ev = threading.Event()
        self.device_label = ""
        self.forward_label = ""
        self.level = 0.0          # VC の声の大きさ（画面の表示用）

    def run(self):
        com_init()
        while not self.stop_ev.is_set():
            try:
                self._run_once()
            except Exception as e:  # 機器が抜けた等。少し待って開き直す
                self.on_error(f"VC の聞き取り: {e}")
                self.stop_ev.wait(3)

    def stop(self):
        self.stop_ev.set()
        self.join(timeout=4)

    def _run_once(self):
        src = find_device(self.source, "input")
        sr, ch = int(src["defaultSampleRate"]), max(1, min(2, int(src["maxInputChannels"])))
        self.device_label = src["name"]
        fwd = None
        if self.cfg.get("forward", True):
            fwd = find_device(self.cfg.get("forward_device", ""), "output")
            if "CABLE" in fwd["name"].upper():
                raise RuntimeError(f"中継先が仮想ケーブルになっている（{fwd['name']}）。ヘッドセットを選ぶ")
            self.forward_label = fwd["name"]
        q = queue.Queue()
        lock = threading.Lock()
        ring = collections.deque()
        state = {"len": 0, "ready": False}
        osr = int(fwd["defaultSampleRate"]) if fwd else sr
        och = max(1, min(2, int(fwd["maxOutputChannels"]))) if fwd else ch

        def in_cb(in_data, frame_count, time_info, status):
            x = np.frombuffer(in_data, np.float32).reshape(-1, ch)
            q.put(x)
            if fwd:
                y = x
                if osr != sr:
                    y = np.stack([resample(x[:, i], sr, osr) for i in range(ch)], axis=1)
                if och != ch:
                    y = np.repeat(y[:, :1], och, axis=1) if och > ch else y[:, :och]
                with lock:
                    ring.append(y)
                    state["len"] += len(y)
                    if state["len"] > osr * 0.12:  # 機器の時計のずれでたまった分を捨てる
                        while state["len"] > osr * 0.04:
                            state["len"] -= len(ring.popleft())
            return (None, pyaudio.paContinue)

        def out_cb(in_data, frame_count, time_info, status):
            out = np.zeros((frame_count, och), np.float32)
            with lock:
                if not state["ready"] and state["len"] >= osr * 0.02:
                    state["ready"] = True
                i = 0
                while state["ready"] and i < frame_count and ring:
                    a = ring[0]
                    k = min(frame_count - i, len(a))
                    out[i:i + k] = a[:k]
                    if k == len(a):
                        ring.popleft()
                    else:
                        ring[0] = a[k:]
                    i += k
                state["len"] -= i
                if state["ready"] and i < frame_count:
                    state["ready"] = False  # 足りなくなったら少しためてから再開
            out *= float(self.cfg.get("forward_gain", 1.0))
            np.clip(out, -1.0, 1.0, out=out)
            return (out.tobytes(), pyaudio.paContinue)

        streams = []
        with _pa_lock:
            streams.append(pa().open(format=pyaudio.paFloat32, channels=ch, rate=sr, input=True,
                                     input_device_index=src["index"], frames_per_buffer=sr // 100,
                                     stream_callback=in_cb))
            if fwd:
                streams.append(pa().open(format=pyaudio.paFloat32, channels=och, rate=osr, output=True,
                                         output_device_index=fwd["index"], frames_per_buffer=osr // 100,
                                         stream_callback=out_cb))
        down = Downsampler(sr)
        last_check = time.monotonic()
        try:
            while not self.stop_ev.is_set():
                try:
                    x = q.get(timeout=0.5).mean(axis=1)
                except queue.Empty:
                    x = np.zeros(sr // 10, np.float32)
                self.level = float(np.abs(x).max()) if len(x) else 0.0
                self.on_audio(down(x))
                now = time.monotonic()
                if now - last_check > 3:
                    last_check = now
                    if not all(s.is_active() for s in streams):
                        raise RuntimeError("音が止まった")
        finally:
            with _pa_lock:
                for s in streams:
                    try:
                        s.stop_stream()
                        s.close()
                    except Exception:
                        pass
