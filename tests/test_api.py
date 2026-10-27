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


def test_board_without_a_week_serves_the_live_one(client, stub_odds):
    """Opening the site lands on the week the season is in, not week 1."""
    from app import main, rules

    live = rules.current_week(main.SEASON_ANCHOR)
    body = client.get("/api/board").json()
    assert body["week"] == live
    assert body["current_week"] == live


def test_an_explicit_week_still_wins(client, stub_odds):
    from app import main, rules

    live = rules.current_week(main.SEASON_ANCHOR)
    other = 1 if live != 1 else 18
    body = client.get("/api/board", params={"week": other}).json()
    assert body["week"] == other
    # The live week is still reported, so the UI can offer a way back to it.
    assert body["current_week"] == live


def test_every_week_reports_the_same_live_week(client, stub_odds):
    weeks = {client.get("/api/board", params={"week": w}).json()["current_week"]
             for w in (1, 5, 12, 18)}
    assert len(weeks) == 1


def test_static_assets_must_be_revalidated(client):
    """Without this a phone keeps running the previous deploy's JavaScript."""
    for path in ("/", "/static/app.js", "/static/styles.css", "/static/index.html"):
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers.get("cache-control") == "no-cache", path


def test_unchanged_assets_still_answer_304(client):
    """no-cache costs a round trip, not a re-download."""
    first = client.get("/static/app.js")
    again = client.get("/static/app.js", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304


def test_cancel_buttons_skip_form_validation(client):
    """A dialog's Cancel shares a form with required fields; without
    formnovalidate the browser blocks it and the dialog will not close."""
    html = client.get("/").text
    cancels = [line for line in html.splitlines() if 'value="cancel"' in line]
    assert len(cancels) == 2
    assert all("formnovalidate" in line for line in cancels), cancels


@pytest.fixture
def texting(monkeypatch):
    """Twilio configured, with every send captured instead of sent."""
    from app import notify

    monkeypatch.setenv("TWILIO_ACCOUNT_SID", "AC_test")
    monkeypatch.setenv("TWILIO_AUTH_TOKEN", "token_test")
    monkeypatch.setenv("TWILIO_FROM_NUMBER", "+15550000000")
    monkeypatch.setenv("PHONE_JIMMY", "+15550000001")
    monkeypatch.setenv("PHONE_MACK", "+15550000002")
    monkeypatch.setenv("PHONE_ANTHONY", "+15550000003")
    monkeypatch.setenv("APP_URL", "https://example.test")

    outbox = []
    monkeypatch.setattr(
        notify, "_post",
        lambda sid, token, sender, to, body: outbox.append((to, body)) or "SM_fake",
    )
    return outbox


def test_a_pick_texts_whoever_is_now_on_the_clock(client, stub_odds, texting):
    refresh(client)
    ids = game_ids(client)
    r = pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    assert r.status_code == 200
    assert r.json()["notified"] == {"player": "Mack", "status": "sent"}

    to, body = texting[0]
    assert to == "+15550000002"                    # Mack is next in week 1
    assert "week 1, pick 2 of 6" in body
    assert "Jimmy took Chicago Bears +3" in body


def test_the_last_pick_of_a_week_texts_nobody(client, stub_odds, texting):
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    extra = [
        client.post("/api/games/manual",
                    json={"week": 1, "home_team": h, "away_team": a}).json()["game_id"]
        for h, a in [("Dallas Cowboys", "Philadelphia Eagles"),
                     ("Denver Broncos", "Las Vegas Raiders")]
    ]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)
    pick(client, "Mack", "Kansas City Chiefs", kc_buf)
    pick(client, "Anthony", "Buffalo Bills", kc_buf)
    pick(client, "Anthony", "Philadelphia Eagles", extra[0],
         override_spread=2.5, override_reason="hand-entered")
    pick(client, "Mack", "Dallas Cowboys", extra[0],
         override_spread=-2.5, override_reason="hand-entered")
    last = pick(client, "Jimmy", "Denver Broncos", extra[1],
                override_spread=-1, override_reason="hand-entered")

    assert last.json()["notified"] is None
    assert len(texting) == 5                        # picks 1-5 each woke someone


def test_a_texting_outage_never_costs_a_pick(client, stub_odds, texting, monkeypatch):
    from app import notify

    def boom(*args):
        raise RuntimeError("twilio is down")

    monkeypatch.setattr(notify, "_post", boom)
    refresh(client)
    ids = game_ids(client)
    r = pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])

    assert r.status_code == 200
    assert r.json()["notified"] == {"player": "Mack", "status": "failed"}
    board = client.get("/api/board", params={"week": 1}).json()
    assert [p["label"] for p in board["picks"]] == ["Chicago Bears +3"]
    assert board["on_clock"] == {"slot": 2, "player": "Mack"}


def test_picks_still_work_with_no_twilio_at_all(client, stub_odds, monkeypatch):
    for key in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_FROM_NUMBER",
                "PHONE_JIMMY", "PHONE_MACK", "PHONE_ANTHONY"):
        monkeypatch.delenv(key, raising=False)
    refresh(client)
    ids = game_ids(client)
    r = pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    assert r.status_code == 200
    assert r.json()["notified"] == {"player": "Mack", "status": "skipped"}


def test_the_board_reports_whether_texting_is_on(client, stub_odds, texting):
    state = client.get("/api/board", params={"week": 1}).json()["texting"]
    assert state["configured"] is True
    assert state["players_with_numbers"] == ["Anthony", "Jimmy", "Mack"]
    assert "5550000001" not in str(state)


def _graded_week(client):
    """Week 1 with three graded picks, spanning a cover, a miss and a push."""
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)         # +3
    pick(client, "Mack", "Green Bay Packers", gb_chi)      # -3
    pick(client, "Anthony", "Buffalo Bills", kc_buf)       # +1.5
    # Packers win by 3 off -3: a push both ways. Bills win by 7 off +1.5: +8.5.
    client.post("/api/scores/manual",
                json={"game_id": gb_chi, "home_score": 24, "away_score": 21})
    client.post("/api/scores/manual",
                json={"game_id": kc_buf, "home_score": 17, "away_score": 24})
    return gb_chi, kc_buf


def test_each_pick_reports_what_it_beat_the_spread_by(client, stub_odds):
    _graded_week(client)
    by_team = {p["team"]: p for p in
               client.get("/api/board", params={"week": 1}).json()["picks"]}
    assert by_team["Chicago Bears"]["margin"] == 0           # push
    assert by_team["Chicago Bears"]["margin_display"] == "0"
    assert by_team["Green Bay Packers"]["margin"] == 0
    assert by_team["Buffalo Bills"]["margin"] == 8.5
    assert by_team["Buffalo Bills"]["margin_display"] == "+8.5"


def test_an_ungraded_pick_has_no_margin(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    saved = client.get("/api/board", params={"week": 1}).json()["picks"][0]
    assert saved["margin"] is None
    assert saved["margin_display"] == "—"


def test_standings_carry_a_season_differential(client, stub_odds):
    _graded_week(client)
    table = {s["player"]: s for s in client.get("/api/standings").json()["standings"]}
    assert table["Anthony"]["margin"] == 8.5
    assert table["Anthony"]["margin_display"] == "+8.5"
    assert table["Jimmy"]["margin"] == 0
    assert table["Mack"]["margin"] == 0


def test_differential_breaks_a_tie_in_the_standings(client, stub_odds):
    """Two players on the same record should not be ordered arbitrarily."""
    refresh(client)
    ids = game_ids(client)
    gb_chi = ids[("Chicago Bears", "Green Bay Packers")]
    kc_buf = ids[("Buffalo Bills", "Kansas City Chiefs")]
    pick(client, "Jimmy", "Chicago Bears", gb_chi)        # +3, covers big
    pick(client, "Mack", "Kansas City Chiefs", kc_buf)    # -1.5, covers barely
    client.post("/api/scores/manual",
                json={"game_id": gb_chi, "home_score": 10, "away_score": 31})
    client.post("/api/scores/manual",
                json={"game_id": kc_buf, "home_score": 24, "away_score": 21})

    order = [s["player"] for s in client.get("/api/standings").json()["standings"]]
    table = {s["player"]: s for s in client.get("/api/standings").json()["standings"]}
    assert table["Jimmy"]["record"] == table["Mack"]["record"] == "1-0"
    assert table["Jimmy"]["margin"] == 24          # won by 21 off +3
    assert table["Mack"]["margin"] == 1.5          # won by 3 off -1.5
    assert order.index("Jimmy") < order.index("Mack")


def test_the_week_and_running_totals_are_reported(client, stub_odds):
    _graded_week(client)
    body = client.get("/api/standings").json()
    assert body["week_margin"]["1"]["Anthony"] == 8.5
    assert body["running_margin"]["1"]["Anthony"] == 8.5
    assert body["running_margin"]["1"]["Jimmy"] == 0


def test_the_running_total_accumulates_across_weeks(client, stub_odds):
    _graded_week(client)                                    # Anthony +8.5

    stub_odds(slate(commence="2026-09-17T00:20:00Z"))
    refresh(client, week=2)
    ids2 = game_ids(client, week=2)
    gb_chi2 = ids2[("Chicago Bears", "Green Bay Packers")]
    # Week 2 leads off with Mack, then Anthony.
    pick(client, "Mack", "Chicago Bears", gb_chi2, week=2)          # +3
    pick(client, "Anthony", "Green Bay Packers", gb_chi2, week=2)   # -3
    client.post("/api/scores/manual",
                json={"game_id": gb_chi2, "home_score": 30, "away_score": 10})

    body = client.get("/api/standings").json()
    assert body["week_margin"]["2"]["Anthony"] == 17        # won by 20 off -3
    assert body["running_margin"]["1"]["Anthony"] == 8.5
    assert body["running_margin"]["2"]["Anthony"] == 25.5   # 8.5 carried forward
    table = {s["player"]: s for s in body["standings"]}
    assert table["Anthony"]["margin"] == 25.5


def test_an_ungraded_week_contributes_nothing(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    body = client.get("/api/standings").json()
    assert body["week_margin"] == {}
    assert body["running_margin"] == {}
    assert {s["player"]: s["margin"] for s in body["standings"]}["Jimmy"] == 0


# One game per slot, so a slot's outcome can be set without tripping the
# both-sides rule -- the snake hands slots 3 and 4 to the same player.
SLOT_GAMES = [
    ("Green Bay Packers", "Chicago Bears"),
    ("Kansas City Chiefs", "Buffalo Bills"),
    ("Dallas Cowboys", "Philadelphia Eagles"),
    ("Baltimore Ravens", "Cincinnati Bengals"),
    ("San Francisco 49ers", "Seattle Seahawks"),
    ("Detroit Lions", "Minnesota Vikings"),
]


def _draft_full_week(client, week, finals):
    """Draft all six slots in `week`, each taking a -3 home favourite.

    `finals` maps a slot (1-6) to its home margin: +7 covers, -7 does not,
    +3 is a push. A slot left out of `finals` stays ungraded.
    """
    games = [
        client.post("/api/games/manual",
                    json={"week": week, "home_team": home, "away_team": away}).json()["game_id"]
        for home, away in SLOT_GAMES
    ]
    order = client.get("/api/board", params={"week": week}).json()["draft_order"]

    for slot, player in enumerate(order, start=1):
        home = SLOT_GAMES[slot - 1][0]
        r = pick(client, player, home, games[slot - 1], week=week,
                 override_spread=-3, override_reason="test slate")
        assert r.status_code == 200, r.text
        assert r.json()["slot"] == slot

    for slot, home_margin in finals.items():
        client.post("/api/scores/manual", json={
            "game_id": games[slot - 1],
            "home_score": 20 + home_margin, "away_score": 20})
    return games


# Every slot's favourite covers by 4, except where a test says otherwise.
ALL_COVER = {slot: 7 for slot in range(1, 7)}


def test_analytics_is_empty_before_anything_is_graded(client, stub_odds):
    body = client.get("/api/analytics").json()
    assert body["weeks_graded"] == 0
    assert body["weeks_drafted"] == 0
    assert [s["slot"] for s in body["by_slot"]] == [1, 2, 3, 4, 5, 6]
    assert all(s["graded"] == 0 for s in body["by_slot"])
    assert all(s["avg_margin"] is None for s in body["by_slot"])


def test_every_slot_is_reported_even_when_unused(client, stub_odds):
    refresh(client)
    ids = game_ids(client)
    pick(client, "Jimmy", "Chicago Bears", ids[("Chicago Bears", "Green Bay Packers")])
    body = client.get("/api/analytics").json()
    assert len(body["by_slot"]) == 6
    assert body["by_slot"][0]["pending"] == 1
    assert body["by_slot"][1]["graded"] == 0


def test_slot_records_add_up_across_weeks(client, stub_odds):
    # Slots 1-2 cover by 4 each week, slot 3 misses by 10, slot 4 pushes.
    finals = {1: 7, 2: 7, 3: -7, 4: 3, 5: 7, 6: -7}
    _draft_full_week(client, 1, finals)
    _draft_full_week(client, 2, finals)

    body = client.get("/api/analytics").json()
    assert body["weeks_graded"] == 2
    assert body["weeks_drafted"] == 2

    by_slot = {s["slot"]: s for s in body["by_slot"]}
    assert by_slot[1]["record"] == "2-0"
    assert by_slot[1]["margin"] == 8          # +4 a week
    assert by_slot[1]["avg_margin"] == 4
    assert by_slot[3]["record"] == "0-2"
    assert by_slot[3]["margin"] == -20        # -10 a week
    assert by_slot[4]["record"] == "0-0-2"    # a 3-point win off -3
    assert by_slot[4]["margin"] == 0


def test_every_graded_pick_lands_in_exactly_one_slot(client, stub_odds):
    _draft_full_week(client, 1, ALL_COVER)
    body = client.get("/api/analytics").json()
    counted = sum(s["graded"] for s in body["by_slot"])
    standings = client.get("/api/standings").json()["standings"]
    expected = sum(p["wins"] + p["losses"] + p["pushes"] for p in standings)
    assert counted == expected == 6


def test_slot_totals_match_the_halves(client, stub_odds):
    _draft_full_week(client, 1, {1: 7, 2: -7, 3: 3, 4: 7, 5: -7, 6: 3})
    body = client.get("/api/analytics").json()
    by_slot = {s["slot"]: s for s in body["by_slot"]}
    halves = {h["half"]: h for h in body["by_half"]}
    assert halves["first"]["margin"] == sum(by_slot[s]["margin"] for s in (1, 2, 3))
    assert halves["second"]["margin"] == sum(by_slot[s]["margin"] for s in (4, 5, 6))
    assert halves["first"]["graded"] + halves["second"]["graded"] == 6


def test_the_player_grid_follows_the_rotating_snake(client, stub_odds):
    """Week 1 gives Jimmy slots 1 and 6; week 2 gives him 3 and 4."""
    _draft_full_week(client, 1, ALL_COVER)
    _draft_full_week(client, 2, ALL_COVER)

    grid = client.get("/api/analytics").json()["by_player_slot"]
    jimmy = {cell["slot"]: cell for cell in grid["Jimmy"]}
    assert jimmy[1]["graded"] == 1 and jimmy[6]["graded"] == 1      # week 1
    assert jimmy[3]["graded"] == 1 and jimmy[4]["graded"] == 1      # week 2
    assert jimmy[2]["graded"] == 0 and jimmy[5]["graded"] == 0

    # Each slot's totals are the three players' cells for that slot.
    by_slot = {s["slot"]: s for s in client.get("/api/analytics").json()["by_slot"]}
    for slot in range(1, 7):
        per_player = sum(
            next(c for c in grid[p] if c["slot"] == slot)["graded"]
            for p in grid
        )
        assert per_player == by_slot[slot]["graded"]


def test_a_slot_with_no_graded_picks_has_no_average(client, stub_odds):
    _draft_full_week(client, 1, {1: 7})             # only slot 1 has a final
    by_slot = {s["slot"]: s for s in client.get("/api/analytics").json()["by_slot"]}
    assert by_slot[1]["graded"] == 1
    assert by_slot[5]["graded"] == 0
    assert by_slot[5]["avg_margin"] is None
    assert by_slot[5]["avg_margin_display"] == "—"
