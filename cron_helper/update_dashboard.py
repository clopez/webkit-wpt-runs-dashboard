#!/usr/bin/env python3
"""Gathers the state of the WebKitGTK and WPE WebKit runs of WPT on
Taskcluster, their results on wpt.fyi and the nightly packaging builds on
build.webkit.org, and writes html/data/dashboard.json for the dashboard page.

Meant to run from cron every 3 hours, with the GitHub token in the
GITHUB_TOKEN environment variable."""

import argparse
import datetime
import fcntl
import json
import os
import pathlib
import re
import sys
import time
import traceback

import diff
import logs
import sources
import states
from fetch import Fetcher, FetchError

REPOSITORY = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = REPOSITORY / "html" / "data"
DEFAULT_CACHE_DIR = REPOSITORY / "cron_helper" / "cache"
DEFAULT_ARCHIVE_DIR = REPOSITORY / "cron_helper" / "logs"

PORTS = [
    {"key": "wpe", "label": "WPE WebKit", "browser": "wpewebkit_minibrowser", "product": "wpewebkit",
     "builder": "WPE-Linux-64bit-Release-Packaging-Nightly"},
    {"key": "gtk", "label": "WebKitGTK", "browser": "webkitgtk_minibrowser", "product": "webkitgtk",
     "builder": "GTK-Linux-64bit-Release-Packaging-Nightly"},
]
CHANNELS_BY_TAG_KIND = {"daily": ["nightly"], "weekly": ["stable", "beta"]}
ALL_CHANNELS = ["nightly", "stable", "beta"]

MAX_AGE_DAYS = 60
STALE_AFTER_HOURS = 7
# A commit's Taskcluster data is final once none of its chunks is running, and
# either its suite finished a day ago and its newest tag is a day old, or its
# newest tag is 3 days old, because Taskcluster ends every task within a day.
# Counting from the newest tag matters because a new tag can land on an old
# commit and start more tasks on it, like a weekly tag on a daily commit.
FINAL_AFTER_SUITE_HOURS = 24
FINAL_AFTER_TAG_HOURS = 72
# Taskcluster ends every task within its deadline, so a decision task that
# started longer ago than that has finished, even if GitHub missed the event
# and still shows its check in progress.
DECISION_DEADLINE_HOURS = 24
FIRST_BUILD_FETCH = MAX_AGE_DAYS + 10
# Builds are kept a few days longer than the window, because the oldest runs
# in the window tested bundles built before it.
BUILD_EXTRA_DAYS = 3
TAG_NAME = re.compile(r"^epochs/(daily|weekly)/\d{4}-\d{2}-\d{2}_\d{2}H$")
# GitHub can list a check run some time after its task ran, so a chunk without
# a check yet is looked up again, but only on this many runs of the script.
CHECK_RUN_LOOKUPS = 8
BUILD_FETCH = 10
SETTINGS = states.Settings()
# An answer that parses but lacks the fields the script reads is as unusable as
# a failed request, so it gets the same fallback instead of stopping the run.
REQUEST_ERRORS = (FetchError, KeyError, TypeError, ValueError)
CACHE_VERSION = 5
# A run that stops because of an unexpected error is tried again right away,
# first with the same cache, in case the error was random, and then with an
# empty one, in case the cache itself is what makes it fail.
RETRY_PAUSE_SECONDS = 60
ATTEMPTS = 3


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def write_json_atomically(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


CACHE_SECTIONS = ("tag_months", "commits", "builds", "build_history_complete", "diffs", "upload_statuses", "wptfyi_runs")


def load_cache(path, logger):
    try:
        cache = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.info(f"No cache at {path} yet; starting with an empty one")
        return {"version": CACHE_VERSION}
    except (OSError, ValueError) as error:
        logger.warning(f"Could not read {path} ({error}); starting with an empty cache")
        return {"version": CACHE_VERSION}
    if not isinstance(cache, dict) or cache.get("version") != CACHE_VERSION:
        logger.warning(f"{path} has an old or unknown format; starting with an empty cache")
        return {"version": CACHE_VERSION}
    return cache


def tag_time(tag_name):
    stamp = tag_name.rsplit("/", 1)[1]
    date, _, hour = stamp.partition("_")
    return datetime.datetime.fromisoformat(f"{date}T{hour.rstrip('H') or '00'}:00:00+00:00")


def months_between(first, last):
    months = []
    year, month = first.year, first.month
    while (year, month) <= (last.year, last.month):
        months.append(f"{year:04d}-{month:02d}")
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return months


def month_is_over(month, today):
    year, number = map(int, month.split("-"))
    first_of_next = datetime.date(year + 1, 1, 1) if number == 12 else datetime.date(year, number + 1, 1)
    return today > first_of_next


class Updater:
    def __init__(self, fetcher, logger, cache, data_dir, now):
        self.fetcher = fetcher
        self.logger = logger
        self.cache = cache
        self.data_dir = pathlib.Path(data_dir)
        self.now = now
        self.today = now.date()
        self.since = self.today - datetime.timedelta(days=MAX_AGE_DAYS)
        self.used_commits = set()
        self.unchecked_listings = set()
        self.tag_listing_failed = False
        self.looked_up_tasks = set()
        self.tags_by_sha = {}
        self.commits_this_run = {}
        self.all_builds = {}
        self.used_diffs = set()
        for section in CACHE_SECTIONS:
            cache.setdefault(section, {})

    def tags(self, kind):
        tags = []
        for month in months_between(self.since, self.today):
            key = f"{kind}/{month}"
            cached = self.cache["tag_months"].get(key)
            if cached and cached["final"]:
                tags.extend(cached["tags"])
                continue
            try:
                month_tags = sources.epoch_tags(self.fetcher, kind, month)
            except REQUEST_ERRORS as error:
                self.tag_listing_failed = True
                self.logger.error(f"Could not list the epochs/{kind} tags of {month}: {error}" + ("; using the previous list" if cached else ""))
                if cached:
                    tags.extend(cached["tags"])
                continue
            unexpected = [tag["name"] for tag in month_tags if not TAG_NAME.match(tag["name"])]
            if unexpected:
                self.logger.warning(f"Leaving out tags with an unexpected name: {', '.join(unexpected)}")
            month_tags = [tag for tag in month_tags if TAG_NAME.match(tag["name"])]
            self.cache["tag_months"][key] = {"final": month_is_over(month, self.today), "tags": month_tags}
            tags.extend(month_tags)
        return [tag for tag in tags if tag["date"] >= self.since.isoformat()]

    def builds(self):
        result = {}
        for port in PORTS:
            known = self.cache["builds"].setdefault(port["key"], {})
            history_complete = bool(known) and self.cache["build_history_complete"].get(port["key"], False)
            # Read outside the handling of request errors, so that a broken cached record reaches the retries with an empty cache.
            finished = {number for number, build in known.items() if build["complete"]}
            stale = False
            try:
                latest = sources.latest_builds(self.fetcher, port["builder"], BUILD_FETCH if history_complete else FIRST_BUILD_FETCH)
                if history_complete and known and latest and min(build["number"] for build in latest) > max(map(int, known)) + 1:
                    self.logger.info(f"build.webkit.org: {port['builder']} has more new builds than one request covers; reading its whole history again")
                    # Until the whole backfill succeeds, the cache has a gap that the next run must fill.
                    self.cache["build_history_complete"][port["key"]] = False
                    latest = sources.latest_builds(self.fetcher, port["builder"], FIRST_BUILD_FETCH)
                for build in latest:
                    if str(build["number"]) in finished:
                        continue
                    build["failed_steps"] = []
                    if build["complete"] and not build["passed"]:
                        build["failed_steps"] = sources.failed_build_steps(self.fetcher, build["buildid"])
                    if build["complete"] and history_complete:
                        self.logger.info(f"build.webkit.org: {port['builder']} #{build['number']} ({build['identifier']}) finished: {build['result']}")
                    known[str(build["number"])] = build
                if not history_complete:
                    self.logger.info(f"build.webkit.org: read the last {len(known)} builds of {port['builder']}")
                self.cache["build_history_complete"][port["key"]] = True
            except REQUEST_ERRORS as error:
                stale = True
                self.logger.error(f"Could not get the builds of {port['builder']}: {error}; keeping the previous ones")
            kept_since = (self.since - datetime.timedelta(days=BUILD_EXTRA_DAYS)).isoformat()
            for number in [number for number, build in known.items() if (build["started_at"] or "") < kept_since]:
                del known[number]
            self.all_builds[port["key"]] = sorted(known.values(), key=lambda build: build["number"], reverse=True)
            shown = [build for build in self.all_builds[port["key"]] if build["started_at"] >= self.since.isoformat()]
            result[port["key"]] = {"builder": port["builder"], "stale": stale, "builds": shown}
        return result

    def upload_statuses(self):
        stored = self.cache["upload_statuses"]
        products = {port["product"]: port for port in PORTS} | {port["browser"]: port for port in PORTS}
        try:
            for entry in sources.wptfyi_upload_statuses(self.fetcher):
                if entry.get("browser_name") in products:
                    stored[str(entry["id"])] = {key: entry.get(key) for key in (
                        "id", "browser_name", "browser_version", "full_revision_hash", "stage", "error", "created", "updated")}
        except REQUEST_ERRORS as error:
            self.logger.error(f"Could not read the wpt.fyi upload statuses: {error}")
        since = self.since.isoformat()
        for key in [key for key, entry in stored.items() if (entry["created"] or "") < since]:
            del stored[key]
        by_commit = {}
        for entry in stored.values():
            product = products[entry["browser_name"]]["product"]
            by_commit.setdefault((entry["full_revision_hash"], product), []).append(entry)
        for entries in by_commit.values():
            entries.sort(key=lambda entry: states.time_key(entry["created"]))
        return by_commit

    def wptfyi_runs(self):
        runs = {}
        for port in PORTS:
            for channel in ALL_CHANNELS:
                key = f"{port['product']}-{channel}"
                try:
                    runs[key] = sources.wptfyi_runs(self.fetcher, port["product"], channel, self.since)
                    self.cache["wptfyi_runs"][key] = runs[key]
                except REQUEST_ERRORS as error:
                    self.logger.error(f"Could not list the wpt.fyi runs of {key}: {error}; using the previous list")
                    runs[key] = self.cache["wptfyi_runs"].get(key, [])
                    self.unchecked_listings.add(key)
        return runs

    def commit(self, tag):
        """Returns (commit_data, stale). A commit shared by a daily and a weekly
        tag is only fetched once per run, and judged with all its tags."""
        sha = tag["sha"]
        self.used_commits.add(sha)
        if sha in self.commits_this_run:
            return self.commits_this_run[sha]
        self.commits_this_run[sha] = result = self._commit(tag)
        return result

    def _commit(self, tag):
        sha = tag["sha"]
        tag_names = self.tags_by_sha.get(sha, set()) | {tag["name"]}
        cached = self.cache["commits"].get(sha)
        if cached and cached["final"] and tag_names <= set(cached["tags"]):
            self.add_check_run_links(tag, cached)
            return cached, False
        try:
            suite = sources.community_tc_suite(self.fetcher, sha)
            decisions = sources.decision_tasks(self.fetcher, sha)
            groups = []
            our_browsers = {port["browser"] for port in PORTS}
            for decision in decisions:
                parsed_chunks = [states.parse_task(entry) for entry in sources.task_group(self.fetcher, decision["task_group_id"])]
                groups.append((decision["started_at"], [chunk for chunk in parsed_chunks if chunk and chunk["browser"] in our_browsers]))
        except REQUEST_ERRORS as error:
            self.logger.error(f"{tag['name']}: {error}; " + ("keeping the previous data" if cached else "no data for this commit yet"))
            if cached:
                return cached, True
            return {"suite": None, "chunks": {}, "task_groups": [], "decision_failed": False, "scheduling": False,
                    "unavailable": True, "error": str(error)}, True
        merged = states.merge_task_groups(groups)
        summaries = {}
        for channel in ALL_CHANNELS:
            by_port = {port["browser"]: [chunk for chunk in merged.values() if chunk["browser"] == port["browser"] and chunk["channel"] == channel]
                       for port in PORTS}
            # A suite that one port ran and the other did not is missing for the other one.
            expected_by_suite = states.expected_suites([chunk for selected in by_port.values() for chunk in selected])
            for browser, selected in by_port.items():
                if selected or expected_by_suite:
                    summaries[f"{browser}-{channel}"] = states.summarize_chunks(selected, expected_by_suite)
        tags = sorted(set(cached["tags"] if cached else []) | tag_names)
        # A retriggered push starts a new decision task on the same commit, so its start counts as new activity too.
        newest_activity = max([tag_time(name) for name in tags] + [states.time_key(decision["started_at"]) for decision in decisions])
        quiet_for = self.now - newest_activity
        suite_done = suite and suite["status"] == "completed" and self.now - states.parse_time(suite["updated_at"]) > datetime.timedelta(hours=FINAL_AFTER_SUITE_HOURS)
        quiet = (suite_done and quiet_for > datetime.timedelta(hours=FINAL_AFTER_SUITE_HOURS)) or quiet_for > datetime.timedelta(hours=FINAL_AFTER_TAG_HOURS)
        decisions_finished = all(decision["status"] == "completed"
                                 or self.now - states.time_key(decision["started_at"]) > datetime.timedelta(hours=DECISION_DEADLINE_HOURS)
                                 for decision in decisions)
        commit_data = {
            "suite": suite,
            "chunks": summaries,
            "task_groups": [decision["task_group_id"] for decision in decisions],
            "decision_failed": any(decision["conclusion"] in ("failure", "cancelled", "timed_out") for decision in decisions),
            "scheduling": not decisions_finished,
            "fetched_at": iso(self.now),
            "tags": tags,
            "final": bool(quiet and decisions_finished and not any(summary["unfinished"] for summary in summaries.values())),
        }
        if cached:
            known_lookups = {failed["task_id"]: failed for summary in cached["chunks"].values() for failed in summary["failed"]}
            for summary in summaries.values():
                for failed in summary["failed"]:
                    previous = known_lookups.get(failed["task_id"], {})
                    for key in ("github_url", "github_lookups"):
                        if key in previous:
                            failed[key] = previous[key]
        self.add_check_run_links(tag, commit_data)
        self.cache["commits"][sha] = commit_data
        return commit_data, False

    def add_check_run_links(self, tag, commit_data):
        for summary in commit_data["chunks"].values():
            for failed in summary["failed"]:
                # A daily and a weekly tag can share a commit, and then both rows get here in the same run.
                if failed.get("github_url") or failed.get("github_lookups", 0) >= CHECK_RUN_LOOKUPS or failed["task_id"] in self.looked_up_tasks:
                    continue
                self.looked_up_tasks.add(failed["task_id"])
                failed["github_lookups"] = failed.get("github_lookups", 0) + 1
                try:
                    failed["github_url"] = sources.check_run_url(self.fetcher, tag["sha"], failed["name"], failed["task_id"])
                except REQUEST_ERRORS as error:
                    self.logger.warning(f"{tag['name']}: could not find the GitHub check of {failed['name']}: {error}; trying again next time")

    def diff_summary(self, previous, run):
        key = f"{previous['id']}-{run['id']}"
        detail = f"diffs/{key}.json"
        self.used_diffs.add(key)
        cached = self.cache["diffs"].get(key)
        if cached and (self.data_dir / detail).exists():
            return cached
        try:
            before = sources.wptfyi_summary(self.fetcher, previous["results_url"])
            after = sources.wptfyi_summary(self.fetcher, run["results_url"])
        except REQUEST_ERRORS as error:
            self.logger.error(f"Could not get the summary files to compare wpt.fyi runs {previous['id']} and {run['id']}: {error}")
            return {"error": str(error)}
        totals, directories = diff.compare(before, after)
        write_json_atomically(self.data_dir / detail, {"previous_run_id": previous["id"], "run_id": run["id"], "totals": totals, "directories": directories})
        summary = {"totals": totals, "top_directories": diff.top_directories(directories), "detail": detail}
        self.cache["diffs"][key] = summary
        return summary

    def wptfyi_cell(self, port, channel, run, channel_runs):
        identifier = identifier_of(run["browser_version"])
        info = {"run_id": run["id"], "time_start": run["time_start"], "created_at": run["created_at"],
                "browser_version": run["browser_version"], "identifier": identifier}
        position = next(index for index, candidate in enumerate(channel_runs) if candidate["id"] == run["id"])
        previous = channel_runs[position + 1] if position + 1 < len(channel_runs) else None
        if previous:
            info["previous"] = {"run_id": previous["id"], "time_start": previous["time_start"],
                                "browser_version": previous["browser_version"], "identifier": identifier_of(previous["browser_version"])}
            info["days"] = (datetime.date.fromisoformat(run["time_start"][:10]) - datetime.date.fromisoformat(previous["time_start"][:10])).days
            info["same_build"] = identifier is not None and identifier == info["previous"]["identifier"]
            info["diff"] = self.diff_summary(previous, run)
        if channel == "nightly":
            # Only a build that passed published its bundle, and only one that finished before the run can be the one it tested.
            published = [build for build in self.all_builds.get(port["key"], [])
                         if identifier is not None and build["identifier"] == identifier and build["passed"]
                         and states.time_key(build["complete_at"]) <= states.time_key(run["time_start"])]
            tested = max(published, key=lambda build: states.time_key(build["complete_at"]), default=None)
            info["tested_build"] = {"number": tested["number"], "started_at": tested["started_at"]} if tested else None
        return info

    def row(self, tag, channels, wptfyi_runs, statuses):
        commit_data, stale = self.commit(tag)
        row = {"tag": tag["name"], "date": tag["date"], "hour": tag["hour"], "sha": tag["sha"], "suite": commit_data["suite"],
               "task_groups": commit_data["task_groups"], "stale": stale, "cells": {}}
        if commit_data.get("unavailable"):
            row["error"] = commit_data["error"]
        log_parts = []
        for port in PORTS:
            row["cells"][port["key"]] = {}
            for channel in channels:
                chunks = commit_data["chunks"].get(f"{port['browser']}-{channel}")
                listing = f"{port['product']}-{channel}"
                channel_runs = wptfyi_runs[listing]
                run = next((candidate for candidate in channel_runs if candidate["sha"] == tag["sha"]), None)
                cell = states.decide_state(chunks, commit_data["suite"], run is not None, self.now, SETTINGS,
                                           status_entries=statuses.get((tag["sha"], port["product"]), []),
                                           decision_failed=commit_data["decision_failed"],
                                           wptfyi_checked=listing not in self.unchecked_listings,
                                           scheduling=commit_data.get("scheduling", False),
                                           commit_unavailable=commit_data.get("unavailable", False))
                cell["chunks"] = with_links(chunks, port["product"])
                if run:
                    cell["wptfyi"] = self.wptfyi_cell(port, channel, run, channel_runs)
                row["cells"][port["key"]][channel] = cell
                log_parts.append(f"{port['product']} {channel} {describe(cell)}")
        if not commit_data.get("final", False) or stale:
            self.logger.info(f"{tag['name']}: " + ", ".join(log_parts))
        return row

    def prune(self):
        for sha in [sha for sha in self.cache["commits"] if sha not in self.used_commits]:
            del self.cache["commits"][sha]
        for key in [key for key in self.cache["diffs"] if key not in self.used_diffs]:
            del self.cache["diffs"][key]
        diffs_dir = self.data_dir / "diffs"
        if diffs_dir.is_dir():
            for path in diffs_dir.glob("*.json"):
                if path.stem not in self.used_diffs:
                    path.unlink()


def identifier_of(browser_version):
    if "(" not in (browser_version or ""):
        return None
    return browser_version.rsplit("(", 1)[1].rstrip(")").strip() or None


def with_links(chunks, product):
    if not chunks:
        return chunks
    chunks = dict(chunks)
    chunks["task_group_url"] = sources.task_group_url(chunks["task_group_id"], product) if chunks.get("task_group_id") else None
    chunks["failed"] = [{**failed, "task_url": sources.task_url(failed["task_id"]),
                         "log_url": sources.task_log_url(failed["task_id"], failed["run_id"]) if failed["run_id"] is not None else None}
                        for failed in chunks["failed"]]
    if chunks.get("slowest"):
        chunks["slowest"] = {**chunks["slowest"], "task_url": sources.task_url(chunks["slowest"]["task_id"])}
    return chunks


def describe(cell):
    chunks = cell.get("chunks") or {}
    if cell["state"] == "running":
        return f"running {chunks.get('completed', 0)}/{chunks.get('expected', 0)}"
    if cell["state"] == "failed":
        return f"failed ({len(chunks.get('failed', []))} failed, {len(chunks.get('missing', []))} missing)"
    return cell["state"].replace("_", " ")


def update(args, logger, recorder, token):
    started = time.monotonic()
    now = utc_now()
    logger.info("Starting update")
    cache_path = pathlib.Path(args.cache_dir) / "cache.json"
    cache = load_cache(cache_path, logger)
    fetcher = Fetcher(logger, token)
    updater = Updater(fetcher, logger, cache, args.data_dir, now)

    builds = updater.builds()
    statuses = updater.upload_statuses()
    wptfyi_runs = updater.wptfyi_runs()
    tags_by_kind = {kind: updater.tags(kind) for kind in CHANNELS_BY_TAG_KIND}
    for tags in tags_by_kind.values():
        for tag in tags:
            updater.tags_by_sha.setdefault(tag["sha"], set()).add(tag["name"])
    rows = {kind: [updater.row(tag, channels, wptfyi_runs, statuses) for tag in reversed(tags_by_kind[kind])]
            for kind, channels in CHANNELS_BY_TAG_KIND.items()}
    # Pruning drops what no row used, so with incomplete inputs it would throw away good data, like every diff.
    if updater.unchecked_listings or updater.tag_listing_failed:
        logger.info("Not cleaning up the cache this time, because some lists could not be read")
    else:
        updater.prune()

    requests = dict(sorted(fetcher.request_counts.items()))
    dashboard = {
        "generated_at": iso(now),
        "duration_seconds": round(time.monotonic() - started, 1),
        "requests": requests,
        "errors": recorder.errors,
        "warnings": recorder.warnings,
        "settings": {"max_age_days": MAX_AGE_DAYS, "stale_after_hours": STALE_AFTER_HOURS,
                     "upload_grace_hours": SETTINGS.upload_grace_hours, "suite_timeout_hours": SETTINGS.suite_timeout_hours,
                     "check_run_limit": SETTINGS.check_run_limit},
        "ports": [{key: port[key] for key in ("key", "label", "product", "builder")} for port in PORTS],
        "builds": builds,
        "daily": rows["daily"],
        "weekly": rows["weekly"],
    }
    write_json_atomically(pathlib.Path(args.data_dir) / "dashboard.json", dashboard)
    write_json_atomically(cache_path, cache)
    cells = sum(len(channel_cells) for row in rows["daily"] + rows["weekly"] for channel_cells in row["cells"].values())
    problems = []
    if recorder.errors:
        problems.append(f"{len(recorder.errors)} {'error' if len(recorder.errors) == 1 else 'errors'}")
    if recorder.warnings:
        problems.append(f"{len(recorder.warnings)} {'warning' if len(recorder.warnings) == 1 else 'warnings'}")
    request_text = ", ".join(f"{service} {count}" for service, count in requests.items())
    logger.info(f"Wrote dashboard.json: {len(rows['daily'])} daily and {len(rows['weekly'])} weekly tags, {cells} cells"
                + (f", {', '.join(problems)}" if problems else "") + f"; requests: {request_text}; {time.monotonic() - started:.1f} s")


def error_text(error):
    location = traceback.extract_tb(error.__traceback__)[-1]
    return f"{type(error).__name__}: {error} (at {pathlib.Path(location.filename).name}:{location.lineno})"


def record_failed_update(data_dir, now, message):
    """Keeps the data of the last good run, but says in it that the updates are
    failing, so the page shows it right away instead of hours later. Without a
    good run to keep, it writes nothing: a dashboard.json with only the failure
    would break the page, which already explains a missing one and shows the
    log."""
    path = pathlib.Path(data_dir) / "dashboard.json"
    try:
        dashboard = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(dashboard, dict) or "ports" not in dashboard:
        return
    dashboard["failed_update"] = {"at": iso(now), "error": message}
    write_json_atomically(path, dashboard)


def run_with_retries(args, logger, recorder, token, sleep=time.sleep):
    cache_path = pathlib.Path(args.cache_dir) / "cache.json"
    set_aside = cache_path.with_name("cache.json.broken")
    failures = []
    for attempt in range(1, ATTEMPTS + 1):
        recorder.errors.clear()
        recorder.warnings[:] = [f"Attempt {number} of the update stopped: {message}" for number, message in failures]
        empty_cache = attempt == ATTEMPTS
        if empty_cache and cache_path.exists():
            os.replace(cache_path, set_aside)
        try:
            update(args, logger, recorder, token)
        except Exception as error:
            failures.append((attempt, error_text(error)))
            if attempt < ATTEMPTS:
                next_try = "with an empty cache" if attempt + 1 == ATTEMPTS else f"with the same cache in {RETRY_PAUSE_SECONDS} s"
                logger.warning(f"The update stopped because of an unexpected error: {failures[-1][1]}; trying again {next_try}")
                if not empty_cache and attempt + 1 < ATTEMPTS:
                    sleep(RETRY_PAUSE_SECONDS)
            continue
        if empty_cache and set_aside.exists():
            logger.warning(f"The update only worked with an empty cache, so the old one was probably broken; it is kept in {set_aside}")
        return 0
    # Every attempt failed, so the problem is not the cache: keep the one that worked before.
    if set_aside.exists():
        os.replace(set_aside, cache_path)
    logger.error(f"The update stopped {ATTEMPTS} times, the last one with an empty cache, so it is probably a bug: {failures[-1][1]}")
    record_failed_update(args.data_dir, utc_now(), failures[-1][1])
    return 1


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--log", help="append the log to this file instead of writing it to stderr, keeping its last 30 days")
    parser.add_argument("--log-archive-dir", default=str(DEFAULT_ARCHIVE_DIR), help="where the log lines older than 30 days go (default: %(default)s)")
    parser.add_argument("--data-dir", default=str(DEFAULT_DATA_DIR), help="where dashboard.json and the diffs are written (default: %(default)s)")
    parser.add_argument("--cache-dir", default=str(DEFAULT_CACHE_DIR), help="where the script keeps its cache (default: %(default)s)")
    return parser.parse_args(argv)


def take_lock(cache_dir):
    """Returns the open lock file, or None when another run holds it. The lock
    lasts as long as the file stays open, that is, until the process ends."""
    pathlib.Path(cache_dir).mkdir(parents=True, exist_ok=True)
    lock = open(pathlib.Path(cache_dir) / "update.lock", "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        return None
    return lock


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    # Taken before touching the log, because rotating it under a run that is still writing would lose that run's lines.
    try:
        lock = take_lock(args.cache_dir)
    except OSError as error:
        # Opening the log to append is safe without the lock; rotating it is not, so it is not rotated here.
        logger, _ = logs.setup_logger(args.log)
        logger.error(f"Could not take the lock in {args.cache_dir} ({error}); stopping")
        return 1
    if lock is None:
        logger, _ = logs.setup_logger(args.log)
        logger.info("Another update is still running; skipping this one")
        return 0
    with lock:
        return run_locked(args)


def run_locked(args):
    rotation_error = None
    if args.log:
        try:
            logs.rotate(args.log, args.log_archive_dir, datetime.date.today())
        except OSError as error:
            rotation_error = error
    logger, recorder = logs.setup_logger(args.log)
    if rotation_error:
        logger.warning(f"Could not rotate the log ({rotation_error}); it will be tried again on the next run")
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not token:
        logger.error("GITHUB_TOKEN is not set; stopping before making any request")
        return 1
    # Checked because a token that HTTP rejects would end up quoted in error messages, which are public.
    if not re.fullmatch(r"[A-Za-z0-9_]+", token):
        logger.error("GITHUB_TOKEN has characters that GitHub tokens never have; stopping without using it")
        return 1
    return run_with_retries(args, logger, recorder, token)


if __name__ == "__main__":
    sys.exit(main())
