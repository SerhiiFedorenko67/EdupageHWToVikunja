"""CLI entry point: run / daemon / check / seed-session subcommands.

Heavy backend modules (edupagetasks.vikunja, edupagetasks.edupage,
edupagetasks.engine) are imported lazily inside the functions that need them,
so ``--help`` and config-error paths work without them being importable.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import signal
import sys
import time
from datetime import UTC, datetime

from edupagetasks.config import Config, ConfigError, load_config
from edupagetasks.lock import RunLock, RunLockError
from edupagetasks.logs import setup_logging
from edupagetasks.notify import notify_failure
from edupagetasks.state import StateStore

logger = logging.getLogger("edupagetasks")

_STATE_FILENAME = "state.db"
_SIGNAL_POLL_S = 5.0


def _state_path(config_path: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), _STATE_FILENAME)


def _utcnow_iso() -> str:
    return datetime.now(UTC).isoformat()


def _meta_fresh(value: str | None, cadence_minutes: int) -> bool:
    if not value:
        return False
    try:
        ts = datetime.fromisoformat(value)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    delta = datetime.now(UTC) - ts
    if delta.total_seconds() < 0:
        return False
    return delta.total_seconds() <= 2 * cadence_minutes * 60


def _notify_failure(cfg: Config, subject: str, detail: str) -> None:
    if cfg.sync.notify_url:
        notify_failure(cfg.sync.notify_url, subject, detail)


def _edu_auth_notify(cfg: Config, exc: Exception) -> None:
    if cfg.edupage.auth.mode == "session":
        _notify_failure(
            cfg,
            "EduPage session needs manual attention",
            "session-mode login failed; reseed PHPSESSID with "
            "'edupagetasks seed-session'",
        )
    else:
        _notify_failure(cfg, "EduPage authentication failed", str(exc))


def _print_dry_run_plan(result: object) -> None:
    print(f"Dry-run plan ({result.items_fetched} homework items):")
    if not result.planned:
        print("  no changes")
        return
    for item in result.planned:
        target = f" -> Vikunja task {item.task_id}" if item.task_id is not None else ""
        print(f"\n  {item.action.upper()} EduPage #{item.timelineid}{target}")
        if item.detail:
            print(f"    Reason: {item.detail}")
        preview = item.preview
        if preview is None:
            if item.action == "close":
                print("    Effect: mark the existing Vikunja task done")
            elif item.action == "delete":
                print("    Effect: delete the existing Vikunja task")
            continue
        print(f"    Title: {preview['title']}")
        print(f"    Subject: {preview['subject'] or 'unknown'}")
        print(f"    Assigned: {preview['assigned'] or 'unknown'}")
        due_day = preview["due_day"] or "none"
        due_utc = preview["due_date"] or "none"
        print(f"    Due: {due_day} (Vikunja timestamp: {due_utc})")
        source_done = "yes" if preview["source_done"] else "no"
        print(f"    EduPage marked complete: {source_done} (source status)")
        if item.action == "create":
            created_done = "yes" if preview["create_done"] else "no"
            print(f"    Done when created in Vikunja: {created_done}")
        elif item.action == "patch_done":
            print(f"    Effect: set Vikunja done to {source_done}")
        elif item.action == "reopen":
            print("    Effect: reopen the Vikunja task")
        elif item.action == "patch":
            effect = (
                "repair labels, bucket, priority, or teacher line"
                if preview["derived_only"] else "update title and description; update due date if changed"
            )
            print(f"    Effect: {effect}")
        elif item.action in ("resume", "retry"):
            print(f"    Effect: resume {preview['source_op']} (may reuse an existing task)")
        priority = preview["priority"]
        print(f"    Configured priority: {priority if priority is not None else 'Vikunja default'}")
        print(f"    Desired labels: {', '.join(preview['labels']) or 'none'}")
        print(f"    Configured bucket: {preview['bucket'] if preview['bucket'] is not None else 'Vikunja default'}")
        print("    Rendered description preview:")
        visible_description = "\n".join(
            line for line in preview["description"].splitlines()
            if not line.strip().startswith("<!-- edupage-key:")
        ).rstrip()
        for line in visible_description.splitlines():
            print(f"      {line}")


def _run_once(cfg: Config, *, config_path: str, dry: bool) -> int:
    from edupagetasks.edupage import (
        EduPageAuthError,
        EduPageCaptchaError,
        EduPageClient,
        EduPageError,
        EduPageTransientError,
    )
    from edupagetasks.engine import SyncEngine
    from edupagetasks.vikunja import (
        VikunjaAuthError,
        VikunjaClient,
        VikunjaError,
        VikunjaNotFoundError,
        VikunjaPermissionError,
        VikunjaTransientError,
    )

    lock = None
    if not dry:
        lock_path = cfg.sync.lock_file or os.path.join(
            os.path.dirname(os.path.abspath(config_path)), ".edupagetasks.lock"
        )
        lock = RunLock(lock_path)
        try:
            if not lock.acquire():
                logger.info("lock %s held by another run; skipping (exit 0)", lock_path)
                return 0
        except RunLockError as exc:
            logger.error("cannot acquire run lock: %s", exc)
            return 2
    store = None
    try:
        try:
            vikunja = VikunjaClient(cfg.vikunja, timeout=10.0)
            project_id = (
                vikunja.resolve_project(project=cfg.vikunja.project, create=False)
                if dry
                else vikunja.ensure_project(
                    project=cfg.vikunja.project, create=cfg.vikunja.create_project
                )
            )
            store = StateStore(
                _state_path(config_path), project_default=project_id, read_only=dry
            )
            edupage = EduPageClient(cfg.edupage, timeout=10.0)
            login = edupage.connect()
            result = SyncEngine(
                store,
                vikunja,
                edupage,
                cfg,
                project_id=project_id,
                userid=login.userid,
                school_year=login.school_year,
                dbi=login.dbi,
                subdomain=cfg.edupage.subdomain,
            ).run(dry_run=dry, state=login)
            if dry:
                _print_dry_run_plan(result)
        except (VikunjaAuthError, VikunjaPermissionError, VikunjaNotFoundError) as exc:
            if not dry:
                _notify_failure(cfg, "Vikunja sync failed permanently", str(exc))
            logger.error("permanent Vikunja failure: %s", exc)
            return 2
        except (EduPageAuthError, EduPageCaptchaError) as exc:
            if not dry:
                _edu_auth_notify(cfg, exc)
            logger.error("EduPage login failed: %s", exc)
            return 2
        except (EduPageTransientError, VikunjaTransientError) as exc:
            logger.error("transient failure during sync: %s", exc)
            return 1
        except EduPageError as exc:
            if not dry:
                _notify_failure(cfg, "EduPage sync failed", str(exc))
            logger.error("EduPage request failed: %s", exc)
            return 2
        except VikunjaError as exc:
            if not dry:
                _notify_failure(cfg, "Vikunja sync failed", str(exc))
            logger.error("Vikunja request failed: %s", exc)
            return 2
        if result.breaker_open:
            logger.error("circuit breaker open; sync paused (exit 1)")
            return 1
        if not result.covered_ok:
            logger.warning("fetch did not fully cover the window; close path disabled")
            if dry:
                logger.error("dry-run plan is incomplete because the fetch did not cover the window")
                return 1
        if result.errors:
            logger.error(
                "sync finished with %d error(s), %d deferred (exit 1)",
                result.errors,
                result.deferred,
            )
            return 1
        if dry and result.deferred:
            logger.error("dry-run plan includes %d deferred item(s)", result.deferred)
            return 1
        logger.info("sync finished")
        return 0
    finally:
        if store is not None:
            store.close()
        if lock is not None:
            lock.release()


class _SignalStop:
    """Registers SIGINT/SIGTERM handlers that stop a daemon loop cleanly."""

    def __init__(self) -> None:
        self.triggered = False
        self._previous: dict[int, signal.Handlers] = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handler)

    def _handler(self, signum: int, frame: object) -> None:
        self.triggered = True
        logger.info("signal %d received; stopping after the current iteration", signum)

    def restore(self) -> None:
        for signum, handler in self._previous.items():
            signal.signal(signum, handler)


def _cmd_run(args: argparse.Namespace, cfg: Config) -> int:
    dry = args.dry_run or cfg.sync.dry_run
    if args.once:
        return _run_once(cfg, config_path=args.config, dry=dry)
    logger.info("run without --once: entering the daemon loop")
    return _cmd_daemon(args, cfg, dry=dry)


def _cmd_daemon(
    args: argparse.Namespace, cfg: Config, *, dry: bool | None = None
) -> int:
    if dry is None:
        dry = cfg.sync.dry_run
    stop = _SignalStop()
    logger.info(
        "daemon started: cadence=%d minutes, dry_run=%s", cfg.sync.cadence_minutes, dry
    )
    try:
        while not stop.triggered:
            code = _run_once(cfg, config_path=args.config, dry=dry)
            if code != 0:
                logger.warning("sync iteration exited with code %d", code)
            remaining = cfg.sync.cadence_minutes * 60.0
            while remaining > 0 and not stop.triggered:
                step = min(_SIGNAL_POLL_S, remaining)
                time.sleep(step)
                remaining -= step
    finally:
        stop.restore()
    logger.info("daemon stopped")
    return 0


def _cmd_check(args: argparse.Namespace, cfg: Config) -> int:
    store = StateStore(_state_path(args.config), project_default=0, read_only=True)
    try:
        last_sync_ts = store.get_meta("last_sync_ts")
        strikes = store.get_meta_int("breaker_strikes", 0)
        seeded = store.get_meta("session_seeded_at")
        logger.debug("breaker_strikes=%s session_seeded_at=%s", strikes, seeded)
        fresh = _meta_fresh(last_sync_ts, cfg.sync.cadence_minutes)
        print("0" if fresh else "1")
        return 0 if fresh else 1
    finally:
        store.close()


def _cmd_seed(args: argparse.Namespace, cfg: Config) -> int:
    from edupagetasks.edupage import (
        EduPageAuthError,
        EduPageCaptchaError,
        EduPageClient,
        EduPageError,
        EduPageTransientError,
    )

    session_id = args.session_id
    if not session_id:
        session_id = getpass.getpass("PHPSESSID: ")
    if not session_id:
        print("edupagetasks: no PHPSESSID provided", file=sys.stderr)
        return 2
    cfg.edupage.auth.mode = "session"
    cfg.edupage.auth.session_id = session_id
    client = EduPageClient(cfg.edupage, timeout=10.0)
    try:
        client.connect()
    except (EduPageAuthError, EduPageCaptchaError) as exc:
        logger.error("session validation failed: %s", exc)
        return 2
    except EduPageTransientError as exc:
        logger.error("session validation failed transiently: %s", exc)
        return 1
    except EduPageError as exc:
        logger.error("session validation failed: %s", exc)
        return 2
    store = StateStore(_state_path(args.config), project_default=0)
    store.set_meta("session_seeded_at", _utcnow_iso())
    store.close()
    logger.info("PHPSESSID validated; running one sync")
    return _run_once(cfg, config_path=args.config, dry=False)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="edupagetasks",
        description="Forward EduPage homework into a Vikunja project.",
    )
    parser.set_defaults(
        command="run", config="config.yaml", dry_run=False, once=False, session_id=None
    )
    sub = parser.add_subparsers(dest="command")

    run_p = sub.add_parser(
        "run", help="run one sync cycle (default; with --once, or the daemon loop)"
    )
    run_p.add_argument(
        "--config", default="config.yaml", help="config file (default: config.yaml)"
    )
    run_p.add_argument(
        "--dry-run", action="store_true", help="plan-only; nothing is written"
    )
    run_p.add_argument(
        "--once", action="store_true", help="run a single cycle and exit"
    )

    daemon_p = sub.add_parser(
        "daemon", help="loop a sync cycle every sync.cadence_minutes"
    )
    daemon_p.add_argument(
        "--config", default="config.yaml", help="config file (default: config.yaml)"
    )

    check_p = sub.add_parser(
        "check", help="monit/cron health probe; prints 0 (fresh) or 1 (stale)"
    )
    check_p.add_argument(
        "--config", default="config.yaml", help="config file (default: config.yaml)"
    )

    seed_p = sub.add_parser(
        "seed-session",
        help="validate a PHPSESSID, record session_seeded_at, then run one sync",
    )
    seed_p.add_argument(
        "--config", default="config.yaml", help="config file (default: config.yaml)"
    )
    seed_p.add_argument(
        "--session-id",
        default=None,
        help="PHPSESSID value (prompted hidden when omitted)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = load_config(args.config, allow_missing_session=args.command == "seed-session")
    except ConfigError as exc:
        print(f"edupagetasks: {exc}", file=sys.stderr)
        return 2
    dry = args.command == "run" and (args.dry_run or cfg.sync.dry_run)
    setup_logging(cfg.sync.log_level, None if dry else cfg.sync.log_file)
    if args.command == "run":
        return _cmd_run(args, cfg)
    if args.command == "daemon":
        return _cmd_daemon(args, cfg)
    if args.command == "check":
        return _cmd_check(args, cfg)
    if args.command == "seed-session":
        return _cmd_seed(args, cfg)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
