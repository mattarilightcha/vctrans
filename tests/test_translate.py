from types import SimpleNamespace

import pytest

from vc import translate


class FakeClient:
    def __init__(self, reply):
        self.reply = reply
        self.calls = []
        self.messages = self

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason="tool_use",
                               content=[SimpleNamespace(type="tool_use", input=self.reply)])


CFG = {"model": "claude-haiku-5-5", "effort": "low", "thinking_off": True}


def make(game="ARC Raiders", reply=None, glossary=None, core=""):
    tr = translate.Translator(None, CFG, game, glossary or [], core_terms=core)
    tr.client = FakeClient(reply or {})
    return tr


def test_no_key():
    tr = translate.Translator(None, CFG, "G", [])
    with pytest.raises(translate.TranslateError):
        tr("hi", "ja", [])


def test_to_japanese_uses_tool_and_context():
    tr = make(reply={"en": "Bastion on the left", "ja": "左にバスティオン"},
              glossary=[("Bastion", "バスティオン")], core="Bastion, Tick")
    fixed, ja = tr("bass tin on the left", "ja", [("相手", "watch out")])
    assert (fixed, ja) == ("Bastion on the left", "左にバスティオン")
    kw = tr.client.calls[0]
    assert kw["tool_choice"] == {"type": "tool", "name": "vc_them"}
    assert "オンラインゲーム「ARC Raiders」" in kw["system"]
    assert "Bastion（バスティオン）" in kw["system"] and "Tick" in kw["system"]
    assert "相手: watch out" in kw["messages"][0]["content"]
    assert kw["thinking"] == {"type": "disabled"}


def test_to_english_without_game():
    tr = make(game="", reply={"ja": "了解", "en": "\"Got it\""})
    fixed, en = tr("りょうかい", "en", [], examples=[("おけ", "OK")])
    assert (fixed, en) == ("了解", "Got it")
    kw = tr.client.calls[0]
    assert "あなたはオンラインゲームのボイスチャット" in kw["system"]
    assert "おけ → OK" in kw["system"]


def test_set_game_switches_terms():
    tr = make(core="Bastion")
    tr.set_game("Other", [("Foo", "フー")], "Foo")
    assert tr.game == "Other" and tr.core == ["Foo（フー）"]


def test_split_en_fallback():
    assert translate._split_en("1. 敵が来た\n2. Enemy incoming", "てきがきた") == ("敵が来た", "Enemy incoming")
    assert translate._split_en("Enemy incoming", "元") == ("元", "Enemy incoming")
