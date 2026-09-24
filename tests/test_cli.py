import sys
import types
from datetime import UTC, datetime
from typing import ClassVar

import pytest

from edupagetasks import __main__ as cli
from edupagetasks.lock import RunLock
from edupagetasks.state import StateStore

CONFIG = """\
edupage:
  subdomain: gymnas
  auth:
    mode: session
    username: alice
    session_id: sess123
vikunja:
  base_url: http://localhost:3456/api/v2
  token: tk_test
  project: 12
sync:
  cadence_minutes: 30
  lock_file: {lock}
  notify_url: ""
"""


class _Namespace:
    pass


@pytest.fixture
def backends():
    """Inject fake edupage/vikunja/engine modules so the CLI never touches the
    real (not-yet-implemented) backend modules or the network."""
    vikunja = types.ModuleType("edupagetasks.vikunja")

    class VikunjaError(Exception): ...

    class VikunjaAuthError(VikunjaError): ...

    class VikunjaPermissionError(VikunjaError): ...

    class VikunjaNotFoundError(VikunjaError): ...

    class VikunjaTransientError(VikunjaError): ...

    class VikunjaClient:
        def __init__(self, cfg, timeout=10.0):
            self.cfg = cfg

        def ensure_project(self, **kwargs):
            return 12

    vikunja.VikunjaError = VikunjaError
    vikunja.VikunjaAuthError = VikunjaAuthError
    vikunja.VikunjaPermissionError = VikunjaPermissionError
    vikunja.VikunjaNotFoundError = VikunjaNotFoundError
    vikunja.VikunjaTransientError = VikunjaTransientError
    vikunja.VikunjaClient = VikunjaClient

    engine = types.ModuleType("edupagetasks.engine")

    class SyncResult:
        def __init__(self, covered_ok=True, breaker_open=False, errors=0, deferred=0):
            self.covered_ok = covered_ok
            self.breaker_open = breaker_open
            self.errors = errors
            self.deferred = deferred

    class SyncEngine:
        instances: ClassVar[list[SyncEngine]] = []
        make_result = None

        def __init__(self, store, vikunja, edupage, config, **kwargs):
            SyncEngine.instances.append(self)
            self.kwargs = kwargs
            self.calls = []
            self.result = SyncResult()

        def run(self, dry_run=False):
            self.calls.append(dry_run)
            if SyncEngine.make_result is not None:
                return SyncEngine.make_result()
            return self.result

    engine.SyncEngine = SyncEngine
    engine.SyncResult = SyncResult

    edupage = types.ModuleType("edupagetasks.edupage")

    class EduPageError(Exception): ...

    class EduPageAuthError(EduPageError): ...

    class EduPageCaptchaError(EduPageAuthError): ...

    class EduPageTransientError(EduPageError): ...

    class LoginState:
        def __init__(self):
            self.userid = "u1"
            self.school_year = 2026
            self.dbi = {}

    class EduPageClient:
        def __init__(self, cfg, timeout=10.0):
            self.cfg = cfg

        def connect(self):
            return LoginState()

    edupage.EduPageError = EduPageError
    edupage.EduPageAuthError = EduPageAuthError
    edupage.EduPageCaptchaError = EduPageCaptchaError
    edupage.EduPageTransientError = EduPageTransientError
    edupage.LoginState = LoginState
    edupage.EduPageClient = EduPageClient

    ns = _Namespace()
    ns.vikunja = vikunja
    ns.engine = engine
    ns.edupage = edupage

    real = {
        "edupagetasks.vikunja": sys.modules.get("edupagetasks.vikunja"),
        "edupagetasks.engine": sys.modules.get("edupagetasks.engine"),
        "edupagetasks.edupage": sys.modules.get("edupagetasks.edupage"),
    }
    sys.modules["edupagetasks.vikunja"] = vikunja
    sys.modules["edupagetasks.engine"] = engine
    sys.modules["edupagetasks.edupage"] = edupage

    yield ns

    for name, mod in real.items():
        if mod is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = mod
    SyncEngine.instances.clear()


@pytest.fixture
def config_file(tmp_path):
    default_lock = str(tmp_path / "run.lock")

    def write(lock: str | None = None):
        path = tmp_path / "config.yaml"
        path.write_text(CONFIG.format(lock=lock if lock is not None else default_lock))
        return str(path)

    return write


def test_run_dry_once_returns_zero_and_runs_engine(backends, config_file):
    cfg = config_file()
    rc = cli.main(["run", "--config", cfg, "--once", "--dry-run"])
    assert rc == 0
    engine = backends.engine.SyncEngine.instances[-1]
    assert engine.calls == [True]
    assert engine.kwargs["project_id"] == 12
    assert engine.kwargs["userid"] == "u1"
    assert engine.kwargs["school_year"] == 2026
    assert engine.kwargs["subdomain"] == "gymnas"


def test_run_once_non_dry(backends, config_file):
    cfg = config_file()
    rc = cli.main(["run", "--config", cfg, "--once"])
    assert rc == 0
    assert backends.engine.SyncEngine.instances[-1].calls == [False]


def test_run_lock_held_skips_engine(backends, config_file, tmp_path):
    lock_path = str(tmp_path / "held.lock")
    cfg = config_file(lock=lock_path)
    outer = RunLock(lock_path)
    assert outer.acquire() is True
    try:
        rc = cli.main(["run", "--config", cfg, "--once"])
        assert rc == 0
        assert backends.engine.SyncEngine.instances == []
    finally:
        outer.release()


def test_permanent_vikunja_auth_exit_2(backends, config_file, mocker):
    mocker.patch.object(
        backends.vikunja.VikunjaClient,
        "ensure_project",
        side_effect=backends.vikunja.VikunjaAuthError("token denied"),
    )
    rc = cli.main(["run", "--config", config_file(), "--once"])
    assert rc == 2


def test_permanent_edupage_auth_exit_2(backends, config_file, mocker):
    mocker.patch.object(
        backends.edupage.EduPageClient,
        "connect",
        side_effect=backends.edupage.EduPageAuthError("expired"),
    )
    rc = cli.main(["run", "--config", config_file(), "--once"])
    assert rc == 2


def test_breaker_open_exit_1(backends, config_file):
    backends.engine.SyncEngine.make_result = lambda: backends.engine.SyncResult(
        covered_ok=False, breaker_open=True
    )
    assert cli.main(["run", "--config", config_file(), "--once"]) == 1


def test_transient_errors_exit_1(backends, config_file):
    backends.engine.SyncEngine.make_result = lambda: backends.engine.SyncResult(
        errors=2, deferred=2
    )
    assert cli.main(["run", "--config", config_file(), "--once"]) == 1


def test_check_fresh_prints_zero(config_file, capsys):
    cfg_path = config_file()
    store = StateStore(cli._state_path(cfg_path), project_default=0)
    store.set_meta("last_sync_ts", datetime.now(UTC).isoformat())
    store.close()
    assert cli.main(["check", "--config", cfg_path]) == 0
    assert capsys.readouterr().out.strip() == "0"


def test_check_stale_prints_one(config_file, capsys):
    cfg_path = config_file()
    store = StateStore(cli._state_path(cfg_path), project_default=0)
    store.set_meta("last_sync_ts", "2020-01-01T00:00:00+00:00")
    store.close()
    assert cli.main(["check", "--config", cfg_path]) == 1
    assert capsys.readouterr().out.strip() == "1"


def test_bad_config_exits_2(tmp_path, capsys):
    path = tmp_path / "bad.yaml"
    path.write_text("edupage: [not, a, mapping]\n")
    assert cli.main(["run", "--config", str(path), "--once"]) == 2
    assert "edupagetasks:" in capsys.readouterr().err
