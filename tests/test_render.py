import re

from edupagetasks.models import HomeworkItem
from edupagetasks.render import (
    build_anchor_label,
    build_description,
    build_sync_marker,
    build_title,
    extract_marker_timelineid,
    extract_sync_key,
    subject_label,
)


def make_item(**overrides) -> HomeworkItem:
    defaults = {
        "timelineid": 123,
        "typ": "homework",
        "timestamp": "2026-09-10 14:30:00",
        "text": "<p>Solve page 12</p>",
        "title": "Fr. equations",
        "due_date": "2026-09-14",
        "author": "J. Smith",
        "subject_id": None,
        "subject_short": "MAT",
        "removed": False,
        "done": False,
    }
    defaults.update(overrides)
    return HomeworkItem(**defaults)


def test_build_title_with_subject():
    assert build_title(make_item()) == "Fr. equations"


def test_build_title_without_subject():
    item = make_item(subject_short=None)
    assert build_title(item) == "Fr. equations"


def test_build_title_long_multiline():
    item = make_item(title="Line one\nLine two\nLine three")
    assert build_title(item) == "Line one"


def test_build_title_fallback_to_plain_text():
    item = make_item(title=None)
    assert build_title(item) == "<p>Solve page 12</p>"


def test_build_title_empty():
    item = make_item(title=None, text="")
    assert build_title(item) == "(no title)"


def test_build_description_contains_body_link_and_marker():
    desc = build_description(
        make_item(), subdomain="gymname", userid="Student42"
    )
    assert "**Assignment:**" not in desc
    assert "**Subject:**" not in desc
    assert "**Teacher:**" not in desc
    assert "**Assigned:**" not in desc
    assert "**Due:**" not in desc
    assert "Solve page 12" in desc
    assert "https://gymname.edupage.org/timeline/?timelineid=123#item-123" in desc
    assert desc.endswith("<!-- edupage-key:Student42:123 -->")


def test_build_description_no_teacher_line():
    desc = build_description(
        make_item(),
        subdomain="gymname",
        userid="Student42",
    )
    assert "**Teacher:**" not in desc
    assert "**Subject:**" not in desc


def test_build_description_unknown_subject_and_author():
    desc = build_description(
        make_item(subject_short=None, author=None),
        subdomain="gymname",
        userid="Student42",
    )
    assert "**Subject:**" not in desc
    assert "**Teacher:**" not in desc


def test_build_description_missing_due_date():
    desc = build_description(
        make_item(due_date=None), subdomain="gymname", userid="Student42"
    )
    assert "**Due:**" not in desc


def test_build_description_missing_timestamp():
    item = make_item(timestamp=None)
    desc = build_description(item, subdomain="gymname", userid="Student42")
    assert "None" not in desc
    assert "**Assigned:**" not in desc


def test_extract_marker_timelineid_roundtrip():
    desc = build_description(make_item(), subdomain="gymname", userid="Student42")
    assert extract_marker_timelineid(desc) == 123
    assert extract_sync_key(desc) == ("Student42", 123)
    assert extract_marker_timelineid("no marker here") is None
    assert re.search(r"<!--\s*edupage-key:Student42:(\d+)\s*-->", desc)


def test_extract_marker_timelineid_uses_last_occurrence():
    desc = (
        "Body text trickily includes <!-- edupage-timelineid:99 --> early\n\n"
        + build_description(make_item(), subdomain="gymname", userid="Student42")
    )
    assert extract_marker_timelineid(desc) == 123
    assert (
        extract_marker_timelineid(
            "<!-- edupage-timelineid:1 --> then <!-- edupage-timelineid:2 -->"
        )
        == 2
    )


def test_anchor_and_subject_labels():
    assert build_anchor_label("Student42", 123) == "edu:Student42:123"
    assert build_sync_marker("Student42", 123) == "<!-- edupage-key:Student42:123 -->"
    assert subject_label("MAT") == "MAT"
