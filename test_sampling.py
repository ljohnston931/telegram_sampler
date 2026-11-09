import random
from datetime import datetime, timedelta, timezone

import pytest

from sampling import DayPlan, draw_times, plan_day, poisson_count

UTC = timezone.utc
START = datetime(2026, 11, 9, 13, 0, tzinfo=UTC)
END = START + timedelta(hours=18)


@pytest.fixture(autouse=True)
def seed():
    random.seed(12345)


# ---------------------------------------------------------------- poisson_count

class TestPoissonCount:
    def test_rejects_nonpositive_mean(self):
        with pytest.raises(ValueError):
            poisson_count(0)
        with pytest.raises(ValueError):
            poisson_count(-1)

    def test_rejects_bad_bounds(self):
        with pytest.raises(ValueError):
            poisson_count(3, lo=-1)
        with pytest.raises(ValueError):
            poisson_count(3, lo=5, hi=4)

    def test_returns_int_in_bounds(self):
        for _ in range(500):
            n = poisson_count(3, 2, 5)
            assert isinstance(n, int)
            assert 2 <= n <= 5

    def test_lo_equals_hi_is_deterministic(self):
        assert all(poisson_count(3, 4, 4) == 4 for _ in range(50))

    def test_upper_bound_only(self):
        assert all(poisson_count(10, hi=2) <= 2 for _ in range(200))

    def test_lower_bound_only(self):
        assert all(poisson_count(3, lo=6) >= 6 for _ in range(200))

    def test_silly_range_terminates(self):
        # mean 3 with min 15 would loop forever with rejection sampling
        assert poisson_count(3, 15, 20) >= 15

    def test_huge_mean_does_not_underflow(self):
        n = poisson_count(50, 0, 100)
        assert 0 <= n <= 100

    def test_unbounded_mean_close_to_parameter(self):
        draws = [poisson_count(3) for _ in range(5000)]
        assert sum(draws) / len(draws) == pytest.approx(3, abs=0.15)

    def test_unbounded_variance_close_to_mean(self):
        draws = [poisson_count(4) for _ in range(5000)]
        m = sum(draws) / len(draws)
        var = sum((x - m) ** 2 for x in draws) / len(draws)
        assert var == pytest.approx(4, abs=0.4)

    def test_truncation_shifts_mean_up(self):
        draws = [poisson_count(3, lo=3) for _ in range(3000)]
        assert min(draws) >= 3
        assert sum(draws) / len(draws) > 3.5

    def test_zero_is_possible_when_unbounded(self):
        assert any(poisson_count(0.5) == 0 for _ in range(200))


# ------------------------------------------------------------------- draw_times

class TestDrawTimes:
    def test_count_sorted_and_in_range(self):
        ts = draw_times(START, END, 20)
        assert len(ts) == 20
        assert ts == sorted(ts)
        assert all(START <= t < END for t in ts)

    def test_zero_gives_empty(self):
        assert draw_times(START, END, 0) == []

    def test_preserves_timezone(self):
        assert all(t.tzinfo is not None for t in draw_times(START, END, 5))

    def test_roughly_uniform(self):
        ts = draw_times(START, END, 4000)
        first_half = sum(1 for t in ts if t < START + (END - START) / 2)
        assert first_half / len(ts) == pytest.approx(0.5, abs=0.05)


# --------------------------------------------------------------------- plan_day

class TestPlanDay:
    def test_full_window_uses_given_params(self):
        plan = plan_day(3, 2, 6, START, END, now=START - timedelta(hours=1))
        assert isinstance(plan, DayPlan)
        assert plan.mean == 3
        assert (plan.min_n, plan.max_n) == (2, 6)
        assert 2 <= len(plan.times) <= 6
        assert all(START <= t < END for t in plan.times)

    def test_now_exactly_at_start_is_full_window(self):
        plan = plan_day(3, 1, 4, START, END, now=START)
        assert plan.mean == 3
        assert (plan.min_n, plan.max_n) == (1, 4)

    def test_partial_window_scales_mean_and_drops_bounds(self):
        now = START + timedelta(hours=9)  # half the window left
        plan = plan_day(4, 3, 5, START, END, now)
        assert plan.mean == pytest.approx(2.0)
        assert plan.min_n is None and plan.max_n is None
        assert all(now <= t < END for t in plan.times)

    def test_partial_window_ignores_min_even_if_large(self):
        now = START + timedelta(hours=17)
        plan = plan_day(3, 10, 12, START, END, now)
        assert len(plan.times) < 10  # bounds not applied

    def test_window_already_over(self):
        plan = plan_day(3, 1, 5, START, END, now=END)
        assert plan == DayPlan([], 0.0, None, None)
        plan = plan_day(3, 1, 5, START, END, now=END + timedelta(hours=5))
        assert plan.times == []

    def test_times_sorted(self):
        plan = plan_day(20, None, None, START, END, now=START - timedelta(hours=1))
        assert plan.times == sorted(plan.times)

    def test_no_bounds_full_window(self):
        plan = plan_day(3, None, None, START, END, now=START - timedelta(hours=1))
        assert (plan.min_n, plan.max_n) == (None, None)
