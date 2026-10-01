import collections
import dataclasses
import datetime
import re

TASK_NAME = re.compile(r"^wpt-(?P<browser>[a-z0-9_]+)-(?P<channel>[a-z]+)-(?P<suite>[a-z0-9_]+)-(?P<chunk>\d+)$")
CHUNK_COUNT = re.compile(r"chunk number (\d+) of (\d+)")
UNFINISHED_STATES = {"unscheduled", "pending", "running"}
FAILED_STATES = {"failed", "exception"}


@dataclasses.dataclass(frozen=True)
class Settings:
    upload_grace_hours: float = 7
    suite_timeout_hours: float = 48
    check_run_limit: int = 500


def parse_time(value):
    """A time without a zone is taken as UTC, which is what every service the
    dashboard reads uses, so that any two times can be compared."""
    if not value:
        return None
    moment = datetime.datetime.fromisoformat(value)
    return moment if moment.tzinfo else moment.replace(tzinfo=datetime.timezone.utc)


EARLIEST = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)


def time_key(value):
    """For sorting times from different services, which don't all write the
    same number of decimals, so their strings don't sort as the times do."""
    return parse_time(value) or EARLIEST


def parse_task(entry):
    """Turns one entry of a Taskcluster task group listing into a chunk, or
    returns None when the task is not a WPT test chunk."""
    name = entry["task"]["metadata"]["name"]
    match = TASK_NAME.match(name)
    if not match:
        return None
    chunk_count = CHUNK_COUNT.search(entry["task"]["metadata"].get("description", ""))
    status = entry["status"]
    for run in status.get("runs", []):
        # Checked now, so a bad time counts as a bad answer instead of failing later, far from where it came from.
        parse_time(run.get("started"))
        parse_time(run.get("resolved"))
    return {
        "name": name,
        "browser": match["browser"],
        "channel": match["channel"],
        "suite": match["suite"],
        "chunk": int(match["chunk"]),
        "chunks_in_suite": int(chunk_count[2]) if chunk_count else None,
        "task_id": status["taskId"],
        "task_group_id": status.get("taskGroupId"),
        "state": status["state"],
        "runs": [{"run_id": run["runId"], "state": run["state"], "reason": run.get("reasonResolved"),
                  "started": run.get("started"), "resolved": run.get("resolved")} for run in status.get("runs", [])],
    }


def merge_task_groups(groups):
    """groups is a list of (decision task start time, parsed chunks). The
    master, daily and weekly pushes of a commit each have their own group with
    different channels, so for each browser and channel the chunks come from the
    newest group that has any of them. Taking the whole run from one group means
    an old chunk can never fill a hole in a newer run."""
    chunks_by_run = {}
    for _, group_chunks in sorted(groups, key=lambda group: time_key(group[0])):
        runs_in_group = collections.defaultdict(dict)
        for chunk in group_chunks:
            runs_in_group[(chunk["browser"], chunk["channel"])][chunk["name"]] = chunk
        chunks_by_run.update(runs_in_group)
    return {name: chunk for run in chunks_by_run.values() for name, chunk in run.items()}


def expected_suites(chunks):
    """The number of chunks of each suite, from the descriptions of the given
    chunks."""
    expected = {}
    for chunk in chunks:
        if chunk["chunks_in_suite"]:
            expected[chunk["suite"]] = max(expected.get(chunk["suite"], 0), chunk["chunks_in_suite"])
    return expected


def summarize_chunks(chunks, also_expected=None):
    """chunks are the chunks of one browser and channel. also_expected adds
    suites that were expected even if none of their chunks ran, such as the
    ones the other port ran."""
    expected_by_suite = dict(also_expected or {})
    present = set()
    summary = {"total": len(chunks), "expected": 0, "completed": 0, "unfinished": 0, "failed": [], "missing": [], "retried": 0,
               "first_started": None, "last_resolved": None, "run_seconds": 0, "timed_chunks": 0, "slowest": None}
    for chunk in chunks:
        present.add((chunk["suite"], chunk["chunk"]))
        if chunk["chunks_in_suite"]:
            expected_by_suite[chunk["suite"]] = max(expected_by_suite.get(chunk["suite"], 0), chunk["chunks_in_suite"])
        if chunk["state"] == "completed":
            summary["completed"] += 1
            summary["retried"] += len(chunk["runs"]) > 1
        elif chunk["state"] in FAILED_STATES:
            last_run = chunk["runs"][-1] if chunk["runs"] else {}
            summary["failed"].append({"name": chunk["name"], "task_id": chunk["task_id"], "run_id": last_run.get("run_id"),
                                      "state": chunk["state"], "reason": last_run.get("reason")})
        else:
            summary["unfinished"] += 1
        for run in chunk["runs"]:
            if run["resolved"] and time_key(run["resolved"]) > time_key(summary["last_resolved"]):
                summary["last_resolved"] = run["resolved"]
            if run["started"] and (summary["first_started"] is None or time_key(run["started"]) < time_key(summary["first_started"])):
                summary["first_started"] = run["started"]
        # Only the last run produced the results, so a run that a retry replaced is not part of the run's time.
        last_run = chunk["runs"][-1] if chunk["runs"] else {}
        if last_run.get("started") and last_run.get("resolved"):
            seconds = round((parse_time(last_run["resolved"]) - parse_time(last_run["started"])).total_seconds())
            summary["run_seconds"] += seconds
            summary["timed_chunks"] += 1
            if summary["slowest"] is None or seconds > summary["slowest"]["seconds"]:
                summary["slowest"] = {"name": chunk["name"], "task_id": chunk["task_id"], "seconds": seconds}
    for suite, count in sorted(expected_by_suite.items()):
        summary["missing"].extend(f"{suite}-{number}" for number in range(1, count + 1) if (suite, number) not in present)
    summary["expected"] = sum(expected_by_suite.values()) or len(chunks)
    summary["failed"].sort(key=lambda failed: failed["name"])
    groups = collections.Counter(chunk["task_group_id"] for chunk in chunks if chunk.get("task_group_id"))
    summary["task_group_id"] = groups.most_common(1)[0][0] if groups else None
    return summary


def upload_reason(suite, settings, status_entries):
    if suite["check_runs"] > settings.check_run_limit:
        return {"kind": "check_run_limit", "check_runs": suite["check_runs"], "limit": settings.check_run_limit}
    # An upload that wpt.fyi accepted is no reason for the run to be missing.
    rejected = [entry for entry in status_entries if entry.get("stage") != "VALID"]
    if rejected:
        return {"kind": "wptfyi_status", "entries": rejected}
    return {"kind": "unknown"}


def decide_state(chunks, suite, uploaded, now, settings, *, status_entries=(), decision_failed=False, wptfyi_checked=True,
                 scheduling=False, commit_unavailable=False):
    """Returns the state of one cell as a dict with "state" and "color", plus
    the details that explain it. wptfyi_checked is False when the list of runs
    on wpt.fyi could not be read this time, so a run missing from the old list
    may have been uploaded since. scheduling is True while a decision task of
    the commit is still creating its tasks, so missing chunks may still come.
    commit_unavailable is True when the commit could not be read at all, so
    having no chunks says nothing about it."""
    if uploaded:
        return {"state": "uploaded", "color": "green"}
    if commit_unavailable:
        return {"state": "commit_unavailable", "color": "yellow"}
    if scheduling and (not chunks or chunks["missing"]):
        return {"state": "scheduling", "color": "yellow"}
    if not chunks or (chunks["total"] == 0 and not chunks["missing"]):
        if decision_failed:
            return {"state": "decision_failed", "color": "red"}
        return {"state": "no_run", "color": "grey"}
    if chunks["unfinished"]:
        return {"state": "running", "color": "yellow"}
    if chunks["failed"] or chunks["missing"]:
        return {"state": "failed", "color": "red"}
    if suite is None or suite["status"] != "completed":
        last_resolved = parse_time(chunks["last_resolved"])
        if last_resolved and now - last_resolved > datetime.timedelta(hours=settings.suite_timeout_hours):
            return {"state": "suite_never_finished", "color": "red", "since": chunks["last_resolved"]}
        return {"state": "waiting_for_suite", "color": "yellow"}
    finished = parse_time(suite["updated_at"])
    red_at = finished + datetime.timedelta(hours=settings.upload_grace_hours)
    if now < red_at:
        return {"state": "uploading", "color": "yellow", "since": suite["updated_at"], "red_at": red_at.isoformat()}
    if not wptfyi_checked:
        return {"state": "wptfyi_unchecked", "color": "yellow", "since": suite["updated_at"]}
    return {"state": "not_uploaded", "color": "red", "since": suite["updated_at"],
            "reason": upload_reason(suite, settings, list(status_entries))}
