"""Configuration loading: one YAML file with ${ENV_VAR} secret placeholders."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")

_log = logging.getLogger("edupagetasks")


class ConfigError(ValueError):
    """Raised for missing/invalid configuration."""


def _resolve_env(value: Any, lenient: frozenset[str] = frozenset()) -> Any:
    """Recursively replace ${ENV_VAR} placeholders in string values.

    Variables in ``lenient`` that are unset resolve to the empty string instead
    of raising, so fields that are only consumed conditionally (e.g. the
    session-mode ``session_id`` while the config runs in password mode) do not
    block startup.
    """
    if isinstance(value, str):

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in os.environ:
                if name in lenient:
                    return ""
                raise ConfigError(
                    f"environment variable '{name}' is referenced in the config but not set"
                )
            return os.environ[name]

        return _ENV_RE.sub(_sub, value)
    if isinstance(value, list):
        return [_resolve_env(v, lenient=lenient) for v in value]
    if isinstance(value, dict):
        return {k: _resolve_env(v, lenient=lenient) for k, v in value.items()}
    return value


def _as_int(name: str, value: Any) -> int:
    if isinstance(value, bool):
        raise ConfigError(f"'{name}' must be an integer, got {value!r}")
    if isinstance(value, int):
        result = value
    elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
        result = int(value)
    else:
        raise ConfigError(f"'{name}' must be an integer, got {value!r}")
    if result < 0:
        raise ConfigError(f"'{name}' must be a non-negative integer, got {value!r}")
    return result


def _as_optional_int(name: str, value: Any) -> int | None:
    return None if value is None else _as_int(name, value)


def _check_unresolved(name: str, value: Any) -> None:
    """Raise ConfigError when a ``${...}`` placeholder survived env resolution.

    ``_resolve_env`` already errors on well-formed but unset variables; anything
    still containing ``${`` is a malformed/leftover placeholder (e.g. ``${ foo }``)
    that would otherwise be kept literally.
    """
    if isinstance(value, str):
        if "${" in value:
            raise ConfigError(
                f"unresolved placeholder in config key '{name}': {value!r}"
            )
        return
    if isinstance(value, dict):
        for k, v in value.items():
            _check_unresolved(f"{name}.{k}", v)
        return
    if isinstance(value, (list, tuple)):
        for i, v in enumerate(value):
            _check_unresolved(f"{name}[{i}]", v)


def _as_bool(name: str, value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low == "true":
            return True
        if low == "false":
            return False
    raise ConfigError(f"'{name}' must be a boolean, got {value!r}")


@dataclass
class EduPageAuthConfig:
    mode: str
    username: str | None
    password: str | None
    session_id: str | None


@dataclass
class EduPageConfig:
    subdomain: str
    auth: EduPageAuthConfig
    window_days: int = 30
    include_types: list[str] = field(
        default_factory=lambda: ["homework", "testpridelenie", "etesthw"]
    )
    timezone: str = "Europe/Bratislava"
    child_person_id: int | None = None
    teacher_line: bool = True

    @property
    def url(self) -> str:
        return f"https://{self.subdomain}.edupage.org"


@dataclass
class PriorityRule:
    overdue: int
    due_within_days: int


@dataclass
class VikunjaConfig:
    base_url: str
    token: str
    project: int | str
    create_project: bool = False
    markdown: bool = True
    labels: list[str] = field(default_factory=list)
    subject_labels: bool = True
    priority_rule: PriorityRule | None = None
    default_bucket: int | str | None = None


@dataclass
class SyncConfig:
    cadence_minutes: int = 30
    dry_run: bool = False
    mirror_done: bool = False
    delete_policy: str = "close"
    grace_days: int = 7
    retry_max: int = 4
    retry_backoff_s: int = 1
    breaker_threshold: int = 5
    log_level: str = "info"
    log_file: str | None = None
    lock_file: str | None = None
    notify_url: str = ""


@dataclass
class Config:
    edupage: EduPageConfig
    vikunja: VikunjaConfig
    sync: SyncConfig


def _as_str_list(name: str, value: Any, default: tuple[str, ...]) -> list[str]:
    if value is None:
        return list(default)
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"'{name}' must be a list of strings, got {value!r}")
    return list(value)


def load_config(path: str, *, allow_missing_session: bool = False) -> Config:
    try:
        with open(path, encoding="utf-8") as f:
            raw = yaml.safe_load(f)
    except OSError as e:
        raise ConfigError(f"cannot read config file {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML in {path}: {e}") from e

    raw = {} if raw is None else raw
    lenient = frozenset({"EDUPAGE_SESSION_ID", "EDUPAGE_PASSWORD"})
    data = _resolve_env(raw, lenient=lenient)
    if not isinstance(data, dict):
        raise ConfigError("config root must be a mapping")
    for key, value in data.items():
        _check_unresolved(key, value)

    edupage = data.get("edupage")
    vikunja = data.get("vikunja")
    if not isinstance(edupage, dict):
        raise ConfigError("config requires an 'edupage' section")
    if not isinstance(vikunja, dict):
        raise ConfigError("config requires a 'vikunja' section")
    sync = data.get("sync")
    if sync is None:
        sync = {}
    if not isinstance(sync, dict):
        raise ConfigError("config 'sync' must be a mapping")

    subdomain = edupage.get("subdomain")
    if not isinstance(subdomain, str) or not subdomain:
        raise ConfigError(
            "'edupage.subdomain' is required and must be a non-empty string"
        )

    auth_raw = edupage.get("auth")
    if not isinstance(auth_raw, dict):
        raise ConfigError("config requires a 'edupage.auth' section")
    mode = auth_raw.get("mode")
    if mode not in ("password", "session"):
        raise ConfigError(
            f"'edupage.auth.mode' must be 'password' or 'session', got {mode!r}"
        )
    password = auth_raw.get("password")
    session_id = auth_raw.get("session_id")
    if mode == "password" and not password:
        raise ConfigError(
            "'edupage.auth.mode: password' requires 'edupage.auth.password'"
        )
    if mode == "session" and not session_id and not allow_missing_session:
        raise ConfigError(
            "'edupage.auth.mode: session' requires 'edupage.auth.session_id'"
        )
    auth = EduPageAuthConfig(
        mode=mode,
        username=auth_raw.get("username"),
        password=password,
        session_id=session_id,
    )

    window_days = _as_int("edupage.window_days", edupage.get("window_days", 30))
    if window_days <= 0:
        raise ConfigError("'edupage.window_days' must be > 0")
    if window_days > 30:
        _log.warning(
            "edupage.window_days=%d > 30: retention past the recent month is "
            "unverified and the close path is disabled",
            window_days,
        )

    timezone = edupage.get("timezone", "Europe/Bratislava") or "Europe/Bratislava"
    if not isinstance(timezone, str):
        raise ConfigError("'edupage.timezone' must be a valid IANA timezone name")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ConfigError(f"'edupage.timezone' is not a valid IANA timezone: {timezone!r}") from exc

    teacher_line = _as_bool("edupage.teacher_line", edupage.get("teacher_line", True))
    edupage_cfg = EduPageConfig(
        subdomain=subdomain,
        auth=auth,
        window_days=window_days,
        include_types=_as_str_list(
            "edupage.include_types",
            edupage.get("include_types"),
            ("homework", "testpridelenie", "etesthw"),
        ),
        timezone=timezone,
        child_person_id=_as_optional_int(
            "edupage.child_person_id", edupage.get("child_person_id")
        ),
        teacher_line=teacher_line,
    )

    base_url = vikunja.get("base_url")
    if not isinstance(base_url, str):
        raise ConfigError(
            "'vikunja.base_url' is required and must be a non-empty string"
        )
    base_url = base_url.rstrip("/")
    if not base_url:
        raise ConfigError(
            "'vikunja.base_url' is required and must be a non-empty string"
        )
    token = vikunja.get("token")
    if not isinstance(token, str) or not token:
        raise ConfigError("'vikunja.token' is required and must be a non-empty string")
    project = vikunja.get("project")
    if (
        project is None
        or isinstance(project, bool)
        or not isinstance(project, (int, str))
    ):
        raise ConfigError(
            "'vikunja.project' is required and must be a numeric id or title"
        )

    priority_rule = None
    pr_raw = vikunja.get("priority_rule")
    if pr_raw is not None:
        if not isinstance(pr_raw, dict):
            raise ConfigError("'vikunja.priority_rule' must be a mapping or null")
        priority_rule = PriorityRule(
            overdue=_as_int("vikunja.priority_rule.overdue", pr_raw.get("overdue")),
            due_within_days=_as_int(
                "vikunja.priority_rule.due_within_days", pr_raw.get("due_within_days")
            ),
        )

    default_bucket = None
    default_bucket_raw = vikunja.get("default_bucket")
    if default_bucket_raw is not None:
        if isinstance(default_bucket_raw, bool) or not isinstance(
            default_bucket_raw, (int, str)
        ):
            raise ConfigError(
                f"'vikunja.default_bucket' must be a numeric id or title, "
                f"got {default_bucket_raw!r}"
            )
        if isinstance(default_bucket_raw, str) and not default_bucket_raw.strip():
            raise ConfigError(
                f"'vikunja.default_bucket' must be a numeric id or title, "
                f"got {default_bucket_raw!r}"
            )
        default_bucket = default_bucket_raw

    vikunja_cfg = VikunjaConfig(
        base_url=base_url,
        token=token,
        project=project,
        create_project=_as_bool(
            "vikunja.create_project", vikunja.get("create_project", False)
        ),
        markdown=_as_bool("vikunja.markdown", vikunja.get("markdown", True)),
        labels=_as_str_list("vikunja.labels", vikunja.get("labels"), ()),
        subject_labels=_as_bool(
            "vikunja.subject_labels", vikunja.get("subject_labels", True)
        ),
        priority_rule=priority_rule,
        default_bucket=default_bucket,
    )

    delete_policy = sync.get("delete_policy", "close")
    if delete_policy not in ("close", "leave", "delete"):
        raise ConfigError(
            f"'sync.delete_policy' must be one of close|leave|delete, got {delete_policy!r}"
        )
    log_file = sync.get("log_file")
    if log_file is not None and not isinstance(log_file, str):
        raise ConfigError("'sync.log_file' must be a string or null")

    sync_cfg = SyncConfig(
        cadence_minutes=_as_int(
            "sync.cadence_minutes", sync.get("cadence_minutes", 30)
        ),
        dry_run=_as_bool("sync.dry_run", sync.get("dry_run", False)),
        mirror_done=_as_bool("sync.mirror_done", sync.get("mirror_done", False)),
        delete_policy=delete_policy,
        grace_days=_as_int("sync.grace_days", sync.get("grace_days", 7)),
        retry_max=_as_int("sync.retry_max", sync.get("retry_max", 4)),
        retry_backoff_s=_as_int("sync.retry_backoff_s", sync.get("retry_backoff_s", 1)),
        breaker_threshold=_as_int(
            "sync.breaker_threshold", sync.get("breaker_threshold", 5)
        ),
        log_level=sync.get("log_level", "info") or "info",
        log_file=log_file,
        lock_file=sync.get("lock_file") or None,
        notify_url=sync.get("notify_url", "") or "",
    )

    if sync_cfg.cadence_minutes <= 0:
        raise ConfigError("'sync.cadence_minutes' must be > 0")
    if sync_cfg.breaker_threshold <= 0:
        raise ConfigError("'sync.breaker_threshold' must be > 0")
    if sync_cfg.lock_file is not None and not isinstance(sync_cfg.lock_file, str):
        raise ConfigError("'sync.lock_file' must be a string or null")

    return Config(edupage=edupage_cfg, vikunja=vikunja_cfg, sync=sync_cfg)
