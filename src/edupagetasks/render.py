"""Build Vikunja task fields from an EduPage HomeworkItem."""

from __future__ import annotations

import re
from urllib.parse import quote, unquote

from edupagetasks.models import HomeworkItem

_MARKER_RE = re.compile(r"<!--\s*edupage-timelineid:(\d+)\s*-->")
_SYNC_MARKER_RE = re.compile(r"<!--\s*edupage-key:([A-Za-z0-9._~%\-]+):(\d+)\s*-->")
TITLE_MAX_LENGTH = 70


def build_title(item: HomeworkItem) -> str:
    title = (item.title or "").split("\n", 1)[0].strip()
    if not title:
        title = item.text or ""
    title = title or "(no title)"
    if len(title) > TITLE_MAX_LENGTH:
        return title[: TITLE_MAX_LENGTH - 3].rstrip() + "..."
    return title


def build_description(
    item: HomeworkItem,
    *,
    subdomain: str,
    userid: str,
) -> str:
    from edupagetasks.markdown import html_to_markdown

    # The source title appears in full here when the concise Vikunja title
    # would lose characters or additional lines.
    full_title = (item.title or "").strip()
    parts: list[str] = []
    if full_title and (len(full_title) > TITLE_MAX_LENGTH or "\n" in full_title):
        parts.append(full_title)
    body = html_to_markdown(item.text)
    if body:
        parts.append(body)
    parts.append(
        f"[Open in EduPage](https://{subdomain}.edupage.org/timeline/"
        f"?timelineid={item.timelineid}#item-{item.timelineid})"
    )
    parts.append(build_sync_marker(userid, item.timelineid))
    return "\n\n".join(parts)


def build_anchor_label(userid: str, timelineid: int) -> str:
    return f"edu:{userid}:{timelineid}"


def build_sync_marker(userid: str, timelineid: int) -> str:
    return f"<!-- edupage-key:{quote(userid, safe='')}:{timelineid} -->"


def extract_sync_key(description_or_body: str) -> tuple[str, int] | None:
    matches = _SYNC_MARKER_RE.findall(description_or_body or "")
    if not matches:
        return None
    userid, timelineid = matches[-1]
    return unquote(userid), int(timelineid)


def subject_label(title: str) -> str:
    return title.strip().upper()


def extract_marker_timelineid(description_or_body: str) -> int | None:
    key = extract_sync_key(description_or_body)
    if key is not None:
        return key[1]
    matches = _MARKER_RE.findall(description_or_body)
    return int(matches[-1]) if matches else None
