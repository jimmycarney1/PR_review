"""League rules. Pure functions -- no database, no network, so they're easy to test."""
from datetime import datetime, timedelta, timezone

PLAYERS = ["Jimmy", "Mack", "Anthony"]
PICKS_PER_PLAYER = 2
SLOTS_PER_WEEK = len(PLAYERS) * PICKS_PER_PLAYER   # 6
TOTAL_WEEKS = 18

# A pick must be made within this long of the line being pulled from the book.
LINE_MAX_AGE = timedelta(hours=1)


def draft_order(week: int) -> list[str]:
    """Snake order for a week.

    The leadoff spot rotates every week and the back half of the round runs in
    reverse, so week 1 is Jimmy, Mack, Anthony | Anthony, Mack, Jimmy and week 2
    is Mack, Anthony, Jimmy | Jimmy, Anthony, Mack.
    """
    if not 1 <= week <= TOTAL_WEEKS:
        raise ValueError(f"week must be 1..{TOTAL_WEEKS}, got {week}")
    rot = (week - 1) % len(PLAYERS)
    first_half = PLAYERS[rot:] + PLAYERS[:rot]
    return first_half + first_half[::-1]


def player_on_clock(week: int, taken_slots: set[int]) -> tuple[int, str] | None:
    """The next open slot in the week and whose it is, or None once the week is full."""
    order = draft_order(week)
    for slot in range(1, SLOTS_PER_WEEK + 1):
        if slot not in taken_slots:
            return slot, order[slot - 1]
    return None


def parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def line_age(fetched_at: str, now: datetime | None = None) -> timedelta:
    return (now or datetime.now(timezone.utc)) - parse_iso(fetched_at)


def line_is_fresh(fetched_at: str, now: datetime | None = None) -> bool:
    return line_age(fetched_at, now) <= LINE_MAX_AGE


def week_for_kickoff(commence_time: str, anchor: str) -> int:
    """Bucket a kickoff into an NFL week as a 7-day block from the week-1 anchor.

    `anchor` is the Tuesday that opens week 1, which keeps a Thursday-to-Monday
    slate inside one bucket.
    """
    start = parse_iso(anchor if "T" in anchor else anchor + "T00:00:00+00:00")
    delta = parse_iso(commence_time) - start
    return max(1, min(TOTAL_WEEKS, delta.days // 7 + 1))


class PickError(Exception):
    """A rejected pick. `code` lets the UI react (e.g. force a line refresh)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_pick(
    *,
    week: int,
    player: str,
    game: dict,
    team: str,
    slot: int,
    on_clock: tuple[int, str] | None,
    existing_picks: list[dict],
    exclude_pick_id: int | None = None,
) -> None:
    """Raise PickError if this pick breaks a league rule.

    `existing_picks` is every pick already recorded for the week; pass
    `exclude_pick_id` when re-validating an edit so the pick doesn't collide
    with itself.
    """
    if player not in PLAYERS:
        raise PickError("UNKNOWN_PLAYER", f"{player} is not in this league.")
    if not 1 <= week <= TOTAL_WEEKS:
        raise PickError("BAD_WEEK", f"Week must be 1..{TOTAL_WEEKS}.")

    sides = {game["home_team"], game["away_team"]}
    if team not in sides:
        raise PickError(
            "TEAM_NOT_IN_GAME",
            f"{team} isn't playing in {game['away_team']} @ {game['home_team']}.",
        )

    others = [p for p in existing_picks if p["id"] != exclude_pick_id]

    # New picks must come from whoever is on the clock. Edits keep their
    # original slot, so they skip this.
    if exclude_pick_id is None:
        if on_clock is None:
            raise PickError("WEEK_FULL", f"Week {week} already has all {SLOTS_PER_WEEK} picks.")
        clock_slot, clock_player = on_clock
        if slot != clock_slot:
            raise PickError("OUT_OF_TURN", f"Pick {clock_slot} is on the clock, not pick {slot}.")
        if player != clock_player:
            raise PickError("OUT_OF_TURN", f"It's {clock_player}'s pick, not {player}'s.")

    # One team per week across the whole league.
    for p in others:
        if p["team"] == team:
            raise PickError(
                "TEAM_TAKEN",
                f"{p['player']} already has {team} this week. "
                f"The other side of that game is still open.",
            )

    # You may take the far side of a game someone else picked, but not both
    # sides of one game yourself.
    for p in others:
        if p["player"] == player and p["game_id"] == game["id"]:
            raise PickError(
                "BOTH_SIDES",
                f"You already have {p['team']} in this game -- you can't have both sides.",
            )


def grade_pick(team: str, spread: float, game: dict) -> str | None:
    """Result of one pick against the spread, or None if the game has no final."""
    home, away = game.get("home_score"), game.get("away_score")
    if home is None or away is None or not game.get("completed"):
        return None
    if team == game["home_team"]:
        margin = home - away
    elif team == game["away_team"]:
        margin = away - home
    else:
        raise ValueError(f"{team} is not in this game")
    adjusted = margin + spread
    if adjusted > 0:
        return "WIN"
    if adjusted < 0:
        return "LOSS"
    return "PUSH"


def format_spread(spread: float) -> str:
    """+3, -3.5, PK -- how a line reads on a board."""
    if spread == 0:
        return "PK"
    body = f"{abs(spread):g}"
    return f"{'+' if spread > 0 else '-'}{body}"
