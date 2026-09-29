import collections

PASSING_STATUSES = {"O", "P"}
COUNTERS = ("tests_fixed", "tests_broken", "subtests_fixed", "subtests_broken",
            "added_tests", "added_subtests", "removed_tests", "removed_subtests")
TOP_DIRECTORIES = 5


def fully_passes(entry):
    passing, total = entry["c"]
    return entry["s"] in PASSING_STATUSES and passing == total


def state(entry):
    return [entry["c"][0], entry["c"][1], entry["s"]]


def directory_of(test):
    return test.split("/")[1] if test.count("/") >= 2 else test


def compare(before, after):
    """Compares two wpt.fyi summary files ({test: {"s": status, "c": [passing,
    total]}}) over the tests present in both. Returns (totals, directories),
    with the directories sorted by the tests that stopped passing."""
    directories = collections.defaultdict(lambda: {"counts": collections.Counter(), "files": [], "added": [], "removed": []})
    for test in sorted(set(before) | set(after)):
        directory = directories[directory_of(test)]
        counts = directory["counts"]
        before_entry, after_entry = before.get(test), after.get(test)
        if before_entry is None:
            counts["added_tests"] += 1
            counts["added_subtests"] += after_entry["c"][1]
            directory["added"].append({"test": test, "after": state(after_entry)})
            continue
        if after_entry is None:
            counts["removed_tests"] += 1
            counts["removed_subtests"] += before_entry["c"][1]
            directory["removed"].append({"test": test, "before": state(before_entry)})
            continue
        was_passing, is_passing = fully_passes(before_entry), fully_passes(after_entry)
        test_change = "fixed" if is_passing and not was_passing else "broken" if was_passing and not is_passing else None
        subtest_delta = after_entry["c"][0] - before_entry["c"][0]
        # A status change that moves no counter, like 3/10 OK becoming 3/10
        # ERROR, is still kept for the expanded table, but adds nothing to the totals.
        if test_change is None and subtest_delta == 0 and before_entry["s"] == after_entry["s"]:
            continue
        counts["tests_fixed"] += test_change == "fixed"
        counts["tests_broken"] += test_change == "broken"
        counts["subtests_fixed"] += max(0, subtest_delta)
        counts["subtests_broken"] += max(0, -subtest_delta)
        directory["files"].append({"test": test, "before": state(before_entry), "after": state(after_entry),
                                   "test_change": test_change, "subtests_fixed": max(0, subtest_delta),
                                   "subtests_broken": max(0, -subtest_delta)})

    def file_order(file):
        return (-(file["test_change"] == "broken"), -file["subtests_broken"], -(file["test_change"] == "fixed"), -file["subtests_fixed"], file["test"])

    totals = collections.Counter()
    result = []
    for name, directory in directories.items():
        counts = directory["counts"]
        if not any(counts.values()) and not directory["files"]:
            continue
        totals.update(counts)
        directory["files"].sort(key=file_order)
        result.append({"name": name, **{key: counts[key] for key in COUNTERS},
                       "files": directory["files"], "added": directory["added"], "removed": directory["removed"]})
    result.sort(key=lambda d: (-d["tests_broken"], -d["subtests_broken"], -d["tests_fixed"], -d["subtests_fixed"], d["name"]))
    return {key: totals[key] for key in COUNTERS}, result


def changed(directory):
    return any(directory[key] for key in ("tests_fixed", "tests_broken", "subtests_fixed", "subtests_broken"))


def top_directories(directories, count=TOP_DIRECTORIES):
    return [{key: directory[key] for key in ("name",) + COUNTERS} for directory in directories if changed(directory)][:count]
