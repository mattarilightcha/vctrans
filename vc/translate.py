"""Claude Haiku で訳す（相手の英語→日本語、自分の日本語→英語）。"""
import re

import anthropic

from .config import pick_glossary

_EN_JA = """あなたは{game}のボイスチャットの通訳です。ほかのプレイヤーの発言（音声認識で文字にしたもの）を日本語に訳します。
結果は vc_them で返す。
- en: 音声認識の聞き違いを直し、uh・um などの言いよどみを除いた英語。意味は変えず、直す所が無ければそのまま。
- ja: その日本語訳。前置き・説明・原文・引用符は付けない。
- ゲーム仲間の会話として自然で短い日本語にする。口調は相手に合わせ、罵り言葉やスラングも意味が伝わるように訳す。
- 音声認識の聞き違いらしい語は、直前の会話とゲームの内容から一番ありそうな意味で訳す。どうしても意味が取れない所は「（聞き取れず）」とする。
- 用語集に載っている語は、用語集の日本語を使う。"""

_JA_EN = """あなたは{game}のボイスチャットの通訳です。日本人プレイヤー（自分）の発言（音声認識で文字にしたもの）を、英語圏のプレイヤーに向けた英語に訳します。訳した英語はそのまま音声で読み上げます。
結果は vc_line で返す。
- ja: 音声認識の聞き違いを直し、「えーと」「あー」「あの」などの言いよどみを除いた日本語。意味は変えず、直す所が無ければそのまま。
- en: その英訳。ゲーム中の VC で自然な、短く口語的な英語。前置き・説明・引用符・絵文字は付けない。
- 聞き違いらしい語は、直前の会話・ゲームの内容・下の「この人の声でよくある聞き違い」から一番ありそうな語に直す。
- 用語集に載っている語は、用語集の英語を使う。"""


_VC_LINE = {
    "name": "vc_line",
    "description": "自分の発言の、直した日本語と英訳を返す。",
    "input_schema": {
        "type": "object",
        "properties": {
            "ja": {"type": "string", "description": "聞き違いと言いよどみを直した日本語"},
            "en": {"type": "string", "description": "読み上げる英訳"},
        },
        "required": ["ja", "en"],
    },
}


_VC_THEM = {
    "name": "vc_them",
    "description": "相手の発言の、直した英語と日本語訳を返す。",
    "input_schema": {
        "type": "object",
        "properties": {
            "en": {"type": "string", "description": "聞き違いと言いよどみを直した英語"},
            "ja": {"type": "string", "description": "日本語訳"},
        },
        "required": ["en", "ja"],
    },
}


class TranslateError(Exception):
    pass


class Translator:
    def __init__(self, key, cfg, game, glossary, core_terms=""):
        self.cfg = cfg
        self.set_game(game, glossary, core_terms)
        self.set_key(key)

    def set_game(self, game, glossary, core_terms=""):
        """ゲーム名・用語集・よく出る語（プロファイルを切り替えたときも）。"""
        self.game = game
        self.glossary = glossary
        # ゲームでよく出る語（発言に出ていなくても毎回渡す。音の似た聞き違いの直し先）
        terms = [t.strip() for t in (core_terms or "").split(",") if t.strip()]
        ja = {en.lower(): j for en, j in glossary}
        self.core = [f"{t}（{ja[t.lower()]}）" if t.lower() in ja else t for t in terms]

    def set_key(self, key):
        self.client = anthropic.Anthropic(api_key=key, max_retries=1, timeout=20.0) if key else None

    def __call__(self, text, to, context, examples=None):
        """to: "ja" か "en"。context: [(誰, 文)] 古い順。examples: [(認識, 正しい)]（自分の声の聞き違いの例）。
        戻り値: ja は (直した英語, 和訳)。en は (直した日本語, 英訳)。"""
        if not self.client:
            raise TranslateError("Claude の API キーが入っていない（操作の窓の「API キー」）")
        game = f"オンラインゲーム「{self.game}」" if self.game else "オンラインゲーム"
        system = (_EN_JA if to == "ja" else _JA_EN).format(game=game)
        terms = pick_glossary(self.glossary, text + "\n" + "\n".join(t for _, t in context))
        if terms:
            system += "\n\n用語集（英語 = 日本語）:\n" + "\n".join(f"{en} = {ja}" for en, ja in terms)
        if self.core:
            system += ("\n\nこのゲームでよく出る語（音の似た聞き違いはこの中の語に直す）:\n"
                       + "、".join(self.core))
        if to == "en" and examples:
            system += "\n\nこの人の声でよくある聞き違い（認識 → 正しい）:\n" + "\n".join(
                f"{a} → {c}" for a, c in examples)
        lines = []
        if context:
            lines.append("直前の会話（古い順）:")
            lines += [f"{who}: {t}" for who, t in context]
            lines.append("")
        lines += ["訳す発言:", text]
        kw = dict(model=self.cfg["model"], max_tokens=1024, system=system,
                  messages=[{"role": "user", "content": "\n".join(lines)}],
                  output_config={"effort": self.cfg.get("effort", "low")})
        if self.cfg.get("thinking_off"):
            kw["thinking"] = {"type": "disabled"}
        # 決まった入れ物で返させる（行で区切らせると形が崩れることがある）
        tool = _VC_LINE if to == "en" else _VC_THEM
        kw["tools"] = [tool]
        kw["tool_choice"] = {"type": "tool", "name": tool["name"]}
        try:
            r = self.client.messages.create(**kw)
        except anthropic.AuthenticationError:
            raise TranslateError("Claude の API キーが通らない（操作の窓の「API キー」で確かめる）")
        except anthropic.PermissionDeniedError as e:
            raise TranslateError(f"API の権限がない: {e.message}")
        except anthropic.RateLimitError:
            raise TranslateError("API の回数の上限。少し待って")
        except anthropic.APIStatusError as e:
            raise TranslateError(f"API エラー {e.status_code}: {e.message}")
        except anthropic.APIConnectionError:
            raise TranslateError("API につながらない")
        if r.stop_reason == "refusal":
            raise TranslateError("（訳を断られた）")
        for b in r.content:
            if b.type == "tool_use" and isinstance(b.input, dict):
                en, ja = (b.input.get("en") or "").strip(), (b.input.get("ja") or "").strip()
                if to == "en" and en:
                    return ja or text, en.strip("\"' ")
                if to == "ja" and ja:
                    return en or text, ja.strip("「」\"' ")
        out = "".join(b.text for b in r.content if b.type == "text").strip()
        if to == "ja":
            return text, out.strip("「」\"' ")
        return _split_en(out, text)


_HEAD = re.compile(r"^\s*(?:[12１２][\.．:：)）]|[12１２]\s*行目\s*[:：]?|日本語\s*[:：]|英訳\s*[:：]|English\s*:)\s*")
_JA = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def _split_en(out, original):
    """Haiku の 2 行 → (直した日本語, 英訳)。形が崩れていたら元の日本語と全体を英訳とみなす。"""
    lines = [_HEAD.sub("", l).strip().strip("「」\"' ") for l in out.splitlines()]
    lines = [l for l in lines if l]
    if len(lines) >= 2 and _JA.search(lines[0]) and not _JA.search(lines[-1]):
        return lines[0], " ".join(l for l in lines[1:] if not _JA.search(l))
    en = " ".join(l for l in lines if not _JA.search(l)) or out
    return original, en
