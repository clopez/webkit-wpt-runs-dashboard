"use strict";

const DATA_DIR = "data/";
const DATA_URL = `${DATA_DIR}dashboard.json`;
const LOG_URL = `${DATA_DIR}cron_helper.log.txt`;
const REFRESH_MINUTES = 5;
const STEP = 3;
const INITIAL = { builds: 3, nightly: 4, weekly: 2 };
const GAP_WARNING_DAYS = 14;
const BUILDBOT_ROOT = "https://build.webkit.org";
const WPTFYI_ROOT = "https://wpt.fyi";
const GITHUB_REPO = "https://github.com/web-platform-tests/wpt";
const SOURCE_REPOSITORY = "https://github.com/clopez/webkit-wpt-runs-dashboard";
const TASKCLUSTER_ROOT = "https://community-tc.services.mozilla.com";
// wpt.fyi's abbreviations (wpt.fyi api/README.md, "Status abbreviations").
const STATUS_NAMES = { O: "OK", P: "PASS", F: "FAIL", S: "SKIP", E: "ERROR", N: "NOTRUN", C: "CRASH", T: "TIMEOUT", PF: "PRECONDITION_FAILED" };
const STATE_KIND = { green: "ok", red: "bad", yellow: "wait", grey: "none" };
const SHORT_PORT_NAMES = { wpe: "WPE", gtk: "GTK" };
const DECIDED_WPT_STATES = { uploaded: "on wpt.fyi", failed: "chunks failed", not_uploaded: "not uploaded",
                             suite_never_finished: "suite never finished", decision_failed: "decision task failed" };

const view = {
  shown: { ...INITIAL },
  openDiff: null,
  openNodes: new Set(),
  diffs: new Map(),
  openGapNotes: new Set(),
  logHeight: null,
  logScrolledUp: false,
  logScrollTop: 0,
};
let dashboard = null;
let dashboardError = null;
let logText = null;
let logError = null;
let logLoadedAt = null;
let logFailedAt = null;
let logResizeObserver = null;

const number = new Intl.NumberFormat("en-US");

function element(tag, attributes = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attributes)) {
    if (value === null || value === undefined || value === false) continue;
    if (name === "class") node.className = value;
    else node.setAttribute(name, value === true ? "" : value);
  }
  for (const child of children.flat()) if (child !== null && child !== undefined && child !== false) node.append(child);
  return node;
}

const LINKED_HOSTS = new Set(["github.com", "community-tc.services.mozilla.com", "wpt.fyi", "build.webkit.org"]);

// Links built from API answers must not become javascript:, data: or another site.
function safeHref(href) {
  try {
    const url = new URL(href, location.href);
    const sameOrigin = url.origin === location.origin && (url.protocol === "https:" || url.protocol === "http:");
    if (sameOrigin || (url.protocol === "https:" && LINKED_HOSTS.has(url.hostname))) return url.href;
  } catch (error) {}
  return null;
}

function link(href, text, className, title) {
  const safe = safeHref(href);
  if (!safe) return element("span", { class: className, title }, text);
  return element("a", { href: safe, target: "_blank", rel: "noopener", class: className, title }, text);
}

function plural(count, singular, pluralForm = `${singular}s`) {
  return `${number.format(count)} ${count === 1 ? singular : pluralForm}`;
}

function formatTime(isoTime, { withDate = true } = {}) {
  if (!isoTime) return "";
  const moment = new Date(isoTime);
  const pad = value => String(value).padStart(2, "0");
  const zone = new Intl.DateTimeFormat(undefined, { timeZoneName: "short" }).formatToParts(moment).find(part => part.type === "timeZoneName");
  const time = `${pad(moment.getHours())}:${pad(moment.getMinutes())}${zone ? ` ${zone.value}` : ""}`;
  return withDate ? `${moment.getFullYear()}-${pad(moment.getMonth() + 1)}-${pad(moment.getDate())} ${time}` : time;
}

function formatAge(milliseconds) {
  const minutes = Math.max(0, Math.round(milliseconds / 60000));
  if (minutes < 60) return plural(minutes, "minute");
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return plural(hours, "hour");
  return plural(Math.floor(hours / 24), "day");
}

// Seconds only matter for durations under an hour.
function formatSeconds(seconds) {
  const total = Math.max(0, Math.round(seconds));
  if (total >= 3600) {
    const minutes = Math.round(total / 60);
    return `${Math.floor(minutes / 60)}h ${minutes % 60}m`;
  }
  const minutes = Math.floor(total / 60), rest = total % 60;
  return minutes ? `${minutes}m ${rest}s` : `${rest}s`;
}

function formatDuration(startIso, endIso) {
  return formatSeconds((new Date(endIso) - new Date(startIso)) / 1000);
}

const shortDate = date => date.slice(5);
const utcDate = isoTime => isoTime.slice(0, 10);

function addDays(date, days) {
  const moment = new Date(`${date}T00:00:00Z`);
  moment.setUTCDate(moment.getUTCDate() + days);
  return moment.toISOString().slice(0, 10);
}

function daysBetween(first, second) {
  return Math.round((new Date(`${second}T00:00:00Z`) - new Date(`${first}T00:00:00Z`)) / 86400000);
}

function plusMinus(label, plus, minus) {
  return element("span", {}, `${label} `,
    element("span", { class: plus ? "plus" : "dim" }, `+${number.format(plus)}`), "/",
    element("span", { class: minus ? "minus" : "dim" }, `−${number.format(minus)}`));
}

function notComparedText(item) {
  const tests = item.added_tests + item.removed_tests;
  if (!tests) return null;
  return `Not compared: ${plural(tests, "test")} added or removed (${plural(item.added_subtests + item.removed_subtests, "subtest")}).`;
}

function describeState([passing, total, status]) {
  const name = STATUS_NAMES[status] || status;
  return total ? `${passing}/${total} ${name}` : name;
}

function portByKey(key) {
  return dashboard.ports.find(port => port.key === key);
}

// Theme

function currentTheme() {
  const explicit = document.documentElement.getAttribute("data-theme");
  if (explicit === "dark" || explicit === "light") return explicit;
  return matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

function themeButton() {
  const theme = currentTheme();
  const button = element("button", { type: "button", class: "plain", "aria-label": `Switch to the ${theme === "dark" ? "light" : "dark"} theme` },
    theme === "dark" ? "☀ Light" : "☾ Dark");
  button.addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try { localStorage.setItem("dashboard-theme", next); } catch (error) {}
    render();
  });
  return button;
}

// Header

// GitHub's mark, from its Octicons, drawn here so that nothing loads from another server.
const GITHUB_MARK_PATH = "M8 0c4.42 0 8 3.58 8 8a8.013 8.013 0 0 1-5.45 7.59c-.4.08-.55-.17-.55-.38 0-.27.01-1.13.01-2.2 0-.75-.25-1.23-.54-1.48 1.78-.2 3.65-.88 3.65-3.95 0-.88-.31-1.59-.82-2.15.08-.2.36-1.02-.08-2.12 0 0-.67-.22-2.2.82-.64-.18-1.32-.27-2-.27-.68 0-1.36.09-2 .27-1.53-1.03-2.2-.82-2.2-.82-.44 1.1-.16 1.92-.08 2.12-.51.56-.82 1.28-.82 2.15 0 3.06 1.86 3.75 3.64 3.95-.23.2-.44.55-.51 1.07-.46.21-1.61.55-2.33-.66-.15-.24-.6-.83-1.23-.82-.67.01-.27.38.01.53.34.19.73.9.82 1.13.16.45.68 1.31 2.69.94 0 .67.01 1.3.01 1.49 0 .21-.15.45-.55.38A7.995 7.995 0 0 1 0 8c0-4.42 3.58-8 8-8Z";

function sourceLink() {
  const svgNamespace = "http://www.w3.org/2000/svg";
  const mark = document.createElementNS(svgNamespace, "svg");
  for (const [name, value] of Object.entries({ viewBox: "0 0 16 16", width: "16", height: "16", "aria-hidden": "true" })) mark.setAttribute(name, value);
  const path = document.createElementNS(svgNamespace, "path");
  path.setAttribute("d", GITHUB_MARK_PATH);
  mark.append(path);
  const anchor = link(SOURCE_REPOSITORY, mark, "source-link", "The source code of this dashboard on GitHub");
  anchor.setAttribute("aria-label", "The source code of this dashboard on GitHub");
  return anchor;
}

function header() {
  const generated = element("div", { class: "freshness" });
  const freshness = element("div", { class: "freshness badges" });
  if (dashboard) {
    const age = Date.now() - new Date(dashboard.generated_at);
    generated.append(element("span", {}, "Data from ", element("b", {}, formatTime(dashboard.generated_at)), ` (${formatAge(age)} ago)`));
    generated.append(lastUpdateBadge(age));
    freshness.append(element("span", { class: "last-runs" }, "Last nightlies:", buildsBadge(), wptRunsBadge(dashboard.daily, "nightly", "#nightly")));
    freshness.append(element("span", { class: "last-runs" }, "Last stable WPT:", wptRunsBadge(dashboard.weekly, "stable", "#weekly", null)));
    freshness.append(element("span", { class: "last-runs" }, "Last beta WPT:", wptRunsBadge(dashboard.weekly, "beta", "#weekly", null)));
  }
  return element("header", { class: "app-head" }, element("h1", {}, "WebKit WPT runs (Linux)"), generated, element("div", { class: "head-right" }, sourceLink(), themeButton()), freshness);
}

// The errors and warnings of the script's last run, unless the last update
// failed or is so old that the cron job has probably stopped, which matter more.
function lastUpdateBadge(age) {
  if (dashboard.failed_update)
    return summaryBadge("#log", "bad", `Last update failed at ${formatTime(dashboard.failed_update.at, { withDate: false })}`,
      `The update script stopped three times in a row, the last one with an empty cache: ${dashboard.failed_update.error}`);
  if (age > dashboard.settings.stale_after_hours * 3600000)
    return summaryBadge("#log", "bad", `Last update ${formatAge(age)} ago (cron stopped?)`,
      `The update script runs every 3 hours, so it has probably stopped working. Check the cron job and the log.`);
  const errors = dashboard.errors.length, warnings = dashboard.warnings.length;
  const problems = [errors && plural(errors, "error"), warnings && plural(warnings, "warning")].filter(Boolean);
  return summaryBadge("#log", errors ? "bad" : warnings ? "wait" : "ok",
    problems.length ? `Last update: ${problems.join(", ")}` : "Last update: no errors", "See the update log");
}

function summaryBadge(href, kind, text, title) {
  return element("a", { href, class: `pill ${kind}`, title }, text);
}

// outcomes has one word per port: the passing word, "failed", "not uploaded"
// or "unknown". Only when both ports agree does the badge speak for both.
function outcomeBadge(href, outcomes, { noun, passing, nothingKnown, title }) {
  const words = new Set(outcomes.map(outcome => outcome.word));
  const failed = outcomes.some(outcome => outcome.word !== passing && outcome.word !== "unknown");
  const unknown = words.has("unknown");
  if (unknown && words.size === 1) return summaryBadge(href, "none", nothingKnown, title);
  let text;
  if (words.size === 1) text = noun ? `${noun}s ${outcomes[0].word}` : outcomes[0].word;
  else {
    const named = failed ? outcomes.filter(outcome => outcome.word !== passing) : outcomes;
    const sameWord = named.length === outcomes.length && new Set(named.map(outcome => outcome.word)).size === 1;
    text = sameWord ? (noun ? `${noun}s ${named[0].word}` : named[0].word)
      : named.map((outcome, index) => `${SHORT_PORT_NAMES[outcome.port.key]}${index === 0 && noun ? ` ${noun}` : ""} ${outcome.word}`).join(", ");
  }
  if (failed) return summaryBadge(href, "bad", `✕ ${text}`, title);
  if (unknown) return summaryBadge(href, "none", text, title);
  return summaryBadge(href, "ok", `✓ ${text}`, title);
}

// The latest finished build of each bot; a build still in progress is left out.
function buildsBadge() {
  const results = dashboard.ports.map(port => ({ port, build: (dashboard.builds[port.key]?.builds || []).find(build => build.complete) }));
  const title = results.map(({ port, build }) => build
    ? `${port.label}: build #${build.number} (${build.identifier}) ${build.result}, ${formatTime(build.complete_at)}`
    : `${port.label}: no finished build`).join("\n");
  const outcomes = results.map(({ port, build }) => ({ port, word: !build ? "unknown" : build.passed ? "passed" : "failed" }));
  return outcomeBadge("#builds", outcomes, { noun: "build", passing: "passed", nothingKnown: "no builds", title });
}

// The latest run of a channel for each port whose outcome is known; a run still in progress is left out.
function wptRunsBadge(rows, channel, href, noun = "WPT run") {
  const results = dashboard.ports.map(port => {
    const row = rows.find(candidate => DECIDED_WPT_STATES[candidate.cells[port.key]?.[channel]?.state]);
    return { port, row, cell: row?.cells[port.key][channel] };
  });
  const title = results.map(({ port, row, cell }) => row
    ? `${port.label}: ${channel} of ${row.date}, ${DECIDED_WPT_STATES[cell.state]}`
    : `${port.label}: no finished ${channel} run`).join("\n");
  const outcomes = results.map(({ port, cell }) => ({ port,
    word: !cell ? "unknown" : cell.state === "uploaded" ? "uploaded" : cell.state === "not_uploaded" ? "not uploaded" : "failed" }));
  return outcomeBadge(href, outcomes, { noun, passing: "uploaded", nothingKnown: "no WPT runs", title });
}

// Show more

function moreRow(section, total) {
  const remaining = total - view.shown[section];
  const more = element("button", { type: "button", class: "plain", disabled: remaining <= 0 }, remaining > 0 ? `+ Show ${Math.min(STEP, remaining)} more` : "All shown");
  more.addEventListener("click", () => { view.shown[section] += STEP; render(); });
  const row = element("div", { class: "more-row" }, more, element("span", {}, `${Math.min(view.shown[section], total)} of ${total}`));
  if (view.shown[section] > INITIAL[section]) {
    const fewer = element("button", { type: "button", class: "plain" }, `Show ${INITIAL[section]}`);
    fewer.addEventListener("click", () => { view.shown[section] = INITIAL[section]; render(); });
    row.append(fewer);
  }
  return row;
}

// Each day is one row of its own, so a narrow screen can move its date above
// both cells without shifting the cells of the next days.
function dayRow(left, date, hour, right) {
  return element("div", { class: "day-row" }, left, spine(date, hour), right);
}

function spine(date, hour) {
  return element("div", { class: "spine", title: date }, shortDate(date), hour ? element("small", {}, hour) : null);
}

// Nightly bundles

function buildUrl(builder, number) {
  return `${BUILDBOT_ROOT}/#/builders/${encodeURIComponent(builder)}/builds/${number}`;
}

function buildNode(builder, build) {
  const kind = !build.complete ? "wait" : build.passed ? "ok" : "bad";
  let when;
  if (!build.complete) when = `building since ${formatTime(build.started_at, { withDate: false })}`;
  else if (build.passed) when = `finished ${formatTime(build.complete_at, { withDate: false })} · ${formatDuration(build.started_at, build.complete_at)}`;
  else when = `${build.result} at ${formatTime(build.complete_at, { withDate: false })} · ${formatDuration(build.started_at, build.complete_at)}`;
  const node = element("div", { class: `build ${kind}`, title: build.state_string },
    element("div", { class: "top" }, link(buildUrl(builder, build.number), `#${build.number}`, "headline"), element("span", { class: "rev" }, build.identifier || "")),
    element("span", { class: "when" }, when));
  if (build.failed_steps?.length)
    node.append(element("div", { class: "links" }, build.failed_steps.map(step =>
      link(`${buildUrl(builder, build.number)}/steps/${step.number}/logs/stdio`, `${step.name} log`, "step"))));
  return node;
}

function buildsSection() {
  const byDate = new Map();
  for (const port of dashboard.ports)
    for (const build of dashboard.builds[port.key]?.builds || []) {
      const date = utcDate(build.started_at);
      if (!byDate.has(date)) byDate.set(date, {});
      (byDate.get(date)[port.key] ||= []).push(build);
    }
  const dates = [...byDate.keys()].sort().reverse();
  const grid = element("div", { class: "aligned" });
  for (const date of dates.slice(0, view.shown.builds)) {
    const side = port => element("div", { class: "stack" }, (byDate.get(date)[port.key] || []).map(build => buildNode(dashboard.builds[port.key].builder, build)));
    grid.append(dayRow(side(dashboard.ports[0]), date, null, side(dashboard.ports[1])));
  }
  const stale = dashboard.ports.filter(port => dashboard.builds[port.key]?.stale);
  return element("section", { class: "infra", id: "builds", "aria-label": "Nightly bundles" },
    element("div", { class: "section-title" }, element("h2", {}, "Nightly bundles"),
      element("small", {}, "build.webkit.org · WPT tests each bundle the next night"),
      stale.length ? element("small", { class: "stale-note" }, `Could not refresh the builds of ${stale.map(port => port.label).join(", ")}; showing the last ones`) : null),
    grid, moreRow("builds", dates.length));
}

// WPT cells

function shortChunkName(name) {
  return name.replace(/^wpt-[a-z0-9_]+-[a-z]+-/, "");
}

// While chunks are still running, the times only count the ones that have
// finished, and the wall time runs until this data was generated.
function runTimes(chunks) {
  if (!chunks?.timed_chunks || !chunks.first_started) return null;
  const running = chunks.unfinished > 0;
  const wallSeconds = (new Date(running ? dashboard.generated_at : chunks.last_resolved) - new Date(chunks.first_started)) / 1000;
  const counted = chunks.timed_chunks < chunks.total ? `${chunks.timed_chunks} of ${plural(chunks.total, "chunk")}` : plural(chunks.total, "chunk");
  const slowest = chunks.slowest;
  return element("div", { class: "run-times" },
    element("span", { class: "dim" }, "Task duration:"), element("span", {}, `${formatSeconds(wallSeconds)} (wall time${running ? " so far" : ""})`),
    element("span", { class: "dim" }, "Total chunk time:"), element("span", {}, `${formatSeconds(chunks.run_seconds)} (${counted})`),
    slowest ? element("span", { class: "dim" }, "Slowest chunk:") : null,
    slowest ? element("span", {}, `${formatSeconds(slowest.seconds)} (`, link(slowest.task_url, shortChunkName(slowest.name), null, `${slowest.name} on Taskcluster`), ")") : null);
}

function taskGroupLink(chunks) {
  return chunks?.task_group_url ? link(chunks.task_group_url, "Taskcluster tasks") : null;
}

function githubChecksLink(row) {
  if (!row.suite) return null;
  return link(`${GITHUB_REPO}/commit/${row.sha}/checks?check_suite_id=${row.suite.id}`, "All GitHub checks of this commit", null,
    "Every check of this commit on GitHub, for all the browsers and channels that ran on it, not only this one");
}

function reasonText(reason) {
  if (!reason) return null;
  if (reason.kind === "check_run_limit") return `The suite has ${number.format(reason.check_runs)} check runs; wpt.fyi drops suites over ${number.format(reason.limit)}`;
  if (reason.kind === "wptfyi_status") {
    const latest = reason.entries[reason.entries.length - 1];
    return `wpt.fyi upload ${latest.stage || "?"}${latest.error ? `: ${latest.error}` : ""}`;
  }
  return "Cause unknown; wpt.fyi's server logs are needed";
}

function stateCell(portKey, cell, row, channelLabel) {
  const chunks = cell.chunks || {};
  const top = element("div", { class: "top" });
  const details = [];
  const links = [];
  switch (cell.state) {
    case "running":
      top.append(element("span", { class: "headline" }, `Running ${chunks.completed}/${chunks.expected}`));
      details.push(`${plural(chunks.unfinished, "chunk")} not finished yet`);
      break;
    case "waiting_for_suite":
      top.append(element("span", { class: "headline" }, "Waiting for the suite"));
      details.push("Our chunks passed; wpt.fyi imports the results once every browser on this commit has finished");
      break;
    case "uploading":
      top.append(element("span", { class: "headline" }, "Uploading"));
      details.push(`The suite finished at ${formatTime(cell.since, { withDate: false })}; this turns red at the first update after ${formatTime(cell.red_at)} if the run is still not on wpt.fyi`);
      break;
    case "failed": {
      const failed = chunks.failed || [], missing = chunks.missing || [];
      const parts = [failed.length && plural(failed.length, "chunk") + " failed", missing.length && plural(missing.length, "chunk") + " missing"].filter(Boolean);
      top.append(element("span", { class: "headline" }, parts.join(", ")));
      if (failed.length)
        details.push(element("div", { class: "chunk-links" }, failed.map(chunk => {
          const logs = [
            chunk.log_url ? link(chunk.log_url, "Taskcluster", null, `The log of ${chunk.name} on Taskcluster`) : null,
            chunk.github_url ? link(chunk.github_url, "GitHub", null, `The check of ${chunk.name} on GitHub`) : null,
          ].filter(Boolean);
          const outcome = chunk.reason && chunk.reason !== chunk.state ? `${chunk.state} (${chunk.reason})` : chunk.state;
          return element("span", {},
            element("span", {}, link(chunk.task_url, shortChunkName(chunk.name), "failed", `${chunk.name} on Taskcluster`), ` ${outcome}.`),
            logs.length ? element("span", { class: "dim" }, "Logs:") : null, logs);
        })));
      if (missing.length) details.push(`Missing: ${missing.join(", ")}`);
      break;
    }
    case "not_uploaded":
      top.append(element("span", { class: "headline" }, "Not uploaded"));
      details.push(reasonText(cell.reason));
      details.push(`The suite finished at ${formatTime(cell.since)}`);
      break;
    case "suite_never_finished":
      top.append(element("span", { class: "headline" }, "Suite never finished"));
      details.push(`Our chunks finished at ${formatTime(cell.since)}, but the suite is still not complete after ${dashboard.settings.suite_timeout_hours} hours`);
      break;
    case "decision_failed":
      top.append(element("span", { class: "headline" }, "Decision task failed"));
      details.push("Taskcluster could not schedule the tasks of this commit");
      break;
    case "scheduling":
      top.append(element("span", { class: "headline" }, "Scheduling tasks"));
      details.push("A decision task of this commit is still creating its tasks");
      break;
    case "commit_unavailable":
      top.append(element("span", { class: "headline", title: row.error || "" }, "Could not read this commit"));
      details.push("GitHub or Taskcluster did not answer in the last update, and there is no earlier data for this commit. The next update tries again.");
      break;
    case "wptfyi_unchecked":
      top.append(element("span", { class: "headline" }, "wpt.fyi not checked"));
      details.push(`The suite finished at ${formatTime(cell.since)}, but the last update could not read the runs on wpt.fyi, so this run may already be there. The next update checks again.`);
      break;
    default:
      top.append(element("span", { class: "headline" }, "No run"));
      details.push("No tasks for this browser on this commit");
  }
  if (channelLabel) top.append(element("span", { class: "channel" }, channelLabel));
  const group = taskGroupLink(chunks) || (row.task_groups?.length
    ? link(`${TASKCLUSTER_ROOT}/tasks/groups/${row.task_groups[row.task_groups.length - 1]}#${portByKey(portKey).product}`, "Taskcluster tasks") : null);
  if (group) links.push(group);
  if (cell.state !== "no_run") {
    const checks = githubChecksLink(row);
    if (checks) links.push(checks);
  }
  const node = element("div", { class: `cell ${STATE_KIND[cell.color] || "none"}` }, top,
    details.map(detail => typeof detail === "string" ? element("div", { class: "detail" }, detail) : detail));
  if (chunks.retried) node.append(element("div", { class: "note" }, `${plural(chunks.retried, "chunk")} passed after a retry`));
  const times = runTimes(cell.chunks);
  if (times) node.append(times);
  if (links.length) node.append(element("div", { class: "links" }, links));
  if (row.stale && cell.state !== "commit_unavailable") node.append(element("div", { class: "stale-note" }, "Could not refresh this commit; showing the last data"));
  return node;
}

function gapWarning(key, days) {
  const text = `Many tests have probably changed in WPT over ${days} days, so take these numbers with a grain of salt.`;
  const button = element("button", { type: "button", class: "gap-warning", title: text, "aria-expanded": String(view.openGapNotes.has(key)) }, `⚠ ${days} days apart`);
  button.addEventListener("click", () => { if (view.openGapNotes.has(key)) view.openGapNotes.delete(key); else view.openGapNotes.add(key); render(); });
  return [button, view.openGapNotes.has(key) ? element("div", { class: "warning-text" }, text) : null];
}

function uploadedCell(portKey, channel, cell, row, channelLabel) {
  const info = cell.wptfyi;
  const diffKey = `${portKey}:${channel}:${info.run_id}`;
  const isOpen = view.openDiff === diffKey;
  const node = element("div", { class: `cell ok${isOpen ? " selected" : ""}` },
    element("div", { class: "top" }, element("span", { class: "headline" }, info.browser_version),
      channelLabel ? element("span", { class: "channel" }, channelLabel) : null,
      link(`${WPTFYI_ROOT}/results/?run_id=${info.run_id}`, "wpt.fyi run")));
  const diff = info.diff;
  if (diff && diff.totals) {
    const lines = element("div", { class: "lines" });
    const addLine = (name, item, className) => lines.append(
      element("span", { class: className }, name),
      element("span", { class: className }, plusMinus("tests", item.tests_fixed, item.tests_broken)),
      element("span", { class: className }, plusMinus("subtests", item.subtests_fixed, item.subtests_broken)));
    addLine("all", diff.totals, "all");
    for (const directory of diff.top_directories) addLine(directory.name, directory);
    node.append(lines);
    const notCompared = notComparedText(diff.totals);
    if (notCompared) node.append(element("div", { class: "detail" }, notCompared));
  } else if (diff && diff.error) {
    node.append(element("div", { class: "detail" }, `Could not compute the diff: ${diff.error}`));
  }
  if (info.previous) {
    node.append(element("div", { class: "detail" },
      `vs ${utcDate(info.previous.time_start)} · ${info.previous.browser_version} · ${plural(info.days, "day")}`));
    if (info.days > GAP_WARNING_DAYS) node.append(element("div", {}, gapWarning(diffKey, info.days)));
  } else {
    node.append(element("div", { class: "detail" }, `No earlier run on wpt.fyi in the last ${dashboard.settings.max_age_days} days, so there is no diff`));
  }
  if (channel === "nightly") {
    if (info.same_build) node.append(element("div", { class: "warning-text" }, `⚠ Same bundle as the run of ${utcDate(info.previous.time_start)}: no new nightly build was tested`));
    else if (info.tested_build) node.append(element("div", { class: "detail" }, "Tested ",
      link(buildUrl(portByKey(portKey).builder, info.tested_build.number), `build #${info.tested_build.number}`), ` of ${shortDate(utcDate(info.tested_build.started_at))}`));
  }
  if (cell.chunks?.retried) node.append(element("div", { class: "note" }, `${plural(cell.chunks.retried, "chunk")} passed after a retry`));
  const times = runTimes(cell.chunks);
  if (times) node.append(times);
  if (row.stale) node.append(element("div", { class: "stale-note" }, "Could not refresh this commit; showing the last data"));
  if (diff && diff.detail) {
    const button = element("button", { type: "button", class: "plain diff-toggle", "aria-expanded": String(isOpen) }, isOpen ? "Hide diff" : "Show diff");
    button.addEventListener("click", () => {
      view.openDiff = isOpen ? null : diffKey;
      if (!isOpen) loadDiff(diff.detail);
      render();
    });
    node.append(button);
  }
  return node;
}

function wptCell(portKey, channel, row, channelLabel) {
  const cell = row.cells[portKey]?.[channel];
  if (!cell) return element("div", { class: "cell none" }, element("div", { class: "top" }, element("span", { class: "headline" }, "No run")));
  return cell.state === "uploaded" ? uploadedCell(portKey, channel, cell, row, channelLabel) : stateCell(portKey, cell, row, channelLabel);
}

// Expanded diff

async function loadDiff(detail) {
  const existing = view.diffs.get(detail);
  if (existing && existing.state !== "error") return;
  view.diffs.set(detail, { state: "loading" });
  try {
    const response = await fetch(DATA_DIR + detail);
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    view.diffs.set(detail, { state: "ready", data: await response.json() });
  } catch (error) {
    view.diffs.set(detail, { state: "error", error: String(error.message || error) });
  }
  render();
}

function countCell(value, kind) {
  if (!value) return element("td", { class: "zero" }, "0");
  return element("td", { class: kind }, `${kind === "plus" ? "+" : "−"}${number.format(value)}`);
}

function subdirectories(directory) {
  const groups = new Map();
  const groupOf = test => {
    // A test's variant, after the "?", can contain slashes that are not directories.
    const parts = test.split(/[?#]/)[0].split("/");
    return parts.length > 3 ? parts[2] : null;
  };
  const ensure = name => {
    if (!groups.has(name)) groups.set(name, { name, tests_fixed: 0, tests_broken: 0, subtests_fixed: 0, subtests_broken: 0, not_compared: 0, files: [], extra: [] });
    return groups.get(name);
  };
  for (const file of directory.files) {
    const group = ensure(groupOf(file.test));
    group.tests_fixed += file.test_change === "fixed";
    group.tests_broken += file.test_change === "broken";
    group.subtests_fixed += file.subtests_fixed;
    group.subtests_broken += file.subtests_broken;
    group.files.push(file);
  }
  for (const [label, list, key] of [["added", directory.added, "after"], ["removed", directory.removed, "before"]])
    for (const file of list) {
      const group = ensure(groupOf(file.test));
      group.not_compared++;
      group.extra.push({ label, test: file.test, state: file[key] });
    }
  return [...groups.values()].sort((a, b) => b.tests_broken - a.tests_broken || b.subtests_broken - a.subtests_broken || b.tests_fixed - a.tests_fixed || String(a.name).localeCompare(String(b.name)));
}

function fileRows(body, files, extra) {
  for (const file of files) {
    const change = element("td", { colspan: "2" });
    if (file.test_change) change.append(element("span", { class: `badge ${file.test_change}` }, file.test_change === "fixed" ? "now passes" : "stopped passing"));
    else if (!file.subtests_fixed && !file.subtests_broken) change.append(element("span", { class: "badge status", title: "Its status changed, but no count did, so it adds nothing to the totals" }, "status changed"));
    body.append(element("tr", { class: "file" },
      element("td", {}, file.test, element("span", { class: "states" }, `${describeState(file.before)} → ${describeState(file.after)}`)),
      change, countCell(file.subtests_fixed, "plus"), countCell(file.subtests_broken, "minus"), element("td")));
  }
  for (const file of extra)
    body.append(element("tr", { class: "file" },
      element("td", {}, file.test, element("span", { class: "states" }, `${file.label} · ${describeState(file.state)}`)),
      element("td", { colspan: "5" }, "not compared")));
}

function nodeRow(key, depth, name, counts, notCompared) {
  const isOpen = view.openNodes.has(key);
  const row = element("tr", { class: `node depth-${depth}${isOpen ? " open" : ""}`, tabindex: "0", "aria-expanded": String(isOpen) },
    element("td", {}, name),
    countCell(counts.tests_fixed, "plus"), countCell(counts.tests_broken, "minus"),
    countCell(counts.subtests_fixed, "plus"), countCell(counts.subtests_broken, "minus"),
    element("td", { class: "dim" }, notCompared ? number.format(notCompared) : ""));
  const toggle = () => { if (view.openNodes.has(key)) view.openNodes.delete(key); else view.openNodes.add(key); render(); };
  row.addEventListener("click", toggle);
  row.addEventListener("keydown", event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); toggle(); } });
  return [row, isOpen];
}

function diffTable(diffKey, detail) {
  const body = element("tbody");
  for (const directory of detail.directories) {
    const key = `${diffKey}:${directory.name}`;
    const [row, isOpen] = nodeRow(key, 0, directory.name, directory, directory.added_tests + directory.removed_tests);
    body.append(row);
    if (!isOpen) continue;
    for (const group of subdirectories(directory)) {
      if (group.name === null) { fileRows(body, group.files, group.extra); continue; }
      const [subRow, subOpen] = nodeRow(`${key}/${group.name}`, 1, group.name, group, group.not_compared);
      body.append(subRow);
      if (subOpen) fileRows(body, group.files, group.extra);
    }
  }
  return element("div", { class: "diff-table-wrap" }, element("table", { class: "dt" },
    element("thead", {}, element("tr", {},
      element("th", {}, "Directory"), element("th", {}, "Tests +"), element("th", {}, "Tests −"),
      element("th", {}, "Subtests +"), element("th", {}, "Subtests −"), element("th", {}, "Not compared"))),
    body));
}

function diffRow(portKey, channel, row) {
  const info = row.cells[portKey][channel].wptfyi;
  const diffKey = `${portKey}:${channel}:${info.run_id}`;
  const port = portByKey(portKey);
  const close = element("button", { type: "button", class: "plain", "aria-label": "Hide the diff" }, "× Hide diff");
  close.addEventListener("click", () => { view.openDiff = null; render(); });
  const previousDate = utcDate(info.previous.time_start);
  const parts = [
    element("div", { class: "diff-head" },
      element("div", { class: "diff-title" },
        element("h3", {}, `${port.label} ${channel} · ${row.date}`),
        element("span", { class: "dim" }, `compared with ${previousDate} (${info.previous.browser_version}) · ${plural(info.days, "day")}`),
        info.days > GAP_WARNING_DAYS ? gapWarning(`${diffKey}:head`, info.days) : null),
      close),
    element("div", { class: "diff-meta" },
      element("span", {}, "Previous run: ", element("b", {}, info.previous.browser_version), ` · ${previousDate}`),
      element("span", {}, "This run: ", element("b", {}, info.browser_version), ` · ${utcDate(info.time_start)}`),
      info.same_build ? element("span", { class: "warning-text" }, "⚠ Same WebKit build in both runs: every change comes from WPT or from flaky tests") : null,
      element("span", { class: "links" },
        link(`${WPTFYI_ROOT}/results/?diff&filter=ADC&run_id=${info.previous.run_id}&run_id=${info.run_id}`, "Full diff on wpt.fyi"),
        link(`${WPTFYI_ROOT}/results/?run_id=${info.run_id}`, "This run on wpt.fyi"),
        link(`${WPTFYI_ROOT}/results/?run_id=${info.previous.run_id}`, "Previous run on wpt.fyi"))),
  ];
  const loaded = view.diffs.get(info.diff.detail);
  // After a reload the open run can point to another file, for example when an older upload arrived late.
  if (!loaded) loadDiff(info.diff.detail);
  if (!loaded || loaded.state === "loading") parts.push(element("div", { class: "detail" }, "Loading the diff…"));
  else if (loaded.state === "error") parts.push(element("div", { class: "detail" }, `Could not load ${info.diff.detail}: ${loaded.error}`));
  else {
    const totals = element("div", { class: "lines diff-totals" },
      element("span", { class: "all" }, "all"),
      element("span", { class: "all" }, plusMinus("tests", loaded.data.totals.tests_fixed, loaded.data.totals.tests_broken)),
      element("span", { class: "all" }, plusMinus("subtests", loaded.data.totals.subtests_fixed, loaded.data.totals.subtests_broken)));
    parts.push(totals);
    const notCompared = notComparedText(loaded.data.totals);
    if (notCompared) parts.push(element("div", { class: "detail" }, notCompared));
    parts.push(diffTable(diffKey, loaded.data));
  }
  return element("div", { class: "diff-row" }, parts);
}

function openDiffIn(row, channels) {
  if (!view.openDiff) return null;
  const [portKey, channel, runId] = view.openDiff.split(":");
  if (!channels.includes(channel)) return null;
  const info = row.cells[portKey]?.[channel]?.wptfyi;
  return info && String(info.run_id) === runId && info.diff?.detail ? { portKey, channel } : null;
}

// WPT sections

// The daily tags of a day are created between 00:00 and 05:00 UTC, so after
// 08:00 UTC a day without one is really missing.
const DAILY_TAG_EXPECTED_HOUR_UTC = 8;

function nightlyRows() {
  const rows = [];
  const generated = new Date(dashboard.generated_at);
  const newestExpected = generated.getUTCHours() >= DAILY_TAG_EXPECTED_HOUR_UTC ? utcDate(dashboard.generated_at) : addDays(utcDate(dashboard.generated_at), -1);
  const newestTag = dashboard.daily[0]?.date;
  if (newestTag)
    for (let date = newestExpected; date > newestTag; date = addDays(date, -1)) rows.push({ gap: true, date });
  for (const row of dashboard.daily) {
    const previous = rows[rows.length - 1];
    if (previous && !previous.gap)
      for (let date = addDays(previous.date, -1); date > row.date; date = addDays(date, -1)) rows.push({ gap: true, date });
    rows.push(row);
  }
  return rows;
}

function nightlySection() {
  const rows = nightlyRows();
  const grid = element("div", { class: "aligned" });
  for (const row of rows.slice(0, view.shown.nightly)) {
    if (row.gap) {
      const noTag = () => element("div", { class: "cell none" }, element("div", { class: "top" }, element("span", { class: "headline" }, "No run")),
        element("div", { class: "detail" }, "No daily tag this day"));
      grid.append(dayRow(noTag(), row.date, null, noTag()));
      continue;
    }
    grid.append(dayRow(wptCell(dashboard.ports[0].key, "nightly", row), row.date, row.hour, wptCell(dashboard.ports[1].key, "nightly", row)));
    const open = openDiffIn(row, ["nightly"]);
    if (open) grid.append(diffRow(open.portKey, open.channel, row));
  }
  return [element("h3", { class: "sub", id: "nightly" }, "Nightly"), grid, moreRow("nightly", rows.length)];
}

function weeklySection() {
  const grid = element("div", { class: "aligned" });
  for (const row of dashboard.weekly.slice(0, view.shown.weekly)) {
    const side = port => element("div", { class: "pair" }, wptCell(port.key, "stable", row, "stable"), wptCell(port.key, "beta", row, "beta"));
    grid.append(dayRow(side(dashboard.ports[0]), row.date, row.hour, side(dashboard.ports[1])));
    const open = openDiffIn(row, ["stable", "beta"]);
    if (open) grid.append(diffRow(open.portKey, open.channel, row));
  }
  return [element("h3", { class: "sub", id: "weekly" }, "Stable and beta"), grid, moreRow("weekly", dashboard.weekly.length)];
}

// Log

function logSection() {
  const box = element("div", { class: "log-box", tabindex: "0", role: "log", "aria-label": "Update log" });
  if (view.logHeight) box.style.height = view.logHeight;
  if (logText === null) {
    box.append(element("div", { class: "log-line" }, logError ? `Could not load the log: ${logError}` : "Loading the log…"));
  } else if (!logText.trim()) {
    box.append(element("div", { class: "log-line" }, "The log is empty."));
  } else {
    for (const line of logText.split("\n")) {
      if (!line) continue;
      const match = line.match(/^(\[[^\]]+\])(.*)$/);
      const level = /^\[[^\]]+\] ERROR:/.test(line) ? " error" : /^\[[^\]]+\] WARNING:/.test(line) ? " warning" : "";
      box.append(element("div", { class: `log-line${level}${/^\[[^\]]+\] Starting update/.test(line) ? " run-start" : ""}` },
        match ? [element("span", { class: "ts" }, match[1]), match[2]] : line));
    }
  }
  box.addEventListener("scroll", () => {
    view.logScrolledUp = box.scrollTop + box.clientHeight < box.scrollHeight - 4;
    view.logScrollTop = box.scrollTop;
  });
  logResizeObserver?.disconnect();
  logResizeObserver = new ResizeObserver(() => { if (box.style.height) view.logHeight = box.style.height; });
  logResizeObserver.observe(box);
  requestAnimationFrame(() => { box.scrollTop = view.logScrolledUp ? view.logScrollTop : box.scrollHeight; });
  const reloadFailed = logText !== null && logError
    ? element("div", { class: "stale-note" }, `Could not reload the log at ${formatTime(logFailedAt)} (${logError}); showing the one loaded at ${formatTime(logLoadedAt)}.`)
    : null;
  return element("section", { class: "log", id: "log", "aria-label": "Update log" },
    element("div", { class: "log-head" }, element("h2", {}, "Update log"),
      element("span", { class: "hint" }, "last 30 days · drag the corner to resize"), element("span", { class: "spacer" }),
      link(LOG_URL, "Raw log")),
    reloadFailed, box);
}

// Page

function render() {
  const page = document.getElementById("page");
  const scrollY = window.scrollY;
  const parts = [header()];
  if (!dashboard) {
    parts.push(element("div", { class: dashboardError ? "banner bad" : "banner" },
      dashboardError ? `Could not load ${DATA_URL}: ${dashboardError}. The update script may not have run yet; see the log below.` : "Loading…"));
  } else {
    if (dashboardError) parts.push(element("div", { class: "banner bad" }, `Could not reload ${DATA_URL}: ${dashboardError}. Showing the data loaded before.`));
    parts.push(
      element("div", { class: "port-heads" }, element("div", { class: "port-head" }, dashboard.ports[0].label), element("div", { class: "spacer" }), element("div", { class: "port-head" }, dashboard.ports[1].label)),
      buildsSection(),
      element("section", { class: "wpt", "aria-label": "WPT runs" },
        element("div", { class: "section-title" }, element("h2", {}, "WPT runs"), element("small", {}, "Taskcluster and wpt.fyi")),
        nightlySection(), weeklySection()));
  }
  parts.push(logSection());
  page.replaceChildren(...parts);
  window.scrollTo(0, scrollY);
}

async function load() {
  const [dashboardResult, logResult] = await Promise.allSettled([
    fetch(DATA_URL, { cache: "no-store" }).then(response => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.json(); }),
    fetch(LOG_URL, { cache: "no-store" }).then(response => { if (!response.ok) throw new Error(`HTTP ${response.status}`); return response.text(); }),
  ]);
  if (dashboardResult.status === "fulfilled") {
    dashboard = dashboardResult.value;
    dashboardError = null;
  } else {
    dashboardError = String(dashboardResult.reason.message || dashboardResult.reason);
  }
  if (logResult.status === "fulfilled") {
    logText = logResult.value;
    logError = null;
    logLoadedAt = new Date().toISOString();
  } else {
    logError = String(logResult.reason.message || logResult.reason);
    logFailedAt = new Date().toISOString();
  }
  try {
    render();
  } catch (error) {
    // Keep what was drawn before, and say why the new data is not shown. The
    // same data fails again on every reload, so the banner is replaced, not added.
    document.getElementById("render-error")?.remove();
    document.getElementById("page").prepend(element("div", { class: "banner bad", id: "render-error" },
      `Could not draw the data loaded at ${formatTime(new Date().toISOString())}: ${error.message || error}. Showing what was drawn before.`));
  }
}

render();
load();
setInterval(load, REFRESH_MINUTES * 60000);
