"""End-to-end checks against the JSON API with the odds feed stubbed out."""
from datetime import datetime, timedelta, timezone

import pytest

from app import db as db_module
from app import odds
from tests.conftest import slate


def refresh(client, week=1):
    r = client.post("/api/lines/refresh", params={"week": week})
    assert r.status_code == 200, r.text
    return r.json()


def game_ids(client, week=1):
    board = client.get("/api/board", params={"week": week}).json()
    return {(g["away_team"], g["home_team"]): g["id"] for g in board["games"]}


def pick(client, player, team, game_id, week=1, **extra):
    return client.post(
        "/api/picks",
        json={"week": week, "player": player, "game_id": game_id, "team": team, **extra},
    )


def test_refresh_stores_a_slate_and_reports_credits(client, stub_odds):
    body = refresh(client)
    assert body["games_with_lines"] == 2
    assert body["credits"]["remaining"] == 480
    assert body["book"] == "DraftKings"

    board = client.get("/api/board", params={"week": 1}).json()
    assert board["snapshot"]["fresh"] is True
    assert board["on_clock"] == {"slot": 1, "player": "Jimmy"}
    bears = next(
        s for g in board["games"] for s in g["sides"] if s["team"] == "Chicago Bears"
    )
    assert bears["display"] == "+3"
    assert bears["available"] is True


def test_a_full_week_drafts_in_snake_order(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]

    # Week 1 order: Jimmy, Mack, Anthony | Anthony, Mack, Jimmy. Only four NFL
    # teams are on this stub slate, so add two more games for the back half.
    extra = []
    for home, away in [("Dallas Cowboys", "Philadelphia Eagles"),
                       ("Denver Broncos", "Las Vegas Raiders")]:
        r = client.post(
            "/api/games/manual",
            json={"week": 1, "home_team": home, "away_team": away},
        )
        extra.append(r.json()["game_id"])

    assert pick(client, "Jimmy", "Chicago Bears", gb_chi).status_code == 200
    assert pick(client, "Mack", "Kansas City Chiefs", kc_buf).status_code == 200
    assert pick(client, "Anthony", "Buffalo Bills", kc_buf).status_code == 200
    # Anthony picks again immediately -- the snake turns.
    r = pick(client, "Anthony", "Philadelphia Eagles", extra[0],
             override_spread=2.5, override_reason="hand-entered game")
    assert r.status_code == 200, r.text
    assert r.json()["slot"] == 4

    assert pick(client, "Mack", "Dallas Cowboys", extra[0],
                override_spread=-2.5, override_reason="hand-entered").status_code == 200
    assert pick(client, "Jimmy", "Denver Broncos", extra[1],
                override_spread=-1, override_reason="hand-entered").status_code == 200

    board = client.get("/api/board", params={"week": 1}).json()
    assert board["on_clock"] is None
    assert [p["player"] for p in board["picks"]] == board["draft_order"]

    # A seventh pick has nowhere to go.
    r = pick(client, "Jimmy", "Green Bay Packers", gb_chi)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "WEEK_FULL"


def test_out_of_turn_pick_is_refused(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    r = pick(client, "Anthony", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "OUT_OF_TURN"


def test_taken_team_is_closed_but_the_far_side_stays_open(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)

    r = pick(client, "Mack", "Chicago Bears", gb_chi)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "TEAM_TAKEN"

    assert pick(client, "Mack", "Green Bay Packers", gb_chi).status_code == 200

    board = client.get("/api/board", params={"week": 1}).json()
    sides = {s["team"]: s for g in board["games"] for s in g["sides"]}
    assert sides["Chicago Bears"]["reason"] == "Taken by Jimmy"
    assert sides["Green Bay Packers"]["reason"] == "Taken by Mack"


def test_a_team_reopens_the_following_week(client, stub_odds):
    refresh(client, week=1)
    ids = game_ids(client, week=1)
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")], week=1)

    stub_odds(slate(commence="2026-09-17T00:20:00Z"))
    refresh(client, week=2)
    ids2 = game_ids(client, week=2)
    # Week 2 leads off with Mack, and the Bears are back on the board.
    r = pick(client, "Mack", "Chicago Bears", ids2[("Chicago Bears", "Green Bay Packers")], week=2)
    assert r.status_code == 200, r.text


def test_one_player_cannot_take_both_sides(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)
    pick(client, "Mack", "Kansas City Chiefs", kc_buf)
    pick(client, "Anthony", "Buffalo Bills", kc_buf)
    pick(client, "Anthony", "Green Bay Packers", gb_chi)   # slot 4, legal
    # Mack now has slot 5 and already holds the Chiefs.
    r = pick(client, "Mack", "Buffalo Bills", kc_buf)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "TEAM_TAKEN"


def test_stale_lines_block_a_pick(client, stub_odds, monkeypatch):
    refresh(client)
    ids = game_ids(client)
    # Age the snapshot past the one-hour window.
    stale = (datetime.now(timezone.utc) - timedelta(hours=2)).replace(microsecond=0).isoformat()
    conn = db_module.connect()
    conn.execute("UPDATE snapshots SET fetched_at = ?", (stale,))
    conn.commit()
    conn.close()

    board = client.get("/api/board", params={"week": 1}).json()
    assert board["snapshot"]["fresh"] is False

    r = pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "STALE_LINES"

    # Refreshing clears the block.
    refresh(client)
    assert pick(
        client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")]
    ).status_code == 200


def test_pick_records_the_line_it_was_made_against(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])

    # The book moves, but the recorded pick keeps the number it was taken at.
    stub_odds(slate(spread=-7.0))
    refresh(client)
    saved = client.get("/api/board", params={"week": 1}).json()["picks"][0]
    assert saved["spread"] == 3.0
    assert saved["line_source"] == "api"
    assert saved["snapshot_id"] is not None


def test_override_is_flagged_and_needs_a_reason(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]

    r = pick(client, "Jimmy", "Chicago Bears", gb_chi, override_spread=6.5)
    assert r.status_code == 400
    assert r.json()["detail"]["code"] == "OVERRIDE_NEEDS_REASON"

    r = pick(client, "Jimmy", "Chicago Bears", gb_chi,
             override_spread=6.5, override_reason="out of API credits")
    assert r.status_code == 200
    saved = client.get("/api/board", params={"week": 1}).json()["picks"][0]
    assert saved["line_source"] == "override"
    assert saved["spread"] == 6.5
    assert saved["override_reason"] == "out of API credits"
    assert saved["snapshot_id"] is None


def test_override_works_when_the_api_is_out_of_credits(client, monkeypatch):
    def broke(conn):
        raise odds.OddsApiError("quota exhausted", status=429, out_of_credits=True)
    monkeypatch.setattr(odds, "fetch_spreads", broke)

    r = client.post("/api/lines/refresh", params={"week": 1})
    assert r.status_code == 502
    assert r.json()["detail"]["out_of_credits"] is True

    gid = client.post(
        "/api/games/manual",
        json={"week": 1, "home_team": "Green Bay Packers", "away_team": "Chicago Bears"},
    ).json()["game_id"]
    r = pick(client, "Jimmy", "Chicago Bears", gid,
             override_spread=3, override_reason="API quota gone")
    assert r.status_code == 200, r.text
    assert r.json()["line_source"] == "override"


def test_pick_without_any_lines_is_refused(client, stub_odds):
    gid = client.post(
        "/api/games/manual",
        json={"week": 1, "home_team": "Green Bay Packers", "away_team": "Chicago Bears"},
    ).json()["game_id"]
    r = pick(client, "Jimmy", "Chicago Bears", gid)
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NO_LINES"


def test_edits_are_written_to_the_ledger(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    pid = pick(
        client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")]
    ).json()["pick_id"]

    r = client.patch(
        f"/api/picks/{pid}",
        json={"team": "Green Bay Packers", "spread": -3.0,
              "actor": "Jimmy", "reason": "picked the wrong side"},
    )
    assert r.status_code == 200
    assert sorted(r.json()["changed"]) == ["spread", "team"]

    entries = client.get("/api/ledger", params={"week": 1}).json()["entries"]
    assert [e["action"] for e in entries].count("edit") == 2
    assert any(e["action"] == "create" for e in entries)
    team_edit = next(e for e in entries if e["field"] == "team")
    assert team_edit["old_value"] == "Chicago Bears"
    assert team_edit["new_value"] == "Green Bay Packers"
    assert team_edit["actor"] == "Jimmy"
    assert team_edit["reason"] == "picked the wrong side"


def test_edit_cannot_steal_a_team_someone_else_holds(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    pid = pick(client, "Mack", "Kansas City Chiefs", kc_buf).json()["pick_id"]
    pick(client, "Anthony", "Buffalo Bills", kc_buf)

    r = client.patch(f"/api/picks/{pid}", json={"team": "Buffalo Bills", "actor": "Mack"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "TEAM_TAKEN"


def test_no_op_edit_writes_nothing(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    pid = pick(
        client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")]
    ).json()["pick_id"]
    client.patch(f"/api/picks/{pid}", json={"team": "Chicago Bears", "actor": "Jimmy"})
    entries = client.get("/api/ledger").json()["entries"]
    assert [e["action"] for e in entries] == ["create"]


def test_only_the_last_pick_can_be_removed(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    first = pick(
        client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")]
    ).json()["pick_id"]
    second = pick(
        client, "Mack", "Kansas City Chiefs", ids[("Buffalo Bills", "Kansas City Chiefs")]
    ).json()["pick_id"]

    r = client.request("DELETE", f"/api/picks/{first}", json={"actor": "Jimmy"})
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "NOT_LAST_PICK"

    r = client.request(
        "DELETE", f"/api/picks/{second}", json={"actor": "Mack", "reason": "misclick"}
    )
    assert r.status_code == 200

    board = client.get("/api/board", params={"week": 1}).json()
    assert board["on_clock"] == {"slot": 2, "player": "Mack"}
    assert any(
        e["action"] == "delete" and e["reason"] == "misclick"
        for e in client.get("/api/ledger").json()["entries"]
    )


def test_scores_grade_picks_and_feed_the_standings(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)        # +3
    pick(client, "Mack", "Green Bay Packers", gb_chi)     # -3
    pick(client, "Anthony", "Buffalo Bills", kc_buf)      # +1.5

    # Bears lose by 7, so +3 doesn't cover. Bills win outright.
    client.post("/api/scores/manual",
                json={"game_id": gb_chi, "home_score": 27, "away_score": 20})
    client.post("/api/scores/manual",
                json={"game_id": kc_buf, "home_score": 17, "away_score": 24})

    body = client.get("/api/standings").json()
    records = {s["player"]: s["record"] for s in body["standings"]}
    assert records["Jimmy"] == "0-1"
    assert records["Mack"] == "1-0"
    assert records["Anthony"] == "1-0"

    results = {p["team"]: p["result"] for p in client.get(
        "/api/board", params={"week": 1}).json()["picks"]}
    assert results["Chicago Bears"] == "LOSS"
    assert results["Green Bay Packers"] == "WIN"
    assert results["Buffalo Bills"] == "WIN"


def test_pushes_are_tracked_separately_from_wins_and_losses(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)   # +3
    client.post("/api/scores/manual",
                json={"game_id": gb_chi, "home_score": 24, "away_score": 21})
    jimmy = next(s for s in client.get("/api/standings").json()["standings"]
                 if s["player"] == "Jimmy")
    assert (jimmy["pushes"], jimmy["wins"], jimmy["losses"]) == (1, 0, 0)
    assert jimmy["record"] == "0-0-1"
    assert jimmy["win_pct"] is None


def test_score_refresh_grades_from_the_api(client, stub_odds, monkeypatch):
    refresh(client)
    ids = game_ids(client)
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])

    monkeypatch.setattr(odds, "fetch_scores", lambda conn, days: ([{
        "event_id": "evt-gb-chi",
        "home_team": "Green Bay Packers", "away_team": "Chicago Bears",
        "completed": True, "home_score": 20, "away_score": 24,
    }], {"remaining": 478, "used": 22}))

    r = client.post("/api/scores/refresh", params={"days_from": 3})
    assert r.status_code == 200
    assert r.json()["games_updated"] == 1
    assert client.get("/api/board", params={"week": 1}).json()["picks"][0]["result"] == "WIN"


def test_weeks_outside_the_season_are_rejected(client):
    assert client.get("/api/board", params={"week": 0}).status_code == 400
    assert client.get("/api/board", params={"week": 19}).status_code == 400
    assert client.get("/api/board", params={"week": 18}).status_code == 200


def test_ui_is_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "Pigskin" in r.text


def test_a_game_can_be_taken_back_off_the_board(client, stub_odds):
    """A hand-added game entered by mistake shouldn't be stuck there all season."""
    gid = client.post(
        "/api/games/manual",
        json={"week": 1, "home_team": "Denver Broncos", "away_team": "Las Vegas Raiders"},
    ).json()["game_id"]
    assert any(g["id"] == gid for g in
               client.get("/api/board", params={"week": 1}).json()["games"])

    assert client.request("DELETE", f"/api/games/{gid}").status_code == 200
    assert not any(g["id"] == gid for g in
                   client.get("/api/board", params={"week": 1}).json()["games"])


def test_a_game_with_a_pick_in_it_cannot_be_removed(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)

    r = client.request("DELETE", f"/api/games/{gb_chi}")
    assert r.status_code == 409
    assert r.json()["detail"]["code"] == "GAME_HAS_PICKS"
    assert "Jimmy has Chicago Bears" in r.json()["detail"]["message"]
    # The game and the pick both survive the refusal.
    assert len(client.get("/api/board", params={"week": 1}).json()["picks"]) == 1


def test_removing_a_game_takes_its_lines_with_it(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    assert client.request("DELETE", f"/api/games/{kc_buf}").status_code == 200

    conn = db_module.connect()
    orphans = conn.execute(
        "SELECT COUNT(*) c FROM lines WHERE game_id = ?", (kc_buf,)
    ).fetchone()["c"]
    conn.close()
    assert orphans == 0


def test_board_marks_which_games_can_be_removed(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)

    games = {g["id"]: g["removable"] for g in
             client.get("/api/board", params={"week": 1}).json()["games"]}
    assert games[gb_chi] is False
    assert games[ids[("Buffalo Bills", "Kansas City Chiefs")]] is True


def test_removing_a_missing_game_is_a_404(client):
    assert client.request("DELETE", "/api/games/999").status_code == 404
