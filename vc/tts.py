"""英語の読み上げ。どこで声を作るかを選べる。

  windows     Windows に入っている声（SAPI 5・OneCore）。PC の中だけで作る。キー不要・無料（既定）
  azure       Azure AI Speech（使う人の Azure のキーと地域）。無料の枠あり
  elevenlabs  ElevenLabs（使う人のキー）
  piper       Piper（使う人が入れた piper.exe と声のモデル）。PC の中だけで作る
  edge        Microsoft Edge の読み上げ（edge-tts）。公式に開かれた方法ではない試し。exe には入れていない

synth(text, sp) → (float32 モノラル, サンプリング周波数)。sp は設定の [speak]。
"""
import io
import json
import re
import subprocess
import urllib.error
import urllib.request
import wave
from pathlib import Path
from xml.sax.saxutils import escape

import numpy as np

from . import config

ENGINES = [  # (鍵, 画面の名前, 説明)
    ("windows", "Windows の声（無料・PC の中だけ）", "Windows に入っている英語の声。機械っぽいが、キーも通信も要らない"),
    ("azure", "Azure AI Speech（自分のキー）", "自然な声。Azure の無料の枠（F0）で月 50 万字まで。英訳の文を Microsoft へ送る"),
    ("elevenlabs", "ElevenLabs（自分のキー）", "とても自然な声。有料のプランが要ることがある。英訳の文を ElevenLabs へ送る"),
    ("piper", "Piper（PC の中だけ・自分で入れる）", "自然寄りの声を PC の中で作る。piper.exe と声のモデル（.onnx）を自分で落として場所を指す"),
    ("edge", "Edge の読み上げ（試し・非公式）", "Microsoft Edge の読み上げを借りる。公式に開かれた方法ではなく、急に使えなくなることがある"),
]
AZURE_VOICES = ["en-US-AndrewNeural", "en-US-BrianNeural", "en-US-GuyNeural", "en-US-ChristopherNeural",
                "en-US-AvaNeural", "en-US-EmmaNeural", "en-US-JennyNeural",
                "en-GB-RyanNeural", "en-GB-SoniaNeural", "en-AU-WilliamNeural"]
EDGE_VOICES = AZURE_VOICES
NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW


class TTSError(Exception):
    pass


def rate_factor(rate):
    """"+10%" → 1.1、"-20%" → 0.8。読めなければ 1.0。"""
    m = re.fullmatch(r"\s*([+-]?\d+(?:\.\d+)?)\s*%\s*", str(rate or ""))
    return max(0.5, min(2.0, 1 + float(m.group(1)) / 100)) if m else 1.0


def pcm16(data, sr, channels=1):
    """16 bit の生 PCM → float32 モノラル。"""
    x = np.frombuffer(data, "<i2").astype(np.float32) / 32768
    if channels > 1:
        x = x.reshape(-1, channels).mean(axis=1)
    return x, sr


def read_wav(data):
    with wave.open(io.BytesIO(data)) as w:
        if w.getsampwidth() != 2:
            raise TTSError("16 bit でない WAV")
        return pcm16(w.readframes(w.getnframes()), w.getframerate(), w.getnchannels())


def synth(text, sp):
    engine = sp.get("engine") or "windows"
    fn = {"windows": _windows, "azure": _azure, "elevenlabs": _elevenlabs, "piper": _piper, "edge": _edge}.get(engine)
    if not fn:
        raise TTSError(f"読み上げの種類がわからない: {engine}")
    audio, sr = fn(text, sp)
    if not len(audio):
        raise TTSError("読み上げの音が来なかった")
    return audio, sr


# ---- Windows の声（SAPI 5。OneCore の声も同じ口で使える）
_SAPI_CATS = ("HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech\\Voices",
              "HKEY_LOCAL_MACHINE\\SOFTWARE\\Microsoft\\Speech_OneCore\\Voices")


def _sapi_tokens():
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    seen, out = set(), []
    for cat_id in _SAPI_CATS:
        try:
            cat = win32com.client.Dispatch("SAPI.SpObjectTokenCategory")
            cat.SetId(cat_id, False)
            toks = cat.EnumerateTokens()
        except Exception:
            continue
        for i in range(toks.Count):
            t = toks.Item(i)
            try:
                name = t.GetDescription()
                lang = t.GetAttribute("Language") or ""
            except Exception:
                continue
            if name in seen:
                continue
            seen.add(name)
            out.append((name, lang, t))
    return out


def _is_english(lang):
    # LCID の 16 進（409 = en-US、809 = en-GB …）。下 2 桁 09 が英語
    return any(code.strip().lower().endswith("09") for code in lang.split(";") if code.strip())


def windows_voices():
    """英語の声の名前（Microsoft David など）。"""
    try:
        return [n for n, lang, _ in _sapi_tokens() if _is_english(lang)]
    except Exception:
        return []


def _windows(text, sp):
    import pythoncom
    import win32com.client
    pythoncom.CoInitialize()
    toks = [(n, t) for n, lang, t in _sapi_tokens() if _is_english(lang)]
    if not toks:
        raise TTSError("Windows に英語の声が入っていない（設定 → 時刻と言語 → 音声 → 音声を追加 で英語を足す）")
    want = (sp.get("windows_voice") or "").lower()
    tok = next((t for n, t in toks if want and want in n.lower()), toks[0][1])
    voice = win32com.client.Dispatch("SAPI.SpVoice")
    voice.Voice = tok
    # SAPI の速さは -10〜10。+10% で 1 ほど
    voice.Rate = max(-10, min(10, round((rate_factor(sp.get("rate")) - 1) * 10)))
    stream = win32com.client.Dispatch("SAPI.SpMemoryStream")
    fmt = win32com.client.Dispatch("SAPI.SpAudioFormat")
    fmt.Type = 34  # SAFT44kHz16BitMono
    stream.Format = fmt
    voice.AudioOutputStream = stream
    voice.Speak(text, 0)
    return pcm16(bytes(stream.GetData()), 44100)


# ---- Azure AI Speech（REST）
def _post(url, body, headers, timeout=20):
    req = urllib.request.Request(url, data=body, headers=dict(headers, **{"User-Agent": "vctrans"}), method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except urllib.error.HTTPError as e:
        detail = e.read()[:200].decode("utf-8", "replace")
        if e.code in (401, 403):
            raise TTSError(f"読み上げのキーが通らない（{e.code}）")
        raise TTSError(f"読み上げのエラー {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise TTSError(f"読み上げにつながらない: {e.reason}")


def _azure(text, sp):
    key = config.secret("azure")
    region = (sp.get("azure_region") or "").strip()
    if not key or not region:
        raise TTSError("Azure のキーと地域を「英語の声」のタブで入れる")
    voice = sp.get("azure_voice") or "en-US-AndrewNeural"
    pct = round((rate_factor(sp.get("rate")) - 1) * 100)
    ssml = (f"<speak version='1.0' xml:lang='en-US'><voice name='{escape(voice)}'>"
            f"<prosody rate='{pct:+d}%'>{escape(text)}</prosody></voice></speak>")
    data = _post(f"https://{region}.tts.speech.microsoft.com/cognitiveservices/v1", ssml.encode("utf-8"),
                 {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/ssml+xml",
                  "X-Microsoft-OutputFormat": "raw-24khz-16bit-mono-pcm"})
    return pcm16(data, 24000)


# ---- ElevenLabs（REST）
def _elevenlabs(text, sp):
    key = config.secret("elevenlabs")
    if not key:
        raise TTSError("ElevenLabs のキーを「英語の声」のタブで入れる")
    voice = (sp.get("elevenlabs_voice") or "").strip()
    if not voice:
        raise TTSError("ElevenLabs の声の ID を入れる")
    body = {"text": text, "model_id": sp.get("elevenlabs_model") or "eleven_flash_v2_5",
            "voice_settings": {"speed": max(0.7, min(1.2, rate_factor(sp.get("rate"))))}}
    data = _post(f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=pcm_24000",
                 json.dumps(body).encode("utf-8"), {"xi-api-key": key, "Content-Type": "application/json"})
    return pcm16(data, 24000)


# ---- Piper（使う人が入れた piper.exe を窓なしで呼ぶ）
def _piper(text, sp):
    exe, model = (sp.get("piper_exe") or "").strip(), (sp.get("piper_model") or "").strip()
    if not exe or not Path(exe).exists():
        raise TTSError("piper.exe の場所を「英語の声」のタブで指す")
    if not model or not Path(model).exists():
        raise TTSError("Piper の声のモデル（.onnx）の場所を指す")
    meta = Path(model + ".json")
    sr = 22050
    if meta.exists():
        try:
            sr = int(json.loads(meta.read_text(encoding="utf-8"))["audio"]["sample_rate"])
        except Exception:
            pass
    length = 1 / rate_factor(sp.get("rate"))
    r = subprocess.run([exe, "--model", model, "--output-raw", "--length_scale", f"{length:.2f}"],
                       input=text.encode("utf-8"), capture_output=True, timeout=30,
                       cwd=str(Path(exe).parent), creationflags=NO_WINDOW)
    if r.returncode != 0 or not r.stdout:
        raise TTSError("Piper が声を作れなかった: " + r.stderr[-200:].decode("utf-8", "replace").strip())
    return pcm16(r.stdout, sr)


# ---- Edge の読み上げ（試し）
def edge_available():
    try:
        import edge_tts  # noqa: F401
        import av  # noqa: F401  mp3 を開くのに使う
        return True
    except Exception:
        return False


def _edge(text, sp):
    import asyncio
    try:
        import edge_tts
        from faster_whisper.audio import decode_audio
    except ImportError:
        raise TTSError("Edge の読み上げはこの版に入っていない（Python で動かすときだけ使える）")

    async def go():
        buf = io.BytesIO()
        com = edge_tts.Communicate(text, sp.get("voice") or "en-US-AndrewNeural", rate=sp.get("rate") or "+0%")
        async for chunk in com.stream():
            if chunk["type"] == "audio":
                buf.write(chunk["data"])
        return buf.getvalue()

    mp3 = asyncio.run(go())
    if not mp3:
        raise TTSError("読み上げの音が来なかった")
    return decode_audio(io.BytesIO(mp3), sampling_rate=24000), 24000


def needs_network(sp):
    return (sp.get("engine") or "windows") in ("azure", "elevenlabs", "edge")
