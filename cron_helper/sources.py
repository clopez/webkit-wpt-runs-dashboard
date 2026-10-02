import datetime
import urllib.parse

from fetch import FetchError
from states import parse_time, time_key

GITHUB_REPO_API = "https://api.github.com/repos/web-platform-tests/wpt"
GITHUB_CHECK_RUN_PAGE = "https://github.com/web-platform-tests/wpt/runs/"
COMMUNITY_TC_APP_ID = 40788
TASKCLUSTER_ROOT = "https://community-tc.services.mozilla.com"
WPTFYI_ROOT = "https://wpt.fyi"
BUILDBOT_ROOT = "https://build.webkit.org"

# Buildbot's result codes (master/buildbot/process/results.py).
BUILD_RESULTS = {0: "success", 1: "warnings", 2: "failure", 3: "skipped", 4: "exception", 5: "retry", 6: "cancelled"}
PASSING_BUILD_RESULTS = {0, 1}
PASSING_STEP_RESULTS = {0, 1, 3, None}


def epoch_tags(fetcher, kind, month):
    refs = fetcher.get_json(f"{GITHUB_REPO_API}/git/matching-refs/tags/epochs/{kind}/{month}?per_page=100", "github")
    tags = []
    for ref in refs:
        name = ref["ref"].removeprefix("refs/tags/")
        sha = ref["object"]["sha"]
        if ref["object"]["type"] == "tag":
            sha = fetcher.get_json(f"{GITHUB_REPO_API}/git/tags/{sha}", "github")["object"]["sha"]
        stamp = name.rsplit("/", 1)[1]
        date, _, hour = stamp.partition("_")
        tags.append({"name": name, "date": date, "hour": hour, "sha": sha})
    return sorted(tags, key=lambda tag: tag["name"])


def community_tc_suite(fetcher, sha):
    suites = fetcher.get_json(f"{GITHUB_REPO_API}/commits/{sha}/check-suites?app_id={COMMUNITY_TC_APP_ID}&per_page=100", "github")["check_suites"]
    if not suites:
        return None
    suite = max(suites, key=lambda candidate: time_key(candidate["created_at"]))
    return {"id": suite["id"], "status": suite["status"], "conclusion": suite["conclusion"],
            "updated_at": suite["updated_at"], "check_runs": suite["latest_check_runs_count"]}


def decision_tasks(fetcher, sha):
    runs = fetcher.get_json(f"{GITHUB_REPO_API}/commits/{sha}/check-runs?check_name=wpt-decision-task&filter=all&per_page=100", "github")["check_runs"]
    return [{"task_group_id": run["external_id"], "started_at": run["started_at"], "status": run["status"],
             "conclusion": run["conclusion"]} for run in runs if run.get("external_id")]


def check_run_url(fetcher, sha, name, task_id):
    """The GitHub page of the check run of one Taskcluster task, or None when
    GitHub has no check run for it."""
    query = urllib.parse.urlencode({"check_name": name, "filter": "all", "per_page": 100})
    runs = fetcher.get_json(f"{GITHUB_REPO_API}/commits/{sha}/check-runs?{query}", "github")["check_runs"]
    matching = [run for run in runs if run.get("external_id") == task_id]
    if not matching:
        return None
    url = max(matching, key=lambda run: time_key(run.get("started_at")))["html_url"]
    return url if isinstance(url, str) and url.startswith(GITHUB_CHECK_RUN_PAGE) else None


def task_group(fetcher, task_group_id):
    tasks = []
    continuation = None
    while True:
        query = {"limit": 1000}
        if continuation:
            query["continuationToken"] = continuation
        page = fetcher.get_json(f"{TASKCLUSTER_ROOT}/api/queue/v1/task-group/{task_group_id}/list?{urllib.parse.urlencode(query)}", "taskcluster")
        tasks.extend(page["tasks"])
        continuation = page.get("continuationToken")
        if not continuation:
            return tasks


def task_url(task_id):
    return f"{TASKCLUSTER_ROOT}/tasks/{task_id}"


def task_group_url(task_group_id, product):
    return f"{TASKCLUSTER_ROOT}/tasks/groups/{task_group_id}#{product}"


def task_log_url(task_id, run_id):
    return f"{TASKCLUSTER_ROOT}/api/queue/v1/task/{task_id}/runs/{run_id}/artifacts/public/logs/live_backing.log"


def wptfyi_runs(fetcher, product, channel, since):
    query = urllib.parse.urlencode([("product", product), ("label", channel), ("label", "master"),
                                    ("from", f"{since.isoformat()}T00:00:00Z"), ("max-count", 500)])
    try:
        runs = fetcher.get_json(f"{WPTFYI_ROOT}/api/runs?{query}", "wptfyi")
    except FetchError as error:
        # wpt.fyi answers 404 when there is no run in the window.
        if error.status != 404:
            raise
        runs = []
    return sorted(({"id": run["id"], "sha": run["full_revision_hash"], "time_start": run["time_start"], "created_at": run["created_at"],
                    "browser_version": run["browser_version"], "results_url": run["results_url"]} for run in runs),
                  key=lambda run: time_key(run["time_start"]), reverse=True)


def wptfyi_summary(fetcher, results_url):
    summary = fetcher.get_json(results_url, "wptfyi")
    if not isinstance(summary, dict):
        raise FetchError(f"{results_url}: not a summary file")
    for test, entry in summary.items():
        valid = (isinstance(entry, dict) and isinstance(entry.get("s"), str) and isinstance(entry.get("c"), list)
                 and len(entry["c"]) == 2 and all(isinstance(count, int) for count in entry["c"]))
        if not valid:
            raise FetchError(f"{results_url}: unexpected entry for {test[:200]}")
    return summary


def wptfyi_upload_statuses(fetcher, stage=None):
    """wpt.fyi's latest 500 uploads, or with stage (for example "invalid") the
    latest 500 in that stage, leaving out any entry that is not a well-formed
    record, because one bad entry should not hide the others."""
    url = f"{WPTFYI_ROOT}/api/status" + (f"/{stage}" if stage else "")
    entries = fetcher.get_json(url, "wptfyi")
    if not isinstance(entries, list):
        raise FetchError(f"{url}: not a list")
    return [entry for entry in entries if isinstance(entry, dict) and isinstance(entry.get("id"), int) and is_time(entry.get("created"))]


def is_time(value):
    try:
        return isinstance(value, str) and parse_time(value) is not None
    except ValueError:
        return False


def latest_builds(fetcher, builder, count):
    query = urllib.parse.urlencode({"order": "-number", "limit": count, "property": "identifier"})
    builds = fetcher.get_json(f"{BUILDBOT_ROOT}/api/v2/builders/{urllib.parse.quote(builder)}/builds?{query}", "buildbot")["builds"]
    return [{
        "number": build["number"],
        "buildid": build["buildid"],
        "identifier": (build.get("properties", {}).get("identifier") or [None])[0],
        "complete": build["complete"],
        "result": BUILD_RESULTS.get(build["results"]) if build["complete"] else None,
        "passed": build["complete"] and build["results"] in PASSING_BUILD_RESULTS,
        "state_string": build["state_string"],
        "started_at": to_iso(build["started_at"]),
        "complete_at": to_iso(build["complete_at"]),
    } for build in builds]


def failed_build_steps(fetcher, buildid):
    steps = fetcher.get_json(f"{BUILDBOT_ROOT}/api/v2/builds/{buildid}/steps", "buildbot")["steps"]
    return [{"number": step["number"], "name": step["name"], "state_string": step["state_string"]}
            for step in steps if step["complete"] and step["results"] not in PASSING_STEP_RESULTS]


def to_iso(timestamp):
    return datetime.datetime.fromtimestamp(timestamp, datetime.timezone.utc).isoformat().replace("+00:00", "Z") if timestamp else None
