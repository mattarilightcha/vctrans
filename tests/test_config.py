import json
import shutil
from pathlib import Path

from vc import config

REPO = Path(__file__).resolve().parent.parent


def write(p, text):
    p.write_text(text, encoding="utf-8")


def test_defaults_without_files(root):
    cfg = config.load()
    # ARC のプロファイルが無いフォルダでは、ゲームを決めない形になる
    assert cfg["game"] == ""
    assert cfg["overlay"]["follow_exe"] == ""
    assert cfg["speak"]["send_key"] == ""
    assert cfg["speak"]["engine"] == "windows"
    assert cfg["asr"]["device"] == "auto"


def test_bundled_arc_profile(root):
    shutil.copy(REPO / "profiles" / "arc-raiders.toml", root / "profiles")
    cfg = config.load()
    assert cfg["game"] == "ARC Raiders"
    assert cfg["overlay"]["follow_exe"] == "PioneerGame.exe"
    assert cfg["speak"]["send_key"] == "b"
    assert "Bastion" in cfg["listen"]["hotwords"]
    assert cfg["profile_info"]["setup"]


def test_bundled_profiles_parse():
    files = list((REPO / "profiles").glob("*.toml"))
    assert len(files) >= 2
    for p in files:
        data = config._read_toml(p)
        assert data.get("name"), p
        assert set(data) <= set(config.GENERIC), p   # 知らない鍵を書いていない
        for row in data.get("setup", []):
            assert len(row) == 2, p


def test_precedence(root):
    write(root / "profiles" / "g.toml", 'name = "G"\nexe = "g.exe"\nsend_key = "v"\nhotwords = "Foo"\n')
    write(root / "config.toml", 'profile = "g"\n[speak]\nsend_key = "c"\nrate = "+0%"\n')
    write(root / "logs" / "settings.json", json.dumps({"speak": {"rate": "+20%"}}))
    cfg = config.load()
    assert cfg["game"] == "G"
    assert cfg["overlay"]["follow_exe"] == "g.exe"
    assert cfg["speak"]["send_key"] == "c"     # config.toml はプロファイルより強い
    assert cfg["speak"]["rate"] == "+20%"      # 操作の窓の値はもっと強い


def test_missing_profile_is_generic(root):
    write(root / "config.toml", 'profile = "nope"\n')
    cfg = config.load()
    assert cfg["game"] == "" and cfg["profile_info"]["setup"] == []


def test_list_profiles(root):
    write(root / "profiles" / "b.toml", 'name = "Beta"\n')
    write(root / "profiles" / "a.toml", 'name = "Alpha"\n')
    assert config.list_profiles() == [("a", "Alpha"), ("b", "Beta")]


def test_save_setting_top_and_remove(root):
    config.save_setting("", "profile", "x")
    config.save_setting("speak", "send_key", "v")
    config.save_setting("speak", "send_key", None)
    data = json.loads((root / "logs" / "settings.json").read_text(encoding="utf-8"))
    assert data == {"profile": "x", "speak": {}}


def test_state_merges(root):
    config.save_state({"log_x": 0.5})
    config.save_state({"guided": True})
    assert config.load_state() == {"log_x": 0.5, "guided": True}
    cfg = config.load()
    assert cfg["overlay"]["log_x"] == 0.5
    assert "guided" not in cfg["overlay"]


def test_secrets_roundtrip(root):
    write(root / ".env", "# memo\nOther=1\nClaudeAPIKey=old\n")
    assert config.api_key() == "old"
    config.save_api_key("sk-ant-new")
    config.save_secret("azure", "az")
    text = (root / ".env").read_text(encoding="utf-8")
    assert "# memo" in text and "Other=1" in text
    assert config.api_key() == "sk-ant-new"
    assert config.secret("azure") == "az"
    config.save_secret("azure", "")
    assert config.secret("azure") is None


def test_secret_env_var_wins(root, monkeypatch):
    write(root / ".env", "ClaudeAPIKey=file\n")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "envkey")
    assert config.api_key() == "envkey"


def test_glossary_layers(root):
    write(root / "profiles" / "g.toml", 'name = "G"\nglossary = "g.json"\n')
    write(root / "profiles" / "g.json", json.dumps({"Bastion": {"ja": "バスティオン"}, "Tick": "ティック", "ab": "短い"}))
    write(root / "extra.txt", "Tick = チック\nLeaper = リーパー\n# comment = no\n")
    write(root / "glossary.txt", "Leaper = 跳ぶ奴\n")
    write(root / "config.toml", f'profile = "g"\n[translate]\nglossary_json = "{(root / "extra.txt").as_posix()}"\n')
    g = dict(config.load_glossary(config.load()))
    assert g == {"Bastion": "バスティオン", "Tick": "チック", "Leaper": "跳ぶ奴"}


def test_pick_glossary_word_boundaries():
    g = [("Tick", "ティック"), ("Bastion", "バスティオン"), ("Raider Hatch", "レイダーハッチ")]
    hits = config.pick_glossary(g, "the raider hatch is open, a tick nearby")
    assert hits[0] == ("Raider Hatch", "レイダーハッチ")  # 長い語が先
    assert ("Tick", "ティック") in hits
    assert config.pick_glossary(g, "sticky situation") == []
    assert config.pick_glossary(g, "バスティオンがいる") == [("Bastion", "バスティオン")]
