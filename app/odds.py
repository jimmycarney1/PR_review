"""The Odds API client (stdlib only) plus credit bookkeeping.

Free tier is 500 credits/month. A spreads pull for one region/market costs 1
credit; the scores endpoint costs 1 (2 when asking for past days). Every call
records the x-requests-remaining header so the UI can show the balance.
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request

from .db import now_iso

BASE = "https://api.the-odds-api.com/v4/sports/americanfootball_nfl"
SPREAD_BOOK_PREFERENCE = ["draftkings", "fanduel", "betmgm", "caesars"]


class OddsApiError(Exception):
    """A failed call. `out_of_credits` tells the UI to offer manual entry."""

    def __init__(self, message: str, status: int | None = None, out_of_credits: bool = False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.out_of_credits = out_of_credits


def api_key() -> str | None:
    key = os.environ.get("ODDS_API_KEY", "").strip()
    return key or None


def _get(conn, path: str, params: dict) -> tuple[list, dict]:
    key = api_key()
    if not key:
        raise OddsApiError(
            "No ODDS_API_KEY set. Enter the line by hand and it'll be flagged as an override.",
            out_of_credits=True,
        )
    url = f"{BASE}{path}?" + urllib.parse.urlencode({**params, "apiKey": key})
    try:
        with urllib.request.urlopen(url, timeout=20) as resp:
            body = json.loads(resp.read().decode())
            headers = {
                "remaining": _as_int(resp.headers.get("x-requests-remaining")),
                "used": _as_int(resp.headers.get("x-requests-used")),
            }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:300]
        # 401 covers a bad key; 429 is the monthly quota.
        out = exc.code in (401, 429)
        _record(conn, path, None, None, ok=False, error=f"{exc.code}: {detail}")
        raise OddsApiError(
            f"Odds API returned {exc.code}. {detail}", status=exc.code, out_of_credits=out
        ) from exc
    except urllib.error.URLError as exc:
        _record(conn, path, None, None, ok=False, error=str(exc.reason))
        raise OddsApiError(f"Couldn't reach the Odds API: {exc.reason}") from exc

    _record(conn, path, headers["remaining"], headers["used"], ok=True)
    return body, headers


def _as_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _record(conn, endpoint, remaining, used, ok=True, error=None) -> None:
    conn.execute(
        "INSERT INTO api_calls (endpoint, called_at, remaining, used, ok, error)"
        " VALUES (?,?,?,?,?,?)",
        (endpoint, now_iso(), remaining, used, 1 if ok else 0, error),
    )
    conn.commit()


def fetch_spreads(conn) -> tuple[list[dict], dict]:
    """Upcoming NFL games with a spread from the most preferred book available."""
    events, headers = _get(
        conn,
        "/odds/",
        {"regions": "us", "markets": "spreads", "oddsFormat": "american"},
    )
    games = []
    for event in events:
        book = _pick_book(event.get("bookmakers", []))
        if not book:
            continue
        outcomes = next(
            (m["outcomes"] for m in book.get("markets", []) if m.get("key") == "spreads"), None
        )
        if not outcomes or len(outcomes) < 2:
            continue
        games.append(
            {
                "event_id": event["id"],
                "commence_time": event["commence_time"],
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "book": book.get("title") or book.get("key"),
                "outcomes": [
                    {
                        "team": o["name"],
                        "spread": float(o["point"]),
                        "price": _as_int(o.get("price")),
                    }
                    for o in outcomes
                    if o.get("point") is not None
                ],
            }
        )
    return games, headers


def _pick_book(bookmakers: list[dict]) -> dict | None:
    """Prefer a consistent book week to week so lines don't jump between sources."""
    by_key = {b.get("key"): b for b in bookmakers}
    for key in SPREAD_BOOK_PREFERENCE:
        if key in by_key:
            return by_key[key]
    return bookmakers[0] if bookmakers else None


def fetch_scores(conn, days_from: int = 3) -> tuple[list[dict], dict]:
    """Finals for recently completed games."""
    events, headers = _get(conn, "/scores/", {"daysFrom": str(days_from)})
    out = []
    for event in events:
        scores = {s["name"]: _as_int(s.get("score")) for s in (event.get("scores") or [])}
        out.append(
            {
                "event_id": event["id"],
                "home_team": event["home_team"],
                "away_team": event["away_team"],
                "completed": bool(event.get("completed")),
                "home_score": scores.get(event["home_team"]),
                "away_score": scores.get(event["away_team"]),
            }
        )
    return out, headers


def credit_status(conn) -> dict:
    """Latest known credit balance, from the most recent successful call."""
    row = conn.execute(
        "SELECT remaining, used, called_at FROM api_calls"
        " WHERE ok = 1 AND remaining IS NOT NULL ORDER BY id DESC LIMIT 1"
    ).fetchone()
    return {
        "remaining": row["remaining"] if row else None,
        "used": row["used"] if row else None,
        "as_of": row["called_at"] if row else None,
        "key_configured": api_key() is not None,
    }
