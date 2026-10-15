"""会話テキスト中のシークレット (API キー・トークン等) を伏せ字にする。DB には触れない。

取り込み時 (transcript.record_to_segment / mcp_server.save_memory) と、既存 DB への
遡及処理 (`myak redact-secrets`) で共有する。過去文脈は他セッションのプロンプトへ
注入されるため、ここで落とさないと横展開する。

方針: 形式が固有のもの (プレフィックス・長さ・構造) は形で判定し、汎用の key=value と
Bearer は値の見た目 (長さ・文字種・エントロピー・識別子らしさ) で判定して誤検知を抑える。
置換後は ``[REDACTED:<type>]`` になり、再適用しても変化しない (冪等)。
正規表現はすべて入力長に対して線形に収まるよう書く (取り込みはフックの同期経路で走る)。
"""

import base64
import binascii
import math
import re
from collections import Counter
from dataclasses import dataclass
from typing import Callable

PLACEHOLDER = "[REDACTED:{}]"

# 英数字・-・_ の連続の途中から始まらない/終わらないための境界
_B = r"(?<![A-Za-z0-9_-])"
_E = r"(?![A-Za-z0-9_-])"

GENERIC_MIN_LENGTH = 16
GENERIC_MIN_ENTROPY = 3.0
# この長さ以下で大文字/小文字が揃った英数字チャンクは語とみなす (b64decode, 4o, k8s 等)
SHORT_WORD_MAX = 12

_CHUNK_SEPARATORS = re.compile(r"[_.\-/+=~]+")
# チャンクを語に分ける: 数字付き大文字 (V2, X25519)・頭字語 (HTTP)・(先頭大文字の) 小文字語・数字
_WORDS = re.compile(r"[A-Z]\d{1,6}(?!\d)|[A-Z]+(?![a-z])|[A-Z]?[a-z]+|\d+")


def _entropy(value):
    """1 文字あたりの Shannon エントロピー (bit)。"""
    counts = Counter(value)
    n = len(value)
    return -sum(c / n * math.log2(c / n) for c in counts.values())


def _is_identifier_chunk(chunk):
    if (
        len(chunk) <= SHORT_WORD_MAX
        and chunk.isascii()
        and chunk.isalnum()
        and chunk in (chunk.lower(), chunk.upper())
    ):
        return True
    words = _WORDS.findall(chunk)
    if "".join(words) != chunk:
        return False
    numbers = [w for w in words if w.isdigit()]
    # 型名の数字 (Ed25519, MLKEM768X25519, PKCS7) は許す。数字だらけなら乱数とみなす
    if len(numbers) > 3 or any(len(w) > 6 for w in numbers) or sum(map(len, numbers)) > 8:
        return chunk.isdigit()
    # 乱数列は 1 文字の語 (aB3 → a, B, 3) が混ざる。識別子 (camelCase 等) の語は 2 文字以上
    return all(len(w) >= 2 for w in words if not w.isdigit())


def _is_identifier_like(value):
    chunks = [c for c in _CHUNK_SEPARATORS.split(value) if c]
    return all(_is_identifier_chunk(c) for c in chunks)


def looks_random(value, min_length=GENERIC_MIN_LENGTH):
    """値が乱数由来のシークレットらしいか (汎用 key=value / Bearer 等の文脈依存の規則用)。

    英字と数字の両方を含み、十分長く、エントロピーが高く、識別子・パス・URL・変数参照の
    形でないこと。
    """
    if len(value) < min_length or value[0] in "/~.$%" or "://" in value:
        return False
    if not any(c.isdigit() for c in value) or not any(c.isalpha() for c in value):
        return False
    if _entropy(value) < GENERIC_MIN_ENTROPY:
        return False
    return not _is_identifier_like(value)


def _not_placeholder(value):
    """`ghp_xxxx…` のような説明用のダミーを除く。"""
    return _entropy(value) >= GENERIC_MIN_ENTROPY


def _mixed_case_and_digit(value):
    return (
        any(c.isdigit() for c in value)
        and any(c.islower() for c in value)
        and any(c.isupper() for c in value)
    )


def _not_reference(value):
    """`$PASS`・`<password>`・`{{ x }}`・`****`・説明用の `pass` / `VALUE` 等を除く。

    英字だけ・数字だけの短い値 (8 文字以下) は例示か開発用の既定値とみなす。
    """
    if value[0] in "$<{*%[" or len(set(value)) == 1:
        return False
    return not ((value.isalpha() or value.isdigit()) and len(value) <= 8)


def _discord_user_id(value):
    """Discord ボットトークンの第 1 部は bot のユーザー ID (数字) の base64。"""
    head = value.split(".", 1)[0]
    try:
        decoded = base64.urlsafe_b64decode(head + "=" * (-len(head) % 4))
    except (binascii.Error, ValueError):
        return False
    return 17 <= len(decoded) <= 20 and decoded.isdigit()


def _basic_credentials(value):
    """Basic 認証の値は `user:password` の base64。"""
    try:
        decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
    except (binascii.Error, ValueError):
        return False
    return b":" in decoded and decoded.isascii() and decoded.decode("ascii").isprintable()


_KEY_BODY_LINE = re.compile(r"[A-Za-z0-9+/]{40,}")


def _has_key_body(block):
    """ヘッダだけの言及 (PEM を扱うコード等) は対象外。鍵本体の行があるときだけ伏せる。"""
    return _KEY_BODY_LINE.search(block) is not None


@dataclass(frozen=True)
class Rule:
    name: str
    pattern: re.Pattern
    group: int = 0  # 置換するグループ (0 = マッチ全体、-1 = 一致した最初のグループ)
    check: Callable[[str], bool] | None = None  # 置換対象の文字列への追加判定


# PEM 本体: base64・空白・ヘッダ行 (Proc-Type: 4,ENCRYPTED)・引用/表の記号・JSON の \n
_PEM_BODY = r"(?:[A-Za-z0-9+/=\s:,>|\-]|\\[nrt])"
_PEM_LABEL = r"(?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----"
# 汎用 key=value の値: ASCII の記号を含めてよいが、区切り (空白・引用符・括弧・& | : 等) で止める
_VALUE = r"([A-Za-z0-9_\-+/=.~!@#$%^*?]{16,})"
_QUOTE = r"[\"'`]?"
_JA_KEYS = r"(?:パスワード|トークン|シークレット|API ?キー|アクセスキー|秘密鍵)"

# 固有形式 → 汎用 の順に適用する (先に置換されたものは後段に掛からない)
RULES = (
    Rule(
        "private_key",
        re.compile(
            r"-----BEGIN " + _PEM_LABEL + _PEM_BODY + r"*?"
            r"(?:-----END " + _PEM_LABEL + r"|(?!" + _PEM_BODY + r"))"
        ),
        check=_has_key_body,
    ),
    Rule(
        "anthropic_api_key",
        re.compile(_B + r"sk-ant-[A-Za-z0-9_-]{20,}"),
        check=_mixed_case_and_digit,
    ),
    Rule(
        "api_token",
        re.compile(
            _B
            + r"(?:sk-or-v1-[0-9a-f]{32,}|hf_[A-Za-z0-9]{30,}|npm_[A-Za-z0-9]{36,}"
            r"|glpat-[A-Za-z0-9_-]{20,}|SG\.[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"
            r"|gsk_[A-Za-z0-9]{40,}|xai-[A-Za-z0-9]{40,}|ya29\.[A-Za-z0-9_-]{20,}"
            r"|GOCSPX-[A-Za-z0-9_-]{20,}|whsec_[A-Za-z0-9+/=]{24,}|pypi-[A-Za-z0-9_-]{50,}"
            r"|dop_v1_[0-9a-f]{64}|sbp_[0-9a-f]{40}|xapp-\d-[A-Z0-9]+-\d+-[0-9a-f]{20,}"
            r"|AGE-SECRET-KEY-1[0-9A-Z]{50,})"
            + _E
        ),
        check=_not_placeholder,
    ),
    Rule(
        "openai_api_key",
        re.compile(
            _B + r"sk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{32,}" + _E + ")"
        ),
        check=_mixed_case_and_digit,
    ),
    Rule(
        "github_token",
        re.compile(
            _B + r"(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{50,})" + _E
        ),
        check=_not_placeholder,
    ),
    Rule("aws_access_key_id", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])")),
    Rule(
        "slack_webhook",
        re.compile(r"https://hooks\.slack\.com/services/T[A-Z0-9]+/B[A-Z0-9]+/[A-Za-z0-9]+"),
    ),
    Rule(
        "slack_token",
        re.compile(_B + r"xox[abposr]-[A-Za-z0-9-]{10,}"),
        check=lambda v: any(c.isdigit() for c in v),
    ),
    Rule("google_api_key", re.compile(_B + r"AIza[0-9A-Za-z_-]{35}" + _E)),
    Rule(
        "discord_webhook",
        re.compile(
            r"https://(?:ptb\.|canary\.)?discord(?:app)?\.com/api/(?:v\d+/)?webhooks/\d+/"
            r"[A-Za-z0-9_-]{50,}"
        ),
    ),
    Rule(
        "discord_bot_token",
        re.compile(
            r"(?<![A-Za-z0-9])[MNO][A-Za-z0-9_-]{22,27}\.[A-Za-z0-9_-]{6}\.[A-Za-z0-9_-]{27,40}"
            + _E
        ),
        check=_discord_user_id,
    ),
    Rule(
        "telegram_bot_token",
        re.compile(r"(?<![A-Za-z0-9])\d{8,10}:AA[A-Za-z0-9_-]{33}" + _E),
    ),
    Rule("stripe_key", re.compile(_B + r"[sr]k_(?:live|test)_[0-9A-Za-z]{20,}" + _E)),
    Rule(
        "jwt",
        re.compile(_B + r"eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}" + _E),
    ),
    Rule(
        "url_credentials",
        re.compile(
            r"\b[A-Za-z][A-Za-z0-9+.-]{1,20}://[^\s:/@'\"\[\]]{0,128}:([^\s@/'\"\[\]]{1,256})@"
        ),
        group=1,
        check=_not_reference,
    ),
    Rule(
        "basic_auth",
        re.compile(r"(?i)\bbasic[ \t]+([A-Za-z0-9+/]{8,}={0,2})(?![A-Za-z0-9+/=])"),
        group=1,
        check=_basic_credentials,
    ),
    Rule(
        "cli_secret",
        re.compile(
            r"\bcurl\b[^\n]{0,200}?[ \t](?:-u|--user)[ \t]+[\"']?[^\s:\"']{1,128}:"
            r"([^\s\"']{1,256})"
            r"|\b(?:mysql|mysqldump|mariadb)\b[^\n]{0,200}?[ \t]-p([^\s\"']{1,256})"
        ),
        group=-1,
        check=_not_reference,
    ),
    Rule(
        "cli_secret",
        re.compile(
            r"(?<![A-Za-z0-9_-])--[a-z0-9-]{0,32}?"
            r"(?:password|passwd|secret|api-?key|access-?key)[a-z0-9-]{0,32}?"
            r"(?:=|[ \t]+)[\"']?" + _VALUE
        ),
        group=1,
        check=looks_random,
    ),
    Rule(
        "bearer_token",
        re.compile(r"(?i)\b(?:bearer|token)[ \t]+([A-Za-z0-9._~+/-]{20,}=*)"),
        group=1,
        check=looks_random,
    ),
    Rule(
        "generic_secret",
        re.compile(
            r"(?i)(?<![A-Za-z0-9_.-])"
            r"[A-Za-z0-9_.-]{0,64}?"
            r"(?:api[_-]?key|secret|token|passw(?:or)?d|pwd|access[_-]?key|private[_-]?key"
            r"|(?:encryption|signing|service[_-]?role|master|session|app)[_-]?key"
            r"|credential)s?"
            r"[A-Za-z0-9_.-]{0,32}?"
            r"(?:" + _QUOTE + r"[ \t]*(?:=>|:=|[:=：])[ \t]*|[ \t]+(?:is|was)[ \t]+)"
            + _QUOTE + _VALUE
        ),
        group=1,
        check=looks_random,
    ),
    Rule(
        "generic_secret",
        re.compile(_JA_KEYS + r"[ \t]*(?:[:=：]|は|が|を)?[ \t]*[\"'`「]?" + _VALUE),
        group=1,
        check=looks_random,
    ),
)


def _target_group(m, rule):
    if rule.group >= 0:
        return rule.group
    return next(i for i in range(1, m.re.groups + 1) if m.group(i) is not None)


def redact_with_counts(text):
    """(伏せ字化したテキスト, Counter[rule 名 → 件数]) を返す。入力は変更しない。"""
    counts = Counter()
    for rule in RULES:

        def _sub(m, rule=rule):
            group = _target_group(m, rule)
            target = m.group(group)
            if rule.check is not None and not rule.check(target):
                return m.group(0)
            counts[rule.name] += 1
            placeholder = PLACEHOLDER.format(rule.name)
            if group == 0:
                return placeholder
            start, end = m.span(group)
            whole = m.group(0)
            offset = m.start(0)
            return whole[: start - offset] + placeholder + whole[end - offset:]

        text = rule.pattern.sub(_sub, text)
    return text, counts


def redact(text):
    """シークレットを ``[REDACTED:<type>]`` に置換したテキストを返す。"""
    return redact_with_counts(text)[0]
