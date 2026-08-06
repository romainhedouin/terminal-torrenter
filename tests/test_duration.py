"""Unit tests for ETA computation and human-readable duration formatting."""


import pytest

from terminal_torrenter.core import eta_seconds, human_duration

MINUTE = 60
HOUR = 60 * MINUTE
DAY = 24 * HOUR


@pytest.mark.parametrize(
    "seconds, expected",
    [
        # Sub-minute: seconds only.
        (0.4, "0 s"),
        (1, "1 s"),
        (45, "45 s"),
        (59, "59 s"),
        # Minutes + seconds.
        (60, "1 min 0 s"),
        (90, "1 min 30 s"),
        (170, "2 min 50 s"),
        (HOUR - 1, "59 min 59 s"),
        # Hours + minutes.
        (HOUR, "1 h 0 min"),
        (HOUR + 1, "1 h 0 min"),
        (15 * HOUR + 42 * MINUTE, "15 h 42 min"),
        (DAY - 1, "23 h 59 min"),
        # Days + hours.
        (DAY, "1 d 0 h"),
        (DAY + 20 * HOUR, "1 d 20 h"),
        (2 * DAY + 3 * HOUR + 59 * MINUTE, "2 d 3 h"),
    ],
)
def test_human_duration_uses_two_significant_units(seconds, expected):
    assert human_duration(seconds) == expected


@pytest.mark.parametrize("seconds", [0, -5, float("inf")])
def test_human_duration_unknown_returns_dash(seconds):
    assert human_duration(seconds) == "—"


def test_human_duration_never_shows_days_below_one_day():
    # Regression: anything under 24h must not render in days.
    for seconds in range(0, DAY, 911):
        assert " d " not in human_duration(seconds), seconds


def test_human_duration_only_shows_days_from_one_day_up():
    assert human_duration(DAY - 1) == "23 h 59 min"
    assert human_duration(DAY).endswith("d 0 h") or human_duration(DAY) == "1 d 0 h"


# --------------------------------------------------------------------------- #
# ETA computation
# --------------------------------------------------------------------------- #


def test_eta_normal_case():
    # 100 MB left at 1 MB/s -> 100 seconds.
    assert eta_seconds(
        total_bytes=200_000_000, done_bytes=100_000_000, rate_bytes_per_s=1_000_000
    ) == pytest.approx(100)


def test_eta_is_infinite_when_rate_is_zero():
    assert eta_seconds(total_bytes=1_000, done_bytes=0, rate_bytes_per_s=0) == float("inf")


def test_eta_is_infinite_when_complete():
    assert eta_seconds(total_bytes=1_000, done_bytes=1_000, rate_bytes_per_s=500) == float("inf")


def test_eta_is_infinite_when_total_unknown():
    assert eta_seconds(total_bytes=0, done_bytes=0, rate_bytes_per_s=500) == float("inf")


def test_eta_formats_to_minutes_for_nearly_done_download():
    # The reported bug: a download finishing in ~1 minute must NOT show days.
    # 30 MB remaining at 1 MB/s = 30 s.
    seconds = eta_seconds(
        total_bytes=1_030_000_000, done_bytes=1_000_000_000, rate_bytes_per_s=1_000_000
    )
    assert human_duration(seconds) == "30 s"


def test_eta_shows_about_a_day_only_for_genuinely_large_remaining():
    # 38 GiB remaining at 460 KiB/s is genuinely ~24h -> days is correct.
    rate = 460 * 1024
    total = 38 * 1024**3
    seconds = eta_seconds(total_bytes=total, done_bytes=0, rate_bytes_per_s=rate)
    assert seconds > DAY  # sanity: this really is more than a day
    assert human_duration(seconds).endswith("h")
    assert human_duration(seconds).startswith("1 d")
