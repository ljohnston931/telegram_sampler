"""Pure sampling logic: how many pings a window gets, and when they fire.

No Telegram, no database, no config. Everything comes in as arguments.
"""
import random
from datetime import timedelta


def draw_times(lo, hi, n, min_gap):
    """n sorted random datetimes in [lo, hi), at least min_gap apart where possible."""
    span = (hi - lo).total_seconds()
    gap = min(min_gap.total_seconds(), span / (2 * n)) if n else 0
    for _ in range(1000):
        ts = sorted(random.uniform(0, span) for _ in range(n))
        if all(b - a >= gap for a, b in zip(ts, ts[1:])):
            return [lo + timedelta(seconds=t) for t in ts]
    seg = span / n  # fallback: one per equal stratum
    return [lo + timedelta(seconds=i * seg + random.uniform(0, seg)) for i in range(n)]


def plan_day(rate, start, end, now, min_gap):
    """Ping times for the window [start, end), given it may already be partly over.

    The rate is scaled by the fraction of the window still remaining (partial
    window on first run or after a restart). Returns a sorted list of datetimes.
    """
    lo = max(start, now)
    if end <= lo:
        return []
    n = round(rate * (end - lo) / (end - start))
    return draw_times(lo, end, n, min_gap) if n else []