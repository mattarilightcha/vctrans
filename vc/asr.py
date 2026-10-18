"""文字起こし（faster-whisper）。相手の声は言語を見て英語などだけ通し、雑音からの空耳を捨てる。"""
import os
import re
import threading

from . import prepare

# 読み込みの前に: exe なら runtime/cuda（初回に取った NVIDIA の部品）、Python なら pip の nvidia-* を DLL の探し先へ
if prepare.FROZEN:
    prepare.stub_av()
    prepare.add_dll_dir(prepare.RUNTIME)
else:
    prepare.pip_cuda_dirs()
from faster_whisper import WhisperModel  # noqa: E402

# 雑音・無音に Whisper がよく出す空耳。必ず捨てる物と、自信の低いときだけ捨てる物
_ALWAYS = re.compile(r"(thanks? (you )?for watching|subscribe|subtitles? by|amara\.org|♪|\[music\]|\(music\))", re.I)
_WEAK = {"you", "thank you", "thank you.", "thanks.", "bye.", "bye", "okay.", "so", "uh", "um", "hmm", "oh", "the", "i"}


# 言いよどみ（文の頭か、読点・句点・空白のあとに来た物だけ消す。「ええ」（はい）や「あの人」は残す）
_FILLER = re.compile(
    r"(?:^|(?<=[、。,.\s！？!?]))\s*"
    r"(?:えーっと|えーと|えっと|ええと|えー+|えぇ+|あー+|あぁ+|ああ(?=[、,])|あのー+|あの(?=[、,])|"
    r"うーん+|うーむ|んー+|んっと|まー+(?=[、,])|そのー+|なんか(?=[、,]))"
    r"[、,。…ー〜\s]*")


def strip_fillers(text):
    """「えーと、こんばんは」→「こんばんは」。消して空になったら空。"""
    prev = None
    while prev != text:
        prev, text = text, _FILLER.sub("", text)
    return text.strip(" 、,")


def _prep(audio):
    """VC の声の下ごしらえ: 80 Hz より低い雑音を切り、大きさをそろえる（小さい声・こもった声の取りこぼしを減らす）。"""
    import numpy as np
    from scipy.signal import butter, sosfilt
    x = np.asarray(audio, np.float32)
    if len(x) < 1600:
        return x
    x = sosfilt(_HP, x).astype(np.float32)
    loud = np.sort(np.abs(x))[int(len(x) * 0.5):]  # 声のある側（上半分）の大きさで測る
    rms = float(np.sqrt(np.mean(loud ** 2))) if len(loud) else 0.0
    if rms > 1e-4:
        x *= min(8.0, max(0.5, 0.12 / rms))
    peak = float(np.abs(x).max())
    if peak > 0.95:
        x *= 0.95 / peak
    return x


def _hp():
    from scipy.signal import butter
    return butter(2, 80, btype="highpass", fs=16000, output="sos")


_HP = _hp()


class ASR:
    def __init__(self, path, device, compute_type, beam_size=5):
        """path: モデルのフォルダ（prepare.ensure_model の戻り値）。"""
        self.device = device
        self.model = WhisperModel(path, device=device, compute_type=compute_type,
                                  cpu_threads=max(1, (os.cpu_count() or 4) // 2))
        self.beam = int(beam_size)
        self.lock = threading.Lock()
        self._warm_up()

    def _warm_up(self):
        """最初の 1 回は GPU の準備で 3 秒ほどかかるので、起動のうちに空の音で済ませておく。"""
        import numpy as np
        segs, _ = self.model.transcribe(np.zeros(16000, np.float32), language="en", beam_size=1,
                                        without_timestamps=True)
        list(segs)

    def listen(self, audio, languages, min_prob, hotwords=None, prompt=None):
        """戻り値 (文, 言語, 言語の確からしさ)。訳す言語でない・空耳なら文は None。

        短い発言は言語の見分けが外れやすい（英語なのに別の言語と出て捨てられる）ので、
        いちばんでなくても訳す言語の見込みが 15% 以上あり、日本語とはっきり出ていなければ、その言語として文字にし直す。"""
        audio = _prep(audio)
        kw = dict(beam_size=self.beam, vad_filter=False, condition_on_previous_text=False,
                  without_timestamps=True, hotwords=hotwords or None, initial_prompt=prompt or None)
        with self.lock:
            segs, info = self.model.transcribe(audio, language=None, **kw)
            lang, prob = info.language, info.language_probability
            probs = dict(info.all_language_probs or [])
            if lang in languages and prob >= min_prob:
                segs = list(segs)
            else:
                best_p, best = max((probs.get(l, 0.0), l) for l in languages)
                if best_p < 0.15 or (lang == "ja" and prob >= 0.5):
                    return None, lang, prob
                segs, _ = self.model.transcribe(audio, language=best, **kw)
                segs = list(segs)
                lang, prob = best, best_p
        parts = []
        for s in segs:
            if s.no_speech_prob > 0.6 and s.avg_logprob < -0.8:
                continue
            if s.avg_logprob < -1.2 or s.compression_ratio > 2.4:
                continue
            parts.append((s.text.strip(), s.avg_logprob, s.no_speech_prob))
        text = " ".join(p[0] for p in parts if p[0]).strip()
        if not text or _ALWAYS.search(text):
            return None, lang, prob
        if text.lower().strip(" .!?,") in {w.strip(".") for w in _WEAK}:
            worst = min(p[1] for p in parts)
            noisy = max(p[2] for p in parts)
            if worst < -0.6 or noisy > 0.3:
                return None, lang, prob
        return text, lang, prob

    def japanese(self, audio, prompt=None):
        with self.lock:
            segs, _ = self.model.transcribe(
                audio, language="ja", beam_size=self.beam, vad_filter=True,
                condition_on_previous_text=False, without_timestamps=True,
                initial_prompt=prompt or None)
            segs = list(segs)
        text = "".join(s.text.strip() for s in segs if s.avg_logprob > -1.5).strip()
        if re.fullmatch(r"[\s。、・…ー!?！？]*|(ご視聴ありがとうございました。?)", text):
            return ""
        return text
