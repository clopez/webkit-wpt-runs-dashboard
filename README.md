# WebKit WPT runs (Linux)

**Live dashboard: https://people.igalia.com/clopez/webkit-wpt-runs-dashboard**

A dashboard for the runs of web-platform-tests (WPT) on the Linux ports of
WebKit, WebKitGTK and WPE WebKit. WPT runs its test suite on Taskcluster for
both ports every night (nightly) and every week (stable and beta), and when all
the chunks of a run pass, wpt.fyi imports its results. Finding out whether a
run worked used to mean going through GitHub, Taskcluster and wpt.fyi by hand,
so this page shows it all at once. For each port it shows:

- the nightly MiniBrowser bundles that build.webkit.org builds, since those are
  what the nightly runs test;
- each nightly, stable and beta run, in green only when it reached wpt.fyi, and
  otherwise with the reason and links to the failed chunks and their logs;
- what changed since the previous run on wpt.fyi, by directory and by file.

It has two parts. A Python script runs from cron every 3 hours, gathers the
data and writes it as JSON, and a static page draws that JSON. So the page
loads at once, and visitors never query GitHub, Taskcluster or wpt.fyi
themselves. [DESIGN.md](DESIGN.md) explains how every piece works and why.


## What it needs

- **Python 3.11 or newer**, with only its standard library, so nothing has to
  be installed with pip.
- **A GitHub token**, which only has to read public data. The script refuses to
  run without one, because without a token GitHub allows only 60 requests per
  hour, and with one it allows 5,000, which leaves plenty of room: a normal run
  makes about 15 requests, and the first one about 550.
- **A web server** that serves static files. There is no server-side code.


## Setting it up

In the examples, `/path/to/this/repo` is the directory where you cloned this
repository.

### 1. The GitHub token

Create a token on GitHub (Settings → Developer settings → Personal access
tokens). A fine-grained token with "Public repositories (read-only)" access and
no permissions is enough, and so is a classic token with no scopes. Keep it in
a file only you can read, outside the repository:

```sh
mkdir -p ~/.config/webkit-wpt-runs-dashboard
install -m 600 /dev/null ~/.config/webkit-wpt-runs-dashboard/github-token
$EDITOR ~/.config/webkit-wpt-runs-dashboard/github-token
```

The script only sends the token over HTTPS to `api.github.com`. It also checks
that the token only has the characters GitHub tokens use, and refuses to run
otherwise, because a token HTTP rejects would end up quoted in error messages,
which are public.

### 2. The first run

Run the script once by hand before adding it to cron. The first run fills its
cache with the last 60 days, so it takes about 10 minutes:

```sh
cd /path/to/this/repo
GITHUB_TOKEN=$(cat ~/.config/webkit-wpt-runs-dashboard/github-token) \
    python3 cron_helper/update_dashboard.py --log html/data/cron_helper.log.txt
```

It writes `html/data/dashboard.json` for the page, one file per diff in
`html/data/diffs/`, and its log in `html/data/cron_helper.log.txt`. It also
keeps a cache in `cron_helper/cache/`, and the log lines older than 30 days in
`cron_helper/logs/`. None of these are in git. Later runs take about 10
seconds.

### 3. The cron job

Add this line with `crontab -e`, so the script runs every 3 hours:

```
0 */3 * * * cd /path/to/this/repo && GITHUB_TOKEN=$(cat $HOME/.config/webkit-wpt-runs-dashboard/github-token) python3 cron_helper/update_dashboard.py --log html/data/cron_helper.log.txt
```

The script takes a lock itself, in its cache directory, so a run that starts
while the previous one is still going just logs that and stops, whether cron
or you started it. The output directories don't need to be given: the script
finds `html/data/` and its cache from where it lives. With `--log`, the
script writes nothing to stdout or stderr, so cron sends no emails: everything
goes to the log, which the page shows at its bottom. If a run stops because of
an unexpected error, the script tries twice more right away, the last time with
an empty cache, so a single failure doesn't have to wait 3 hours for the next
run.

### 4. The web server

The web server only has to serve `html/` as static files. For nginx:

```nginx
server {
    listen 80;
    server_name wpt-dashboard.example.org;
    root /path/to/this/repo/html;
    index index.html;
}
```

For Apache:

```apache
<VirtualHost *:80>
    ServerName wpt-dashboard.example.org
    DocumentRoot /path/to/this/repo/html
    <Directory /path/to/this/repo/html>
        Require all granted
    </Directory>
</VirtualHost>
```

Two things to check:

- The user the web server runs as must be able to read `html/` and everything
  the script writes in `html/data/`, and reach them, which may need
  `chmod o+x` on the directories above them, such as your home directory.
- The page reloads its data every 5 minutes and always asks for a fresh copy,
  so no special caching settings are needed.

Only `html/` must be served. The cache and the rotated logs stay out of it on
purpose, and the token is never written anywhere by the script.

### 5. Updating it

To update the dashboard, pull the new version into the repository. The cron
job uses it on its next run. When a new version changes the format of the
cache, the script notices, starts with an empty one and logs a warning, so that
run takes about 10 minutes again.


## Trying it locally

To try a change without touching the data of the real dashboard, write the
output to a temporary directory and serve a copy of the page:

```sh
cd /path/to/this/repo
GITHUB_TOKEN=$(cat ~/.config/webkit-wpt-runs-dashboard/github-token) \
    python3 cron_helper/update_dashboard.py \
    --log /tmp/wpt-dashboard/data/cron_helper.log.txt \
    --data-dir /tmp/wpt-dashboard/data \
    --cache-dir /tmp/wpt-dashboard/cache \
    --log-archive-dir /tmp/wpt-dashboard/logs
cp html/*.html html/*.js html/*.css html/*.svg html/*.png /tmp/wpt-dashboard/
python3 -m http.server 8000 --bind 127.0.0.1 --directory /tmp/wpt-dashboard
```

Then open http://127.0.0.1:8000. The page needs to be served over HTTP, because
browsers don't let a page opened from a file read its JSON.


## Running the tests

The unit tests use `unittest` and made-up answers instead of the network, so
they need no token and run anywhere:

```sh
cd /path/to/this/repo/cron_helper
python3 -m unittest discover -s tests
```

They cover the state of each run, the diff, the cache, the retries and the log.
The page has no automated tests; check it in a browser after changing it, at
full width and at a phone's width.


## When something goes wrong

- The **"Last update"** badge at the top turns red when the last update failed,
  or when the data is more than 7 hours old, which usually means cron stopped
  running the script.
- The **log** at the bottom of the page, and in
  `html/data/cron_helper.log.txt`, has every run, with `ERROR:` and `WARNING:`
  lines in red and yellow.
- If a run only worked with an empty cache, the old cache is kept in
  `cron_helper/cache/cache.json.broken`, in case you want to see what was wrong
  with it.
