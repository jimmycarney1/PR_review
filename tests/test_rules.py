from datetime import datetime, timedelta, timezone

import pytest

from app import rules


def test_draft_order_rotates_and_snakes():
    assert rules.draft_order(1) == ["Jimmy", "Mack", "Anthony", "Anthony", "Mack", "Jimmy"]
    assert rules.draft_order(2) == ["Mack", "Anthony", "Jimmy", "Jimmy", "Anthony", "Mack"]
    assert rules.draft_order(3) == ["Anthony", "Jimmy", "Mack", "Mack", "Jimmy", "Anthony"]
    # The rotation has a period of three weeks.
    assert rules.draft_order(4) == rules.draft_order(1)
    assert rules.draft_order(18) == rules.draft_order(3)


@pytest.mark.parametrize("week", range(1, rules.TOTAL_WEEKS + 1))
def test_everyone_picks_twice_every_week(week):
    order = rules.draft_order(week)
    assert len(order) == 6
    assert {name: order.count(name) for name in rules.PLAYERS} == dict.fromkeys(rules.PLAYERS, 2)


def test_each_player_leads_off_six_times_across_the_season():
    leadoffs = [rules.draft_order(w)[0] for w in range(1, 19)]
    assert {name: leadoffs.count(name) for name in rules.PLAYERS} == dict.fromkeys(rules.PLAYERS, 6)


def test_on_clock_walks_the_snake():
    taken = set()
    seen = []
    while (clock := rules.player_on_clock(1, taken)):
        seen.append(clock)
        taken.add(clock[0])
    assert seen == list(enumerate(rules.draft_order(1), start=1))
    assert rules.player_on_clock(1, taken) is None


GAME = {"id": 1, "home_team": "Green Bay Packers", "away_team": "Chicago Bears"}


def _pick(pid, player, team, game_id=1, slot=1):
    return {"id": pid, "player": player, "team": team, "game_id": game_id, "slot": slot}


def _validate(**kw):
    base = dict(
        week=1, player="Jimmy", game=GAME, team="Chicago Bears", slot=1,
        on_clock=(1, "Jimmy"), existing_picks=[],
    )
    rules.validate_pick(**{**base, **kw})


def test_accepts_a_legal_opening_pick():
    _validate()


def test_team_is_locked_for_the_rest_of_the_week():
    with pytest.raises(rules.PickError) as exc:
        _validate(
            player="Mack", slot=2, on_clock=(2, "Mack"),
            existing_picks=[_pick(1, "Jimmy", "Chicago Bears")],
        )
    assert exc.value.code == "TEAM_TAKEN"


def test_another_player_may_take_the_far_side_of_the_same_game():
    _validate(
        player="Mack", team="Green Bay Packers", slot=2, on_clock=(2, "Mack"),
        existing_picks=[_pick(1, "Jimmy", "Chicago Bears")],
    )


def test_one_player_cannot_hold_both_sides_of_a_game():
    with pytest.raises(rules.PickError) as exc:
        _validate(
            team="Green Bay Packers", slot=6, on_clock=(6, "Jimmy"),
            existing_picks=[_pick(1, "Jimmy", "Chicago Bears")],
        )
    assert exc.value.code == "BOTH_SIDES"


def test_picking_out_of_turn_is_rejected():
    with pytest.raises(rules.PickError) as exc:
        _validate(player="Anthony", on_clock=(1, "Jimmy"))
    assert exc.value.code == "OUT_OF_TURN"


def test_team_must_be_playing_in_the_game():
    with pytest.raises(rules.PickError) as exc:
        _validate(team="Detroit Lions")
    assert exc.value.code == "TEAM_NOT_IN_GAME"


def test_edit_does_not_collide_with_the_pick_being_edited():
    existing = [_pick(7, "Jimmy", "Chicago Bears")]
    rules.validate_pick(
        week=1, player="Jimmy", game=GAME, team="Chicago Bears", slot=1,
        on_clock=None, existing_picks=existing, exclude_pick_id=7,
    )


FINAL = {
    "home_team": "Green Bay Packers", "away_team": "Chicago Bears",
    "home_score": 20, "away_score": 24, "completed": 1,
}


def test_underdog_covers_outright_win():
    assert rules.grade_pick("Chicago Bears", 3, FINAL) == "WIN"


def test_favorite_loses_outright():
    assert rules.grade_pick("Green Bay Packers", -3, FINAL) == "LOSS"


def test_favorite_can_win_the_game_and_lose_the_bet():
    game = {**FINAL, "home_score": 24, "away_score": 21}
    assert rules.grade_pick("Green Bay Packers", -7, game) == "LOSS"
    assert rules.grade_pick("Chicago Bears", 7, game) == "WIN"


def test_exact_landing_is_a_push():
    game = {**FINAL, "home_score": 24, "away_score": 21}
    assert rules.grade_pick("Green Bay Packers", -3, game) == "PUSH"
    assert rules.grade_pick("Chicago Bears", 3, game) == "PUSH"


def test_hook_prevents_a_push():
    game = {**FINAL, "home_score": 24, "away_score": 21}
    assert rules.grade_pick("Green Bay Packers", -3.5, game) == "LOSS"
    assert rules.grade_pick("Green Bay Packers", -2.5, game) == "WIN"


def test_unplayed_game_has_no_result():
    assert rules.grade_pick("Chicago Bears", 3, {**FINAL, "completed": 0}) is None
    assert rules.grade_pick(
        "Chicago Bears", 3,
        {**FINAL, "home_score": None, "away_score": None, "completed": 0},
    ) is None


def test_line_freshness_window_is_one_hour():
    now = datetime(2026, 9, 10, 18, 0, tzinfo=timezone.utc)
    fresh = (now - timedelta(minutes=59)).isoformat()
    stale = (now - timedelta(minutes=61)).isoformat()
    assert rules.line_is_fresh(fresh, now)
    assert not rules.line_is_fresh(stale, now)
    # Exactly one hour still counts.
    assert rules.line_is_fresh((now - timedelta(hours=1)).isoformat(), now)


def test_kickoffs_bucket_into_weeks_from_the_anchor():
    anchor = "2026-09-08"
    assert rules.week_for_kickoff("2026-09-10T00:20:00Z", anchor) == 1   # Thursday opener
    assert rules.week_for_kickoff("2026-09-13T17:00:00Z", anchor) == 1   # Sunday early
    assert rules.week_for_kickoff("2026-09-14T00:20:00Z", anchor) == 1   # Sunday night
    assert rules.week_for_kickoff("2026-09-17T00:15:00Z", anchor) == 2   # next Thursday


def test_monday_night_stays_with_its_own_week():
    """MNF kicks at 8:15pm ET, which is already Tuesday in UTC.

    A midnight rollover would file every Monday nighter under the next week.
    """
    anchor = "2026-09-08"
    assert rules.week_for_kickoff("2026-09-15T00:15:00Z", anchor) == 1   # kickoff
    assert rules.week_for_kickoff("2026-09-15T04:00:00Z", anchor) == 1   # still playing
    # A real week 17 Monday nighter that used to land in week 18.
    assert rules.week_for_kickoff("2027-01-05T01:15:00Z", anchor) == 17
    assert rules.week_for_kickoff("2027-01-10T18:00:00Z", anchor) == 18


def test_an_explicit_anchor_time_overrides_the_default_rollover():
    assert rules.week_for_kickoff("2026-09-15T00:15:00Z", "2026-09-08T00:00:00Z") == 2
    assert rules.week_for_kickoff("2026-09-15T00:15:00Z", "2026-09-08") == 1


def test_spreads_render_the_way_a_board_shows_them():
    assert rules.format_spread(3) == "+3"
    assert rules.format_spread(-3.5) == "-3.5"
    assert rules.format_spread(0) == "PK"
    assert rules.format_spread(-10) == "-10"


def test_current_week_tracks_the_calendar():
    anchor = "2026-09-08"
    def at(ts):
        return rules.current_week(anchor, datetime.fromisoformat(ts.replace("Z", "+00:00")))

    assert at("2026-09-10T18:00:00Z") == 1    # Thursday opener
    assert at("2026-09-14T23:00:00Z") == 1    # Monday, week 1 still running
    assert at("2026-09-15T13:00:00Z") == 2    # Tuesday, week 2 is up
    assert at("2026-09-22T16:00:00Z") == 3


def test_current_week_is_clamped_to_the_season():
    anchor = "2026-09-08"
    def at(ts):
        return rules.current_week(anchor, datetime.fromisoformat(ts.replace("Z", "+00:00")))

    assert at("2026-07-04T12:00:00Z") == 1    # long before kickoff
    assert at("2027-02-15T12:00:00Z") == 18   # after the finale
    assert at("2030-01-01T12:00:00Z") == 18


def test_current_week_shares_its_boundary_with_game_bucketing():
    """The week the site opens on must be the week those games are filed under."""
    anchor = "2026-09-08"
    kickoff = "2026-09-15T00:15:00Z"          # week 1's Monday nighter
    moment = rules.parse_iso(kickoff)
    assert rules.current_week(anchor, moment) == rules.week_for_kickoff(kickoff, anchor)


def test_current_week_defaults_to_the_real_clock():
    week = rules.current_week("2026-09-08")
    assert 1 <= week <= rules.TOTAL_WEEKS


FAV_WON_BY_6 = {"home_team": "A", "away_team": "B",
                "home_score": 26, "away_score": 20, "completed": 1}
FAV_LOST_BY_3 = {"home_team": "A", "away_team": "B",
                 "home_score": 17, "away_score": 20, "completed": 1}


def test_cover_margin_matches_the_league_definition():
    """A -3 favorite winning by 6 is +3; losing by 3 off that same -3 is -6."""
    assert rules.cover_margin("A", -3, FAV_WON_BY_6) == 3
    assert rules.cover_margin("A", -3, FAV_LOST_BY_3) == -6


def test_the_two_sides_of_a_game_are_mirror_images():
    for spread in (-3, -7.5, 0):
        ours = rules.cover_margin("A", spread, FAV_WON_BY_6)
        theirs = rules.cover_margin("B", -spread, FAV_WON_BY_6)
        assert ours == -theirs


def test_a_push_is_exactly_zero():
    game = {**FAV_WON_BY_6, "home_score": 24, "away_score": 21}
    assert rules.cover_margin("A", -3, game) == 0
    assert rules.grade_pick("A", -3, game) == "PUSH"


def test_margin_and_result_can_never_disagree():
    game = {**FAV_WON_BY_6}
    for spread in (-10, -6.5, -6, -5.5, 0, 3, 7.5):
        margin = rules.cover_margin("A", spread, game)
        result = rules.grade_pick("A", spread, game)
        assert result == ("WIN" if margin > 0 else "LOSS" if margin < 0 else "PUSH")


def test_an_ungraded_game_has_no_margin():
    assert rules.cover_margin("A", -3, {**FAV_WON_BY_6, "completed": 0}) is None
    assert rules.cover_margin(
        "A", -3, {**FAV_WON_BY_6, "home_score": None, "away_score": None, "completed": 0}
    ) is None


def test_a_margin_always_shows_its_sign():
    assert rules.format_margin(3) == "+3"
    assert rules.format_margin(-6) == "-6"
    assert rules.format_margin(0) == "0"
    assert rules.format_margin(2.5) == "+2.5"
    assert rules.format_margin(None) == "—"


def test_a_big_loss_hurts_more_than_a_narrow_one():
    """The point of tracking it: two 0-1 weeks are not the same week."""
    blowout = {"home_team": "A", "away_team": "B",
               "home_score": 3, "away_score": 38, "completed": 1}
    narrow = {"home_team": "A", "away_team": "B",
              "home_score": 20, "away_score": 24, "completed": 1}
    assert rules.grade_pick("A", -3, blowout) == rules.grade_pick("A", -3, narrow) == "LOSS"
    assert rules.cover_margin("A", -3, blowout) < rules.cover_margin("A", -3, narrow)
