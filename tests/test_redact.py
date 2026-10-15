"""redact.py: シークレットの伏せ字化 (検出漏れ・誤検知・冪等性・取り込み経路)。

偽トークンは実行時に組み立てる (リポジトリにトークン形の文字列を置かない)。
失敗時もトークン本体を出さないよう、assert は件数と含有の真偽だけを比べる。
"""

import base64
import random
import string
import time

import pytest

from myak import redact as R
from myak.config import MAX_CONTENT_LENGTH
from myak.redact import redact, redact_with_counts
from myak.transcript import record_to_segment

ALNUM = string.ascii_letters + string.digits
B64URL = ALNUM + "-_"
UPPER_DIGITS = string.ascii_uppercase + string.digits


class Fake(str):
    """偽トークン。pytest の失敗表示 (repr) に値を出さない。"""

    def __repr__(self):
        return f"<fake secret len={len(self)}>"


def rand(n, alphabet=ALNUM, seed=0):
    """決定的な乱数文字列。英大文字・小文字・数字を必ず含める (alphabet が許す範囲で)。"""
    rng = random.Random(f"{seed}:{n}:{alphabet}")
    while True:
        s = "".join(rng.choice(alphabet) for _ in range(n))
        kinds = [str.isupper, str.islower, str.isdigit]
        if all(any(k(c) for c in s) for k in kinds if any(k(c) for c in alphabet)):
            return Fake(s)


def digits(n, seed=0):
    return rand(n, string.digits, seed)


def discord_token(user_id="123456789012345678", seed=0):
    head = base64.urlsafe_b64encode(user_id.encode()).rstrip(b"=").decode()
    return Fake(f"{head}.{rand(6, B64URL, seed)}.{rand(38, B64URL, seed)}")


def private_key():
    body = "\n".join(rand(64, ALNUM + "+/", i) for i in range(4))
    return Fake(
        "-----BEGIN OPENSSH PRIVATE KEY-----\n" + body + "\n-----END OPENSSH PRIVATE KEY-----"
    )


def escaped_private_key():
    """サービスアカウント JSON 等: 改行が \\n にエスケープされた PEM。"""
    body = "\\n".join(rand(64, ALNUM + "+/", i) for i in range(4))
    return Fake("-----BEGIN PRIVATE KEY-----\\n" + body + "\\n-----END PRIVATE KEY-----")


def quoted_private_key():
    """Markdown の引用 (> ) に貼られた PEM。"""
    body = "\n> ".join(rand(64, ALNUM + "+/", i) for i in range(3))
    return Fake("-----BEGIN RSA PRIVATE KEY-----\n> " + body + "\n> -----END RSA PRIVATE KEY-----")


def jwt():
    return Fake(f"eyJ{rand(20, B64URL)}.eyJ{rand(40, B64URL, 1)}.{rand(43, B64URL, 2)}")


def basic(user="admin"):
    return Fake(base64.b64encode(f"{user}:{rand(16)}".encode()).decode())


# (期待する type, シークレット本体, それを含む文脈)
POSITIVE = [
    ("anthropic_api_key", Fake("sk-ant-api03-" + rand(80, B64URL)), "export ANTHROPIC_API_KEY={}"),
    ("openai_api_key", Fake("sk-proj-" + rand(60, B64URL)), "key is {} ok"),
    ("openai_api_key", Fake("sk-" + rand(48)), "OPENAI_API_KEY={}"),
    ("github_token", Fake("ghp_" + rand(36)), "git clone https://{}@github.com/x/y"),
    ("github_token", Fake("github_pat_" + rand(22) + "_" + rand(59, seed=1)), "GH_TOKEN={}"),
    ("aws_access_key_id", Fake("AKIA" + rand(16, UPPER_DIGITS)), "aws_access_key_id = {}"),
    ("slack_token", Fake(f"xoxb-{digits(12)}-{digits(13, 1)}-{rand(24)}"), "SLACK={}"),
    (
        "slack_webhook",
        Fake(
            f"https://hooks.slack.com/services/T{rand(8, UPPER_DIGITS)}"
            f"/B{rand(8, UPPER_DIGITS, 1)}/{rand(24)}"
        ),
        "curl -X POST {} -d '{{}}'",
    ),
    ("google_api_key", Fake("AIza" + rand(35, B64URL)), "maps?key={}&q=tokyo"),
    ("discord_bot_token", discord_token(), "Discord ボットのトークンは{}です"),
    ("discord_bot_token", discord_token("9876543210987654321", 2), "DISCORD_BOT_TOKEN={}"),
    (
        "discord_webhook",
        Fake(f"https://discord.com/api/webhooks/{digits(18)}/{rand(68, B64URL)}"),
        "webhook: {}",
    ),
    ("stripe_key", Fake("sk_live_" + rand(24)), "stripe.api_key = '{}'"),
    ("private_key", private_key(), "鍵はこれ:\n{}\nです"),
    ("bearer_token", rand(40), "curl -H 'Authorization: Bearer {}' https://api.example.com"),
    ("generic_secret", rand(24), 'DB_PASSWORD="{}"'),
    ("generic_secret", rand(32, "0123456789abcdef"), "api_key: {}"),
    ("generic_secret", rand(40, B64URL), '{{"client_secret": "{}", "id": 1}}'),
    ("generic_secret", rand(30), "https://example.com/cb?access_token={}&state=x"),
    (
        "generic_secret",
        rand(28),
        "VERY_LONG_ENVIRONMENT_VARIABLE_PREFIX_FOR_SERVICE_API_KEY={}",
    ),
    ("private_key", escaped_private_key(), '{{"type": "service_account", "private_key": "{}\\n"}}'),
    ("private_key", quoted_private_key(), "貼ってもらった鍵:\n> {}\n以上"),
    ("jwt", jwt(), "SUPABASE_SERVICE_ROLE_KEY={}"),
    ("url_credentials", rand(20), "DATABASE_URL=postgresql://app:{}@db.example.com:5432/app"),
    ("url_credentials", rand(20, seed=1), "redis://:{}@cache:6379/0"),
    ("basic_auth", basic(), "curl -H 'Authorization: Basic {}' https://api.example.com"),
    ("cli_secret", rand(20), "curl -u admin:{} https://api.example.com/v1"),
    ("cli_secret", rand(18), "mysql -u root -p{} app_db"),
    ("cli_secret", rand(24), "deploy --api-key {} --region tokyo"),
    ("bearer_token", rand(32), "gh-tool login --token {}"),
    ("api_token", Fake("hf_" + rand(34, string.ascii_letters)), "HF_TOKEN={}"),
    ("api_token", Fake("sk-or-v1-" + rand(64, "0123456789abcdef")), "OPENROUTER_KEY={}"),
    ("api_token", Fake("glpat-" + rand(20, B64URL)), "export GITLAB={}"),
    ("api_token", Fake("npm_" + rand(36)), "//registry.npmjs.org/:_authToken={}"),
    ("telegram_bot_token", Fake(digits(10) + ":AA" + rand(33, B64URL)), "TELEGRAM={}"),
    (
        "discord_webhook",
        Fake(f"https://discord.com/api/v10/webhooks/{digits(18)}/{rand(68, B64URL)}"),
        "通知先 {}",
    ),
    ("discord_bot_token", discord_token("12345678901234567", 3), "{}"),
    ("discord_bot_token", discord_token(seed=4), "client.run('Bot_{}')"),
    ("generic_secret", rand(20), "パスワードは{}です"),
    ("generic_secret", rand(24), "API_KEY：{}"),
    ("generic_secret", rand(22), "the password is {} for now"),
    ("generic_secret", Fake("Ab3!@#Cd5$%^Ef7*gh9"), "DB_PASSWORD={}"),
    ("generic_secret", rand(40), "SERVICE_ROLE_KEY={}"),
]

# 伏せ字にしてはいけない (現実の会話・コードに出る) テキスト
NEGATIVE = [
    "pip install sk-learn-compatible-transformer-pipeline-example-package",
    "sk-proj-management-system-for-large-engineering-teams を参照",
    "max_tokens=4096 and total_tokens: 15000000",
    "tokenize='trigram'",
    "password = os.environ['DB_PASSWORD']",
    "api_key: ${{ secrets.OPENAI_API_KEY }}",
    "token: my_session_token_value_2024",
    "password=getPasswordFromVault2",
    "auth_token_path=/Users/nil/.config/token_file_2024.json",
    "secret_key = settings.SECRET_KEY_FALLBACK_V2",
    "the secret: TheQuickBrownFoxJumpsOverTheLazyDog",
    "commit 595f4c9a1b2c3d4e5f60718293a4b5c6d7e8f901 fixes the token refresh",
    "session 9e2d1aa4-6e68-4c92-aafe-8deca8e7756d",
    "ModuleNameThatIsVeryLongXY.helper.some_really_long_function_name_here_ok",
    "Bearer authentication is configured in the middleware",
    "Authorization: Bearer ${TOKEN}",
    "xoxb-your-bot-token-here",
    "AWS_ACCESS_KEY_ID=$AWS_ACCESS_KEY_ID",
    "https://example.com/oauth?state=abc&token_type=bearer",
    "password_hash: $2b$12$abcdefghijklmnopqrstuv",
    "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk",
    "Discord のボットトークンはローテーション済み。パスワードは 1Password に保存。",
    "-----BEGIN PUBLIC KEY-----\nMFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAE\n-----END PUBLIC KEY-----",
    'if line.startswith("-----BEGIN RSA PRIVATE KEY-----"):',
    "-----BEGIN PRIVATE KEY----- の後に base64 の本体が続き -----END PRIVATE KEY----- で終わる",
    "[REDACTED:discord_bot_token] token=[REDACTED:generic_secret]",
    "token = base64.b64decode(data)",
    "credentials = base64.b64encode(raw_bytes)",
    "private_key: PKCS7PrivateKeyTypes",
    'tokenizer="gpt-4o-mini-2024-07-18"',
    "token: k8s-service-account-token-secret",
    "ghp_" + "x" * 36,
    "deploy --password-file /etc/app/password.txt",
    "DATABASE_URL=postgres://app:${DB_PASS}@db:5432/app",
    "postgresql://app:<password>@localhost/app",
    "basic authentication is enabled for the admin panel",
    "mysql -u root -p app_db",
    'curl -u "$USER:$PASS" https://api.example.com',
    "token_expires_at=2024-10-15T12:00:00Z",
    "see https://example.com:8080/path?x=1 and git@github.com:org/repo.git",
    "トークンはローテーション済みです",
    "use --token $GITHUB_TOKEN when calling the API",
    "Authorization: Basic abcdefghi",
    "secret_id: 550e8400-e29b-41d4-a716-446655440000",
    "session_token: 9e2d1aa4-6e68-4c92-aafe-8deca8e7756d",
    "private_key: Ed25519PrivateKey | Ed448PrivateKey",
    "private_key = Ed25519PrivateKey.from_private_bytes(d)",
    "MLKEM768X25519PrivateKey = rust_openssl.hpke.MLKEM768X25519PrivateKey",
    "e.g. `pass` in `https://user:pass@host:port/path?query#fragment`",
    "MyDatabaseModel(db='postgres://user:VALUE@localhost:5432/foobar')",
    "The word password: Passw0rdManagerIntegration2024",
    "mhu = MultiHostUrl('https://foo.com:123@bar.com/path')",
]


@pytest.mark.parametrize(
    ("kind", "secret", "context"),
    POSITIVE,
    ids=[f"{k}-{i}" for i, (k, _, _) in enumerate(POSITIVE)],
)
def test_detects_and_replaces(kind, secret, context):
    out, counts = redact_with_counts(context.format(secret))
    assert (secret in out) is False
    assert dict(counts) == {kind: 1}
    assert out == context.format(f"[REDACTED:{kind}]")  # 周囲の文脈はそのまま残る


@pytest.mark.parametrize("text", NEGATIVE)
def test_leaves_benign_text_alone(text):
    out, counts = redact_with_counts(text)
    assert out == text
    assert not counts


def test_generic_keeps_key_name_and_quotes():
    out = redact(f'DB_PASSWORD="{rand(24)}"')
    assert out == 'DB_PASSWORD="[REDACTED:generic_secret]"'


def test_multiple_secrets_and_idempotent():
    text = "\n".join(c.format(s) for _, s, c in POSITIVE)
    once, counts = redact_with_counts(text)
    assert sum(counts.values()) == len(POSITIVE)
    assert not any(s in once for _, s, _ in POSITIVE)
    again, counts2 = redact_with_counts(once)
    assert again == once and not counts2


def test_truncated_private_key_is_still_redacted():
    head = private_key().split("-----END")[0]
    out, counts = redact_with_counts("pasted: " + head)
    assert counts == {"private_key": 1}
    assert out == "pasted: [REDACTED:private_key]"


def test_discord_shape_requires_numeric_user_id():
    tok = discord_token()
    fake_head = "M" + rand(23, ALNUM, 5)  # base64 だが数字列にならない
    assert redact_with_counts(fake_head + tok[tok.index("."):])[1] == {}
    assert not R._discord_user_id("M" + rand(24, ALNUM, 6))  # base64 として長さが不正
    assert R._discord_user_id(tok)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (rand(24), True),
        (rand(32, "0123456789abcdef"), True),
        ("short1A", False),
        ("/Users/nil/a1b2c3d4e5f6g7h8", False),
        ("aaaaaaaaaaaaaaaa1111", False),
        ("my_secret_value_2024", False),
        ("NoDigitsButMixedCaseLetters", False),
    ],
)
def test_looks_random(value, expected):
    assert R.looks_random(value) is expected


@pytest.mark.parametrize(
    "text",
    [
        "api_key=Xy" + digits(40) + "z",
        "token=Aa" + rand(60, string.ascii_uppercase) + "1x",
        "Bearer ab" + digits(40) + "c",
        "secret=" + ("Ab1" * 400),
        "password=" + "aB" * 2000 + "1",
    ],
    ids=["digits", "capitals", "bearer", "camel-ish", "long"],
)
def test_identifier_check_has_no_catastrophic_backtracking(text):
    start = time.perf_counter()
    redact(text)
    assert time.perf_counter() - start < 0.5


def test_linear_time_on_adversarial_input():
    blobs = [
        "a" * 200_000,
        "token=" * 30_000,
        "-----BEGIN PRIVATE KEY-----" * 5_000,
        "sk-" + "A1" * 100_000,
        ("M" + "A" * 26 + ".") * 20_000,
        "x_" * 100_000 + "token",
    ]
    start = time.perf_counter()
    for b in blobs:
        redact(b)
    assert time.perf_counter() - start < 5.0


def _rec(text, role="user"):
    return {"type": role, "uuid": "u1", "timestamp": "t",
            "message": {"role": role, "content": text}}


def test_record_to_segment_redacts_before_storing():
    tok = discord_token()
    text = f"このボットのトークン {tok} を .env に書いて起動してください。お願いします"
    seg = record_to_segment(_rec(text))
    assert seg is not None
    assert (tok in seg["content"]) is False
    assert "[REDACTED:discord_bot_token]" in seg["content"]


def test_secret_straddling_truncation_point_is_not_partially_kept():
    secret = Fake("ghp_" + rand(36))
    text = "x" * (MAX_CONTENT_LENGTH - 10) + " " + secret + " tail"
    seg = record_to_segment(_rec(text, "assistant"))
    assert len(seg["content"]) == MAX_CONTENT_LENGTH
    assert (secret[:9] in seg["content"]) is False


def test_save_memory_redacts_and_dedupes(db_env):
    import sqlite3

    from myak import mcp_server

    db_env.memory_dir.mkdir(parents=True, exist_ok=True)
    secret = Fake("sk-ant-api03-" + rand(80, B64URL))
    text = f"remember: the anthropic key for staging is {secret}"
    assert mcp_server.save_memory(text, session_id="desktop-r")["saved"]
    again = mcp_server.save_memory(text, session_id="desktop-r")
    assert again.get("duplicate") is True
    c = sqlite3.connect(str(db_env.db_path))
    try:
        rows = [r[0] for r in c.execute("SELECT content FROM segments")]
    finally:
        c.close()
    assert len(rows) == 1
    assert (secret in rows[0]) is False and "[REDACTED:anthropic_api_key]" in rows[0]


def test_indexer_stores_redacted_text_end_to_end(db_env, tmp_path, monkeypatch):
    """transcript → index_transcript_tree → DB/FTS まで平文が届かない (サブエージェント含む)。"""
    from myak import indexer
    from tests.idx_helpers import rec, setup_env, write

    env = setup_env(db_env, tmp_path, monkeypatch)
    bot, gh = discord_token(), Fake("ghp_" + rand(36))
    main = write(env.tmp / "s1.jsonl", [
        rec("u1", "user", f"起動用の DISCORD_BOT_TOKEN={bot} を設定しました。確認をお願いします"),
    ])
    write(env.tmp / "s1" / "subagents" / "agent-a1.jsonl", [
        rec("a1", "assistant", f"remote を https://{gh}@github.com/x/y に設定し push しました"),
    ])
    try:
        assert indexer.index_transcript_tree(env.conn, main, "proj") == 2
        rows = [r[0] for r in env.conn.execute("SELECT content FROM segments ORDER BY id")]
        hits = env.conn.execute(
            "SELECT COUNT(*) FROM segments_fts WHERE segments_fts MATCH ?", (f'"{bot[:12]}"',)
        ).fetchone()[0]
    finally:
        env.conn.close()
    assert not any(bot in r or gh in r for r in rows)
    assert "[REDACTED:discord_bot_token]" in rows[0] and "[REDACTED:github_token]" in rows[1]
    assert hits == 0
