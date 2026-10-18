"""自分の声の学習。録音と文字起こしをためて、直した例を次の聞き取りと訳に生かす。

logs/voice/
  clips/<id>.wav   自分の録音（16 kHz・モノラル）。PC の中だけ。あとで文字起こしのモデルを自分の声で追加学習する材料
  index.jsonl      1 行 1 録音: id・時刻・文字起こし（asr）・Haiku が直した日本語（fixed）・自分で直した日本語（corrected）
  vocab.txt        よく使う言葉（1 行 1 語）。文字起こしのヒントに渡す

生かし方
- 文字起こし（Whisper）: よく使う言葉と、自分で直した最近の文を initial_prompt に（言い回しと語を寄せる）
- 訳（Haiku）: 「認識 → 正しい」の例を渡して、同じ聞き違いを文脈で直させる
"""
import datetime
import json
import threading
import wave
from pathlib import Path

import numpy as np

from .config import ROOT

DIR = ROOT / "logs" / "voice"
CLIPS = DIR / "clips"
INDEX = DIR / "index.jsonl"
VOCAB = DIR / "vocab.txt"
BASE_PROMPT = "オンラインゲームのボイスチャットでの会話。"  # ゲームのプロファイルの prompt_ja が無いとき
_lock = threading.Lock()


def _rows():
    if not INDEX.exists():
        return []
    rows = []
    for line in INDEX.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except Exception:
            pass
    return rows


def _write(rows):
    INDEX.write_text("".join(json.dumps(r, ensure_ascii=False) + chr(10) for r in rows), encoding="utf-8")


def save_clip(audio):
    """16 kHz の録音を残して id を返す。"""
    CLIPS.mkdir(parents=True, exist_ok=True)
    cid = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    pcm = (np.clip(np.asarray(audio, np.float32), -1, 1) * 32767).astype("<i2")
    with wave.open(str(CLIPS / f"{cid}.wav"), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(pcm.tobytes())
    return cid


def record(cid, asr, fixed=None):
    """文字起こしと Haiku の直しを残す。"""
    if not cid:
        return
    with _lock:
        DIR.mkdir(parents=True, exist_ok=True)
        with open(INDEX, "a", encoding="utf-8") as f:
            f.write(json.dumps(dict(id=cid, t=datetime.datetime.now().isoformat(timespec="seconds"),
                                    asr=asr, fixed=fixed, corrected=None), ensure_ascii=False) + chr(10))


def correct(cid, text):
    """自分で直した日本語を残す（その録音の正しい文になる）。"""
    with _lock:
        rows = _rows()
        for r in rows:
            if r.get("id") == cid:
                r["corrected"] = text
        _write(rows)


def vocab():
    if not VOCAB.exists():
        return []
    return [w.strip() for w in VOCAB.read_text(encoding="utf-8-sig").splitlines() if w.strip()]


def save_vocab(words):
    DIR.mkdir(parents=True, exist_ok=True)
    VOCAB.write_text("".join(w + chr(10) for w in words if w.strip()), encoding="utf-8")


def prompt_text(base="", limit=180):
    """文字起こしのヒント。ゲームの一言＋よく使う言葉＋自分で直した最近の文（新しい順に、字数の上限まで）。"""
    parts = [base or BASE_PROMPT]
    words = vocab()
    if words:
        parts.append("、".join(words[:40]) + "。")
    with _lock:
        rows = _rows()
    for r in reversed(rows):
        c = r.get("corrected")
        if c and sum(len(p) for p in parts) + len(c) < limit:
            parts.append(c if c.endswith(("。", "？", "！")) else c + "。")
    return "".join(parts)


def examples(limit=12):
    """Haiku に渡す「認識 → 正しい」の例（違っていた物だけ、新しい順）。"""
    with _lock:
        rows = _rows()
    out = []
    for r in reversed(rows):
        a, c = r.get("asr") or "", r.get("corrected") or ""
        if c and a and a != c:
            out.append((a, c))
        if len(out) >= limit:
            break
    return out


def stats():
    with _lock:
        rows = _rows()
    secs = 0.0
    for r in rows:
        p = CLIPS / f"{r.get('id')}.wav"
        if p.exists():
            secs += max(0, p.stat().st_size - 44) / 32000
    return dict(clips=len(rows), corrected=sum(1 for r in rows if r.get("corrected")), minutes=secs / 60)


def recent(n=12):
    with _lock:
        rows = _rows()
    return rows[-n:]
