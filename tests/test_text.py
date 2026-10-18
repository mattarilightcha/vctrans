"""文字起こしのあとの文の扱い（faster-whisper を読み込むが、モデルは読まない）。"""
import numpy as np
import pytest

asr = pytest.importorskip("vc.asr")


@pytest.mark.parametrize("raw, want", [
    ("えーと、こんばんは", "こんばんは"),
    ("あのー、右に敵がいる", "右に敵がいる"),
    ("ええ、そうです", "ええ、そうです"),          # 「ええ」（はい）は残す
    ("あの人が持ってる", "あの人が持ってる"),
    ("うーん、えっと、", ""),
    ("行こう。えー、右から", "行こう。右から"),
])
def test_strip_fillers(raw, want):
    assert asr.strip_fillers(raw) == want


def test_prep_levels_and_shape():
    rng = np.random.default_rng(0)
    quiet = (np.sin(np.arange(16000) / 16000 * 2 * np.pi * 300) * 0.01).astype(np.float32)
    out = asr._prep(quiet + rng.standard_normal(16000).astype(np.float32) * 1e-4)
    assert out.shape == quiet.shape and out.dtype == np.float32
    assert np.abs(out).max() > 0.05            # 小さい声を持ち上げる
    loud = np.ones(16000, np.float32) * 3
    assert np.abs(asr._prep(loud)).max() <= 0.95 + 1e-6
    short = np.zeros(100, np.float32)
    assert asr._prep(short) is not None
