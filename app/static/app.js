"use strict";

const state = { week: null, view: "draft", board: null };

const $ = (sel) => document.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

/* ----------------------------------------------------------------- api */

async function api(path, options = {}) {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = body.detail ?? {};
    const err = new Error(
      typeof detail === "string" ? detail : detail.message || `Request failed (${res.status})`
    );
    err.code = typeof detail === "object" ? detail.code : undefined;
    err.outOfCredits = typeof detail === "object" ? detail.out_of_credits : false;
    throw err;
  }
  return body;
}

let alertTimer;
function say(message, kind = "info", sticky = false) {
  const box = $("#alert");
  box.textContent = message;
  box.className = `alert ${kind === "info" ? "" : kind}`;
  box.hidden = false;
  clearTimeout(alertTimer);
  if (!sticky) alertTimer = setTimeout(() => (box.hidden = true), 6000);
}
const clearAlert = () => ($("#alert").hidden = true);

/* -------------------------------------------------------------- format */

const KICKOFF_FMT = new Intl.DateTimeFormat(undefined, {
  weekday: "short", hour: "numeric", minute: "2-digit",
});

const kickoff = (iso) => (iso ? KICKOFF_FMT.format(new Date(iso)) : "TBD");

function ago(seconds) {
  if (seconds < 60) return "just now";
  const mins = Math.floor(seconds / 60);
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.floor(mins / 60);
  return hours < 24 ? `${hours}h ago` : `${Math.floor(hours / 24)}d ago`;
}

const stamp = (iso) =>
  new Date(iso).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });

/* ---------------------------------------------------------------- shell */

function buildWeekPicker() {
  const select = $("#week");
  for (let w = 1; w <= 18; w++) select.append(new Option(`Week ${w}`, w));
  select.addEventListener("change", () => {
    state.week = Number(select.value);
    render();
  });
  $("#week-prev").addEventListener("click", () => shiftWeek(-1));
  $("#week-next").addEventListener("click", () => shiftWeek(1));
}

function shiftWeek(delta) {
  const next = Math.min(18, Math.max(1, state.week + delta));
  if (next === state.week) return;
  state.week = next;
  $("#week").value = next;
  render();
}

function buildTabs() {
  document.querySelectorAll(".tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t === tab));
      state.view = tab.dataset.view;
      document.querySelectorAll(".view").forEach((v) => {
        v.hidden = v.id !== `view-${state.view}`;
      });
      render();
    });
  });
}

async function render() {
  clearAlert();
  // No week on the first load: the server answers with the live one.
  state.board = await api(
    state.week === null ? "/api/board" : `/api/board?week=${state.week}`
  );
  state.week = state.board.week;
  $("#week").value = state.week;
  markLiveWeek(state.board.current_week);
  renderCredits(state.board.credits);
  renderTexting(state.board.texting);
  if (state.view === "draft") renderDraft();
  if (state.view === "results") await renderResults();
  if (state.view === "standings") await renderStandings();
  if (state.view === "analytics") await renderAnalytics();
  if (state.view === "ledger") await renderLedger();
}

// Label the live week in the picker, so a week you navigated to is never
// mistaken for the one the season is actually in.
function markLiveWeek(live) {
  [...$("#week").options].forEach((option) => {
    const week = Number(option.value);
    option.textContent = week === live ? `Week ${week} · now` : `Week ${week}`;
  });
  const away = $("#back-to-live");
  away.hidden = state.week === live;
  away.textContent = `Back to week ${live}`;
  away.dataset.week = live;
}

function renderCredits(credits) {
  const box = $("#credits");
  if (!credits.key_configured) {
    box.className = "credits low";
    box.innerHTML = "<strong>No API key</strong><br>lines must be entered by hand";
    return;
  }
  if (credits.remaining == null) {
    box.className = "credits";
    box.innerHTML = "<strong>500</strong> credits/mo<br>none spent yet";
    return;
  }
  box.className = `credits${credits.remaining < 50 ? " low" : ""}`;
  box.innerHTML = `<strong>${credits.remaining}</strong> API credits left<br>as of ${stamp(credits.as_of)}`;
}

// Whether the person on the clock actually gets a text, without naming numbers.
function renderTexting(texting) {
  const box = $("#texting");
  if (!texting) return;
  const ready = texting.configured && texting.players_with_numbers.length === 3;
  // Configured is not the same as working -- say so when the last one bounced,
  // rather than showing "on" while nothing is arriving.
  const failing = texting.last?.status === "failed";
  box.className = `texting${failing ? " bad" : ready ? " on" : ""}`;
  box.title = failing
    ? "The last text failed. Check the notifications table for the reason."
    : "Whether the player on the clock gets a text";
  box.textContent = failing
    ? "texts failing"
    : ready
      ? "texts on"
      : texting.configured
        ? `texts: ${texting.players_with_numbers.length}/3 numbers set`
        : "texts off";
}

/* ---------------------------------------------------------------- draft */

function renderDraft() {
  const board = state.board;
  renderFreshness(board);

  const clock = $("#clock");
  if (board.on_clock) {
    clock.className = "clock";
    clock.innerHTML =
      `<div class="who">${board.on_clock.player} is on the clock</div>` +
      `<div class="sub">Pick ${board.on_clock.slot} of ${board.slots_per_week} &middot; week ${board.week}</div>`;
  } else {
    clock.className = "clock done";
    clock.innerHTML =
      `<div class="who">Week ${board.week} is drafted</div>` +
      `<div class="sub">All ${board.slots_per_week} picks are in. Grade them on the Results tab.</div>`;
  }

  const strip = $("#order-strip");
  strip.replaceChildren();
  const bySlot = new Map(board.picks.map((p) => [p.slot, p]));
  board.draft_order.forEach((player, index) => {
    const slot = index + 1;
    const pick = bySlot.get(slot);
    const item = el("li");
    if (board.on_clock?.slot === slot) item.classList.add("current");
    item.append(el("div", "slot", `PICK ${slot}`), el("div", "name", player));
    if (pick) {
      const made = el("div", "made");
      const button = el("button", null, pick.label);
      button.title = "Edit this pick";
      button.addEventListener("click", () => openEdit(pick));
      made.append(button);
      item.append(made);
      if (pick.line_source === "override") item.append(el("div", "flag", "line overridden"));
    } else {
      item.append(el("div", "empty", "—"));
    }
    strip.append(item);
  });

  const list = $("#games");
  list.replaceChildren();
  if (!board.games.length) {
    list.append(el("p", "hint", "No games yet for this week. Refresh the lines, or add a game by hand."));
    return;
  }
  board.games.forEach((game) => list.append(gameRow(game, board)));
}

function renderFreshness(board) {
  const box = $("#freshness");
  const snap = board.snapshot;
  if (!snap) {
    box.className = "freshness";
    box.innerHTML = `<span class="dot"></span>No lines pulled for week ${board.week} yet.`;
    return;
  }
  box.className = `freshness ${snap.fresh ? "fresh" : "stale"}`;
  const source = snap.book ? ` from ${snap.book}` : "";
  box.innerHTML = snap.fresh
    ? `<span class="dot"></span>Lines${source} pulled ${ago(snap.age_seconds)} — good for picking.`
    : `<span class="dot"></span>Lines are ${ago(snap.age_seconds)} — older than ${board.line_max_age_minutes} min. Refresh before picking.`;
}

function gameRow(game, board) {
  const row = el("div", "game");
  const when = el("div", "kickoff", kickoff(game.commence_time));
  if (game.completed) {
    when.append(
      el("span", "final", `Final ${game.away_team} ${game.away_score} — ${game.home_team} ${game.home_score}`)
    );
  }
  row.append(when);

  // A hand-added game with nothing riding on it can be taken back off.
  if (game.removable && !game.event_id) {
    const remove = el("button", "remove-game", "×");
    remove.title = "Remove this game from the board";
    remove.setAttribute("aria-label", `Remove ${game.away_team} at ${game.home_team}`);
    remove.addEventListener("click", async () => {
      try {
        await api(`/api/games/${game.id}`, { method: "DELETE" });
        await render();
        say(`Removed ${game.away_team} @ ${game.home_team} from the board.`, "ok");
      } catch (err) {
        say(err.message, "error", true);
      }
    });
    when.append(remove);
  }

  game.sides.forEach((side) => {
    const noLine = side.spread === null;
    const button = el("button", "side");
    const label = el("span");
    label.append(el("span", "team", side.team));
    if (side.reason) label.append(el("span", "why", ` ${side.reason}`));
    button.append(label, el("span", "num", side.display ?? "enter line"));

    // A side with no line can still be taken with a hand-entered number.
    const pickable = side.available || (noLine && !side.reason?.startsWith("Taken"));
    button.disabled = !pickable || !board.on_clock;
    if (side.reason?.startsWith("Taken")) button.classList.add("taken");
    if (pickable && board.on_clock) {
      button.addEventListener("click", () => openPick(game, side, noLine));
    }
    row.append(button);
  });
  return row;
}

/* -------------------------------------------------------- spread input */
/* A mobile decimal keypad has no minus key, so the sign is its own control
   and the number field only ever holds a magnitude. The preview spells the
   whole line out, since the sign is what decides the bet. */

function spreadControl(root, teamOf) {
  const buttons = [...root.querySelectorAll(".sign-btn")];
  const magnitude = root.querySelector(".magnitude");
  const preview = root.querySelector(".spread-preview");

  const signed = () => {
    const chosen = buttons.find((b) => b.getAttribute("aria-pressed") === "true");
    const points = Number(magnitude.value);
    if (!chosen || magnitude.value === "" || Number.isNaN(points)) return null;
    // A pick'em has no side to it, so don't let the sign make 0 look signed.
    return points === 0 ? 0 : points * Number(chosen.dataset.sign);
  };

  const paint = () => {
    const value = signed();
    preview.textContent =
      value === null ? "" : `${teamOf()} ${formatSpread(value)}`;
    preview.classList.toggle("set", value !== null);
  };

  buttons.forEach((button) => {
    button.addEventListener("click", () => {
      buttons.forEach((b) => b.setAttribute("aria-pressed", String(b === button)));
      paint();
    });
  });
  magnitude.addEventListener("input", paint);

  return {
    value: signed,
    repaint: paint,
    set(spread) {
      const sign = spread < 0 ? "-1" : "1";
      buttons.forEach((b) =>
        b.setAttribute("aria-pressed", String(b.dataset.sign === sign))
      );
      magnitude.value = spread === null || spread === undefined ? "" : Math.abs(spread);
      paint();
    },
    clear() {
      buttons.forEach((b) => b.setAttribute("aria-pressed", "false"));
      magnitude.value = "";
      paint();
    },
    focus: () => magnitude.focus(),
  };
}

// Mirrors rules.format_spread on the server.
function formatSpread(spread) {
  if (spread === 0) return "PK";
  return `${spread > 0 ? "+" : "-"}${Math.abs(spread)}`;
}

/* ----------------------------------------------------------- pick flow */

let pending = null;
let pickSpread = null;

function openPick(game, side, forceOverride) {
  pending = { game, side, override: forceOverride };
  const board = state.board;
  $("#pick-title").textContent = `${board.on_clock.player} takes ${side.team}`;
  $("#pick-detail").textContent =
    `${game.away_team} @ ${game.home_team} · ` +
    (forceOverride ? "no line on file — enter it below" : `${side.team} ${side.display}`);

  const fields = $("#override-fields");
  $("#pick-form").reset();
  pickSpread ??= spreadControl(fields, () => pending?.side.team ?? "");

  // With no line on file the hand-entry is the only way through, so it starts
  // open. With a line it stays one tap away -- the book can be wrong, or the
  // three of you may have agreed on a different number.
  fields.hidden = !forceOverride;
  $("#override-toggle").hidden = forceOverride;
  fields.querySelector("[name=override_reason]").required = !!forceOverride;

  if (forceOverride) {
    pickSpread.clear();
  } else {
    // Seed with the book's number so a tweak is an edit, not a re-type.
    pickSpread.set(side.spread);
  }

  $("#pick-dialog").showModal();
  if (forceOverride) pickSpread.focus();
}

$("#override-toggle").addEventListener("click", () => {
  if (!pending) return;
  pending.override = true;
  const fields = $("#override-fields");
  fields.hidden = false;
  fields.querySelector("[name=override_reason]").required = true;
  $("#override-toggle").hidden = true;
  $("#pick-detail").textContent =
    `${pending.game.away_team} @ ${pending.game.home_team} · entering the line by hand`;
  pickSpread.focus();
});

$("#pick-form").addEventListener("submit", async (event) => {
  if (event.submitter?.value !== "confirm" || !pending) return;
  const data = new FormData(event.target);
  const payload = {
    week: state.week,
    player: state.board.on_clock.player,
    game_id: pending.game.id,
    team: pending.side.team,
  };
  if (pending.override) {
    const spread = pickSpread.value();
    if (spread === null) {
      say("Pick a side of the line and enter the points.", "error", true);
      pending = null;
      return;
    }
    payload.override_spread = spread;
    payload.override_reason = data.get("override_reason");
  }
  try {
    const made = await api("/api/picks", { method: "POST", body: JSON.stringify(payload) });
    say(`Pick ${made.slot}: ${payload.player} has ${made.display}.`, "ok");
    await render();
  } catch (err) {
    if (err.code === "STALE_LINES" || err.code === "NO_LINES") {
      say(`${err.message} Pulling a fresh slate now…`, "error", true);
      await refreshLines();
    } else {
      say(err.message, "error", true);
      await render();
    }
  } finally {
    pending = null;
  }
});

/* ----------------------------------------------------------- edit flow */

let editing = null;
let editSpread = null;

function openEdit(pick) {
  editing = pick;
  $("#edit-detail").textContent =
    `Pick ${pick.slot}, week ${pick.week} — ${pick.player} · ${pick.away_team} @ ${pick.home_team}`;
  const form = $("#edit-form");
  form.reset();

  const teams = form.querySelector("[name=team]");
  teams.replaceChildren();
  [pick.away_team, pick.home_team].forEach((t) => teams.append(new Option(t, t)));
  teams.value = pick.team;

  const actor = form.querySelector("[name=actor]");
  actor.replaceChildren();
  state.board.players.forEach((p) => actor.append(new Option(p, p)));
  actor.value = pick.player;

  if (!editSpread) {
    editSpread = spreadControl(
      form.querySelector(".spread-field"),
      () => form.querySelector("[name=team]").value
    );
    // Flipping the side changes what the line reads as. Bound once, with the
    // control, so reopening the dialog doesn't stack listeners.
    teams.addEventListener("change", editSpread.repaint);
  }
  editSpread.set(pick.spread);
  $("#edit-delete").hidden = pick.slot !== Math.max(...state.board.picks.map((p) => p.slot));
  $("#edit-dialog").showModal();
}

$("#edit-form").addEventListener("submit", async (event) => {
  const action = event.submitter?.value;
  if (!editing || action === "cancel") return;
  const data = new FormData(event.target);
  const actor = data.get("actor");
  const reason = data.get("reason");

  try {
    if (action === "delete") {
      await api(`/api/picks/${editing.id}`, {
        method: "DELETE",
        body: JSON.stringify({ actor, reason }),
      });
      say(`Removed ${editing.label}. It stays in the ledger.`, "ok");
    } else {
      const spread = editSpread.value();
      if (spread === null) {
        say("Pick a side of the line and enter the points.", "error", true);
        editing = null;
        return;
      }
      const body = { team: data.get("team"), spread, actor, reason };
      const out = await api(`/api/picks/${editing.id}`, { method: "PATCH", body: JSON.stringify(body) });
      say(
        out.changed.length
          ? `Edited ${out.changed.join(" and ")} — logged to the ledger.`
          : "Nothing changed.",
        "ok"
      );
    }
    await render();
  } catch (err) {
    say(err.message, "error", true);
  } finally {
    editing = null;
  }
});

/* -------------------------------------------------------------- lines */

async function refreshLines() {
  const button = $("#refresh-lines");
  button.disabled = true;
  button.textContent = "Pulling…";
  try {
    const out = await api(`/api/lines/refresh?week=${state.week}`, { method: "POST" });
    await render();
    say(
      `${out.games_with_lines} game${out.games_with_lines === 1 ? "" : "s"} on the week ${state.week} board` +
        `${out.book ? ` (${out.book})` : ""}. ${out.credits.remaining ?? "?"} credits left.`,
      "ok"
    );
  } catch (err) {
    say(
      err.outOfCredits
        ? `${err.message} Add games by hand and enter each line yourself — they'll be flagged as overrides.`
        : err.message,
      "error",
      true
    );
  } finally {
    button.disabled = false;
    button.textContent = "Refresh lines";
  }
}

$("#refresh-lines").addEventListener("click", refreshLines);

$("#manual-game-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const data = new FormData(event.target);
  try {
    await api("/api/games/manual", {
      method: "POST",
      body: JSON.stringify({
        week: state.week,
        home_team: data.get("home_team").trim(),
        away_team: data.get("away_team").trim(),
      }),
    });
    event.target.reset();
    await render();
    say("Game added. Pick a side and enter the line by hand.", "ok");
  } catch (err) {
    say(err.message, "error", true);
  }
});

/* ------------------------------------------------------------ results */

async function renderResults() {
  const board = state.board;
  const list = $("#results");
  list.replaceChildren();

  const picked = board.games.filter((g) => board.picks.some((p) => p.game_id === g.id));
  if (!picked.length) {
    list.append(el("p", "hint", `No picks to grade in week ${board.week} yet.`));
    return;
  }

  // Differential: points a pick beat its spread by. Graded games only, so an
  // ungraded week shows nothing rather than a misleading zero.
  const season = await api("/api/standings");
  list.append(marginPanel(board, season));

  picked.forEach((game) => {
    const row = el("div", "result-row");
    const left = el("div");
    left.append(el("div", "matchup", `${game.away_team} @ ${game.home_team}`));
    const picks = el("div", "picks");
    board.picks
      .filter((p) => p.game_id === game.id)
      .forEach((p, i) => {
        if (i) picks.append(document.createTextNode(" · "));
        picks.append(document.createTextNode(`${p.player}: ${p.label} `));
        if (p.result) picks.append(el("span", `pill ${p.result}`, p.result));
        if (p.margin !== null && p.margin !== undefined) {
          picks.append(el("span", `diff ${marginTone(p.margin)}`, ` ${p.margin_display}`));
        }
      });
    left.append(picks);
    row.append(left);

    const form = el("form", "score-form");
    const away = el("input");
    const home = el("input");
    [away, home].forEach((input) => {
      input.type = "number";
      input.min = "0";
      input.required = true;
    });
    away.value = game.away_score ?? "";
    home.value = game.home_score ?? "";
    away.title = `${game.away_team} points`;
    home.title = `${game.home_team} points`;
    const save = el("button", "secondary", game.completed ? "Update" : "Save");
    form.append(away, el("span", null, "—"), home, save);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        await api("/api/scores/manual", {
          method: "POST",
          body: JSON.stringify({
            game_id: game.id,
            away_score: Number(away.value),
            home_score: Number(home.value),
          }),
        });
        await render();
        say("Final recorded and picks graded.", "ok");
      } catch (err) {
        say(err.message, "error", true);
      }
    });
    row.append(form);
    list.append(row);
  });
}

const marginTone = (margin) => (margin > 0 ? "up" : margin < 0 ? "down" : "level");

// This week's differential per player, and the season running total through
// this week -- the figure the league actually argues about.
function marginPanel(board, season) {
  const panel = el("div", "margin-panel");
  panel.append(el("h2", "section-title", "Point differential"));

  const weekly = season.week_margin?.[board.week] ?? {};
  const graded = season.running_margin ?? {};
  // Through this week, not the latest week, so an older week reads correctly.
  const throughWeek = Object.keys(graded)
    .map(Number)
    .filter((w) => w <= board.week)
    .sort((a, b) => a - b)
    .pop();
  const running = throughWeek ? graded[throughWeek] : null;

  const table = el("table", "margin-table");
  table.innerHTML =
    `<thead><tr><th>Player</th><th class="num">Week ${board.week}</th>` +
    `<th class="num">Season${throughWeek && throughWeek !== board.week
      ? ` (thru wk ${throughWeek})` : ""}</th></tr></thead>`;
  const body = el("tbody");

  board.players.forEach((player) => {
    const week = weekly[player];
    const total = running?.[player];
    const tr = el("tr");
    tr.append(el("td", null, player));
    const weekCell = el("td", `num diff ${week === undefined ? "" : marginTone(week)}`,
      week === undefined ? "—" : formatSpreadSigned(week));
    const totalCell = el("td", `num diff ${total === undefined || total === null
      ? "" : marginTone(total)}`,
      total === undefined || total === null ? "—" : formatSpreadSigned(total));
    tr.append(weekCell, totalCell);
    body.append(tr);
  });
  table.append(body);
  panel.append(table);

  if (!Object.keys(weekly).length) {
    panel.append(el("p", "hint", "Nothing graded this week yet — enter finals below."));
  }
  return panel;
}

// Mirrors rules.format_margin: a differential always carries its sign.
const formatSpreadSigned = (value) =>
  value > 0 ? `+${+value.toFixed(1)}` : `${+value.toFixed(1)}`;

$("#grade-week").addEventListener("click", async (event) => {
  event.target.disabled = true;
  try {
    const out = await api("/api/scores/refresh?days_from=3", { method: "POST" });
    await render();
    say(
      out.games_updated
        ? `Graded ${out.games_updated} game${out.games_updated === 1 ? "" : "s"} from the API.`
        : "No new finals from the API. Type in any that are missing.",
      "ok"
    );
  } catch (err) {
    say(
      err.outOfCredits ? `${err.message} Enter the finals by hand below.` : err.message,
      "error",
      true
    );
  } finally {
    event.target.disabled = false;
  }
});

/* ---------------------------------------------------------- standings */

async function renderStandings() {
  const { standings, weekly } = await api("/api/standings");
  const box = $("#standings");
  box.replaceChildren();

  const table = el("table");
  table.innerHTML =
    "<thead><tr><th>Player</th><th>Record</th><th class='num'>W</th><th class='num'>L</th>" +
    "<th class='num'>Push</th><th class='num'>Pending</th><th class='num'>Win %</th>" +
    "<th class='num' title='Points beaten the spread by, all season'>Diff</th></tr></thead>";
  const body = el("tbody");
  const best = standings[0]?.wins ?? 0;
  standings.forEach((s) => {
    const tr = el("tr");
    if (s.wins === best && best > 0) tr.className = "leader";
    tr.innerHTML =
      `<td>${s.player}</td><td>${s.record}</td><td class="num">${s.wins}</td>` +
      `<td class="num">${s.losses}</td><td class="num">${s.pushes}</td>` +
      `<td class="num">${s.pending}</td>` +
      `<td class="num">${s.win_pct == null ? "—" : s.win_pct.toFixed(3)}</td>` +
      `<td class="num diff ${marginTone(s.margin)}">${s.margin_display}</td>`;
    body.append(tr);
  });
  table.append(body);
  box.append(el("h2", "section-title", "Season standings"), table);

  const weeks = Object.keys(weekly).map(Number).sort((a, b) => a - b);
  if (!weeks.length) return;

  const grid = el("table");
  grid.innerHTML =
    "<thead><tr><th>Week</th>" +
    standings.map((s) => `<th>${s.player}</th>`).join("") +
    "</tr></thead>";
  const gridBody = el("tbody");
  weeks.forEach((week) => {
    const tr = el("tr");
    tr.append(el("td", null, `Week ${week}`));
    standings.forEach((s) => {
      const cell = el("td");
      (weekly[week][s.player] || []).forEach((p, i) => {
        if (i) cell.append(el("br"));
        cell.append(document.createTextNode(`${p.display} `));
        if (p.result) cell.append(el("span", `pill ${p.result}`, p.result));
        if (p.margin !== null && p.margin !== undefined) {
          cell.append(el("span", `diff ${marginTone(p.margin)}`, ` ${p.margin_display}`));
        }
      });
      if (!cell.childNodes.length) cell.textContent = "—";
      tr.append(cell);
    });
    gridBody.append(tr);
  });
  grid.append(gridBody);
  box.append(el("h2", "section-title", "Week by week"), grid);
}

/* ---------------------------------------------------------- analytics */

async function renderAnalytics() {
  const data = await api("/api/analytics");
  const box = $("#analytics");
  box.replaceChildren();

  if (!data.weeks_graded) {
    box.append(el("p", "hint", data.weeks_drafted
      ? "Nothing graded yet — enter some finals on the Results tab."
      : "No picks yet."));
    return;
  }

  box.append(el("p", "hint",
    `Draft position across ${data.weeks_graded} graded week${data.weeks_graded === 1 ? "" : "s"}. ` +
    `The snake rotates, so everyone cycles through the same slot pairs — ` +
    `picks 1 and 6, 2 and 5, 3 and 4 — which makes these comparable.`));

  box.append(el("h2", "section-title", "Time on the clock"));
  box.append(clockTable(data.on_the_clock));

  box.append(el("h2", "section-title", "By draft position"));
  box.append(slotTable(data.by_slot));
  box.append(marginChart(data.by_slot));
  box.append(el("h2", "section-title", "Opening three vs the snake back"));
  box.append(halfTable(data.by_half));
  box.append(el("h2", "section-title", "Each player by position"));
  const scroller = el("div", "table-scroll");
  scroller.append(playerSlotTable(data));
  box.append(scroller);
}

// How long someone sat on the clock before picking. Median, because one
// overnight wait would drag a mean somewhere nobody actually picks.
function clockTable(clock) {
  const wrap = el("div", "clock-panel");
  if (!clock || !clock.handoffs_counted) {
    wrap.append(el("p", "hint", "Not enough picks yet to time anyone."));
    return wrap;
  }

  const table = el("table");
  table.innerHTML =
    "<thead><tr><th>Player</th><th class='num'>Picks</th>" +
    "<th class='num'>Median</th><th class='num'>Fastest</th>" +
    "<th class='num'>Slowest</th></tr></thead>";
  const body = el("tbody");
  clock.players.forEach((p) => {
    const tr = el("tr");
    const slow = el("td", "num");
    slow.textContent = p.slowest;
    if (p.slowest_detail) {
      slow.title = `Week ${p.slowest_detail.week}, pick ${p.slowest_detail.slot}`;
    }
    tr.innerHTML =
      `<td>${p.player}</td><td class="num">${p.picks}</td>` +
      `<td class="num"><strong>${p.median}</strong></td>` +
      `<td class="num">${p.fastest}</td>`;
    tr.append(slow);
    body.append(tr);
  });
  table.append(body);
  // Five columns do not fit a phone; the table scrolls, the page does not.
  const scroller = el("div", "table-scroll");
  scroller.append(table);
  wrap.append(scroller);
  wrap.append(el("p", "chart-note",
    `League median ${clock.league_median} across ${clock.handoffs_counted} handoffs. ` +
    "Counted from the previous pick in that week, so the first pick of a week is " +
    "excluded — nothing put that player on the clock. Pick 4 is excluded too: the " +
    "snake hands it straight back to whoever took pick 3."));
  return wrap;
}

function slotTable(slots) {
  const table = el("table");
  table.innerHTML =
    "<thead><tr><th>Pick</th><th>Record</th><th class='num'>Win %</th>" +
    "<th class='num' title='Total points beaten the spread by'>Diff</th>" +
    "<th class='num' title='Differential per graded pick'>Avg</th></tr></thead>";
  const body = el("tbody");
  slots.forEach((s) => {
    const tr = el("tr");
    tr.innerHTML =
      `<td>Pick ${s.slot}</td><td>${s.record}</td>` +
      `<td class="num">${s.win_pct == null ? "—" : s.win_pct.toFixed(3)}</td>` +
      `<td class="num diff ${marginTone(s.margin)}">${s.margin_display}</td>` +
      `<td class="num diff ${marginTone(s.avg_margin ?? 0)}">${s.avg_margin_display}</td>`;
    body.append(tr);
  });
  table.append(body);
  return table;
}

function halfTable(halves) {
  const names = { first: "Picks 1–3", second: "Picks 4–6" };
  const table = el("table");
  table.innerHTML =
    "<thead><tr><th>Half of the round</th><th>Record</th><th class='num'>Win %</th>" +
    "<th class='num'>Diff</th><th class='num'>Avg</th></tr></thead>";
  const body = el("tbody");
  halves.forEach((h) => {
    const tr = el("tr");
    tr.innerHTML =
      `<td>${names[h.half]}</td><td>${h.record}</td>` +
      `<td class="num">${h.win_pct == null ? "—" : h.win_pct.toFixed(3)}</td>` +
      `<td class="num diff ${marginTone(h.margin)}">${h.margin_display}</td>` +
      `<td class="num diff ${marginTone(h.avg_margin ?? 0)}">${h.avg_margin_display}</td>`;
    body.append(tr);
  });
  table.append(body);
  return table;
}

/* Differential per pick by draft slot.
   Polarity is the job, so this is a diverging form anchored at zero. Direction
   is carried by which side of the baseline a bar sits on and by a signed label
   on every bar -- never by colour alone, which greens and reds cannot do for a
   red-green colourblind reader (the pair measures dE 1.3 under deuteranopia). */
function marginChart(slots) {
  const figure = el("figure", "chart");
  figure.append(el("figcaption", "chart-title", "Differential per pick, by draft position"));

  const values = slots.map((s) => s.avg_margin ?? 0);
  const bound = Math.max(1, ...values.map(Math.abs));

  const plot = el("div", "chart-plot");
  slots.forEach((s) => {
    const value = s.avg_margin ?? 0;
    const row = el("div", "chart-row");
    row.append(el("div", "chart-label", `Pick ${s.slot}`));

    const track = el("div", "chart-track");
    track.append(el("div", "chart-zero"));
    const bar = el("div", `chart-bar ${marginTone(value)}`);
    // Half the track is each polarity; the bar grows out from the centre.
    const extent = (Math.abs(value) / bound) * 50;
    bar.style.width = `${extent}%`;
    if (value < 0) bar.style.right = "50%";
    else bar.style.left = "50%";
    bar.title = `Pick ${s.slot}: ${s.record}, ${s.avg_margin_display} per pick ` +
                `(${s.margin_display} over ${s.graded} graded)`;
    track.append(bar);
    row.append(track);
    row.append(el("div", `chart-value diff ${marginTone(value)}`, s.avg_margin_display));
    plot.append(row);
  });

  figure.append(plot);
  figure.append(el("p", "chart-note",
    "Bars run left of the centre line for under, right for over. The table above carries the same numbers."));
  return figure;
}

function playerSlotTable(data) {
  const table = el("table");
  const heads = Array.from({ length: data.slots }, (_, i) => `<th class="num">Pick ${i + 1}</th>`);
  table.innerHTML = `<thead><tr><th>Player</th>${heads.join("")}</tr></thead>`;
  const body = el("tbody");
  data.players.forEach((player) => {
    const tr = el("tr");
    tr.append(el("td", null, player));
    (data.by_player_slot[player] || []).forEach((cell) => {
      const td = el("td", "num");
      if (!cell.graded) {
        td.textContent = "—";
      } else {
        td.append(document.createTextNode(cell.record + " "));
        td.append(el("span", `diff ${marginTone(cell.margin)}`, cell.margin_display));
      }
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(body);
  return table;
}

/* ------------------------------------------------------------- ledger */

async function renderLedger() {
  const { entries } = await api("/api/ledger");
  const box = $("#ledger");
  box.replaceChildren();
  if (!entries.length) {
    box.append(el("p", "hint", "Nothing recorded yet."));
    return;
  }
  entries.forEach((entry) => {
    const row = el("div", "ledger-entry");
    row.append(el("div", "when", stamp(entry.created_at)));
    const what = el("div", "what");

    const verb = { create: "made a pick", edit: `changed ${entry.field}`, delete: "removed a pick" }[
      entry.action
    ];
    what.append(el("div", "action", `Week ${entry.week} · ${entry.player} — ${verb}`));

    const detail = el("div");
    if (entry.action === "edit") {
      detail.append(
        document.createTextNode(entry.old_value ?? "—"),
        el("span", "arrow", " → "),
        document.createTextNode(entry.new_value ?? "—")
      );
    } else {
      detail.textContent = entry.new_value ?? entry.old_value ?? "";
    }
    what.append(detail);

    const by = entry.actor !== entry.player ? ` (edited by ${entry.actor})` : "";
    if (entry.reason || by) what.append(el("div", "reason", `${entry.reason ?? ""}${by}`));
    row.append(what);
    box.append(row);
  });
}

/* --------------------------------------------------------------- boot */

$("#back-to-live").addEventListener("click", (event) => {
  state.week = Number(event.target.dataset.week);
  render().catch((err) => say(err.message, "error", true));
});

buildWeekPicker();
buildTabs();
render().catch((err) => say(err.message, "error", true));

// Keep the "lines are N minutes old" readout honest without re-fetching.
setInterval(() => {
  if (state.view === "draft" && state.board?.snapshot) {
    state.board.snapshot.age_seconds += 30;
    state.board.snapshot.fresh =
      state.board.snapshot.age_seconds <= state.board.line_max_age_minutes * 60;
    renderFreshness(state.board);
  }
}, 30000);
