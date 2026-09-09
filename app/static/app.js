"use strict";

const state = { week: 1, view: "draft", board: null };

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
  select.value = state.week;
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
  state.board = await api(`/api/board?week=${state.week}`);
  renderCredits(state.board.credits);
  if (state.view === "draft") renderDraft();
  if (state.view === "results") renderResults();
  if (state.view === "standings") await renderStandings();
  if (state.view === "ledger") await renderLedger();
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

function renderResults() {
  const board = state.board;
  const list = $("#results");
  list.replaceChildren();

  const picked = board.games.filter((g) => board.picks.some((p) => p.game_id === g.id));
  if (!picked.length) {
    list.append(el("p", "hint", `No picks to grade in week ${board.week} yet.`));
    return;
  }

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
    "<th class='num'>Push</th><th class='num'>Pending</th><th class='num'>Win %</th></tr></thead>";
  const body = el("tbody");
  const best = standings[0]?.wins ?? 0;
  standings.forEach((s) => {
    const tr = el("tr");
    if (s.wins === best && best > 0) tr.className = "leader";
    tr.innerHTML =
      `<td>${s.player}</td><td>${s.record}</td><td class="num">${s.wins}</td>` +
      `<td class="num">${s.losses}</td><td class="num">${s.pushes}</td>` +
      `<td class="num">${s.pending}</td>` +
      `<td class="num">${s.win_pct == null ? "—" : s.win_pct.toFixed(3)}</td>`;
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
      });
      if (!cell.childNodes.length) cell.textContent = "—";
      tr.append(cell);
    });
    gridBody.append(tr);
  });
  grid.append(gridBody);
  box.append(el("h2", "section-title", "Week by week"), grid);
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
