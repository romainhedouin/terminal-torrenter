"""Tests for the ETA-smoothing moving average."""



from terminal_torrenter.core import ETA_WINDOW_SECONDS, MovingAverage, eta_seconds, human_duration


def test_empty_average_is_zero():
    assert MovingAverage().average(now=100.0) == 0.0


def test_single_sample_returns_that_value():
    avg = MovingAverage(window_seconds=10)
    avg.add(500.0, now=0.0)
    assert avg.average(now=0.0) == 500.0


def test_average_of_samples_within_window():
    avg = MovingAverage(window_seconds=10)
    for t, rate in enumerate([100, 200, 300]):  # t = 0, 1, 2
        avg.add(rate, now=float(t))
    assert avg.average(now=2.0) == 200.0


def test_old_samples_outside_window_are_dropped():
    avg = MovingAverage(window_seconds=10)
    avg.add(1000.0, now=0.0)   # will fall out of the window
    avg.add(100.0, now=11.0)
    avg.add(200.0, now=12.0)
    # at now=12 the 1000 sample (t=0) is older than 10s and excluded
    assert avg.average(now=12.0) == 150.0


def test_default_window_is_ten_seconds():
    assert ETA_WINDOW_SECONDS == 10.0


def test_smoothing_stabilises_a_spiky_rate():
    """A momentary stall should not blow the ETA up to days once smoothed."""
    avg = MovingAverage(window_seconds=10)
    rates = [1_000_000] * 9 + [1_000]  # steady 1 MB/s then a 1-second stall
    for t, rate in enumerate(rates):
        avg.add(rate, now=float(t))

    remaining = 30_000_000  # 30 MB left

    # Instantaneous rate during the stall gives an absurd ETA...
    instant_eta = eta_seconds(remaining, 0, 1_000)
    assert human_duration(instant_eta).endswith(("h", "min"))  # minutes/hours, way off

    # ...but the smoothed rate keeps it realistic (~33s).
    smoothed_eta = eta_seconds(remaining, 0, avg.average(now=9.0))
    assert smoothed_eta < 60
    assert human_duration(smoothed_eta).endswith("s")
