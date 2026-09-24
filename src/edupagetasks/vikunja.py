"""Vikunja v2 API client: tasks, labels, projects and the paginated wrapper.

Implements the on-wire contract pinned by tests/test_vikunja.py: markdown
exchange via ``?format=markdown`` params on list/create and the
``X-Vikunja-Format: markdown`` header on PATCH, pagination from the ``/info``
``max_items_per_page`` hint, RFC7807 ``application/problem+json`` error bodies
with ``status``/``code``/``detail`` surfaced on the raised exception, and whole-
set label replace via ``PUT /tasks/{id}/labels/bulk``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import requests

from edupagetasks.config import VikunjaConfig

logger = logging.getLogger(__name__)

_DEFAULT_PER_PAGE = 50

#: Sentinel distinguishing "field not supplied" from "explicitly set to null":
#: without it every ``patch_task`` call would send ``due_date: null`` and wipe
#: the stored due date even when the caller only wanted to flip ``done``.
_UNSET = object()


class VikunjaError(Exception):
    """Base for all Vikunja client errors."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        code: int | None = None,
        detail: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.detail = detail

    @staticmethod
    def is_transient(status: int) -> bool:
        return status == 429 or status >= 500


class VikunjaTransientError(VikunjaError):
    """Network/429/5xx failure: safe to retry after a backoff."""


class VikunjaAuthError(VikunjaError):
    """Permanent auth failure (token invalid)."""


class VikunjaPermissionError(VikunjaError):
    """Token lacks write permission on the target."""


class VikunjaNotFoundError(VikunjaError):
    """404/410: resource vanished (project missing, task gone...)."""


#: Downstream engine code treats 404/410 as "the task is gone": an alias so it
#: can catch either name and rebuild/deleting the mapping is unambiguous.
VikunjaGoneError = VikunjaNotFoundError


@dataclass
class VikLabel:
    id: int
    title: str
    hex_color: str | None = None
    created: str | None = None


@dataclass
class VikTask:
    id: int
    project_id: int
    title: str
    description: str | None = None
    due_date: str | None = None
    done: bool = False
    priority: int = 0
    bucket_id: int | None = None
    bucket_ids: list[int] = field(default_factory=list)
    labels: list[VikLabel] = field(default_factory=list)
    identifier: str = ""
    created: str | None = None
    updated: str | None = None


class VikunjaClient:
    def __init__(self, cfg: VikunjaConfig, *, timeout: float = 10.0) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Authorization": f"Bearer {cfg.token}", "Accept": "application/json"}
        )

    def __repr__(self) -> str:
        return f"{type(self).__name__}(base_url={self.cfg.base_url!r}, project={self.cfg.project!r})"

    # -- public API --------------------------------------------------------

    def info(self) -> dict[str, Any]:
        return self._json("GET", "/info")

    def list_tasks(self, project_id: int) -> list[VikTask]:
        per_page = self._max_items_per_page()
        page = 1
        tasks: list[VikTask] = []
        while True:
            params: dict[str, Any] = {
                "page": page,
                "per_page": per_page,
                "expand": "buckets",
            }
            if self.cfg.markdown:
                params["format"] = "markdown"
            data = self._json("GET", f"/projects/{project_id}/tasks", params=params)
            page_items, total_pages = self._page_items(data, "tasks")
            for raw in page_items:
                task = self._task_from(raw)
                if task is None:
                    raise VikunjaError("malformed task in paginated list response")
                tasks.append(task)
            if page >= total_pages:
                break
            page += 1
        return tasks

    def get_task(self, task_id: int) -> VikTask | None:
        params = {"format": "markdown"} if self.cfg.markdown else None
        try:
            data = self._json("GET", f"/tasks/{task_id}", params=params)
        except VikunjaNotFoundError:
            return None
        task = self._task_from(data)
        if task is None:
            raise VikunjaError(f"malformed task response for task {task_id}")
        return task

    def create_task(self, project_id: int, **kwargs: Any) -> int:
        params = {"format": "markdown"} if self.cfg.markdown else None
        data = self._json(
            "POST", f"/projects/{project_id}/tasks", params=params, json=dict(kwargs)
        )
        try:
            return int(data["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VikunjaError("malformed create response for task") from exc

    def patch_task(
        self,
        task_id: int,
        *,
        title: str | None = None,
        description: str | None = None,
        due_date: str | None | object = _UNSET,
        priority: int | None = None,
        bucket_id: int | None = None,
        done: bool | None = None,
        labels: list[int] | None = None,
        markdown: bool | None = None,
    ) -> None:
        body: dict[str, Any] = {}
        if title is not None:
            body["title"] = title
        if description is not None:
            body["description"] = description
        if due_date is not _UNSET:
            body["due_date"] = due_date
        if priority is not None:
            body["priority"] = priority
        if bucket_id is not None:
            body["bucket_id"] = bucket_id
        if done is not None:
            body["done"] = done
        headers = {}
        if markdown if markdown is not None else self.cfg.markdown:
            headers["X-Vikunja-Format"] = "markdown"
        self._json("PATCH", f"/tasks/{task_id}", json=body, headers=headers)
        if labels is not None:
            self.replace_labels(task_id, labels)

    def delete_task(self, task_id: int) -> None:
        self._request("DELETE", f"/tasks/{task_id}")

    def list_labels(self) -> list[VikLabel]:
        labels: list[VikLabel] = []
        per_page = self._max_items_per_page()
        page = 1
        while True:
            data = self._json("GET", "/labels", params={"page": page, "per_page": per_page})
            items, total_pages = self._page_items(data, "labels")
            for raw in items:
                label = self._label_from(raw)
                if label is None:
                    raise VikunjaError("malformed label in paginated list response")
                labels.append(label)
            if page >= total_pages:
                break
            page += 1
        return labels

    def ensure_label(self, title: str) -> VikLabel:
        for label in self.list_labels():
            if label.title == title:
                return label
        data = self._json("POST", "/labels", json={"title": title})
        label = self._label_from(data)
        if label is None:
            raise VikunjaError(f"malformed label create response for {title!r}")
        return label

    def attach_label(self, task_id: int, label_id: int) -> None:
        self._json("POST", f"/tasks/{task_id}/labels", json={"label_id": label_id})

    def replace_labels(self, task_id: int, label_ids: list[int]) -> None:
        self._json(
            "PUT",
            f"/tasks/{task_id}/labels/bulk",
            json={"labels": [{"id": label_id} for label_id in label_ids]},
        )

    def _project_id_for(self, project: int | str, create: bool) -> int:
        if isinstance(project, int):
            try:
                data = self._json("GET", f"/projects/{project}")
                try:
                    return int(data["id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise VikunjaError(
                        f"malformed project response for {project!r}"
                    ) from exc
            except VikunjaNotFoundError:
                if not create:
                    raise
            data = self._json("POST", "/projects", json={"title": str(project)})
            try:
                return int(data["id"])
            except (KeyError, TypeError, ValueError) as exc:
                raise VikunjaError(
                    f"malformed project create response for {project!r}"
                ) from exc
        items = self._all_pages("/projects", {"q": str(project)})
        match = next(
            (i for i in items if str(i.get("title") or "") == str(project)),
            None,
        )
        if match is None:
            if create:
                data = self._json("POST", "/projects", json={"title": str(project)})
                try:
                    return int(data["id"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise VikunjaError(
                        f"malformed project create response for {project!r}"
                    ) from exc
            raise VikunjaError(f"no Vikunja project titled {project!r}")
        try:
            return int(match["id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VikunjaError(f"malformed project response for {project!r}") from exc

    def resolve_project(
        self,
        project: int | str | None = None,
        create: bool | None = None,
    ) -> int:
        return self._project_id_for(
            self.cfg.project if project is None else project,
            self.cfg.create_project if create is None else create,
        )

    def ensure_project(
        self,
        project: int | str | None = None,
        create: bool | None = None,
    ) -> int:
        project_id = self.resolve_project(project=project, create=create)
        data = self._json("GET", f"/projects/{project_id}")
        try:
            max_permission = int(data.get("max_permission") or 0)
        except (KeyError, TypeError, ValueError) as exc:
            raise VikunjaError(
                f"malformed project response for {project_id!r}"
            ) from exc
        if max_permission < 1:
            raise VikunjaPermissionError(
                f"token lacks write permission on project {project_id!r} "
                f"(max_permission={max_permission})",
                status=403,
            )
        return project_id

    def resolve_bucket_id(self, project_id: int, bucket: int | str) -> int:
        if isinstance(bucket, int):
            return bucket
        views = self._all_pages(f"/projects/{project_id}/views")
        for view in views:
            try:
                view_id = int(view["id"])
            except (KeyError, TypeError, ValueError):
                continue
            for raw in self._all_pages(
                f"/projects/{project_id}/views/{view_id}/buckets"
            ):
                if str(raw.get("title") or "") == str(bucket):
                    try:
                        return int(raw["id"])
                    except (KeyError, TypeError, ValueError):
                        continue
        raise VikunjaError(
            f"no Vikunja bucket titled {bucket!r} in project {project_id}"
        )

    def move_task_to_bucket(self, project_id: int, task_id: int, bucket_id: int) -> None:
        """Move a task using the project's kanban bucket endpoint."""
        views = self._all_pages(f"/projects/{project_id}/views")
        for view in views:
            try:
                view_id = int(view["id"])
            except (KeyError, TypeError, ValueError):
                continue
            bucket_ids = set()
            for raw in self._all_pages(
                f"/projects/{project_id}/views/{view_id}/buckets"
            ):
                try:
                    bucket_ids.add(int(raw["id"]))
                except (KeyError, TypeError, ValueError):
                    continue
            if bucket_id in bucket_ids:
                self._json(
                    "PUT",
                    f"/projects/{project_id}/views/{view_id}/buckets/{bucket_id}/tasks",
                    json={"task_id": task_id},
                )
                return
        raise VikunjaError(f"bucket {bucket_id} is not in project {project_id}")

    # -- internals ---------------------------------------------------------

    def _max_items_per_page(self) -> int:
        try:
            data = self.info()
            return int(data.get("max_items_per_page") or _DEFAULT_PER_PAGE)
        except (VikunjaError, TypeError, ValueError):
            logger.warning(
                "could not read max_items_per_page; using %s", _DEFAULT_PER_PAGE
            )
            return _DEFAULT_PER_PAGE

    @staticmethod
    def _page_items(data: dict[str, Any], resource: str) -> tuple[list[Any], int]:
        items = data.get("items")
        try:
            total_pages = int(data["total_pages"])
        except (KeyError, TypeError, ValueError) as exc:
            raise VikunjaError(f"malformed paginated {resource} response") from exc
        if not isinstance(items, list) or total_pages < 0:
            raise VikunjaError(f"malformed paginated {resource} response")
        if total_pages == 0:
            try:
                total = int(data["total"])
            except (KeyError, TypeError, ValueError) as exc:
                raise VikunjaError(
                    f"malformed empty paginated {resource} response"
                ) from exc
            if total != 0 or items:
                raise VikunjaError(f"malformed paginated {resource} response")
        return items, total_pages

    def _all_pages(self, path: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        per_page = self._max_items_per_page()
        page = 1
        output: list[dict[str, Any]] = []
        while True:
            query = dict(params or {})
            query.update(page=page, per_page=per_page)
            data = self._json("GET", path, params=query)
            items, total_pages = self._page_items(data, path)
            if any(not isinstance(item, dict) for item in items):
                raise VikunjaError(f"malformed item in paginated {path} response")
            output.extend(items)
            if page >= total_pages:
                return output
            page += 1

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        url = f"{self.cfg.base_url}{path}"
        headers = dict(self.session.headers)
        headers.update(kwargs.get("headers") or {})
        kwargs["headers"] = headers
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as exc:
            raise VikunjaTransientError(f"request failed: {exc}") from exc
        status = resp.status_code
        if status in (404, 410):
            raise VikunjaNotFoundError(
                f"{method} {path} -> {status}",
                status=status,
                code=self._problem_code(resp),
                detail=self._problem_detail(resp),
            )
        if status == 401:
            raise VikunjaAuthError(
                f"{method} {path} -> unauthorized",
                status=status,
                code=self._problem_code(resp),
                detail=self._problem_detail(resp),
            )
        if status == 403:
            raise VikunjaPermissionError(
                f"{method} {path} -> forbidden",
                status=status,
                code=self._problem_code(resp),
                detail=self._problem_detail(resp),
            )
        if VikunjaError.is_transient(status):
            raise VikunjaTransientError(
                f"{method} {path} -> {status}",
                status=status,
                code=self._problem_code(resp),
                detail=self._problem_detail(resp),
            )
        if status >= 400:
            raise VikunjaError(
                f"{method} {path} -> {status}",
                status=status,
                code=self._problem_code(resp),
                detail=self._problem_detail(resp),
            )
        return resp

    def _json(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        resp = self._request(method, path, **kwargs)
        if resp.status_code == 204:
            return {}
        try:
            data = resp.json()
        except ValueError as exc:
            if resp.text:
                raise VikunjaError(f"{method} {path} -> unreadable JSON body") from exc
            return {}
        if not isinstance(data, dict):
            raise VikunjaError(f"{method} {path} -> non-object body")
        return data

    @staticmethod
    def _problem_code(resp: requests.Response) -> int | None:
        data = VikunjaClient._problem_body(resp)
        if data is None:
            return None
        try:
            return int(data["code"])
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _problem_detail(resp: requests.Response) -> str | None:
        data = VikunjaClient._problem_body(resp)
        if data is None:
            return None
        return str(data.get("detail")) if data.get("detail") is not None else None

    @staticmethod
    def _problem_body(resp: requests.Response) -> dict[str, Any] | None:
        try:
            data = resp.json()
        except ValueError:
            return None
        return data if isinstance(data, dict) else None

    @staticmethod
    def _label_from(raw: Any) -> VikLabel | None:
        if not isinstance(raw, dict):
            return None
        try:
            return VikLabel(
                id=int(raw["id"]),
                title=str(raw["title"]),
                hex_color=raw.get("hex_color"),
                created=raw.get("created"),
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _task_from(raw: Any) -> VikTask | None:
        if not isinstance(raw, dict):
            return None
        try:
            task_id = int(raw["id"])
            project_id = int(raw["project_id"])
        except (KeyError, TypeError, ValueError):
            return None
        title = raw.get("title")
        if not isinstance(title, str):
            return None
        bucket_id = raw.get("bucket_id")
        try:
            bucket_id = int(bucket_id) if bucket_id is not None else None
        except (TypeError, ValueError):
            return None
        raw_buckets = raw.get("buckets", [])
        if not isinstance(raw_buckets, list):
            return None
        bucket_ids: list[int] = []
        for bucket in raw_buckets:
            if not isinstance(bucket, dict):
                return None
            try:
                bucket_ids.append(int(bucket["id"]))
            except (KeyError, TypeError, ValueError):
                return None
        if "labels" not in raw:
            return None
        raw_labels = raw["labels"]
        if raw_labels is None:
            raw_labels = []
        elif not isinstance(raw_labels, list):
            return None
        labels: list[VikLabel] = []
        for raw_label in raw_labels:
            label = VikunjaClient._label_from(raw_label)
            if label is None:
                return None
            labels.append(label)
        try:
            priority = int(raw.get("priority") or 0)
        except (TypeError, ValueError):
            return None
        return VikTask(
            id=task_id,
            project_id=project_id,
            title=title,
            description=raw.get("description"),
            due_date=raw.get("due_date"),
            done=bool(raw.get("done")),
            priority=priority,
            bucket_id=bucket_id,
            bucket_ids=bucket_ids,
            labels=labels,
            identifier=str(raw.get("identifier") or ""),
            created=raw.get("created"),
            updated=raw.get("updated"),
        )
