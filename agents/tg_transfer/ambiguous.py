"""Phase 5: Ambiguous queue UI helpers.

When Phase 4's thumb dedup produces `thumb_match_metadata_mismatch`, the source
message is parked in `pending_dedup` instead of being auto-skipped or uploaded.
At batch end we surface a summary so the user can arbitrate.

Display convention: source rows get 1-based index `[N]`, target candidates get
letters `a/b/c/...` inside each row. Letters (not digits) for candidates make
`1b` unambiguous — `11` would otherwise collide with source index 11.
"""
from __future__ import annotations

import re
from typing import Union


# "same 1a" / "same 1a, 2b" / "same 1a 2b" / "相同 1a"
_SAME_TOKEN_RE = re.compile(r"(\d+)\s*([a-z])", re.IGNORECASE)
_SAME_PREFIX_RE = re.compile(r"^\s*(same|相同|一樣)\b", re.IGNORECASE)
_SKIP_RE = re.compile(r"^\s*(skip|略過|跳過|全部上傳|都上傳)\s*$", re.IGNORECASE)


def format_ambiguous_summary(pending_rows: list[dict]) -> str:
    """Render the pending_dedup queue as a user-facing summary.

    `pending_rows` is the list returned by MediaDB.list_pending_dedup_by_job —
    each row has `source_msg_id` and `candidate_target_msg_ids` (already parsed
    from JSON). Candidates beyond 26 get truncated to fit a single letter; that
    cap is intentional — more than ~5 candidates already means the user should
    probably use /index_target to widen coverage, not arbitrate them manually.
    """
    if not pending_rows:
        return ""
    lines = ["以下訊息的縮圖 hash 撞到目標候選，但檔案 metadata 不吻合："]
    for idx, row in enumerate(pending_rows, start=1):
        src = row["source_msg_id"]
        candidates = row.get("candidate_target_msg_ids") or []
        # Cap at 26 to stay inside a-z. Unlikely in practice but defends
        # against pathological thumb collisions.
        candidates = candidates[:26]
        lines.append(f"\n[{idx}] 來源訊息 #{src}")
        for cand_idx, target_msg_id in enumerate(candidates):
            letter = chr(ord("a") + cand_idx)
            lines.append(f"    {letter}) 目標訊息 #{target_msg_id}")
    lines.append(
        "\n請回覆格式：「same 1a, 2b」表示 [1] 跟 a 相同、[2] 跟 b 相同。"
    )
    lines.append("未提到的視為不同 → 會上傳；回覆「skip」則全部略過不上傳。")
    return "\n".join(lines)


def parse_ambiguous_reply(content: str) -> Union[dict[int, str], str, None]:
    """Parse user's resolution reply.

    Returns:
      - "skip"            → drop all pending rows, no uploads
      - {1: 'a', 2: 'b'}  → source idx → target candidate letter mapping;
                            unmentioned source indices mean "different, upload"
      - None              → unrecognized input, caller should re-prompt

    Empty/whitespace content returns None (re-prompt) so accidental blank
    replies never silently upload the whole queue.
    """
    if not content or not content.strip():
        return None

    if _SKIP_RE.match(content):
        return "skip"

    # Accept both "same 1a, 2b" and bare "1a, 2b" / "1a 2b".
    tokens = _SAME_TOKEN_RE.findall(content)
    if not tokens:
        # User said "same" with no tokens — treat as "all different, upload
        # everything" since they explicitly engaged with the prompt. Use an
        # empty dict; caller's default (unmentioned → upload) covers this.
        if _SAME_PREFIX_RE.match(content):
            return {}
        return None

    result: dict[int, str] = {}
    for idx_str, letter in tokens:
        result[int(idx_str)] = letter.lower()
    return result
