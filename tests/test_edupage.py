"""Tests for the EduPage client (no network: mocks only)."""

from __future__ import annotations

import json

import pytest

from edupagetasks.config import EduPageAuthConfig, EduPageConfig
from edupagetasks.edupage import (
    DEFAULT_INCLUDE_TYPES,
    EduPageAuthError,
    EduPageClient,
    EduPageReloadRequired,
    LoginState,
)
from edupagetasks.encoding import chromium_b64encode


def eqz(payload: dict) -> str:
    return "eqz:" + chromium_b64encode(json.dumps(payload).encode("utf-8"))


class FakeResp:
    def __init__(
        self,
        text: str = "",
        url: str = "http://x",
        status_code: int = 200,
        json_data: dict | None = None,
    ):
        self.text = text
        self.url = url
        self.status_code = status_code
        self._json = json_data

    def json(self) -> dict:
        if self._json is None:
            raise ValueError("no json")
        return self._json


USERHOME_HTML = (
    '<html><head><script>ASC.gsechash="gsecABC";</script></head><body>'
    '<script>userhome({"userid":"Student1","dp":{"year":2025},'
    '"dbi":{"mat":{"short":"MAT","name":"Matematika"}},'
    '"items":[],"userProps":{}});</script></body></html>'
)


def make_client(**config_kwargs) -> EduPageClient:
    config_kwargs.setdefault(
        "auth",
        EduPageAuthConfig(mode="password", username="u", password="p", session_id=None),
    )
    cfg = EduPageConfig(subdomain="gymtest", **config_kwargs)
    return EduPageClient(cfg)


def route_userhome(fake, html: str = USERHOME_HTML) -> None:
    def handler(method, url, **kwargs):
        if method == "GET" and url.rstrip("/").endswith("/user"):
            return FakeResp(text=html, url=url)
        raise AssertionError(f"unexpected {method} {url}")

    fake.side_effect = handler


def test_homework_items_extraction_and_exact_typ_filter():
    client = make_client()
    items = [
        {
            "timelineid": 111,
            "typ": "homework",
            "timestamp": "2025-09-20 08:00:00",
            "text": "text body one",
            "data": json.dumps(
                {
                    "oldVals": {"title": "Domaca uloha", "date": "2025-09-25"},
                    "subjectid": "mat",
                    "nazov": "fallback",
                }
            ),
            "vlastnik_meno": "Pan Ucitel",
            "removed": 0,
        },
        {
            "timelineid": 222,
            "typ": "h_homework",
            "timestamp": "2025-09-20 08:01:00",
            "text": "helper, unrelated",
            "data": "{}",
        },
        {
            "timelineid": 333,
            "typ": "homeworkstudentstav",
            "timestamp": "2025-09-20 08:02:00",
            "text": "state change, noise",
            "data": "{}",
        },
        {
            "timelineid": 444,
            "typ": "homework",
            "timestamp": "2025-09-21 09:00:00",
            "text": "text body three",
            "data": json.dumps(
                {"oldVals": {"date": "2025-09-26"}, "nazov": "Only nazov title"}
            ),
            "vlastnik_meno": "*",
            "removed": 1,
        },
    ]
    state = LoginState(
        userid="Student1",
        dbi={"mat": {"short": "MAT", "name": "Matematika"}},
        items=items,
        user_props={"111": {"doneMaxCas": "2025-09-22 18:00:00", "starred": "1"}},
    )
    result = client.homework_items(state)
    assert [i.timelineid for i in result] == [111, 444]
    a, b = result
    assert a.title == "Domaca uloha"
    assert a.due_date == "2025-09-25"
    assert a.author == "Pan Ucitel"
    assert a.subject_id == "mat"
    assert a.subject_short == "MAT"
    assert a.done is True
    assert a.removed is False
    assert b.title == "Only nazov title"
    assert b.due_date == "2025-09-26"
    assert b.author is None
    assert b.subject_id is None
    assert b.subject_short is None
    assert b.done is False
    assert b.removed is True


def test_homework_items_default_include_types():
    client = make_client()
    items = [
        {"timelineid": 1, "typ": "testpridelenie", "data": "{}", "text": "t"},
        {"timelineid": 2, "typ": "etesthw", "data": "{}", "text": "t"},
        {"timelineid": 3, "typ": "sprava", "data": "{}", "text": "t"},
    ]
    state = LoginState(items=items, school_year=2025)
    result = client.homework_items(state)
    assert [i.timelineid for i in result] == [1, 2]
    assert DEFAULT_INCLUDE_TYPES == ["homework", "testpridelenie", "etesthw"]


def test_wire_removed_flag_is_string_and_inverts():
    client = make_client()
    state = LoginState(
        userid="Student1",
        dbi={},
        items=[
            {"timelineid": 1, "typ": "homework", "removed": "0", "data": "{}"},
            {"timelineid": 2, "typ": "homework", "removed": "1", "data": "{}"},
            {"timelineid": 3, "typ": "homework", "removed": 1, "data": "{}"},
            {"timelineid": 4, "typ": "homework", "data": "{}"},
        ],
    )
    result = client.homework_items(state)
    assert [(i.timelineid, i.removed) for i in result] == [
        (1, False),
        (2, True),
        (3, True),
        (4, False),
    ]


def test_nested_dbi_subjects_and_eqz_data_double_parse():
    client = make_client()
    dbi = {"subjects": {"mat": {"short": "MAT", "name": "Matematika"}}}
    encoded = eqz(
        {"oldVals": {"title": "From eqz", "date": "2025-09-30"}, "subjectid": "mat"}
    )
    state = LoginState(
        userid="Student1",
        dbi=dbi,
        items=[
            {"timelineid": 9, "typ": "homework", "data": encoded},
        ],
    )
    result = client.homework_items(state)
    assert len(result) == 1
    assert result[0].title == "From eqz"
    assert result[0].due_date == "2025-09-30"
    assert result[0].subject_short == "MAT"


def test_subject_probed_from_oldvals_and_string_ok():
    client = make_client()
    state = LoginState(
        userid="Student1",
        dbi={"subjects": {"99": {"short": "AJ"}}},
        items=[
            {
                "timelineid": 6,
                "typ": "homework",
                "data": json.dumps(
                    {"oldVals": {"title": "X", "date": "2025-09-25", "subjectid": "99"}}
                ),
            }
        ],
    )
    result = client.homework_items(state)
    assert result[0].subject_id == "99"
    assert result[0].subject_short == "AJ"


def test_userhome_payload_with_json_json_semicolon():
    page = (
        '<script>userhome({"userid":"S",'
        '"items":[{"timelineid":1,"typ":"homework","data":"{\\"a\\":\\");\\"}"}],'
        '"userProps":{},"dbi":{},"dp":{"year":2025}});</script>'
    )
    client = make_client()
    parsed = client._parse_userhome(page)
    assert parsed["userid"] == "S"
    assert parsed["items"][0]["timelineid"] == 1


def test_merge_history_dedupes_by_timelineid():
    client = make_client()
    login_items = [
        {
            "timelineid": 1,
            "typ": "homework",
            "timestamp": "2025-09-20 08:00:00",
            "text": "full",
            "data": json.dumps(
                {"oldVals": {"title": "FromLogin", "date": "2025-09-25"}}
            ),
            "vlastnik_meno": "A",
        }
    ]
    history_items = [
        {
            "timelineid": 1,
            "typ": "homework",
            "timestamp": "2025-09-20 08:00:00",
            "text": "fuller",
            "data": json.dumps(
                {"oldVals": {"title": "FromHistory", "date": "2025-09-25"}}
            ),
            "vlastnik_meno": "A",
            "extra": "richer",
        },
        {
            "timelineid": 2,
            "typ": "homework",
            "timestamp": "2025-09-21 09:00:00",
            "text": "old",
            "data": json.dumps({"oldVals": {"title": "Old", "date": "2025-09-26"}}),
            "vlastnik_meno": "B",
        },
    ]
    state = LoginState(
        userid="Student1",
        dbi={},
        items=login_items,
        user_props={"1": {"doneMaxCas": "D1"}},
    )
    # history pulls carry their own (fresh) props; login userProps is only a
    # fallback when the history pull has NO props payload at all (docs).
    result = client.merge_history(state, history_items, {"2": {"doneMaxCas": "D2"}})
    assert [i.timelineid for i in result] == [1, 2]
    one, two = result
    assert one.title == "FromHistory"
    assert one.done is False
    assert two.title == "Old"
    assert two.done is True


def test_merge_history_falls_back_to_login_props_when_history_has_none():
    client = make_client()
    login_items = [
        {
            "timelineid": 1,
            "typ": "homework",
            "timestamp": "2025-09-20 08:00:00",
            "text": "full",
            "data": json.dumps({"oldVals": {"title": "T", "date": "2025-09-25"}}),
        }
    ]
    state = LoginState(
        userid="Student1",
        dbi={},
        items=login_items,
        user_props={"1": {"doneMaxCas": "D1"}},
    )
    result = client.merge_history(state, [], {})
    assert [i.timelineid for i in result] == [1]
    assert result[0].done is True


def test_eqap_login_request_body_shape(mocker):
    client = make_client(
        auth=EduPageAuthConfig(
            mode="password", username="user1", password="pw", session_id=None
        )
    )
    calls = []

    def handler(method, url, **kwargs):
        calls.append((method, url, kwargs.get("data"), kwargs.get("json")))
        if method == "GET" and "/login/" in url:
            return FakeResp(text="login page", url=url)
        if method == "POST" and url.endswith("/login/?cmd=MainLogin&akcia=getToken"):
            return FakeResp(text=eqz({"token": "tok123"}), url=url)
        if method == "POST" and url.endswith("/login/?cmd=MainLogin&akcia=login"):
            return FakeResp(
                text=eqz({"redirectUrl": "https://gymtest.edupage.org/user"}), url=url
            )
        if method == "GET" and url.rstrip("/").endswith("/user"):
            return FakeResp(text=USERHOME_HTML, url=url)
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    state = client.connect()
    assert state.userid == "Student1"
    assert state.school_year == 2025
    assert state.gsec_hash == "gsecABC"
    token_call = next(c for c in calls if c[1].endswith("akcia=getToken"))
    body = token_call[2]
    assert set(body) == {"eqap", "eqacs", "eqaz"}
    assert body["eqap"].startswith("dz:")
    assert body["eqaz"] == "1"


def test_switchchild_happy_path(mocker):
    client = make_client(
        auth=EduPageAuthConfig(
            mode="session", username="u", password=None, session_id="sess123"
        ),
        child_person_id="456",
    )
    switch_calls = []

    def handler(method, url, **kwargs):
        if method == "GET" and url.rstrip("/").endswith("/user"):
            return FakeResp(text=USERHOME_HTML, url=url)
        if method == "GET" and "switchchild" in url:
            switch_calls.append(url)
            return FakeResp(text="OK", url=url)
        raise AssertionError(f"unexpected {method} {url}")

    mocker.patch.object(client.session, "request", side_effect=handler)
    state = client.connect()
    assert state.userid == "Student1"
    assert switch_calls == [
        "https://gymtest.edupage.org/login/switchchild?studentid=456"
    ]


def test_jsx_call_reload_raises(mocker):
    client = make_client()
    state = LoginState(userid="Student1", gsec_hash="gsecABC")
    posted: list[dict] = []

    def handler(method, url, **kwargs):
        posted.append(kwargs.get("json"))
        return FakeResp(text="", json_data={"reload": True})

    mocker.patch.object(client.session, "request", side_effect=handler)
    with pytest.raises(EduPageReloadRequired):
        client.jsx_call(
            "/substitution/server/viewer.js",
            "getSubstViewerDayDataHtml",
            {"date": "2025-09-01"},
            state,
        )
    assert posted[0]["__gsh"] == "gsecABC"
    assert posted[0]["__args"] == [None, {"date": "2025-09-01"}]


def test_jsx_call_returns_r(mocker):
    client = make_client()
    state = LoginState(userid="Student1", gsec_hash="gsecABC")
    mocker.patch.object(
        client.session,
        "request",
        return_value=FakeResp(text="", json_data={"reload": None, "r": {"ok": 1}}),
    )
    assert client.jsx_call(
        "/timetable/server/currenttt.js", "curentttGetData", {}, state
    ) == {"ok": 1}


def test_session_mode_rejects_missing_session_id():
    client = make_client(
        auth=EduPageAuthConfig(
            mode="session", username="u", password=None, session_id=""
        )
    )
    with pytest.raises(EduPageAuthError):
        client.connect()
