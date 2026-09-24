# Vikunja API notes (v2)

Sources: <http://100.119.216.58:3456/api/v2/docs> (Redoc UI), spec at `http://100.119.216.58:3456/api/v2/openapi.json`.

- Service: **Vikunja API v2.6.0** (AGPL-3.0-or-later), self-hosted.
- **Use the v2 API**: base URL `http://100.119.216.58:3456/api/v2` (project decision; this instance's
  docs still reference a v1 path family — e.g. `/api/v1/tokens` and `/api/v1/login` under authorization).
- Spec is OpenAPI 3.1 (131 paths, 137 schemas).

## Authorization

Global `security` default: either `JWTKeyAuth` or `APITokenAuth` on most endpoints. Public endpoints that declare `security: []` (10 ops): `GET /info`, `GET /health`, `POST /login`, `POST /register`, `POST /oauth/token`, `POST /shares/{share}/auth`, `POST /user/confirm`, `POST /user/password/reset`, `POST /user/password/token`, `POST /user/token/refresh`. Of the oauth pair only `/oauth/token` is public; `/oauth/authorize` requires auth.

1. **JWT** (main): `POST /login` -> `{ "token": "<jwt>" }`. Body `Login` schema: `username`, `password`, optional `long_token`, optional `totp_passcode`. Send as `Authorization: Bearer <jwt-token>`.
2. **API tokens** (scoped): `tk_`-prefixed tokens. Created via `POST /tokens` (create is `POST` in v2, was `PUT` in v1). Body `APIToken`-ish: `{ title, expires_at, permissions }`; cleartext `token` returned **only in the create response**. Send as `Authorization: Bearer <token>`. Permission routes reference `GET /routes`.
3. **BasicAuth** (HTTP basic): only for the notifications Atom feed (`/notifications.atom`) — username = token owner, password = feeds-scoped API token.

Also: `POST /logout`, `POST /user/token/refresh`, session management (`/user/sessions`).

## Conventions

- **Pagination** (`page`, `per_page`) returns an inline body wrapper, e.g. `PaginatedAPIToken`/`PaginatedTask` `{ items, page, per_page, total, total_pages }`. Default `per_page` = 50, max 1000 (verify actual cap via `GET /info` → `max_items_per_page`). Header-based pagination (`x-pagination-*`) is the v1 style; v2 uses the body wrapper.
- **Permissions**: single-item responses expose the caller's permission as **`max_permission` in the response body** (int: `0` = Read Only, `1` = Read & Write, `2` = Admin — present on `TaskReadOneBody`, `Project`, etc.). No `x-max-permission` header is declared in the v2 spec; the app reads `max_permission` from the body.
- **Errors**: error responses are `application/problem+json` (RFC 9457) with numeric `code` (links <https://vikunja.io/docs/errors/>) plus `title`/`status`/`detail`. Always check the error `code`, not just HTTP status. There is no top-level `message` field; per-item detail may appear in nested `errors[].message`.
- **Rich-text fields** (big change in v2): descriptions (task, project, label, team, saved filter) and task comments are stored as **HTML**.
  - Use `?format=markdown` to read/write them as GFM Markdown instead; written Markdown is converted to HTML and `@mentions` resolved to users.
  - On `PATCH` use header `X-Vikunja-Format: markdown` (merge-patch drops query params). The header is
    documented in spec prose, not as a declared parameter — treat it as convention.
  - CalDAV always exchanges task descriptions as Markdown.
  - Writing is lossy (Markdown can't express all HTML); send `format=html` for round-tripping resources you didn't edit.
  - Unknown `@mentions` stay as plain text.

## Instance info & health

- `GET /info` -> `VikunjaInfos`: version, enabled features (caldav, attachments, comments, webhooks, totp, link sharing, user deletion, ...), frontend_url, `max_file_size` (readable string, e.g. "20MB"), `max_items_per_page`, migrators.
- `GET /health` -> healthcheck (new in v2).

## Resource groups (131 paths)

| Group | Endpoints |
| --- | --- |
| Auth | `/login`, `/logout`, `/register`, `/user/token`, `/user/token/refresh`, `/oauth/authorize`, `/oauth/token` |
| User | `/user` (`UserInfoBody`), `/user/settings/totp*`, `/user/export*`, `/user/deletion/*`, `/user/password*`, `/user/sessions`, `/user/bots`, `/users`, `/avatar/{username}` |
| Tokens | `/tokens`, `/tokens/{id}`, `/token/test` |
| Projects | `/projects`, `/projects/{id}`, duplicate, background, views, buckets, webhooks, shares, users/teams |
| Tasks | `/tasks`, `/tasks/bulk` (`PUT`, update many), `/tasks/{projecttask}`, read state, position, duplicate, attachments, assignees, comments, relations, labels, `/projects/{project}/tasks/by-index/{index}` |
| Time entries | `/time-entries`, `/time-entries/{id}`, `/time-entries/timer/stop`, `/projects/{project_id}/time-entries`, `/tasks/{task_id}/time-entries` (new in v2) |
| Views & buckets | `/projects/{project}/views`, `/views/{view}/buckets`, `.../tasks` |
| Teams | `/teams`, `/teams/{id}`, members, admins |
| Labels | `/labels`, `/labels/{id}`; attach to task `POST /tasks/{projecttask}/labels` (body `LabelTask{label_id}`, 201) |
| Filters | `/filters`, `/filters/{filter}` |
| Migrations | v2 ships CSV, Planka, TickTick, Wekan, Vikunja-file migrators |
| Webhooks | `/webhooks/events`, `/user/settings/webhooks/*`, `/projects/{project}/webhooks*` |
| Shares / subscriptions | `/projects/{project}/shares`, `/shares/{share}/auth`, `/subscriptions/{entity}/{entityID}` |
| Notifications | `/notifications`, `/notifications/{notificationid}`, `/notifications.atom` (BasicAuth) |
| Reactions | `/{entitykind}/{entityid}/reactions`, `.../delete` |
| Admin | `/admin/overview`, `/admin/projects`, `/admin/users` (incl. password reset email endpoints) |
| Misc | `/info`, `/health`, `/routes` |

## Key endpoint shapes

- `POST /login` (v2 path too) -> `{ token }`; body supports `long_token` (long-lived) + `totp_passcode`
- `GET /user` -> `UserInfoBody`
- `GET /tasks` — filters/sort via query params: `q`, `filter`, `filter_timezone`, `filter_include_nulls`, `sort_by`, `order_by`, `expand`, `format`
- `POST /projects/{project}/tasks` — create task (body `Task`; `project_id` taken from URL; 201; supports `?format=markdown` for the description)
- `PUT /tasks/bulk` — bulk-update ("apply `fields` of `values` to every task in `task_ids`"). `POST /projects/{project}/tasks/bulk` instead creates up to 100 tasks atomically.
- `PATCH /tasks/{projecttask}` — partial update (use `X-Vikunja-Format: markdown` for the description); `PUT /tasks/{projecttask}` — full update
- `GET/POST /projects/{project}/views` — list/create views
- `POST /tokens` — create API token -> `APIToken` (cleartext once)
- Labels: create `POST /labels` (body `Label`); attach one `POST /tasks/{projecttask}/labels` (body `{label_id}`); **replace the whole label set** `PUT /tasks/{projecttask}/labels/bulk` (body `LabelTaskBulk { labels: [Label …] }` — adds missing labels, removes any not in the list)

## Key schemas (v2)

- `Project`: `id`, `title`, `description`, `identifier` (short code in task IDs), `hex_color`, `is_archived`, `is_favorite`, `position`, `parent_project_id`, `owner`, `max_permission`, `background_*`, `views`, `subscription`, created/updated.
- `Task` (no `uid`/`external_id` field — mapping must be client-side): `title`, `description` (HTML; Markdown via `format`), `due_date`, `priority` (writable int; spec declares no range/scale — probe before use), `labels` (read-only in body — use the label endpoints), `bucket_id`, `project_id` (on create from URL; setting on update moves the task), `identifier`, `index`, `done`, `done_at`, `percent_done`, `start_date`, `end_date`, `repeat_after`, `reminders`, created/updated.
- `APIToken`: `id`, `title`, `token` (cleartext only on create), `expires_at`, `permissions`, `owner_id` (set to bot's ID for bot tokens), `created`.
- `PaginatedTask`/`PaginatedAPIToken`: `{ items, page, per_page, total, total_pages }`.