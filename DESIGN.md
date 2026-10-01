# WebKit WPT runs (Linux) dashboard: design notes

## The problem

The web-platform-tests (WPT) project runs its whole test suite on Taskcluster for
`webkitgtk_minibrowser` and `wpewebkit_minibrowser`, in three channels:

- nightly, every day;
- stable and beta, every week.

When all the chunks of a run pass, wpt.fyi imports the results so they show up
on its dashboard.

Today, finding out whether a given run worked takes several manual steps. You
open the epoch commit on GitHub, open its checks, open the Taskcluster panel,
and then look for the failed chunk and its log. It is also easy to miss that a
run passed on Taskcluster but never reached wpt.fyi, and that has been
happening silently for months (see "Known wpt.fyi upload bug" below).

The dashboard should show, at a glance, the latest runs for each browser and
channel. A run is green only when it has actually reached wpt.fyi, and then the
cell also shows what changed since the previous run. Otherwise it is red, with
a direct link to whatever went wrong.

## Architecture

The work is split in two, because the information has to be there as soon as
the page loads, and gathering it takes many requests to GitHub, Taskcluster and
wpt.fyi that no visitor should have to wait for:

- a Python script on the server runs every 3 hours from cron. It gathers
  everything, decides the state of each cell, computes the diffs, and writes a
  single JSON file;
- a static page reads that JSON and draws it. It never talks to GitHub,
  Taskcluster or wpt.fyi itself, apart from the links the visitor clicks.

Every 3 hours is enough because the runs are daily at most. However, it means
the page always shows the state as it was when the script last ran, so the page
says when that was.

### Repository layout

Everything lives in one repository:

- `html/`: the page (`index.html`, `dashboard.js`, `dashboard.css` and the
  icons). The web server serves this directory.
- `html/data/`: written by the script, and listed in `.gitignore`. It holds
  `dashboard.json`, which the page draws, the script's log, which the page
  shows, and `diffs/`, with the expanded diff of each pair of runs.
- `cron_helper/`: the Python script. `update_dashboard.py` is the entry point
  and ties the rest together: `sources.py` talks to GitHub, Taskcluster,
  wpt.fyi and build.webkit.org, `states.py` decides the state of each cell,
  `diff.py` compares two runs, `fetch.py` makes the requests and retries them,
  and `logs.py` writes and rotates the log. `tests/` has the unit tests.
- `cron_helper/cache/`: the script's own memory between runs (see "Cache"),
  also in `.gitignore`. It stays out of `html/` because the page doesn't need
  it.
- `cron_helper/logs/`: the rotated logs, also in `.gitignore`. The cache
  directory also holds the lock file, `update.lock`, that keeps two runs from
  overlapping.

### The script

- It targets Python 3.11, the version on the server (Debian 12, Python
  3.11.2), and uses only the standard library. The server does have packages
  like `requests`, but `urllib`, `gzip` and `json` are enough here, and this way
  the script also runs on any other machine without installing anything.
- It only looks at tags and runs from the last `MAX_AGE_DAYS` days, so it never
  goes back through years of history.
- It reads the GitHub token from the `GITHUB_TOKEN` environment variable, which
  cron passes to it. If the variable is not set, it stops with an error before
  making any request. It also stops if the token has characters GitHub tokens
  never have, because `http.client` rejects such a header with an error that
  quotes it, and error messages are public. For the same reason, an error
  raised while building a request is never retried nor quoted, and the token is
  replaced with `***` in any message the fetcher writes. The token is only sent
  over HTTPS to `api.github.com`, and it is dropped when a redirect leaves that
  origin, because Python's `urllib` copies every header to the target of a
  redirect.
- It takes a lock in its cache directory (`update.lock`) before doing
  anything, even before rotating its log, so a run never overlaps with another
  that uses the same cache directory: a second one logs that the first is still
  going and stops. That is what matters, since the cache, the data and the log
  of one installation go together. If the lock cannot even be taken, for
  example because the cache directory cannot be created, the error goes to the
  log. Every
  request has a timeout of 90 seconds, and only `https` addresses are fetched.
- A request that fails with a server error (5xx), a rate limit (403 or 429), a
  network error, or a body that is truncated or cannot be decoded, is tried
  again after 10 seconds, and once more after 30 seconds. Reading and decoding
  the body happen inside these retries, because a connection that drops
  halfway can still answer 200. If a retry succeeds, the script logs a `WARNING:`; if all three
  attempts fail, it logs an `ERROR:`. Other client errors, like a 404, are not
  retried.
- With `--log /path/to/file`, it appends its log to that file instead of
  writing it to stderr. Cron runs it with
  `--log $repo/html/data/cron_helper.log.txt`, so the log is shown on the page
  and cron never sends emails. The name ends in `.txt` so that any web server
  sends it as plain text, and the browser shows it instead of downloading it.
- Every log line starts with a timestamp in the format of WebKit's
  `Tools/Scripts/filter-test-logs`, which includes the short time zone name:
  `[2026-09-29|12:00:01|CEST] message`. After the timestamp, errors carry an
  `ERROR:` prefix and problems the script worked around (for example a request
  that only succeeded when retried) carry `WARNING:`, so the page can show them
  in red and yellow.
- The log is public, and so are the errors and warnings that `dashboard.json`
  repeats, so a path inside the repository is written relative to it, for
  example `cron_helper/cache/cache.json`, and the place where the repository
  lives on the server stays private. That also covers the paths that Python
  quotes in its own error messages. The paths given on the command line are
  resolved first, because one that goes through a symbolic link would not
  start like the repository's own path. A path outside the repository is
  written in full.
- The log file on the web always holds the last 30 calendar days, today
  included, as a rolling window. A failed rotation, or a line cut in the middle
  of a character, never stops the update. Each run of the script moves the lines older than that out of
  `html/data/` into rotated files in `cron_helper/logs/`, one per block of 30
  days, so they are no longer on the website but stay on the server just in
  case. A block older than 90 days is deleted. So the server keeps at most 90
  days of log, and the last 30 are always visible on the page.
- The unit tests use `unittest` and made-up replies instead of the network, so
  they run anywhere. They cover the diff counting, the state of each cell, the
  retries, the log format and rotation, and how the script combines and caches
  the data of a commit.
- When one query fails, the script keeps going, and every error goes into the
  JSON as well as the log. What it shows instead depends on the query:
  - if GitHub or Taskcluster fail for a commit, the row keeps the last good
    data of that commit, and the page marks it "Could not refresh this commit;
    showing the last data". If there is no earlier data, its cells say "Could
    not read this commit", in yellow, with the error in the tooltip, instead of
    looking like a run that did not happen;
  - if build.webkit.org fails, the builds section keeps the last builds, and
    the page says so above them;
  - if the list of wpt.fyi runs fails, the script uses the last list it got,
    but a run missing from it is not called "not uploaded", because it may
    have been uploaded since: the cell says "wpt.fyi not checked" instead;
  - an answer that parses but lacks the fields the script reads gets the same
    fallback as a failed request. The answers are checked where they arrive:
    every entry of a summary file must have a status and two counts, every
    time of a task must parse, and an upload status entry that is not a
    well-formed record is left out, so a bad answer fails close to where it
    came from instead of in the middle of the comparison. A time without a
    zone is taken as UTC, which is what every one of these services uses, so
    any two times can be compared;
  - if a summary file fails, the cell shows "Could not compute the diff", and
    the next run tries again.

  This matters because a partial failure would otherwise look like fresh
  data. For the same reason, a run whose lists could not all be read does not
  clean up the cache, because it would throw away what no row asked for, which
  after a wpt.fyi outage would be every diff.
- It writes `dashboard.json` to a temporary name and then renames it, so the
  browser never reads a half-written file.
- If a run stops because of an unexpected error, which the fallbacks above did
  not handle, the script tries again right away instead of waiting 3 hours for
  cron, up to 3 attempts in all:
  1. the normal run;
  2. again with the same cache, a minute later, in case the error was random;
  3. with an empty cache, keeping the old one aside in `cache.json.broken`, in
     case the cache itself is what makes it fail. If this attempt works, the
     old cache stays there for anyone who wants to see what went wrong.

  If all three fail, the problem is not the cache, so the old cache goes back
  in place, and the script writes the error into `dashboard.json`, keeping the
  data of the last good run, so the page shows it right away. Network errors
  never get this far, because each request is retried and each item falls
  back on its own, so a run with network problems still finishes, only less
  complete. That is also why the cache records are not checked one by one:
  the attempt with an empty cache covers a broken cache, whatever broke it.
  Each attempt starts with a clean list of errors, but the successful one
  carries a warning for each attempt that failed before it.

### The page

- It fetches `data/dashboard.json` and the log without using the browser's
  cached copy, and fetches them again every 5 minutes while the tab stays
  open, keeping what is open, scrolled or expanded.
- It loads nothing from other servers: no fonts, libraries or analytics, so
  visitors' addresses are not sent anywhere else.
- Every link built from data goes through one function that only accepts HTTPS
  links to github.com, community-tc.services.mozilla.com, wpt.fyi and
  build.webkit.org, or HTTP(S) links within the dashboard, and shows anything
  else, `blob:` links included, as plain text. Text from the APIs is always inserted as text, never as HTML.
- The icon (`favicon.svg`, with PNG copies for browsers and devices that do
  not use SVG icons) is the dashboard in miniature: two columns of rows split
  by a dashed line, each row a bar with a colored stripe, mostly green with one
  yellow and one red, because the page exists to spot the run that went wrong.
- A button at the top right switches between the light and the dark theme.
  The page starts in the visitor's system theme, and remembers the choice in
  the browser. That choice is applied by a small script in the `<head>`, before
  the first paint, so the page never flashes in the other theme.
- Each section starts with its most recent rows: 3 for the builds, 4 for the
  nightly runs, and 2 for stable and beta. A "Show 3 more" button under it adds
  3 more rows, on both columns at once so that the rows stay lined up by date,
  until everything in the JSON is shown, and another button goes back to the
  first number.
- It always shows when the data was generated. If the last update failed
  after its three attempts, the "Last update" badge turns red right away
  ("Last update failed at 12:00"), with the error in its tooltip. If the data
  is more than 7 hours old, which means about two missed runs of the script,
  the badge turns red and says so ("Last update 10 hours ago (cron
  stopped?)"), because the script has probably stopped running. That replaces
  what the badge says about the errors and warnings of the last run, so the
  header never shows a green "no errors" next to data that is too old.
- It always shows the script's log, not only when there were errors, in a
  resizable block at the bottom of the page, scrolled to its end, with the
  `ERROR:` lines in red and the `WARNING:` lines in yellow, and a link to the
  raw log. When a reload of the log fails, it keeps the old text and says when
  the reload failed and when that text was loaded.
- Times are shown in the visitor's local time. The dates in tag names are UTC,
  as WPT writes them.
- There is no refresh button, because the page cannot refresh anything by
  itself.
- After an action or a reload, keyboard focus goes back to the top of the page,
  because every change redraws the whole page. Keeping it would mean giving
  every control a stable identity to find again after each redraw, which is not
  worth it for a dashboard that is mostly read.

## Running it

The web server serves `html/`. Cron runs the script every 3 hours, with the
token in the environment and the log in `html/data/`:

```
0 */3 * * * cd /path/to/this/repo && GITHUB_TOKEN=... python3 cron_helper/update_dashboard.py --log html/data/cron_helper.log.txt
```

The script takes its own lock, so a run that starts while the previous one is
still going logs that and stops.
The first run fills the cache with the last `MAX_AGE_DAYS` days, which takes
about 10 minutes and some 550 requests; later runs take about 10 seconds and
some 15 requests. [README.md](README.md) has the full instructions: the token,
the cron job, the web server, and how to try it locally.

The unit tests need no network:

```
cd cron_helper && python3 -m unittest discover -s tests
```

## How runs are scheduled (WPT side)

- The epochs workflow (`.github/workflows/epochs.yml`) runs every 3 hours. It
  calls `tools/ci/epochs_update.sh`, which moves the `epochs/daily` and
  `epochs/weekly` branches forward.
- Each time the target commit changes, it also creates a tag named
  `epochs/daily/YYYY-MM-DD_HHH` or `epochs/weekly/YYYY-MM-DD_HHH`.
- Pushing the branch is what starts Taskcluster; the tag is only a label. In
  `tools/ci/tc/tasks/test.yml`:
  - `trigger-daily` runs nightly;
  - `trigger-weekly` runs stable and beta.
- Each browser and channel is split into 33 chunks: testharness 16, reftest 6,
  test262 8, wdspec 2, crashtest 1.

### Tag names cannot be worked out from the date

- The hour at the end varies. Over 120 days it was anywhere from `00H` to
  `05H`, and it drifts over time.
- Some days have no daily tag. If the daily epoch lands on a commit that
  already has a daily tag (for example 2026-09-07 and 2026-08-10), no new tag
  is created. The branch does not move either, so there is no nightly run that
  day. The dashboard shows that as "no run", not as a failure.
- Instead, the script lists the tags by prefix, which returns each tag with its
  commit, and leaves out, with a warning, any tag whose name is not
  `epochs/{daily,weekly}/YYYY-MM-DD_HHH`:
  `GET https://api.github.com/repos/web-platform-tests/wpt/git/matching-refs/tags/epochs/daily/2026-09`.
  The tags point directly at the commit, so no extra lookup is needed.

## Data flow for each tag

1. **Tag to commit**: use `matching-refs` as above.
2. **Commit to Taskcluster task groups**:
   `GET /repos/web-platform-tests/wpt/commits/<sha>/check-runs?check_name=wpt-decision-task&filter=all`.
   - Each result's `external_id` is a task group ID.
   - `filter=all` is required. A commit can have several decision tasks (one
     each for the master, daily and weekly pushes), and by default GitHub only
     returns the latest one.
3. **Task group to tasks**:
   `GET https://community-tc.services.mozilla.com/api/queue/v1/task-group/<id>/list?limit=1000`,
   following `continuationToken` if there is one.
   - This returns each task's name, state, task group and runs, so a chunk
     that failed and passed when retried shows up as a task with more than one
     run.
   - The master, daily and weekly pushes of a commit each have their own group,
     with different channels. For each browser and channel, the chunks come
     from the newest group that has any of them, taken as a whole, which is
     what happens when a push is retriggered. So an old chunk can never fill a
     hole in a newer run.
   - Tasks are matched to a browser and channel by name, e.g.
     `wpt-webkitgtk_minibrowser-nightly-testharness-3`, so it does not matter
     which push created the group.
   - Each task's description says "chunk number N of M", so a chunk that was
     never scheduled can be detected. A whole suite that one port ran and the
     other did not, on the same commit and channel, counts as missing for the
     other one, even when the other port has no task at all. When both ports
     lack a suite, the dashboard cannot tell a scheduling problem apart from a
     deliberate change in what WPT schedules, as when test262 was added in
     April, so it does not report it.
4. **Log of a failed task**, for the link in its cell:
   `…/api/queue/v1/task/<taskId>/runs/<runId>/artifacts/public/logs/live_backing.log`,
   with the task's last run (this redirects to a direct download). The
   dashboard does not read the reports of the tasks
   (`public/results/wpt_report.json.gz`), because wpt.fyi's summary files
   already have what the diff needs.
5. **Check suite status**:
   `GET /repos/web-platform-tests/wpt/commits/<sha>/check-suites?app_id=40788`,
   40788 being the "Community-TC Integration" app. The suite gives `status`,
   `updated_at` (when it finished, once it has) and `latest_check_runs_count`,
   so counting its check runs needs no other request. This matters because
   wpt.fyi only starts importing once the whole suite has finished, which
   includes every browser on that commit, not only ours. It also starts again
   each time the suite finishes: wpt.fyi handles every `check_suite` event with
   action `completed` (`api/taskcluster/webhook.go`), and a suite that a retried
   task reopened finishes again, so wpt.fyi answers `DUPLICATE` for the runs it
   already has.

   There is one known limitation here. A commit's suite is shared by all its
   pushes, so its `updated_at` and its number of check runs are those of its
   last completion, not of the one that concerned a given run. On a commit
   whose weekly tag came a day after its daily one, a nightly run that was not
   uploaded would be judged from the later suite: its 7-hour clock would start
   from the weekly completion, and the reason could name the weekly suite's
   check runs. It has not happened in 60 days of real data, because every run on
   such commits was uploaded, and the fix, keeping a snapshot of the suite per
   row, is not worth it yet.
6. **Did it reach wpt.fyi**: one request per product and channel lists all
   its runs of the last `MAX_AGE_DAYS` days,
   `GET https://wpt.fyi/api/runs?product=webkitgtk&label=<channel>&label=master&from=<date>&max-count=500`,
   and a commit's run is found in that list by its `full_revision_hash`.
   - Use `webkitgtk` or `wpewebkit` here. The product names with
     `_minibrowser` are not accepted.
   - wpt.fyi answers 404 when there is no run in that window, which is the
     case for stable and beta today; that counts as an empty list.
   - The reply also gives the run's `browser_version`, for example
     `2.55.0 (321964@main)`, and its `results_url`, which is needed for the
     diff.
   - Do not use the "wpt.fyi - …" GitHub check run for this:
     - wpt.fyi only creates it when there is an earlier run to compare with;
     - stable and beta both get the same name, `wpt.fyi - webkitgtk`;
     - its conclusion is never `failure`.
7. **Rejected uploads**: `GET https://wpt.fyi/api/status`.
   - This is wpt.fyi's list of recent uploads, with `stage` (`VALID`,
     `INVALID`, `EMPTY`, `DUPLICATE`, …) and `error`.
   - It has no filters and only returns the latest 500 entries, which is about
     one day, so the script saves the entries for our runs in its cache (see
     "Cache").
   - Our entries have `browser_name` `webkitgtk` or `wpewebkit`, and are
     matched to a commit by `full_revision_hash`. They don't say the channel,
     so on a commit with a weekly tag the stable and beta cells of a port show
     the same entries.

## State of each cell (one browser and channel, for one tag)

The script decides the state when it runs, and the page shows it as it was at
that time.

The rules are checked in this order, and the first one that matches wins, so
a run that is on wpt.fyi is green whatever else happened:

| Stage | Cell |
|---|---|
| on wpt.fyi | green: WebKit version, run link, diff against the previous run |
| the commit could not be read, and there is no earlier data for it | yellow: could not read this commit |
| a decision task is still running, and chunks are missing or there are none yet | yellow: scheduling tasks |
| no tasks for this browser and channel, none expected from the other port, and a decision task failed | red: decision task failed |
| no tasks for this browser and channel, and none expected from the other port | grey: "no run" |
| chunks unscheduled, pending or running | yellow: running (completed/expected) |
| any chunk `failed` / `exception`, or missing | red: the failed and missing chunks, with links (see below) |
| our chunks passed, the suite is not finished | yellow: waiting for the suite to finish |
| our chunks passed, the suite is still not finished 48 hours later | red: suite never finished |
| suite finished, not on wpt.fyi, within the grace period | yellow: uploading |
| grace period over, not in a list of wpt.fyi runs that could not be refreshed | yellow: wpt.fyi not checked |
| grace period over, still not on wpt.fyi | red: not uploaded, with a reason if one can be found (see below) |

The page also fills the days without a daily tag with a grey "No run · No daily
tag this day" row, so a missing day is visible. That includes the days after
the newest tag, up to today once it is 08:00 UTC, because the daily tags are
created between 00:00 and 05:00 UTC.

The suite should always finish, because Taskcluster ends every task: the WPT
tasks have a `deadline` of 24 hours, after which an unfinished task is closed
as `exception`, and a running task is killed after its `maxRunTime` of 2 or 4
hours. So the 48-hour state only covers the rare case where GitHub never
records the suite as finished.

The grace period is 7 hours after the suite finished. Because the script runs
every 3 hours, that means at least two runs of the script have looked for the
run on wpt.fyi before the cell turns red, so a slow upload is never reported as
a failure. In the cases measured, the run appeared on wpt.fyi 26 to 70 minutes
after the suite finished. So a missing upload shows as red between 7 and 10
hours after the suite finished.

When a run is not uploaded, the cell tries to explain why:

- if the suite has more check runs than wpt.fyi can list, it points to the
  known wpt.fyi bug. That limit is 500 today, and it is a single constant in the
  script (`check_run_limit` in `states.Settings`), so it can be changed when
  wpt.fyi deploys a fix. However, the reason is worked out again on every run,
  so after that change the older runs that did hit the limit would say "cause
  unknown" instead; keeping the limit that applied on each date would fix that;
- if the saved `/api/status` entries have one for the run that wpt.fyi did not
  accept, it shows the stage and the error of the latest one. A `VALID` entry
  is not shown as a reason, since it means the upload was accepted;
- otherwise it says the cause is unknown and that wpt.fyi's server logs are
  needed.

Each failed chunk in a cell reads, for example, "test262-2 failed. Logs:
Taskcluster GitHub", and links to three pages: its task on Taskcluster (from
its name), its log, and its own check on GitHub
(`https://github.com/web-platform-tests/wpt/runs/<check run id>`). The script
finds that check with
`GET /repos/web-platform-tests/wpt/commits/<sha>/check-runs?check_name=<task name>&filter=all`,
choosing the check run whose `external_id` is the Taskcluster task ID, and
accepting only a `https://github.com/web-platform-tests/wpt/runs/` address. A
check of the same name for another task, such as a later retry, does not count.
Once found, the link is kept with the commit in the cache. GitHub can list a
check some time after its task ran, so a chunk without one is looked up again
on up to 8 runs of the script, and at most once per run, even when a daily and
a weekly row share its commit.

Every cell that is not green also links to two pages about its whole run:

- its task group on Taskcluster, filtered to the port, for example
  `https://community-tc.services.mozilla.com/tasks/groups/<task group id>#webkitgtk`;
- the checks of its commit on GitHub, filtered to the Community-TC check
  suite, for example
  `https://github.com/web-platform-tests/wpt/commit/<sha>/checks?check_suite_id=<suite id>`.
  These are GitHub checks, not GitHub Actions, because Taskcluster reports to
  GitHub through the "Community-TC Integration" app. The link says "All GitHub
  checks of this commit" because that page shows every browser and channel
  that ran on the commit. For example on a Monday the daily and the weekly tag
  share a commit, so the page of a nightly cell also lists the failures of
  stable and beta.

A task that failed and passed when retried counts as passed, and the cell shows
a small note with the number of chunks that passed after a retry. Taskcluster
retries a task by itself when a run ends in `exception` for a reason outside
the task, such as `worker-shutdown` (the machine was shut down, common with the
preemptible machines WPT uses) or `claim-expired` (the worker stopped
answering), up to `retriesLeft` times. A run that ends in `failed` is not
retried, and a person can also rerun a task by hand. Every run of a task is
kept in its list of runs.

Every cell with chunks that ran, whatever its state, also shows how long the
run took, in three lines:

    Task duration:    2h 21m (wall time)
    Total chunk time: 15h 29m (33 chunks, avg 28m 9s)
    Slowest chunk:    58m 3s (testharness-12)

The task duration is the time from the first run of any chunk starting to the
last one finishing, retries included, which is how long the run took for
someone waiting on it. The total chunk time is the sum of the time of each
chunk's last run, the one that produced its results, so it says what the run
cost, and a run that a retry replaced is not counted. It is much longer than
the task duration, because the chunks run in parallel. Next to it is the
average per chunk, the total divided by the chunks it counts. The slowest
chunk links to its task on Taskcluster. While chunks are still running, the
times count only the chunks that have finished ("12 of 33 chunks"), and the
task duration runs until the data was generated ("so far"). All of it comes
from the `started` and `resolved` times of each run in the task group listing,
so it costs no request. Like every duration on the page, the build times
included, a duration of an hour or more is shown in hours and minutes, and a
shorter one in minutes and seconds.

## Diff against the previous run (green runs only)

### Which runs are compared

- **Previous run**: the one wpt.fyi itself would pick, that is the latest
  earlier run with the same product and channel, labelled `master`. The script
  takes it from the same list of runs it uses to find the run itself (see "Data
  flow for each tag"), as the next older run in it.
- If wpt.fyi has no previous run from the last `MAX_AGE_DAYS` days, the cell
  shows no diff, and says so.
- Otherwise the runs are compared however far apart they are; the "Show 3
  more" button can go back as far as `MAX_AGE_DAYS`. The previous run
  can be older than expected when some uploads were lost, so the header of the
  comparison always shows the date of both runs and the number of days between
  them.
- When there are more than 14 days between the two runs, the header also shows
  a warning. Its tooltip explains that many tests have probably changed in WPT
  over that time, so the numbers should be taken with a grain of salt. Tooltips
  don't show on phones, so the warning can also be tapped to show the same
  text.

### Where the numbers come from

The diff is computed from the two runs' summary files (`results_url`, about
0.9 MB each). For each test file, a summary file gives its status and its
number of passing and total subtests, for example
`{"s": "O", "c": [2040, 2055]}`.

`/api/diff` is not used. It compares subtest by subtest, but it only returns
three numbers per file and not the file's status, so it cannot tell whether a
whole test passes. It also counts a subtest that disappeared (for example
because the file crashed) only in its "total" number, not as a failure.

### How it is counted

- **Only the tests that exist in both runs are compared.** Tests added to or
  removed from WPT between the two runs are only counted, as "not compared".
- **A test passes** when its file ends with status OK or PASS and every subtest
  in it passes. A file that ends in ERROR after reporting 5 passing subtests out
  of 5 does not pass, because the rest of the file never ran.
- **tests +N / −N**: tests that started or stopped passing.
- **subtests +N / −N**: the change in passing subtests inside those same tests.
  The subtests that a crashed or timed-out file no longer reports count in the
  minus, because they stopped passing; the ones it still reports keep their
  result.
- A file whose status changed but whose counts didn't, for example 3/10 OK
  becoming 3/10 ERROR, adds nothing to the totals, but it is listed in the
  expanded table with a "status changed" label, because it can still explain
  what happened. The 85 diffs of the last 60 days have 3,036 of them, about 36
  per pair of runs.
- The summary files only give counts, not subtest names. So when WPT adds or
  removes subtests inside a file that exists in both runs, that cannot be told
  apart from subtests that didn't run. It happened in 3 files with +7 subtests
  in the pair measured, so it is ignored.

### How it is shown

- **The cell**: the WebKit version of the run, as visible as its date, then
  one line with the totals and the five changed directories with the most
  tests that stopped passing (then the most subtests that stopped passing),
  then one line with the tests not compared, the date and version of the
  previous run with the days between them, and for nightly runs the build they
  tested:

  ```
  all            tests +148/-55   subtests +3841/-2748
  html-aam       tests +2/-3      subtests +12/-137
  pointerevents  tests +7/-3      subtests +158/-24
  Not compared: 44 tests added or removed (57 subtests).
  ```

- **Expanded**, with a "Show diff" button in the cell: the versions and dates
  of both runs, links to the full diff on wpt.fyi and to both runs, and a table
  with all the directories, then their subdirectories, then their files, with
  each file's state before and after, for example `2055/2055 OK → ERROR`. This
  is where it shows that a whole file crashed.
- The expanded diff does not open inside the cell, which only has half the
  width of the page, because the table would need sideways scrolling. It opens
  in a row of its own under the row of its day, across both columns, and
  pushes the rows below down. Only one diff is open at a time, and "Hide diff"
  closes it.
- The expanded diff of one pair of runs is about 40 to 170 KB of JSON, so it
  does not go in `dashboard.json`, which would grow to several MB. The script
  writes it to `html/data/diffs/<previous run id>-<run id>.json`, and the page
  loads it when the button is pressed. `dashboard.json` only carries the
  totals and the five directories shown in the cell.
- It also shows the WebKit version of the previous run. When both runs used
  the same build, it says so, because then every change comes from WPT or from
  flaky tests: for nightly runs the cell says "Same bundle as the run of …",
  and the expanded diff says so for every channel. In the pair measured with the same build (webkitgtk nightly,
  09-24 to 09-25), about a hundred tests still changed in each direction.
- **Links**: the run (`https://wpt.fyi/results/?run_id=<id>`), the previous
  run, and the full diff on wpt.fyi
  (`https://wpt.fyi/results/?diff&filter=ADC&run_id=<prev>&run_id=<this>`).

The counting is in `cron_helper/diff.py`, and the grouping by subdirectory for
the expanded table is done by the page.

## Nightly packaging bots

The nightly WPT runs don't build WebKit. They download the MiniBrowser bundle
that build.webkit.org builds every night, from `webkitgtk.org/built-products/`
and `wpewebkit.org/built-products/` (`tools/wpt/browser.py`). When that build
fails, no new bundle is published, and the next nightly WPT run tests the old
bundle again. So a failed build is also a problem for the dashboard, because it
means there are no fresh nightlies to test.

For example, the GTK builds 749 and 750 (2026-09-23 and 09-24) failed in
`test-minibrowser-bundle`, before `upload-minibrowser-bundle-via-sftp`. So the
WPT runs of 09-23, 09-24 and 09-25 all tested the same build, `321570@main`.

The two bots are `GTK-Linux-64bit-Release-Packaging-Nightly` and
`WPE-Linux-64bit-Release-Packaging-Nightly`. They build once a day, starting at
about 05:00 UTC and finishing between 2 and 8 hours later, so the script's
3-hour cycle is more than enough.

### Data flow

The Buildbot REST API of build.webkit.org (Buildbot v4.3.0) needs no token.

1. **The latest builds of a bot**, with the WebKit revision each one built, in
   a single request (about 3 KB for 7 builds):
   `GET https://build.webkit.org/api/v2/builders/<builder>/builds?order=-number&limit=<n>&property=identifier`.
   The first run asks for `MAX_AGE_DAYS` + 10 builds, enough to cover the
   window; later runs ask for the 10 newest ones, because finished builds are
   in the cache.
   - Each build gives `number`, `buildid`, `complete`, `results`,
     `state_string`, `started_at` and `complete_at`.
   - `properties.identifier` is the revision, for example `322032@main`.
   - Asking without `limit` returns every build of the bot, about 286 KB,
     which is why the script always asks for a limited number.
2. **The failed steps of a failed build**:
   `GET https://build.webkit.org/api/v2/builds/<buildid>/steps`, keeping the
   finished steps whose `results` is not success, warnings or skipped. Each
   failed step has a `stdio` log.
3. **Links**:
   - the build:
     `https://build.webkit.org/#/builders/<builder>/builds/<number>`;
   - the log of a failed step:
     `https://build.webkit.org/#/builders/<builder>/builds/<number>/steps/<step number>/logs/stdio`.

A build's `results` follows Buildbot's codes: 0 success, 1 warnings,
2 failure, 3 skipped, 4 exception, 5 retry, 6 cancelled. Success and warnings
count as green, a build still running as yellow, and everything else as red,
because in all those cases no bundle was uploaded.

### Cache

A finished build never changes, so the script keeps each one in its cache, with
its failed steps, and never asks for it again. The cache also records when a
bot's history has been read in full: until then, for example after a first
read that failed halfway, the script asks for the whole window again, and it
does the same when the newest builds no longer overlap the cached ones. That
record is cleared before such a backfill starts and set again only when it
succeeds, so a backfill that fails halfway is done again on the next run. Each run of the script only
makes the one request per bot for the latest builds, plus one request for the
steps of each newly failed build. Builds that started more than `MAX_AGE_DAYS`
ago are dropped.

### How it is shown

- The builds of each bot, newest first (3 at first, more with "Show 3
  more"), lined up by the UTC date they started on, each in green, red or
  yellow, with its number, revision, and when it finished and how long it
  took, or when it started if it is still building.
  Each build links to its page on build.webkit.org, and a failed build also
  lists its failed steps, each linking to its log.
- Each green nightly WPT cell says which build it tested, found by matching the
  revision in wpt.fyi's `browser_version` (for example `2.55.0 (321964@main)`)
  with the build's `identifier`. Only a build that passed published its
  bundle, and only one that finished before the run started can be the one it
  tested, so the cell names the latest build that meets both. A version
  without a revision names no build. The cache keeps builds for 3 days more
  than the window, without showing them, so that the oldest runs of the window
  can still name the build they tested. When a run tested
  the same bundle as the run before it, the cell says so with a warning
  instead, because a failed build is the usual reason. When no build in the
  cache meets both, the cell doesn't name one.
- Only the nightly WPT runs use these bundles. The stable and beta runs use
  bundles from uploaders that are still private, so the dashboard ignores them
  for now. When they become public, the code and this design will be updated
  to show them too.

## Layout

The page is split in two columns: WPE WebKit on the left and WebKitGTK on the
right. From top to bottom:

1. **Header**, across both columns, in two lines:
   - the first line is about the dashboard's own data: the title, when the
     data was generated, and the "Last update" badge, which links to the log.
     It shows the errors and warnings of the script's last run, or turns red
     when the data is more than 7 hours old. The theme button sits at its
     right end;
   - the second line is about the runs, with badges that each take the
     latest build or run whose outcome is known, leaving out the ones still in
     progress:
     - "Last nightlies:" has two badges, one for each half of the nightly
       chain. **builds**: the latest finished build of each bot, "builds
       passed", or "GTK build failed", "WPE build failed" or "builds failed".
       **WPT runs**: "WPT runs uploaded", or for example "GTK WPT run failed",
       "WPT runs not uploaded" or "WPE WPT run failed, GTK not uploaded";
     - "Last stable WPT:" and "Last beta WPT:" have one badge each, with the
       same wording but shorter, because the label already says what they
       are about: "uploaded", "not uploaded", or "WPE failed, GTK not
       uploaded".

   The WPT badges say "uploaded" rather than "passed", because a green run can
   still have failing tests; green means the run reached wpt.fyi. A badge only
   speaks for both ports when both have a known result: when one of them has
   none yet, it names it, for example "WPE WPT run uploaded, GTK unknown", in
   grey, or in red if the other one failed. Hovering a
   badge shows which build or run it is about, and clicking it goes to that
   section.
2. **Nightly bundles**, in each column: the builds of that port's packaging
   bot on build.webkit.org. This block has a tinted background and a
   frame of its own, so it reads as clearly separate from the WPT runs below
   it: it is the input that the WPT runs test.
3. **Nightly WPT runs**, in each column: one cell per daily tag.
4. **Stable and beta WPT runs**, in each column: one row per weekly tag, with
   the stable and beta cells side by side.
5. **The log**, across both columns.

The rows of both columns line up by date, with the dates (and the hour of the
tag) written once, down the middle. So each row holds the same day for both
ports, and comparing them is a glance across the row. When one cell is taller,
for example with an expanded diff, the other side's row grows with it.

Each box shows its state in three ways at once: a wide stripe on its left in
green, yellow, red or grey, a light tint of the same color, and a symbol before
its headline (✓, ◔, ✕ or –). So a failed run stands out in both themes, and the
state can be read without relying on color alone.

On a narrow screen both ports stay side by side, with the date above each row.
Each day is one element of its own, so moving its date above its two cells
cannot shift the cells of the other days.


## The JSON file

`dashboard.json`, with example values:

```json
{
  "generated_at": "2026-09-29T18:31:04Z",
  "duration_seconds": 22.0,
  "requests": {"buildbot": 2, "github": 21, "taskcluster": 2, "wptfyi": 7},
  "errors": ["…"],
  "warnings": ["…"],
  "settings": {"max_age_days": 60, "stale_after_hours": 7, "upload_grace_hours": 7,
               "suite_timeout_hours": 48, "check_run_limit": 500},
  "ports": [{"key": "wpe", "label": "WPE WebKit", "product": "wpewebkit",
             "builder": "WPE-Linux-64bit-Release-Packaging-Nightly"}, "…"],
  "builds": {
    "wpe": {"builder": "WPE-Linux-64bit-Release-Packaging-Nightly", "stale": false, "builds": [
      {"number": 746, "buildid": 5830797, "identifier": "322032@main", "complete": true,
       "result": "success", "passed": true, "state_string": "build successful",
       "started_at": "…", "complete_at": "…", "failed_steps": []}]},
    "gtk": {"…": "…"}
  },
  "daily": [
    {
      "tag": "epochs/daily/2026-09-27_05H", "date": "2026-09-27", "hour": "05H",
      "sha": "647d3bdf13…", "task_groups": ["UuEJqLHnR-GWszLd0ZFaog", "…"], "stale": false,
      "suite": {"id": 98200970712, "status": "completed", "conclusion": "success",
                "updated_at": "2026-09-27T09:54:38Z", "check_runs": 412},
      "cells": {
        "wpe": {
          "nightly": {
            "state": "uploaded", "color": "green",
            "chunks": {"total": 33, "expected": 33, "completed": 33, "unfinished": 0, "retried": 0,
                       "failed": [], "missing": [], "first_started": "…", "last_resolved": "…",
                       "run_seconds": 33120, "timed_chunks": 33,
                       "slowest": {"name": "…", "task_id": "…", "seconds": 3483, "task_url": "…"},
                       "task_group_id": "…", "task_group_url": "…"},
            "wptfyi": {
              "run_id": 5193370485129216, "time_start": "…", "created_at": "…",
              "browser_version": "2.55.0 (321964@main)", "identifier": "321964@main",
              "previous": {"run_id": 5095979987763200, "time_start": "…",
                           "browser_version": "2.55.0 (321873@main)", "identifier": "321873@main"},
              "days": 1, "same_build": false,
              "tested_build": {"number": 744, "started_at": "…"},
              "diff": {"totals": {"tests_fixed": 148, "tests_broken": 55, "…": "…"},
                       "top_directories": ["…"],
                       "detail": "diffs/5095979987763200-5193370485129216.json"}
            }
          }
        },
        "gtk": {"nightly": {"…": "…"}}
      }
    }
  ],
  "weekly": ["the same, with a \"stable\" and a \"beta\" cell for each port"]
}
```

The other states carry what explains them:

- `failed`: each failed chunk in `chunks.failed`, with `name`, `state`,
  `reason`, `task_url`, `log_url` and `github_url`, and the missing ones in
  `chunks.missing`;
- `uploading`: `since`, when the suite finished, and `red_at`, when the cell
  turns red;
- `not_uploaded`: `since` and `reason`, which is `check_run_limit` (with
  `check_runs` and `limit`), `wptfyi_status` (with the saved `entries`) or
  `unknown`;
- `suite_never_finished`: `since`, when our last chunk finished.

When all three attempts of an update fail, `dashboard.json` keeps the data of
the last good run, with one more field, `failed_update`, holding `at` and
`error`. The next run that works writes the file anew, without it. If there
has been no good run yet, for example on a new server, the script writes no
`dashboard.json` at all, because the page already explains a missing one and
shows the log with the error, while a file with only the failure would break
it.

The file of an expanded diff, `diffs/<previous run id>-<run id>.json`, has the
`totals` and every changed directory, with its counts, its changed files
(each with its state before and after), and its added and removed tests.

## Cache

Most of the data never changes once a run is over, so the script keeps it in
`cron_helper/cache/cache.json` and only asks again for what is still moving:

- **commits**: the suite, decision tasks, chunks and tags of each commit. A
  commit is final, and never fetched again, once none of its chunks is running
  and all its decision tasks have finished, and either its suite finished more
  than 24 hours ago and its newest tag or decision task is more than 24 hours
  old, or its newest tag or decision task is more than 72 hours old, because
  Taskcluster ends every task within a day. Until then it is fetched again on
  every run, but only once per run, even when a daily and a weekly tag share
  it, and it is judged with all its tags. A decision task counts as finished when GitHub says so, or once
  it started more than 24 hours ago, because Taskcluster ends every task
  within its deadline; otherwise a check that GitHub left in progress by
  mistake would keep the commit waiting for 60 days. A new tag on a final
  commit makes it fetched again, because a
  weekly tag can land on a commit that had a daily tag days before and start
  its stable and beta tasks on it. However, a push retriggered by hand after a
  commit became final is not noticed, because nothing the script reads says
  so; the commit keeps the state it had;
- **tags**: the tags of each month, final once the month is over;
- **the GitHub check of each failed chunk**, kept with its commit;
- **builds**: every finished build, with its failed steps, and whether each
  bot's history has been read in full;
- **diffs**: the summary of the diff between two run IDs, which never changes,
  with its detail file in `html/data/diffs/`;
- **wpt.fyi upload statuses**: our entries of `/api/status`, kept after wpt.fyi
  drops them from its list;
- **the last lists of wpt.fyi runs**, used only when wpt.fyi fails to answer.

The commits and diffs no longer used by any row, the diff files nothing points
to, and the builds and upload statuses older than `MAX_AGE_DAYS` are removed.
The tag lists of past months stay, but they are small. The cache has a version
number, and when its format changes, or its structure is broken, the script
starts with an empty one and logs a warning. The records inside it are not
checked one by one: if a broken record makes a run stop, the third attempt,
with an empty cache, recovers from it (see "The script").

So most runs of the script only need a handful of requests.

## Known wpt.fyi upload bug (as of 2026-09-29)

wpt.fyi's Taskcluster webhook lists the check runs of the finished check suite
25 at a time. It gives up after 20 pages, with the error "more than 500
CheckRuns returned for CheckSuite", so nothing from that suite is imported
(`api/taskcluster/webhook.go`, `ListCheckRuns`).

Since WPT commit `ac3c70021a6` (2026-04-15) added test262 chunks to Taskcluster,
every commit with a weekly tag has more than 500 check runs. So:

- stable and beta have not reached wpt.fyi since 2026-04-13 (the 04-13 suite
  had 454 check runs; the 04-20 suite had 590);
- nightly is also lost when a daily and a weekly tag share a commit (09-21,
  09-28).

Upstream PR web-platform-tests/wpt.fyi#5127 raises the limit to 1000.

Separately, webkitgtk nightly on 09-27 was not uploaded, even though that suite
had only 412 check runs and all its chunks passed. The cause is not known yet.
The dashboard also showed that two other missing runs were not upload
problems: webkitgtk nightly on 09-22 and wpewebkit nightly on 09-28 each had a
failed chunk.

## Settings and later work

The settings are constants at the top of the files:

- `update_dashboard.py`: `ATTEMPTS` 3 and `RETRY_PAUSE_SECONDS` 60,
  `BUILD_EXTRA_DAYS` 3,
  `MAX_AGE_DAYS` 60, `STALE_AFTER_HOURS` 7,
  `FINAL_AFTER_SUITE_HOURS` 24, `FINAL_AFTER_TAG_HOURS` 72,
  `DECISION_DEADLINE_HOURS` 24,
  `FIRST_BUILD_FETCH` 70, `BUILD_FETCH` 10 and `CHECK_RUN_LOOKUPS` 8;
- `states.py` (`Settings`): `upload_grace_hours` 7, `suite_timeout_hours` 48
  and `check_run_limit` 500;
- `fetch.py`: 3 attempts, 10 and 30 seconds between them, 90-second timeout;
- `logs.py`: 30 days on the web, 90 days in total;
- `dashboard.js`: reload every 5 minutes, 3, 4 and 2 rows at first, 3 more per
  click, the warning above 14 days between runs, and 08:00 UTC as the hour
  after which today's daily tag should exist.

Notes and later work:

- `MAX_AGE_DAYS` is 60. For stable and beta, the latest runs on wpt.fyi are
  from 2026-04-13, so the first stable and beta runs after the upload bug is
  fixed will show no diff, and the ones after that will.
- Later: marking flaky tests, by looking at each test's history over several
  runs.
