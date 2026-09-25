"""The core sync engine: fetch -> diff -> apply with idempotent, three-phase syncing.

Implements docs/application.md sections 6, 9, 10 and 11: reconciliation on
every cycle, intent rows before POST, duplicate-anchor resolution, policy-close
gated on coverage, deferred re-apply with exponential backoff, and a circuit
breaker that switches the cycle to a fetch + deferred-only pass.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from edupagetasks.config import Config
from edupagetasks.edupage import EduPageClient, EduPageTransientError, LoginState
from edupagetasks.models import HomeworkItem, MapRow, PlanItem, PlannedStep, SyncState
from edupagetasks.reconcile import UNSET_FINGERPRINT, Reconciler
from edupagetasks.render import (
    build_anchor_label,
    build_description,
    build_title,
    extract_sync_key,
    subject_label,
)
from edupagetasks.state import StateStore
from edupagetasks.vikunja import VikunjaGoneError, VikunjaTransientError

logger = logging.getLogger(__name__)

_META_COVERED_FROM = "last_coverage_from"
_META_COVERED_TO = "last_coverage_to"
_META_COVERED_OK = "covered_ok"
_META_BREAKER_STRIKES = "breaker_strikes"
_META_LAST_SYNC = "last_sync_ts"

_STAMP = "%Y-%m-%dT%H:%M:%SZ"
_ITEM_FIELDS = (
    "timelineid",
    "typ",
    "timestamp",
    "text",
    "title",
    "due_date",
    "author",
    "subject_id",
    "subject_short",
    "removed",
    "done",
    "source_url",
)

_RESTORE_BY_ACTION = {
    "create": SyncState.CREATED,
    "resume": SyncState.CREATED,
    "patch": SyncState.UPDATED,
    "patch_done": SyncState.UPDATED,
    "reopen": SyncState.UPDATED,
}


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _stamp(now: datetime) -> str:
    return now.strftime(_STAMP)


def _parse_stamp(value: str) -> datetime:
    return datetime.fromisoformat(value)


@dataclass
class SyncResult:
    items_fetched: int = 0
    planned: list[PlanItem] = field(default_factory=list)
    applied: list[PlanItem] = field(default_factory=list)
    deferred: int = 0
    closed: int = 0
    covered_ok: bool = False
    errors: int = 0
    breaker_open: bool = False


class SyncEngine:
    def __init__(
        self,
        store: StateStore,
        vikunja: object,
        edupage: EduPageClient,
        config: Config,
        *,
        project_id: int,
        userid: str,
        school_year: int,
        dbi: dict,
        subdomain: str,
    ) -> None:
        self.store = store
        self.vikunja = vikunja
        self.edupage = edupage
        self.config = config
        self.project_id = project_id
        self.userid = userid
        self.school_year = school_year
        self.dbi = dbi
        self.subdomain = subdomain
        self.reconciler = Reconciler(
            store, vikunja, project_id=project_id, userid=userid
        )
        self._bucket_ids: dict[str, int] = {}

    # -- public entry ------------------------------------------------------

    def should_run(self, now: datetime | None = None) -> bool:
        n = self._aware(now)
        return n >= self.next_run(self.config.sync.cadence_minutes, now=n)

    def next_run(self, cadence: int, *, now: datetime | None = None) -> datetime:
        n = self._aware(now)
        last = self.store.get_meta(_META_LAST_SYNC)
        if not last:
            return n
        try:
            return _parse_stamp(last) + timedelta(minutes=cadence)
        except ValueError:
            return n

    def run(
        self,
        dry_run: bool = False,
        state: LoginState | None = None,
        now: datetime | None = None,
    ) -> SyncResult:
        n = self._aware(now)
        threshold = self.config.sync.breaker_threshold
        strikes = self.store.get_meta_int(_META_BREAKER_STRIKES, 0)
        breaker_open = strikes >= threshold
        if breaker_open:
            logger.warning(
                "circuit breaker open (%d strikes): deferred-only pass", strikes
            )
        try:
            self.reconciler.run(dry_run=dry_run)
            items, covered_ok, _, _ = self._fetch(state, dry_run=dry_run)
            self.reconciler.adopt_content(items, dry_run=dry_run)
            if breaker_open:
                steps, plan = self._deferred_plan()
            else:
                steps, plan = self._diff(items, covered_ok, n)
            if dry_run:
                self._add_plan_previews(steps, plan, n)
            deferred, closed, errors, applied = self._apply(steps, n, dry_run=dry_run)
            if not dry_run:
                self._finish_pass(
                    deferred=deferred, errors=errors, now=n,
                    deferred_only=breaker_open, covered_ok=covered_ok
                )
            return SyncResult(
                items_fetched=len(items),
                planned=plan,
                applied=applied,
                deferred=deferred,
                closed=closed,
                covered_ok=covered_ok,
                errors=errors,
                breaker_open=breaker_open,
            )
        except (VikunjaTransientError, EduPageTransientError) as exc:
            logger.error("cycle aborted (transient): %s", exc)
            deferred = closed = errors = 0
            applied: list[PlanItem] = []
            plan: list[PlanItem] = []
            if not dry_run:
                strikes += 1
                steps, plan = self._deferred_plan()
                deferred, closed, errors, applied = self._apply(steps, n, dry_run=False)
                strikes += deferred + errors
                self.store.set_meta(_META_BREAKER_STRIKES, str(strikes))
            return SyncResult(
                items_fetched=0,
                planned=plan,
                applied=applied,
                deferred=deferred,
                closed=closed,
                covered_ok=False,
                errors=1 + deferred,
                breaker_open=strikes >= threshold,
            )

    # -- phase 1: fetch ----------------------------------------------------

    def _fetch(
        self,
        state: LoginState | None,
        *,
        dry_run: bool,
    ) -> tuple[list[HomeworkItem], bool, datetime, datetime]:
        cfg = self.config.edupage
        n = self._aware(None)
        window_start = n - timedelta(days=cfg.window_days)
        coverage_from = window_start.date()
        coverage_to = n.date()
        if state is None:
            state = self.edupage.connect()
        try:
            items = self.edupage.homework_items(state, include_types=cfg.include_types)
            history_items, history_props = self.edupage.fetch_history(
                state, window_start.strftime("%Y-%m-%d")
            )
            items = self.edupage.merge_history(state, history_items, history_props)
            covered_ok = not (cfg.window_days > 30 or not history_items)
        except EduPageTransientError:
            # partial fetch: keep the login payload items, never close from it
            items = self.edupage.homework_items(state, include_types=cfg.include_types)
            covered_ok = False
        items = [item for item in items if self._in_window(item, window_start)]
        resolver = getattr(self.edupage, "resolve_links", None)
        if resolver is not None:
            resolver(items)
        if not dry_run:
            self.store.set_meta(_META_COVERED_FROM, coverage_from.isoformat())
            self.store.set_meta(_META_COVERED_TO, coverage_to.isoformat())
            self.store.set_meta(_META_COVERED_OK, "1" if covered_ok else "0")
        return items, covered_ok, coverage_from, coverage_to

    def _in_window(self, item: HomeworkItem, window_start: datetime) -> bool:
        try:
            assigned = datetime.strptime(item.timestamp, "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=ZoneInfo(self.config.edupage.timezone)
            ).astimezone(UTC)
        except ValueError:
            return True
        return assigned >= window_start

    # -- phase 2: diff -----------------------------------------------------

    def _diff(
        self,
        items: list[HomeworkItem],
        covered_ok: bool,
        now: datetime,
    ) -> tuple[list[PlannedStep], list[PlanItem]]:
        now = self._aware(now)
        cfg = self.config.sync
        mirror = cfg.mirror_done
        closed_states = {SyncState.CLOSED, SyncState.TOMBSTONED}
        steps: list[PlannedStep] = []
        plan: list[PlanItem] = []
        processed: set[int] = set()
        remote_by_id = {
            task.id: task for task in self.vikunja.list_tasks(self.project_id)
        }

        for item in items:
            tid = item.timelineid
            processed.add(tid)
            row = self.store.get_row(self.userid, tid)
            if row is not None and row.sync_state == SyncState.CREATING:
                if item.removed and row.vikunja_task_id is None:
                    steps.append(PlannedStep("cancel_pending_create", payload={"timelineid": tid}))
                    plan.append(PlanItem("skip", self.userid, tid, None, detail="source removed before create"))
                    continue
                steps.append(PlannedStep("resume", payload={"timelineid": tid, "item": self._item_fields(item)}))
                plan.append(PlanItem("resume", self.userid, tid, row.vikunja_task_id))
                continue
            if row is not None and row.sync_state == SyncState.DEFERRED:
                ops = json.loads(row.pending_ops or "[]")
                queued = ops[0] if ops and isinstance(ops[0], dict) else {}
                op_action = queued.get("action", "patch")
                if item.removed and op_action in ("create", "resume") and row.vikunja_task_id is None:
                    steps.append(PlannedStep("cancel_pending_create", payload={"timelineid": tid}))
                    plan.append(PlanItem("skip", self.userid, tid, None, detail="source removed before create"))
                    continue
                item_data = self._item_fields(item) if not item.removed else queued.get("item")
                if isinstance(item_data, dict):
                    steps.append(PlannedStep("retry", payload={
                        "timelineid": tid, "item": item_data,
                        "restore": queued.get("restore"), "source_op": op_action,
                    }))
                    plan.append(PlanItem("retry", self.userid, tid, row.vikunja_task_id))
                continue
            if item.removed:
                if (
                    row is not None
                    and row.vikunja_task_id is not None
                    and row.sync_state in (SyncState.CREATED, SyncState.UPDATED)
                ):
                    step, item_plan = self._close_candidate(row, now, covered_ok)
                    if step is not None:
                        steps.append(step)
                        plan.append(item_plan)
                continue

            payload = {"timelineid": int(tid), "item": self._item_fields(item)}
            if row is None or (
                row.vikunja_task_id is None
                and row.sync_state not in (SyncState.CREATING, SyncState.DEFERRED)
            ):
                steps.append(PlannedStep("create", payload=payload))
                plan.append(PlanItem("create", self.userid, tid, None))
                continue

            worked_on = False
            original_state = row.sync_state
            seen_fp = row.content_fingerprint
            seen_done = row.done_state
            if original_state in closed_states and not item.done:
                steps.append(PlannedStep("reopen", payload=payload))
                plan.append(PlanItem("reopen", self.userid, tid, row.vikunja_task_id))
                seen_done = False
                worked_on = True
            fingerprint = item.content_fingerprint()
            if seen_fp == UNSET_FINGERPRINT:
                steps.append(
                    PlannedStep(
                        "persist_fingerprint",
                        payload={"timelineid": tid, "item": self._item_fields(item)},
                    )
                )
                plan.append(
                    PlanItem(
                        "skip",
                        self.userid,
                        tid,
                        row.vikunja_task_id,
                        detail="adopted baseline",
                    )
                )
                worked_on = True
            elif seen_fp != fingerprint:
                steps.append(PlannedStep("patch", payload=payload))
                plan.append(PlanItem("patch", self.userid, tid, row.vikunja_task_id))
                worked_on = True
            if item.done != seen_done and (mirror or original_state in closed_states):
                steps.append(
                    PlannedStep(
                        "patch_done",
                        payload={"timelineid": tid, "item": self._item_fields(item)},
                    )
                )
                plan.append(
                    PlanItem("patch_done", self.userid, tid, row.vikunja_task_id)
                )
                worked_on = True
            remote = remote_by_id.get(row.vikunja_task_id)
            old_title = (
                f"{item.subject_short} · {build_title(item)}"
                if item.subject_short and item.title else None
            )
            old_fields = ("**Assignment:**", "**Subject:**", "**Teacher:**",
                          "**Assigned:**", "**Due:**")
            old_description_lines = (
                [line.strip() for line in (remote.description or "").splitlines()
                 if line.strip()][:5]
                if remote else []
            )
            format_drift = bool(
                remote and (
                    (old_title is not None and remote.title == old_title)
                    or extract_sync_key(remote.description or "") != (self.userid, tid)
                    or any(line.startswith(old_fields) for line in old_description_lines)
                    or (
                        item.source_url is not None
                        and f"[Open in EduPage]({item.source_url})"
                        not in (remote.description or "")
                    )
                )
            )
            if format_drift and not any(
                s.action == "patch" and s.payload.get("timelineid") == tid
                for s in steps
            ):
                steps.append(PlannedStep("patch", payload=payload))
                plan.append(PlanItem("patch", self.userid, tid, row.vikunja_task_id,
                                     detail="update title and description format"))
                worked_on = True
            if mirror and remote is not None and remote.done != item.done and not any(
                s.action == "patch_done" and s.payload.get("timelineid") == tid
                for s in steps
            ):
                steps.append(
                    PlannedStep(
                        "patch_done",
                        payload={"timelineid": tid, "item": self._item_fields(item)},
                    )
                )
                plan.append(PlanItem("patch_done", self.userid, tid, row.vikunja_task_id))
                worked_on = True
            desired_priority = self._priority(item, now)
            desired_bucket = self._bucket_id(self.config.vikunja.default_bucket)
            label_titles = {label.title for label in remote.labels} if remote else set()
            derived_drift = bool(
                remote
                and (
                    (desired_priority is not None and remote.priority != desired_priority)
                    or (desired_bucket is not None and desired_bucket not in remote.bucket_ids)
                    or any(label not in label_titles for label in self._extra_labels(item))
                    or build_anchor_label(self.userid, tid) in label_titles
                    or "edupage" in label_titles
                    or any(label.startswith("subject:") for label in label_titles)
                )
            )
            if derived_drift and not any(
                s.action == "patch" and s.payload.get("timelineid") == tid
                for s in steps
            ):
                payload["derived_only"] = True
                steps.append(PlannedStep("patch", payload=payload))
                plan.append(PlanItem("patch", self.userid, tid, row.vikunja_task_id,
                                     detail="derived fields"))
                worked_on = True
            if not worked_on:
                steps.append(
                    PlannedStep(
                        "touch",
                        payload={"timelineid": tid, "item": self._item_fields(item)},
                    )
                )
                plan.append(PlanItem("skip", self.userid, tid, row.vikunja_task_id))

        for row in self.store.rows():
            if row.userid != self.userid or row.timelineid in processed:
                continue
            if row.sync_state in (SyncState.CREATING, SyncState.DEFERRED):
                ops = json.loads(row.pending_ops or "[]")
                queued = ops[0] if ops and isinstance(ops[0], dict) else {}
                item_data = queued.get("item")
                if isinstance(item_data, dict):
                    steps.append(PlannedStep("retry", payload={
                        "timelineid": row.timelineid, "item": item_data,
                        "restore": queued.get("restore"),
                        "source_op": queued.get("action", "create"),
                    }))
                    plan.append(PlanItem("retry", self.userid, row.timelineid, row.vikunja_task_id))
                continue
            if row.sync_state not in (SyncState.CREATED, SyncState.UPDATED):
                continue
            if row.vikunja_task_id is None:
                continue
            step, item_plan = self._close_candidate(row, now, covered_ok)
            if step is not None:
                steps.append(step)
                plan.append(item_plan)
        return steps, plan

    def _deferred_plan(self) -> tuple[list[PlannedStep], list[PlanItem]]:
        steps: list[PlannedStep] = []
        plan: list[PlanItem] = []
        for row in self.store.rows_by_state(SyncState.DEFERRED):
            if row.userid != self.userid:
                continue
            ops = json.loads(row.pending_ops or "[]")
            if not ops:
                continue
            queued = ops[0]
            if not isinstance(queued, dict) or "item" not in queued:
                continue
            payload = {
                "timelineid": row.timelineid,
                "item": queued.get("item"),
                "restore": queued.get("restore"),
                "source_op": queued.get("action", "patch"),
            }
            steps.append(PlannedStep("retry", payload=payload))
            plan.append(
                PlanItem("retry", self.userid, row.timelineid, row.vikunja_task_id)
            )
        return steps, plan

    def _add_plan_previews(
        self, steps: list[PlannedStep], plan: list[PlanItem], now: datetime
    ) -> None:
        """Attach the source and rendered fields to every dry-run action."""
        for step, planned in zip(steps, plan, strict=True):
            fields = (step.payload or {}).get("item")
            if not isinstance(fields, dict):
                continue
            item = self._item_from_fields(fields)
            rendered = self._render_payload(item, now)
            planned.preview = {
                "title": rendered["title"],
                "description": rendered["description"],
                "subject": item.subject_short,
                "assigned": item.timestamp,
                "due_day": item.due_date,
                "due_date": rendered["due_date"],
                "source_done": item.done,
                "create_done": rendered.get("done", False),
                "priority": rendered.get("priority"),
                "labels": list(dict.fromkeys(self._extra_labels(item))),
                "bucket": self.config.vikunja.default_bucket,
                "derived_only": bool((step.payload or {}).get("derived_only")),
                "teacher_cleanup": bool((step.payload or {}).get("teacher_cleanup")),
                "source_op": (step.payload or {}).get("source_op", step.action),
            }

    # -- phase 3: apply ----------------------------------------------------

    def _apply(
        self,
        steps: list[PlannedStep],
        now: datetime,
        *,
        dry_run: bool,
    ) -> tuple[int, int, int, list[PlanItem]]:
        now = self._aware(now)
        applied: list[PlanItem] = []
        if dry_run:
            return 0, 0, 0, applied
        deferred = closed = errors = 0
        for step in steps:
            item, inc_deferred, inc_closed, inc_errors = self._apply_step(step, now)
            if item is not None:
                applied.append(item)
            deferred += inc_deferred
            closed += inc_closed
            errors += inc_errors
        return deferred, closed, errors, applied

    def _apply_step(
        self, step: PlannedStep, now: datetime
    ) -> tuple[PlanItem | None, int, int, int]:
        action = step.action
        payload = step.payload or {}
        tid = payload.get("timelineid")
        row = self.store.get_row(self.userid, tid) if tid is not None else None
        cfg = self.config.sync
        if action in ("touch", "persist_fingerprint"):
            item = self._item_from_fields(payload["item"])
            self._write_touch(row, item, now)
            return None, 0, 0, 0
        if action == "cancel_pending_create":
            if row is not None:
                with self.store.in_transaction():
                    self.store.upsert_row(
                        replace(
                            row, vikunja_task_id=None, sync_state=SyncState.TOMBSTONED,
                            pending_ops="[]", retry_count=0, closed_at=_stamp(now),
                            updated_at=_stamp(now),
                        )
                    )
            return None, 0, 0, 0
        if action in ("close", "delete"):
            policy = payload["policy"]
            row_ = payload["row"]
            try:
                outcome = self._op_close(row_, policy, now)
            except VikunjaGoneError:
                logger.warning(
                    "task %s for timeline %s is gone; leaving row for the "
                    "reconciler to rebuild",
                    row_.vikunja_task_id,
                    row_.timelineid,
                )
                return None, 0, 0, 0
            except (VikunjaTransientError, EduPageTransientError):
                return None, 0, 0, 1
            if outcome == "closed":
                return (
                    PlanItem(
                        "close",
                        row_.userid,
                        row_.timelineid,
                        row_.vikunja_task_id,
                        detail=f"policy={policy}",
                    ),
                    0,
                    1,
                    0,
                )
            if outcome == "deleted":
                return (
                    PlanItem(
                        "delete",
                        row_.userid,
                        row_.timelineid,
                        row_.vikunja_task_id,
                        detail=f"policy={policy}",
                    ),
                    0,
                    0,
                    0,
                )
            return None, 0, 0, 0

        item = self._item_from_fields(payload["item"])
        op_action = payload.get("source_op", action) if action == "retry" else action
        restore = payload.get("restore")
        attempts = 0
        while True:
            try:
                self._run_op(op_action, payload, row, item, now, restore)
            except VikunjaGoneError:
                row = self.store.get_row(self.userid, item.timelineid) or row
                self._handle_gone(row, item, now)
                return None, 0, 0, 0
            except (VikunjaTransientError, EduPageTransientError):
                attempts += 1
                if attempts >= cfg.retry_max:
                    row = self.store.get_row(self.userid, item.timelineid) or row
                    self._defer(op_action, payload, row, item, now)
                    return None, 1, 0, 0
                _sleep(cfg.retry_backoff_s * (2**attempts))
                continue
            display = op_action if action == "retry" else action
            return (
                PlanItem(
                    display,
                    self.userid,
                    item.timelineid,
                    row.vikunja_task_id if row else None,
                ),
                0,
                0,
                0,
            )

    # -- idempotent operations ---------------------------------------------

    def _run_op(
        self,
        action: str,
        payload: dict,
        row: MapRow | None,
        item: HomeworkItem,
        now: datetime,
        restore: str | None,
    ) -> None:
        target = restore or str(_RESTORE_BY_ACTION.get(action, SyncState.UPDATED))
        if action in ("create", "resume"):
            if row is not None and row.vikunja_task_id is not None:
                found_id = row.vikunja_task_id
                found = next(
                    (t for t in self.vikunja.list_tasks(self.project_id) if t.id == found_id),
                    None,
                )
                if found is not None:
                    self._adopt_created(item, found, now)
                    return
            found = self._ensure_anchor(item)
            if found is not None:
                self._adopt_created(item, found, now)
                return
            with self.store.in_transaction():
                self._write_intent(item, now)
            task_id, anchor_id = self._post_and_attach(item, now)
            with self.store.in_transaction():
                self._finalize_create(item, task_id, anchor_id, now)
            return
        if action == "patch":
            self._patch_task(
                item, row, now,
                derived_only=bool(payload.get("derived_only")),
                teacher_cleanup=bool(payload.get("teacher_cleanup")),
            )
            return
        if action == "patch_done":
            assert row is not None
            if row.sync_state in (SyncState.CLOSED, SyncState.TOMBSTONED) and item.done:
                target = row.sync_state
            self.vikunja.patch_task(row.vikunja_task_id, done=item.done)
            with self.store.in_transaction():
                self.store.upsert_row(
                    replace(
                        row,
                        done_state=item.done,
                        sync_state=target,
                        content_fingerprint=item.content_fingerprint(),
                        school_year=self.school_year,
                        last_seen_due_date=item.due_date,
                        last_seen_at=_stamp(now),
                        pending_ops="[]",
                        retry_count=0,
                        updated_at=_stamp(now),
                    )
                )
            return
        if action == "reopen":
            assert row is not None
            self.vikunja.patch_task(row.vikunja_task_id, done=False)
            with self.store.in_transaction():
                self.store.upsert_row(
                    replace(
                        row,
                        done_state=False,
                        sync_state=target,
                        content_fingerprint=item.content_fingerprint(),
                        school_year=self.school_year,
                        last_seen_due_date=item.due_date,
                        last_seen_at=_stamp(now),
                        pending_ops="[]",
                        retry_count=0,
                        updated_at=_stamp(now),
                    )
                )
            return
        raise ValueError(f"unknown op action {action!r}")

    def _patch_task(
        self, item: HomeworkItem, row: MapRow, now: datetime, *,
        derived_only: bool = False, teacher_cleanup: bool = False,
    ) -> None:
        payload = self._render_payload(item, now)
        kwargs: dict = (
            {"priority": payload["priority"]}
            if derived_only and "priority" in payload
            else {} if derived_only else dict(payload)
        )
        if teacher_cleanup and derived_only:
            remote = next(
                (t for t in self.vikunja.list_tasks(self.project_id) if t.id == row.vikunja_task_id),
                None,
            )
            if remote is not None:
                cleaned = "\n".join(
                    line for line in (remote.description or "").splitlines()
                    if not line.strip().startswith("**Teacher:**")
                )
                if cleaned != (remote.description or ""):
                    kwargs["description"] = cleaned
        if item.due_date == row.last_seen_due_date or derived_only:
            kwargs.pop("due_date", None)
        kwargs.pop("bucket_id", None)
        if kwargs:
            self.vikunja.patch_task(row.vikunja_task_id, **kwargs)
        desired_titles = set(self._extra_labels(item))
        remote = next(
            (t for t in self.vikunja.list_tasks(self.project_id) if t.id == row.vikunja_task_id),
            None,
        )
        existing_labels = list(remote.labels or []) if remote else []
        obsolete_titles = {build_anchor_label(self.userid, item.timelineid), "edupage"}
        label_ids = [
            label.id for label in existing_labels
            if label.title not in obsolete_titles and not label.title.startswith("subject:")
        ]
        removing_obsolete = len(label_ids) != len(existing_labels)
        for title in sorted(desired_titles):
            label = self.vikunja.ensure_label(title)
            if label.id not in label_ids:
                label_ids.append(label.id)
                if not removing_obsolete:
                    self.vikunja.attach_label(row.vikunja_task_id, label.id)
        if removing_obsolete:
            self.vikunja.replace_labels(row.vikunja_task_id, label_ids)
        bucket = self._bucket_id(self.config.vikunja.default_bucket)
        if bucket is not None:
            self.vikunja.move_task_to_bucket(self.project_id, row.vikunja_task_id, bucket)
        with self.store.in_transaction():
            self.store.upsert_row(
                replace(
                    row,
                    content_fingerprint=item.content_fingerprint(),
                    done_state=item.done,
                    sync_state=(
                        row.sync_state
                        if row.sync_state in (SyncState.CLOSED, SyncState.TOMBSTONED)
                        else SyncState.UPDATED
                    ),
                    school_year=self.school_year,
                    last_seen_due_date=item.due_date,
                    last_seen_at=_stamp(now),
                    pending_ops="[]",
                    retry_count=0,
                    updated_at=_stamp(now),
                )
            )

    def _post_and_attach(self, item: HomeworkItem, now: datetime) -> tuple[int, int | None]:
        payload = self._render_payload(item, now)
        task = self.vikunja.create_task(self.project_id, **payload)
        task_id = self._task_id(task)
        current = self.store.get_row(self.userid, item.timelineid)
        if current is not None:
            with self.store.in_transaction():
                self.store.upsert_row(
                    replace(current, vikunja_task_id=task_id, updated_at=_stamp(now))
                )
        bucket = self._bucket_id(self.config.vikunja.default_bucket)
        if bucket is not None:
            self.vikunja.move_task_to_bucket(self.project_id, task_id, bucket)
        for extra in self._extra_labels(item):
            label = self.vikunja.ensure_label(extra)
            self.vikunja.attach_label(task_id, label.id)
        return task_id, None

    def _adopt_created(self, item: HomeworkItem, task: object, now: datetime) -> None:
        task_id = self._task_id(task)
        bucket = self._bucket_id(self.config.vikunja.default_bucket)
        if bucket is not None:
            self.vikunja.move_task_to_bucket(self.project_id, task_id, bucket)
        for extra in self._extra_labels(item):
            label = self.vikunja.ensure_label(extra)
            self.vikunja.attach_label(task_id, label.id)
        with self.store.in_transaction():
            self._finalize_create(item, task_id, None, now)

    @staticmethod
    def _task_id(task: object) -> int:
        return task.id if hasattr(task, "id") else int(task)

    def _op_close(self, row: MapRow, policy: str, now: datetime) -> str:
        if policy == "close":
            self.vikunja.patch_task(row.vikunja_task_id, done=True)
            with self.store.in_transaction():
                self.store.upsert_row(
                    replace(
                        row,
                        sync_state=SyncState.CLOSED,
                        closed_at=_stamp(now),
                        updated_at=_stamp(now),
                    )
                )
            return "closed"
        if policy == "delete":
            self.vikunja.delete_task(row.vikunja_task_id)
            with self.store.in_transaction():
                self.store.delete_row(row.userid, row.timelineid)
            return "deleted"
        return "leave"

    def _write_touch(
        self, row: MapRow | None, item: HomeworkItem, now: datetime
    ) -> None:
        if row is None:
            return
        with self.store.in_transaction():
            self.store.upsert_row(
                replace(
                    row,
                    content_fingerprint=item.content_fingerprint(),
                    school_year=self.school_year,
                    last_seen_due_date=item.due_date,
                    last_seen_at=_stamp(now),
                    updated_at=_stamp(now),
                )
            )

    def _write_intent(self, item: HomeworkItem, now: datetime) -> None:
        self.store.upsert_row(
            MapRow(
                userid=self.userid,
                timelineid=item.timelineid,
                vikunja_task_id=None,
                vikunja_project_id=self.project_id,
                content_fingerprint=item.content_fingerprint(),
                done_state=item.done,
                school_year=self.school_year,
                last_seen_due_date=item.due_date,
                last_seen_at=_stamp(now),
                closed_at=None,
                pending_ops=json.dumps([{
                    "action": "create", "restore": str(SyncState.CREATED),
                    "item": self._item_fields(item),
                }]),
                retry_count=0,
                sync_state=SyncState.CREATING,
                anchor_label_id=None,
                updated_at=_stamp(now),
            )
        )

    def _finalize_create(
        self, item: HomeworkItem, task_id: int, anchor_id: int | None, now: datetime
    ) -> None:
        row = self.store.get_row(self.userid, item.timelineid)
        base = (
            MapRow(
                userid=self.userid,
                timelineid=item.timelineid,
                vikunja_task_id=task_id,
                vikunja_project_id=self.project_id,
                content_fingerprint=item.content_fingerprint(),
                done_state=item.done,
                school_year=self.school_year,
                last_seen_due_date=item.due_date,
                last_seen_at=_stamp(now),
                closed_at=None,
                pending_ops="[]",
                retry_count=0,
                sync_state=SyncState.CREATED,
                anchor_label_id=anchor_id,
                updated_at=_stamp(now),
            )
            if row is None
            else replace(
                row,
                vikunja_task_id=task_id,
                anchor_label_id=anchor_id,
                content_fingerprint=item.content_fingerprint(),
                done_state=item.done,
                school_year=self.school_year,
                last_seen_due_date=item.due_date,
                last_seen_at=_stamp(now),
                sync_state=SyncState.CREATED,
                pending_ops="[]",
                retry_count=0,
                closed_at=None,
                updated_at=_stamp(now),
            )
        )
        self.store.upsert_row(base)

    def _defer(
        self,
        op_action: str,
        payload: dict,
        row: MapRow | None,
        item: HomeworkItem,
        now: datetime,
    ) -> None:
        restore = str(_RESTORE_BY_ACTION.get(op_action, SyncState.UPDATED))
        queued = {
            "action": op_action,
            "restore": restore,
            "item": self._item_fields(item),
        }
        if row is None:
            row = MapRow(
                userid=self.userid,
                timelineid=item.timelineid,
                vikunja_task_id=None,
                vikunja_project_id=self.project_id,
                content_fingerprint=item.content_fingerprint(),
                done_state=item.done,
                school_year=self.school_year,
                last_seen_due_date=item.due_date,
                last_seen_at=_stamp(now),
                closed_at=None,
                pending_ops="[]",
                retry_count=0,
                sync_state=SyncState.CREATING,
                anchor_label_id=None,
                updated_at=_stamp(now),
            )
        with self.store.in_transaction():
            self.store.upsert_row(
                replace(
                    row,
                    sync_state=SyncState.DEFERRED,
                    pending_ops=json.dumps([queued]),
                    retry_count=row.retry_count + 1,
                    updated_at=_stamp(now),
                )
            )

    def _handle_gone(
        self, row: MapRow | None, item: HomeworkItem, now: datetime
    ) -> None:
        if row is None:
            return
        if row.sync_state in (SyncState.CREATING, SyncState.DEFERRED):
            with self.store.in_transaction():
                self.store.upsert_row(
                    replace(
                        row,
                        vikunja_task_id=None,
                        sync_state=SyncState.CREATED,
                        pending_ops="[]",
                        retry_count=0,
                        updated_at=_stamp(now),
                    )
                )
        else:
            with self.store.in_transaction():
                self.store.delete_row(row.userid, row.timelineid)

    # -- helpers -----------------------------------------------------------

    def _ensure_anchor(self, item: HomeworkItem) -> object | None:
        denylisted = self.reconciler.denylisted_ids
        for task in self.vikunja.list_tasks(self.project_id):
            if task.project_id != self.project_id:
                continue
            if task.id in denylisted:
                continue
            mapped_tid, _ = self.reconciler._timelineid(task)
            if mapped_tid == item.timelineid:
                return task
        return None

    def _render_payload(self, item: HomeworkItem, now: datetime) -> dict:
        title = build_title(item)
        description = build_description(
            item,
            subdomain=self.subdomain,
            userid=self.userid,
        )
        due = None
        if item.due_date:
            try:
                local_end = datetime.strptime(item.due_date, "%Y-%m-%d").replace(
                    hour=23, minute=59, second=59,
                    tzinfo=ZoneInfo(self.config.edupage.timezone),
                )
                due = local_end.astimezone(UTC).isoformat().replace("+00:00", "Z")
            except (ValueError, KeyError):
                due = None
        priority = self._priority(item, now)
        payload: dict = {"title": title, "description": description, "due_date": due}
        if self.config.sync.mirror_done:
            payload["done"] = item.done
        if priority is not None:
            payload["priority"] = priority
        return payload

    def _priority(self, item: HomeworkItem, now: datetime) -> int | None:
        rule = self.config.vikunja.priority_rule
        if rule is None or not item.due_date:
            return None if rule is None else 0
        try:
            due_day = datetime.strptime(item.due_date, "%Y-%m-%d").date()
        except ValueError:
            return 0
        local_today = now.astimezone(ZoneInfo(self.config.edupage.timezone)).date()
        delta_days = (due_day - local_today).days
        if delta_days < 0:
            return rule.overdue
        if delta_days <= rule.due_within_days:
            return rule.due_within_days
        return 0

    def _extra_labels(self, item: HomeworkItem) -> list[str]:
        labels = list(self.config.vikunja.labels)
        if self.config.vikunja.subject_labels and item.subject_short:
            labels.append(subject_label(item.subject_short))
        return labels

    def _bucket_id(self, bucket: int | str | None) -> int | None:
        if bucket is None:
            return None
        if isinstance(bucket, int):
            return bucket
        if bucket not in self._bucket_ids:
            self._bucket_ids[bucket] = self.vikunja.resolve_bucket_id(
                self.project_id, bucket
            )
        return self._bucket_ids[bucket]

    def _close_candidate(
        self, row: MapRow, now: datetime, covered_ok: bool
    ) -> tuple[PlannedStep | None, PlanItem | None]:
        policy = self.config.sync.delete_policy
        if not covered_ok or not self._close_eligible(row, now):
            return None, None
        if policy == "leave":
            return None, None
        action = "close" if policy == "close" else "delete"
        step = PlannedStep(
            action, payload={"policy": policy, "row": row, "timelineid": row.timelineid}
        )
        item_plan = PlanItem(
            action,
            row.userid,
            row.timelineid,
            row.vikunja_task_id,
            detail=f"policy={policy}",
        )
        return step, item_plan

    def _close_eligible(self, row: MapRow, now: datetime) -> bool:
        cfg = self.config
        due = row.last_seen_due_date
        if due:
            try:
                due_dt = datetime.strptime(due, "%Y-%m-%d").date()
            except ValueError:
                due_dt = None
            if due_dt is not None:
                local_today = now.astimezone(ZoneInfo(cfg.edupage.timezone)).date()
                return (local_today - due_dt).days > cfg.sync.grace_days
        try:
            last_seen = _parse_stamp(row.last_seen_at)
        except ValueError:
            last_seen = now
        window = cfg.edupage.window_days + cfg.sync.grace_days
        return now - last_seen > timedelta(days=window)

    def _finish_pass(
        self, *, deferred: int, errors: int, now: datetime,
        deferred_only: bool = False, covered_ok: bool = False
    ) -> None:
        if errors or deferred:
            strikes = self.store.get_meta_int(_META_BREAKER_STRIKES, 0)
            self.store.set_meta(_META_BREAKER_STRIKES, str(strikes + errors + deferred))
        elif covered_ok or deferred_only:
            self.store.set_meta(_META_BREAKER_STRIKES, "0")
        if not deferred_only and covered_ok and errors == 0 and deferred == 0:
            self.store.set_meta(_META_LAST_SYNC, _stamp(now))

    @staticmethod
    def _item_fields(item: HomeworkItem) -> dict:
        return {key: getattr(item, key) for key in _ITEM_FIELDS}

    @staticmethod
    def _item_from_fields(data: dict) -> HomeworkItem:
        # Queued operations from older versions have no source_url field.
        return HomeworkItem(**{key: data[key] for key in _ITEM_FIELDS if key in data})

    @staticmethod
    def _aware(now: datetime | None) -> datetime:
        value = now if now is not None else datetime.now(UTC)
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.replace(second=0, microsecond=0)
