import json
from datetime import UTC, datetime, timedelta

from edupagetasks.config import (
    Config,
    EduPageAuthConfig,
    EduPageConfig,
    SyncConfig,
    VikunjaConfig,
)
from edupagetasks.edupage import EduPageTransientError, LoginState
from edupagetasks.engine import SyncEngine
from edupagetasks.models import HomeworkItem, MapRow, PlannedStep, SyncState
from edupagetasks.state import StateStore
from edupagetasks.vikunja import VikLabel, VikTask, VikunjaGoneError


def make_item(**overrides) -> HomeworkItem:
    defaults = {
        "timelineid": 123,
        "typ": "homework",
        "timestamp": (datetime.now().astimezone(tz=None) - timedelta(days=2)).strftime(
            "%Y-%m-%d %H:%M:%S"
        ),
        "text": "<p>Solve page 12</p>",
        "title": "Fr. equations",
        "due_date": "2026-09-14",
        "author": "J. Smith",
        "subject_id": None,
        "subject_short": "MAT",
        "removed": False,
        "done": False,
    }
    defaults.update(overrides)
    return HomeworkItem(**defaults)


def make_row(
    *,
    timelineid: int = 123,
    task_id: int | None = 7,
    state: str = SyncState.CREATED,
    fingerprint: str = "fp1",
    due: str | None = "2026-02-01",
    last_seen: str = "2026-01-01T00:00:00Z",
    pending: str = "[]",
    retry: int = 0,
    done_state: bool = False,
    closed_at: str | None = None,
) -> MapRow:
    return MapRow(
        userid="u1",
        timelineid=timelineid,
        vikunja_task_id=task_id,
        vikunja_project_id=12,
        content_fingerprint=fingerprint,
        done_state=done_state,
        school_year=2026,
        last_seen_due_date=due,
        last_seen_at=last_seen,
        closed_at=closed_at,
        pending_ops=pending,
        retry_count=retry,
        sync_state=state,
        anchor_label_id=3,
        updated_at="2026-01-01T00:00:00Z",
    )


def make_config(
    *,
    window_days: int = 30,
    mirror_done: bool = False,
    delete_policy: str = "close",
    grace_days: int = 7,
    retry_max: int = 4,
    retry_backoff_s: int = 1,
    breaker_threshold: int = 5,
    priority_rule=None,
    default_bucket=None,
) -> Config:
    return Config(
        edupage=EduPageConfig(
            subdomain="gym",
            auth=EduPageAuthConfig(
                mode="password", username="u", password="p", session_id=None
            ),
            window_days=window_days,
            include_types=["homework"],
            timezone="UTC",
            teacher_line=True,
        ),
        vikunja=VikunjaConfig(
            base_url="http://v/api/v2",
            token="t",
            project=12,
            labels=[],
            subject_labels=True,
            priority_rule=priority_rule,
            default_bucket=default_bucket,
        ),
        sync=SyncConfig(
            mirror_done=mirror_done,
            delete_policy=delete_policy,
            grace_days=grace_days,
            retry_max=retry_max,
            retry_backoff_s=retry_backoff_s,
            breaker_threshold=breaker_threshold,
        ),
    )


class FakeVikunja:
    def __init__(self, tasks: list[VikTask] | None = None, on_create=None) -> None:
        self.tasks = list(tasks or [])
        self.calls: list = []
        self.labels: dict[int, VikLabel] = {}
        self._next_task = max([t.id for t in self.tasks] or [0]) + 1
        self._next_label = 1
        self.on_create = on_create
        self.last_create_snapshot = None

    def _find(self, task_id: int) -> VikTask | None:
        return next((t for t in self.tasks if t.id == task_id), None)

    def list_tasks(self, project_id: int) -> list[VikTask]:
        self.calls.append(("list_tasks", project_id))
        return [t for t in self.tasks if t.project_id == project_id]

    def create_task(self, project_id: int, **kwargs) -> VikTask:
        self.calls.append(("create_task", project_id, dict(kwargs)))
        if self.on_create is not None:
            self.last_create_snapshot = self.on_create()
        task = VikTask(
            id=self._next_task,
            project_id=project_id,
            title=kwargs.get("title") or "",
            description=kwargs.get("description") or "",
            due_date=kwargs.get("due_date"),
            priority=int(kwargs.get("priority") or 0),
            bucket_id=kwargs.get("bucket_id"),
            labels=[],
            created="2026-01-01T00:00:00Z",
        )
        self._next_task += 1
        self.tasks.append(task)
        return task

    def patch_task(self, task_id: int, **kwargs) -> VikTask:
        self.calls.append(("patch_task", task_id, dict(kwargs)))
        task = self._find(task_id)
        if task is None:
            raise VikunjaGoneError(f"task {task_id} gone")
        if kwargs.get("title") is not None:
            task.title = kwargs["title"]
        if kwargs.get("description") is not None:
            task.description = kwargs["description"]
        if kwargs.get("due_date") is not None:
            task.due_date = kwargs["due_date"]
        if kwargs.get("done") is not None:
            task.done = kwargs["done"]
        if kwargs.get("priority") is not None:
            task.priority = kwargs["priority"]
        if kwargs.get("bucket_id") is not None:
            task.bucket_id = kwargs["bucket_id"]
        if kwargs.get("labels") is not None:
            task.labels = [VikLabel(id=i, title=f"label-{i}") for i in kwargs["labels"]]
        return task

    def delete_task(self, task_id: int) -> None:
        self.calls.append(("delete_task", task_id))
        if self._find(task_id) is None:
            raise VikunjaGoneError(f"task {task_id} gone")
        self.tasks = [t for t in self.tasks if t.id != task_id]

    def resolve_bucket_id(self, project_id: int, bucket) -> int:
        self.calls.append(("resolve_bucket_id", project_id, bucket))
        return 11 if str(bucket) == "Inbox" else 0

    def ensure_label(self, title: str) -> VikLabel:
        self.calls.append(("ensure_label", title))
        for label in self.labels.values():
            if label.title == title:
                return label
        label = VikLabel(id=self._next_label, title=title)
        self._next_label += 1
        self.labels[label.id] = label
        return label

    def attach_label(self, task_id: int, label_id: int) -> None:
        self.calls.append(("attach_label", task_id, label_id))
        task = self._find(task_id)
        label = self.labels.get(label_id)
        if task is not None and label is not None and label not in task.labels:
            task.labels.append(label)

    def replace_labels(self, task_id: int, label_ids: list[int]) -> None:
        self.calls.append(("replace_labels", task_id, list(label_ids)))
        task = self._find(task_id)
        if task is None:
            raise VikunjaGoneError(f"task {task_id} gone")
        existing = {label.id: label for label in task.labels}
        task.labels = [self.labels.get(label_id) or existing[label_id] for label_id in label_ids]


class FakeEduPage:
    def __init__(
        self,
        login: list[HomeworkItem] | None = None,
        history_fail: bool = False,
        history_fail_times: int = 0,
        history_items: list[HomeworkItem] | None = None,
    ) -> None:
        self.login = login or []
        self.history_fail_times = 9999999 if history_fail else history_fail_times
        self.history_items = list(history_items or [])
        self.calls: list = []

    def connect(self) -> LoginState:
        self.calls.append("connect")
        return LoginState(userid="u1")

    def homework_items(self, state, *, include_types=None):
        self.calls.append(("homework_items", include_types))
        return list(self.login)

    def fetch_history(self, state, date_from):
        self.calls.append(("fetch_history", date_from))
        if self.history_fail_times > 0:
            self.history_fail_times -= 1
            raise EduPageTransientError("history pull failed")
        return [i.__dict__ for i in self.history_items], {}

    def merge_history(self, state, hist_items, hist_props):
        self.calls.append("merge_history")
        merged = {item.timelineid: item for item in self.login}
        for raw in hist_items:
            item = HomeworkItem(**raw)
            merged[item.timelineid] = item
        return list(merged.values())


def make_engine(
    tmp_path, **overrides
) -> tuple[SyncEngine, StateStore, FakeVikunja, FakeEduPage]:
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    vikunja = FakeVikunja(
        **{k: overrides.pop(k) for k in ("tasks", "on_create") if k in overrides}
    )
    edupage = FakeEduPage(
        **{
            k: overrides.pop(k)
            for k in (
                "login",
                "history_fail",
                "history_fail_times",
                "history_items",
            )
            if k in overrides
        }
    )
    engine = SyncEngine(
        store,
        vikunja,
        edupage,
        make_config(**overrides),
        project_id=12,
        userid="u1",
        school_year=2026,
        dbi={},
        subdomain="gym",
    )
    return engine, store, vikunja, edupage


NOW = datetime(2026, 9, 21, 10, 0, 0, tzinfo=UTC)


# -- diff: decision engine --------------------------------------------------


def test_diff_no_row_plans_create(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    steps, plan = engine._diff([make_item()], covered_ok=True, now=NOW)
    assert any(s.action == "create" for s in steps)
    assert any(p.action == "create" for p in plan)
    assert store.rows() == []


def test_diff_fingerprint_change_plans_patch(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    item = make_item()
    store.upsert_row(make_row(fingerprint="old-fingerprint"))
    steps, _ = engine._diff([item], covered_ok=True, now=NOW)
    assert any(s.action == "patch" for s in steps)
    assert not any(s.action == "create" for s in steps)
    assert not any(s.action == "patch_done" for s in steps)


def test_diff_patch_done_only_with_mirror_done(tmp_path):
    item = make_item(done=True)
    engine_off, store, _, _ = make_engine(tmp_path, mirror_done=False)
    store.upsert_row(make_row(fingerprint=item.content_fingerprint(), done_state=False))
    steps_off, _ = engine_off._diff([item], covered_ok=True, now=NOW)
    assert not any(s.action == "patch_done" for s in steps_off)

    engine_on, store2, _, _ = make_engine(tmp_path, mirror_done=True)
    store2.upsert_row(
        make_row(fingerprint=item.content_fingerprint(), done_state=False)
    )
    steps_on, _ = engine_on._diff([item], covered_ok=True, now=NOW)
    assert any(s.action == "patch_done" for s in steps_on)
    assert not any(s.action == "patch" for s in steps_on)


def test_diff_unchanged_plans_skip(tmp_path):
    item = make_item()
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(make_row(fingerprint=item.content_fingerprint(), done_state=False))
    steps, plan = engine._diff([item], covered_ok=True, now=NOW)
    assert all(s.action == "touch" for s in steps)
    assert not any(
        s.action in ("create", "patch", "patch_done", "reopen") for s in steps
    )
    assert any(p.action == "skip" for p in plan)


def test_diff_reopen_from_closed(tmp_path):
    item = make_item(done=False)
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(
        make_row(
            fingerprint=item.content_fingerprint(),
            done_state=True,
            state=SyncState.CLOSED,
            closed_at="2026-01-02T00:00:00Z",
        )
    )
    steps, _ = engine._diff([item], covered_ok=True, now=NOW)
    assert any(s.action == "reopen" for s in steps)
    reopened = [s for s in steps if s.action == "reopen"]
    assert reopened
    assert len(reopened) == 1


def test_diff_deferred_row_plans_retry(tmp_path):
    item = make_item()
    engine, store, _, _ = make_engine(tmp_path)
    op = {"action": "patch", "restore": "updated", "item": engine._item_fields(item)}
    store.upsert_row(
        make_row(
            fingerprint="old",
            state=SyncState.DEFERRED,
            pending=json.dumps([op]),
            retry=2,
        )
    )
    steps, _ = engine._diff([item], covered_ok=True, now=NOW)
    retries = [s for s in steps if s.action == "retry"]
    assert len(retries) == 1
    assert retries[0].payload["source_op"] == "patch"
    assert retries[0].payload["restore"] == "updated"


def test_diff_create_step_for_creating_row_resumes(tmp_path):
    item = make_item()
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(
        make_row(
            task_id=None,
            state=SyncState.CREATING,
            fingerprint=item.content_fingerprint(),
        )
    )
    steps, plan = engine._diff([item], covered_ok=True, now=NOW)
    assert any(s.action == "resume" for s in steps)
    assert any(p.action == "resume" for p in plan)


# -- diff: close policy gated on coverage and due dates ----------------------


def test_close_not_fired_when_uncovered(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(make_row(due="2026-09-01"))
    steps, _ = engine._diff([], covered_ok=False, now=NOW)
    assert not any(s.action in ("close", "delete") for s in steps)


def test_close_fired_when_covered_and_past_grace(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(make_row(due="2026-09-01"))
    steps, plan = engine._diff([], covered_ok=True, now=NOW)
    closes = [s for s in steps if s.action == "close"]
    assert len(closes) == 1
    assert any(p.action == "close" for p in plan)


def test_close_not_fired_when_due_ahead(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(make_row(due="2026-10-01"))
    steps, _ = engine._diff([], covered_ok=True, now=NOW)
    assert not any(s.action in ("close", "delete") for s in steps)


def test_close_never_for_deferred_or_creating_absent(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(make_row(due="2026-09-01", state=SyncState.DEFERRED, task_id=None))
    store.upsert_row(
        make_row(
            timelineid=200, due="2026-09-01", state=SyncState.CREATING, task_id=None
        )
    )
    steps, _ = engine._diff([], covered_ok=True, now=NOW)
    assert not any(s.action in ("close", "delete") for s in steps)


def test_close_no_due_fallback_after_window_plus_grace(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(make_row(due=None, last_seen="2026-08-01T00:00:00Z"))
    steps, _ = engine._diff([], covered_ok=True, now=NOW)
    assert any(s.action == "close" for s in steps)


def test_close_no_due_fallback_not_until_deadline(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.upsert_row(
        make_row(
            due=None,
            last_seen=(NOW - timedelta(days=36)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
    )
    steps, _ = engine._diff([], covered_ok=True, now=NOW)
    assert not any(s.action == "close" for s in steps)


# -- apply: create-safe, dry run, render, retry, close ----------------------


def test_apply_create_writes_intent_before_post(tmp_path):
    item = make_item()
    spy = {"state": None}

    def on_create():
        spy["state"] = store.get_row("u1", item.timelineid).sync_state
        return spy["state"]

    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    vikunja = FakeVikunja(on_create=on_create)
    edupage = FakeEduPage()
    engine = SyncEngine(
        store,
        vikunja,
        edupage,
        make_config(),
        project_id=12,
        userid="u1",
        school_year=2026,
        dbi={},
        subdomain="gym",
    )
    step = PlannedStep(
        "create",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    engine._apply([step], NOW, dry_run=False)
    assert spy["state"] == SyncState.CREATING
    assert any(call[0] == "create_task" for call in vikunja.calls)
    row = store.get_row("u1", item.timelineid)
    assert row.sync_state == SyncState.CREATED
    assert row.vikunja_task_id is not None
    assert row.anchor_label_id is None
    assert row.content_fingerprint == item.content_fingerprint()
    assert "<!-- edupage-key:u1:123 -->" in vikunja._find(row.vikunja_task_id).description
    assert not any(c[0] == "ensure_label" and c[1] == "edu:u1:123" for c in vikunja.calls)
    assert any(c[0] == "ensure_label" and c[1] == "MAT" for c in vikunja.calls)
    store.close()


def test_apply_dry_run_makes_zero_write_calls(tmp_path):
    item = make_item()
    engine, store, vikunja, _ = make_engine(tmp_path)
    step = PlannedStep(
        "create",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    deferred, closed, errors, applied = engine._apply([step], NOW, dry_run=True)
    assert vikunja.calls == []
    assert store.rows() == []
    assert (deferred, closed, errors) == (0, 0, 0)
    assert applied == []


def test_apply_patch_sends_rendered_fields(tmp_path):
    item = make_item()
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="old",
        description="",
        due_date=None,
        priority=0,
        done=False,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(tmp_path, tasks=[fake_task])
    store.upsert_row(make_row(fingerprint="old"))
    step = PlannedStep(
        "patch",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    engine._apply([step], NOW, dry_run=False)
    patches = [c for c in vikunja.calls if c[0] == "patch_task"]
    assert len(patches) == 1
    _, task_id, kwargs = patches[0]
    assert task_id == 7
    assert kwargs["title"] == "Fr. equations"
    assert "<!-- edupage-key:u1:123 -->" in kwargs["description"]
    assert (
        "[Open in EduPage](https://gym.edupage.org/timeline/?timelineid=123#item-123)"
        in kwargs["description"]
    )
    assert kwargs["due_date"] == "2026-09-14T23:59:59Z"
    row = store.get_row("u1", item.timelineid)
    assert row.sync_state == SyncState.UPDATED
    assert row.content_fingerprint == item.content_fingerprint()


def test_apply_deferred_retry_restores_state_and_resets_counter(tmp_path):
    item = make_item()
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="old",
        description="",
        due_date=None,
        priority=0,
        done=False,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(tmp_path, tasks=[fake_task])
    op = {"action": "patch", "restore": "updated", "item": engine._item_fields(item)}
    store.upsert_row(
        make_row(
            fingerprint="old-fingerprint",
            state=SyncState.DEFERRED,
            pending=json.dumps([op]),
            retry=2,
        )
    )
    step = PlannedStep(
        "retry",
        payload={
            "timelineid": item.timelineid,
            "item": engine._item_fields(item),
            "restore": "updated",
            "source_op": "patch",
        },
    )
    engine._apply([step], NOW, dry_run=False)
    assert any(c[0] == "patch_task" for c in vikunja.calls)
    row = store.get_row("u1", item.timelineid)
    assert row.sync_state == SyncState.UPDATED
    assert row.retry_count == 0
    assert row.pending_ops == "[]"
    assert row.content_fingerprint == item.content_fingerprint()


def test_apply_close_sets_done_and_closes_row(tmp_path):
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="old",
        description="",
        due_date=None,
        priority=0,
        done=False,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(tmp_path, tasks=[fake_task])
    row = make_row(due="2026-09-01")
    store.upsert_row(row)
    step = PlannedStep(
        "close", payload={"policy": "close", "row": row, "timelineid": row.timelineid}
    )
    deferred, closed, errors, _ = engine._apply([step], NOW, dry_run=False)
    assert (deferred, closed, errors) == (0, 1, 0)
    assert any(c[0] == "patch_task" and c[2]["done"] is True for c in vikunja.calls)
    updated = store.get_row("u1", row.timelineid)
    assert updated.sync_state == SyncState.CLOSED
    assert updated.closed_at == "2026-09-21T10:00:00Z"


# -- run: full cycle --------------------------------------------------------


def test_run_end_to_end_create(tmp_path):
    item = make_item()
    engine, store, _, _ = make_engine(tmp_path, login=[item])
    result = engine.run()
    assert result.items_fetched == 1
    assert result.covered_ok is True
    assert result.deferred == 0
    assert result.closed == 0
    assert result.errors == 0
    assert any(p.action == "create" for p in result.applied)
    row = store.get_row("u1", item.timelineid)
    assert row is not None
    assert row.sync_state == SyncState.CREATED
    assert row.vikunja_task_id is not None
    assert store.get_meta("last_sync_ts") is not None
    assert store.get_meta_int("breaker_strikes", -1) == 0


def test_run_removed_item_with_past_due_closes_when_covered(tmp_path):
    item = make_item(due_date="2026-09-01", removed=True)
    anchor = VikLabel(id=1, title="edu:u1:123")
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="MAT · Fr. equations",
        description="",
        due_date=None,
        priority=0,
        done=False,
        labels=[anchor],
        created="2026-01-01T00:00:00Z",
    )
    engine, store, _, _ = make_engine(tmp_path, login=[item], tasks=[fake_task])
    store.upsert_row(
        make_row(
            fingerprint=item.content_fingerprint(), due="2026-09-01", done_state=False
        )
    )
    result = engine.run()
    assert any(p.action == "close" for p in result.applied)
    assert result.closed == 1
    assert store.get_row("u1", item.timelineid).sync_state == SyncState.CLOSED


def test_run_breaker_open_performs_deferred_only_pass(tmp_path):
    item = make_item()
    engine, store, vikunja, _ = make_engine(tmp_path, login=[item])
    store.set_meta("breaker_strikes", "7")
    result = engine.run()
    assert result.breaker_open is True
    assert not any(c[0] == "create_task" for c in vikunja.calls)
    assert store.rows() == []
    assert store.get_meta_int("breaker_strikes", -1) == 0  # successful pass resets
    # §9: a deferred-only (breaker) pass must not masquerade as a heartbeat
    assert store.get_meta("last_sync_ts") is None


def test_next_run_and_should_run(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    assert engine.should_run() is True  # never ran
    store.set_meta("last_sync_ts", "2026-09-21T08:00:00Z")
    assert engine.next_run(30) == datetime(2026, 9, 21, 8, 30, tzinfo=UTC)
    assert engine.should_run(now=datetime(2026, 9, 21, 8, 30, tzinfo=UTC)) is True
    assert engine.should_run(now=datetime(2026, 9, 21, 8, 29, tzinfo=UTC)) is False


# -- diff: payloads carry the item's timelineid -----------------------------


def test_diff_payloads_carry_timelineid(tmp_path):
    item = make_item()
    engine, store, _, _ = make_engine(tmp_path)
    steps, _ = engine._diff([item], covered_ok=True, now=NOW)
    creates = [s for s in steps if s.action == "create"]
    assert len(creates) == 1
    assert creates[0].payload["timelineid"] == 123
    assert isinstance(creates[0].payload["timelineid"], int)

    op = {"action": "patch", "restore": "updated", "item": engine._item_fields(item)}
    store.upsert_row(
        make_row(
            fingerprint="old",
            state=SyncState.DEFERRED,
            pending=json.dumps([op]),
            retry=2,
        )
    )
    steps2, _ = engine._diff([item], covered_ok=True, now=NOW)
    retries = [s for s in steps2 if s.action == "retry"]
    assert len(retries) == 1
    assert retries[0].payload["timelineid"] == 123
    assert isinstance(retries[0].payload["timelineid"], int)


# -- apply: create-safe probe also on the missing-row path ------------------


def test_apply_create_probes_anchor_on_missing_row(tmp_path):
    item = make_item()
    preexisting = VikTask(
        id=50,
        project_id=12,
        title="MAT · Fr. equations",
        description="past body\n\n<!-- edupage-timelineid:123 -->",
        due_date=None,
        priority=0,
        done=True,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(tmp_path, tasks=[preexisting])
    step = PlannedStep(
        "create",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    deferred, closed, errors, applied = engine._apply([step], NOW, dry_run=False)
    assert (deferred, closed, errors) == (0, 0, 0)
    assert not any(c[0] == "create_task" for c in vikunja.calls)
    assert any(p.action == "create" for p in applied)
    row = store.get_row("u1", item.timelineid)
    assert row is not None
    assert row.vikunja_task_id == 50
    assert row.sync_state == SyncState.CREATED
    assert any(c[0] == "attach_label" and c[1] == 50 for c in vikunja.calls)


# -- apply: a gone task on close/delete must not crash the pass -------------


def test_close_gone_task_does_not_crash_pass(tmp_path):
    engine, store, vikunja, _ = make_engine(tmp_path)
    row = make_row(task_id=77, due="2026-09-01")
    store.upsert_row(row)
    step = PlannedStep(
        "close", payload={"policy": "close", "row": row, "timelineid": row.timelineid}
    )
    deferred, closed, errors, applied = engine._apply([step], NOW, dry_run=False)
    assert (deferred, closed, errors) == (0, 0, 0)
    assert applied == []
    assert any(c[0] == "patch_task" and c[1] == 77 for c in vikunja.calls)
    surviving = store.get_row("u1", row.timelineid)
    assert surviving is not None
    assert surviving.sync_state == SyncState.CREATED  # left for the reconciler


def test_delete_gone_task_does_not_crash_pass(tmp_path):
    engine, store, vikunja, _ = make_engine(tmp_path)
    row = make_row(task_id=88, due="2026-09-01")
    store.upsert_row(row)
    step = PlannedStep(
        "delete", payload={"policy": "delete", "row": row, "timelineid": row.timelineid}
    )
    deferred, closed, errors, _ = engine._apply([step], NOW, dry_run=False)
    assert (deferred, closed, errors) == (0, 0, 0)
    assert any(c[0] == "delete_task" and c[1] == 88 for c in vikunja.calls)
    assert store.get_row("u1", row.timelineid) is not None


# -- apply: due_date is preserved when it did not change --------------------


def test_apply_patch_preserves_due_when_unchanged(tmp_path):
    item = make_item(due_date="2026-09-14")
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="old",
        description="",
        due_date="2026-09-14T23:59:59Z",
        priority=0,
        done=False,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(tmp_path, tasks=[fake_task])
    store.upsert_row(make_row(fingerprint="old", due="2026-09-14"))
    step = PlannedStep(
        "patch",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    engine._apply([step], NOW, dry_run=False)
    patches = [c for c in vikunja.calls if c[0] == "patch_task"]
    assert len(patches) == 1
    _, task_id, kwargs = patches[0]
    assert task_id == 7
    assert "due_date" not in kwargs
    assert kwargs["title"] == "Fr. equations"


def test_apply_patch_sends_due_when_changed(tmp_path):
    item = make_item(due_date="2026-09-14")
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="old",
        description="",
        due_date=None,
        priority=0,
        done=False,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(tmp_path, tasks=[fake_task])
    store.upsert_row(make_row(fingerprint="old", due="2026-02-01"))
    step = PlannedStep(
        "patch",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    engine._apply([step], NOW, dry_run=False)
    patches = [c for c in vikunja.calls if c[0] == "patch_task"]
    assert patches[0][2]["due_date"] == "2026-09-14T23:59:59Z"


# -- default_bucket: title resolved and cached -------------------------------


def test_default_bucket_title_resolved_and_cached(tmp_path):
    item = make_item()
    fake_task = VikTask(
        id=7,
        project_id=12,
        title="old",
        description="",
        due_date=None,
        priority=0,
        done=False,
        created="2026-01-01T00:00:00Z",
    )
    engine, store, vikunja, _ = make_engine(
        tmp_path, tasks=[fake_task], default_bucket="Inbox"
    )
    store.upsert_row(make_row(fingerprint="old"))
    step = PlannedStep(
        "patch",
        payload={"timelineid": item.timelineid, "item": engine._item_fields(item)},
    )
    engine._apply([step], NOW, dry_run=False)
    engine._apply([step], NOW, dry_run=False)
    patches = [c for c in vikunja.calls if c[0] == "patch_task"]
    assert len(patches) == 2
    assert all(p[2]["bucket_id"] == 11 for p in patches)
    resolves = [c for c in vikunja.calls if c[0] == "resolve_bucket_id"]
    assert resolves == [("resolve_bucket_id", 12, "Inbox")]


# -- anchor probe: denylisted tasks are skipped -----------------------------


def test_ensure_anchor_skips_denylisted_task(tmp_path):
    item = make_item()
    anchor = VikLabel(id=1, title="edu:u1:123")
    loser = VikTask(
        id=8,
        project_id=12,
        title="MAT · Fr. equations",
        description="",
        due_date=None,
        priority=0,
        done=True,
        labels=[anchor],
        created="2026-01-02T00:00:00Z",
    )
    winner = VikTask(
        id=7,
        project_id=12,
        title="MAT · Fr. equations",
        description="",
        due_date=None,
        priority=0,
        done=False,
        labels=[anchor],
        created="2026-01-01T00:00:00Z",
    )
    engine, _, _, _ = make_engine(tmp_path, tasks=[loser, winner])
    engine.reconciler.run(dry_run=True)
    found = engine._ensure_anchor(item)
    assert found is not None
    assert engine._task_id(found) == 7


# -- fetch coverage: fallback validation + full refetch ---------------------


def test_run_fallback_uncovered_refetches_history(tmp_path):
    item = make_item()
    engine, store, _, edupage = make_engine(
        tmp_path, login=[item], history_fail=True
    )
    store.upsert_row(make_row(timelineid=200, due="2026-09-01"))
    result = engine.run()
    assert result.covered_ok is False
    assert result.closed == 0
    assert not any(p.action in ("close", "delete") for p in result.applied)
    fh_calls = [c for c in edupage.calls if c[0] == "fetch_history"]
    assert len(fh_calls) == 2  # initial attempt + full refetch
    assert store.get_meta("covered_ok") == "0"


def test_run_fallback_salvaged_by_full_refetch(tmp_path):
    item = make_item()
    old_item = make_item(
        timelineid=456,
        timestamp=(
            datetime.now(tz=None) - timedelta(days=30) + timedelta(minutes=20)
        ).strftime("%Y-%m-%d %H:%M:%S"),
        due_date="2026-09-01",
    )
    engine, store, _, edupage = make_engine(
        tmp_path, login=[item], history_fail_times=1, history_items=[old_item]
    )
    result = engine.run()
    assert result.covered_ok is True
    fh_calls = [c for c in edupage.calls if c[0] == "fetch_history"]
    assert len(fh_calls) == 2


# -- circuit breaker: reset when errors == 0 even with deferred rows --------


def test_finish_pass_resets_breaker_with_deferred_only(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.set_meta("breaker_strikes", "7")
    engine._finish_pass(deferred=3, errors=0, now=NOW)
    assert store.get_meta_int("breaker_strikes", -1) == 0
    assert store.get_meta("last_sync_ts") is not None


def test_finish_pass_keeps_strikes_while_errors(tmp_path):
    engine, store, _, _ = make_engine(tmp_path)
    store.set_meta("breaker_strikes", "7")
    engine._finish_pass(deferred=0, errors=1, now=NOW, deferred_only=True)
    assert store.get_meta_int("breaker_strikes", -1) == 7
    assert store.get_meta("last_sync_ts") is None
