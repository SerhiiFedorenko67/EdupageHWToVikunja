"""Tests for the Vikunja v2 client (no network: mocks only)."""

from __future__ import annotations

import json

import pytest
import requests

from edupagetasks.config import VikunjaConfig
from edupagetasks.vikunja import (
    VikLabel,
    VikTask,
    VikunjaAuthError,
    VikunjaClient,
    VikunjaError,
    VikunjaNotFoundError,
    VikunjaPermissionError,
    VikunjaTransientError,
)

BASE = "http://vikunja.example/api/v2"

PROBLEM_404 = {
    "type": "about:blank",
    "title": "The requested resource could not be found",
    "status": 404,
    "code": 1501,
    "detail": "task 99 not found",
}


class FakeResp:
    def __init__(self, status_code: int = 200, json_data=None, text: str = ""):
        self.status_code = status_code
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json


def make_client(**cfg) -> VikunjaClient:
    defaults = {"base_url": BASE, "token": "tk_testtoken", "project": 7}
    defaults.update(cfg)
    return VikunjaClient(VikunjaConfig(**defaults))


def _task(pk: int, title: str, **extra) -> dict:
    body = {
        "id": pk,
        "project_id": 7,
        "title": title,
        "labels": [],
        "done": False,
    }
    body.update(extra)
    return body


def assert_auth(headers: dict) -> dict:
    assert headers["Authorization"] == "Bearer tk_testtoken"
    assert headers["Accept"] == "application/json"
    return headers


def test_auth_header_on_every_call(mocker):
    client = make_client()
    seen: list[tuple[str, str, dict]] = []

    def handler(method, url, **kwargs):
        seen.append((method, url, kwargs.get("headers") or {}))
        if method == "GET" and url == f"{BASE}/info":
            return FakeResp(json_data={})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    client.info()
    assert seen
    for _, _, headers in seen:
        assert_auth(headers)


def test_list_tasks_walks_pages(mocker):
    client = make_client()
    calls: list[tuple[str, str, dict]] = []

    def handler(method, url, **kwargs):
        params = kwargs.get("params") or {}
        calls.append((method, url, params))
        assert_auth(kwargs.get("headers") or {})
        if method == "GET" and url == f"{BASE}/info":
            return FakeResp(json_data={"max_items_per_page": 50})
        if method == "GET" and url == f"{BASE}/projects/7/tasks":
            page = params.get("page")
            if page == 1:
                return FakeResp(
                    json_data={
                        "items": [_task(1, "one"), _task(2, "two")],
                        "page": 1,
                        "per_page": 50,
                        "total": 3,
                        "total_pages": 2,
                    }
                )
            if page == 2:
                return FakeResp(
                    json_data={
                        "items": [_task(3, "three")],
                        "page": 2,
                        "per_page": 50,
                        "total": 3,
                        "total_pages": 2,
                    }
                )
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    tasks = client.list_tasks(7)
    assert isinstance(tasks, list) and all(isinstance(t, VikTask) for t in tasks)
    assert [t.id for t in tasks] == [1, 2, 3]
    assert tasks[0].description is None
    list_calls = [params for m, u, params in calls if u.endswith("/projects/7/tasks")]
    assert len(list_calls) == 2
    assert list_calls[0]["page"] == 1
    assert list_calls[1]["page"] == 2
    assert list_calls[0]["per_page"] == 50
    assert list_calls[0]["format"] == "markdown"


def test_create_task_posts_to_project_with_markdown(mocker):
    client = make_client()
    seen: dict = {}

    def handler(method, url, **kwargs):
        if method == "POST" and url == f"{BASE}/projects/7/tasks":
            assert_auth(kwargs.get("headers") or {})
            seen["params"] = kwargs.get("params")
            seen["json"] = kwargs.get("json")
            return FakeResp(
                status_code=201, json_data={"id": 42, "title": "hw", "project_id": 7}
            )
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    task_id = client.create_task(
        7,
        title="hw",
        description="body",
        due_date="2026-09-20",
        priority=2,
        bucket_id=5,
    )
    assert task_id == 42
    assert seen["params"] == {"format": "markdown"}
    assert seen["json"] == {
        "title": "hw",
        "description": "body",
        "due_date": "2026-09-20",
        "priority": 2,
        "bucket_id": 5,
    }


def test_patch_task_header_and_only_supplied_fields(mocker):
    client = make_client()
    seen: list[dict] = []

    def handler(method, url, **kwargs):
        if method == "PATCH" and url == f"{BASE}/tasks/9":
            assert_auth(kwargs.get("headers") or {})
            assert kwargs["headers"]["X-Vikunja-Format"] == "markdown"
            seen.append(kwargs.get("json"))
            return FakeResp(json_data={})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    client.patch_task(9, done=True, due_date=None)
    client.patch_task(9, title="new title")
    assert seen[0] == {"due_date": None, "done": True}
    assert seen[1] == {"title": "new title"}
    assert "description" not in seen[0]
    assert "priority" not in seen[1]


def test_patch_task_no_markdown_header_when_false(mocker):
    client = make_client()
    headers_seen: dict = {}

    def handler(method, url, **kwargs):
        if method == "PATCH" and url == f"{BASE}/tasks/9":
            headers_seen.update(kwargs.get("headers") or {})
            return FakeResp(json_data={})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    client.patch_task(9, due_date=None, markdown=False)
    assert "X-Vikunja-Format" not in headers_seen


def test_replace_labels_body_shape(mocker):
    client = make_client()
    seen: dict = {}

    def handler(method, url, **kwargs):
        if method == "PUT" and url == f"{BASE}/tasks/9/labels/bulk":
            assert_auth(kwargs.get("headers") or {})
            seen["json"] = kwargs.get("json")
            return FakeResp(json_data={})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    client.replace_labels(9, [3, 5, 9])
    assert seen["json"] == {"labels": [{"id": 3}, {"id": 5}, {"id": 9}]}


def test_get_task_404_returns_none(mocker):
    client = make_client()
    mocker.patch.object(
        client.session,
        "request",
        return_value=FakeResp(
            status_code=404, json_data=PROBLEM_404, text=json.dumps(PROBLEM_404)
        ),
    )
    assert client.get_task(99) is None


def test_patch_task_404_raises_not_found(mocker):
    client = make_client()
    mocker.patch.object(
        client.session,
        "request",
        return_value=FakeResp(
            status_code=404, json_data=PROBLEM_404, text=json.dumps(PROBLEM_404)
        ),
    )
    with pytest.raises(VikunjaNotFoundError) as excinfo:
        client.patch_task(99, title="x")
    assert excinfo.value.status == 404
    assert excinfo.value.code == 1501
    assert excinfo.value.detail == "task 99 not found"


def test_429_raises_transient(mocker):
    client = make_client()
    problem = {"status": 429, "code": 429, "title": "Too Many Requests"}
    mocker.patch.object(
        client.session,
        "request",
        return_value=FakeResp(
            status_code=429, json_data=problem, text=json.dumps(problem)
        ),
    )
    with pytest.raises(VikunjaTransientError):
        client.create_task(7, title="x")


def test_401_raises_auth_error(mocker):
    client = make_client()
    problem = {"status": 401, "code": 1001, "title": "Unauthorized"}
    mocker.patch.object(
        client.session,
        "request",
        return_value=FakeResp(
            status_code=401, json_data=problem, text=json.dumps(problem)
        ),
    )
    with pytest.raises(VikunjaAuthError) as excinfo:
        client.info()
    assert excinfo.value.code == 1001
    assert excinfo.value.status == 401


def test_connection_error_is_transient(mocker):
    client = make_client()
    mocker.patch.object(
        client.session,
        "request",
        side_effect=requests.exceptions.ConnectionError("boom"),
    )
    with pytest.raises(VikunjaTransientError):
        client.info()


def test_ensure_project_numeric_ok(mocker):
    client = make_client(project=7)
    seen: list[tuple[str, str]] = []

    def handler(method, url, **kwargs):
        seen.append((method, url))
        assert_auth(kwargs.get("headers") or {})
        if method == "GET" and url == f"{BASE}/projects/7":
            return FakeResp(json_data={"id": 7, "title": "School", "max_permission": 2})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    assert client.ensure_project() == 7


def test_ensure_project_numeric_read_write_ok(mocker):
    client = make_client(project=7)

    def handler(method, url, **kwargs):
        assert_auth(kwargs.get("headers") or {})
        if method == "GET" and url == f"{BASE}/projects/7":
            return FakeResp(json_data={"id": 7, "title": "School", "max_permission": 1})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    assert client.ensure_project() == 7


def test_ensure_project_numeric_read_only_raises_permission(mocker):
    client = make_client(project=7)

    def handler(method, url, **kwargs):
        if method == "GET" and url == f"{BASE}/projects/7":
            return FakeResp(json_data={"id": 7, "title": "School", "max_permission": 0})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    with pytest.raises(VikunjaPermissionError) as excinfo:
        client.ensure_project()
    assert "max_permission" in str(excinfo.value)


def test_resolve_project_numeric_create_on_missing(mocker):
    client = make_client(project=9, create_project=True)
    seen: list[tuple[str, str, dict | None]] = []

    def handler(method, url, **kwargs):
        seen.append((method, url, kwargs.get("json")))
        if method == "GET" and url == f"{BASE}/projects/9":
            return FakeResp(
                status_code=404, json_data=PROBLEM_404, text=json.dumps(PROBLEM_404)
            )
        if method == "POST" and url == f"{BASE}/projects":
            return FakeResp(
                status_code=201,
                json_data={"id": 9, "title": "9", "max_permission": 2},
            )
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    assert client.resolve_project() == 9
    assert seen[0] == ("GET", f"{BASE}/projects/9", None)
    assert seen[1] == ("POST", f"{BASE}/projects", {"title": "9"})


def test_resolve_project_by_title(mocker):
    client = make_client(project="School")

    def handler(method, url, **kwargs):
        assert_auth(kwargs.get("headers") or {})
        if method == "GET" and url == f"{BASE}/projects":
            assert kwargs.get("params") == {"q": "School"}
            return FakeResp(json_data={"items": [{"id": 7, "title": "School"}]})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    assert client.resolve_project() == 7


def test_resolve_project_title_missing_raises(mocker):
    client = make_client(project="Nope")

    def handler(method, url, **kwargs):
        if method == "GET" and url == f"{BASE}/projects":
            return FakeResp(json_data={"items": [], "total_pages": 0})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    with pytest.raises(VikunjaError) as excinfo:
        client.resolve_project()
    assert "Nope" in str(excinfo.value)


def test_ensure_label_found_and_created(mocker):
    client = make_client()
    created: list[dict] = []

    def handler(method, url, **kwargs):
        if method == "GET" and url == f"{BASE}/labels":
            if created:
                return FakeResp(json_data={"items": [{"id": 5, "title": "edupage"}]})
            return FakeResp(json_data={"items": []})
        if method == "POST" and url == f"{BASE}/labels":
            created.append(kwargs.get("json") or {})
            return FakeResp(status_code=201, json_data={"id": 5, "title": "edupage"})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    first = client.ensure_label("edupage")
    second = client.ensure_label("edupage")
    assert isinstance(first, VikLabel) and first.id == 5
    assert first.title == "edupage"
    assert second.id == 5
    assert created == [{"title": "edupage"}]


def test_viktask_from_json_preserves_markdown_fields(mocker):
    client = make_client()
    mocker.patch.object(
        client.session,
        "request",
        return_value=FakeResp(
            json_data=_task(
                3,
                "hw",
                description="**Assignment:** x\n\n<!-- edupage-timelineid:42 -->",
                due_date="2026-09-20T23:59:59Z",
                priority=1,
                bucket_id=4,
                created="2026-09-10T08:00:00Z",
                index=3,
                identifier="SCHOOL-3",
            )
        ),
    )
    task = client.get_task(3)
    assert task is not None
    assert task.id == 3
    assert task.description == "**Assignment:** x\n\n<!-- edupage-timelineid:42 -->"
    assert task.due_date == "2026-09-20T23:59:59Z"
    assert task.priority == 1
    assert task.bucket_id == 4
    assert task.identifier == "SCHOOL-3"


def test_resolve_bucket_id_by_title(mocker):
    client = make_client()
    seen: list[tuple[str, str]] = []

    def handler(method, url, **kwargs):
        seen.append((method, url))
        assert_auth(kwargs.get("headers") or {})
        if method == "GET" and url == f"{BASE}/projects/7/views":
            return FakeResp(
                json_data={"items": [{"id": 12, "title": "Board"}, {"id": 13, "title": "Done"}]}
            )
        if method == "GET" and url == f"{BASE}/views/12/buckets":
            return FakeResp(
                json_data={"items": [{"id": 55, "title": "Inbox"}, {"id": 56, "title": "Later"}]}
            )
        if method == "GET" and url == f"{BASE}/views/13/buckets":
            return FakeResp(json_data={"items": []})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    assert client.resolve_bucket_id(7, "Inbox") == 55


def test_resolve_bucket_id_int_passthrough(mocker):
    client = make_client()
    mocker.patch.object(client.session, "request", side_effect=AssertionError)
    assert client.resolve_bucket_id(7, 55) == 55


def test_resolve_bucket_id_title_missing_raises(mocker):
    client = make_client()

    def handler(method, url, **kwargs):
        if method == "GET" and url == f"{BASE}/projects/7/views":
            return FakeResp(json_data={"items": []})
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    with pytest.raises(VikunjaError) as excinfo:
        client.resolve_bucket_id(7, "Nope")
    assert "Nope" in str(excinfo.value)


def test_repr_hides_token():
    client = make_client()
    assert "tk_testtoken" not in repr(client)
    assert BASE in repr(client)


def test_is_transient_classifier():
    assert VikunjaError.is_transient(status=500)
    assert VikunjaError.is_transient(status=502)
    assert VikunjaError.is_transient(status=429)
    assert not VikunjaError.is_transient(status=404)
    assert not VikunjaError.is_transient(status=403)
    assert not VikunjaError.is_transient(status=200)
