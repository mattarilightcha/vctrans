"""声のある所だけを切り出す（faster-whisper 同梱の Silero VAD を流しながら使う）。"""
import collections

import numpy as np
from faster_whisper.vad import get_vad_model

WIN = 512  # 16 kHz で 32 ms
CTX = 64


class StreamVAD:
    """SileroVADModel.__call__ と同じ入力の組み方で、状態（h・c・前の窓の尾）を呼び出しの間で持ち越す。"""

    def __init__(self):
        self.sess = get_vad_model().session
        self.reset()

    def reset(self):
        self.h = np.zeros((1, 1, 128), np.float32)
        self.c = np.zeros((1, 1, 128), np.float32)
        self.tail = np.zeros(CTX, np.float32)

    def __call__(self, x):
        w = x.reshape(-1, WIN).astype(np.float32)
        ctx = np.concatenate([self.tail[None, :], w[:-1, -CTX:]], axis=0)
        batch = np.concatenate([ctx, w], axis=1)
        out, self.h, self.c = self.sess.run(None, {"input": batch, "h": self.h, "c": self.c})
        self.tail = w[-1, -CTX:].copy()
        return np.asarray(out).reshape(-1)


class Segmenter:
    """話し始めから、silence_ms 黙るまでを 1 つの発言として on_segment へ渡す。"""

    def __init__(self, on_segment, threshold=0.5, silence_ms=600, max_segment_s=12,
                 min_speech_ms=300, preroll_ms=300, on_state=None):
        self.on_segment = on_segment
        self.on_state = on_state or (lambda speaking: None)
        self.th = threshold
        self.neg = max(0.15, threshold - 0.15)
        self.silence_win = max(1, silence_ms // 32)
        self.max_win = int(max_segment_s * 1000 // 32)
        self.min_speech_win = max(1, min_speech_ms // 32)
        self.vad = StreamVAD()
        self.pending = np.zeros(0, np.float32)
        self.pre = collections.deque(maxlen=max(1, preroll_ms // 32))
        self.cur = None
        self.silence = 0
        self.speech = 0

    def feed(self, x):
        x = np.concatenate([self.pending, x]) if len(self.pending) else x
        n = len(x) // WIN * WIN
        self.pending = x[n:]
        if n == 0:
            return
        probs = self.vad(x[:n])
        for w, p in zip(x[:n].reshape(-1, WIN), probs):
            self._step(w, float(p))

    def _step(self, w, p):
        if self.cur is None:
            self.pre.append(w)
            if p >= self.th:
                self.cur = list(self.pre)
                self.pre.clear()
                self.silence, self.speech = 0, 1
                self.on_state(True)
            return
        self.cur.append(w)
        if p >= self.th:
            self.speech += 1
            self.silence = 0
        elif p >= self.neg:
            self.silence = 0
        else:
            self.silence += 1
        if self.silence >= self.silence_win:
            keep = len(self.cur) - self.silence + min(self.silence, 6)  # 後ろの無音は 0.2 秒だけ残す
            self._emit(self.cur[:keep])
            self.cur = None
            self.on_state(False)
        elif len(self.cur) >= self.max_win:  # 長すぎる発言は区切って先に出す（話し中のまま続ける）
            self._emit(self.cur)
            self.cur, self.silence, self.speech = [], 0, 0

    def _emit(self, wins):
        if self.speech >= self.min_speech_win and wins:
            self.on_segment(np.concatenate(wins))
