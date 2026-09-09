# Pigskin Pick'em

A three-player NFL pick'em league against the spread, for Jimmy, Mack and Anthony.
Eighteen weeks, two picks each per week, drafted in a snake that rotates.

Lightweight on purpose: a FastAPI service over one SQLite file, and a static page
with no build step.

## The rules it enforces

**Snake draft, rotating.** Six picks a week. The leadoff spot rotates weekly and
the back half of the round runs in reverse:

| Week | Order |
| ---- | ----- |
| 1 | Jimmy, Mack, Anthony \| Anthony, Mack, Jimmy |
| 2 | Mack, Anthony, Jimmy \| Jimmy, Anthony, Mack |
| 3 | Anthony, Jimmy, Mack \| Mack, Jimmy, Anthony |
| 4 | back to week 1's order |

Over 18 weeks each player leads off exactly six times. Picks can only be made by
whoever is on the clock.

**One team per week.** Once Jimmy takes the Bears in week 5, the Bears are closed
to everyone else that week. They open back up in week 6.

**The far side stays open.** Someone else may take the Packers against Jimmy's
Bears. But no single player can hold both sides of one game.

**Lines must be fresh.** A pick is recorded against a specific snapshot of the
board, and that snapshot has to be under an hour old. Older than that and the
server refuses the pick with `STALE_LINES`; the UI pulls a new slate and you pick
against the new number. The spread stored on a pick is the one that was showing
when it was made — later line movement never rewrites it.

**Overrides are flagged.** The free API tier allows 500 calls a month. When it's
exhausted (or a matchup isn't on the board), add the game by hand and type the
line in yourself. It requires a reason, and the pick is permanently marked
`line_source = 'override'`.

**Edits are never silent.** Picks can be changed, but every create, field-level
edit and removal is appended to `pick_ledger` with who did it, what changed, and
why. The ledger is append-only; nothing in it is ever updated or deleted. Edits
are re-validated, so an edit can't be used to grab a team someone already holds.
Only the most recent pick in a week can be removed, which keeps the snake intact.

**Grading.** `margin + spread > 0` wins, `< 0` loses, exactly `0` pushes. Pushes
are tracked separately and don't count toward win percentage.

## Running it

```bash
pip install -r requirements.txt
cp .env.example .env          # then paste in your key
export $(grep -v '^#' .env | xargs)
uvicorn app.main:app --reload
```

Open http://localhost:8000. The SQLite file is created on first run.

### The API key

Sign up free at [the-odds-api.com](https://the-odds-api.com) and put the key in
`.env` as `ODDS_API_KEY`. Free tier is 500 credits a month.

Budgeting them: one line refresh costs 1 credit, one score pull costs 1. At
18 weeks that's 36 credits if you refresh once and grade once per week — so
there's plenty of headroom to re-refresh when a slate goes stale mid-draft.
Remaining credits are read from the API's own `x-requests-remaining` header and
shown in the top right.

Everything works without a key; every line just has to be entered by hand and
gets flagged as an override.

### Season dates

`SEASON_WEEK1_ANCHOR` is the Tuesday that opens week 1 — games are bucketed into
weeks in 7-day blocks from it, so a Thursday-through-Monday slate lands in one
week. **Check this before week 1**: the default (`2026-09-08`) is a guess at the
2026 opener. If your slate shows up under the wrong week, move the anchor.

## Deploying to Railway

Railway builds this with Nixpacks off `requirements.txt`; `railway.toml` supplies
the start command and a health check at `/api/health`.

**Add a volume first.** Railway container disks are ephemeral — without one, the
whole season is wiped on every redeploy.

1. Create the service from this repo.
2. Add a **Volume** to the service, mount path `/data`.
3. Set the service variables:

   | Variable | Value |
   | -------- | ----- |
   | `PIGSKIN_DB` | `/data/pigskin.db` |
   | `ODDS_API_KEY` | your key |
   | `SEASON_WEEK1_ANCHOR` | the Tuesday opening week 1 |

4. Deploy, then generate a public domain under Settings → Networking.

`$PORT` is injected by Railway and read by the start command; nothing else needs
configuring. Keep it at one replica — SQLite takes a single writer, and three
players don't need more.

To back up a season, `railway run sqlite3 /data/pigskin.db .dump > backup.sql`.

**Note on access.** There's no login, as discussed. On a public Railway URL that
means anyone with the link can make and edit picks — every change is attributed
and ledgered, but nothing stops it. Fine if you keep the URL between the three of
you; if you'd rather not rely on that, a shared passcode is a small addition.

## Layout

```
app/rules.py    league rules as pure functions -- draft order, legality, grading
app/db.py       SQLite schema
app/odds.py     The Odds API client and credit tracking
app/main.py     JSON API
app/static/     the UI (plain HTML/CSS/JS, no build)
tests/          57 tests
```

`rules.py` has no database or network access, so the league rules are testable on
their own and there's one place to change them.

## API

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/api/board?week=N` | Draft order, picks, open board, line freshness |
| `POST` | `/api/lines/refresh?week=N` | Pull a fresh slate (1 credit) |
| `POST` | `/api/games/manual` | Add a game by hand |
| `POST` | `/api/picks` | Make a pick |
| `PATCH` | `/api/picks/{id}` | Edit a pick (ledgered) |
| `DELETE` | `/api/picks/{id}` | Remove the week's last pick (ledgered) |
| `POST` | `/api/scores/refresh` | Pull finals and grade (1 credit) |
| `POST` | `/api/scores/manual` | Enter a final by hand |
| `GET` | `/api/standings` | Records and the week-by-week grid |
| `GET` | `/api/ledger?week=N` | Audit trail |

Rejected picks return `409` with a machine-readable code — `OUT_OF_TURN`,
`TEAM_TAKEN`, `BOTH_SIDES`, `STALE_LINES`, `WEEK_FULL` — plus a message written
for the person reading it.

## Tests

```bash
python -m pytest tests/ -q
```
