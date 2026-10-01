import collections
import datetime
import logging
import os
import pathlib
import sys

# Same format as WebKit's Tools/Scripts/filter-test-logs.
TIMESTAMP_FORMAT = "%Y-%m-%d|%H:%M:%S|%Z"
VISIBLE_DAYS = 30
KEPT_DAYS = 90
BLOCK_DAYS = 30
BLOCK_EPOCH = datetime.date(2000, 1, 1)
LOGGER_NAME = "webkit_wpt_runs_dashboard"
REPOSITORY = pathlib.Path(__file__).resolve().parent.parent


def without_local_paths(text):
    """The log and the errors in dashboard.json are public, so paths inside the
    repository are written relative to it, keeping where it lives on the
    server private. Paths given outside the repository stay as they are."""
    return text.replace(f"{REPOSITORY}{os.sep}", "")


class DashboardFormatter(logging.Formatter):
    PREFIXES = {logging.ERROR: "ERROR: ", logging.CRITICAL: "ERROR: ", logging.WARNING: "WARNING: "}

    def format(self, record):
        timestamp = datetime.datetime.fromtimestamp(record.created).astimezone().strftime(TIMESTAMP_FORMAT)
        message = without_local_paths(record.getMessage()).replace("\r", " ").replace("\n", " | ")
        return f"[{timestamp}] {self.PREFIXES.get(record.levelno, '')}{message}"


class ProblemRecorder(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.errors = []
        self.warnings = []

    def emit(self, record):
        message = without_local_paths(record.getMessage()).replace("\n", " | ")
        (self.errors if record.levelno >= logging.ERROR else self.warnings).append(message)


def setup_logger(log_path=None):
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    if log_path:
        pathlib.Path(log_path).parent.mkdir(parents=True, exist_ok=True)
    output = logging.FileHandler(log_path, encoding="utf-8") if log_path else logging.StreamHandler(sys.stderr)
    output.setFormatter(DashboardFormatter())
    logger.addHandler(output)
    recorder = ProblemRecorder()
    logger.addHandler(recorder)
    return logger, recorder


def line_date(line):
    try:
        return datetime.date.fromisoformat(line[1:11]) if line.startswith("[") else None
    except ValueError:
        return None


def block_start(date):
    return BLOCK_EPOCH + datetime.timedelta(days=(date - BLOCK_EPOCH).days // BLOCK_DAYS * BLOCK_DAYS)


def archive_path(log_path, archive_dir, start):
    return pathlib.Path(archive_dir) / f"{pathlib.Path(log_path).name.removesuffix('.txt')}.{start.isoformat()}.txt"


def write_atomically(path, text):
    temporary = pathlib.Path(f"{path}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, path)


def split_by_age(lines, cutoff):
    """Returns (lines to keep, old lines by the start of their block). A line
    without a timestamp stays with the line before it."""
    kept = []
    old_by_block = collections.defaultdict(list)
    current_date = None
    for line in lines:
        current_date = line_date(line) or current_date
        if current_date is not None and current_date < cutoff:
            old_by_block[block_start(current_date)].append(line)
        else:
            kept.append(line)
    return kept, old_by_block


def rotate(log_path, archive_dir, today):
    log_path = pathlib.Path(log_path)
    if not log_path.exists():
        prune_archives(log_path, archive_dir, today)
        return
    # A line cut in the middle of a character, for example by a full disk, must not stop every later run.
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    kept, old_by_block = split_by_age(lines, today - datetime.timedelta(days=VISIBLE_DAYS - 1))
    if old_by_block:
        pathlib.Path(archive_dir).mkdir(parents=True, exist_ok=True)
        for start, block_lines in sorted(old_by_block.items()):
            with archive_path(log_path, archive_dir, start).open("a", encoding="utf-8") as archive:
                archive.writelines(block_lines)
        write_atomically(log_path, "".join(kept))
    prune_archives(log_path, archive_dir, today)


def prune_archives(log_path, archive_dir, today):
    archive_dir = pathlib.Path(archive_dir)
    if not archive_dir.is_dir():
        return
    cutoff = today - datetime.timedelta(days=KEPT_DAYS)
    prefix = f"{pathlib.Path(log_path).name.removesuffix('.txt')}."
    for archive in archive_dir.glob(f"{prefix}*.txt"):
        try:
            start = datetime.date.fromisoformat(archive.name.removeprefix(prefix).removesuffix(".txt"))
        except ValueError:
            continue
        if start + datetime.timedelta(days=BLOCK_DAYS) <= cutoff:
            archive.unlink()
        elif start < cutoff:
            kept, _ = split_by_age(archive.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True), cutoff)
            write_atomically(archive, "".join(kept))
