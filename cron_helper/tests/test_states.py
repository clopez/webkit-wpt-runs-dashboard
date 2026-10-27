import datetime
import unittest

import states

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.timezone.utc)
SETTINGS = states.Settings()


def task(name, state="completed", chunk_of=None, runs=None, group="groupA", task_id=None):
    description = "Some task."
    if chunk_of:
        number, suite = int(name.rsplit("-", 1)[1]), name.rsplit("-", 2)[1]
        description = f'A subset of WPT\'s "{suite}" tests (chunk number {number} of {chunk_of}), run in the nightly release.'
    return {
        "task": {"metadata": {"name": name, "description": description}},
        "status": {"taskId": task_id or f"id-{name}", "taskGroupId": group, "state": state,
                   "runs": runs if runs is not None else [{"runId": 0, "state": state, "reasonResolved": state, "resolved": "2026-09-29T06:00:00.000Z"}]},
    }


def chunks_summary(**overrides):
    summary = {"total": 33, "expected": 33, "completed": 33, "unfinished": 0, "failed": [], "missing": [], "retried": 0,
               "last_resolved": "2026-09-29T06:00:00.000Z", "task_group_id": "groupA"}
    summary.update(overrides)
    return summary


def suite(status="completed", updated_at="2026-09-29T08:00:00Z", check_runs=412):
    return {"id": 1, "status": status, "conclusion": "success", "updated_at": updated_at, "check_runs": check_runs}


class ParseTaskTest(unittest.TestCase):
    def test_parses_a_wpt_chunk(self):
        chunk = states.parse_task(task("wpt-webkitgtk_minibrowser-nightly-testharness-10", chunk_of=16))
        self.assertEqual((chunk["browser"], chunk["channel"], chunk["suite"], chunk["chunk"], chunk["chunks_in_suite"]),
                         ("webkitgtk_minibrowser", "nightly", "testharness", 10, 16))
        self.assertEqual(chunk["task_group_id"], "groupA")

    def test_ignores_tasks_that_are_not_chunks(self):
        entry = task("wpt-webkitgtk_minibrowser-nightly-testharness-1")
        entry["task"]["metadata"]["name"] = "wpt-decision-task"
        self.assertIsNone(states.parse_task(entry))


class ChunksTest(unittest.TestCase):
    def test_a_newer_task_group_replaces_chunks_of_the_same_name(self):
        old = states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", state="failed", group="old"))
        new = states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", group="new"))
        merged = states.merge_task_groups([("2026-09-29T05:00:00Z", [new]), ("2026-09-28T18:00:00Z", [old])])
        self.assertEqual(merged["wpt-wpewebkit_minibrowser-nightly-reftest-1"]["state"], "completed")

    def test_a_newer_group_replaces_the_whole_run_and_old_chunks_do_not_fill_its_holes(self):
        old = [states.parse_task(task(f"wpt-wpewebkit_minibrowser-nightly-reftest-{number}", chunk_of=2, group="old")) for number in (1, 2)]
        new = [states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=2, group="new"))]
        merged = states.merge_task_groups([("2026-09-28T05:00:00Z", old), ("2026-09-29T05:00:00Z", new)])
        self.assertEqual(sorted(merged), ["wpt-wpewebkit_minibrowser-nightly-reftest-1"])
        self.assertEqual(states.summarize_chunks(list(merged.values()))["missing"], ["reftest-2"])

    def test_groups_with_different_runs_are_combined(self):
        nightly = [states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=1, group="daily"))]
        stable = [states.parse_task(task("wpt-wpewebkit_minibrowser-stable-reftest-1", chunk_of=1, group="weekly"))]
        merged = states.merge_task_groups([("2026-09-28T05:00:00Z", nightly), ("2026-09-28T05:01:00Z", stable)])
        self.assertEqual(len(merged), 2)

    def test_a_suite_that_the_other_port_ran_is_missing(self):
        gtk = [states.parse_task(task("wpt-webkitgtk_minibrowser-nightly-reftest-1", chunk_of=1))]
        wpe = [states.parse_task(task(f"wpt-wpewebkit_minibrowser-nightly-test262-{number}", chunk_of=2)) for number in (1, 2)]
        summary = states.summarize_chunks(gtk, states.expected_suites(gtk + wpe))
        self.assertEqual(summary["missing"], ["test262-1", "test262-2"])
        self.assertEqual(summary["expected"], 3)

    def test_a_port_without_any_task_misses_the_suites_of_the_other_port(self):
        wpe = [states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=1))]
        summary = states.summarize_chunks([], states.expected_suites(wpe))
        self.assertEqual((summary["total"], summary["missing"]), (0, ["reftest-1"]))
        self.assertEqual(states.decide_state(summary, suite(), False, NOW, SETTINGS)["state"], "failed")
        self.assertEqual(states.decide_state(summary, suite(), False, NOW, SETTINGS, scheduling=True)["state"], "scheduling")

    def test_a_bad_time_in_a_task_is_a_bad_answer(self):
        entry = task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=1)
        entry["status"]["runs"][0]["resolved"] = "yesterday"
        with self.assertRaises(ValueError):
            states.parse_task(entry)

    def test_the_run_time_and_the_slowest_chunk_count_the_last_run_of_each_chunk_and_the_wall_time_starts_with_the_first(self):
        retried = [{"runId": 0, "state": "exception", "reasonResolved": "worker-shutdown", "started": "2026-09-29T05:00:00Z", "resolved": "2026-09-29T05:20:00Z"},
                   {"runId": 1, "state": "completed", "reasonResolved": "completed", "started": "2026-09-29T05:30:00Z", "resolved": "2026-09-29T06:30:00Z"}]
        once = [{"runId": 0, "state": "completed", "reasonResolved": "completed", "started": "2026-09-29T05:10:00Z", "resolved": "2026-09-29T05:40:00Z"}]
        chunks = [states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=2, runs=retried)),
                  states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-2", chunk_of=2, runs=once))]
        summary = states.summarize_chunks(chunks)
        self.assertEqual(summary["run_seconds"], 60 * 60 + 30 * 60)
        self.assertEqual((summary["first_started"], summary["last_resolved"]), ("2026-09-29T05:00:00Z", "2026-09-29T06:30:00Z"))
        self.assertEqual(summary["timed_chunks"], 2)
        self.assertEqual((summary["slowest"]["name"], summary["slowest"]["seconds"]), ("wpt-wpewebkit_minibrowser-nightly-reftest-1", 60 * 60))

    def queued(self, name, state, scheduled, started=None, group="groupA"):
        run = {"runId": 0, "state": state, "scheduled": scheduled, "started": started}
        return states.parse_task(task(name, state=state, chunk_of=33, runs=[run], group=group))

    def test_the_summary_counts_running_and_waiting_chunks(self):
        chunks = [self.queued("wpt-wpewebkit_minibrowser-nightly-reftest-1", "pending", "2026-09-29T12:40:01Z"),
                  self.queued("wpt-wpewebkit_minibrowser-nightly-reftest-2", "pending", "2026-09-29T12:40:03Z"),
                  self.queued("wpt-wpewebkit_minibrowser-nightly-reftest-3", "running", "2026-09-29T12:40:02Z", "2026-09-29T13:00:00Z")]
        summary = states.summarize_chunks(chunks)
        self.assertEqual((summary["running"], summary["pending"]), (1, 2))

    def test_a_time_without_a_zone_is_taken_as_utc(self):
        self.assertEqual(states.parse_time("2026-09-27T06:00:00"), states.parse_time("2026-09-27T06:00:00Z"))

    def test_times_with_different_decimals_are_compared_as_times(self):
        # As strings, "06:00:00Z" sorts after "06:00:00.900Z", although it is earlier.
        runs = [{"runId": 0, "state": "completed", "reasonResolved": "completed", "resolved": "2026-09-29T06:00:00.900Z"},
                {"runId": 1, "state": "completed", "reasonResolved": "completed", "resolved": "2026-09-29T06:00:00Z"}]
        chunk = states.parse_task(task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=1, runs=runs))
        self.assertEqual(states.summarize_chunks([chunk])["last_resolved"], "2026-09-29T06:00:00.900Z")

    def test_summary_counts_failed_missing_and_retried_chunks(self):
        retried_runs = [{"runId": 0, "state": "exception", "reasonResolved": "worker-shutdown", "resolved": "2026-09-29T05:30:00.000Z"},
                        {"runId": 1, "state": "completed", "reasonResolved": "completed", "resolved": "2026-09-29T07:00:00.000Z"}]
        chunks = [states.parse_task(entry) for entry in [
            task("wpt-wpewebkit_minibrowser-nightly-reftest-1", chunk_of=3),
            task("wpt-wpewebkit_minibrowser-nightly-reftest-2", chunk_of=3, runs=retried_runs),
            task("wpt-wpewebkit_minibrowser-nightly-crashtest-1", state="failed", chunk_of=1),
        ]]
        summary = states.summarize_chunks(chunks)
        self.assertEqual(summary["expected"], 4)
        self.assertEqual(summary["completed"], 2)
        self.assertEqual(summary["retried"], 1)
        self.assertEqual(summary["missing"], ["reftest-3"])
        self.assertEqual([failed["name"] for failed in summary["failed"]], ["wpt-wpewebkit_minibrowser-nightly-crashtest-1"])
        self.assertEqual(summary["failed"][0]["run_id"], 0)
        self.assertEqual(summary["last_resolved"], "2026-09-29T07:00:00.000Z")
        self.assertEqual(summary["task_group_id"], "groupA")


class DecideStateTest(unittest.TestCase):
    def decide(self, chunks=None, suite_data=None, uploaded=False, now=NOW, entries=(), decision_failed=False, received_at=None):
        return states.decide_state(chunks, suite_data, uploaded, now, SETTINGS, status_entries=entries, decision_failed=decision_failed,
                                   received_at=received_at)

    def test_a_run_on_wptfyi_is_green_whatever_else_happened(self):
        self.assertEqual(self.decide(chunks_summary(failed=[{"name": "x"}]), suite(), uploaded=True)["state"], "uploaded")

    def test_no_chunks_is_grey_unless_the_decision_task_failed(self):
        self.assertEqual(self.decide(None, suite())["color"], "grey")
        self.assertEqual(self.decide(None, suite(), decision_failed=True)["state"], "decision_failed")

    def test_unfinished_chunks_are_running(self):
        self.assertEqual(self.decide(chunks_summary(completed=20, unfinished=13), suite(status="in_progress"))["state"], "running")

    def test_failed_or_missing_chunks_are_red(self):
        self.assertEqual(self.decide(chunks_summary(failed=[{"name": "x"}]), suite())["state"], "failed")
        self.assertEqual(self.decide(chunks_summary(missing=["reftest-3"]), suite())["state"], "failed")

    def test_waiting_for_the_suite_turns_red_after_48_hours(self):
        waiting = self.decide(chunks_summary(), suite(status="in_progress"))
        self.assertEqual(waiting["state"], "waiting_for_suite")
        late = self.decide(chunks_summary(last_resolved="2026-09-27T11:00:00.000Z"), suite(status="in_progress"))
        self.assertEqual((late["state"], late["color"]), ("suite_never_finished", "red"))

    def test_uploading_for_7_hours_after_the_suite_finished(self):
        uploading = self.decide(chunks_summary(), suite(updated_at="2026-09-29T05:30:00Z"))
        self.assertEqual(uploading["state"], "uploading")
        self.assertEqual(uploading["red_at"], "2026-09-29T12:30:00+00:00")
        self.assertEqual(self.decide(chunks_summary(), suite(updated_at="2026-09-29T04:30:00Z"))["state"], "not_uploaded")

    def test_not_uploaded_explains_the_check_run_limit_first(self):
        cell = self.decide(chunks_summary(), suite(updated_at="2026-09-28T12:00:00Z", check_runs=614), entries=[{"stage": "INVALID"}])
        self.assertEqual(cell["reason"], {"kind": "check_run_limit", "check_runs": 614, "limit": 500})

    def test_wptfyi_batches_group_the_uploads_created_together_and_count_the_processed_ones(self):
        spread = datetime.timedelta(minutes=1)
        uploads = [("2026-10-27T01:17:03.586Z", False), ("2026-10-27T01:17:03.594Z", True), ("2026-10-27T01:17:03.661Z", True),
                   ("2026-10-27T03:02:00Z", True), ("2026-10-27T03:02:00.010Z", None)]
        # A batch that has left wpt.fyi's list keeps what was known of it.
        previous = [{"at": "2026-10-26T13:40:38Z", "uploads": 8, "processed": 8}]
        self.assertEqual(states.upload_batches(previous, uploads, spread),
                         [{"at": "2026-10-26T13:40:38Z", "uploads": 8, "processed": 8},
                          {"at": "2026-10-27T01:17:03.586Z", "uploads": 3, "processed": 1},
                          {"at": "2026-10-27T03:02:00Z", "uploads": 1, "processed": 0}])
        # An upload known only from the rejected ones says when a batch came, not how big it was.
        self.assertEqual(states.upload_batches([], [("2026-10-01T13:21:36Z", None)], spread), [{"at": "2026-10-01T13:21:36Z", "uploads": None, "processed": None}])

    def test_a_run_that_wptfyi_rejected_is_not_uploaded_at_once_but_only_for_its_own_channel(self):
        # As on 2026-10-14: the chunks of the run tested two bundles, and wpt.fyi said so with an empty version.
        conflict = {"stage": "INVALID", "browser_version": "", "created": "2026-09-29T09:00:00Z",
                    "error": "Conflicting 'browser_version: [2.55.0 (323696@main), 2.55.0 (323533@main)]' found in the metadata"}
        nightly = chunks_summary(version="2.55.0 (323533@main)")
        cell = self.decide(nightly, suite(), entries=[conflict], received_at="2026-09-29T08:30:00Z")
        self.assertEqual((cell["state"], cell["reason"]), ("not_uploaded", {"kind": "wptfyi_status", "entries": [conflict]}))
        # The stable run of the same browser on the same commit is not that one, so it waits as usual.
        stable = chunks_summary(version="2.54.1")
        self.assertEqual(self.decide(stable, suite(), entries=[conflict], received_at="2026-09-29T08:30:00Z")["state"], "uploading")
        # Neither is a run whose version is not known, nor one whose chunks finished after the rejection.
        self.assertEqual(self.decide(chunks_summary(), suite(), entries=[conflict], received_at="2026-09-29T08:30:00Z")["state"], "uploading")
        later = chunks_summary(version="2.55.0 (323533@main)", last_resolved="2026-09-29T10:00:00Z")
        self.assertEqual(self.decide(later, suite(updated_at="2026-09-29T10:30:00Z"), entries=[conflict])["state"], "uploading")
        # A duplicate means the run is already on wpt.fyi from another batch, so it is no rejection.
        self.assertEqual(self.decide(nightly, suite(), entries=[{**conflict, "stage": "DUPLICATE"}], received_at="2026-09-29T08:30:00Z")["state"],
                         "uploading")

    def test_a_run_that_wptfyi_received_is_uploading_whatever_the_suite_says(self):
        cell = self.decide(chunks_summary(), suite(status="in_progress", updated_at="2026-09-29T11:40:00Z"), received_at="2026-09-29T11:17:03Z")
        self.assertEqual((cell["state"], cell["since"], cell["received"]), ("uploading", "2026-09-29T11:17:03Z", True))
        # The grace period counts from when wpt.fyi received it.
        self.assertEqual(cell["red_at"], "2026-09-29T18:17:03+00:00")

    def test_the_check_run_limit_is_the_one_wptfyi_had_when_the_suite_finished(self):
        # wpt.fyi could list 2000 check runs from 2026-10-03 on, so 638 is no reason any more.
        now = datetime.datetime(2026, 10, 27, 12, 0, tzinfo=datetime.timezone.utc)
        late = states.decide_state(chunks_summary(), suite(updated_at="2026-10-27T01:16:53Z", check_runs=638), False, now, SETTINGS)
        self.assertEqual(late["reason"], {"kind": "unknown"})
        too_many = states.decide_state(chunks_summary(), suite(updated_at="2026-10-27T01:16:53Z", check_runs=2100), False, now, SETTINGS)
        self.assertEqual(too_many["reason"], {"kind": "check_run_limit", "check_runs": 2100, "limit": 2000})

    def test_a_missing_upload_is_not_confirmed_when_wptfyi_could_not_be_checked(self):
        cell = states.decide_state(chunks_summary(), suite(updated_at="2026-09-28T12:00:00Z"), False, NOW, SETTINGS, wptfyi_checked=False)
        self.assertEqual((cell["state"], cell["color"]), ("wptfyi_unchecked", "yellow"))

    def test_an_accepted_upload_is_not_a_reason(self):
        cell = self.decide(chunks_summary(), suite(updated_at="2026-09-28T12:00:00Z"), entries=[{"stage": "VALID", "error": ""}])
        self.assertEqual(cell["reason"], {"kind": "unknown"})

    def test_a_commit_that_could_not_be_read_is_not_a_run_that_did_not_happen(self):
        cell = states.decide_state(None, None, False, NOW, SETTINGS, commit_unavailable=True)
        self.assertEqual((cell["state"], cell["color"]), ("commit_unavailable", "yellow"))

    def test_not_uploaded_uses_the_wptfyi_upload_status_or_says_it_is_unknown(self):
        entries = [{"stage": "INVALID", "error": "bad report"}]
        with_status = self.decide(chunks_summary(), suite(updated_at="2026-09-28T12:00:00Z"), entries=entries)
        self.assertEqual(with_status["reason"], {"kind": "wptfyi_status", "entries": entries})
        self.assertEqual(self.decide(chunks_summary(), suite(updated_at="2026-09-28T12:00:00Z"))["reason"], {"kind": "unknown"})



def brief(name, state, started=None, resolved=None, group="groupA", queue="proj-wpt/ci-gw", task_id=None, run_id=0, chunk_of=None):
    entry = task(name, state=state, chunk_of=chunk_of, runs=[{"runId": run_id, "state": state, "started": started, "resolved": resolved}],
                 group=group, task_id=task_id)
    entry["task"]["taskQueueId"] = queue
    return states.task_brief(entry)


class EstimateTest(unittest.TestCase):
    def test_every_task_of_a_push_is_kept_with_its_kind_and_the_finished_ones_give_the_time_of_their_kind(self):
        tasks = [brief(f"wpt-firefox-beta-testharness-{number}", "completed", "2026-09-29T10:00:00Z", f"2026-09-29T10:{minutes}:00Z")
                 for number, minutes in enumerate((50, 20, 40, 30, 10), 1)]
        # Too few Chrome tasks have finished to say how long they take, but they still count for the commit as a whole.
        tasks += [brief("wpt-chrome-stable-reftest-1", "completed", "2026-09-29T10:00:00Z", "2026-09-29T11:00:00Z"),
                  brief("wpt-firefox-beta-reftest-1", "pending"),
                  brief("download-firefox-beta-1", "running", "2026-09-29T11:00:00Z")]
        tasks.append(brief("wpt-decision-task", "completed", "2026-09-29T09:00:00Z", "2026-09-29T09:02:00Z"))
        progress = states.commit_progress(tasks)
        self.assertEqual(progress["seconds_by_kind"], {"firefox-beta": [10 * 60, 20 * 60, 30 * 60, 40 * 60, 50 * 60]})
        # Kept even alone, to be pooled with the decision tasks of the other commits.
        self.assertEqual(progress["decision_seconds"], [2 * 60])
        self.assertEqual(progress["chunk_seconds"], [minutes * 60 for minutes in (10, 20, 30, 40, 50, 60)])
        self.assertEqual(progress["chunk_history"], {"firefox-beta": {f"testharness-{number}": minutes * 60 for number, minutes in enumerate((50, 20, 40, 30, 10), 1)},
                                                     "chrome-stable": {"reftest-1": 60 * 60}})
        self.assertEqual([(task["name"], task["kind"], task["state"]) for task in progress["unfinished"]],
                         [("wpt-firefox-beta-reftest-1", "firefox-beta", "pending"), ("download-firefox-beta-1", "download-firefox-beta-1", "running")])

    def test_each_chunk_takes_its_own_times_from_its_latest_runs(self):
        # One run a day, newest first: the chunk got longer when tests were added to it, and only its latest runs say so.
        minutes = [155, 158, 154, 160, 154, 155, 157, 146, 145, 58, 57]
        dated = [(NOW - datetime.timedelta(days=day), {"progress": {"chunk_history": {"firefox_android-nightly": {"testharness-25": run * 60, "testharness-2": 2400}}}})
                 for day, run in enumerate(minutes)]
        dated.append((NOW, {"progress": {"unfinished": [], "decision_seconds": [120]}}))
        # The order they come in does not matter.
        history = states.chunk_history(reversed(dated), NOW)
        self.assertEqual(history["wpt-firefox_android-nightly-testharness-25"], sorted(run * 60 for run in minutes[:states.HISTORY_RUNS]))
        self.assertEqual(history["wpt-firefox_android-nightly-testharness-2"], [2400] * states.HISTORY_RUNS)

    def test_a_chunk_that_runs_once_a_week_only_takes_its_runs_of_the_last_two_weeks(self):
        weekly = [(NOW - datetime.timedelta(weeks=week), {"progress": {"chunk_history": {"webkitgtk_minibrowser-stable": {"test262-5-of-8": seconds}}}})
                  for week, seconds in enumerate((2400, 2700, 9000, 9000))]
        self.assertEqual(states.chunk_history(weekly, NOW), {"wpt-webkitgtk_minibrowser-stable-test262-5-of-8": [2400, 2700]})
        self.assertEqual(states.chunk_history(weekly[3:], NOW), {})

    def test_a_suite_split_into_another_number_of_chunks_starts_a_new_history(self):
        # Chunk 5 of 20 and chunk 5 of 30 run different tests, so the times of one say nothing about the other.
        before = brief("wpt-firefox-beta-testharness-5", "completed", "2026-09-28T10:00:00Z", "2026-09-28T11:00:00Z", chunk_of=20)
        now = brief("wpt-firefox-beta-testharness-5", "running", "2026-09-29T11:30:00Z", chunk_of=30)
        self.assertEqual(states.chunk_history([(NOW, {"progress": states.commit_progress([before])})], NOW), {"wpt-firefox-beta-testharness-5-of-20": [3600]})
        self.assertEqual(now["history_key"], "wpt-firefox-beta-testharness-5-of-30")
        # Tasks that are not chunks keep their name, so the decision tasks still find their times.
        self.assertEqual(brief("wpt-decision-task", "completed")["history_key"], "wpt-decision-task")

    def test_a_task_of_an_older_kind_names_its_queue_by_provisioner_and_worker_type(self):
        self.assertEqual(states.task_queue_of({"provisionerId": "proj-wpt", "workerType": "ci-gw"}), "proj-wpt/ci-gw")

    def test_the_pace_counts_the_tasks_that_were_ahead_of_the_current_head(self):
        an_hour_ago = {"at": "2026-09-29T11:00:00Z", "tasks": ["a/0", "b/0", "c/0", "d/0"]}
        self.assertEqual(states.queue_pace(an_hour_ago, "c/0", NOW), (2, None, True))
        # A queue that did not move is stuck, which an old pace must not hide.
        self.assertEqual(states.queue_pace({**an_hour_ago, "busy_pace": {"at": "2026-09-29T11:00:00Z", "tasks_per_hour": 150}}, "a/0", NOW),
                         (None, "stuck", False))
        self.assertEqual(states.queue_pace({}, "a/0", NOW), (None, "pace", False))
        # A view from the future, as after the clock was set back, measures nothing.
        self.assertEqual(states.queue_pace({**an_hour_ago, "at": "2026-09-29T13:00:00Z"}, "c/0", NOW), (None, "pace", False))

    def test_a_queue_that_ran_dry_takes_the_last_busy_pace_if_it_is_recent(self):
        # All 4 left, and maybe many more, so 4 an hour is only a lower bound.
        ran_dry = {"at": "2026-09-29T11:00:00Z", "tasks": ["a/0", "b/0", "c/0", "d/0"], "busy_pace": {"at": "2026-09-29T07:00:00Z", "tasks_per_hour": 150}}
        self.assertEqual(states.queue_pace(ran_dry, "z/0", NOW), (150, None, False))
        self.assertEqual(states.queue_pace({**ran_dry, "tasks": []}, None, NOW), (150, None, False))
        too_old = {**ran_dry, "busy_pace": {"at": "2026-09-29T05:00:00Z", "tasks_per_hour": 150}}
        self.assertEqual(states.queue_pace(too_old, "z/0", NOW), (None, "pace", False))
        self.assertEqual(states.queue_pace({**ran_dry, "busy_pace": None}, None, NOW), (None, "pace", False))

    def queue(self, positions, length=100, tasks_per_hour=60):
        return {"id": "proj-wpt/ci-gw", "positions": positions, "length": length, "tasks_per_hour": tasks_per_hour}

    def test_running_tasks_finish_after_their_usual_time_and_waiting_ones_after_the_tasks_ahead_of_them(self):
        tasks = [brief("wpt-firefox-beta-reftest-1", "running", "2026-09-29T11:50:00Z"),
                 brief("wpt-firefox-beta-reftest-2", "pending", task_id="t2"),
                 brief("wpt-firefox-beta-reftest-3", "pending", task_id="t3")]
        estimate = states.finish_estimate(tasks, self.queue({"t2/0": 10, "t3/0": 30}), lambda task: [30 * 60], NOW)
        # The last waiting one starts when the 30 tasks ahead of it have left at 60 an hour, and then takes 30 minutes.
        self.assertEqual(estimate, {"running": 1, "pending": 2, "unscheduled": 0, "position": 30, "queue_length": 100, "tasks_per_hour": 60,
                                    "expected_finish": "2026-09-29T13:00:00Z", "unknown": None})

    def test_a_running_task_longer_than_any_of_its_kind_has_no_estimate_rather_than_one_that_says_it_is_done(self):
        tasks = [brief("wpt-firefox-beta-reftest-1", "running", "2026-09-29T10:00:00Z")]
        estimate = states.finish_estimate(tasks, None, lambda task: [30 * 60], NOW)
        self.assertEqual((estimate["expected_finish"], estimate["unknown"]), (None, "longer"))

    def test_a_task_that_has_run_for_a_while_takes_as_long_as_the_finished_ones_that_took_longer(self):
        # The Android chunks of 2026-10-26: the first to finish took 20 to 48 minutes, so their median said done while some ran on for 70.
        finished = [minutes * 60 for minutes in (20, 35, 48, 80, 100)]
        tasks = [brief("wpt-firefox_android-stable-testharness-2", "running", "2026-09-29T10:50:00Z")]
        self.assertEqual(states.finish_estimate(tasks, None, lambda task: finished, NOW)["expected_finish"], "2026-09-29T12:30:00Z")
        self.assertEqual(states.expected_seconds(finished), 48 * 60)
        self.assertEqual(states.expected_seconds(finished, 70 * 60), 100 * 60)
        self.assertIsNone(states.expected_seconds(finished, 120 * 60))

    def test_a_waiting_task_no_longer_in_the_queue_has_started_and_one_waiting_for_another_task_joins_the_end(self):
        started = states.finish_estimate([brief("wpt-firefox-beta-reftest-1", "pending")], self.queue({}), lambda task: [60 * 60], NOW)
        self.assertEqual((started["position"], started["expected_finish"]), (None, "2026-09-29T13:00:00Z"))
        later = states.finish_estimate([brief("wpt-firefox-beta-reftest-1", "unscheduled")], self.queue({}, length=59), lambda task: [60 * 60], NOW)
        self.assertEqual((later["unscheduled"], later["expected_finish"]), (1, "2026-09-29T14:00:00Z"))

    def test_without_the_queue_its_pace_or_the_durations_there_is_no_time_and_the_estimate_says_why(self):
        waiting = [brief("wpt-firefox-beta-reftest-1", "pending", task_id="t1")]
        self.assertEqual(states.finish_estimate(waiting, None, lambda task: [60], NOW)["unknown"], "queue")
        self.assertEqual(states.finish_estimate([brief("wpt-firefox-beta-reftest-1", "pending", queue="other/queue")], self.queue({}),
                                                lambda task: [60], NOW)["unknown"], "queue")
        no_pace = states.finish_estimate(waiting, self.queue({"t1/0": 7}, tasks_per_hour=None), lambda task: [60], NOW)
        self.assertEqual((no_pace["unknown"], no_pace["position"], no_pace["expected_finish"]), ("pace", 7, None))
        self.assertEqual(states.finish_estimate(waiting, self.queue({"t1/0": 7}), lambda task: None, NOW)["unknown"], "durations")

    def test_wptfyi_takes_the_median_time_of_the_uploads_and_a_suite_that_changed_after_its_upload_says_nothing(self):
        def row(suite_finished, created, state="uploaded"):
            return {"suite": suite(updated_at=suite_finished), "cells": {"gtk": {"nightly": {"state": state, "wptfyi": {"created_at": created}}}}}
        rows = [row("2026-09-29T08:00:00Z", f"2026-09-29T08:{minutes}:00Z") for minutes in (10, 20, 30, 40, 50)]
        rows.append(row("2026-09-30T08:00:00Z", "2026-09-29T08:10:00Z"))
        self.assertEqual(states.upload_delay(rows), [minutes * 60 for minutes in (10, 20, 30, 40, 50)])


if __name__ == "__main__":
    unittest.main()
