import logging

import pytest

from edupagetasks.config import (
    ConfigError,
    EduPageAuthConfig,
    EduPageConfig,
    PriorityRule,
    SyncConfig,
    VikunjaConfig,
    load_config,
)

MINIMAL = """\
edupage:
  subdomain: gymnas
  auth:
    mode: password
    username: alice
    password: secret
vikunja:
  base_url: http://localhost:3456/api/v2
  token: tk_test
  project: 12
"""

FULL = """\
edupage:
  subdomain: gymnas
  auth:
    mode: password
    username: alice
    password: secret
    session_id: null
  window_days: 45
  include_types: [homework, testpridelenie]
  timezone: Europe/Bratislava
  child_person_id: 5
  teacher_line: false
vikunja:
  base_url: http://localhost:3456/api/v2
  token: tk_test
  project: 12
  create_project: true
  markdown: false
  labels: [edupage, misc]
  subject_labels: false
  priority_rule: {overdue: 3, due_within_days: 2}
  default_bucket: 9
sync:
  cadence_minutes: 15
  dry_run: true
  mirror_done: true
  delete_policy: delete
  grace_days: 3
  retry_max: 6
  retry_backoff_s: 2
  breaker_threshold: 8
  log_level: debug
  log_file: /tmp/edu.log
  lock_file: /tmp/lock
  notify_url: https://hc.example/ping
"""


def write(tmp_path, text: str) -> str:
    path = tmp_path / "config.yaml"
    path.write_text(text)
    return str(path)


def test_defaults_filled_in(tmp_path):
    cfg = load_config(write(tmp_path, MINIMAL))
    expected_auth = EduPageAuthConfig(
        mode="password", username="alice", password="secret", session_id=None
    )
    assert cfg.edupage == EduPageConfig(
        subdomain="gymnas",
        auth=expected_auth,
        window_days=30,
        include_types=["homework", "testpridelenie", "etesthw"],
        timezone="Europe/Bratislava",
        child_person_id=None,
        teacher_line=True,
    )
    assert cfg.edupage.url == "https://gymnas.edupage.org"
    assert cfg.vikunja == VikunjaConfig(
        base_url="http://localhost:3456/api/v2",
        token="tk_test",
        project=12,
        create_project=False,
        markdown=True,
        labels=["edupage"],
        subject_labels=True,
        priority_rule=None,
        default_bucket=None,
    )
    assert cfg.sync == SyncConfig(
        cadence_minutes=30,
        dry_run=False,
        mirror_done=False,
        delete_policy="close",
        grace_days=7,
        retry_max=4,
        retry_backoff_s=1,
        breaker_threshold=5,
        log_level="info",
        log_file=None,
        lock_file="/var/lib/edupagetasks/lock",
        notify_url="",
    )


def test_env_placeholders_resolved(tmp_path, monkeypatch):
    monkeypatch.setenv("EDUPAGE_USERNAME", "bob")
    monkeypatch.setenv("EDUPAGE_PASSWORD", "s3cret")
    monkeypatch.setenv("VIKUNJA_TOKEN", "tk_from_env")
    text = (
        MINIMAL.replace("username: alice", "username: ${EDUPAGE_USERNAME}")
        .replace("password: secret", "password: ${EDUPAGE_PASSWORD}")
        .replace("token: tk_test", "token: ${VIKUNJA_TOKEN}")
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.edupage.auth.username == "bob"
    assert cfg.edupage.auth.password == "s3cret"
    assert cfg.vikunja.token == "tk_from_env"


def test_missing_env_var_raises(tmp_path, monkeypatch):
    monkeypatch.delenv("VIKUNJA_TOKEN", raising=False)
    text = MINIMAL.replace("token: tk_test", "token: ${VIKUNJA_TOKEN}")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "VIKUNJA_TOKEN" in str(excinfo.value)


def test_password_mode_session_env_lenient(tmp_path, monkeypatch):
    monkeypatch.delenv("EDUPAGE_SESSION_ID", raising=False)
    text = MINIMAL.replace(
        "    password: secret\n",
        "    password: secret\n    session_id: ${EDUPAGE_SESSION_ID}\n",
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.edupage.auth.mode == "password"
    assert cfg.edupage.auth.session_id == ""


def test_session_mode_password_env_lenient(tmp_path, monkeypatch):
    monkeypatch.delenv("EDUPAGE_PASSWORD", raising=False)
    text = (
        MINIMAL.replace("mode: password", "mode: session")
        .replace(
            "    password: secret\n",
            "    password: ${EDUPAGE_PASSWORD}\n    session_id: sess1\n",
        )
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.edupage.auth.mode == "session"
    assert cfg.edupage.auth.password == ""
    assert cfg.edupage.auth.session_id == "sess1"


def test_password_placeholder_missing_still_required(tmp_path, monkeypatch):
    monkeypatch.delenv("EDUPAGE_PASSWORD", raising=False)
    text = MINIMAL.replace("password: secret", "password: ${EDUPAGE_PASSWORD}")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "password" in str(excinfo.value)


def test_missing_file_raises(tmp_path):
    with pytest.raises(ConfigError):
        load_config(str(tmp_path / "nope.yaml"))


def test_invalid_delete_policy(tmp_path):
    text = MINIMAL + "sync:\n  delete_policy: explode\n"
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "delete_policy" in str(excinfo.value)


def test_invalid_auth_mode(tmp_path):
    text = MINIMAL.replace("mode: password", "mode: token")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "mode" in str(excinfo.value)


def test_password_mode_requires_password(tmp_path):
    text = MINIMAL.replace("    password: secret\n", "")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "password" in str(excinfo.value)


def test_session_mode_requires_session_id(tmp_path):
    text = MINIMAL.replace("mode: password", "mode: session").replace(
        "    password: secret\n", ""
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "session_id" in str(excinfo.value)


def test_window_days_must_be_positive(tmp_path):
    text = MINIMAL.replace("vikunja:", "  window_days: 0\nvikunja:")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "window_days" in str(excinfo.value)


def test_window_days_over_30_warns(tmp_path, caplog):
    text = MINIMAL.replace("vikunja:", "  window_days: 45\nvikunja:")
    with caplog.at_level(logging.WARNING, logger="edupagetasks"):
        cfg = load_config(write(tmp_path, text))
    assert cfg.edupage.window_days == 45
    messages = [r.message for r in caplog.records]
    assert any("window_days" in m and "close" in m for m in messages)


def test_int_coercion_from_string(tmp_path):
    text = MINIMAL.replace("vikunja:", '  window_days: "45"\nvikunja:')
    cfg = load_config(write(tmp_path, text))
    assert cfg.edupage.window_days == 45
    assert isinstance(cfg.edupage.window_days, int)


def test_project_kept_as_given(tmp_path):
    text = MINIMAL.replace("project: 12", 'project: "My Homework"')
    cfg = load_config(write(tmp_path, text))
    assert cfg.vikunja.project == "My Homework"


def test_as_int_negative_raises():
    from edupagetasks.config import _as_int

    for bad in ("-5", -5, "  -5 "):
        with pytest.raises(ConfigError):
            _as_int("test.int", bad)
    assert _as_int("test.int", "5") == 5
    assert _as_int("test.int", 0) == 0


def test_unresolved_placeholder_with_space_raises(tmp_path):
    text = MINIMAL.replace("password: secret", "password: '${ EDUPAGE_PASSWORD }'")
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "password" in str(excinfo.value)
    assert "${" in str(excinfo.value)


def test_default_bucket_title_accepted(tmp_path):
    text = MINIMAL.replace(
        "  project: 12\n", "  project: 12\n  default_bucket: Inbox\n"
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.vikunja.default_bucket == "Inbox"
    assert isinstance(cfg.vikunja.default_bucket, str)


def test_default_bucket_invalid_rejected(tmp_path):
    text = MINIMAL.replace(
        "  project: 12\n", "  project: 12\n  default_bucket: [1, 2]\n"
    )
    with pytest.raises(ConfigError) as excinfo:
        load_config(write(tmp_path, text))
    assert "default_bucket" in str(excinfo.value)


def test_base_url_trailing_slash_stripped(tmp_path):
    text = MINIMAL.replace(
        "http://localhost:3456/api/v2", "http://localhost:3456/api/v2//"
    )
    cfg = load_config(write(tmp_path, text))
    assert cfg.vikunja.base_url == "http://localhost:3456/api/v2"
    assert "//" not in cfg.vikunja.base_url.split("//", 1)[1]


def test_full_config_and_priority_rule(tmp_path):
    cfg = load_config(write(tmp_path, FULL))
    assert cfg.edupage.window_days == 45
    assert cfg.edupage.include_types == ["homework", "testpridelenie"]
    assert cfg.edupage.child_person_id == 5
    assert cfg.edupage.teacher_line is False
    assert cfg.vikunja.create_project is True
    assert cfg.vikunja.markdown is False
    assert cfg.vikunja.labels == ["edupage", "misc"]
    assert cfg.vikunja.subject_labels is False
    assert cfg.vikunja.priority_rule == PriorityRule(overdue=3, due_within_days=2)
    assert cfg.vikunja.default_bucket == 9
    assert cfg.sync.cadence_minutes == 15
    assert cfg.sync.dry_run is True
    assert cfg.sync.mirror_done is True
    assert cfg.sync.delete_policy == "delete"
    assert cfg.sync.grace_days == 3
    assert cfg.sync.retry_max == 6
    assert cfg.sync.retry_backoff_s == 2
    assert cfg.sync.breaker_threshold == 8
    assert cfg.sync.log_level == "debug"
    assert cfg.sync.log_file == "/tmp/edu.log"
    assert cfg.sync.lock_file == "/tmp/lock"
    assert cfg.sync.notify_url == "https://hc.example/ping"
