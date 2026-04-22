"""Tests for Phase 5 ambiguous-dedup queue formatter and parser."""
from agents.tg_transfer.ambiguous import (
    format_ambiguous_summary, parse_ambiguous_reply,
)


def test_format_empty():
    assert format_ambiguous_summary([]) == ""


def test_format_single_row_single_candidate():
    rows = [{
        "source_msg_id": 101,
        "candidate_target_msg_ids": [501],
    }]
    text = format_ambiguous_summary(rows)
    assert "[1]" in text
    assert "#101" in text
    assert "a)" in text
    assert "#501" in text
    # Help-text reminds user about the grammar.
    assert "same" in text
    assert "skip" in text


def test_format_multiple_candidates_lettered():
    rows = [{
        "source_msg_id": 200,
        "candidate_target_msg_ids": [600, 601, 602],
    }]
    text = format_ambiguous_summary(rows)
    assert "a)" in text and "b)" in text and "c)" in text
    assert "d)" not in text


def test_format_caps_candidates_at_26():
    """More than 26 candidates would overflow a-z. We cap rather than moving
    to 'aa/ab/...' which would break the single-letter parser."""
    rows = [{
        "source_msg_id": 1,
        "candidate_target_msg_ids": list(range(1000, 1030)),
    }]
    text = format_ambiguous_summary(rows)
    assert "z)" in text
    # Beyond z should not appear; we truncated to 26.
    assert text.count(")") == 26


def test_parse_skip_variants():
    assert parse_ambiguous_reply("skip") == "skip"
    assert parse_ambiguous_reply("SKIP") == "skip"
    assert parse_ambiguous_reply("略過") == "skip"
    assert parse_ambiguous_reply("跳過") == "skip"
    assert parse_ambiguous_reply("  skip  ") == "skip"


def test_parse_same_single():
    assert parse_ambiguous_reply("same 1a") == {1: "a"}
    assert parse_ambiguous_reply("相同 1a") == {1: "a"}


def test_parse_same_multiple():
    assert parse_ambiguous_reply("same 1a, 2b") == {1: "a", 2: "b"}
    assert parse_ambiguous_reply("same 1a 2b 3c") == {1: "a", 2: "b", 3: "c"}


def test_parse_bare_tokens_accepted():
    """Dropping the 'same' prefix still works — the digit+letter pattern is
    unambiguous on its own."""
    assert parse_ambiguous_reply("1a, 2b") == {1: "a", 2: "b"}


def test_parse_case_insensitive_letters():
    assert parse_ambiguous_reply("same 1A") == {1: "a"}


def test_parse_empty_returns_none():
    """A blank reply must not be interpreted as 'upload everything' — that
    could drop a lot of bytes by accident. Re-prompt instead."""
    assert parse_ambiguous_reply("") is None
    assert parse_ambiguous_reply("   ") is None


def test_parse_unrecognized_returns_none():
    assert parse_ambiguous_reply("what does this mean") is None


def test_parse_same_with_no_tokens_is_empty_dict():
    """'same' with no indices means 'I looked, none are the same' — i.e. all
    unmentioned → all upload. Empty dict lets the caller's default do that."""
    assert parse_ambiguous_reply("same") == {}
