"""Pure sampling logic: how many pings a window gets, and when they fire.

No Telegram, no database, no config. Everything comes in as arguments.

Each window's ping count is drawn from a Poisson distribution, optionally
truncated to [min_n, max_n]; the pings are then placed uniformly at random in
the window. With no bounds this is exactly a Poisson process: how many pings
you've had so far says nothing about how many are still to come.
"""
import math
import random
from datetime import timedelta
from typing import NamedTuple, Optional


def poisson_count(mean, lo=None, hi=None):
    """Draw from Poisson(mean) conditioned on lo <= k <= hi (None = no bound on that side).

    Samples directly from the truncated distribution, which is the same as
    redrawing until the count is in range but can never loop forever, even for a
    silly range like mean 3 with min 15. Probabilities are computed in log space
    so extreme ranges don't underflow.
    """
    if mean <= 0:
        raise ValueError("mean must be > 0")
    lo = 0 if lo is None else lo
    if lo < 0 or (hi is not None and lo > hi):
        raise ValueError("need 0 <= min <= max")
    # With no upper bound, stop far enough out that the remaining tail mass is negligible.
    top = hi if hi is not None else max(lo, int(mean + 10 * math.sqrt(mean))) + 50
    ks = range(lo, top + 1)
    logp = [-mean + k * math.log(mean) - math.lgamma(k + 1) for k in ks]
    peak = max(logp)
    return random.choices(ks, weights=[math.exp(x - peak) for x in logp])[0]


def draw_times(lo, hi, n):
    """n sorted datetimes placed uniformly at random in [lo, hi)."""
    span = (hi - lo).total_seconds()
    return sorted(lo + timedelta(seconds=random.uniform(0, span)) for _ in range(n))


class DayPlan(NamedTuple):
    times: list
    mean: float              # the Poisson mean actually drawn from (scaled for a partial window)
    min_n: Optional[int]     # bounds actually applied; None = no bound (always None for partial windows)
    max_n: Optional[int]


def plan_day(mean, min_n, max_n, start, end, now):
    """Plan the window [start, end), which may already be partly over.

    Full window: draw the count from Poisson(mean) truncated to [min_n, max_n].
    Partial window (first run or restart mid-window): scale the mean by the
    fraction remaining and skip the bounds, since a whole-day min/max doesn't
    mean anything for part of a day. The returned DayPlan records what was drawn.
    """
    lo = max(start, now)
    if end <= lo:
        return DayPlan([], 0.0, None, None)
    if lo > start:
        mean *= (end - lo) / (end - start)
        min_n = max_n = None
    n = poisson_count(mean, min_n, max_n)
    return DayPlan(draw_times(lo, end, n), mean, min_n, max_n)