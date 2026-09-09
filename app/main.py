"""Thin JSON API over SQLite, plus the static UI."""
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import odds, rules
from .db import DB_PATH, connect, init_db, now_iso

STATIC_DIR = Path(__file__).parent / "static"
SEASON_ANCHOR = os.environ.get("SEASON_WEEK1_ANCHOR", "2026-09-08")

@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="Pigskin Pick'em", version="1.0", lifespan=lifespan)


def db():
    conn = connect()
    try:
        yield conn
    finally:
        conn.close()


# ---------------------------------------------------------------- request bodies


class PickIn(BaseModel):
    week: int
    player: str
    game_id: int
    team: str
    # Only supplied when the API is out of credits and the line is hand-entered.
    override_spread: float | None = None
    override_reason: str | None = None


class PickEdit(BaseModel):
    team: str | None = None
    spread: float | None = None
    actor: str = Field(..., description="Who is making the edit -- recorded in the ledger.")
    reason: str | None = None


class PickDelete(BaseModel):
    actor: str
    reason: str | None = None


class ManualGame(BaseModel):
    week: int
    home_team: str
    away_team: str
    commence_time: str | None = None


class ManualScore(BaseModel):
    game_id: int
    home_score: int
    away_score: int


# ---------------------------------------------------------------- helpers


def _game_row(conn, game_id: int) -> dict:
    row = conn.execute("SELECT * FROM games WHERE id = ?", (game_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"No game {game_id}.")
    return dict(row)


def _picks_for_week(conn, week: int) -> list[dict]:
    rows = conn.execute(
        """SELECT p.*, g.home_team, g.away_team, g.home_score, g.away_score,
                  g.completed, g.commence_time
             FROM picks p JOIN games g ON g.id = p.game_id
            WHERE p.week = ? ORDER BY p.slot""",
        (week,),
    ).fetchall()
    picks = []
    for row in rows:
        pick = dict(row)
        pick["result"] = rules.grade_pick(pick["team"], pick["spread"], pick)
        pick["label"] = f"{pick['team']} {rules.format_spread(pick['spread'])}"
        picks.append(pick)
    return picks


def _latest_snapshot(conn, week: int) -> dict | None:
    row = conn.execute(
        "SELECT * FROM snapshots WHERE week = ? AND source = 'api' ORDER BY id DESC LIMIT 1",
        (week,),
    ).fetchone()
    if not row:
        return None
    snap = dict(row)
    age = rules.line_age(snap["fetched_at"])
    snap["age_seconds"] = int(age.total_seconds())
    snap["fresh"] = age <= rules.LINE_MAX_AGE
    return snap


def _log(conn, pick_id, week, player, action, actor, *, field=None, old=None, new=None, reason=None):
    conn.execute(
        """INSERT INTO pick_ledger
             (pick_id, week, player, action, field, old_value, new_value, actor, reason, created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (
            pick_id, week, player, action, field,
            None if old is None else str(old),
            None if new is None else str(new),
            actor, reason, now_iso(),
        ),
    )


def _pick_error(exc: rules.PickError) -> HTTPException:
    return HTTPException(409, {"code": exc.code, "message": exc.message})


@app.get("/api/health")
def health(conn=Depends(db)):
    """Readiness probe -- confirms the process is up and the database answers."""
    conn.execute("SELECT 1").fetchone()
    return {"status": "ok", "database": DB_PATH}


# ---------------------------------------------------------------- board


@app.get("/api/board")
def board(week: int = 1, conn=Depends(db)):
    """Everything the draft screen renders: order, picks, open board, freshness."""
    if not 1 <= week <= rules.TOTAL_WEEKS:
        raise HTTPException(400, f"Week must be 1..{rules.TOTAL_WEEKS}.")

    picks = _picks_for_week(conn, week)
    on_clock = rules.player_on_clock(week, {p["slot"] for p in picks})
    snapshot = _latest_snapshot(conn, week)

    games = [dict(r) for r in conn.execute(
        "SELECT * FROM games WHERE week = ? ORDER BY commence_time, id", (week,)
    ).fetchall()]

    # Lines come from the newest snapshot; hand-entered games have none.
    spreads: dict[tuple[int, str], dict] = {}
    if snapshot:
        for row in conn.execute(
            "SELECT * FROM lines WHERE snapshot_id = ?", (snapshot["id"],)
        ).fetchall():
            spreads[(row["game_id"], row["team"])] = {"spread": row["spread"], "price": row["price"]}

    taken = {p["team"]: p["player"] for p in picks}
    current_player = on_clock[1] if on_clock else None
    own_games = {p["game_id"] for p in picks if current_player and p["player"] == current_player}

    for game in games:
        sides = []
        for team in (game["away_team"], game["home_team"]):
            line = spreads.get((game["id"], team))
            reason = None
            if team in taken:
                reason = f"Taken by {taken[team]}"
            elif game["id"] in own_games:
                reason = "You already have the other side"
            elif line is None:
                reason = "No line -- needs a refresh or a manual override"
            sides.append(
                {
                    "team": team,
                    "spread": line["spread"] if line else None,
                    "display": rules.format_spread(line["spread"]) if line else None,
                    "price": line["price"] if line else None,
                    "available": reason is None,
                    "reason": reason,
                }
            )
        game["sides"] = sides
        game["result_known"] = bool(game["completed"])
        game["removable"] = not any(p["game_id"] == game["id"] for p in picks)

    return {
        "week": week,
        "total_weeks": rules.TOTAL_WEEKS,
        "players": rules.PLAYERS,
        "draft_order": rules.draft_order(week),
        "slots_per_week": rules.SLOTS_PER_WEEK,
        "on_clock": {"slot": on_clock[0], "player": on_clock[1]} if on_clock else None,
        "picks": picks,
        "games": games,
        "snapshot": snapshot,
        "line_max_age_minutes": int(rules.LINE_MAX_AGE.total_seconds() // 60),
        "credits": odds.credit_status(conn),
    }


# ---------------------------------------------------------------- lines


@app.post("/api/lines/refresh")
def refresh_lines(week: int = 1, conn=Depends(db)):
    """Spend one API credit to pull a fresh slate of spreads."""
    try:
        games, headers = odds.fetch_spreads(conn)
    except odds.OddsApiError as exc:
        raise HTTPException(
            502,
            {
                "code": "ODDS_UNAVAILABLE",
                "message": exc.message,
                "out_of_credits": exc.out_of_credits,
            },
        ) from exc

    fetched_at = now_iso()
    book = games[0]["book"] if games else None
    cur = conn.execute(
        "INSERT INTO snapshots (week, fetched_at, source, book) VALUES (?,?,'api',?)",
        (week, fetched_at, book),
    )
    snapshot_id = cur.lastrowid

    stored = 0
    for game in games:
        game_week = rules.week_for_kickoff(game["commence_time"], SEASON_ANCHOR)
        conn.execute(
            """INSERT INTO games (event_id, week, commence_time, home_team, away_team)
               VALUES (?,?,?,?,?)
               ON CONFLICT(event_id) DO UPDATE SET
                 week = excluded.week,
                 commence_time = excluded.commence_time""",
            (game["event_id"], game_week, game["commence_time"], game["home_team"], game["away_team"]),
        )
        if game_week != week:
            continue
        game_id = conn.execute(
            "SELECT id FROM games WHERE event_id = ?", (game["event_id"],)
        ).fetchone()["id"]
        for outcome in game["outcomes"]:
            conn.execute(
                "INSERT OR REPLACE INTO lines (snapshot_id, game_id, team, spread, price)"
                " VALUES (?,?,?,?,?)",
                (snapshot_id, game_id, outcome["team"], outcome["spread"], outcome["price"]),
            )
        stored += 1
    conn.commit()

    return {
        "snapshot_id": snapshot_id,
        "fetched_at": fetched_at,
        "games_with_lines": stored,
        "games_returned": len(games),
        "book": book,
        "credits": {"remaining": headers["remaining"], "used": headers["used"]},
    }


@app.post("/api/games/manual")
def add_manual_game(payload: ManualGame, conn=Depends(db)):
    """Add a game by hand so a pick can be made when the API is unavailable."""
    cur = conn.execute(
        "INSERT INTO games (week, commence_time, home_team, away_team) VALUES (?,?,?,?)",
        (payload.week, payload.commence_time, payload.home_team.strip(), payload.away_team.strip()),
    )
    conn.commit()
    return {"game_id": cur.lastrowid}


@app.delete("/api/games/{game_id}")
def remove_game(game_id: int, conn=Depends(db)):
    """Take a game off the board -- for a hand-added one entered by mistake.

    Refused once anyone has picked in it: a pick's game is part of the record,
    and the ledger would be left pointing at a game that no longer exists.
    Remove the pick first if that's really the intent.
    """
    _game_row(conn, game_id)
    picks = conn.execute(
        "SELECT player, team FROM picks WHERE game_id = ? ORDER BY slot", (game_id,)
    ).fetchall()
    if picks:
        held = ", ".join(f"{p['player']} has {p['team']}" for p in picks)
        raise HTTPException(
            409,
            {
                "code": "GAME_HAS_PICKS",
                "message": f"Can't remove this game -- {held}. Remove the pick first.",
            },
        )
    # Lines are snapshot data for this game and go with it.
    conn.execute("DELETE FROM lines WHERE game_id = ?", (game_id,))
    conn.execute("DELETE FROM games WHERE id = ?", (game_id,))
    conn.commit()
    return {"deleted": game_id}


# ---------------------------------------------------------------- picks


@app.post("/api/picks")
def make_pick(payload: PickIn, conn=Depends(db)):
    game = _game_row(conn, payload.game_id)
    existing = _picks_for_week(conn, payload.week)
    on_clock = rules.player_on_clock(payload.week, {p["slot"] for p in existing})
    slot = on_clock[0] if on_clock else rules.SLOTS_PER_WEEK + 1

    try:
        rules.validate_pick(
            week=payload.week,
            player=payload.player,
            game=game,
            team=payload.team,
            slot=slot,
            on_clock=on_clock,
            existing_picks=existing,
        )
    except rules.PickError as exc:
        raise _pick_error(exc) from exc

    if payload.override_spread is not None:
        # Hand-entered line: no freshness rule applies, but it's flagged forever.
        if not (payload.override_reason or "").strip():
            raise HTTPException(
                400,
                {"code": "OVERRIDE_NEEDS_REASON", "message": "Say why the line was entered by hand."},
            )
        spread, source, snapshot_id = payload.override_spread, "override", None
        line_fetched_at = now_iso()
    else:
        snapshot = _latest_snapshot(conn, payload.week)
        if not snapshot:
            raise HTTPException(
                409,
                {"code": "NO_LINES", "message": "No lines pulled for this week yet. Refresh first."},
            )
        if not snapshot["fresh"]:
            raise HTTPException(
                409,
                {
                    "code": "STALE_LINES",
                    "message": f"These lines are {snapshot['age_seconds'] // 60} minutes old. "
                               f"Refresh before picking.",
                },
            )
        line = conn.execute(
            "SELECT spread FROM lines WHERE snapshot_id = ? AND game_id = ? AND team = ?",
            (snapshot["id"], payload.game_id, payload.team),
        ).fetchone()
        if not line:
            raise HTTPException(
                409,
                {"code": "NO_LINE_FOR_TEAM", "message": f"No current line for {payload.team}."},
            )
        spread, source = line["spread"], "api"
        snapshot_id, line_fetched_at = snapshot["id"], snapshot["fetched_at"]

    ts = now_iso()
    cur = conn.execute(
        """INSERT INTO picks (week, slot, player, game_id, team, spread, line_source,
                              snapshot_id, line_fetched_at, override_reason, created_at, updated_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        (payload.week, slot, payload.player, payload.game_id, payload.team, spread, source,
         snapshot_id, line_fetched_at, payload.override_reason, ts, ts),
    )
    pick_id = cur.lastrowid
    _log(
        conn, pick_id, payload.week, payload.player, "create", payload.player,
        new=f"{payload.team} {rules.format_spread(spread)}",
        reason=payload.override_reason if source == "override" else None,
    )
    conn.commit()
    return {
        "pick_id": pick_id,
        "slot": slot,
        "team": payload.team,
        "spread": spread,
        "display": f"{payload.team} {rules.format_spread(spread)}",
        "line_source": source,
    }


@app.patch("/api/picks/{pick_id}")
def edit_pick(pick_id: int, payload: PickEdit, conn=Depends(db)):
    """Change a recorded pick. Every changed field is written to the ledger."""
    row = conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"No pick {pick_id}.")
    pick = dict(row)

    new_team = payload.team or pick["team"]
    new_spread = pick["spread"] if payload.spread is None else payload.spread
    game = _game_row(conn, pick["game_id"])

    if new_team != pick["team"]:
        try:
            rules.validate_pick(
                week=pick["week"],
                player=pick["player"],
                game=game,
                team=new_team,
                slot=pick["slot"],
                on_clock=None,
                existing_picks=_picks_for_week(conn, pick["week"]),
                exclude_pick_id=pick_id,
            )
        except rules.PickError as exc:
            raise _pick_error(exc) from exc

    changes = [
        ("team", pick["team"], new_team),
        ("spread", pick["spread"], new_spread),
    ]
    changed = [(f, o, n) for f, o, n in changes if o != n]
    if not changed:
        return {"pick_id": pick_id, "changed": []}

    conn.execute(
        "UPDATE picks SET team = ?, spread = ?, updated_at = ? WHERE id = ?",
        (new_team, new_spread, now_iso(), pick_id),
    )
    for field, old, new in changed:
        _log(
            conn, pick_id, pick["week"], pick["player"], "edit", payload.actor,
            field=field, old=old, new=new, reason=payload.reason,
        )
    conn.commit()
    return {"pick_id": pick_id, "changed": [c[0] for c in changed]}


@app.delete("/api/picks/{pick_id}")
def delete_pick(pick_id: int, payload: PickDelete, conn=Depends(db)):
    """Undo a pick. Only the week's most recent one, so the snake order stays intact."""
    row = conn.execute("SELECT * FROM picks WHERE id = ?", (pick_id,)).fetchone()
    if not row:
        raise HTTPException(404, f"No pick {pick_id}.")
    pick = dict(row)
    last = conn.execute(
        "SELECT MAX(slot) AS s FROM picks WHERE week = ?", (pick["week"],)
    ).fetchone()["s"]
    if pick["slot"] != last:
        raise HTTPException(
            409,
            {
                "code": "NOT_LAST_PICK",
                "message": f"Only the last pick of week {pick['week']} can be removed "
                           f"(that's slot {last}). Edit this one instead.",
            },
        )

    _log(
        conn, pick_id, pick["week"], pick["player"], "delete", payload.actor,
        old=f"{pick['team']} {rules.format_spread(pick['spread'])}", reason=payload.reason,
    )
    conn.execute("DELETE FROM picks WHERE id = ?", (pick_id,))
    conn.commit()
    return {"deleted": pick_id}


# ---------------------------------------------------------------- results


@app.post("/api/scores/refresh")
def refresh_scores(days_from: int = 3, conn=Depends(db)):
    """Pull finals and grade every pick they settle."""
    try:
        scores, headers = odds.fetch_scores(conn, days_from)
    except odds.OddsApiError as exc:
        raise HTTPException(
            502,
            {
                "code": "ODDS_UNAVAILABLE",
                "message": exc.message,
                "out_of_credits": exc.out_of_credits,
            },
        ) from exc

    updated = 0
    for game in scores:
        if not game["completed"] or game["home_score"] is None:
            continue
        cur = conn.execute(
            """UPDATE games SET home_score = ?, away_score = ?, completed = 1,
                                score_source = 'api', scored_at = ?
                WHERE event_id = ?""",
            (game["home_score"], game["away_score"], now_iso(), game["event_id"]),
        )
        updated += cur.rowcount
    conn.commit()
    return {
        "games_updated": updated,
        "credits": {"remaining": headers["remaining"], "used": headers["used"]},
    }


@app.post("/api/scores/manual")
def set_score(payload: ManualScore, conn=Depends(db)):
    """Type in a final the API didn't return."""
    _game_row(conn, payload.game_id)
    conn.execute(
        """UPDATE games SET home_score = ?, away_score = ?, completed = 1,
                            score_source = 'manual', scored_at = ? WHERE id = ?""",
        (payload.home_score, payload.away_score, now_iso(), payload.game_id),
    )
    conn.commit()
    return {"game_id": payload.game_id}


@app.get("/api/standings")
def standings(conn=Depends(db)):
    """Season record per player, plus a week-by-week grid."""
    rows = conn.execute(
        """SELECT p.*, g.home_team, g.away_team, g.home_score, g.away_score, g.completed
             FROM picks p JOIN games g ON g.id = p.game_id ORDER BY p.week, p.slot"""
    ).fetchall()

    table = {name: {"player": name, "wins": 0, "losses": 0, "pushes": 0, "pending": 0}
             for name in rules.PLAYERS}
    weekly: dict[int, dict] = {}
    for row in rows:
        pick = dict(row)
        result = rules.grade_pick(pick["team"], pick["spread"], pick)
        bucket = table.setdefault(
            pick["player"],
            {"player": pick["player"], "wins": 0, "losses": 0, "pushes": 0, "pending": 0},
        )
        key = {"WIN": "wins", "LOSS": "losses", "PUSH": "pushes", None: "pending"}[result]
        bucket[key] += 1
        week = weekly.setdefault(pick["week"], {name: [] for name in rules.PLAYERS})
        week.setdefault(pick["player"], []).append(
            {
                "team": pick["team"],
                "display": f"{pick['team']} {rules.format_spread(pick['spread'])}",
                "result": result,
            }
        )

    for bucket in table.values():
        decided = bucket["wins"] + bucket["losses"]
        bucket["win_pct"] = round(bucket["wins"] / decided, 3) if decided else None
        bucket["record"] = f"{bucket['wins']}-{bucket['losses']}" + (
            f"-{bucket['pushes']}" if bucket["pushes"] else ""
        )

    order = sorted(
        table.values(), key=lambda b: (b["wins"], -b["losses"]), reverse=True
    )
    return {"standings": order, "weekly": weekly}


@app.get("/api/ledger")
def ledger(week: int | None = None, limit: int = 200, conn=Depends(db)):
    """Full audit trail of picks -- creates, edits and removals."""
    if week is None:
        rows = conn.execute(
            "SELECT * FROM pick_ledger ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM pick_ledger WHERE week = ? ORDER BY id DESC LIMIT ?", (week, limit)
        ).fetchall()
    return {"entries": [dict(r) for r in rows]}


# ---------------------------------------------------------------- static UI

app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")
