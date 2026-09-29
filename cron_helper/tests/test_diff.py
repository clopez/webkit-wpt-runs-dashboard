import unittest

import diff


def entry(status, passing, total):
    return {"s": status, "c": [passing, total]}


class CompareTest(unittest.TestCase):
    def test_a_test_passes_only_when_it_ends_ok_and_every_subtest_passes(self):
        before = {"/a/ok.html": entry("O", 5, 5), "/a/partial.html": entry("O", 4, 5), "/a/error.html": entry("O", 5, 5)}
        after = {"/a/ok.html": entry("O", 5, 5), "/a/partial.html": entry("O", 5, 5), "/a/error.html": entry("E", 5, 5)}
        totals, directories = diff.compare(before, after)
        self.assertEqual(totals["tests_fixed"], 1)
        self.assertEqual(totals["tests_broken"], 1)
        changes = {file["test"]: file["test_change"] for file in directories[0]["files"]}
        self.assertEqual(changes, {"/a/partial.html": "fixed", "/a/error.html": "broken"})

    def test_subtests_that_stop_running_count_as_stopped_passing(self):
        before = {"/shadow-dom/setters.html": entry("O", 2055, 2055)}
        after = {"/shadow-dom/setters.html": entry("E", 0, 0)}
        totals, directories = diff.compare(before, after)
        self.assertEqual((totals["tests_broken"], totals["subtests_fixed"], totals["subtests_broken"]), (1, 0, 2055))
        self.assertEqual(directories[0]["files"][0]["before"], [2055, 2055, "O"])
        self.assertEqual(directories[0]["files"][0]["after"], [0, 0, "E"])

    def test_added_and_removed_tests_are_only_counted(self):
        before = {"/a/old.html": entry("O", 3, 3)}
        after = {"/a/new.html": entry("O", 1, 2)}
        totals, directories = diff.compare(before, after)
        self.assertEqual(totals, {"tests_fixed": 0, "tests_broken": 0, "subtests_fixed": 0, "subtests_broken": 0,
                                  "added_tests": 1, "added_subtests": 2, "removed_tests": 1, "removed_subtests": 3})
        self.assertEqual(directories[0]["files"], [])
        self.assertEqual(directories[0]["added"], [{"test": "/a/new.html", "after": [1, 2, "O"]}])
        self.assertEqual(directories[0]["removed"], [{"test": "/a/old.html", "before": [3, 3, "O"]}])

    def test_reftests_pass_by_status(self):
        before = {"/css/ref.html": entry("F", 0, 0)}
        after = {"/css/ref.html": entry("P", 0, 0)}
        totals, _ = diff.compare(before, after)
        self.assertEqual(totals["tests_fixed"], 1)

    def test_unchanged_tests_and_directories_are_left_out(self):
        before = {"/a/same.html": entry("O", 2, 3), "/b/same.html": entry("T", 0, 0)}
        after = {"/a/same.html": entry("O", 2, 3), "/b/same.html": entry("T", 0, 0)}
        totals, directories = diff.compare(before, after)
        self.assertEqual(directories, [])
        self.assertFalse(any(totals.values()))

    def test_a_status_change_that_moves_no_counter_is_kept_but_not_counted(self):
        before = {"/a/partial.html": entry("O", 3, 10)}
        after = {"/a/partial.html": entry("E", 3, 10)}
        totals, directories = diff.compare(before, after)
        self.assertFalse(any(totals.values()))
        self.assertEqual(directories[0]["files"], [{"test": "/a/partial.html", "before": [3, 10, "O"], "after": [3, 10, "E"],
                                                    "test_change": None, "subtests_fixed": 0, "subtests_broken": 0}])
        self.assertEqual(diff.top_directories(directories), [])

    def test_directories_are_sorted_by_tests_that_stopped_passing(self):
        before = {"/a/1.html": entry("O", 1, 1), "/b/1.html": entry("O", 1, 1), "/b/2.html": entry("O", 1, 1), "/c/1.html": entry("O", 0, 1)}
        after = {"/a/1.html": entry("O", 0, 1), "/b/1.html": entry("O", 0, 1), "/b/2.html": entry("O", 0, 1), "/c/1.html": entry("O", 1, 1)}
        _, directories = diff.compare(before, after)
        self.assertEqual([directory["name"] for directory in directories], ["b", "a", "c"])

    def test_top_directories_skip_the_ones_with_only_added_or_removed_tests(self):
        before = {"/a/1.html": entry("O", 1, 1)}
        after = {"/a/1.html": entry("O", 0, 1), "/new/1.html": entry("O", 1, 1)}
        _, directories = diff.compare(before, after)
        self.assertEqual([directory["name"] for directory in diff.top_directories(directories)], ["a"])
        self.assertNotIn("files", diff.top_directories(directories)[0])


if __name__ == "__main__":
    unittest.main()
