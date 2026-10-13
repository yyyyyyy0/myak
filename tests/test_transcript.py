"""transcript.py (pure parsing) のテスト。"""

import json

import pytest

from myak import transcript
from myak.config import MAX_CONTENT_LENGTH
from myak.transcript import (
    clean_text,
    extract_segments,
    extract_text_content,
    iter_records,
    read_agent_meta,
    record_to_segment,
)

LONG = "x" * 60


def _rec(content, **kw):
    r = {"type": "user", "message": {"role": "user", "content": content},
         "uuid": "u", "timestamp": "t", "cwd": "/c"}
    r.update(kw)
    return r


class TestCleanText:
    def test_strips_noise_blocks_keeps_surrounding(self):
        t = "before <system-reminder>a\nb</system-reminder> after"
        assert clean_text(t) == "before  after"

    @pytest.mark.parametrize("tag", transcript.NOISE_TAGS)
    def test_each_noise_tag(self, tag):
        assert clean_text(f"keep<{tag}>drop\nme</{tag}>") == "keep"

    def test_case_sensitive(self):
        t = "<System-Reminder>x</System-Reminder>"
        assert clean_text(t) == t

    def test_control_chars_removed_but_newline_tab_kept(self):
        assert clean_text("a\x00b\x07c\n\td\x1b") == "abc\n\td"

    def test_collapses_blank_lines(self):
        assert clean_text("a\n\n\n\n\nb") == "a\n\nb"

    def test_strips(self):
        assert clean_text("  \n hi \n ") == "hi"


class TestCleanTextPerformance:
    @pytest.mark.parametrize("role", [None, "user", "assistant"])
    def test_many_unclosed_openers_is_linear(self, role):
        import time

        text = ("<system-reminder>" + "y" * 133) * 2000
        assert len(text) >= 300_000
        t0 = time.perf_counter()
        clean_text(text, role)
        assert time.perf_counter() - t0 < 0.2

    def test_many_closed_blocks(self):
        text = "k<system-reminder>z</system-reminder>" * 5000
        assert clean_text(text) == "k" * 5000


class TestExtractTextContent:
    def test_str(self):
        assert extract_text_content("  hi ") == "hi"

    def test_list_text_blocks_only(self):
        c = [{"type": "text", "text": " a "}, {"type": "tool_use"},
             {"type": "text", "text": ""}, "junk", {"type": "text", "text": "b"}]
        assert extract_text_content(c) == "a\nb"

    def test_other(self):
        assert extract_text_content(None) == ""
        assert extract_text_content(5) == ""

    def test_indexer_reexport(self):
        from myak import indexer
        assert indexer.extract_text_content is extract_text_content


class TestRecordToSegment:
    def test_basic(self):
        seg = record_to_segment(_rec(LONG, isSidechain=True, agentId="a1"))
        assert seg == {
            "uuid": "u", "role": "user", "content": LONG, "timestamp": "t",
            "is_sidechain": True, "agent_id": "a1", "cwd": "/c",
        }

    def test_defaults(self):
        seg = record_to_segment({"type": "assistant", "message": {"content": LONG}})
        assert seg["uuid"] is None
        assert seg["role"] == "assistant"
        assert seg["is_sidechain"] is False
        assert seg["agent_id"] is None
        assert seg["timestamp"] == ""

    @pytest.mark.parametrize("rec", [
        {"type": "system", "message": {"role": "user", "content": LONG}},
        _rec(LONG, isMeta=True),
        {"type": "user"},
        {"type": "user", "message": None},
        {"type": "user", "message": "str"},
        _rec([{"type": "tool_result", "content": LONG}]),
        _rec("short"),
        "not a dict",
    ])
    def test_none_cases(self, rec):
        assert record_to_segment(rec) is None

    @pytest.mark.parametrize("tag", transcript.DROP_TAGS)
    def test_drop_tags(self, tag):
        assert record_to_segment(_rec(f"<{tag}>{LONG}</{tag}>")) is None

    def test_drop_tag_only_when_leading(self):
        assert record_to_segment(_rec(f"  \n<command-name>/x</command-name>{LONG}")) is None
        seg = record_to_segment(_rec(f"{LONG} <command-name>/x</command-name>"))
        assert seg is not None

    def test_assistant_mentioning_drop_tag_is_kept(self):
        rec = {"type": "assistant", "uuid": "a", "timestamp": "t",
               "message": {"role": "assistant", "content": [
                   {"type": "text", "text": f"{LONG} the <command-name> tag is used"}]}}
        assert record_to_segment(rec) is not None
        rec["message"]["content"][0]["text"] = f"<command-name>{LONG}</command-name>"
        assert record_to_segment(rec) is not None

    def test_unclosed_noise_tag_user_drops_to_end(self):
        seg = record_to_segment(_rec(f"{LONG} <system-reminder>never closed {LONG}"))
        assert seg["content"] == LONG

    def test_unclosed_noise_tag_assistant_kept(self):
        assert clean_text("a <system-reminder>never closed", "assistant") == (
            "a <system-reminder>never closed")

    def test_cleaned_too_short(self):
        assert record_to_segment(_rec(f"<system-reminder>{LONG}</system-reminder>ok")) is None

    def test_truncated(self):
        seg = record_to_segment(_rec("y" * (MAX_CONTENT_LENGTH + 100)))
        assert len(seg["content"]) == MAX_CONTENT_LENGTH


class TestIterRecords:
    def _write(self, tmp_path, data: bytes):
        p = tmp_path / "s.jsonl"
        p.write_bytes(data)
        return p

    def test_offsets_and_partial_line(self, tmp_path):
        a, b = b'{"a":1}\n', b'{"b":2}\n'
        p = self._write(tmp_path, a + b + b'{"c":')
        out = list(iter_records(p))
        assert out == [({"a": 1}, len(a)), ({"b": 2}, len(a) + len(b))]

    def test_start_offset(self, tmp_path):
        a, b = b'{"a":1}\n', b'{"b":2}\n'
        p = self._write(tmp_path, a + b)
        assert list(iter_records(p, len(a))) == [({"b": 2}, len(a) + len(b))]

    def test_malformed_skipped_but_advanced(self, tmp_path):
        data = b'{bad\n{"a":1}\n\n'
        out = list(iter_records(self._write(tmp_path, data)))
        assert out == [({"a": 1}, 13)]

    def test_non_dict_and_blank_skipped(self, tmp_path):
        out = list(iter_records(self._write(tmp_path, b'[1]\n\n"x"\n{"a":1}\n')))
        assert [r for r, _ in out] == [{"a": 1}]

    def test_invalid_utf8_skipped(self, tmp_path):
        out = list(iter_records(self._write(tmp_path, b'\xff\xfe\n{"a":1}\n')))
        assert [r for r, _ in out] == [{"a": 1}]

    def test_crlf(self, tmp_path):
        out = list(iter_records(self._write(tmp_path, b'{"a":1}\r\n{"b":2}\r\n')))
        assert out == [({"a": 1}, 9), ({"b": 2}, 18)]

    def test_complete_final_line_without_newline_not_yielded(self, tmp_path):
        out = list(iter_records(self._write(tmp_path, b'{"a":1}\n{"b":2}')))
        assert out == [({"a": 1}, 8)]

    def test_offset_beyond_eof(self, tmp_path):
        assert list(iter_records(self._write(tmp_path, b'{"a":1}\n'), 999)) == []


class TestExtractSegmentsFixture:
    def test_golden_roles_and_uuids(self, fixtures_dir):
        segs, _ = extract_segments(fixtures_dir / "session_basic.jsonl")
        assert [(s["role"], s["uuid"]) for s in segs] == [
            ("user", "u1"),
            ("assistant", "a1"),
            ("user", "u4"),
            ("user", "u7"),
            ("assistant", None),
            ("assistant", "a2"),
        ]

    def test_cleaned_content(self, fixtures_dir):
        segs, _ = extract_segments(fixtures_dir / "session_basic.jsonl")
        by_uuid = {s["uuid"]: s for s in segs}
        assert by_uuid["u4"]["content"].startswith("Why does the scheduler")
        assert "system-reminder" not in by_uuid["u4"]["content"]
        u7 = by_uuid["u7"]["content"]
        assert "real question" in u7 and "keep the answer short" in u7
        assert "task-notification" not in u7 and "side-sessions-event" not in u7
        assert "tool_use" not in by_uuid["a1"]["content"]
        assert all(s["cwd"] == "/Users/x/proj" for s in segs)

    def test_offset_stops_before_truncated_line(self, fixtures_dir):
        path = fixtures_dir / "session_basic.jsonl"
        _, offset = extract_segments(path)
        raw = path.read_bytes()
        assert raw[offset:].startswith(b'{"parentUuid":null,"type":"assistant"')
        assert b"\n" not in raw[offset:]

    def test_incremental_resume(self, tmp_path, fixtures_dir):
        p = tmp_path / "s.jsonl"
        raw = (fixtures_dir / "session_basic.jsonl").read_bytes()
        p.write_bytes(raw)
        _, off = extract_segments(p)
        # Claude Code finishes writing the partial line
        tail = json.dumps(_rec(LONG, uuid="late")).encode() + b"\n"
        p.write_bytes(raw[:off] + tail)
        segs, off2 = extract_segments(p, off)
        assert [s["uuid"] for s in segs] == ["late"]
        assert off2 == off + len(tail)
        assert extract_segments(p, off2) == ([], off2)

    def test_sidechain(self, fixtures_dir):
        segs, _ = extract_segments(fixtures_dir / "session_sidechain.jsonl")
        assert len(segs) == 2
        assert all(s["is_sidechain"] and s["agent_id"] == "a1" for s in segs)

    def test_subagent_file(self, fixtures_dir):
        segs, _ = extract_segments(fixtures_dir / "subagents" / "agent-a1.jsonl")
        assert [s["uuid"] for s in segs] == ["g1", "g2", "g3"]


class TestRobustness:
    def test_non_str_text_block_ignored(self):
        assert extract_text_content([{"type": "text", "text": 5},
                                     {"type": "text", "text": ["a"]},
                                     {"type": "text", "text": " ok "}]) == "ok"

    def test_deeply_nested_json_does_not_crash(self, tmp_path):
        p = tmp_path / "s.jsonl"
        deep = "[" * 100000 + "]" * 100000
        good = json.dumps({"type": "user"})
        p.write_bytes((deep + "\n" + good + "\n").encode())
        recs = list(iter_records(p))
        assert len(recs) == 1 and recs[0][0]["type"] == "user"


class TestReadAgentMeta:
    def test_reads_sibling_meta(self, fixtures_dir):
        meta = read_agent_meta(fixtures_dir / "subagents" / "agent-a1.jsonl")
        assert meta["agent_type"] == "general-purpose"
        assert meta["description"] == "x"
        assert meta["name"] == "n"

    def test_missing(self, tmp_path):
        assert read_agent_meta(tmp_path / "agent-z.jsonl") == {"agent_type": "unknown"}

    @pytest.mark.parametrize("body", ["{bad", "[1]", '{"description":"x"}', '{"agentType":""}'])
    def test_invalid_or_untyped(self, tmp_path, body):
        (tmp_path / "agent-z.meta.json").write_text(body)
        assert read_agent_meta(tmp_path / "agent-z.jsonl")["agent_type"] == "unknown"


class TestFinalAndStartOffsets:
    def test_final_accepts_valid_unterminated_tail(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_bytes(b'{"a":1}\n{"b":2}')
        assert list(iter_records(p, 0, final=True)) == [({"a": 1}, 8), ({"b": 2}, 15)]
        assert list(iter_records(p, 0)) == [({"a": 1}, 8)]

    def test_final_rejects_invalid_unterminated_tail(self, tmp_path):
        p = tmp_path / "t.jsonl"
        p.write_bytes(b'{"a":1}\n{"b":')
        assert list(iter_records(p, 0, final=True)) == [({"a": 1}, 8)]

    def test_segments_carry_start_offset(self, tmp_path):
        import json

        a = json.dumps(_rec(LONG)).encode() + b"\n"
        p = tmp_path / "t.jsonl"
        p.write_bytes(a + a)
        segs, off = extract_segments(p)
        assert [s["start_offset"] for s in segs] == [0, len(a)]
        assert off == 2 * len(a)
