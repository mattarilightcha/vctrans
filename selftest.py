"""動作の確かめ。音は鳴らさない（cable は仮想ケーブルの中だけで鳴らして拾い直す）。

  python selftest.py api              Haiku で英→日・日→英を 1 回ずつ
  python selftest.py asr [--cpu]      合成音声で 声の切り出し → 文字起こし（英語は通す・日本語は捨てる）
  python selftest.py cable            英語の声を CABLE Input へ流し、CABLE Output で届いたか測る
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

import numpy as np  # noqa: E402

from vc import config  # noqa: E402

EN = "Hey, there's an ARC sentinel near the tower. Let's grab the loot and extract."
JA = "了解、こっちにも敵がいるから先に脱出地点に向かって。"


def part_api(cfg):
    from vc.translate import Translator
    glossary = config.load_glossary(cfg)
    tr = Translator(config.api_key(), cfg["translate"], cfg["game"], glossary, core_terms=cfg["listen"]["hotwords"])
    print("用語集", len(glossary), "語。この発言で渡す語:", config.pick_glossary(glossary, EN))
    for text, to, ctx in ((EN, "ja", []), (JA, "en", [("相手", EN)])):
        t0 = time.time()
        out = tr(text, to, ctx)
        print(f"{to}  {time.time() - t0:.2f} 秒  {text}\n    → {out}")


def part_asr(cfg, cpu):
    from vc.asr import ASR
    from vc.tts import synth
    from vc.audio import resample
    from vc.vad import Segmenter
    acfg = dict(cfg["asr"])
    if cpu:
        acfg.update(device="cpu", beam_size=1)
    from vc.tts import edge_available
    eng = "edge" if edge_available() else "windows"  # 日本語の声は Edge の読み上げだけ（無ければ英語だけ確かめる）
    en, sr = synth(EN, dict(cfg["speak"], engine=eng, voice="en-US-AndrewNeural"))
    ja = synth(JA, dict(cfg["speak"], engine=eng, voice="ja-JP-NanamiNeural"))[0] if eng == "edge" else np.zeros(0, np.float32)
    en, ja = resample(en, sr, 16000), resample(ja, sr, 16000)
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(16000 * 2) * 0.02).astype(np.float32)  # 雑音だけの 2 秒
    gap = np.zeros(16000, np.float32)
    stream = np.concatenate([gap, en, gap, noise, gap, ja, gap, gap])
    segs = []
    seg = Segmenter(segs.append, **{k: cfg["listen"][k] for k in ("silence_ms", "max_segment_s", "min_speech_ms")},
                    threshold=cfg["listen"]["vad_threshold"])
    for i in range(0, len(stream), 480):  # 30 ms ずつ流す
        seg.feed(stream[i:i + 480])
    print("切り出し:", [f"{len(s) / 16000:.1f}秒" for s in segs], "（英語・日本語の 2 つが出れば正しい）")
    t0 = time.time()
    from vc import prepare
    device, model, compute, _ = prepare.plan(acfg)
    asr = ASR(prepare.ensure_model(model), device, compute, acfg.get("beam_size", 5))
    print(f"モデル {model}（{device}）{time.time() - t0:.1f} 秒")
    ls = cfg["listen"]
    for s in segs:
        t0 = time.time()
        text, lang, prob = asr.listen(s, ls["languages"], ls["min_lang_prob"], ls["hotwords"])
        print(f"  相手として: {lang} {prob:.2f} {time.time() - t0:.2f} 秒 → {text!r}")
    t0 = time.time()
    text = asr.japanese(ja)
    print(f"  自分として（日本語）: {time.time() - t0:.2f} 秒 → {text!r}")


def part_cable(cfg):
    import pyaudiowpatch as pyaudio
    from vc.audio import VoiceRouter, find_device, pa
    from vc.tts import synth
    sp = dict(cfg["speak"], passthrough=False, monitor_volume=0.0)
    router = VoiceRouter(sp)
    print("出す先:", router.out_label)
    cap = find_device("CABLE Output", "input")
    sr, ch = int(cap["defaultSampleRate"]), max(1, min(2, int(cap["maxInputChannels"])))
    got = []
    stream = pa().open(format=pyaudio.paFloat32, channels=ch, rate=sr, input=True, input_device_index=cap["index"],
                       frames_per_buffer=sr // 50,
                       stream_callback=lambda d, n, t, s: (got.append(np.frombuffer(d, np.float32)), (None, pyaudio.paContinue))[1])
    audio, asr = synth("Testing the virtual cable.", sp)
    time.sleep(0.3)
    secs = router.play(audio * 0.3, asr)
    time.sleep(secs + 0.5)
    stream.stop_stream()
    stream.close()
    router.close()
    x = np.concatenate(got) if got else np.zeros(1)
    print(f"CABLE Output で拾えた音の大きさ: 最大 {np.abs(x).max():.3f}（0.05 以上なら届いている）")


if __name__ == "__main__":
    cfg = config.load()
    part = sys.argv[1] if len(sys.argv) > 1 else "api"
    if part == "api":
        part_api(cfg)
    elif part == "asr":
        part_asr(cfg, "--cpu" in sys.argv)
    elif part == "cable":
        part_cable(cfg)
