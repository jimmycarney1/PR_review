import pytest
from fastapi.testclient import TestClient

from app import db as db_module
from app import main, odds


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db_module, "DB_PATH", str(tmp_path / "test.db"))
    db_module.init_db()
    with TestClient(main.app) as c:
        yield c


def slate(spread=-3.0, commence="2026-09-10T00:20:00Z"):
    """Two NFL games in the shape fetch_spreads returns."""
    return [
        {
            "event_id": "evt-gb-chi",
            "commence_time": commence,
            "home_team": "Green Bay Packers",
            "away_team": "Chicago Bears",
            "book": "DraftKings",
            "outcomes": [
                {"team": "Green Bay Packers", "spread": spread, "price": -110},
                {"team": "Chicago Bears", "spread": -spread, "price": -110},
            ],
        },
        {
            "event_id": "evt-kc-buf",
            "commence_time": commence,
            "home_team": "Kansas City Chiefs",
            "away_team": "Buffalo Bills",
            "book": "DraftKings",
            "outcomes": [
                {"team": "Kansas City Chiefs", "spread": -1.5, "price": -110},
                {"team": "Buffalo Bills", "spread": 1.5, "price": -110},
            ],
        },
    ]


@pytest.fixture
def stub_odds(monkeypatch):
    """Replace the network call with a fixed slate; no credits are spent."""
    def _install(games=None, headers=None):
        payload = slate() if games is None else games
        monkeypatch.setattr(
            odds, "fetch_spreads",
            lambda conn: (payload, headers or {"remaining": 480, "used": 20}),
        )
    _install()
    return _install
