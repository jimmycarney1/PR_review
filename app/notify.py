"""Texting whoever is on the clock, via Twilio (stdlib only).

Dormant until the environment carries Twilio credentials and phone numbers:
with nothing configured every send is recorded as 'skipped' and the draft
carries on untouched. Numbers live in the environment, never in the repo or
the database -- only a masked form is stored, enough to tell two numbers
apart when something looks wrong.
"""
import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .db import now_iso

TWILIO_API = "https://api.twilio.com/2010-04-01/Accounts/{sid}/Messages.json"

# Each player's number comes from its own variable, so one can be changed or
# removed without touching the others.
PHONE_ENV = {"Jimmy": "PHONE_JIMMY", "Mack": "PHONE_MACK", "Anthony": "PHONE_ANTHONY"}


def _env(name: str) -> str | None:
    return (os.environ.get(name) or "").strip() or None


def phone_for(player: str) -> str | None:
    key = PHONE_ENV.get(player)
    return _env(key) if key else None


def credentials() -> tuple[str, str, str] | None:
    sid, token, sender = (
        _env("TWILIO_ACCOUNT_SID"),
        _env("TWILIO_AUTH_TOKEN"),
        _env("TWILIO_FROM_NUMBER"),
    )
    return (sid, token, sender) if sid and token and sender else None


def app_url() -> str:
    """Where the text should point. Railway sets RAILWAY_PUBLIC_DOMAIN itself."""
    explicit = _env("APP_URL")
    if explicit:
        return explicit.rstrip("/")
    domain = _env("RAILWAY_PUBLIC_DOMAIN")
    return f"https://{domain}" if domain else ""


def mask(number: str) -> str:
    """Last four digits only -- enough to identify, not enough to dial."""
    digits = [c for c in number if c.isdigit()]
    return f"***{''.join(digits[-4:])}" if len(digits) >= 4 else "***"


def status(conn) -> dict:
    """What the UI shows about texting, without revealing any number."""
    return {
        "configured": credentials() is not None,
        "players_with_numbers": sorted(p for p in PHONE_ENV if phone_for(p)),
        "last": _last_notification(conn),
    }


def _last_notification(conn) -> dict | None:
    row = conn.execute(
        "SELECT player, kind, status, created_at FROM notifications ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def _record(conn, *, week, player, to, kind, body, state, detail=None) -> None:
    conn.execute(
        """INSERT INTO notifications
             (week, player, to_masked, kind, body, status, detail, created_at)
           VALUES (?,?,?,?,?,?,?,?)""",
        (week, player, mask(to) if to else None, kind, body, state, detail, now_iso()),
    )
    conn.commit()


def _post(sid: str, token: str, sender: str, to: str, body: str) -> str:
    payload = urllib.parse.urlencode({"To": to, "From": sender, "Body": body}).encode()
    request = urllib.request.Request(TWILIO_API.format(sid=sid), data=payload, method="POST")
    auth = base64.b64encode(f"{sid}:{token}".encode()).decode()
    request.add_header("Authorization", f"Basic {auth}")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode()).get("sid", "")


def send(conn, *, player: str, body: str, week: int | None = None, kind: str = "on_clock") -> str:
    """Text one player. Returns the outcome; never raises.

    A texting problem must never cost someone their pick, so every failure is
    swallowed into the notifications table for later reading.
    """
    to = phone_for(player)
    creds = credentials()
    if not to or not creds:
        missing = "no phone number on file" if not to else "Twilio not configured"
        _record(conn, week=week, player=player, to=to, kind=kind, body=body,
                state="skipped", detail=missing)
        return "skipped"

    try:
        message_sid = _post(*creds, to, body)
    except urllib.error.HTTPError as exc:
        detail = f"{exc.code}: {exc.read().decode(errors='replace')[:200]}"
        _record(conn, week=week, player=player, to=to, kind=kind, body=body,
                state="failed", detail=detail)
        return "failed"
    except Exception as exc:  # network, DNS, timeout -- still not the pick's problem
        _record(conn, week=week, player=player, to=to, kind=kind, body=body,
                state="failed", detail=str(exc)[:200])
        return "failed"

    _record(conn, week=week, player=player, to=to, kind=kind, body=body,
            state="sent", detail=message_sid)
    return "sent"


def on_clock_message(*, week: int, slot: int, slots: int, previous: dict | None) -> str:
    """Short enough to read on a lock screen, with the board one tap away."""
    lead = f"Pigskin: you're up — week {week}, pick {slot} of {slots}."
    if previous:
        lead += f" {previous['player']} took {previous['label']}."
    url = app_url()
    return f"{lead} {url}".strip()


def notify_on_clock(conn, *, week: int, slot: int, slots: int, player: str,
                    previous: dict | None) -> str:
    return send(
        conn,
        player=player,
        week=week,
        kind="on_clock",
        body=on_clock_message(week=week, slot=slot, slots=slots, previous=previous),
    )
