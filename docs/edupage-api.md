# EduPage API notes

Source (inspiration only, NOT imported as a dependency): <https://github.com/EdupageAPI/edupage-api> (+ docs https://edupageapi.github.io/edupage-api/). We replicate the endpoints directly, they are undocumented — this is reverse-engineered knowledge.

Base URL for all school endpoints: `https://{subdomain}.edupage.org`.

## What it is

- Not a Selenium scraper: plain `requests` against EduPage's real endpoints, then parses HTML/JS-embedded JSON.
- Session cookie: `PHPSESSID` on the `{subdomain}.edupage.org` domain.
- Timeout default 5s (raise for big file uploads).

## Authentication / login

Ways in:

1. **RPC login (primary, JSON):**
   - `GET /login/?cmd=MainLogin` (200 check)
   - `POST /login/?cmd=MainLogin&akcia=getToken`, body = `RequestData.encode_request_body({"rpcparams": json.dumps({"username": ..., "edupage": ""})})` → response has `token`.
   - `POST /login/?cmd=MainLogin&akcia=login`, same encoding, `rpcparams={"username","password","userToken": token,"edupage":"","ctxt":"","tu":None,"gu":None,"au":None}` → `{"err":{"error_id"},"redirectUrl"}`. `error_id == "invalid_token"` = CSRF rejected. Follow `redirectUrl`.
2. **Form fallback (HTML):**
   - `GET /login/?cmd=MainLogin` → extract `"csrftoken":"..."`.
   - `POST /login/edubarLogin.php` with `{csrfauth, username, password}`.
   - Failure markers in redirect URL: `cap=1` or `lerr=b43b43` → **captcha**; `bad=1` → **bad credentials**.
3. **Auto-login (no subdomain given):** `login()` with default subdomain `login1` posts to `https://login1.edupage.org/*` and the real school subdomain is resolved from the redirect hostname (`login1` → `{school}`). (`portal.edupage.org` only appears in the library's docstrings; the wire base is `login1.edupage.org`.)
4. **2FA:** after login redirect to `.../login/twofactor`. Parse hidden fields `csrfauth`, `au`, `gu` from `GET /login/twofactor?sn=1`. Poll `POST /login/twofactor?akcia=checkIfConfirmed` (status `ok` → body has `data` code; `fail` → wait), or `akcia=resendNotifs` to resend. Finish with `POST /login/edubarLogin.php` params `{csrfauth, t2fasec: code, 2fNoSave:"y", 2fform:"1", gu, au}` → success when response text contains `window.location = gu;`.
5. **Reuse existing session:** `GET /user` with `PHPSESSID` cookie set, then parse login JSON (`from_session_id`).
6. **Parent account switching (all GET, not POST):**
   - switch to a child: `GET /login/switchchild?studentid={person_id}` → body `OK`
   - switch back to parent / to another school row: `GET /login/edupageChange?rid=edupage;{subdomain};{username}`
   - list accessible schools: profile-switcher rows (`data-rowid="edupage;{subdomain};{user};rodic"`) parsed from `GET /user`.

### Login payload parsing (critical)

Login/`/user` pages embed a JS call `userhome(<json>);`. Extract that JSON as `data`:

- `data["userid"]` — user id (`Student123` / `Teacher45` / `Rodic…`)
- `data["dp"]["year"]` — current school year (int, starting year)
- `data["dbi"]` — reference tables: `classes`, `teachers`, `subjects`, `classrooms` (id → `{name, short, ...}`)
- `data["items"]` — current timeline/notification items (see Timeline)
- `data["userProps"]` — per-item user state (`starred`, `doneMaxCas` = done timestamp)
- Also scrape `ASC.gsechash="<hash>"` from the page → the **`gsec_hash`** used as `__gsh` by the JSX RPC endpoints below.

## Token-name glossary (gsec_hash / gsechash / gsh)

These are the same kind of value under different names:

- **`gsec_hash`** — canonical name in these notes; the authenticated RPC hash.
- **`gsechash`** — the literal EduPage script variable name (`ASC.gsechash="…"` on the login page, `gsechash=` inside `/dashboard/eb.php`).
- **`gsh`** — the param `__gsh` sent in JSX RPC bodies, and *separately* a form field of the same name in the older GP `/gcall` protocol (scraped from `/dashboard/eb.php?mode=ttday` — same kind of token, independent scrape, used with `gpid`).

## Generic request/response encoding (`eqap` scheme)

EduPage "module" endpoints (login RPC, messages) don't take plain JSON. They use a custom compressed+base64 form:

**Request body** (form-urlencoded), keys:
- `eqap` = `"dz:" + chromium_base64( raw-deflate( urlencoded_json_body ) )`
- `eqacs` = `sha1(eqap).hexdigest()`
- `eqaz` = `"1"` (use compression)

Encoding details (request side only):
- Deflate: `zlib.compressobj(-1, zlib.DEFLATED, -15, 8, zlib.Z_DEFAULT_STRATEGY)` — raw deflate, no header (wbits=-15).
- Base64 is Chromium `btoa`-compatible RFC 4648; on encode it keeps `=` padding, on decode it strips padding and tolerates/*ignores* line-folding whitespace (`\t\n\f\r`) on decode. For plain ASCII/latin-1 bodies Python `base64.b64encode` produces byte-identical output; the difference vs stdlib is only in the lenient decode path.

**Response decoding, by prefix (base64-only — responses are NOT zlib-inflated):**
- `eqz:` → chromium-base64-decode the trailing data (compressed marker, but body is already base64-only)
- `eqwd:` → chromium-base64-decode the trailing data (e.g. error payloads)
- otherwise → response is already plain (JSON/HTML)

`eqap` encoding is used by: `login` RPC and `send_message`. The `/gcall` GP-protocol call and the JSON JSX calls use plain encodings — do not eqap-encode them.

## JSX "server/*.js" RPC endpoints (JSON, NOT eqap-encoded)

POST JSON `{"__args": [null, <args>], "__gsh": <gsec_hash>}` to `https://{subdomain}.edupage.org/{path}/server/{func}.js?__func={FunctionName}`. Response is JSON; check `"reload"` truthy = expired/invalid gsechash; else read `"r"`. (Only the substitution and online-lesson helper endpoints reference `reload`; timetable `curentttGetData` and school-wide `mainDBIAccessor` do not emit it — replicate the check on every call ourselves.)

| Purpose | URL / __func | args |
| --- | --- | --- |
| School-wide students | `/rpr/server/maindbi.js?__func=mainDBIAccessor` | `[null, school_year, {}, {"op":"fetch","needed_part":{"students":["id","classid","short"]}}]` → `r.tables[0].data_rows` |
| Timetable | `/timetable/server/currenttt.js?__func=curentttGetData` | `{year, datefrom, dateto, table, id, showColors…}` → `r.ttitems`; `table` = lowercase plural `students` / `teachers` / `classes` / `classrooms`, `id` = the entity's `person_id` / `class_id` / `classroom_id` |
| Substitution | `/substitution/server/viewer.js?__func=getSubstViewerDayDataHtml` | `{date, mode:"classes"}` → `r` = HTML (missing teachers, changes) |
| Online lessons sign-in | `/dashboard/server/onlinelesson.js?__func=getOnlineLessonOpenUrl` | `{click, date, ol_url, subjectid}`; `__gsh` is re-scraped fresh from `GET /dashboard/eb.php` (split `gsechash=`) rather than the login hash |

## Other known endpoints

| Endpoint | Method/params | Returns |
| --- | --- | --- |
| `/timeline/` `?module=todo&akcia=getData&filterTab=messages` | `POST`, form `datefrom=YYYY-MM-DD` (note: the client sends `filterTab` twice — once empty, once `messages`) | JSON `timelineItems` + `timelineUserProps` (notification **history**) and a separate `homeworks` list; when `timelineUserProps` is absent the client falls back to the login payload's `userProps` |
| `/elearning/?cmd=EtestCreator&akcia=getResultsData` | `POST`, `eqap`-encoded form `{superid: N}` | JSON containing `superid`, `testid`, `etestType`, and `resultsData.planid`; read-only assignment detail lookup |
| `/timeline/?=&akcia=createItem&eqav=1&maxEqav=7` | eqap-encoded `{selectedUser, text, attachements, receipt, typ:"sprava"}` | decoded JSON `changes[0].timelineid` (sends message) |
| `/timeline/?akcia=uploadAtt` | file upload | attachment (also used for cloud uploads) |
| `/znamky/` | `GET` | grades HTML/JS to parse (`.znamkyStudentViewer(` JSON) |
| `/znamky/?what=studentviewer&znamky_yearid={year}&nadobdobie={term}` | **POST**; `Term` = `P1` (first) / `P2` (second) | grades for one term |
| `/menu/?date=YYYYMMDD` | GET (date is **compact `%Y%m%d`**, no dashes) | lunch/menu HTML |
| `/gcall` | `POST`, **plain form** `{gpid, gsh, action:"loadData", user, changes:"{}", date, dateto, _LJSL:"4096"}` | curriculum/day plan; `gpid`/`gsh` scraped from `/dashboard/eb.php?mode=ttday` (send `gpid+1`, "fresh token per call") |
| `/dashboard/eb.php` | GET | dashboard; scrape a fresh `gsechash=` (used as the online-lesson `__gsh`) |
| `/dashboard/eb.php?mode=ttday` | GET | scrape `gpid=` and `gsh=` (used for the `/gcall` curriculum call, sending `gpid+1`) |

## Data model notes

- Person detection: `numberinclass` present → Student; `classroomid` present → Teacher; else Parent. Recipient id strings: `Student{id}`, `Teacher{id}`, `StudentOnly{id}`, `Rodic{id}`.
- Timeline event fields: `timelineid`, `typ` (event type, e.g. `homework`, `sprava` (message), `znamka` (grade), `testpridelenie`…), `timestamp` (`YYYY-MM-DD HH:MM:SS`), `text`, `data` (JSON string with detail, e.g. `messageContent`, `nazov`, and — for homework — `oldVals`), `user_meno` / `vlastnik_meno` (recipient/author display names; the special value `"Celá škola"`/"*" = everyone is honoured for the **recipient** side; the author branch only special-cases `*`), `pocet_reakcii`, `cas_pridania`, `removed`.
- Live homework records use `data.nazov` for the current title, `data.date` for the due date (`YYYY-MM-DD`), and `data.predmetid` for the subject id; `oldVals` is a fallback. Assigned-test records may have `data.parametre.predmetid`. A `data.planid` can also resolve through `dbi.plans[planid].predmetid`, then `dbi.subjects` supplies the short name.
- Some timeline homework records include `data.superid` and `data.planid`, but no `testid` or direct URL. `getResultsData` resolves the missing ID. An e-learning URL encodes `cmd=ETestCreator`, `planid`, `testid`, `superid`, `cspohladStart=tests`, `pohlad=results:overview`, `etestType`, and `edit=` as a base64 `eqa` query value. The URL requires an authenticated EduPage session. Plain homework without `superid` keeps the timeline fallback.
- The author (**`vlastnik_meno`**) is available and is resolved to an account by the client.
- `userProps[timelineid]` per-user state: `starred` (`"1"`), `doneMaxCas` (done timestamp, set => homework done).
- **Homework event type is `typ == "homework"`** (`EventType.HOMEWORK`). Do NOT use `h_homework` — that is a different, unrelated "helper" enum value (`EventType.H_HOMEWORK`), not a homework event. The client filters homework with `EventType.HOMEWORK` (value `"homework"`).
- Most current items already live in the login `data["items"]` (≈ the recent month); the `/timeline/?akcia=getData` endpoint exists to fetch **older** history — the reference client reaches back further than a month with it. Actual server-side retention is unverified; assume only the recent month is trustworthy until measured (§14 of `application.md`).
- School year: `data["dp"]["year"]` (starting year of school year).

## Lessons for our client (EduPage → Vikunja sync)

- Keep one `requests.Session` for cookies; maintain `{subdomain, userid, gsec_hash, dbi, dp}` state.
- For tasks/homework: `POST /timeline/?module=todo&akcia=getData&filterTab=messages` → `timelineItems`, filter `typ == "homework"` (plus optional `testpridelenie`, `etesthw`), enrich subject/teacher names via `dbi`, format HTML `data` → Markdown (use Vikunja's `format=markdown`).
- Required low-level pieces: chromium base64 (btoa/atob semantics incl. lenient decode), raw-deflate codec, `eqap/eqacs/eqaz` request wrapper, and `__args/__gsh` JSON RPC.
