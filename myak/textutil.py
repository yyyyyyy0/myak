"""プロンプトに差し込むテキストの無害化ヘルパー (query / recall / cleanup で共有)。"""

import re

# 改行 (\n) 以外の制御文字、行区切り、bidi 制御、ゼロ幅文字、Unicode タグ文字
_CONTROL_CHARS = re.compile(
    "[\x00-\x09\x0b-\x1f\x7f-\x9f"
    "  ‪-‮⁦-⁩​-‏"
    "\U000e0000-\U000e007f]"
)
_NEWLINES = re.compile(r"\n+")


def sanitize(text):
    """改行以外の制御文字・bidi・タグ文字を除去する（過去ログは信頼できない入力）。"""
    return _CONTROL_CHARS.sub("", str(text))


def escape(text):
    """タグ/属性を閉じられないよう < > " を実体参照にする。"""
    return text.replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def flat(text):
    """改行を畳み、制御文字を除去し、エスケープする。"""
    return escape(_NEWLINES.sub(" ", sanitize(text)))
