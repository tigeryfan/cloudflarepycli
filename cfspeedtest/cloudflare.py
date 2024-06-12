"""
Library for the Cloudflare speedtest suite.

This uses endpoints from speed.cloudflare.com.
"""

from __future__ import annotations

import statistics
import time
from enum import Enum
from typing import TYPE_CHECKING, Any, NamedTuple

import requests

if TYPE_CHECKING:
    from logging import Logger


class TestType(Enum):
    """The type of an individual test."""

    Down = "GET"
    Up = "POST"


class TestSpec(NamedTuple):
    """The specifications of an individual test."""

    size: int
    iterations: int
    name: str
    type: TestType

    @property
    def bits(self) -> int:
        """Convert the size of the test in bytes to bits."""
        return self.size * 8


TestSpecs = tuple[TestSpec, ...]

DOWNLOAD_TESTS: TestSpecs = (
    TestSpec(100_000, 10, "100kB", TestType.Down),
    TestSpec(1_000_000, 8, "1MB", TestType.Down),
    TestSpec(10_000_000, 6, "10MB", TestType.Down),
    TestSpec(25_000_000, 4, "25MB", TestType.Down),
)
UPLOAD_TESTS: TestSpecs = (
    TestSpec(100_000, 8, "100kB", TestType.Up),
    TestSpec(1_000_000, 6, "1MB", TestType.Up),
    TestSpec(10_000_000, 4, "10MB", TestType.Up),
)
DEFAULT_TESTS: TestSpecs = (
    TestSpec(1, 20, "latency", TestType.Down),
    *DOWNLOAD_TESTS,
    *UPLOAD_TESTS,
)


class TestResult(NamedTuple):
    """The result of an individual test."""

    value: Any
    time: float = time.time()


class TestTimers(NamedTuple):
    """
    A collection of test timer collections.

    Here, `full` is the actual time taken to prepare and perform the request,
    `server` is the time taken to process the request as reported by the worker,
    and `request` is the internal client time elapsed to complete the request.
    """

    full: list[float]
    server: list[float]
    request: list[float]


class TestMetadata(NamedTuple):
    """The metadata of a test suite."""

    ip: str
    isp: str
    location_code: str
    region: str
    city: str


class CloudflareSpeedtest:
    """Suite of speedtests."""

    def __init__(  # noqa: D417
        self,
        results: dict[str, TestResult] | None = None,
        tests: TestSpecs = DEFAULT_TESTS,
        timeout: tuple[float, float] | float = (3.05, 25),
        logger: Logger | None = None,
    ) -> None:
        """
        Initialize the test suite.

        Arguments:
        ---------
        - `results`: A dictionary of test results. This can be used to include
        results from previous runs.
        - `tests`: The specifications (see `TestSpec`) for all tests to run.
        - `timeout`: The timeout settings for all requests. See the Timeouts
        page of the `requests` documentation for more information.
        - `logger`: The logger that `CloudflareSpeedtest` will use when it
        runs tests, exclusively via `run_all`. When this is set to None,
        no logging will occur.

        """
        self.results = results or {}
        self.tests = tests
        self.request_sess = requests.Session()
        self.timeout = timeout
        self.logger = logger

    def metadata(self) -> TestMetadata:
        """Retrieve test location code, IP address, ISP, city, and region."""
        result_data: dict[str, str] = self.request_sess.get(
            "https://speed.cloudflare.com/meta"
        ).json()
        return TestMetadata(
            result_data["clientIp"],
            result_data["asOrganization"],
            result_data["colo"],
            result_data["region"],
            result_data["city"],
        )

    def run_test(self, test: TestSpec) -> TestTimers:
        """Run a test specification iteratively and collect timers."""
        coll = TestTimers([], [], [])
        url = f"https://speed.cloudflare.com/__down?bytes={test.size}"
        data = None
        if test.type == TestType.Up:
            url = "https://speed.cloudflare.com/__up"
            data = b"".zfill(test.size)

        for _ in range(test.iterations):
            start = time.time()
            r = self.request_sess.request(
                test.type.value, url, data=data, timeout=self.timeout
            )
            coll.full.append(time.time() - start)
            coll.server.append(
                float(r.headers["Server-Timing"].split("=")[1]) / 1e3
            )
            coll.request.append(
                r.elapsed.seconds + r.elapsed.microseconds / 1e6
            )
        return coll

    def sprint(self, label: str, result: TestResult) -> None:
        """Add an entry to the suite results and log it."""
        if self.logger:
            self.logger.info("%s: %s", label, result.value)
        self.results[label] = result

    @staticmethod
    def calculate_percentile(data: list[float], percentile: float) -> float:
        """Find the percentile of a list of values."""
        data = sorted(data)
        idx = (len(data) - 1) * percentile
        rem = idx % 1

        if rem == 0:
            return data[int(idx)]

        edges = (data[int(idx)], data[int(idx) + 1])
        return edges[0] + (edges[1] - edges[0]) * rem

    def run_all(self) -> dict[str, TestResult]:
        """Run the full test suite."""
        meta = self.metadata()
        self.sprint("ip", TestResult(meta.ip))
        self.sprint("isp", TestResult(meta.isp))
        self.sprint("location_code", TestResult(meta.location_code))
        self.sprint("location_city", TestResult(meta.city))
        self.sprint("location_region", TestResult(meta.region))

        data = {"down": [], "up": []}
        for test in self.tests:
            timers = self.run_test(test)

            if test.name == "latency":
                latencies = [
                    (timers.request[i] - timers.server[i]) * 1e3
                    for i in range(len(timers.request))
                ]
                jitter = statistics.median(
                    [
                        abs(latencies[i] - latencies[i - 1])
                        for i in range(1, len(latencies))
                    ]
                )
                self.sprint(
                    "latency",
                    TestResult(round(statistics.median(latencies), 2)),
                )
                self.sprint("jitter", TestResult(round(jitter, 2)))
                continue

            if test.type == TestType.Down:
                speeds = [
                    int(test.bits / (timers.full[i] - timers.server[i]))
                    for i in range(len(timers.full))
                ]
            else:
                speeds = [
                    int(test.bits / server_time)
                    for server_time in timers.server
                ]
            data[test.type.name.lower()].extend(speeds)
            self.sprint(
                f"{test.name}_{test.type.name.lower()}_bps",
                TestResult(int(statistics.mean(speeds))),
            )
        for k, v in data.items():
            self.sprint(
                f"90th_percentile_{k}_bps",
                TestResult(int(self.calculate_percentile(v, 0.9))),
            )

        return self.results
