"""Texting the player on the clock.

The rule under test throughout: a texting problem never costs anyone a pick.
"""
import pytest

from app import db as db_module
from app import notify


@pytest.fixture
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "n.db"))
    db_module.init_db()
    connection = db_module.connect()
    yield connection
    connection.close()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setenv("PHONE_MACK", "+15551234567")
    monkeypatch.setenv("APP_URL", "https://example.test")


def sent_rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY id")]


def test_nothing_is_sent_until_twilio_is_configured(conn, monkeypatch):
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER", "PHONE_MACK"):
        monkeypatch.delenv(key, raising=False)
    assert notify.send(conn, player="Mack", body="hi", week=2) == "skipped"
    row = sent_rows(conn)[0]
    assert row["status"] == "skipped"
    assert row["detail"] == "no phone number on file"


def test_a_player_without_a_number_is_skipped(conn, configured, monkeypatch):
    monkeypatch.delenv("PHONE_JIMMY", raising=False)
    assert notify.send(conn, player="Jimmy", body="hi", week=2) == "skipped"
    assert sent_rows(conn)[0]["detail"] == "no phone number on file"


def test_a_configured_player_is_texted(conn, configured, monkeypatch):
    seen = {}

    def fake_post(sid, token, sender, to, body):
        seen.update(sid=sid, token=token, sender=sender, to=to, body=body)
        return "SM_fake"

    monkeypatch.setattr(notify, "_post", fake_post)
    assert notify.send(conn, player="Mack", body="you're up", week=2) == "sent"
    assert seen["to"] == "+15551234567"
    assert seen["sender"] == "+15550000000"
    assert seen["body"] == "you're up"

    row = sent_rows(conn)[0]
    assert row["status"] == "sent"
    assert row["detail"] == "SM_fake"


def test_the_full_number_is_never_stored(conn, configured, monkeypatch):
    monkeypatch.setattr(notify, "_post", lambda *a: "SM_fake")
    notify.send(conn, player="Mack", body="hi", week=2)
    row = sent_rows(conn)[0]
    assert row["to_masked"] == "***4567"
    assert "5551234567" not in str(dict(row))


def test_a_twilio_failure_is_recorded_not_raised(conn, configured, monkeypatch):
    def boom(*args):
        raise RuntimeError("twilio is down")

    monkeypatch.setattr(notify, "_post", boom)
    assert notify.send(conn, player="Mack", body="hi", week=2) == "failed"
    row = sent_rows(conn)[0]
    assert row["status"] == "failed"
    assert "twilio is down" in row["detail"]


def test_the_message_says_who_is_up_and_what_just_happened():
    body = notify.on_clock_message(
        week=2, slot=4, slots=6,
        previous={"player": "Mack", "label": "New England Patriots -4.5"},
    )
    assert "week 2" in body
    assert "pick 4 of 6" in body
    assert "Mack took New England Patriots -4.5" in body


def test_the_first_pick_of_a_week_has_no_previous_pick(monkeypatch):
    monkeypatch.setenv("APP_URL", "https://example.test")
    body = notify.on_clock_message(week=3, slot=1, slots=6, previous=None)
    assert "week 3" in body
    assert "took" not in body
    assert body.endswith("https://example.test")


def test_the_link_falls_back_to_the_railway_domain(monkeypatch):
    monkeypatch.delenv("APP_URL", raising=False)
    monkeypatch.setenv("RAILWAY_PUBLIC_DOMAIN", "pigskin.example.app")
    assert notify.app_url() == "https://pigskin.example.app"


def test_status_reports_readiness_without_leaking_numbers(conn, configured):
    state = notify.status(conn)
    assert state["configured"] is True
    assert state["players_with_numbers"] == ["Mack"]
    assert "5551234567" not in str(state)
