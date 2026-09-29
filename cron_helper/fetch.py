import collections
import gzip
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib

USER_AGENT = "webkit-wpt-dashboard/1.0"
GITHUB_API_HOST = "api.github.com"


def sends_github_token(url):
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme == "https" and parsed.hostname == GITHUB_API_HOST


class TokenSafeRedirectHandler(urllib.request.HTTPRedirectHandler):
    """urllib copies every header to the target of a redirect, the token
    included, so it has to be dropped when the redirect leaves the GitHub API."""

    def redirect_request(self, request, fp, code, message, headers, new_url):
        redirected = super().redirect_request(request, fp, code, message, headers, new_url)
        if redirected is not None and not sends_github_token(new_url):
            redirected.remove_header("Authorization")
        return redirected


def default_opener():
    return urllib.request.build_opener(TokenSafeRedirectHandler).open


class FetchError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Fetcher:
    def __init__(self, logger, github_token, attempts=3, timeout=90, retry_delays=(10, 30), sleep=time.sleep, opener=None):
        self.logger = logger
        self.github_token = github_token
        self.attempts = attempts
        self.timeout = timeout
        self.retry_delays = retry_delays
        self.sleep = sleep
        self.opener = opener or default_opener()
        self.request_counts = collections.Counter()

    def get_json(self, url, service):
        return self.get_bytes(url, service, decode=json.loads)

    def get_bytes(self, url, service, decode=lambda body: body):
        """Reading and decoding happen inside the retries, because a
        connection that drops halfway can still answer 200 with a truncated
        body."""
        if urllib.parse.urlparse(url).scheme != "https":
            raise FetchError(f"{url}: only https addresses are fetched")
        request = urllib.request.Request(url, headers=self._headers(url))
        last_error = None
        for attempt in range(1, self.attempts + 1):
            self.request_counts[service] += 1
            try:
                with self.opener(request, timeout=self.timeout) as response:
                    body = response.read()
                    content_encoding = response.headers.get("Content-Encoding", "")
            except urllib.error.HTTPError as error:
                last_error = f"HTTP {error.code} {error.reason}"
                # A client error will not go away by asking again, except for rate limits.
                if error.code < 500 and error.code not in (403, 429):
                    raise FetchError(self._without_token(f"{url}: {last_error}"), status=error.code) from None
            except (urllib.error.URLError, http.client.HTTPException, TimeoutError, OSError) as error:
                last_error = f"{type(error).__name__}: {getattr(error, 'reason', error)}"
            except ValueError as error:
                # Raised while building the request, for example by a header
                # that http.client rejects, and its message quotes that header,
                # which can be the token. Asking again would fail the same way.
                raise FetchError(f"{url}: the request could not be built ({type(error).__name__})") from None
            else:
                try:
                    if content_encoding == "gzip" or body[:2] == b"\x1f\x8b":
                        body = gzip.decompress(body)
                    decoded = decode(body)
                except (EOFError, gzip.BadGzipFile, zlib.error, ValueError) as error:
                    last_error = f"{type(error).__name__}: {error}"
                else:
                    if last_error:
                        self.logger.warning(self._without_token(f"{url} succeeded on attempt {attempt} of {self.attempts}, after: {last_error}"))
                    return decoded
            if attempt < self.attempts:
                self.sleep(self.retry_delays[min(attempt, len(self.retry_delays)) - 1])
        raise FetchError(self._without_token(f"{url}: {last_error}, after {self.attempts} attempts"))

    def _without_token(self, text):
        return text.replace(self.github_token, "***") if self.github_token else text

    def _headers(self, url):
        headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
        if sends_github_token(url):
            headers["Authorization"] = f"Bearer {self.github_token}"
            headers["Accept"] = "application/vnd.github+json"
            headers["X-GitHub-Api-Version"] = "2022-11-28"
        return headers
