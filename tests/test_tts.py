import io
import json
import wave

import numpy as np
import pytest

from vc import config, tts


def test_rate_factor():
    assert tts.rate_factor("+10%") == pytest.approx(1.1)
    assert tts.rate_factor("-20%") == pytest.approx(0.8)
    assert tts.rate_factor("fast") == 1.0
    assert tts.rate_factor("+500%") == 2.0


def test_pcm16_and_wav():
    x = (np.array([0, 16384, -16384, 32767], "<i2")).tobytes()
    a, sr = tts.pcm16(x, 24000)
    assert sr == 24000 and a.dtype == np.float32 and a[1] == pytest.approx(0.5)
    st, _ = tts.pcm16(np.array([100, 300, -100, -300], "<i2").tobytes(), 8000, channels=2)
    assert len(st) == 2
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(x)
    a, sr = tts.read_wav(buf.getvalue())
    assert sr == 16000 and len(a) == 4


def test_unknown_engine():
    with pytest.raises(tts.TTSError):
        tts.synth("hi", {"engine": "nope"})


def test_azure_request(root, monkeypatch):
    config.save_secret("azure", "AZKEY")
    sent = {}

    def fake_post(url, body, headers, timeout=20):
        sent.update(url=url, body=body.decode("utf-8"), headers=headers)
        return np.zeros(2400, "<i2").tobytes()

    monkeypatch.setattr(tts, "_post", fake_post)
    a, sr = tts.synth("Tom & Jerry <go>", {"engine": "azure", "azure_region": "japaneast",
                                           "azure_voice": "en-US-AvaNeural", "rate": "+10%"})
    assert sr == 24000 and len(a) == 2400
    assert sent["url"] == "https://japaneast.tts.speech.microsoft.com/cognitiveservices/v1"
    assert sent["headers"]["Ocp-Apim-Subscription-Key"] == "AZKEY"
    assert "Tom &amp; Jerry &lt;go&gt;" in sent["body"] and "en-US-AvaNeural" in sent["body"]
    assert "rate='+10%'" in sent["body"]


def test_azure_needs_key(root):
    with pytest.raises(tts.TTSError, match="Azure"):
        tts.synth("hi", {"engine": "azure", "azure_region": "japaneast"})


def test_elevenlabs_request(root, monkeypatch):
    config.save_secret("elevenlabs", "ELKEY")
    sent = {}

    def fake_post(url, body, headers, timeout=20):
        sent.update(url=url, body=json.loads(body), headers=headers)
        return np.zeros(100, "<i2").tobytes()

    monkeypatch.setattr(tts, "_post", fake_post)
    tts.synth("Hello", {"engine": "elevenlabs", "elevenlabs_voice": "VOICE", "rate": "+50%"})
    assert sent["url"].startswith("https://api.elevenlabs.io/v1/text-to-speech/VOICE?output_format=pcm_24000")
    assert sent["headers"]["xi-api-key"] == "ELKEY"
    assert sent["body"]["voice_settings"]["speed"] == 1.2   # ElevenLabs の上限に丸める


def test_piper_paths_checked(tmp_path):
    with pytest.raises(tts.TTSError, match="piper.exe"):
        tts.synth("hi", {"engine": "piper", "piper_exe": str(tmp_path / "none.exe")})


def test_english_lcid():
    assert tts._is_english("409")
    assert tts._is_english("809;409")
    assert not tts._is_english("411")
