# EdupageTasks

Forward homework from an **EduPage** account into a **Vikunja** project as
tasks, idempotently and unattended (one-way: EduPage → Vikunja).

> **Warning:** This project was fully vibecoded. Review and test it carefully
> before relying on it with real accounts or data.

- Design/architecture: [`docs/application.md`](docs/application.md)
- EduPage wire protocol: [`docs/edupage-api.md`](docs/edupage-api.md)
- Vikunja v2 API notes: [`docs/vikunja-api.md`](docs/vikunja-api.md)
- Example config: [`examples/config.example.yaml`](examples/config.example.yaml)

## Usage

Install with `uv sync`, then run from the repo root (`uv run edupagetasks ...`),
or add the venv `bin` to your `PATH`.

```
edupagetasks run [--once] [--dry-run] [--config PATH]
```

Run a sync cycle. Without `--once` this enters the daemon loop. Omitting the
subcommand also defaults to `run`. The state DB (`state.db`) and, by default,
the lock file (`.edupagetasks.lock`) are created beside the config. Set
`sync.lock_file` to override the lock path.

```
edupagetasks run --once --dry-run          # print detailed task previews without local or target writes
edupagetasks daemon --config PATH
```

`daemon` loops a full cycle every `sync.cadence_minutes` minutes; SIGINT /
SIGTERM stop it cleanly after the current iteration.

```
edupagetasks check --config PATH           # health probe
```

Prints `0` when `meta.last_sync_ts` is not in the future and is fresh (within `2 * cadence_minutes`),
else `1`. For monit/cron health checks — exit code matches the printed digit.

```
edupagetasks seed-session [--session-id SID] --config PATH
```

Validate a `PHPSESSID` (prompted hidden when `--session-id` is omitted),
record `session_seeded_at` in `meta`, then run one sync using that session.
The session id is never written to disk; supply it via `EDUPAGE_SESSION_ID` to
keep running. For seeding, a session-mode config may omit `EDUPAGE_SESSION_ID`;
the command uses the supplied or prompted value.

### Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Ran and finished — including "nothing to do" and skipped-via-lock; a complete `--dry-run` plan also exits 0 |
| 1 | Transient error / circuit breaker open / run finished with errors or incomplete dry-run coverage (`check`: not fresh) |
| 2 | Permanent failure — bad config, Vikunja auth/permission/missing project, EduPage auth/captcha |

Config typos exit `2` with a message on stderr and never send notifications.
Set `sync.notify_url` for best-effort webhook alerts on permanent failures and
session-mode login failures. The lock file, log file and `state.db` are
created with mode `0600`; no credentials are ever logged or persisted by the
CLI. A dry run uses an in-memory state snapshot and does not create a lock or
state DB, create a Vikunja project, or write tasks. It still authenticates to
EduPage and reads homework and Vikunja data to calculate the plan; dry-run logs
go to stderr even when `log_file` is configured. Each planned item shows its
title, source dates and done state, configured labels/priority/bucket, and
rendered description. A rendered preview may include fields that a specific
action, such as `patch_done`, does not write.

## Docker deployment

Copy `compose.yaml` to a directory on the server and put your real
`config.yaml` beside it. Compose builds the image from the `main` branch of
the GitHub repository; the config is mounted read-only and excluded from the
image. The repository must be reachable from the server during the build. The
server must also be able to reach EduPage and the URL in `vikunja.base_url`
**from the container**.
If Vikunja runs on the same server, `localhost` in the config points to this
container, so use an address the container can reach.

Create the data directory as the account that will run Compose:

```sh
mkdir -p data
```

Compose runs the app with UID and GID 1000 by default. If your server account
uses different IDs, copy `.env.example` to `.env` and set `PUID` and `PGID` to
the output of `id -u` and `id -g`. The same account must be able to read
`config.yaml` and write `data/`. If your config uses `${EDUPAGE_USERNAME}`,
`${EDUPAGE_PASSWORD}`, `${EDUPAGE_SESSION_ID}`, or `${VIKUNJA_API_TOKEN}`,
set the corresponding values in `.env` as well. `.env` is ignored by Git and
excluded from the image.

If moving an existing installation, stop its local daemon and copy
`state.db` (and any `state.db-wal` / `state.db-shm` files) from beside the old
config into `data/` before starting the container. Keeping this database
preserves the task mapping across deployments.

Review the planned tasks, then start the daemon:

```sh
docker compose build
docker compose run --rm edupagetasks run --once --dry-run --config /data/config.yaml
docker compose up -d
docker compose logs -f edupagetasks
```

The daemon syncs every `sync.cadence_minutes` and restarts after a server
reboot. `docker compose down` stops it without deleting `data/`. After a code
update from GitHub, run `docker compose up -d --build`. No inbound port is
required.

## Operational limits

- Recovery uses the hidden account-specific `edupage-key` marker in the task
  description. Legacy `edu:{userid}:{timelineid}` labels are recognized and
  removed during migration. If Vikunja strips the marker and local state is
  lost, a retry can create a duplicate; marker round-trip behavior still needs
  confirmation on a real created task.
- Label sync is additive: removing a configured label does not detach it from
  existing tasks.
- EduPage history responses do not signal completeness. A successful history
  request supports the coverage check but cannot prove every matching record
  was returned. An empty history response disables task closure and leaves the
  health check stale until a covered sync succeeds.
