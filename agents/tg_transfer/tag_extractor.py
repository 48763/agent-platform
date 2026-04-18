import re

_TAG_RE = re.compile(r"#(\w+)", re.UNICODE)


def extract_tags(text: str | None) -> list[str]:
    """Extract unique #tags from text, preserving order of first appearance."""
    if not text:
        return []
    seen = set()
    tags = []
    for m in _TAG_RE.finditer(text):
        tag = m.group(1)
        if tag not in seen:
            seen.add(tag)
            tags.append(tag)
    return tags
