"""query.py の hook 契約・閾値フィルタ・dedupe のテスト。"""

import json

from myak.query import (
    filter_results,
    format_hook,
    matching_token_count,
    tokenize_query_terms,
)


def _make_result(score, session_id="sess0001", content="テスト内容"):
    return {
        "role": "user",
        "content": content,
        "timestamp": "2025-03-20",
        "session_id": session_id,
        "project": "test-project",
        "score": score,
    }


class TestTokenizeQueryTerms:
    def test_basic_split(self):
        tokens = tokenize_query_terms("認証エラーの修正方法")
        assert len(tokens) >= 1
        for t in tokens:
            assert len(t) >= 3

    def test_short_query_returns_empty(self):
        tokens = tokenize_query_terms("ab")
        assert tokens == []

    def test_deduplication(self):
        tokens = tokenize_query_terms("テスト テスト テスト 別の単語")
        assert len(tokens) == len(set(tokens))

    def test_max_five_tokens(self):
        tokens = tokenize_query_terms("aaa bbb ccc ddd eee fff ggg")
        assert len(tokens) <= 5


class TestMatchingTokenCount:
    def test_all_match(self):
        count = matching_token_count("認証エラーの修正方法", ["認証", "エラー"])
        assert count == 2

    def test_partial_match(self):
        count = matching_token_count("認証エラーの修正方法", ["認証", "データベース"])
        assert count == 1

    def test_no_match(self):
        count = matching_token_count("テスト内容", ["データベース", "パフォーマンス"])
        assert count == 0

    def test_case_insensitive(self):
        count = matching_token_count("Hello World test", ["hello", "TEST"])
        assert count == 2


class TestFilterResults:
    def test_empty_results(self):
        assert filter_results([], ["token"]) == []

    def test_absolute_threshold(self):
        results = [_make_result(0.01)]
        filtered = filter_results(results, [])
        assert len(filtered) == 0

    def test_relative_threshold(self):
        results = [_make_result(1.0), _make_result(0.2)]
        filtered = filter_results(results, [])
        assert len(filtered) == 1
        assert filtered[0]["score"] == 1.0

    def test_token_match_guard(self):
        results = [_make_result(1.0, content="aaa bbb ccc ddd")]
        tokens = ["xxx", "yyy", "zzz"]  # 3 tokens, none match
        filtered = filter_results(results, tokens)
        assert len(filtered) == 0

    def test_token_match_guard_skipped_for_few_tokens(self):
        results = [_make_result(1.0, content="aaa bbb")]
        tokens = ["xxx", "yyy"]  # 2 tokens < TOKEN_MATCH_GUARD, guard skipped
        filtered = filter_results(results, tokens)
        assert len(filtered) == 1

    def test_session_dedupe_in_hook_mode(self):
        results = [
            _make_result(1.0, session_id="aabbccdd"),
            _make_result(0.8, session_id="aabbccdd"),
            _make_result(0.6, session_id="eeffgghh"),
        ]
        filtered = filter_results(results, [], hook_mode=True)
        session_ids = [r["session_id"] for r in filtered]
        assert session_ids == ["aabbccdd", "eeffgghh"]

    def test_no_dedupe_without_hook_mode(self):
        results = [
            _make_result(1.0, session_id="aabbccdd"),
            _make_result(0.8, session_id="aabbccdd"),
        ]
        filtered = filter_results(results, [], hook_mode=False)
        assert len(filtered) == 2


class TestFormatHook:
    def test_empty_returns_empty(self):
        assert format_hook([], "query") == ""

    def test_output_uses_additional_context(self):
        results = [_make_result(1.0)]
        output = format_hook(results, "query")
        parsed = json.loads(output)
        assert "additionalContext" in parsed
        assert "system_message" not in parsed

    def test_output_contains_header(self):
        results = [_make_result(1.0)]
        output = format_hook(results, "query")
        parsed = json.loads(output)
        assert "関連する過去の記憶" in parsed["additionalContext"]

    def test_max_results_limited(self):
        results = [_make_result(1.0 - i * 0.1) for i in range(10)]
        output = format_hook(results, "query")
        parsed = json.loads(output)
        # HOOK_MAX_RESULTS = 3 なので最大3件分のコンテンツ
        content = parsed["additionalContext"]
        assert content.count("テスト内容") <= 3

    def test_snippet_truncation(self):
        long_content = "あ" * 500
        results = [_make_result(1.0, content=long_content)]
        output = format_hook(results, "query")
        parsed = json.loads(output)
        # HOOK_SNIPPET_CHARS = 220
        assert len(parsed["additionalContext"]) < 500
