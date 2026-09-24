"""Reconciliation: rebuild/refresh the (edu_userid, timelineid) -> Vikunja task map.

Runs at the start of every cycle, before the fetch/diff/apply phases: a wiped
state DB is re-seeded from account-specific description markers. Legacy anchor
labels are recognized for migration. Crash gaps are healed, vanished mappings
are pruned and duplicate losers are retired.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

from edupagetasks.models import HomeworkItem, MapRow, SyncState
from edupagetasks.render import extract_sync_key
from edupagetasks.state import StateStore
from edupagetasks.vikunja import VikTask

logger = logging.getLogger(__name__)

#: Reconciler cannot see the raw EduPage source fields, so newly discovered
#: map rows get this placeholder fingerprint. The engine's diff phase treats
#: it as an unset baseline: the first time the item is observed it persists
#: item.content_fingerprint() without firing a spurious PATCH.
UNSET_FINGERPRINT = "unset"

_MARKER_STRIP = re.compile(
    r"\s*<!--\s*(?:edupage-timelineid:\d+|edupage-key:[^:\s]+:\d+)\s*-->\s*"
)


@dataclass
class ReconcileResult:
    adopted: int = 0
    pruned: int = 0
    duplicates_handled: int = 0
    denylisted: list[int] = field(default_factory=list)
    fresh: bool = False


_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)


def _iso_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_created(value: str | None) -> datetime:
    if not value:
        return _EPOCH_UTC
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        return _EPOCH_UTC


def _strip_marker(description: str) -> str:
    return _MARKER_STRIP.sub("", description or "").rstrip()


def _anchor_re(userid: str) -> re.Pattern[str]:
    return re.compile(rf"^edu:{re.escape(userid)}:(\d+)$")


class Reconciler:
    """Rebuilds the key -> Vikunja task map from the project's tasks + state DB."""

    def __init__(
        self,
        store: StateStore,
        vikunja: object,
        *,
        project_id: int,
        userid: str,
    ) -> None:
        self.store = store
        self.vikunja = vikunja
        self.project_id = project_id
        self.userid = userid
        self._anchor = _anchor_re(userid)
        self._orphans: list[VikTask] = []

    @property
    def denylisted_ids(self) -> set[int]:
        return set(getattr(self, "_denylist_ids", set()))

    def run(self, *, dry_run: bool = False) -> ReconcileResult:
        result = ReconcileResult()
        self._denylist_ids: set[int] = set()
        tasks = self.vikunja.list_tasks(self.project_id)

        quality: dict[int, tuple[int, int | None]] = {}
        groups: dict[int, list[VikTask]] = {}
        orphans: list[VikTask] = []
        for task in tasks:
            if task.project_id != self.project_id:
                continue
            tid, anchor_id = self._timelineid(task)
            if tid is not None:
                quality[task.id] = (tid, anchor_id)
                groups.setdefault(tid, []).append(task)
            else:
                orphans.append(task)
        self._orphans = orphans

        winners: dict[int, VikTask] = {}
        canonical: dict[int, int] = {}
        for tid, group in groups.items():
            group.sort(key=_parse_task_created_key)
            winner = group[0]
            winners[tid] = winner
            canonical[winner.id] = tid
            for loser in group[1:]:
                result.duplicates_handled += 1
                result.denylisted.append(loser.id)
                canonical.pop(loser.id, None)
                if not dry_run:
                    self._strip_duplicate(loser, quality[loser.id])
        self._denylist_ids = set(result.denylisted)

        self._sync_store(
            winners=winners,
            quality=quality,
            canonical=canonical,
            result=result,
            checked_tasks=tasks,
            dry_run=dry_run,
        )
        return result

    def adopt_content(self, items: list[HomeworkItem], *, dry_run: bool = False) -> int:
        """Content alone is not a safe identity; only durable keys are adopted."""
        return 0

    # -- internal ----------------------------------------------------------

    def _timelineid(self, task: VikTask) -> tuple[int | None, int | None]:
        own_anchors: list[tuple[int, int]] = []
        for label in task.labels or []:
            match = self._anchor.match(label.title)
            if match:
                own_anchors.append((int(match.group(1)), label.id))
            elif label.title.startswith("edu:"):
                return None, None
        key = extract_sync_key(task.description or "")
        if key is not None:
            userid, tid = key
            if userid != self.userid or any(anchor_tid != tid for anchor_tid, _ in own_anchors):
                return None, None
            anchor_id = own_anchors[0][1] if own_anchors else None
            return tid, anchor_id
        if own_anchors and len({tid for tid, _ in own_anchors}) == 1:
            return own_anchors[0]
        return None, None

    def _strip_duplicate(self, task: VikTask, target: tuple[int, int | None]) -> None:
        _, anchor_id = target
        description = _strip_marker(task.description)
        labels = [l.id for l in task.labels or [] if l.id != anchor_id]
        try:
            self.vikunja.patch_task(
                task.id, done=True, description=description, labels=labels
            )
        except Exception as exc:  # noqa: BLE001 - best-effort; reconcile must not abort here
            logger.warning("duplicate strip failed for task %s: %s", task.id, exc)

    def _sync_store(
        self,
        *,
        winners: dict[int, VikTask],
        quality: dict[int, tuple[int, int | None]],
        canonical: dict[int, int],
        result: ReconcileResult,
        checked_tasks: list[VikTask],
        dry_run: bool,
    ) -> None:
        raw_rows = self.store.rows()
        existing = {(r.userid, r.timelineid): r for r in raw_rows}
        had_own_rows = any(r.userid == self.userid for r in raw_rows)
        id_to_task = {t.id: t for t in checked_tasks}

        to_upsert: list[MapRow] = []
        to_delete: list[tuple[str, int]] = []
        for (uid, row_tid), row in existing.items():
            if uid != self.userid:
                continue
            task_id = row.vikunja_task_id
            if row.sync_state in (SyncState.CREATING, SyncState.DEFERRED):
                task = id_to_task.get(task_id) if task_id is not None else None
                mapped_tid = canonical.get(task_id) if task_id is not None else None
                valid = (
                    task is not None
                    and task.project_id == self.project_id
                    and mapped_tid == row_tid
                )
                if valid:
                    anchor_id = quality[task_id][1] if task_id in quality else None
                    to_upsert.append(
                        replace(
                            row,
                            vikunja_task_id=task_id,
                            anchor_label_id=anchor_id,
                            updated_at=_iso_now(),
                        )
                    )
                # Keep an unresolved intent/queue intact. The engine can safely
                # retry it; clearing it here loses the only copy of the work.
                continue
            if task_id is None:
                continue
            task = id_to_task.get(task_id)
            mapped_tid = canonical.get(task_id)
            unanchored_own_mapping = (
                task is not None
                and task.project_id == self.project_id
                and mapped_tid is None
                and extract_sync_key(task.description or "") is None
                and not any(label.title.startswith("edu:") for label in task.labels or [])
            )
            broken = (
                task is None
                or task.project_id != self.project_id
                or row.vikunja_project_id != self.project_id
                or (mapped_tid != row_tid and not unanchored_own_mapping)
            )
            if not broken:
                continue
            redirect = winners.get(row_tid)
            if redirect is not None and redirect.id != task_id and task is not None:
                anchor_id = quality[redirect.id][1] if redirect.id in quality else None
                to_upsert.append(
                    replace(
                        row,
                        vikunja_task_id=redirect.id,
                        anchor_label_id=anchor_id,
                        updated_at=_iso_now(),
                    )
                )
            else:
                result.pruned += 1
                to_delete.append((uid, row_tid))

        for tid, winner in winners.items():
            row = existing.get((self.userid, tid))
            anchor_id = quality[winner.id][1] if winner.id in quality else None
            if row is None:
                result.adopted += 1
                to_upsert.append(
                    self._row_for_task(winner, tid, anchor_id, UNSET_FINGERPRINT)
                )
            elif row.vikunja_task_id != winner.id:
                to_upsert.append(
                    replace(
                        row,
                        vikunja_task_id=winner.id,
                        anchor_label_id=anchor_id,
                        updated_at=_iso_now(),
                    )
                )
            elif row.anchor_label_id is None and anchor_id is not None:
                to_upsert.append(
                    replace(row, anchor_label_id=anchor_id, updated_at=_iso_now())
                )

        if not dry_run or getattr(self.store, "read_only", False):
            with self.store.in_transaction():
                for uid, tid in to_delete:
                    self.store.delete_row(uid, tid)
                for row in to_upsert:
                    self.store.upsert_row(row)

        result.fresh = (not had_own_rows) and bool(winners)

    def _row_for_task(
        self,
        task: VikTask,
        tid: int,
        anchor_id: int | None,
        fingerprint: str,
    ) -> MapRow:
        due = task.due_date or None
        if due:
            due = due[:10]
        return MapRow(
            userid=self.userid,
            timelineid=tid,
            vikunja_task_id=task.id,
            vikunja_project_id=self.project_id,
            content_fingerprint=fingerprint,
            done_state=bool(getattr(task, "done", False)),
            school_year=0,
            last_seen_due_date=due,
            last_seen_at=_iso_now(),
            closed_at=None,
            pending_ops="[]",
            retry_count=0,
            sync_state=SyncState.CREATED,
            anchor_label_id=anchor_id,
            updated_at=_iso_now(),
        )

def _parse_task_created_key(task: VikTask) -> tuple[datetime, int]:
    return _parse_created(task.created), task.id
