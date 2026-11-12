"""Draft content: validation, storage, settings, the shared render path and the size check."""

import dataclasses
import sqlite3
from pathlib import Path

import pytest

from app import compose, db, render
from app.compose import DraftContent
from app.recipients import Recipient
from app.unsubscribe_tokens import make_token, verify_token

BASE = "https://mail.example.com"
SECRET = "x" * 32
GOOD = DraftContent(
    subject="Merry Christmas, {{first_name|Friend}}",
    preheader="Hours and wreaths for {{first_name|you}}",
    body="Dear {{first_name|Friend}},\n\n**Thanks** for a great year.",
    business_name="The Calico Barn",
    postal_address="123 Barn Road\nSpringfield, NC 27000",
)
ADA = {"first_name": "Ada", "email": "ada@example.com"}


@pytest.fixture
def database(tmp_path: Path) -> db.Database:
    d = db.Database(tmp_path / "app.db")
    d.init()
    return d


def _draft(database: db.Database, draft_id: str = "a" * 32) -> str:
    with database.connect() as conn:
        conn.execute(
            "INSERT INTO drafts (id, created_at, original_name) VALUES (?, '2026-11-11', 'l.csv')",
            (draft_id,),
        )
    return draft_id


# --- validation ------------------------------------------------------------------------------


def test_good_content_is_valid() -> None:
    assert compose.validate(GOOD) == {}


def test_every_problem_reported_by_field() -> None:
    bad = DraftContent(
        subject="Hi {{nme}}",
        preheader="{{7*7}}",
        body="[x](javascript:alert(1))",
        business_name="",
        postal_address="",
        reason="r" * 400,
        headline="",
        tagline="Hello {{first_name}}",
    )
    errors = compose.validate(bad)
    assert set(errors) == {
        "subject",
        "preheader",
        "body",
        "business_name",
        "postal_address",
        "reason",
        "headline",
        "tagline",
    }
    assert any("Unknown field {{nme}}" in e for e in errors["subject"])
    assert any("Malformed" in e for e in errors["preheader"])
    assert any("javascript" in e for e in errors["body"])
    assert errors["business_name"] == ["The business or sender name is required."]
    assert errors["postal_address"] == ["The postal address is required (CAN-SPAM)."]
    assert "too long" in errors["reason"][0]
    assert errors["headline"] == ["The headline is required."]
    assert errors["tagline"] == ["Merge fields can't be used in the tagline."]


def test_empty_subject_and_body() -> None:
    errors = compose.validate(dataclasses.replace(GOOD, subject=" ", body="\n"))
    assert errors["subject"] == ["The subject is empty."]
    assert errors["body"] == ["The message is empty."]


def test_limits() -> None:
    errors = compose.validate(
        dataclasses.replace(
            GOOD,
            subject="s" * 251,
            preheader="p" * 251,
            body="b" * 100_001,
            headline="h" * 61,
            tagline="t\x07",
        )
    )
    assert "too long" in errors["subject"][0]
    assert "too long" in errors["preheader"][0]
    assert "too long" in errors["body"][0]
    assert "too long" in errors["headline"][0]
    assert "control character" in errors["tagline"][0]


def test_subject_must_be_one_line() -> None:
    errors = compose.validate(dataclasses.replace(GOOD, subject="a\nb"))
    assert errors["subject"] == ["The subject must be one line."]


def test_from_form() -> None:
    c = DraftContent.from_form(
        {"subject": "s", "body": "a\r\nb\rc", "postal_address": "1\r\n2", "csrf_token": "x"}
    )
    assert c.body == "a\nb\nc"
    assert c.postal_address == "1\n2"
    assert c.is_advertisement is False  # unticked checkbox is absent
    assert c.headline == "" and c.reason == ""
    assert DraftContent.from_form({"is_advertisement": "1"}).is_advertisement is True
    assert DraftContent.from_form({"is_advertisement": ""}).is_advertisement is False


# --- rendering -------------------------------------------------------------------------------


def test_render_message_uses_shared_path() -> None:
    out = compose.render_message(GOOD, ADA, public_base_url=BASE, unsubscribe_secret=SECRET)
    assert out.subject == "Merry Christmas, Ada"
    assert "Dear Ada," in out.html and "<strong>Thanks</strong>" in out.html
    assert "Hours and wreaths for Ada" in out.html
    token = make_token(SECRET, "ada@example.com")
    assert f'href="{BASE}/u/{token}"' in out.html
    assert f"Unsubscribe: {BASE}/u/{token}" in out.text
    assert out.unsubscribe_url == f"{BASE}/u/{token}"
    assert verify_token(SECRET, token) == "ada@example.com"
    assert out.text.startswith("Dear Ada,\n\nThanks for a great year.")
    assert render.DEFAULT_HEADLINE in out.html and render.DEFAULT_TAGLINE in out.html


def test_fallbacks_and_custom_header() -> None:
    c = dataclasses.replace(GOOD, headline="Happy Holidays", tagline=" ")
    out = compose.render_message(
        c,
        {"first_name": "", "email": "f@example.com"},
        public_base_url=BASE,
        unsubscribe_secret=SECRET,
    )
    assert out.subject == "Merry Christmas, Friend"
    assert "Happy Holidays" in out.html
    assert render.DEFAULT_TAGLINE not in out.html
    assert 'class="hero-tagline"' not in out.html


def test_force_dark_adds_rules_outside_media_query() -> None:
    light = compose.render_message(GOOD, ADA, public_base_url=BASE, unsubscribe_secret=SECRET)
    dark = compose.render_message(
        GOOD, ADA, public_base_url=BASE, unsubscribe_secret=SECRET, force_dark=True
    )
    assert '<meta name="color-scheme" content="light dark">' in light.html
    assert '<meta name="color-scheme" content="dark">' in dark.html
    assert 'content="light dark"' not in dark.html
    rule = f".email-bg {{ background: {render.DARK['page']} !important;"
    assert light.html.count(rule) == 1  # only inside @media (prefers-color-scheme: dark)
    assert dark.html.count(rule) == 2
    after_media = dark.html.split("@media (prefers-color-scheme: dark)", 1)[1]
    assert ":root { color-scheme: dark; }" in after_media
    assert dark.text == light.text


# --- size --------------------------------------------------------------------------------------


def test_worst_case_values() -> None:
    people = [
        Recipient(2, "a@example.com", "Bartholomew"),
        Recipient(3, "much.longer.address@example.com", "Al"),
        Recipient(4, "b@example.com", "A&B"),  # escapes to 7 characters
    ]
    assert compose.worst_case_values(people) == {
        "first_name": "Bartholomew",
        "email": "much.longer.address@example.com",
    }
    assert compose.worst_case_values([])["first_name"] == ""


def test_size_check_levels() -> None:
    small = compose.size_check(GOOD, [], public_base_url=BASE, unsubscribe_secret=SECRET)
    assert 5 * 1024 < small.bytes < 40 * 1024
    assert not small.warn and not small.too_large and small.message() is None
    assert compose.SizeCheck(91 * 1024).warn
    assert "close to" in (compose.SizeCheck(91 * 1024).message() or "")
    big = compose.SizeCheck(103 * 1024)
    assert big.too_large and "over the 102 KB limit" in (big.message() or "")


def test_size_counts_longest_name() -> None:
    body = "{{first_name}} " * 2000
    c = dataclasses.replace(GOOD, body=body)
    short = compose.size_check(
        c, [Recipient(2, "a@example.com", "Al")], public_base_url=BASE, unsubscribe_secret=SECRET
    )
    long = compose.size_check(
        c,
        [Recipient(2, "a@example.com", "Al"), Recipient(3, "b@example.com", "B" * 40)],
        public_base_url=BASE,
        unsubscribe_secret=SECRET,
    )
    assert long.bytes - short.bytes >= 2000 * 38


def test_size_counts_long_fallback() -> None:
    """Everyone has a short name, but a recipient without one gets the much longer fallback."""
    body = ("{{first_name|" + "F" * 60 + "}} ") * 1000
    c = dataclasses.replace(GOOD, body=body)
    named = [Recipient(2, "a@example.com", "Al")]
    named_size = compose.size_check(c, named, public_base_url=BASE, unsubscribe_secret=SECRET)
    al = compose.render_message(
        c,
        {"first_name": "Al", "email": "a@example.com"},
        public_base_url=BASE,
        unsubscribe_secret=SECRET,
    )
    nameless = compose.render_message(
        c,
        {"first_name": "", "email": "a@example.com"},
        public_base_url=BASE,
        unsubscribe_secret=SECRET,
    )
    assert len(nameless.html.encode()) > len(al.html.encode()) + 1000 * 50
    assert named_size.bytes >= len(nameless.html.encode())


def test_size_counts_longest_token_address() -> None:
    """The address longest in a merged link isn't always the longest in the unsubscribe token."""
    plus = Recipient(2, "a+b+c+d+e+f+g+h@example.com", "Al")  # each + is %2B in a link
    long = Recipient(3, "abcdefghijklmnopqrstuvwxyz0@example.com", "Al")
    assert compose.worst_case_values([plus, long])["email"] == plus.email
    assert compose._token_length(long.email) > compose._token_length(plus.email)
    check = compose.size_check(GOOD, [plus, long], public_base_url=BASE, unsubscribe_secret=SECRET)
    for r in (plus, long):
        out = compose.render_message(
            GOOD,
            {"first_name": "Al", "email": r.email},
            public_base_url=BASE,
            unsubscribe_secret=SECRET,
        )
        assert check.bytes >= len(out.html.encode())


# --- storage -----------------------------------------------------------------------------------


def test_defaults_without_settings(database: db.Database) -> None:
    c = compose.defaults(database)
    assert c == DraftContent()
    assert c.is_advertisement is True
    assert c.headline == render.DEFAULT_HEADLINE


def test_save_and_load(database: db.Database) -> None:
    draft = _draft(database)
    assert compose.get_content(database, draft) is None
    c = dataclasses.replace(GOOD, is_advertisement=False, reason="Because.", tagline="")
    compose.save_content(database, draft, c)
    assert compose.get_content(database, draft) == c
    settings = compose.get_settings(database)
    assert settings == {
        "business_name": "The Calico Barn",
        "postal_address": "123 Barn Road\nSpringfield, NC 27000",
        "is_advertisement": "0",
        "reason": "Because.",
        "headline": render.DEFAULT_HEADLINE,
        "tagline": "",
    }
    fresh = compose.defaults(database)
    assert fresh.subject == "" and fresh.body == ""
    assert fresh.business_name == "The Calico Barn"
    assert fresh.is_advertisement is False and fresh.tagline == ""

    # Saving again updates in place.
    compose.save_content(database, draft, dataclasses.replace(c, subject="Second"))
    assert compose.get_content(database, draft).subject == "Second"  # type: ignore[union-attr]
    with database.connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM draft_content").fetchone()[0] == 1


def test_content_needs_a_draft(database: db.Database) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        compose.save_content(database, "b" * 32, GOOD)


def test_migrations_upgrade_v1_database(tmp_path: Path) -> None:
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN;\n{db.MIGRATIONS[0]}\nPRAGMA user_version = 1;\nCOMMIT;")
    conn.execute(
        "INSERT INTO drafts (id, created_at, original_name) VALUES (?, '2026-11-01', 'old.csv')",
        ("c" * 32,),
    )
    conn.commit()
    conn.close()

    database = db.Database(path)
    database.init()
    with database.connect() as conn:
        assert conn.execute("PRAGMA user_version").fetchone()[0] == len(db.MIGRATIONS) == 4
        assert conn.execute("SELECT original_name FROM drafts").fetchone()[0] == "old.csv"
    compose.save_content(database, "c" * 32, GOOD)
    assert compose.get_content(database, "c" * 32) == GOOD
    database.init()  # idempotent


def test_migration_4_keeps_send_log_ids(tmp_path: Path) -> None:
    """Rebuilding send_log keeps its AUTOINCREMENT counter: ids of deleted rows aren't reused."""
    path = tmp_path / "app.db"
    conn = sqlite3.connect(path)
    for number, sql in enumerate(db.MIGRATIONS[:3], start=1):
        conn.executescript(f"BEGIN;\n{sql}\nPRAGMA user_version = {number};\nCOMMIT;")
    for row_no in (1, 2, 3):
        conn.execute(
            "INSERT INTO send_log (campaign_id, row_no, started_at, outcome) "
            "VALUES (1, ?, '2026-12-01T00:00:00+00:00', 'sent')",
            (row_no,),
        )
    conn.execute("DELETE FROM send_log WHERE id = 3")  # e.g. an unmarked send at shutdown
    conn.commit()
    conn.close()

    database = db.Database(path)
    database.init()
    with database.connect() as conn:
        assert [r[0] for r in conn.execute("SELECT id FROM send_log ORDER BY id")] == [1, 2]
        new_id = conn.execute(
            "INSERT INTO send_log (started_at) VALUES ('2026-12-02T00:00:00+00:00')"
        ).lastrowid
        assert new_id == 4
        names = [r[0] for r in conn.execute("SELECT name FROM sqlite_sequence")]
        assert "send_log_new" not in names and names.count("send_log") == 1
