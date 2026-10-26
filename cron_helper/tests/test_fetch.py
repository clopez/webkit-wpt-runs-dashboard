import gzip
import http.client
import io
import logging
import unittest
import urllib.error
import urllib.request

from fetch import AnswerTooLarge, Fetcher, FetchError, TokenSafeRedirectHandler


class FakeResponse(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(body)
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *arguments):
        self.close()


class FakeOpener:
    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def http_error(code):
    return urllib.error.HTTPError("https://example.org", code, "Error", {}, None)


class FetcherTest(unittest.TestCase):
    def fetcher(self, opener):
        self.logger = logging.getLogger("test_fetch")
        return Fetcher(self.logger, "secret-token", sleep=lambda seconds: None, opener=opener)

    def test_retries_server_errors_and_warns_when_a_retry_succeeds(self):
        opener = FakeOpener(http_error(503), FakeResponse(b'{"ok": true}'))
        with self.assertLogs("test_fetch", level="WARNING") as captured:
            self.assertEqual(self.fetcher(opener).get_json("https://wpt.fyi/api/runs", "wptfyi"), {"ok": True})
        self.assertIn("succeeded on attempt 2 of 3, after: HTTP 503 Error", captured.output[0])

    def test_gives_up_after_three_attempts(self):
        fetcher = self.fetcher(FakeOpener(http_error(500), urllib.error.URLError("timed out"), http_error(502)))
        with self.assertRaisesRegex(FetchError, "HTTP 502 Error, after 3 attempts"):
            fetcher.get_json("https://wpt.fyi/api/runs", "wptfyi")
        self.assertEqual(fetcher.request_counts["wptfyi"], 3)

    def test_client_errors_are_not_retried_and_keep_their_status(self):
        opener = FakeOpener(http_error(404))
        with self.assertRaises(FetchError) as raised:
            self.fetcher(opener).get_json("https://wpt.fyi/api/runs", "wptfyi")
        self.assertEqual(raised.exception.status, 404)
        self.assertEqual(len(opener.requests), 1)

    def test_a_truncated_answer_is_retried_and_reported_as_a_request_error(self):
        truncated = lambda: FakeResponse(b'{"check_runs": [')
        with self.assertRaisesRegex(FetchError, "JSONDecodeError"):
            self.fetcher(FakeOpener(truncated(), truncated(), truncated())).get_json("https://wpt.fyi/api/runs", "wptfyi")
        with self.assertLogs("test_fetch", level="WARNING"):
            self.assertEqual(self.fetcher(FakeOpener(truncated(), FakeResponse(b"[]"))).get_json("https://wpt.fyi/api/runs", "wptfyi"), [])

    def test_a_connection_that_closes_early_is_retried(self):
        opener = FakeOpener(http.client.IncompleteRead(b"{", 10), FakeResponse(b"{}"))
        with self.assertLogs("test_fetch", level="WARNING") as captured:
            self.assertEqual(self.fetcher(opener).get_json("https://wpt.fyi/api/runs", "wptfyi"), {})
        self.assertIn("IncompleteRead", captured.output[0])

    def test_a_corrupt_gzip_body_is_retried_and_reported_as_a_request_error(self):
        corrupt = gzip.compress(b'{"a": 1}')[:12] + b"\xff" * 20
        opener = FakeOpener(*[FakeResponse(corrupt, {"Content-Encoding": "gzip"}) for _ in range(3)])
        with self.assertRaises(FetchError):
            self.fetcher(opener).get_json("https://storage.googleapis.com/x.json.gz", "wptfyi")
        self.assertEqual(len(opener.requests), 3)

    def test_a_token_that_http_rejects_is_never_quoted_nor_retried(self):
        fetcher = Fetcher(logging.getLogger("test_fetch"), "ghp_FAKE0123456789\r", sleep=lambda seconds: None)
        with self.assertRaises(FetchError) as raised:
            fetcher.get_json("https://api.github.com/repos/web-platform-tests/wpt", "github")
        self.assertNotIn("ghp_FAKE0123456789", str(raised.exception))
        self.assertEqual(fetcher.request_counts["github"], 1)

    def test_the_start_of_a_compressed_file_is_read_with_a_range(self):
        log = "".join(f"line {number}\n" for number in range(20000)).encode()
        opener = FakeOpener(FakeResponse(gzip.compress(log)[:4096], {"Content-Encoding": "gzip"}))
        text = self.fetcher(opener).get_bytes("https://community-tc.example/log", "taskcluster", decode=bytes.decode, first_bytes=4096)
        self.assertEqual(opener.requests[0].get_header("Range"), "bytes=0-4095")
        self.assertTrue(text.startswith("line 0\nline 1\n"))

    def limited(self, opener, limit=1000):
        return Fetcher(logging.getLogger("test_fetch"), "secret-token", sleep=lambda seconds: None, opener=opener, max_answer_bytes=limit)

    def test_an_answer_over_the_limit_is_refused_and_not_asked_again(self):
        opener = FakeOpener(FakeResponse(b"[" + b"1," * 600 + b"1]"))
        with self.assertRaisesRegex(AnswerTooLarge, "larger than"):
            self.limited(opener).get_json("https://wpt.fyi/api/runs", "wptfyi")
        self.assertEqual(len(opener.requests), 1)

    def test_a_small_compressed_answer_that_expands_over_the_limit_is_refused(self):
        bomb = gzip.compress(b" " * 100_000)
        self.assertLess(len(bomb), 1000)
        opener = FakeOpener(FakeResponse(bomb, {"Content-Encoding": "gzip"}))
        with self.assertRaises(AnswerTooLarge):
            self.limited(opener).get_json("https://wpt.fyi/api/runs", "wptfyi")
        self.assertEqual(len(opener.requests), 1)

    def test_an_answer_at_the_limit_is_read(self):
        body = b"[" + b"1," * 498 + b"1]"
        self.assertEqual(len(body), 999)
        self.assertEqual(len(self.limited(FakeOpener(FakeResponse(gzip.compress(body), {"Content-Encoding": "gzip"}))).get_json("https://wpt.fyi/x", "wptfyi")), 499)

    def test_a_whole_compressed_answer_cut_short_is_asked_again(self):
        cut = gzip.compress(b'{"a": 1}')[:-8]
        opener = FakeOpener(FakeResponse(cut, {"Content-Encoding": "gzip"}), FakeResponse(gzip.compress(b'{"a": 1}'), {"Content-Encoding": "gzip"}))
        with self.assertLogs("test_fetch", level="WARNING") as captured:
            self.assertEqual(self.fetcher(opener).get_json("https://wpt.fyi/x", "wptfyi"), {"a": 1})
        self.assertIn("ends too early", captured.output[0])

    def test_decompresses_gzip(self):
        opener = FakeOpener(FakeResponse(gzip.compress(b'{"a": 1}'), {"Content-Encoding": "gzip"}))
        self.assertEqual(self.fetcher(opener).get_json("https://storage.googleapis.com/x.json.gz", "wptfyi"), {"a": 1})

    def test_the_token_is_only_sent_to_the_github_api(self):
        opener = FakeOpener(FakeResponse(b"{}"), FakeResponse(b"{}"))
        fetcher = self.fetcher(opener)
        fetcher.get_json("https://api.github.com/repos/web-platform-tests/wpt", "github")
        fetcher.get_json("https://wpt.fyi/api/runs", "wptfyi")
        self.assertEqual(opener.requests[0].get_header("Authorization"), "Bearer secret-token")
        self.assertIsNone(opener.requests[1].get_header("Authorization"))

    def test_only_https_addresses_are_fetched(self):
        opener = FakeOpener(FakeResponse(b"{}"))
        for url in ("http://api.github.com/repos/web-platform-tests/wpt", "file:///etc/passwd"):
            with self.assertRaisesRegex(FetchError, "only https"):
                self.fetcher(opener).get_json(url, "github")
        self.assertEqual(opener.requests, [])


class TokenSafeRedirectHandlerTest(unittest.TestCase):
    def redirect(self, new_url):
        request = urllib.request.Request("https://api.github.com/repos/a/b", headers={"Authorization": "Bearer secret-token"})
        return TokenSafeRedirectHandler().redirect_request(request, None, 302, "Found", {}, new_url)

    def test_a_redirect_to_another_host_drops_the_token(self):
        self.assertIsNone(self.redirect("https://elsewhere.example/steal").get_header("Authorization"))

    def test_a_redirect_to_plain_http_drops_the_token(self):
        self.assertIsNone(self.redirect("http://api.github.com/repos/a/b").get_header("Authorization"))

    def test_a_redirect_within_the_github_api_keeps_the_token(self):
        self.assertEqual(self.redirect("https://api.github.com/repositories/1").get_header("Authorization"), "Bearer secret-token")


if __name__ == "__main__":
    unittest.main()
