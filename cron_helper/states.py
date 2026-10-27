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
    # How many check runs wpt.fyi's webhook could list from each time on, as
    # (since, limit), oldest first. wpt.fyi#5127 raised it from 500 to 2000;
    # it was merged on 2026-10-01 at 18:27 UTC, and its deployment is taken
    # to be two days later, because its exact time is not known.
    check_run_limits: tuple = ((None, 500), ("2026-10-03T18:27:00Z", 2000))

    def check_run_limit_at(self, moment):
        return [limit for since, limit in self.check_run_limits if since is None or parse_time(since) <= moment][-1]


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
        parse_time(run.get("scheduled"))
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
        "runs": [{"run_id": run["runId"], "state": run["state"], "reason": run.get("reasonResolved"), "scheduled": run.get("scheduled"),
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
               "first_started": None, "last_resolved": None, "run_seconds": 0, "timed_chunks": 0, "slowest": None,
               "running": 0, "pending": 0}
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
            if chunk["state"] == "running":
                summary["running"] += 1
            elif chunk["state"] == "pending":
                summary["pending"] += 1
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


def iso(moment):
    return moment.isoformat().replace("+00:00", "Z")


def median(values):
    return sorted(values)[len(values) // 2] if values else None


def task_queue_of(task):
    """Older tasks name their queue by provisioner and worker type instead."""
    return task.get("taskQueueId") or (f"{task['provisionerId']}/{task['workerType']}" if task.get("provisionerId") else None)


def task_brief(entry):
    """What the estimates need from any task of a push, a WPT chunk or not,
    because the suite waits for all of them. Tasks of the same kind (the
    chunks of one browser and channel, or the tasks of the same name) take
    about as long."""
    status = entry["status"]
    name = entry["task"]["metadata"]["name"]
    match = TASK_NAME.match(name)
    chunk_count = CHUNK_COUNT.search(entry["task"]["metadata"].get("description", "")) if match else None
    last_run = status["runs"][-1] if status.get("runs") else {}
    started, resolved = parse_time(last_run.get("started")), parse_time(last_run.get("resolved"))
    return {"task_id": status["taskId"], "run_id": last_run.get("runId"), "task_group_id": status.get("taskGroupId"),
            "queue": task_queue_of(entry["task"]), "name": name, "kind": f"{match['browser']}-{match['channel']}" if match else name, "chunk": bool(match),
            # A suite split into another number of chunks runs other tests in each, so its chunks start a new history.
            "history_key": f"{name}-of-{chunk_count[2]}" if chunk_count else name,
            "state": status["state"], "started": last_run.get("started"),
            "seconds": round((resolved - started).total_seconds()) if status["state"] == "completed" and started and resolved else None}


# The first tasks of a kind to finish are often its shortest, for example 9
# minutes for Firefox beta chunks that usually take 30 to 50, so a kind's
# time is only trusted once this many of its tasks have finished.
MIN_FINISHED_OF_KIND = 5


# The task that creates the others; it waits in the same queue as they do.
DECISION_TASK = "wpt-decision-task"


def commit_progress(tasks):
    """The unfinished tasks of a commit, of every browser, and how long its
    finished ones took by kind, which is what the estimates need from it. A
    commit has only one to three decision tasks, too few to say how long one
    takes, so their times are kept to be pooled over the window."""
    seconds_by_kind = collections.defaultdict(list)
    chunk_seconds = []
    history = collections.defaultdict(dict)
    for task in tasks:
        if task["seconds"] is not None:
            seconds_by_kind[task["kind"]].append(task["seconds"])
            if task["chunk"]:
                chunk_seconds.append(task["seconds"])
                history[task["kind"]][task["history_key"].removeprefix(f"wpt-{task['kind']}-")] = task["seconds"]
    unfinished = [{key: value for key, value in task.items() if key != "seconds"} for task in tasks if task["state"] in UNFINISHED_STATES]
    return {"unfinished": unfinished,
            "seconds_by_kind": {kind: sorted(seconds) for kind, seconds in seconds_by_kind.items() if len(seconds) >= MIN_FINISHED_OF_KIND},
            # The fallback for chunks of a kind with too few finished, so short tasks like the decision tasks do not count.
            "chunk_seconds": sorted(chunk_seconds),
            "decision_seconds": seconds_by_kind.get(DECISION_TASK, []),
            # Every finished chunk of every browser, by browser and channel and then by suite and number, which is shorter to keep.
            "chunk_history": dict(history)}


# A chunk takes about the same time every day, until the tests in it change:
# Firefox Android's nightly testharness-25 took 57 to 61 minutes until
# 2026-09-23, and 142 to 169 from the next day on. So only its latest runs
# count: enough of them that one odd run does not decide, and none so old
# that its tests may have changed since. That is a week of nightly runs, and
# the last two of the stable and beta ones, which run once a week.
HISTORY_RUNS = 7
HISTORY_AGE = datetime.timedelta(days=14)


def chunk_history(dated_commits, now):
    """How long each chunk, by its history key, took on its latest runs.
    dated_commits are (time, commit) pairs, the time being when the commit
    was last tagged, which is about when its chunks ran. The same chunk
    takes about the same time every day, because it runs the same tests: on
    2026-10-23, 24 and 25 Firefox Android's testharness-25 took 160, 155 and
    154 minutes, while its other chunks took 22 to 113."""
    durations = collections.defaultdict(list)
    for _, commit in sorted((pair for pair in dated_commits if now - pair[0] < HISTORY_AGE), key=lambda pair: pair[0], reverse=True):
        for kind, chunks in (commit.get("progress") or {}).get("chunk_history", {}).items():
            for short_name, seconds in chunks.items():
                if len(durations[f"wpt-{kind}-{short_name}"]) < HISTORY_RUNS:
                    durations[f"wpt-{kind}-{short_name}"].append(seconds)
    return {name: sorted(seconds) for name, seconds in durations.items()}


def expected_seconds(durations, elapsed=None):
    """How long a task, or an upload, should take in all, from how long the
    finished ones of its kind took. One that has already taken a while can
    only be one of the longer ones: the ones that finish first are the short
    ones, so the median of all of them would say that it is done when it is
    not. Longer than any of them, there is nothing to tell how much longer it
    takes, so None, rather than saying it is done."""
    if elapsed is None:
        return median(durations)
    return median([seconds for seconds in durations if seconds > elapsed])


# The pace of the queue is measured between two updates, but not over less
# than this, as after an update run by hand: over 12 minutes on 2026-10-27 it
# came out at 243 an hour, against 171 over the hour before.
QUEUE_PACE_MIN_WINDOW = datetime.timedelta(minutes=30)


# A queue that ran dry since the previous update only says that the tasks
# waiting then have left, which can be far fewer than its machines can take,
# so the last pace measured while it was busy is used instead, if this recent.
BUSY_PACE_MAX_AGE = datetime.timedelta(hours=6)


def queue_pace(previous, head, now):
    """How many tasks an hour left the queue since the previous view of it, as
    (tasks_per_hour, why it is unknown, whether it was measured just now).
    Tasks leave it in order, so the ones that left are those ahead of the
    current head in the previous view. A head that did not move means that
    the queue is stuck, and no old pace should hide that. A head that was not
    in the previous view means that the queue ran dry in between."""
    if not previous or "tasks" not in previous:
        return None, "pace", False
    hours = (now - parse_time(previous["at"])).total_seconds() / 3600
    # A view from the future, as after the clock was set back, measures nothing.
    if hours <= 0:
        return None, "pace", False
    tasks = previous["tasks"]
    if head in tasks:
        left = tasks.index(head)
        return (max(1, round(left / hours)), None, True) if left else (None, "stuck", False)
    busy = previous.get("busy_pace")
    if busy and now - parse_time(busy["at"]) <= BUSY_PACE_MAX_AGE:
        return max(busy["tasks_per_hour"], round(len(tasks) / hours)), None, False
    return None, "pace", False


# Fewer uploads than this say too little about how long wpt.fyi takes.
MIN_UPLOADS_FOR_ESTIMATE = 5


def upload_delay(rows):
    """How long wpt.fyi took to create each run of the given rows after its
    suite finished, sorted. A suite that changed after its run was
    created, as when a later push added checks to the same commit, or when
    it opened again for a retried task and completed later, has a finish
    time that says nothing about the upload, so it is left out."""
    delays = []
    for row in rows:
        suite = row.get("suite") or {}
        for channels in row["cells"].values():
            for cell in channels.values():
                created = (cell.get("wptfyi") or {}).get("created_at")
                if cell["state"] == "uploaded" and created and suite.get("status") == "completed":
                    delay = (parse_time(created) - parse_time(suite["updated_at"])).total_seconds()
                    if delay >= 0:
                        delays.append(delay)
    return sorted(delays)


def upload_batches(previous, uploads, spread):
    """The batches in which wpt.fyi received a commit's runs, oldest first,
    each with when it arrived ("at"), how many uploads it had and how many of
    them wpt.fyi has processed. uploads are (created, pending) pairs, pending
    being None for an upload that only the list of the rejected ones gave,
    which says when a batch arrived but not how big it was. previous are the
    batches known before, whose counts stay when their uploads have left
    wpt.fyi's lists, which only cover about a day."""
    times = sorted({batch["at"] for batch in previous} | {created for created, _ in uploads}, key=time_key)
    starts = []
    for created in times:
        if not starts or parse_time(created) - parse_time(starts[-1]) >= spread:
            starts.append(created)
    known = {batch["at"]: batch for batch in previous}
    batches = []
    for number, start in enumerate(starts):
        end = parse_time(starts[number + 1]) if number + 1 < len(starts) else None
        inside = [pending for created, pending in uploads
                  if pending is not None and parse_time(start) <= parse_time(created) and (end is None or parse_time(created) < end)]
        if inside:
            batches.append({"at": start, "uploads": len(inside), "processed": inside.count(False)})
        else:
            batches.append({"at": start, "uploads": known.get(start, {}).get("uploads"), "processed": known.get(start, {}).get("processed")})
    return batches


def finish_estimate(tasks, queue, durations_for, now):
    """When the given unfinished tasks should all have finished. durations_for
    gives the sorted times of the finished tasks of a task's kind, or None.
    A running task finishes as expected_seconds says, and a waiting one starts
    when the tasks ahead of it have left the queue at its pace. A task
    waiting for another one to finish is not in the queue yet, and is taken
    to join it at the end. queue is None when it could not be read, and
    otherwise has its "id", the "positions" of its tasks by "taskId/runId"
    counting from 1, its "length" and its "tasks_per_hour"."""
    estimate = {"running": 0, "pending": 0, "unscheduled": 0, "position": None, "queue_length": queue["length"] if queue else None,
                "tasks_per_hour": queue["tasks_per_hour"] if queue else None, "expected_finish": None, "unknown": None}
    finishes = []
    unknown = []
    for task in tasks:
        estimate[task["state"]] += 1
        durations = durations_for(task)
        if not durations:
            unknown.append("durations")
        if task["state"] == "running":
            start = parse_time(task["started"]) or now
            seconds = expected_seconds(durations, max(0, (now - start).total_seconds())) if durations else None
            if durations and seconds is None:
                unknown.append("longer")
        elif queue is None or task["queue"] != queue["id"]:
            unknown.append("queue")
            continue
        else:
            position = queue["positions"].get(f"{task['task_id']}/{task['run_id']}") if task["state"] == "pending" else queue["length"] + 1
            if task["state"] == "pending" and position is not None:
                estimate["position"] = max(estimate["position"] or 0, position)
            if position is None:
                # Not in the queue any more, so it started after its push was read.
                start = now
            elif not queue["tasks_per_hour"]:
                unknown.append(queue.get("pace_unknown") or "pace")
                continue
            else:
                start = now + datetime.timedelta(hours=position / queue["tasks_per_hour"])
            seconds = expected_seconds(durations) if durations else None
        if seconds is not None:
            finishes.append(max(start + datetime.timedelta(seconds=seconds), now))
    for reason in ("queue", "stuck", "pace", "durations", "longer"):
        if reason in unknown:
            estimate["unknown"] = reason
            break
    else:
        estimate["expected_finish"] = iso(max(finishes).replace(microsecond=0)) if finishes else None
    return estimate


# What a processed upload ends as: on wpt.fyi (VALID), or already there from
# another batch of the same suite (DUPLICATE); anything else was rejected.
ACCEPTED_STAGES = {"VALID", "DUPLICATE"}


def rejected_uploads(status_entries, chunks):
    """The uploads that wpt.fyi rejected for this run, after its last chunk
    finished. wpt.fyi's entries only name the browser, not the channel, so on
    a commit with several channels the run is told apart by the version its
    chunks tested: a rejected entry has an empty version, but its error lists
    the versions in conflict, which on 2026-10 were always our chunks' one
    and the next bundle's. Without a version, nothing can be told apart."""
    version, finished = chunks.get("version"), parse_time(chunks.get("last_resolved"))
    if not version or not finished:
        return []
    return [entry for entry in status_entries
            if entry.get("stage") not in ACCEPTED_STAGES and parse_time(entry["created"]) >= finished
            and (entry.get("browser_version") == version or version in (entry.get("error") or ""))]


def upload_reason(suite, settings, status_entries, since):
    # The limit of when wpt.fyi tried to import the run.
    limit = settings.check_run_limit_at(parse_time(since))
    if suite is not None and suite["check_runs"] > limit:
        return {"kind": "check_run_limit", "check_runs": suite["check_runs"], "limit": limit}
    # An upload that wpt.fyi accepted is no reason for the run to be missing.
    rejected = [entry for entry in status_entries if entry.get("stage") not in ACCEPTED_STAGES]
    if rejected:
        return {"kind": "wptfyi_status", "entries": rejected}
    return {"kind": "unknown"}


def decide_state(chunks, suite, uploaded, now, settings, *, status_entries=(), decision_failed=False, wptfyi_checked=True,
                 scheduling=False, commit_unavailable=False, received_at=None):
    """Returns the state of one cell as a dict with "state" and "color", plus
    the details that explain it. wptfyi_checked is False when the list of runs
    on wpt.fyi could not be read this time, so a run missing from the old list
    may have been uploaded since. scheduling is True while a decision task of
    the commit is still creating its tasks, so missing chunks may still come.
    commit_unavailable is True when the commit could not be read at all, so
    having no chunks says nothing about it. received_at is when wpt.fyi got a
    batch of this commit's runs after our last chunk finished, if it did."""
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
    # wpt.fyi takes every run whose checks all passed as soon as the suite
    # completes, and a suite can complete early, while a retried task waits
    # for a machine without a check, and then open again. So a batch that
    # wpt.fyi received after our last chunk finished has our run in it,
    # whatever the suite says now, and the upload counts from then.
    if received_at:
        since, received = received_at, True
    elif suite is not None and suite["status"] == "completed":
        since, received = suite["updated_at"], False
    else:
        last_resolved = parse_time(chunks["last_resolved"])
        if last_resolved and now - last_resolved > datetime.timedelta(hours=settings.suite_timeout_hours):
            return {"state": "suite_never_finished", "color": "red", "since": chunks["last_resolved"]}
        return {"state": "waiting_for_suite", "color": "yellow"}
    # wpt.fyi may still take the run from a later batch of the same suite, but a rejection of it is the likely end, so it shows at once.
    rejected = rejected_uploads(status_entries, chunks)
    if rejected:
        return {"state": "not_uploaded", "color": "red", "since": since, "received": received,
                "reason": {"kind": "wptfyi_status", "entries": rejected}}
    red_at = parse_time(since) + datetime.timedelta(hours=settings.upload_grace_hours)
    if now < red_at:
        return {"state": "uploading", "color": "yellow", "since": since, "received": received, "red_at": red_at.isoformat()}
    if not wptfyi_checked:
        return {"state": "wptfyi_unchecked", "color": "yellow", "since": since, "received": received}
    return {"state": "not_uploaded", "color": "red", "since": since, "received": received,
            "reason": upload_reason(suite, settings, list(status_entries), since)}
