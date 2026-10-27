#!/usr/bin/env python3
"""Gathers the state of the WebKitGTK and WPE WebKit runs of WPT on
Taskcluster, their results on wpt.fyi and the nightly packaging builds on
build.webkit.org, and writes html/data/dashboard.json for the dashboard page.

Meant to run from cron every hour, with the GitHub token in the
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

REPOSITORY = logs.REPOSITORY
DEFAULT_DATA_DIR = REPOSITORY / "html" / "data"
DEFAULT_CACHE_DIR = REPOSITORY / "cron_helper" / "cache"
DEFAULT_ARCHIVE_DIR = REPOSITORY / "cron_helper" / "logs"

PORTS = [
    {"key": "wpe", "label": "WPE WebKit", "browser": "wpewebkit_minibrowser", "product": "wpewebkit",
     "builder": "WPE-Linux-64bit-Release-Packaging-Nightly", "site": "wpewebkit.org"},
    {"key": "gtk", "label": "WebKitGTK", "browser": "webkitgtk_minibrowser", "product": "webkitgtk",
     "builder": "GTK-Linux-64bit-Release-Packaging-Nightly", "site": "webkitgtk.org"},
]
CHANNELS_BY_TAG_KIND = {"daily": ["nightly"], "weekly": ["stable", "beta"]}
RELEASE_CHANNELS = ["stable", "beta"]
ALL_CHANNELS = ["nightly", "stable", "beta"]

MAX_AGE_DAYS = 60
# How often cron runs the script, which the script cannot read from cron
# itself: it has to match the cron line in the README, "0 * * * *". The page
# uses it to say when the next update is due, at the next multiple of it
# counted from midnight UTC, which is when such a cron line runs.
UPDATE_EVERY_MINUTES = 60
# About two missed runs.
STALE_AFTER_HOURS = 3
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
# A chunk that stopped before installing the browser has no version in its
# log, so the next one is tried, but no more than this many per run.
VERSION_CANDIDATES = 3
# A log that cannot be read keeps its commit open so that the next run tries
# again, but only on this many runs, so that a log that stays broken does not
# keep the commit open for the whole window.
VERSION_READS = 8
SETTINGS = states.Settings()
# An answer that parses but lacks the fields the script reads is as unusable as
# a failed request, so it gets the same fallback instead of stopping the run.
REQUEST_ERRORS = (FetchError, KeyError, TypeError, ValueError)
CACHE_VERSION = 7
# A run that stops because of an unexpected error is tried again right away,
# first with the same cache, in case the error was random, and then with an
# empty one, in case the cache itself is what makes it fail.
RETRY_PAUSE_SECONDS = 60
ATTEMPTS = 3
# The Taskcluster queue that every WPT task waits in, as its tasks say in
# taskQueueId; the machines of its pool take tasks from it in order.
TASK_QUEUE = "proj-wpt/ci-gw"
# wpt.fyi creates the uploads of all the runs of a finished suite at once.
BATCH_SPREAD = datetime.timedelta(minutes=1)


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


iso = states.iso


def write_json_atomically(path, data):
    path = pathlib.Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(data, separators=(",", ":"), sort_keys=True), encoding="utf-8")
    os.replace(temporary, path)


CACHE_SECTIONS = ("tag_months", "commits", "builds", "build_history_complete", "diffs", "upload_statuses", "wptfyi_runs", "releases",
                  "queue_view", "upload_batches")


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
        self.clock = utc_now
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
        self.estimate_targets = []
        self.row_logs = []
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

    def releases(self):
        """For each port and release channel, the newest release and the newest
        bundle, and whether the bundle is the build of that release. The build
        of the stable and beta bundles still has to be triggered by hand after
        each release, so an outdated bundle means that a build is due."""
        result = {}
        for port in PORTS:
            known = self.cache["releases"].setdefault(port["key"], {})
            try:
                released = sources.latest_releases(self.fetcher, port["site"], port["product"])
                listing_stale = False
            except REQUEST_ERRORS as error:
                released = {channel: known.get(channel, {}).get("release") for channel in RELEASE_CHANNELS}
                listing_stale = True
                self.logger.error(f"Could not read the releases of {port['site']}: {error}; keeping the previous ones")
            result[port["key"]] = {}
            for channel in RELEASE_CHANNELS:
                previous = known.get(channel, {})
                try:
                    bundle = sources.latest_bundle(self.fetcher, port["site"], channel)
                    bundle_stale = False
                except REQUEST_ERRORS as error:
                    bundle = previous.get("bundle")
                    bundle_stale = True
                    self.logger.error(f"Could not read the {channel} bundle of {port['site']}: {error}; keeping the previous one")
                release = released[channel]
                for name, item in (("release", release), ("bundle", bundle)):
                    if item and previous.get(name) and item["version"] != previous[name]["version"]:
                        self.logger.info(f"{port['site']}: new {channel} {name} {item['version']}")
                known[channel] = {"release": release, "bundle": bundle}
                result[port["key"]][channel] = {"release": release, "bundle": bundle, "state": bundle_state(release, bundle),
                                                "stale": listing_stale or bundle_stale}
        return result

    def upload_statuses(self):
        stored = self.cache["upload_statuses"]
        products = {port["product"]: port for port in PORTS} | {port["browser"]: port for port in PORTS}
        # The list of all the uploads only covers about a day, so a run that
        # wpt.fyi rejected before the cache existed is only in the list of the
        # rejected ones, which goes back weeks.
        uploads_by_sha = {}
        for stage in (None, "invalid"):
            try:
                for entry in sources.wptfyi_upload_statuses(self.fetcher, stage):
                    if entry.get("browser_name") in products:
                        stored[str(entry["id"])] = {key: entry.get(key) for key in (
                            "id", "browser_name", "browser_version", "full_revision_hash", "stage", "error", "created", "updated")}
                    # An upload has no browser name until wpt.fyi has processed it, which can take an
                    # hour, but it says from the start which commit it is for and that Taskcluster sent it.
                    sha = entry.get("full_revision_hash")
                    if entry.get("uploader") == "taskcluster" and sha in self.tags_by_sha:
                        pending = str(entry.get("stage", "")).startswith("WPTFYI_") if stage is None else None
                        uploads_by_sha.setdefault(sha, []).append((entry["created"], pending))
            except REQUEST_ERRORS as error:
                self.logger.error(f"Could not read the wpt.fyi {stage or 'latest'} upload statuses: {error}")
        since = self.since.isoformat()
        for key in [key for key, entry in stored.items() if (entry["created"] or "") < since]:
            del stored[key]
        batches = self.cache["upload_batches"]
        for sha in set(batches) | set(uploads_by_sha):
            if sha in self.tags_by_sha:
                batches[sha] = states.upload_batches(batches.get(sha, []), uploads_by_sha.get(sha, []), BATCH_SPREAD)
            else:
                del batches[sha]
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
            every_task = []
            our_browsers = {port["browser"] for port in PORTS}
            for decision in decisions:
                tasks = sources.task_group(self.fetcher, decision["task_group_id"], lambda entry: (states.parse_task(entry), states.task_brief(entry)))
                every_task.extend(brief for _, brief in tasks)
                groups.append((decision["started_at"], [chunk for chunk, _ in tasks if chunk and chunk["browser"] in our_browsers]))
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
                    key = f"{browser}-{channel}"
                    summaries[key] = states.summarize_chunks(selected, expected_by_suite)
                    previous = cached["chunks"].get(key, {}) if cached else {}
                    summaries[key].update(self.run_version(tag, selected, previous))
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
            # Only a commit still in progress has unfinished tasks, and a commit in progress is read again on every run, so this is never old.
            "progress": states.commit_progress(every_task),
            "fetched_at": iso(self.now),
            "tags": tags,
            # A version that could not be read is not final either, so that the next run reads it.
            "final": bool(quiet and decisions_finished and not any(summary["unfinished"] or summary["version_read_failed"] for summary in summaries.values())),
        }
        if cached:
            known_lookups = {failed["task_id"]: failed for summary in cached["chunks"].values() for failed in summary["failed"]}
            for summary in summaries.values():
                for failed in summary["failed"]:
                    previous = known_lookups.get(failed["task_id"], {})
                    for key in ("github_url", "github_lookups"):
                        if key in previous:
                            failed[key] = previous[key]
        if commit_data["final"]:
            # Nothing of it is left to estimate, but its chunks and decision tasks still say how long they take.
            commit_data["progress"] = {"unfinished": [], "decision_seconds": commit_data["progress"]["decision_seconds"],
                                       "chunk_history": commit_data["progress"]["chunk_history"]}
        self.add_check_run_links(tag, commit_data)
        self.cache["commits"][sha] = commit_data
        return commit_data, False

    def run_version(self, tag, chunks, previous):
        """The browser version that a run tested, from the log of its chunk
        that started first among the ones that finished. Only a finished
        chunk has its whole log stored. Every chunk of a run normally tests
        the same bundle, so one log is enough; a run whose chunks did not
        is rejected by wpt.fyi, which says so in its error."""
        finished = sorted((chunk for chunk in chunks if chunk["runs"] and chunk["runs"][-1]["started"] and chunk["runs"][-1]["resolved"]),
                          key=lambda chunk: states.time_key(chunk["runs"][-1]["started"]))
        read_failed = False
        logs_without_version = 0
        for chunk in finished[:VERSION_CANDIDATES]:
            run_id = chunk["runs"][-1]["run_id"]
            source = f"{chunk['task_id']}/{run_id}"
            # A finished run's log never changes, so a version read before is not read again.
            if source == previous.get("version_source"):
                return {"version": previous["version"], "version_source": source, "version_read_failed": False, "version_failures": 0}
            try:
                version = sources.chunk_version(self.fetcher, chunk["task_id"], run_id)
            except REQUEST_ERRORS as error:
                self.logger.warning(f"{tag['name']}: could not read the log of {chunk['name']}: {error}; trying again next time")
                read_failed = True
                continue
            if version:
                return {"version": version, "version_source": source, "version_read_failed": False, "version_failures": 0}
            logs_without_version += 1
        # One chunk that stopped before installing the browser is normal, but several logs in a row without the line can mean that WPT changed it.
        if logs_without_version and logs_without_version == min(len(finished), VERSION_CANDIDATES):
            self.logger.warning(f"{tag['name']}: no log of the first {plural(logs_without_version, 'finished chunk')} of {chunks[0]['browser']} "
                                f"{chunks[0]['channel']} says which version it installed; the format of WPT's log may have changed")
        failures = previous.get("version_failures", 0) + read_failed
        return {"version": None, "version_source": None, "version_read_failed": read_failed and failures < VERSION_READS, "version_failures": failures}

    def queue_view(self):
        """Where each waiting task is in the Taskcluster queue, and how fast the
        queue moves, from the previous view of it, kept in the cache. It is
        read on every run, even with nothing of ours waiting, so that the pace
        is known as soon as something is."""
        try:
            waiting = sources.pending_tasks(self.fetcher, TASK_QUEUE)
        except REQUEST_ERRORS as error:
            self.logger.warning(f"Could not read the Taskcluster queue {TASK_QUEUE}: {error}; no estimates for waiting tasks this time")
            return None
        # The time of the read, not of the start of the run, which can be many minutes earlier when the cache is filled again.
        read_at = self.clock()
        previous = self.cache["queue_view"]
        if previous and read_at - states.parse_time(previous["at"]) < states.QUEUE_PACE_MIN_WINDOW:
            # Too recent to measure the pace from, as after an update run by hand, so the view is kept for the next run, with the pace it had.
            pace, unknown = previous.get("tasks_per_hour"), previous.get("pace_unknown")
        else:
            pace, unknown, measured = states.queue_pace(previous, waiting[0] if waiting else None, read_at)
            busy = {"at": iso(read_at), "tasks_per_hour": pace} if measured else (previous or {}).get("busy_pace")
            self.cache["queue_view"] = {"at": iso(read_at), "tasks": waiting, "tasks_per_hour": pace, "pace_unknown": unknown, "busy_pace": busy}
        pace_text = f"{pace} an hour leaving it" if pace else "it has not moved since the previous update" if unknown == "stuck" else "pace not known yet"
        self.logger.info(f"Taskcluster queue {TASK_QUEUE}: {plural(len(waiting), 'task')} waiting, {pace_text}")
        return {"id": TASK_QUEUE, "positions": {key: number for number, key in enumerate(waiting, 1)}, "length": len(waiting),
                "tasks_per_hour": pace, "pace_unknown": unknown}

    def add_estimates(self, rows):
        """Adds when each running run, each commit still scheduling and each
        unfinished suite should finish. A chunk, of any browser, takes about
        as long as the same chunk took in its latest runs, and a chunk that
        never finished before about as long as the finished chunks of its
        browser and channel on the same commit, or as the commit's finished
        chunks. A decision task takes as long as the window's ones did."""
        queue = self.queue_view()
        # Every commit in the window is in the cache, so its chunks and decision tasks give their usual times.
        typical = states.chunk_history(((max(map(tag_time, commit.get("tags", [])), default=states.EARLIEST), commit)
                                        for commit in self.cache["commits"].values()), self.now)
        decision_seconds = [seconds for commit in self.cache["commits"].values() for seconds in (commit.get("progress") or {}).get("decision_seconds", [])]
        if decision_seconds:
            typical[states.DECISION_TASK] = sorted(decision_seconds)
        for row, commit_data in self.estimate_targets:
            progress = commit_data.get("progress")
            if commit_data.get("final") or not progress or not progress["unfinished"]:
                continue

            def durations_for(task, progress=progress):
                return typical.get(task["history_key"]) or progress["seconds_by_kind"].get(task["kind"]) or progress["chunk_seconds"] or None

            for port in PORTS:
                for channel, cell in row["cells"][port["key"]].items():
                    if cell["state"] == "scheduling":
                        # The chunks are created as soon as the decision tasks finish.
                        tasks = [task for task in progress["unfinished"] if task["name"] == states.DECISION_TASK]
                    elif cell["state"] == "running":
                        group = cell["chunks"].get("task_group_id")
                        tasks = [task for task in progress["unfinished"] if task["kind"] == f"{port['browser']}-{channel}" and task["task_group_id"] == group]
                    else:
                        continue
                    if tasks:
                        cell["estimate"] = states.finish_estimate(tasks, queue, durations_for, self.now)
            if not row["suite"] or row["suite"]["status"] != "completed":
                row["suite_estimate"] = states.finish_estimate(progress["unfinished"], queue, durations_for, self.now)
        self.add_upload_estimates(rows)

    def batch_with(self, sha, chunks):
        """The first batch of this commit's runs that wpt.fyi received after our
        last chunk finished, or None. wpt.fyi sends every run whose checks all
        passed in one batch, so a batch from after that has our run in it."""
        if not chunks or not chunks.get("last_resolved"):
            return None
        finished = states.parse_time(chunks["last_resolved"])
        return next((batch for batch in self.cache["upload_batches"].get(sha, []) if states.parse_time(batch["at"]) >= finished), None)

    def has_weekly_tag(self, sha):
        return any(name.startswith("epochs/weekly/") for name in self.tags_by_sha.get(sha, ()))

    def add_upload_estimates(self, rows):
        """A run should be on wpt.fyi by when wpt.fyi has gone through the batch
        it came in, at the pace it has gone through it so far. Before it has
        processed any of the batch, the run should be ready as long after
        wpt.fyi received it as our runs took to be processed, which is longer
        for a commit with a weekly tag: it sends wpt.fyi the stable and beta
        runs of every browser too, about twice as many at once. Until there
        are enough of those times, which the cache keeps from when it
        started, a daily commit can still use how long after its suite
        finished its run appeared, because its suite rarely opens again; a
        weekly one has no estimate."""
        processed = {True: [], False: []}
        for entry in self.cache["upload_statuses"].values():
            if entry["stage"] == "VALID" and entry["created"] and entry["updated"]:
                seconds = (states.parse_time(entry["updated"]) - states.parse_time(entry["created"])).total_seconds()
                processed[self.has_weekly_tag(entry["full_revision_hash"])].append(seconds)
        after_suite, after_suite_count = states.upload_delay([row for row in rows if not self.has_weekly_tag(row["sha"])])
        for row in rows:
            weekly = self.has_weekly_tag(row["sha"])
            if len(processed[weekly]) >= states.MIN_UPLOADS_FOR_ESTIMATE:
                delay, uploads, measured = round(states.median(processed[weekly])), len(processed[weekly]), "received"
            elif not weekly and after_suite is not None:
                delay, uploads, measured = after_suite, after_suite_count, "suite"
            else:
                delay, uploads, measured = None, len(processed[weekly]), None
            for channels in row["cells"].values():
                for cell in channels.values():
                    if cell["state"] != "uploading":
                        continue
                    cell["estimate"] = {"expected_finish": iso(states.parse_time(cell["since"]) + datetime.timedelta(seconds=delay)) if delay is not None else None,
                                        "upload_seconds": delay, "uploads": uploads, "measured": measured, "weekly": weekly,
                                        "unknown": None if delay is not None else "weekly_uploads" if weekly else "uploads"}
                    batch = self.batch_with(row["sha"], cell["chunks"]) if cell.get("received") else None
                    if batch and batch["uploads"]:
                        cell["estimate"].update({"batch_uploads": batch["uploads"], "batch_processed": batch["processed"]})
                        # Our run is one of them, so it is ready when the batch is at the latest, at the pace wpt.fyi has
                        # gone through it so far, which counts the uploads of other commits that its processor also takes.
                        if batch["processed"]:
                            received = states.parse_time(batch["at"])
                            done = received + (self.now - received) * batch["uploads"] / batch["processed"]
                            cell["estimate"].update({"expected_finish": iso(max(done, self.now).replace(microsecond=0)), "measured": "batch", "unknown": None})

    def log_rows(self):
        """Logged once the estimates are known, because the lines include them."""
        for tag_name, row in self.row_logs:
            self.logger.info(f"{tag_name}: " + ", ".join(f"{port['product']} {channel} {describe(cell, row)}"
                                                       for port in PORTS for channel, cell in row["cells"][port["key"]].items()))

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
                                           commit_unavailable=commit_data.get("unavailable", False),
                                           received_at=(self.batch_with(tag["sha"], chunks) or {}).get("at"))
                cell["chunks"] = with_links(chunks, port["product"])
                if run:
                    cell["wptfyi"] = self.wptfyi_cell(port, channel, run, channel_runs)
                row["cells"][port["key"]][channel] = cell
        self.estimate_targets.append((row, commit_data))
        if not commit_data.get("final", False) or stale:
            self.row_logs.append((tag["name"], row))
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


def describe(cell, row):
    chunks = cell.get("chunks") or {}
    if cell["state"] == "running":
        return f"{chunks.get('running', 0)} running, {chunks.get('completed', 0)}/{chunks.get('expected', 0)} done" + expected_text(cell.get("estimate"))
    if cell["state"] == "scheduling" and cell.get("estimate"):
        estimate = cell["estimate"]
        waiting = f", decision task number {estimate['position']} in the queue" if estimate["position"] else ""
        return f"scheduling{waiting}{expected_text(estimate)}"
    if cell["state"] == "waiting_for_suite" and row.get("suite_estimate"):
        estimate = row["suite_estimate"]
        unfinished = estimate["running"] + estimate["pending"] + estimate["unscheduled"]
        return f"waiting for suite ({plural(unfinished, 'task')} left{expected_text(estimate)})"
    if cell["state"] == "uploading" and cell.get("estimate"):
        return f"uploading{expected_text(cell['estimate'])}"
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
    releases = updater.releases()
    wptfyi_runs = updater.wptfyi_runs()
    tags_by_kind = {kind: updater.tags(kind) for kind in CHANNELS_BY_TAG_KIND}
    for tags in tags_by_kind.values():
        for tag in tags:
            updater.tags_by_sha.setdefault(tag["sha"], set()).add(tag["name"])
    # After the tags, so that only the batches of the commits in the window are kept.
    statuses = updater.upload_statuses()
    rows = {kind: [updater.row(tag, channels, wptfyi_runs, statuses) for tag in reversed(tags_by_kind[kind])]
            for kind, channels in CHANNELS_BY_TAG_KIND.items()}
    updater.add_estimates(rows["daily"] + rows["weekly"])
    updater.log_rows()
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
        "settings": {"max_age_days": MAX_AGE_DAYS, "stale_after_hours": STALE_AFTER_HOURS, "update_every_minutes": UPDATE_EVERY_MINUTES,
                     "upload_grace_hours": SETTINGS.upload_grace_hours, "suite_timeout_hours": SETTINGS.suite_timeout_hours,
                     "check_run_limits": [list(limit) for limit in SETTINGS.check_run_limits]},
        "ports": [{key: port[key] for key in ("key", "label", "product", "builder", "site")} for port in PORTS],
        "builds": builds,
        "releases": releases,
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


def expected_text(estimate):
    if not estimate:
        return ""
    if estimate["expected_finish"]:
        return f", expected around {estimate['expected_finish'][11:16]} UTC"
    return f", no estimate ({estimate['unknown']} not known)" if estimate.get("unknown") else ""


def plural(count, noun):
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def bundle_state(release, bundle):
    if not release or not bundle:
        return "unknown"
    # A bundle newer than the listing can only mean the listing is behind, so it is not a build to do.
    return "current" if sources.release_version(bundle["version"]) >= sources.release_version(release["version"]) else "outdated"


def error_text(error):
    location = traceback.extract_tb(error.__traceback__)[-1]
    return logs.without_local_paths(f"{type(error).__name__}: {error} (at {pathlib.Path(location.filename).name}:{location.lineno})")


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
    args = parser.parse_args(argv)
    # Resolved like the repository's own path, so that a path given through a
    # symbolic link or relative to the current directory is still recognized
    # as inside the repository and written relative to it in the log.
    for name in ("log", "log_archive_dir", "data_dir", "cache_dir"):
        if getattr(args, name):
            setattr(args, name, str(pathlib.Path(getattr(args, name)).resolve()))
    return args


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
