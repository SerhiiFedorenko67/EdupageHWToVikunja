import pytest

from edupagetasks.models import MapRow, SyncState
from edupagetasks.reconcile import UNSET_FINGERPRINT, Reconciler
from edupagetasks.state import StateStore
from edupagetasks.vikunja import VikLabel, VikTask, VikunjaGoneError


def make_row(
    *,
    timelineid: int = 1001,
    task_id: int | None = 7,
    state: str = SyncState.CREATED,
    fingerprint: str = "fp1",
    pending: str = "[]",
    retry: int = 0,
    userid: str = "u1",
) -> MapRow:
    return MapRow(
        userid=userid,
        timelineid=timelineid,
        vikunja_task_id=task_id,
        vikunja_project_id=12,
        content_fingerprint=fingerprint,
        done_state=False,
        school_year=2026,
        last_seen_due_date="2026-02-01",
        last_seen_at="2026-01-01T00:00:00Z",
        closed_at=None,
        pending_ops=pending,
        retry_count=retry,
        sync_state=state,
        anchor_label_id=3,
        updated_at="2026-01-01T00:00:00Z",
    )


def anchor(userid: str, timelineid: int, label_id: int) -> VikLabel:
    return VikLabel(id=label_id, title=f"edu:{userid}:{timelineid}")


def task(
    task_id: int,
    title: str,
    *,
    created: str,
    labels: list[VikLabel] | None = None,
    description: str = "body",
    done: bool = False,
) -> VikTask:
    return VikTask(
        id=task_id,
        project_id=12,
        title=title,
        description=description,
        labels=list(labels or []),
        created=created,
        done=done,
    )


class FakeVikunja:
    def __init__(self, tasks: list[VikTask] | None = None) -> None:
        self.tasks = list(tasks or [])
        self.strip_calls: list[tuple[int, dict]] = []

    def list_tasks(self, project_id: int) -> list[VikTask]:
        return [t for t in self.tasks if t.project_id == project_id]

    def patch_task(self, task_id: int, **kwargs) -> VikTask:
        self.strip_calls.append((task_id, dict(kwargs)))
        task = next((t for t in self.tasks if t.id == task_id), None)
        if task is None:
            raise VikunjaGoneError(f"task {task_id} gone")
        if kwargs.get("done") is not None:
            task.done = bool(kwargs["done"])
        if kwargs.get("description") is not None:
            task.description = kwargs["description"]
        if kwargs.get("labels") is not None:
            by_id = {
                l.id
                for l in [
                    VikLabel(id=100, title="edupage"),
                    VikLabel(id=101, title="MAT"),
                ]
            }
            labels = []
            for lid in kwargs["labels"]:
                if lid in by_id:
                    labels.append(VikLabel(id=lid, title=f"label-{lid}"))
            task.labels = labels
        return task


@pytest.fixture()
def store(tmp_path):
    s = StateStore(str(tmp_path / "state.db"), project_default=12)
    yield s
    s.close()


def test_duplicate_anchor_loser_stripped_and_denylisted(store):
    label = anchor("u1", 1001, 1)
    loser = task(
        1,
        "MAT · Fr. equations",
        created="2026-01-02T00:00:00Z",
        description="body\n\n<!-- edupage-timelineid:1001 -->",
        labels=[label],
    )
    winner = task(
        2,
        "MAT · Fr. equations",
        created="2026-01-01T00:00:00Z",
        description="body\n\n<!-- edupage-timelineid:1001 -->",
        labels=[label],
    )
    reconciler = Reconciler(
        store, FakeVikunja([loser, winner]), project_id=12, userid="u1"
    )
    result = reconciler.run()
    assert result.denylisted == [1]
    assert result.duplicates_handled == 1
    assert loser.done is True
    assert "edupage-timelineid" not in loser.description
    assert all(l.title != "edu:u1:1001" for l in loser.labels)
    row = store.get_row("u1", 1001)
    assert row.vikunja_task_id == 2
    assert row.sync_state == SyncState.CLOSED
    assert row.content_fingerprint == UNSET_FINGERPRINT
    assert row.anchor_label_id == 1


def test_vanished_task_prunes_row(store):
    store.upsert_row(make_row(task_id=7))
    reconciler = Reconciler(store, FakeVikunja([]), project_id=12, userid="u1")
    result = reconciler.run()
    assert result.pruned == 1
    assert store.get_row("u1", 1001) is None
    assert store.rows() == []


def test_wiped_db_reseeded_from_anchors_fresh(store):
    first = task(1, "A", created="2026-01-01T00:00:00Z", labels=[anchor("u1", 1001, 1)])
    second = task(
        2, "B", created="2026-01-01T00:00:00Z", labels=[anchor("u1", 2002, 2)]
    )
    reconciler = Reconciler(
        store, FakeVikunja([first, second]), project_id=12, userid="u1"
    )
    result = reconciler.run()
    assert result.fresh is True
    assert result.adopted == 2
    assert result.pruned == 0
    rows = store.rows()
    assert {r.timelineid for r in rows} == {1001, 2002}
    for row in rows:
        assert row.sync_state == SyncState.CLOSED
        assert row.content_fingerprint == UNSET_FINGERPRINT
        assert row.vikunja_task_id is not None


def test_deferred_row_with_vanished_task_reset_to_created(store):
    store.upsert_row(
        make_row(
            task_id=7,
            state=SyncState.DEFERRED,
            pending='[{"action": "patch", "item": {}}]',
            retry=2,
        )
    )
    reconciler = Reconciler(store, FakeVikunja([]), project_id=12, userid="u1")
    result = reconciler.run()
    assert result.pruned == 0
    row = store.get_row("u1", 1001)
    assert row is not None
    assert row.sync_state == SyncState.CREATED
    assert row.pending_ops == "[]"
    assert row.retry_count == 0
    assert row.vikunja_task_id is None


def test_creating_row_with_vanished_task_reset_not_deleted(store):
    store.upsert_row(make_row(task_id=None, state=SyncState.CREATING, pending="[]"))
    reconciler = Reconciler(store, FakeVikunja([]), project_id=12, userid="u1")
    result = reconciler.run()
    assert result.pruned == 0
    assert store.get_row("u1", 1001).sync_state == SyncState.CREATED


def test_dry_run_makes_no_writes(store):
    store.upsert_row(make_row(task_id=7))
    loser = task(1, "L", created="2026-01-02T00:00:00Z", labels=[anchor("u1", 1001, 1)])
    winner = task(
        2, "W", created="2026-01-01T00:00:00Z", labels=[anchor("u1", 1001, 1)]
    )
    fake = FakeVikunja([loser, winner])
    Reconciler(store, fake, project_id=12, userid="u1").run(dry_run=True)
    assert fake.strip_calls == []
    assert store.get_row("u1", 1001).vikunja_task_id == 7


def test_projects_mismatch_treated_as_gone(tmp_path):
    store = StateStore(str(tmp_path / "state.db"), project_default=12)
    other = task(7, "Moved", created="2026-01-01T00:00:00Z")
    other.project_id = 99
    store.upsert_row(make_row(task_id=7))
    Reconciler(store, FakeVikunja([other]), project_id=12, userid="u1").run()
    assert store.get_row("u1", 1001) is None
    store.close()


def test_anchor_pattern_respects_userid_namespace(store):
    label = anchor("u2", 1001, 1)
    foreign = task(7, "Other child", created="2026-01-01T00:00:00Z", labels=[label])
    store.upsert_row(make_row(task_id=8))
    Reconciler(store, FakeVikunja([foreign]), project_id=12, userid="u1").run()
    assert store.get_row("u1", 1001) is None  # pruned: task 8 vanished
    assert store.rows() == []


def test_adopted_done_task_does_not_import_done_bit(store):
    done_task = task(
        1,
        "MAT · Fr. equations",
        created="2026-01-01T00:00:00Z",
        labels=[anchor("u1", 1001, 1)],
        done=True,
    )
    Reconciler(store, FakeVikunja([done_task]), project_id=12, userid="u1").run()
    row = store.get_row("u1", 1001)
    assert row is not None
    assert row.done_state is False
    assert row.sync_state == SyncState.CLOSED


def test_adopted_row_was_patched_and_not_done(store):
    open_task = task(
        1,
        "MAT · Fr. equations",
        created="2026-01-01T00:00:00Z",
        labels=[anchor("u1", 1001, 1)],
        done=False,
    )
    Reconciler(store, FakeVikunja([open_task]), project_id=12, userid="u1").run()
    row = store.get_row("u1", 1001)
    assert row.done_state is False
    assert row.sync_state == SyncState.CLOSED


def test_adopted_due_with_z_suffix_truncated(store):
    from edupagetasks.vikunja import VikTask

    z_task = VikTask(
        id=1,
        project_id=12,
        title="MAT · Fr. equations",
        description="",
        due_date="2026-09-14T23:59:59Z",
        done=False,
        labels=[anchor("u1", 1001, 1)],
        created="2026-01-01T00:00:00Z",
    )
    Reconciler(store, FakeVikunja([z_task]), project_id=12, userid="u1").run()
    row = store.get_row("u1", 1001)
    assert row.last_seen_due_date == "2026-09-14"


def test_denylisted_ids_expose_per_run_denylist(store):
    label = anchor("u1", 1001, 1)
    loser = task(1, "L", created="2026-01-02T00:00:00Z", labels=[label])
    winner = task(2, "W", created="2026-01-01T00:00:00Z", labels=[label])
    fake = FakeVikunja([loser, winner])
    reconciler = Reconciler(store, fake, project_id=12, userid="u1")
    reconciler.run(dry_run=True)
    assert reconciler.denylisted_ids == {1}
