"""設定（config.toml）・ゲームごとの設定（profiles/*.toml）・キー（.env）・用語集を読む。

値の強さ（後ろほど強い）: DEFAULTS → ゲームのプロファイル → config.toml → logs/settings.json（操作の窓で変えた値）
"""
import copy
import json
import os
import re
import sys
import tomllib
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))
# 使う人が書き換える物（config.toml・profiles・logs・.env・models）を置く所。exe ならその exe のフォルダ
ROOT = Path(sys.executable).resolve().parent if FROZEN else Path(__file__).resolve().parent.parent
PROFILES = ROOT / "profiles"
DEFAULT_PROFILE = "arc-raiders"

DEFAULTS = {
    "profile": DEFAULT_PROFILE,
    "game": "",
    "listen": {
        "device": "",
        "languages": ["en"],
        "min_lang_prob": 0.5,
        "vad_threshold": 0.5,
        "silence_ms": 700,
        "max_segment_s": 12,
        "min_speech_ms": 300,
        "hotwords": "",
        "vc_source": "",
        "forward": True,
        "forward_device": "",
        "forward_gain": 1.0,
    },
    "speak": {
        "enabled": True,
        "ptt": "pad:right",
        "ptt_hold_ms": 500,
        "save_voice": True,
        "send_mode": "auto",
        "send_key": "",
        "lead_ms": 250,
        "pending_s": 90,
        "mute_while_recording": True,
        "mic_device": "",
        "vc_device": "CABLE Input",
        "passthrough": True,
        "mic_gain": 1.0,
        "mute_mic_while_tts": True,
        "engine": "windows",          # windows / azure / elevenlabs / piper / edge（試し）
        "windows_voice": "",
        "azure_voice": "en-US-AndrewNeural",
        "azure_region": "japaneast",
        "elevenlabs_voice": "pNInz6obpgDQGcFmaJgB",
        "elevenlabs_model": "eleven_flash_v2_5",
        "piper_exe": "",
        "piper_model": "",
        "voice": "en-US-AndrewNeural",  # edge（試し）の声
        "rate": "+10%",
        "tts_gain": 1.0,
        "monitor_volume": 0.0,
    },
    "asr": {
        "device": "auto",             # auto: NVIDIA の GPU があれば cuda、無ければ cpu
        "model": "large-v3-turbo",
        "compute_type": "int8_float16",
        "cpu_model": "small",
        "cpu_compute_type": "int8",
        "beam_size": 5,
    },
    "translate": {
        "model": "claude-haiku-5-5",
        "effort": "low",
        "thinking_off": True,
        "context_lines": 6,
        "glossary_json": "",
    },
    "overlay": {
        "follow_exe": "",
        "log_x": 0.012,
        "log_bottom": 0.26,
        "log_w": 0.30,
        "log_h": 0.30,
        "font_size": 0.0175,
        "src_size": 0.013,
        "show_source": True,
        "hold_s": 0,
        "locked": True,
        "font": "Noto Sans JP",
        "edit_key": "ctrl+alt+o",
        "quit_key": "ctrl+alt+q",
        "panel_key": "ctrl+alt+p",
    },
}

GENERIC = {"id": "", "name": "（ゲームを決めない）", "exe": "", "send_key": "", "hotwords": "", "glossary": "",
           "prompt_ja": "", "setup": [], "notes": []}


def _merge(base, over):
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def _read_toml(p):
    try:
        return tomllib.loads(Path(p).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def _read_json(p):
    try:
        return json.loads(Path(p).read_text(encoding="utf-8"))
    except Exception:
        return {}


# ---- ゲームごとの設定
def list_profiles():
    """[(id, ゲーム名)]。profiles/<id>.toml を名前の順に。"""
    out = []
    for p in sorted(PROFILES.glob("*.toml")):
        try:
            out.append((p.stem, _read_toml(p).get("name") or p.stem))
        except Exception:
            continue
    return out


def load_profile(pid):
    """profiles/<pid>.toml → dict（GENERIC の鍵がそろう）。無い・空の id なら GENERIC。"""
    prof = dict(GENERIC, setup=[], notes=[], glossary_path="")
    if not pid:
        return prof
    p = PROFILES / f"{pid}.toml"
    if not p.exists():
        return prof
    data = _read_toml(p)
    prof.update({k: v for k, v in data.items() if k in GENERIC})
    prof["id"] = pid
    g = prof.get("glossary") or ""
    prof["glossary_path"] = str((PROFILES / g).resolve()) if g else ""
    return prof


def apply_profile(cfg, prof):
    """プロファイルの値を設定の該当する所へ写す。"""
    cfg["game"] = prof["name"] if prof.get("id") else ""
    cfg["listen"]["hotwords"] = prof.get("hotwords", "")
    cfg["overlay"]["follow_exe"] = prof.get("exe", "")
    cfg["speak"]["send_key"] = prof.get("send_key", "")
    cfg["profile_info"] = prof


def user_overrides():
    """config.toml と logs/settings.json を合わせた物（プロファイルより強い）。"""
    over = {}
    _merge(over, _read_toml(ROOT / "config.toml"))
    _merge(over, _read_json(ROOT / "logs" / "settings.json"))
    return over


def load():
    cfg = copy.deepcopy(DEFAULTS)
    over = user_overrides()
    apply_profile(cfg, load_profile(over.get("profile", DEFAULT_PROFILE)))
    _merge(cfg, over)
    st = _read_json(ROOT / "logs" / "state.json")  # 字幕を動かした位置・大きさ
    if st:
        _merge(cfg["overlay"], {k: v for k, v in st.items() if k in DEFAULTS["overlay"]})
    return cfg


def load_state():
    return _read_json(ROOT / "logs" / "state.json")


def save_state(values):
    """logs/state.json に足す（字幕の位置・大きさ、初回の案内を見たか など）。"""
    st = ROOT / "logs" / "state.json"
    st.parent.mkdir(exist_ok=True)
    old = _read_json(st)
    old.update(values)
    st.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")


save_overlay_state = save_state


def save_setting(section, key, value):
    """操作の窓で変えた値を logs/settings.json に残す。section が空なら一番上の鍵。value が None なら消す。"""
    sv = ROOT / "logs" / "settings.json"
    sv.parent.mkdir(exist_ok=True)
    data = _read_json(sv)
    box = data.setdefault(section, {}) if section else data
    if value is None:
        box.pop(key, None)
    else:
        box[key] = value
    sv.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---- キー（.env）
ENV = ROOT / ".env"
KEY_NAMES = {  # 種類: .env で読む名前（先にある物ほど強い。書くときは最初の名前）
    "claude": ("ClaudeAPIKey", "ANTHROPIC_API_KEY"),
    "azure": ("AzureSpeechKey",),
    "elevenlabs": ("ElevenLabsKey",),
}


def _env_lines():
    try:
        return ENV.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []


def secret(kind):
    """環境変数か .env から。無ければ None。"""
    names = KEY_NAMES[kind]
    for n in names:
        v = os.environ.get(n, "").strip()
        if v:
            return v
    vals = {}
    for line in _env_lines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, val = line.split("=", 1)
        vals.setdefault(name.strip(), val.strip().strip("\"'"))
    for n in names:
        if vals.get(n):
            return vals[n]
    return None


def save_secret(kind, value):
    """.env の該当する行を書き換える（ほかの行はそのまま）。value が空なら消す。"""
    name = KEY_NAMES[kind][0]
    out, done = [], False
    for line in _env_lines():
        n = line.split("=", 1)[0].strip() if "=" in line else ""
        if n == name:
            if not done and value:
                out.append(f"{name}={value}")
            done = True
            continue
        out.append(line)
    if not done and value:
        out.append(f"{name}={value}")
    ENV.write_text("".join(f"{line}{chr(10)}" for line in out), encoding="utf-8")


def api_key():
    return secret("claude")


def save_api_key(key):
    save_secret("claude", key)


# ---- 用語集
def load_glossary(cfg):
    """[(英語, 日本語)]。プロファイルの用語集 → [translate] glossary_json → glossary.txt（後ろほど強い）。"""
    pairs = {}
    paths = [(cfg.get("profile_info") or {}).get("glossary_path"), cfg["translate"].get("glossary_json")]
    for path in paths:
        if not path or not Path(path).exists():
            continue
        try:
            if str(path).lower().endswith(".json"):
                data = json.loads(Path(path).read_text(encoding="utf-8"))
                for en, v in data.items():
                    ja = v.get("ja") if isinstance(v, dict) else v
                    if isinstance(ja, str) and ja and len(en) >= 3:
                        pairs[en.strip('"')] = ja
            else:
                pairs.update(read_pairs(path))
        except Exception:
            pass
    pairs.update(read_pairs(ROOT / "glossary.txt"))
    return list(pairs.items())


def read_pairs(path):
    """「英語 = 日本語」の行が並ぶ文書 → dict。# から始まる行は飛ばす。"""
    out = {}
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return out
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        en, ja = (s.strip() for s in line.split("=", 1))
        if en and ja:
            out[en] = ja
    return out


def pick_glossary(glossary, text, limit=40):
    """発言に出てくる語だけを選ぶ（英語は語の境目つき・大小を無視、日本語はそのまま）。"""
    low = text.lower()
    hits = []
    for en, ja in glossary:
        if len(ja) >= 2 and ja in text:
            hits.append((en, ja))
        elif en.lower() in low and re.search(r"(?<![a-z])" + re.escape(en.lower()) + r"(?![a-z])", low):
            hits.append((en, ja))
    hits.sort(key=lambda p: -len(p[0]))  # 長い語を先に
    return hits[:limit]
