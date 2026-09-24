"""HTML -> Markdown conversion helpers."""

from __future__ import annotations


def html_to_markdown(html: str | None) -> str:
    if not html:
        return ""
    from markdownify import markdownify

    text = markdownify(html) or ""
    lines = [line.rstrip() for line in text.splitlines()]
    collapsed = []
    blank = False
    for line in lines:
        if not line.strip():
            if blank:
                continue
            blank = True
        else:
            blank = False
        collapsed.append(line)
    return "\n".join(collapsed).strip()
