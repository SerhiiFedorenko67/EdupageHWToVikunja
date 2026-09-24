"""EduPage client: a session-holding wrapper over EduPage's wire endpoints.

Reverse-engineered protocol (see docs/edupage-api.md): eqap-encoded RPC login
(getToken -> login), HTML form fallback, 2FA polling, session reuse via
PHPSESSID, parent-child switch, timeline history fetch, and homework
extraction from the userhome(tm) login payload.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import requests

from edupagetasks.config import EduPageConfig
from edupagetasks.encoding import chromium_b64decode, decode_request_body, eqap_encode
from edupagetasks.models import HomeworkItem

logger = logging.getLogger(__name__)

DEFAULT_INCLUDE_TYPES = ["homework", "testpridelenie", "etesthw"]

LOGIN_URL = "/login/?cmd=MainLogin"
USER_PATH = "/user"
SUBJECT_ID_KEYS = ("predmetid", "subjectid", "subject", "termid")

_TWOFA_TIMEOUT = 60.0
_TWOFA_INTERVAL = 2.0


class EduPageError(Exception):
    """Base for all EduPage client errors."""


class EduPageAuthError(EduPageError):
    """Permanent authentication failure (bad credentials / 2FA / session)."""


class EduPageCaptchaError(EduPageAuthError):
    """Captcha presented; unattended login impossible."""


class EduPageTransientError(EduPageError):
    """Network/timeout/5xx failure; safe to retry."""


class EduPageReloadRequired(EduPageTransientError):
    """A JSX RPC replied ``{"reload": true}`` -- re-login and retry (transient)."""


@dataclass
class LoginState:
    userid: str = ""
    school_year: int = 0
    dbi: dict[str, dict] = field(default_factory=dict)
    gsec_hash: str = ""
    items: list[dict] = field(default_factory=list)
    user_props: dict[str, dict] = field(default_factory=dict)


class EduPageClient:
    def __init__(self, cfg: EduPageConfig, *, timeout: float = 10.0) -> None:
        self.cfg = cfg
        self.timeout = timeout
        self.session = requests.Session()

    @property
    def _host(self) -> str:
        return self.cfg.url.split("://", 1)[1]

    def _url(self, path: str) -> str:
        return self.cfg.url + path

    def _request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        try:
            resp = self.session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            raise EduPageTransientError(str(exc)) from exc
        if resp.status_code >= 500:
            raise EduPageTransientError(f"server error {resp.status_code} from {url}")
        if resp.status_code == 429:
            raise EduPageTransientError(f"rate limited ({resp.status_code}) by {url}")
        if resp.status_code in (401, 403):
            raise EduPageAuthError(f"authentication failed ({resp.status_code}) for {url}")
        if resp.status_code >= 400:
            raise EduPageError(f"request failed ({resp.status_code}) for {url}")
        return resp

    def connect(self) -> LoginState:
        if self.cfg.auth.mode == "session":
            return self._connect_session()
        return self._connect_password()

    def refresh(self) -> LoginState:
        self.session = requests.Session()
        return self.connect()

    def fetch_history(
        self, state: LoginState, date_from: str
    ) -> tuple[list[dict], dict[str, dict]]:
        url = self._url("/timeline/")
        params = [("module", "todo"), ("akcia", "getData"),
                  ("filterTab", ""), ("filterTab", "messages")]
        resp = self._request("POST", url, params=params, data={"datefrom": date_from})
        try:
            payload = self._decode_payload(resp.text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EduPageTransientError("unreadable timeline response") from exc
        if not isinstance(payload, dict):
            raise EduPageTransientError("malformed timeline response")
        items = payload.get("timelineItems")
        props = payload.get("timelineUserProps", {})
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise EduPageTransientError("malformed timelineItems in timeline response")
        if not isinstance(props, dict):
            raise EduPageTransientError("malformed timelineUserProps in timeline response")
        return items, props

    def jsx_call(self, path: str, func: str, args: Any, state: LoginState) -> Any:
        url = f"{self.cfg.url}{path}?__func={func}"
        resp = self._request(
            "POST", url, json={"__args": [None, args], "__gsh": state.gsec_hash}
        )
        try:
            payload = resp.json()
        except ValueError as exc:
            raise EduPageTransientError("unreadable JSX response") from exc
        if payload.get("reload"):
            raise EduPageReloadRequired(f"reload required by {func}")
        return payload.get("r")

    def homework_items(
        self, state: LoginState, *, include_types: list[str] | None = None
    ) -> list[HomeworkItem]:
        include_set = set(
            DEFAULT_INCLUDE_TYPES if include_types is None else include_types
        )
        return [
            self._extract_homework(raw, state)
            for raw in state.items
            if isinstance(raw, dict) and raw.get("typ") in include_set
        ]

    def merge_history(
        self,
        state: LoginState,
        history_items: list[dict],
        history_props: dict[str, dict],
    ) -> list[HomeworkItem]:
        """Merge login-payload + history items, deduped by timelineid.

        Per docs/edupage-api.md the login payload's ``userProps`` is the
        FALLBACK only when the history pull carries no ``timelineUserProps``
        at all (a per-key union would let stale login done-state win over a
        fresh history view).
        """
        merged_items = self._merge_items(state.items, history_items)
        if history_props:
            merged_props = dict(history_props)
        else:
            merged_props = dict(state.user_props or {})
        merged_state = LoginState(
            userid=state.userid,
            school_year=state.school_year,
            dbi=state.dbi,
            gsec_hash=state.gsec_hash,
            items=merged_items,
            user_props=merged_props,
        )
        return self.homework_items(merged_state)

    # -- internals: login -------------------------------------------------

    def _connect_password(self) -> LoginState:
        self._rpc_login()
        if self.cfg.child_person_id:
            self._switchchild(self.cfg.child_person_id)
        return self._load_user()

    def _rpc_login(self) -> None:
        self._request("GET", self._url(LOGIN_URL))
        for _ in range(2):
            try:
                token = self._get_token()
            except EduPageError as exc:
                logger.debug("getToken failed (%s); using form fallback", exc)
                self._form_login()
                return
            try:
                redirect = self._login(token)
            except EduPageReloadRequired:
                logger.debug("login CSRF token rejected; retrying")
                continue
            if redirect:
                self._follow_login_redirect(redirect)
            return
        self._form_login()

    def _get_token(self) -> str:
        rpcparams = {"username": self.cfg.auth.username, "edupage": ""}
        body = eqap_encode(decode_request_body({"rpcparams": json.dumps(rpcparams)}))
        resp = self._request(
            "POST", self._url(LOGIN_URL + "&akcia=getToken"), data=body
        )
        try:
            payload = self._decode_payload(resp.text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EduPageTransientError("unreadable token response") from exc
        token = payload.get("token") if isinstance(payload, dict) else None
        if not token:
            raise EduPageAuthError("no token in RPC login response")
        return str(token)

    def _login(self, token: str) -> str | None:
        rpcparams = {
            "username": self.cfg.auth.username,
            "password": self.cfg.auth.password,
            "userToken": token,
            "edupage": "",
            "ctxt": "",
            "tu": None,
            "gu": None,
            "au": None,
        }
        body = eqap_encode(decode_request_body({"rpcparams": json.dumps(rpcparams)}))
        resp = self._request("POST", self._url(LOGIN_URL + "&akcia=login"), data=body)
        self._check_login_markers(resp.url)
        if "userhome(" in resp.text:
            return None
        try:
            payload = self._decode_payload(resp.text)
        except (json.JSONDecodeError, ValueError) as exc:
            raise EduPageTransientError("unreadable login response") from exc
        if not isinstance(payload, dict):
            raise EduPageAuthError("malformed login response")
        redirect = payload.get("redirectUrl")
        if redirect:
            return str(redirect)
        err = payload.get("err") or {}
        if isinstance(err, dict) and err.get("error_id") == "invalid_token":
            raise EduPageReloadRequired("login CSRF token rejected")
        raise EduPageAuthError(f"EduPage login refused: {err}")

    def _follow_login_redirect(self, redirect: str) -> None:
        if not redirect.startswith("http"):
            redirect = self._url(redirect)
        resp = self._request("GET", redirect, allow_redirects=True)
        self._check_login_markers(resp.url)
        if "twofactor" in resp.url:
            self._twofactor(resp.url)

    def _twofactor(self, url: str) -> None:
        if not url.startswith("http"):
            url = self._url(url)
        snap = url + ("&" if "?" in url else "?") + "sn=1"
        resp = self._request("GET", snap)
        csrfauth = self._hidden_field(resp.text, "csrfauth")
        gu = self._hidden_field(resp.text, "gu")
        au = self._hidden_field(resp.text, "au")
        if not (csrfauth and gu and au):
            raise EduPageAuthError("2FA required/unsupported: fields not found")
        code = None
        deadline = time.monotonic() + _TWOFA_TIMEOUT
        while time.monotonic() < deadline:
            try:
                poll = self._request(
                    "POST", self._url("/login/twofactor?akcia=checkIfConfirmed")
                )
                data = poll.json()
            except (ValueError, EduPageTransientError):
                data = None
            if isinstance(data, dict) and data.get("data"):
                code = str(data["data"])
                break
            time.sleep(_TWOFA_INTERVAL)
        if code is None:
            raise EduPageAuthError(
                "2FA required/unsupported: code not confirmed in time"
            )
        params = {
            "csrfauth": csrfauth,
            "t2fasec": code,
            "2fNoSave": "y",
            "2fform": "1",
            "gu": gu,
            "au": au,
        }
        complete = self._request(
            "POST",
            self._url("/login/edubarLogin.php"),
            data=params,
            allow_redirects=True,
        )
        if (
            "window.location = gu;" not in complete.text
            and "userhome(" not in complete.text
        ):
            raise EduPageAuthError("2FA completion failed")

    @staticmethod
    def _hidden_field(page: str, name: str) -> str:
        for pattern in (
            rf'<input[^>]*name="{name}"[^>]*value="([^"]*)"',
            rf'<input[^>]*value="([^"]*)"[^>]*name="{name}"',
        ):
            match = re.search(pattern, page)
            if match:
                return match.group(1)
        return ""

    def _form_login(self) -> None:
        page = self._request("GET", self._url(LOGIN_URL)).text
        match = re.search(r'"csrftoken":"([^"]+)"', page)
        if not match:
            raise EduPageAuthError("login page did not expose csrftoken")
        params = {
            "csrfauth": match.group(1),
            "username": self.cfg.auth.username,
            "password": self.cfg.auth.password,
        }
        resp = self._request(
            "POST",
            self._url("/login/edubarLogin.php"),
            data=params,
            allow_redirects=True,
        )
        self._check_login_markers(resp.url)

    def _switchchild(self, person_id: str) -> None:
        resp = self._request(
            "GET", self._url(f"/login/switchchild?studentid={person_id}")
        )
        if resp.text.strip() != "OK":
            raise EduPageAuthError("switchchild failed")

    @staticmethod
    def _check_login_markers(url: str) -> None:
        if "cap=1" in url or "lerr=b43b43" in url:
            raise EduPageCaptchaError("captcha presented by EduPage")
        if "bad=1" in url:
            raise EduPageAuthError("bad EduPage credentials")

    def _connect_session(self) -> LoginState:
        if not self.cfg.auth.session_id:
            raise EduPageAuthError("session mode requires a session_id")
        self.session.cookies.set(
            "PHPSESSID", self.cfg.auth.session_id, domain=self._host, path="/"
        )
        resp = self._request("GET", self._url(USER_PATH))
        if "userhome(" not in resp.text:
            raise EduPageAuthError("EDUPAGE_SESSION_ID expired or invalid")
        if self.cfg.child_person_id:
            self._switchchild(self.cfg.child_person_id)
        return self._load_user()

    # -- internals: userhome parsing --------------------------------------

    def _load_user(self) -> LoginState:
        resp = self._request("GET", self._url(USER_PATH))
        payload = self._parse_userhome(resp.text)
        raw_userid = payload.get("userid")
        if isinstance(raw_userid, bool) or not isinstance(raw_userid, (str, int)):
            raise EduPageAuthError("userhome payload has no valid userid")
        userid = str(raw_userid).strip()
        if not userid:
            raise EduPageAuthError("userhome payload has no valid userid")
        if "items" not in payload or not isinstance(payload["items"], list):
            raise EduPageTransientError("userhome payload has malformed items")
        raw_items = payload["items"]
        if any(not isinstance(item, dict) for item in raw_items):
            raise EduPageTransientError("userhome payload has malformed items")

        dp = payload.get("dp")
        if not isinstance(dp, dict):
            dp = {}
        school_year = (dp or {}).get("year")
        if isinstance(school_year, str) and school_year.isdigit():
            school_year = int(school_year)
        elif not isinstance(school_year, int):
            school_year = 0
        dbi = (
            self._decode_embedded(payload.get("dbi"), "dbi")
            if isinstance(payload, dict)
            else {}
        )
        user_props = (
            self._decode_embedded(payload.get("userProps"), "userProps")
            if isinstance(payload, dict)
            else {}
        )
        if dbi is not None and not isinstance(dbi, dict):
            raise EduPageTransientError("malformed EduPage dbi data")
        if user_props is not None and not isinstance(user_props, dict):
            raise EduPageTransientError("malformed EduPage userProps data")
        items = raw_items
        state = LoginState(
            userid=userid,
            school_year=school_year,
            dbi=dbi or {},
            gsec_hash=self._parse_gsechash(resp.text),
            items=items,
            user_props=user_props or {},
        )
        logger.debug("connected as %s", state.userid)
        return state

    @staticmethod
    def _decode_payload(data: str) -> Any:
        """Decode a base64/eqz/eqwd response payload (or plain JSON) to a Python object."""
        text = data.lstrip()
        if text.startswith("eqz:"):
            text = chromium_b64decode(text[4:]).decode("utf-8")
        elif text.startswith("eqwd:"):
            text = chromium_b64decode(text[5:]).decode("utf-8")
        return json.loads(text)

    @staticmethod
    def _parse_userhome(page: str) -> dict:
        # userhome(<json>); -- the payload is JSON that may itself contain
        # brackets/semicolons inside string values, so locate the matching
        # closing paren with a string-aware scan instead of a regex.
        start = page.find("userhome(")
        if start == -1:
            raise EduPageAuthError("userhome payload not found in page")
        begin = start + len("userhome(")
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for i in range(begin, len(page)):
            ch = page[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "(":
                depth += 1
            elif ch == ")":
                if depth == 0:
                    end = i
                    break
                depth -= 1
        if end == -1:
            raise EduPageAuthError("malformed userhome payload")
        try:
            data = EduPageClient._decode_payload(page[begin:end])
        except (json.JSONDecodeError, ValueError) as exc:
            raise EduPageAuthError("malformed userhome payload") from exc
        if not isinstance(data, dict):
            raise EduPageAuthError("malformed userhome payload")
        return data

    @staticmethod
    def _parse_gsechash(page: str) -> str:
        match = re.search(r'ASC\.gsechash="([^"]+)"', page)
        return match.group(1) if match else ""

    @staticmethod
    def _maybe_decode(value: Any) -> Any:
        if isinstance(value, str) and value.startswith(("eqz:", "eqwd:")):
            return EduPageClient._decode_payload(value)
        return value

    @staticmethod
    def _decode_embedded(value: Any, field_name: str) -> Any:
        try:
            return EduPageClient._maybe_decode(value)
        except (ValueError, UnicodeError, TypeError) as exc:
            raise EduPageTransientError(
                f"malformed encoded EduPage {field_name} data"
            ) from exc

    # -- internals: homework extraction -----------------------------------

    def _extract_homework(self, raw: dict, state: LoginState) -> HomeworkItem:
        data = self._parse_item_data(raw)
        old = data.get("oldVals") if isinstance(data, dict) else None
        title = data.get("nazov") if isinstance(data, dict) else None
        due_date = data.get("date") if isinstance(data, dict) else None
        if not isinstance(title, str):
            title = None
        if not isinstance(due_date, str) or not self._valid_date(due_date):
            due_date = None
        if not title and isinstance(old, dict):
            title = old.get("title")
        if not isinstance(title, str):
            title = None
        if due_date is None and isinstance(old, dict):
            old_date = old.get("date")
            if isinstance(old_date, str) and self._valid_date(old_date):
                due_date = old_date
        subject_id = self._subject_id(data)
        if subject_id is None and isinstance(data, dict):
            plans = state.dbi.get("plans") if isinstance(state.dbi, dict) else None
            plan = plans.get(str(data.get("planid"))) if isinstance(plans, dict) else None
            subject_id = self._subject_id(plan)
        author = raw.get("vlastnik_meno")
        if not isinstance(author, str) or author == "*":
            author = None
        props = self._props_for(state.user_props, raw.get("timelineid"))
        try:
            timelineid = int(raw.get("timelineid"))
            if timelineid <= 0:
                raise ValueError("timelineid must be positive")
        except (TypeError, ValueError):
            raise EduPageTransientError(
                f"timeline item without a numeric id: {raw.get('timelineid')!r}"
            )
        removed = str(raw.get("removed") or "0").strip().lower() in ("1", "true")
        timestamp = raw.get("timestamp")
        if not isinstance(timestamp, str) or not self._valid_timestamp(timestamp):
            raise EduPageTransientError(
                f"homework timeline item {timelineid} has malformed timestamp"
            )
        return HomeworkItem(
            timelineid=timelineid,
            typ=raw.get("typ"),
            timestamp=timestamp,
            text=raw.get("text") if isinstance(raw.get("text"), str) else "",
            title=title,
            due_date=due_date,
            author=author,
            subject_id=subject_id,
            subject_short=self._subject_short(state.dbi, subject_id),
            removed=removed,
            done=bool(props.get("doneMaxCas")),
        )

    @staticmethod
    def _parse_item_data(raw: dict) -> Any:
        data = raw.get("data")
        if isinstance(data, str):
            decoded = EduPageClient._decode_embedded(data, "timeline item")
            if isinstance(decoded, (dict, list)):
                return decoded
            try:
                return json.loads(decoded)
            except (json.JSONDecodeError, ValueError, TypeError) as exc:
                raise EduPageTransientError(
                    "malformed timeline item data"
                ) from exc
        return data or {}

    @staticmethod
    def _valid_timestamp(value: str) -> bool:
        try:
            datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
            return True
        except ValueError:
            return False

    @staticmethod
    def _valid_date(value: str) -> bool:
        try:
            date.fromisoformat(value)
            return len(value) == 10
        except ValueError:
            return False

    @staticmethod
    def _subject_id(data: Any) -> str | None:
        if not isinstance(data, dict):
            return None
        for key in SUBJECT_ID_KEYS:
            value = data.get(key)
            if isinstance(value, (int, str)) and str(value).strip():
                return str(value)
        for nested_key in ("parametre", "oldVals"):
            nested = data.get(nested_key)
            if isinstance(nested, dict):
                for key in SUBJECT_ID_KEYS:
                    value = nested.get(key)
                    if isinstance(value, (int, str)) and str(value).strip():
                        return str(value)
        return None

    @staticmethod
    def _props_for(props: dict, timelineid: Any) -> dict:
        if timelineid is None:
            return {}
        for key in {str(timelineid), timelineid}:
            value = props.get(key)
            if isinstance(value, dict):
                return value
        return {}

    @staticmethod
    def _subject_short(dbi: dict, subject_id: str | None) -> str | None:
        if not subject_id:
            return None
        # dbi is {classes, teachers, subjects, classrooms} -> id -> {short, ...}
        subjects = dbi.get("subjects") if isinstance(dbi, dict) else None
        candidates = (
            subjects
            if isinstance(subjects, dict)
            else (dbi if isinstance(dbi, dict) else {})
        )
        for key in {subject_id, str(subject_id)}:
            entry = candidates.get(key)
            if isinstance(entry, dict):
                short = entry.get("short")
                if short:
                    return str(short)
        return None

    @staticmethod
    def _merge_items(items_a: list[dict], items_b: list[dict]) -> list[dict]:
        merged: dict[Any, dict] = {}
        for item in list(items_a) + list(items_b):
            if not isinstance(item, dict):
                continue
            tid = item.get("timelineid")
            try:
                key = int(tid)
                if key <= 0:
                    raise ValueError("timelineid must be positive")
            except (TypeError, ValueError):
                if item.get("typ") in DEFAULT_INCLUDE_TYPES:
                    raise EduPageTransientError(
                        "homework timeline item has malformed timelineid"
                    )
                continue
            # The history response is fetched after the login snapshot and is
            # authoritative for duplicate ids, regardless of payload size.
            merged[key] = item
        return list(merged.values())
