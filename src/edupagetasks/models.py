"""Shared data models and contracts for the EduPage -> Vikunja sync engine."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class SyncState(StrEnum):
    CREATING = "creating"
    CREATED = "created"
    UPDATED = "updated"
    CLOSED = "closed"
    DEFERRED = "deferred"
    TOMBSTONED = "tombstoned"


@dataclass
class HomeworkItem:
    """A single EduPage timeline item of a homework-ish type.

    The fingerprint is computed from the source fields (text, title, due
    date, author, subject id, and a resolved EduPage link when present) --
    never from the rendered title/description or the done state.
    """

    timelineid: int
    typ: str
    timestamp: str  # assigned-at, "YYYY-MM-DD HH:MM:SS"
    text: str
    title: str | None  # oldVals.title or fallback
    due_date: str | None  # oldVals.date, "YYYY-MM-DD"
    author: str | None  # vlastnik_meno
    subject_id: str | None  # raw id, if reliably present; else None
    subject_short: str | None  # resolved from dbi tables, may be filled later
    removed: bool
    done: bool  # doneMaxCas observed in userProps
    source_url: str | None = None  # direct e-learning URL when EduPage exposes a material
    material_superid: str | None = None  # source IDs used to resolve the direct URL
    material_planid: str | None = None

    def content_fingerprint(self) -> str:
        fields = (
            self.text or "",
            self.title or "",
            self.due_date or "",
            self.author or "",
            self.subject_id or "",
        )
        # Preserve the old fingerprint for plain homework. Existing tasks with
        # a newly resolved direct link will get a PATCH on the next sync.
        if self.source_url:
            fields += (self.source_url,)
        raw = "\x1f".join(fields)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class MapRow:
    """One row of the homework_map table (state DB)."""

    userid: str
    timelineid: int
    vikunja_task_id: int | None
    vikunja_project_id: int
    content_fingerprint: str
    done_state: bool
    school_year: int
    last_seen_due_date: str | None
    last_seen_at: str
    closed_at: str | None
    pending_ops: str  # JSON-encoded list of queued ops; "[]" otherwise
    retry_count: int
    sync_state: str  # SyncState
    anchor_label_id: int | None
    updated_at: str

    @property
    def key(self) -> tuple[str, int]:
        return (self.userid, self.timelineid)


@dataclass
class PlanItem:
    """One planned action from the diff phase (a dry-run reportable unit)."""

    action: str  # create | patch | patch_done | reopen | delete | close | skip | resume | retry
    userid: str
    timelineid: int
    task_id: int | None
    detail: str = ""
    preview: dict[str, Any] | None = None


@dataclass
class PlannedStep:
    """An intent-row write for the apply phase (survives crashes by design)."""

    action: str
    payload: dict[str, Any] | None = None
