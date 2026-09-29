import datetime
import unittest

import states

NOW = datetime.datetime(2026, 9, 29, 12, 0, tzinfo=datetime.timezone.utc)
SETTINGS = states.Settings()


def task(name, state="completed", chunk_of=None, runs=None, group="groupA", task_id=None):
    number = int(name.rsplit("-", 1)[1])
    suite = name.rsplit("-", 2)[1]
    description = f'A subset of WPT\'s "{suite}" tests (chunk number {number} of {chunk_of}), run in the nightly release.' if chunk_of else "Some task."
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
    def decide(self, chunks=None, suite_data=None, uploaded=False, now=NOW, entries=(), decision_failed=False):
        return states.decide_state(chunks, suite_data, uploaded, now, SETTINGS, status_entries=entries, decision_failed=decision_failed)

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


if __name__ == "__main__":
    unittest.main()
