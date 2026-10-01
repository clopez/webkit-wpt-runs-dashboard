import collections
import datetime
import json
import logging
import pathlib
import tempfile
import types
import unittest
from unittest import mock

import sources
import update_dashboard
from fetch import FetchError

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.timezone.utc)
SHA = "647d3bdf133159739b57cfb7afa0be3f5d76b9db"
TAG = {"name": "epochs/daily/2026-09-27_05H", "date": "2026-09-27", "hour": "05H", "sha": SHA}


class FakeFetcher:
    def __init__(self, replies):
        self.replies = replies
        self.request_counts = collections.Counter()
        self.urls = []

    def get_json(self, url, service):
        self.urls.append(url)
        self.request_counts[service] += 1
        if callable(self.replies):
            return self.replies(url)
        for fragment, reply in self.replies.items():
            if fragment in url:
                if isinstance(reply, Exception):
                    raise reply
                return reply
        raise FetchError(f"{url}: no reply in the test", status=404)


def chunk_entry(browser, name, number, of, state="completed"):
    return {"task": {"metadata": {"name": f"wpt-{browser}-nightly-{name}-{number}",
                                  "description": f'A subset of WPT\'s "{name}" tests (chunk number {number} of {of}), run in the nightly release.'}},
            "status": {"taskId": f"{browser}-{name}-{number}", "taskGroupId": "group1", "state": state,
                       "runs": [{"runId": 0, "state": state, "reasonResolved": state, "resolved": "2026-09-27T06:00:00.000Z"}]}}


def run(run_id, sha, day, identifier):
    return {"id": run_id, "sha": sha, "time_start": f"2026-09-{day}T05:00:00Z", "created_at": f"2026-09-{day}T10:00:00Z",
            "browser_version": f"2.55.0 ({identifier})", "results_url": f"https://storage.example/{run_id}.json.gz"}


class UpdaterTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.data_dir = pathlib.Path(directory.name)
        self.logger = logging.getLogger("test_update")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False

    def replies(self, gtk_state="completed"):
        return {
            "check_name=wpt-webkitgtk_minibrowser-nightly-reftest-1": {"check_runs": [
                {"external_id": "webkitgtk_minibrowser-reftest-1", "started_at": "2026-09-27T05:20:00Z", "html_url": "https://github.com/web-platform-tests/wpt/runs/42"}]},
            f"commits/{SHA}/check-suites": {"check_suites": [{"id": 1, "status": "completed", "conclusion": "success", "created_at": "2026-09-26T18:40:52Z",
                                                              "updated_at": "2026-09-27T09:54:38Z", "latest_check_runs_count": 412}]},
            f"commits/{SHA}/check-runs": {"check_runs": [{"external_id": "group1", "started_at": "2026-09-27T05:16:50Z", "status": "completed", "conclusion": "success"}]},
            "task-group/group1/list": {"tasks": [
                chunk_entry("wpewebkit_minibrowser", "reftest", 1, 1),
                chunk_entry("webkitgtk_minibrowser", "reftest", 1, 1, gtk_state),
                {"task": {"metadata": {"name": "wpt-firefox-nightly-reftest-1", "description": ""}}, "status": {"taskId": "f", "state": "failed", "runs": []}},
            ]},
            "storage.example/2.json.gz": {"/a/1.html": {"s": "O", "c": [1, 1]}},
            "storage.example/3.json.gz": {"/a/1.html": {"s": "E", "c": [0, 0]}},
        }

    def updater(self, fetcher, cache=None):
        return update_dashboard.Updater(fetcher, self.logger, cache if cache is not None else {"version": update_dashboard.CACHE_VERSION}, self.data_dir, NOW)

    def row(self, updater, runs, builds=None):
        wptfyi_runs = {f"{port['product']}-{channel}": [] for port in update_dashboard.PORTS for channel in update_dashboard.ALL_CHANNELS}
        wptfyi_runs.update(runs)
        if builds:
            updater.all_builds = {port: section["builds"] for port, section in builds.items()}
        return updater.row(TAG, ["nightly"], wptfyi_runs, {})

    def test_a_row_combines_taskcluster_github_and_wptfyi(self):
        updater = self.updater(FakeFetcher(self.replies(gtk_state="failed")))
        builds = {"wpe": {"builds": [{"number": 744, "identifier": "321964@main", "passed": True,
                                      "started_at": "2026-09-26T05:03:00Z", "complete_at": "2026-09-26T12:08:00Z"}]}, "gtk": {"builds": []}}
        row = self.row(updater, {"wpewebkit-nightly": [run(3, SHA, 27, "321964@main"), run(2, "older", 26, "321873@main")]}, builds)

        wpe = row["cells"]["wpe"]["nightly"]
        self.assertEqual(wpe["state"], "uploaded")
        self.assertEqual(wpe["wptfyi"]["previous"]["run_id"], 2)
        self.assertEqual(wpe["wptfyi"]["days"], 1)
        self.assertFalse(wpe["wptfyi"]["same_build"])
        self.assertEqual(wpe["wptfyi"]["tested_build"]["number"], 744)
        self.assertEqual(wpe["wptfyi"]["diff"]["totals"]["tests_broken"], 1)
        self.assertTrue((self.data_dir / wpe["wptfyi"]["diff"]["detail"]).exists())

        gtk = row["cells"]["gtk"]["nightly"]
        self.assertEqual(gtk["state"], "failed")
        self.assertEqual(gtk["chunks"]["failed"][0]["task_url"], "https://community-tc.services.mozilla.com/tasks/webkitgtk_minibrowser-reftest-1")
        self.assertEqual(gtk["chunks"]["task_group_url"], "https://community-tc.services.mozilla.com/tasks/groups/group1#webkitgtk")
        self.assertEqual(gtk["chunks"]["failed"][0]["github_url"], "https://github.com/web-platform-tests/wpt/runs/42")

    def test_the_github_link_of_a_failed_chunk_is_looked_up_only_once(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        first = FakeFetcher(self.replies(gtk_state="failed"))
        self.row(self.updater(first, cache), {})
        self.assertEqual(sum("check_name=wpt-webkitgtk" in url for url in first.urls), 1)
        cache["commits"][SHA]["final"] = False
        second = FakeFetcher(self.replies(gtk_state="failed"))
        row = self.row(self.updater(second, cache), {})
        self.assertEqual(sum("check_name=wpt-webkitgtk" in url for url in second.urls), 0)
        self.assertEqual(row["cells"]["gtk"]["nightly"]["chunks"]["failed"][0]["github_url"], "https://github.com/web-platform-tests/wpt/runs/42")

    def test_a_final_commit_from_an_older_cache_gets_its_github_links(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.row(self.updater(FakeFetcher(self.replies(gtk_state="failed")), cache), {})
        del cache["commits"][SHA]["chunks"]["webkitgtk_minibrowser-nightly"]["failed"][0]["github_url"]
        later = FakeFetcher({"check_name=": self.replies()["check_name=wpt-webkitgtk_minibrowser-nightly-reftest-1"]})
        row = self.row(self.updater(later, cache), {})
        self.assertEqual(row["cells"]["gtk"]["nightly"]["chunks"]["failed"][0]["github_url"], "https://github.com/web-platform-tests/wpt/runs/42")

    def test_a_final_commit_is_not_fetched_again(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.row(self.updater(FakeFetcher(self.replies()), cache), {})
        self.assertTrue(cache["commits"][SHA]["final"])
        second = FakeFetcher({})
        row = self.row(self.updater(second, cache), {})
        self.assertEqual(second.urls, [])
        self.assertEqual(row["cells"]["gtk"]["nightly"]["state"], "not_uploaded")

    def test_a_commit_with_running_chunks_is_never_final(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.row(self.updater(FakeFetcher(self.replies(gtk_state="running")), cache), {})
        self.assertFalse(cache["commits"][SHA]["final"])

    def test_a_new_tag_on_a_final_commit_fetches_it_again(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.row(self.updater(FakeFetcher(self.replies()), cache), {})
        self.assertTrue(cache["commits"][SHA]["final"])
        weekly = {**TAG, "name": "epochs/weekly/2026-09-29_05H", "date": "2026-09-29"}
        fetcher = FakeFetcher(self.replies())
        updater = self.updater(fetcher, cache)
        wptfyi_runs = {f"{port['product']}-{channel}": [] for port in update_dashboard.PORTS for channel in update_dashboard.ALL_CHANNELS}
        updater.row(weekly, ["stable", "beta"], wptfyi_runs, {})
        self.assertTrue(any("check-suites" in url for url in fetcher.urls))
        self.assertEqual(cache["commits"][SHA]["tags"], ["epochs/daily/2026-09-27_05H", "epochs/weekly/2026-09-29_05H"])
        self.assertFalse(cache["commits"][SHA]["final"])

    def test_a_missing_run_is_not_called_not_uploaded_when_wptfyi_could_not_be_listed(self):
        fetcher = FakeFetcher({"api/runs": FetchError("HTTP 503", status=503), **self.replies()})
        updater = self.updater(fetcher)
        runs = updater.wptfyi_runs()
        row = updater.row(TAG, ["nightly"], runs, {})
        self.assertEqual(row["cells"]["wpe"]["nightly"]["state"], "wptfyi_unchecked")

    def test_the_tested_build_passed_and_finished_before_the_run(self):
        builds = {"wpe": {"builds": [
            {"number": 746, "identifier": "321964@main", "passed": False, "started_at": "2026-09-26T20:00:00Z", "complete_at": "2026-09-26T23:00:00Z"},
            {"number": 745, "identifier": "321964@main", "passed": True, "started_at": "2026-09-26T05:00:00Z", "complete_at": "2026-09-26T12:00:00Z"},
            {"number": 747, "identifier": "321964@main", "passed": True, "started_at": "2026-09-27T05:00:00Z", "complete_at": "2026-09-27T12:00:00Z"},
        ]}, "gtk": {"builds": []}}
        row = self.row(self.updater(FakeFetcher(self.replies())), {"wpewebkit-nightly": [run(3, SHA, 27, "321964@main")]}, builds)
        self.assertEqual(row["cells"]["wpe"]["nightly"]["wptfyi"]["tested_build"]["number"], 745)

    def test_a_running_decision_task_keeps_a_commit_from_being_final(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        replies = self.replies()
        replies[f"commits/{SHA}/check-runs"] = {"check_runs": [
            {"external_id": "group1", "started_at": "2026-09-27T05:16:50Z", "status": "completed", "conclusion": "success"},
            {"external_id": "group2", "started_at": "2026-09-29T08:00:00Z", "status": "in_progress", "conclusion": None}]}
        replies = {"task-group/group2/list": {"tasks": []}, **replies}
        self.row(self.updater(FakeFetcher(replies), cache), {})
        self.assertFalse(cache["commits"][SHA]["final"])

    def test_a_decision_task_left_in_progress_by_github_counts_as_finished_after_its_deadline(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        replies = self.replies()
        replies[f"commits/{SHA}/check-runs"] = {"check_runs": [
            {"external_id": "group1", "started_at": "2026-09-17T05:16:50Z", "status": "in_progress", "conclusion": None}]}
        row = self.row(self.updater(FakeFetcher(replies), cache), {})
        self.assertNotEqual(row["cells"]["gtk"]["nightly"]["state"], "scheduling")
        self.assertTrue(cache["commits"][SHA]["final"])

    def test_a_port_without_tasks_is_waiting_while_a_decision_task_runs(self):
        replies = self.replies()
        replies["task-group/group1/list"] = {"tasks": [chunk_entry("wpewebkit_minibrowser", "reftest", 1, 1)]}
        row = self.row(self.updater(FakeFetcher(replies)), {})
        self.assertEqual(row["cells"]["gtk"]["nightly"]["state"], "failed")
        self.assertEqual(row["cells"]["gtk"]["nightly"]["chunks"]["missing"], ["reftest-1"])
        replies[f"commits/{SHA}/check-runs"] = {"check_runs": [{"external_id": "group1", "started_at": "2026-09-29T08:00:00Z", "status": "in_progress", "conclusion": None}]}
        row = self.row(self.updater(FakeFetcher(replies)), {})
        self.assertEqual(row["cells"]["gtk"]["nightly"]["state"], "scheduling")

    def test_a_bad_summary_file_gives_a_diff_error_instead_of_stopping(self):
        replies = self.replies()
        replies["storage.example/2.json.gz"] = {"/a/test": {"s": "O"}}
        with self.assertLogs("test_update", level="ERROR"):
            row = self.row(self.updater(FakeFetcher(replies)), {"wpewebkit-nightly": [run(3, SHA, 27, "321964@main"), run(2, "older", 26, "321873@main")]})
        self.assertIn("error", row["cells"]["wpe"]["nightly"]["wptfyi"]["diff"])

    def test_bad_upload_status_entries_are_left_out(self):
        fetcher = FakeFetcher({"api/status": [None, {"id": 1, "browser_name": "webkitgtk", "created": "not a time"},
                                              {"id": 2, "browser_name": "webkitgtk", "browser_version": "", "full_revision_hash": SHA,
                                               "stage": "INVALID", "error": "x", "created": "2026-09-27T10:00:00Z", "updated": ""}]})
        statuses = self.updater(fetcher).upload_statuses()
        self.assertEqual([entry["id"] for entry in statuses[(SHA, "webkitgtk")]], [2])

    def test_a_shared_commit_uses_one_github_lookup_per_run(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        replies = self.replies(gtk_state="failed")
        del replies["check_name=wpt-webkitgtk_minibrowser-nightly-reftest-1"]
        replies = {"check_name=wpt-webkitgtk": {"check_runs": []}, **replies}
        weekly = {**TAG, "name": "epochs/weekly/2026-09-27_05H"}
        for _ in range(2):
            updater = self.updater(FakeFetcher(replies), cache)
            wptfyi_runs = {f"{port['product']}-{channel}": [] for port in update_dashboard.PORTS for channel in update_dashboard.ALL_CHANNELS}
            builds = {"wpe": {"builds": []}, "gtk": {"builds": []}}
            updater.row(TAG, ["nightly"], wptfyi_runs, {})
            updater.row(weekly, ["stable", "beta"], wptfyi_runs, {})
        self.assertEqual(cache["commits"][SHA]["chunks"]["webkitgtk_minibrowser-nightly"]["failed"][0]["github_lookups"], 2)

    def test_a_commit_that_could_not_be_read_the_first_time_says_so(self):
        updater = self.updater(FakeFetcher({"check-suites": FetchError("HTTP 503", status=503)}))
        with self.assertLogs("test_update", level="ERROR"):
            row = self.row(updater, {})
        self.assertEqual(row["cells"]["gtk"]["nightly"]["state"], "commit_unavailable")
        self.assertIn("HTTP 503", row["error"])

    def test_a_commit_shared_by_two_tags_is_fetched_once_per_run(self):
        fetcher = FakeFetcher(self.replies())
        updater = self.updater(fetcher)
        weekly = {**TAG, "name": "epochs/weekly/2026-09-27_05H"}
        updater.tags_by_sha = {SHA: {TAG["name"], weekly["name"]}}
        wptfyi_runs = {f"{port['product']}-{channel}": [] for port in update_dashboard.PORTS for channel in update_dashboard.ALL_CHANNELS}
        updater.row(TAG, ["nightly"], wptfyi_runs, {})
        updater.row(weekly, ["stable", "beta"], wptfyi_runs, {})
        self.assertEqual(sum("check-suites" in url for url in fetcher.urls), 1)
        self.assertEqual(updater.cache["commits"][SHA]["tags"], sorted([TAG["name"], weekly["name"]]))

    def test_a_tag_with_an_unexpected_name_is_left_out(self):
        tags = [{"ref": f"refs/tags/epochs/daily/{name}", "object": {"type": "commit", "sha": SHA}} for name in ("2026-09-27_05H", "2026-09-27_05H-retry")]
        updater = self.updater(FakeFetcher({"matching-refs/tags/epochs/daily/2026-09": tags, "matching-refs": []}))
        with self.assertLogs("test_update", level="WARNING"):
            names = [tag["name"] for tag in updater.tags("daily")]
        self.assertEqual(names, ["epochs/daily/2026-09-27_05H"])

    def test_an_outage_without_saved_lists_does_not_clean_up_the_diffs(self):
        (self.data_dir / "diffs").mkdir()
        (self.data_dir / "diffs" / "1-2.json").write_text("{}")
        cache = {"version": update_dashboard.CACHE_VERSION, "diffs": {"1-2": {"totals": {}, "top_directories": [], "detail": "diffs/1-2.json"}}}
        args = types.SimpleNamespace(cache_dir=str(self.data_dir / "cache"), data_dir=str(self.data_dir))
        (self.data_dir / "cache").mkdir()
        (self.data_dir / "cache" / "cache.json").write_text(json.dumps(cache))
        replies = {"api/runs": FetchError("HTTP 503", status=503), "matching-refs": [], "api/status": [], "builders/": {"builds": []}}
        with mock.patch.object(update_dashboard, "Fetcher", lambda logger, token: FakeFetcher(replies)), self.assertLogs("test_update", level="INFO"):
            update_dashboard.update(args, self.logger, types.SimpleNamespace(errors=[], warnings=[]), "token")
        self.assertTrue((self.data_dir / "diffs" / "1-2.json").exists())

    def test_a_broken_cached_build_reaches_the_retries(self):
        cache = {"version": update_dashboard.CACHE_VERSION, "builds": {"wpe": {"7": {"number": 7, "started_at": "2026-09-20T00:00:00Z"}}, "gtk": {}},
                 "build_history_complete": {"wpe": True}}
        with self.assertRaises(KeyError):
            self.updater(FakeFetcher(self.build_replies([7])), cache).builds()

    def test_a_version_without_a_revision_names_no_build(self):
        builds = {"wpe": {"builds": [{"number": 1, "identifier": None, "passed": True, "started_at": "2026-09-26T05:00:00Z", "complete_at": "2026-09-26T12:00:00Z"}]}, "gtk": {"builds": []}}
        run_without_revision = {**run(3, SHA, 27, "x"), "browser_version": "2.55.0"}
        row = self.row(self.updater(FakeFetcher(self.replies())), {"wpewebkit-nightly": [run_without_revision]}, builds)
        self.assertIsNone(row["cells"]["wpe"]["nightly"]["wptfyi"]["tested_build"])

    def test_builds_just_before_the_window_are_kept_but_not_shown(self):
        started = datetime.datetime(2026, 7, 30, 5, tzinfo=datetime.timezone.utc).timestamp()
        def reply(url):
            return {"builds": [{"number": 1, "buildid": 1, "complete": True, "results": 0, "state_string": "ok", "started_at": started,
                                "complete_at": started + 3600, "properties": {"identifier": ["1@main"]}}]}
        updater = self.updater(FakeFetcher(reply))
        result = updater.builds()
        self.assertEqual(result["wpe"]["builds"], [])
        self.assertEqual([build["number"] for build in updater.all_builds["wpe"]], [1])

    def test_a_failed_request_keeps_the_previous_data_and_marks_the_row_stale(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.row(self.updater(FakeFetcher(self.replies()), cache), {})
        cache["commits"][SHA]["final"] = False
        failing = FakeFetcher({"check-suites": FetchError("HTTP 503", status=503)})
        row = self.row(self.updater(failing, cache), {})
        self.assertTrue(row["stale"])
        self.assertEqual(row["cells"]["wpe"]["nightly"]["state"], "not_uploaded")

    def test_the_same_identifier_means_the_same_bundle(self):
        updater = self.updater(FakeFetcher(self.replies()))
        row = self.row(updater, {"wpewebkit-nightly": [run(3, SHA, 27, "321570@main"), run(2, "older", 26, "321570@main")]})
        self.assertTrue(row["cells"]["wpe"]["nightly"]["wptfyi"]["same_build"])

    def test_an_empty_wptfyi_listing_is_not_an_error(self):
        fetcher = FakeFetcher({"api/runs": FetchError("HTTP 404 Not Found", status=404)})
        updater = self.updater(fetcher)
        with self.assertNoLogs("test_update", level="ERROR"):
            runs = updater.wptfyi_runs()
        self.assertEqual(runs["webkitgtk-stable"], [])

    def test_only_github_check_run_pages_are_accepted_as_links(self):
        for url in ("javascript:alert(1)", "http://github.com/web-platform-tests/wpt/runs/1", "https://elsewhere.example/runs/1"):
            fetcher = FakeFetcher({"check-runs": {"check_runs": [{"external_id": "task", "started_at": "2026-09-27T05:00:00Z", "html_url": url}]}})
            self.assertIsNone(sources.check_run_url(fetcher, SHA, "wpt-a-nightly-b-1", "task"), url)

    def test_a_check_run_of_another_task_is_not_used(self):
        fetcher = FakeFetcher({"check-runs": {"check_runs": [{"external_id": "retry-task", "started_at": "2026-09-27T05:00:00Z",
                                                               "html_url": "https://github.com/web-platform-tests/wpt/runs/2"}]}})
        self.assertIsNone(sources.check_run_url(fetcher, SHA, "wpt-a-nightly-b-1", "first-task"))

    def test_a_missing_github_check_is_looked_up_again_but_not_forever(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        replies = self.replies(gtk_state="failed")
        del replies["check_name=wpt-webkitgtk_minibrowser-nightly-reftest-1"]
        replies = {"check_name=wpt-webkitgtk": {"check_runs": []}, **replies}
        lookups = 0
        for _ in range(update_dashboard.CHECK_RUN_LOOKUPS + 3):
            fetcher = FakeFetcher(replies)
            self.row(self.updater(fetcher, cache), {})
            lookups += sum("check_name=wpt-webkitgtk" in url for url in fetcher.urls)
        self.assertEqual(lookups, update_dashboard.CHECK_RUN_LOOKUPS)
        later = FakeFetcher({"check_name=wpt-webkitgtk": self.replies()["check_name=wpt-webkitgtk_minibrowser-nightly-reftest-1"]})
        cache["commits"][SHA]["chunks"]["webkitgtk_minibrowser-nightly"]["failed"][0]["github_lookups"] = 0
        row = self.row(self.updater(later, cache), {})
        self.assertEqual(row["cells"]["gtk"]["nightly"]["chunks"]["failed"][0]["github_url"], "https://github.com/web-platform-tests/wpt/runs/42")

    def build_replies(self, numbers, failing_steps_for=()):
        def reply(url):
            if "/steps" in url:
                buildid = int(url.split("/builds/")[1].split("/")[0])
                if buildid in failing_steps_for:
                    raise FetchError("HTTP 503", status=503)
                return {"steps": []}
            limit = int(url.split("limit=")[1].split("&")[0])
            return {"builds": [{"number": number, "buildid": number, "complete": True, "results": 2, "state_string": "failed",
                                "started_at": 1790500000 + number, "complete_at": 1790510000 + number,
                                "properties": {"identifier": [f"{number}@main"]}} for number in numbers[:limit]]}
        return reply

    def builds_limits(self, fetcher):
        return [int(url.split("limit=")[1].split("&")[0]) for url in fetcher.urls if "/builds?" in url]

    def test_a_partial_first_read_of_the_builds_is_done_again_in_full(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        numbers = list(range(100, 30, -1))
        first = FakeFetcher(self.build_replies(numbers, failing_steps_for={99}))
        self.updater(first, cache).builds()
        self.assertEqual(self.builds_limits(first), [update_dashboard.FIRST_BUILD_FETCH] * 2)
        second = FakeFetcher(self.build_replies(numbers))
        self.updater(second, cache).builds()
        self.assertEqual(self.builds_limits(second), [update_dashboard.FIRST_BUILD_FETCH] * 2)
        third = FakeFetcher(self.build_replies(numbers))
        self.updater(third, cache).builds()
        self.assertEqual(self.builds_limits(third), [update_dashboard.BUILD_FETCH] * 2)

    def test_an_interrupted_backfill_is_done_again_on_the_next_run(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.updater(FakeFetcher(self.build_replies(list(range(100, 30, -1)))), cache).builds()
        interrupted = FakeFetcher(self.build_replies(list(range(130, 60, -1)), failing_steps_for={129}))
        self.updater(interrupted, cache).builds()
        recovery = FakeFetcher(self.build_replies(list(range(130, 60, -1))))
        self.updater(recovery, cache).builds()
        self.assertEqual(self.builds_limits(recovery), [update_dashboard.FIRST_BUILD_FETCH] * 2)
        self.assertTrue(all(str(number) in cache["builds"]["wpe"] for number in range(101, 131)))

    def test_a_gap_after_the_cached_builds_reads_the_history_again(self):
        cache = {"version": update_dashboard.CACHE_VERSION}
        self.updater(FakeFetcher(self.build_replies(list(range(100, 30, -1)))), cache).builds()
        later = FakeFetcher(self.build_replies(list(range(130, 60, -1))))
        self.updater(later, cache).builds()
        self.assertEqual(self.builds_limits(later), [update_dashboard.BUILD_FETCH, update_dashboard.FIRST_BUILD_FETCH] * 2)

    def test_a_cache_that_is_not_a_dictionary_is_replaced(self):
        path = self.data_dir / "cache.json"
        path.write_text("[]")
        with self.assertLogs("test_update", level="WARNING"):
            cache = update_dashboard.load_cache(path, self.logger)
        self.assertEqual(cache, {"version": update_dashboard.CACHE_VERSION})

    def test_identifier_of_a_browser_version(self):
        self.assertEqual(update_dashboard.identifier_of("2.55.0 (321964@main)"), "321964@main")
        self.assertIsNone(update_dashboard.identifier_of("2.55.0"))

    def test_months_between_two_dates(self):
        self.assertEqual(update_dashboard.months_between(datetime.date(2026, 7, 31), datetime.date(2026, 9, 29)), ["2026-07", "2026-08", "2026-09"])
        self.assertTrue(update_dashboard.month_is_over("2026-08", datetime.date(2026, 9, 2)))
        self.assertFalse(update_dashboard.month_is_over("2026-09", datetime.date(2026, 9, 29)))


class LocalPathTest(unittest.TestCase):
    def test_an_unexpected_error_names_repository_paths_relative_to_it(self):
        try:
            open(update_dashboard.REPOSITORY / "html" / "data" / "missing" / "dashboard.json")
        except OSError as error:
            text = update_dashboard.error_text(error)
        self.assertIn("'html/data/missing/dashboard.json'", text)
        self.assertNotIn(str(update_dashboard.REPOSITORY), text)

    def test_paths_given_on_the_command_line_are_resolved(self):
        with tempfile.TemporaryDirectory() as directory:
            link = pathlib.Path(directory) / "repo"
            link.symlink_to(update_dashboard.REPOSITORY)
            args = update_dashboard.parse_args(["--cache-dir", str(link / "cron_helper" / "cache")])
        self.assertEqual(args.cache_dir, str(update_dashboard.DEFAULT_CACHE_DIR))


class RetryTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = pathlib.Path(directory.name)
        self.args = types.SimpleNamespace(cache_dir=str(root / "cache"), data_dir=str(root / "data"))
        (root / "cache").mkdir()
        (root / "data").mkdir()
        self.cache_path = root / "cache" / "cache.json"
        self.cache_path.write_text('{"version": %d, "commits": {"old": {}}}' % update_dashboard.CACHE_VERSION)
        self.dashboard_path = root / "data" / "dashboard.json"
        self.dashboard_path.write_text('{"generated_at": "2026-09-29T09:00:00Z", "ports": []}')
        self.logger = logging.getLogger("test_retry")
        self.logger.addHandler(logging.NullHandler())
        self.logger.propagate = False
        self.recorder = types.SimpleNamespace(errors=[], warnings=[])
        self.pauses = []

    def run_failing(self, failures):
        """failures is how many attempts fail before one works; each call
        records whether the cache existed when that attempt started."""
        self.cache_seen = []

        def fake_update(args, logger, recorder, token):
            self.cache_seen.append(self.cache_path.exists())
            if len(self.cache_seen) <= failures:
                raise KeyError("tags")

        with mock.patch.object(update_dashboard, "update", fake_update):
            return update_dashboard.run_with_retries(self.args, self.logger, self.recorder, "token", sleep=self.pauses.append)

    def test_a_random_failure_is_tried_again_with_the_same_cache(self):
        self.assertEqual(self.run_failing(1), 0)
        self.assertEqual(self.cache_seen, [True, True])
        self.assertEqual(self.pauses, [update_dashboard.RETRY_PAUSE_SECONDS])
        self.assertEqual(len(self.recorder.warnings), 1)
        self.assertIn("KeyError", self.recorder.warnings[0])

    def test_the_third_attempt_uses_an_empty_cache_and_keeps_the_old_one_aside(self):
        self.assertEqual(self.run_failing(2), 0)
        self.assertEqual(self.cache_seen, [True, True, False])
        self.assertTrue(self.cache_path.with_name("cache.json.broken").exists())

    def test_when_every_attempt_fails_the_old_cache_is_kept_and_the_page_is_told(self):
        self.assertEqual(self.run_failing(3), 1)
        self.assertEqual(json.loads(self.cache_path.read_text())["commits"], {"old": {}})
        self.assertFalse(self.cache_path.with_name("cache.json.broken").exists())
        dashboard = json.loads(self.dashboard_path.read_text())
        self.assertEqual(dashboard["generated_at"], "2026-09-29T09:00:00Z")
        self.assertIn("KeyError", dashboard["failed_update"]["error"])

    def test_a_second_run_does_not_start_while_the_first_holds_the_lock(self):
        first = update_dashboard.take_lock(self.args.cache_dir)
        self.addCleanup(first.close)
        self.assertIsNone(update_dashboard.take_lock(self.args.cache_dir))

    def test_a_cache_directory_that_cannot_be_used_is_logged(self):
        log = pathlib.Path(self.args.data_dir) / "log.txt"
        below_a_file = self.cache_path / "cache"
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "token"}):
            self.assertEqual(update_dashboard.main(["--log", str(log), "--cache-dir", str(below_a_file), "--data-dir", self.args.data_dir]), 1)
        self.assertIn("ERROR: Could not take the lock", log.read_text())

    def test_a_token_with_characters_github_never_uses_is_refused(self):
        log = pathlib.Path(self.args.data_dir) / "log.txt"
        with mock.patch.dict("os.environ", {"GITHUB_TOKEN": "ghp_ab cd"}):
            self.assertEqual(update_dashboard.main(["--log", str(log), "--cache-dir", self.args.cache_dir, "--data-dir", self.args.data_dir]), 1)
        self.assertIn("ERROR: GITHUB_TOKEN has characters", log.read_text())
        self.assertNotIn("ab cd", log.read_text())

    def test_without_a_good_run_to_keep_a_failure_writes_no_dashboard(self):
        self.dashboard_path.unlink()
        self.assertEqual(self.run_failing(3), 1)
        self.assertFalse(self.dashboard_path.exists())


if __name__ == "__main__":
    unittest.main()
