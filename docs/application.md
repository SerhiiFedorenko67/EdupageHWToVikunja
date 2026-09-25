# Edupage → Vikunja Homework Forwarder

Status: **implemented, with live integration behavior still requiring account verification**. This document
contains the original design and should be read alongside the implementation and the usage notes in
[`../README.md`](../README.md); some aspirations below are not guarantees of the current code.

---

## 1. Purpose and goals

Forward homework assignments from a school's **EduPage** account into a **Vikunja** instance
as tasks inside **one specific project**, so homework is visible where the student already plans work.

### Goals

- One-way pipeline: `EduPage homework → Vikunja task`.
- Deterministic: running twice must not duplicate or drift.
- Robust to EduPage's undocumented API and Vikunja's paginated, HTML-stored rich text.
- Headless and unattended: runs on a scheduler, needs no browser, no daily interaction.
- Self-healing: survives session expiry, network hiccups, wiped local state — and homework the
  teacher reopens or edits.

### Non-goals (explicitly out of scope)

- Reverse sync (marking homework done in Vikunja does not report back to EduPage).
- Grades, messages, timetable, substitution, lunches.
- Multiple target projects / complex routing rules.
- Running inside EduPage's or Vikunja's sandbox (we only call both APIs).

---

## 2. System context

```
                     ┌─────────────────┐
                     │  Scheduler      │   cron / systemd timer (or daemon loop)
                     └────────┬────────┘
                              │ every N minutes
                     ┌────────▼────────┐
                     │  sync engine    │   1. fetch homework (EduPage)
                     │                 │   2. reconcile + diff vs local state (SQLite)
                     │                 │   3. apply to Vikunja
                     └───┬────────┬────┘
                         │        │
        PHPSESSID /      │        │  bearer API token (tk_…)
        gsec_hash        │        │  JSON over HTTP
        eqap-encoded     │        │
              ┌──────────▼──┐  ┌──▼──────────────────┐
              │  EduPage    │  │  Vikunja            │
              │  {sub}.      │  │  100.119.216.58:3456 │
              │  edupage.org │  │  /api/v2            │
              └─────────────┘  └─────────────────────┘
                    │                     │
         local state DB  ──────────────────┘  mapping + last-run metadata
         (SQLite: homework→task identity)
```

Actors:
- **Source** — EduPage, the *undocumented* side. We mirror its real endpoints directly.
- **Target** — Vikunja v2 with a clean OpenAPI 3.1 spec.
- **Sync engine** — the application itself.
- **State store** — local SQLite giving the engine memory and idempotence.

---

## 3. Source system: how EduPage models homework

Reverse-engineered knowledge (see `docs/edupage-api.md`); key facts for homework:

### Where homework lives

EduPage exposes a per-user **timeline** of events. Homework is one event type.

- **Recent items**: already embedded in the login payload JSON — the `userhome(<json>)`
  blob's `items` array (and per-item user state in `userProps`).
- **History**: `POST /timeline/?module=todo&akcia=getData&filterTab=messages`
  with form body `datefrom=YYYY-MM-DD` → JSON `{timelineItems, timelineUserProps}`.
  The login payload covers roughly the **recent month**; this endpoint exists to fetch items
  **older** than that. Real retention is server-side and unverified — treat `window_days ≤ 30` as a
  working ceiling paired with per-run coverage checks (§10), not as a hard promise.

### Event types relevant to homework

| EventType (typ field) | Meaning |
| --- | --- |
| `homework` | homework assignment (main — the wire value is literally `homework`) |
| `testpridelenie` | exam/test assignment |
| `etesthw` | electronic-test homework |
| `bexam` / `oexam` / `rexam` / `pexam` / `sexam` / `testing` | big/oral/paper/project/short exam notices |
| `homeworkstudentstav` | homework *state change* (student marked done), noise to filter out |

Typical filter for the app: `typ == "homework"` (+ optional `testpridelenie`, `etesthw`). Match the
type **exactly** — `h_homework` (`EventType.H_HOMEWORK`) is an unrelated "helper" enum and must never
be treated as homework (see `docs/edupage-api.md`).

### Fields of a timeline item

| Field | Meaning |
| --- | --- |
| `timelineid` | stable id → our external key |
| `typ` | event type (see above) |
| `timestamp` | when assigned, `%Y-%m-%d %H:%M:%S` |
| `text` | short text / fallback title |
| `data` | JSON string with details (`nazov`, `messageContent`, and homework: `predmetid`, `date`, `planid`) |
| `data.nazov` / `data.date` | current homework title and **due date** (`%Y-%m-%d`); `oldVals` is a fallback |
| `user_meno` / `vlastnik_meno` | recipient / author display names (**author is parsed reliably**) |
| `cas_pridania`, `removed`, `pocet_reakcii` | extras |
| `userProps[timelineid]` (or `timelineUserProps`) | per-user state: `starred`, `doneMaxCas` (done date) |
| `dbi` tables | subject/teacher/classroom short names (id → name) |

Live homework records carry `data.predmetid`; assigned-test records can carry
`data.parametre.predmetid`. The client resolves these against `dbi.subjects`,
with `data.planid` → `dbi.plans[*].predmetid` as a fallback. When none of those
fields resolve, the app shows `Subject: unknown`.

### EduPage session essentials (for the fetch layer)

- Login: RPC `POST /login/?cmd=MainLogin&akcia=getToken` → `...&akcia=login`, or form fallback
  `POST /login/edubarLogin.php` (`csrfauth` from the login page). Fail markers: `cap=1`/`lerr=b43b43`
  = captcha, `bad=1` = bad credentials. 2FA = poll + finish flow.
- Success yields the `userhome(...)` JSON — keep it (`userid`, `dp.year`, `dbi`, `items`, `userProps`)
  plus the `ASC.gsechash="…"` hash → **`gsec_hash`** used as `__gsh` in `server/*.js?__func=…` calls.
- Session cookie `PHPSESSID`. Expired/garbage `gsec_hash` is signalled as `{"reload": true}` by the
  substitution and online-lesson helpers only (timetable `curentttGetData` and school-wide
  `mainDBIAccessor` do not emit it) — implement the `reload ⇒ transparent re-login` check uniformly on
  every RPC call (§11).
- Parent accounts: pick the student with `GET /login/switchchild?studentid={id}` (returns `OK`).
- The online-lesson helper re-scrapes a fresh `gsechash` from `/dashboard/eb.php` — not used here.
- Wire encodings, per `docs/edupage-api.md`: inbound payloads use the `eqz:`/`eqwd:` prefixes, decoded
  **base64-only** (responses are NOT zlib-inflated); the `eqap` request wrapper (`eqacs`/`eqaz` pair)
  applies to the login RPC and `send_message` only — the JSON `__args/__gsh` RPC and `/gcall` don't eqap-encode.

---

## 4. Target system: how Vikunja models tasks

From the live instance spec `GET /api/v2/openapi.json` (see `docs/vikunja-api.md`):

- **Project-centric**: tasks live in a project. Create with
  `POST /projects/{project}/tasks` (body `Task`, 201 → created `Task`; `project_id` taken from URL).
- **Task fields** we rely on: `title`, `description`, `due_date`, `priority` (int),
  `labels` (read-only in body — set via separate endpoints), `bucket_id`
  (move via `PUT /projects/{project}/views/{view}/buckets/{bucket}/tasks` with `TaskBucket`).
- **List** `GET /projects/{project}/tasks` → paginated wrapper `{items, page, per_page, total, total_pages}`
  (default `per_page` 50, max 1000 — verify real cap from `GET /info` → `max_items_per_page`).
- **Update** `PATCH /tasks/{projecttask}` (partial) / `PUT` (full); **delete** `DELETE /tasks/{projecttask}` (204).
- **Rich text**: `description` is stored **as HTML**. Read/write Markdown via `?format=markdown`;
  on PATCH use header `X-Vikunja-Format: markdown` (merge-patch drops query parameters).
- **Labels**: create `POST /labels` (`Label`); **attach one** `POST /tasks/{projecttask}/labels`
  body `{label_id}`; **replace the whole set** `PUT /tasks/{projecttask}/labels/bulk`
  body `LabelTaskBulk {labels:[Label…]}`. Bulk replaces — do not use it to append.
- **Permission**: single-item responses carry `max_permission` **in the body** (`0` read / `1` rw /
  `2` admin — e.g. on `Project`, `TaskReadOneBody`). The token's owner needs rw on the target
  project; there is no `x-max-permission` header in v2 (v1-only concept).
- **No external-id field**: the v2 `Task` schema has *no* `uid`/`external_id` (verified). Idempotent
  mapping therefore **must be maintained client-side** (§6); there is also no create-idempotency key,
  so the appliance must make create-safe-by-construction.

---

## 5. Mapping: EduPage homework → Vikunja task

| EduPage source | Vikunja target | Notes |
| --- | --- | --- |
| `timelineid` + `userid` | hidden description marker | stable recovery key, also kept in state DB (§6) |
| `data.nazov` / `text` | `title` | first line, at most 70 characters; no subject prefix |
| `text` + full long title + source URL | `description` (Markdown) | HTML→Markdown; no assignment/subject/teacher/date boilerplate |
| `data.date` | `due_date` | `%Y-%m-%d` → end of that local day as a UTC timestamp; omit when absent |
| subject short | `labels` | attach the short code such as `PRO`; optional extra labels come from config |
| optional priority rule (by due date) | `priority` | writable int; no documented range — probe (§14.5); default off |
| `userProps.doneMaxCas` / `timelineUserProps` | `done` (flag `mirror_done`) | one-way EduPage→Vikunja; see §10 |
| — | hidden marker line | `<!-- edupage-key:{userid}:{timelineid} -->` in the description footer |

### Title construction

Use the first line of the homework title, or fall back to `text`. Limit it to
70 characters and append `...` when truncated. The full source title appears
in the description when truncation or additional lines would hide content.

### Description construction (Markdown)

```
<full title if the task title was shortened>
<body: HTML→Markdown, trimmed, sanitised>
<blank>
[Open in EduPage](https://SUBDOMAIN.edupage.org/elearning/?eqa=...)
<!-- edupage-key:USERID:NNN -->
```

For a timeline item with `data.superid`, request the e-learning result data to
resolve its `testid`, `planid`, and `etestType`, then encode the direct
`/elearning/?eqa=...` URL. A plain homework item without an e-learning material
uses the timeline URL as a fallback.

The account-specific HTML-comment marker lets reconciliation identify a task
without a visible technical label. Legacy `edu:{userid}:{timelineid}` labels
are still recognized for migration. Matching title or body text alone never
establishes identity.

What gets fingerprinted (§6) are the **EduPage source fields** (`text`, current title, due date,
author, subject id, and a resolved direct link when available) — not the rendered title/description. Subject short names from `dbi` and
timezone-rendered timestamps are excluded, so a `dbi` edit or a DST/timezone change never triggers a
spurious PATCH.

---

## 6. Identity, idempotence and reconciliation

EduPage has no inbound external-id story and Vikunja v2 has no outbound `uid` field, so identity is
a **client-owned bijection** kept in SQLite, keyed by what makes an EduPage homework unique per
account. `timelineid` is a per-account unique event id, so the year is **not** part of identity (a
school rollover must not fork an in-flight task into a duplicate — §13):

```
key = (edu_userid, timelineid)
```

### State schema

```
key:                  (edu_userid, timelineid)  PK   (timelineid is unique per account → no year in key)
vikunja_task_id:      int|null     (null while `creating` / a never-created `deferred`)
vikunja_project_id:   int          (guard — §11 treats a mismatched project as task-gone)
content_fingerprint:  sha256 of source fields (text, title, due date, author, subject id,
                                   direct link when available)
                                   — NOT rendered output, and NOT done state
done_state:           bool         (EduPage's last seen done flag — diffed separately)
school_year:          int          (dp.year at last observation — reporting + rollover continuity only)
last_seen_due_date:   date|null    (last observed data.date — drives the §10 close rules)
last_seen_at:         timestamp    (last time the LIVE item appeared; `removed=1` rows do NOT refresh it)
closed_at:            timestamp|null (set when the row enters `closed`; tombstone age is unused)
pending_ops:          json         (queued patch/create payload while `deferred`; `[]` otherwise)
retry_count:          int          (attempts made on the pending op)
sync_state:           creating | created | updated | closed | deferred (`tombstoned` reserved, not produced)
anchor_label_id:      int|null     (legacy anchor label id, when migrating an older task)
updated_at:           timestamp
```

`meta` table: `schema_version`, `last_sync_ts`, `last_edu_userid`, `last_edu_school_year`,
`session_seeded_at` (§12), coverage records `last_coverage_from`, `last_coverage_to`, `covered_ok`
(§10), plus counters.

### Implemented `sync_state` transitions

- `creating → created` — POST + configured subject/extra labels attached.
- `creating → deferred` — create failed transiently; `pending_ops` holds the create payload.
- `created | updated → deferred` — an op failed transiently; `pending_ops` stores it.
- `deferred → (prior state)` — pending op re-applied OK; `retry_count` resets to 0.
- `deferred → created` — pending *create* re-applied OK (row gains its task id), or a 404/410 cleared
  `pending_ops` and returned the row to `created` for a clean re-create next cycle (§11).
- `created → updated` — a patch (content, done, or reopen) was applied.
- `created | updated → closed` — §10 policy-close applied (`done=true`); `closed_at := now`.
- `closed → updated` — §6 reopen: item reappeared with `done_state == false` → `done=false`.
- `delete_policy: delete` removes the row with the task; a reappearance is a fresh `create` (a
  disappeared row means "no mapped key" → `create`).

The tombstone lifecycle described in §10 is **design only and not implemented**: rows do not
transition from `closed` to `tombstoned`, and there is no tombstone garbage collection. The enum
contains a tombstoned value for forward compatibility, but the current engine does not produce it.

### Idempotence model (be precise about what guarantees what)

- A local SQLite transaction **cannot span the HTTP boundary**, so "transaction per task" is record
  hygiene, *not* an idempotency mechanism (Vikunja offers no create-idempotency key). The intent row
  and account-specific description marker reduce duplicate risk:
  1. **Write an intent row** (`sync_state='creating'`, no task id) **before** `POST /projects/{id}/tasks`.
  2. POST the task with its hidden key already in the description, record its id, and attach labels.
  3. If the response is lost, reconciliation can find the committed task by its marker if Vikunja
     retained it. Because the server offers no idempotency key, a stripped or unreadable marker can
     still allow a duplicate; a live create/read-back check is needed before relying on recovery.
- **Reconciliation runs at the start of every cycle** (not only on a fresh DB): page through
  `GET /projects/{project}/tasks`, read each task's account-specific marker, and rebuild or
  refresh the `key→task` map. This re-seeds a wiped state DB and heals any gap left by a crash. It also
  **prunes** map rows whose task has vanished from Vikunja (except `creating`/`deferred` — §11), treats
  a mapped task whose `project_id` differs from the configured project as gone (§11), and records the
  fetch-window coverage (`last_coverage_from/to`, `covered_ok`) that §10 gates the close path on.
- **Duplicate-key resolution**: if two tasks carry the same marker, keep the *earliest-created* as
  canonical; mark the others `done=true`, strip their marker and any legacy anchor label, and denylist
  them for the current reconciliation pass.
- **Orphan tasks without a namespaced marker or legacy anchor are not adopted**, even if title or
  body text matches. This avoids unsafe cross-account or ambiguous matches.
- `dry_run` (§9) runs reconciliation + planning **read-only**: no API writes, no new DB rows, no
  project/label ensurement — the plan it prints is a dry diff.
- Everything else that is idempotent by construction: `PATCH` with identical fields, label attach,
  `done` writes (policy-close included — re-patching `done=true` is a no-op).

### Three-phase cycle per run

1. **Fetch** — pull homework items whose **assignment timestamp** falls in the window (default: last
   30 days), apply filters, and record the covered `(date_from, date_to)`. A run marks `covered_ok`
   (§6 reconcile) when login `items` and a successful history pull were requested from the window
   start, with no reported truncation/error. EduPage's history response has no completeness signal,
   so this is an operational coverage estimate, not proof every record was returned. Nothing is *closed* from an
   uncovered fetch — it only adds/updates (§10). Due-date logic is applied later, separately from the
   window filter.
2. **Diff** (after reconciliation) — per item, evaluate in order, from first match:
   - row `creating` → resume using its persisted operation state and marker reconciliation (§6)
   - row `deferred` → re-apply `pending_ops` to the row's task; success → back to prior state,
     `retry_count` reset; transient → keep `deferred`, bump `retry_count`; 404/410 → clear
     `pending_ops`, state `created` (re-created next cycle — never a 404 loop, §11)
   - no mapped task → `create`
   - mapped with `sync_state == closed`, item **present**, `done_state == false` →
     `reopen`: `PATCH done=false`, set `sync_state='updated'` — then **keep evaluating** the checks
     below, so a reopen that also carries new content or a done-state flip is handled in the same run
   - mapped, `content_fingerprint` changed → `patch` (title/description/due), state `updated`
   - mapped, `done_state` changed → `patch done` only — gated on `mirror_done: true` (the default must
     NOT write EduPage's done flag) or a `closed` row
   - mapped, unchanged → `skip`
   - absent from window (or `removed=1`) → deletion / close policy (§10)
3. **Apply** — run the resulting plan in order; each item's writes are recorded against its row and
   committed per item so a crash cannot lose or double-apply a mid-run item. Derived fields (`priority`
   from the due-date rule, `bucket_id` from `default_bucket`, labels) are re-derived and re-applied
   idempotently when their inputs change. Derived priority and bucket drift is checked during each
   sync and repaired when a configured rule/default specifies a value, including after a human edit.

Fingerprint covers content only; done-state is tracked and diffed separately so a reopen with
identical content still flips `done` back to false.

---

## 7. State & persistence

- **SQLite file** `state.db` beside the config (path configurable). WAL mode; single file, no server.
- Tables: `homework_map` (above) and `meta`.
- **No credentials** stored in SQLite — only ids, fingerprints, timestamps, states.
- Never leave `creating` rows unhandled: they are resolved by reconciliation or a guarded re-create.

---

## 8. Configuration

Env providers for secrets + one YAML file (secrets via env):

```yaml
edupage:
  subdomain: <school>                 # https://{subdomain}.edupage.org
  auth:
    mode: password                    # password (RPC re-login) | session (reuse PHPSESSID)
    username: ${EDUPAGE_USERNAME}
    password: ${EDUPAGE_PASSWORD}     # required for mode: password
    session_id: ${EDUPAGE_SESSION_ID} # for mode: session
  window_days: 30                     # look-back window; §10 explains the coverage + close rules
  include_types: [homework, testpridelenie, etesthw]
  timezone: Europe/Bratislava
  child_person_id: null               # on parent accounts: pick the student (switchchild)

vikunja:
  base_url: http://100.119.216.58:3456/api/v2
  token: ${VIKUNJA_API_TOKEN}         # tk_…, scoped to write on the project
  project: 12                         # numeric id preferred; exact title also accepted
  create_project: false               # create if missing (needs admin)
  markdown: true                      # exchange rich text as Markdown
  labels: []                          # optional extra labels attached to synced tasks
  subject_labels: true                # auto-create a label such as `PRO` per subject
  priority_rule: null                 # null | {overdue: 3, due_within_days: 2}
  default_bucket: null                # bucket title or (preferred) numeric id

sync:
  cadence_minutes: 30                 # used by the daemon runner
  dry_run: false                      # plan-only; prints what would change
  mirror_done: false                  # mirror EduPage done-state to Vikunja `done`
  delete_policy: close                # close | leave | delete
  grace_days: 7                       # close only when due-date past + grace (see §10)
  retry_max: 4
  retry_backoff_s: 1
  breaker_threshold: 5                # consecutive transient failures before pausing
  log_level: info
  log_file: null                      # null = stderr
  lock_file: null                   # default: .edupagetasks.lock beside this config
  notify_url: ""                      # optional healthchecks/ntfy/webhook for failures
```

Notes:
- `project` by title breaks on rename → prefer the numeric id.
- `default_bucket`: numeric bucket id is stable; a title is resolved at startup, every run.
- The `edupage-key` description marker is the reconciliation identity. Legacy `edu:…` and `edupage`
  labels are removed when an older task is updated; unrelated human labels are kept.
- Sync adds configured labels. Removing an arbitrary extra label from config does not detach it from
  existing tasks; disabling subject labels likewise does not remove existing subject-code labels.
- `timezone` controls assignment-window interpretation, Vikunja due-date conversion, priority
  comparisons against the local date; stored timestamps are UTC.
- Missing knobs used to hardcode behaviour (retry, breaker, log) are now explicit.

---

## 9. Scheduled operation

- **Cadence**: `cadence_minutes` (default 30). Two runners:
  - `systemd timer` / `cron` → short-lived process per run (recommended).
  - daemon loop for sub-minute needs.
- **Overlap guard**: `lock_file` guarded by the OS `flock` lock. While held, a new run
  exits `0` silently (the freshness metric below is the real health signal).
- **Exit codes, standardised**: `0` = ran and finished (including "nothing to do" and "skipped via
  lock"); non-zero = genuine error (permanent credential/token failure, breaker tripped across the
  whole cycle). This keeps cron flaps and pager storms from trivial causes.
- **Health**: `meta.last_sync_ts` is written only after a **full fetch → diff → apply pass** — never
  for `dry_run` and never for a deferred-only (breaker) pass (an empty deferred queue must not
  masquerade as a heartbeat). External probes (monit/healthchecks) alert when it goes stale — not on
  exit codes alone.
- **On start**: config → state DB → ensure project & labels exist (idempotent) → login/reuse EduPage
  session → **reconcile** → full cycle (fetch → diff → apply). A dry run copies existing SQLite state
  into an in-memory snapshot (or starts with empty in-memory state), skips lock and project creation,
  performs read requests needed to plan, and prints the computed actions. It makes no local state,
  task, project, label, or notification writes.

---

## 10. Deletion, completion and reopen policy

The one rule that keeps Homework safe: **close only when it is both gone from the source AND
overdue.** Tying close to item *age* alone would kill long-running homework that merely leaves the
fetch window. The window is bounded by EduPage reach: the login payload covers roughly the recent
month and the history endpoint extends further, with real retention unverified
(`docs/edupage-api.md`). So `window_days` defaults to 30, and the close path additionally requires
that the run passed the coverage check (§6 fetch) — a partial or empty fetch never closes anything
(its rows still map; they just get no delete-policy action).

| Source observation | Action |
| --- | --- |
| `removed=1`, or absent from window, **and** due date past by at least `grace_days` | `delete_policy`: `close` → `PATCH done=true`; `leave` → nothing; `delete` → `DELETE` |
| absent but **not** yet overdue (no due date, or due ahead) | never touch — task stays open even if it left the window |
| no due date at all | treat "overdue" when `now > last_seen_at + (window_days + grace_days) days` (`last_seen_at` is refreshed only by live, non-`removed` items) |
| `doneMaxCas` observed (and `mirror_done: true`) | `PATCH done=true` (completion **mirror**) |
| `doneMaxCas` stops being observed while item still present (`mirror_done: true`) | `PATCH done=false` — **teacher reopened** the homework |
| row `closed`, item present, `done_state == false` | §6 `reopen`: `done=false` patch; content/done checks still run this cycle |

- Do not conflate the two `done=true` writers. **Policy-close** (item absent & overdue — above, runs
  regardless of `mirror_done`) and **completion-mirror** (`doneMaxCas`, only with `mirror_done: true`)
  both write `done=true`, and both are undone by *observing the item again*: policy-close by the §6
  `reopen` transition, completion-mirror by a missing `doneMaxCas`. Both are tracked separately in the
  row (`last_seen_at`, `last_seen_due_date`, `done_state`), never inferred from the Vikunja `done` bit.
- Completion-mirror reverts only while the item is observed; a task that leaves the window keeps its
  last mirrored `done` until it reappears (documented — absence alone never rewrites the mirror).
- **Tombstones (design only)**: the proposed closed→tombstoned transition and tombstone garbage
  collection are not implemented. Current rows remain `closed` until reopened, deleted by policy, or
  pruned because their Vikunja task disappeared.
- Closed-but-present quarrels are impossible because `close` requires *absence from the fetch*.

---

## 11. Error handling & resilience

- **Permanent** (no retry; notify operator, exit non-zero): bad credentials, captcha page,
  2FA-unpayable session, project 404/`create_project` denied, token 401, `max_permission < 1` on the
  target project (read from the response body), job-destroying config errors.
- **Transient** (retry with backoff): network timeout, 5xx, 429/503, EduPage `{"reload":true}`
  (→ transparent RPC re-login with stored credentials, then retry), Vikunja temp failures.
- **Retry**: per-item `retry_max` attempts with `retry_backoff_s` exponential growth (1,4,16…s).
  Items still failing become `sync_state='deferred'` and are retried next run — **never silently
  dropped**.
- **Deferred rows are excluded from the deletion/close path** (a deferred item that ages out of the
  window while Vikunja is down must not be closed as if it were seen-and-gone).
- **Gone-in-Vikunja**: a 404/410 on PATCH/close/delete, or reconcile seeing a mapped task's `project_id`
  differ from the configured project, means a human or another client removed/moved the task. Treat it
  as reconciled-away: drop the row; for `creating`/`deferred` clear `pending_ops` and reset to
  `created` so the item is re-created next cycle — never a silent skip, never an infinite 404 loop.
- **Circuit breaker**: after `breaker_threshold` consecutive transient failures stop the cycle, log
  and exit non-zero (timer restart policy re-triggers later). While the breaker is open, runs perform
  a **fetch + deferred-only pass** (re-apply deferred items) so deferred work is still attempted, and
  the breaker resets on a successful pass.
- **Idempotence by construction** (§6) makes retries and overlapping runs safe: re-applying a PATCH
  that already applied is a no-op.

---

## 12. Security

- Secrets only via environment/secret vault; the YAML/file and SQLite hold none.
- **EduPage credentials — pick one mode (config `edupage.auth.mode`):**
  - `password`: store the password, RPC re-login on demand. Works fully unattended where 2FA/captcha
    are not enforced; suffers captcha/2FA where they are.
  - `session`: reuse a seeded `PHPSESSID`, store no password. Meets 2FA/captcha schools; but a session
    that expires (e.g. mid-summer) can only be refreshed by a human. Track `session_seeded_at` in `meta`
    and send a `manual_attention` alert via `sync.notify_url` when a session-mode login fails, so the
    "unattended forever" promise is explicit rather than silent.
  - Default recommendation: `password` when the school allows it; `session` otherwise (with alerting).
- Vikunja token: created via `POST /tokens`, **scoped** (project read/write, no admin), stored in the
  vault when created (cleartext is returned exactly once); rotate periodically.
- Logging: never log cookies/tokens/passwords; task content only at `debug`, and only ids+fingerprint
  otherwise. `notice`/`error` logs mention timelineid/Vikunja task id, not bodies.
- **Shared-project privacy**: the target Vikunja project is one shared space. Homework bodies and
  subject-code labels are visible to people who can read the project; teacher metadata is omitted.
- `state.db`, config, and the lock file are `chmod 600`, owned by the service user.

---

## 13. Edge cases handled

- Teacher edits homework → content fingerprint changes → `PATCH`; unchanged fields untouched.
- Teacher reopens / un-checks homework → `done_state` diff → `done=false` even with identical content.
- Teacher reopens after a **policy-close** → §6 `reopen` transition (present item + `closed` row →
  `done=false`, state → `updated`).
- Overdue-but-still-active homework → kept open (close is due-date-anchored, §10).
- No due date → task created without `due_date`; closure uses the "no due date" fallback (§10).
- School-year rollover → `timelineid` is unique per account, so the year is **not** part of identity; a
  late-August assignment still in-window on 1 Sep stays the SAME task (`school_year` column just
  updates). No duplicate tasks at the boundary.
- Parent account / child switching → `switchchild` before fetch and **`edu_userid` in the key**:
  each child is a distinct namespace. Child B can never overwrite child A's mapping. (If two children
  must sync, use one project per child.)
- Same `timelineid` across students → the account id in the hidden marker distinguishes them.
- Vikunja description edited by a human → other human edits are left alone until a source change
  requires a content patch. Some Vikunja installations strip the hidden marker from Markdown;
  the state DB remains authoritative for already mapped tasks.
- Task deleted in Vikunja (human/other client) → reconcile prunes the row; item is re-created (§6/§11).
- Wiped state DB → reconciliation rebuilds identity from account-specific description markers or
  legacy anchors. Tasks missing both cannot be adopted safely (§6).
- EduPage session expired → RPC re-login (password mode) or `manual_attention` alert (session mode).
- Vikunja temporarily down → breaker + deferred; state untouched; nothing lost.
- Removing a configured extra label does not detach that label from existing tasks; sync is additive (§8).
- `timelineUserProps` absent on a history pull → fall back to the login payload's `userProps` (§3).
- Wall-clock/DST shifts → stored timestamps are UTC; `timezone` also sets local assignment-window,
  due-date, and priority date boundaries.
- `window_days > 30` → startup warning: retention past the recent month is unverified and a window
  that fails the operational coverage check disables the close path (§10); successful history
  responses do not prove completeness.

---

## 14. Remaining live verification

Implementation is present for the CLI, state store, EduPage and Vikunja clients, reconciliation, and
sync engine. The checks below still need a real account/instance before behavior should be treated as
verified. Resolved/confirmed already (from the v2 spec): no `uid` on `Task`; label attach body is `{label_id}`
(single) and label bulk is `PUT` (replace-set); `max_permission` is a body field, not a header;
`POST /tokens` creates tokens; pagination wrapper shape; `PUT /tasks/bulk` vs `POST /projects/{id}/tasks/bulk`.

Still to confirm against a live account/instance:

1. Check whether other schools or older homework formats use additional title, due-date, or subject keys.
2. `POST /timeline/?module=todo&akcia=getData&filterTab=messages` returns homework in-window; measure
   how far back history actually reaches (login payload ≈ recent month, the endpoint extends beyond;
   the real ceiling is load-bearing for §10). The response has no completeness signal, so successful
   requests cannot establish that the server returned every item.
3. **Marker durability:** create a task with `?format=markdown`, edit it via PATCH with
   `X-Vikunja-Format: markdown`, and re-fetch — confirm the account-specific HTML comment survives
   creation and an ordinary human edit.
4. Cancel/clear `due_date` via PATCH (the "teacher removes due date later" transition).
5. `priority` actual behaviour: spec declares a plain writable int with no range — probe accepted
   values/flags (the UI shows 0–3) and what each level does.
6. `max_permission` value observed in bodies for a rw-scoped token on the target project.
7. `switchchild` request/response on a real parent account; confirm per-child namespacing.
8. `per_page` clamp: compare `GET /info` `max_items_per_page` vs effective API behaviour.
9. Run the full cycle in `dry_run` first; inspect its printed action plan and compare with Vikunja UI.

---

## 15. Implementation coverage

| Area | Current implementation |
| --- | --- |
| CLI | `run`, `daemon`, `check`, and `seed-session`; omitted command defaults to `run` |
| Fetch and apply | EduPage homework fetch and Vikunja task reconciliation are implemented; live account behavior remains unverified |
| State and scheduling | SQLite state, run lock, daemon cadence, health check, retry and breaker paths are implemented |
| Dry run | Uses a read-only on-disk snapshot and in-memory state, skips the lock and project creation, and prints the planned actions |
| Remaining work | Complete the live verification list in §14 and update this document where observed behavior differs |

---

## 16. References

- `docs/edupage-api.md` — EduPage endpoints, login flow, eqap encoding, homework timeline (inspiration
  source only; the third-party library is NOT a dependency).
- `docs/vikunja-api.md` — Vikunja v2 OpenAPI notes from the live instance.
- Target project is configured, not discovered: one EduPage account → one Vikunja project by default.
