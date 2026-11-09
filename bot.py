"""Random-sampling life logger: Telegram bot (MVP).

Pings at random times; each reply is stored with the *ping's* timestamp.
All times are stored as UTC ISO strings; TZ in config is for scheduling/display only.
"""
import logging
import os
import re
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, time as dtime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as dparser
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters

from sampling import plan_day

load_dotenv()

BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNER = int(os.environ["OWNER_CHAT_ID"])
TZ = ZoneInfo(os.environ.get("TZ_NAME", "America/New_York"))
DB_PATH = os.environ.get("DB_PATH", "sampler.db")
DEFAULT_MEAN = os.environ.get("PINGS_MEAN", os.environ.get("PINGS_PER_DAY", "3"))  # Poisson mean pings/day
DEFAULT_MIN = os.environ.get("PINGS_MIN", "none")  # whole number, or "none" for no lower bound
DEFAULT_MAX = os.environ.get("PINGS_MAX", "none")  # whole number, or "none" for no upper bound
START_H = int(os.environ.get("WINDOW_START_HOUR", "8"))
END_H = int(os.environ.get("WINDOW_END_HOUR", "2"))  # may be < START_H (crosses midnight)
GRACE = timedelta(minutes=int(os.environ.get("LATE_GRACE_MIN", "15")))  # older than this -> 'missed', not sent late
PROMPT = os.environ.get("PING_TEXT", "Sample please")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sampler")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pings(
  id INTEGER PRIMARY KEY, scheduled_at TEXT NOT NULL, sent_at TEXT, telegram_msg_id INTEGER,
  status TEXT NOT NULL DEFAULT 'pending',  -- pending|sent|answered|skipped|missed|paused
  silent INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS entries(
  id INTEGER PRIMARY KEY, ping_id INTEGER REFERENCES pings(id), sampled_at TEXT NOT NULL,
  entered_at TEXT NOT NULL, text TEXT NOT NULL, source TEXT NOT NULL DEFAULT 'text',
  mood INTEGER, energy INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS settings_log(id INTEGER PRIMARY KEY, setting TEXT, value TEXT, effective_from TEXT);
CREATE TABLE IF NOT EXISTS pauses(id INTEGER PRIMARY KEY, start TEXT NOT NULL, end TEXT NOT NULL);
-- one row per sampling window: the distribution the count was drawn from, and the count itself.
-- mean is the Poisson mean actually used (scaled down for a partial window); min_n/max_n are NULL
-- when unbounded or not applied. Rows from before the Poisson change have NULL mean/min_n/max_n.
CREATE TABLE IF NOT EXISTS day_plans(window_date TEXT PRIMARY KEY, n_pings INTEGER, window_start TEXT, window_end TEXT,
  mean REAL, min_n INTEGER, max_n INTEGER);
"""


@contextmanager
def db():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_db():
    with db() as c:
        c.execute("PRAGMA journal_mode=WAL")
        c.executescript(SCHEMA)
        # migrate a day_plans table created before the Poisson change
        have = {r["name"] for r in c.execute("PRAGMA table_info(day_plans)")}
        for col, typ in (("mean", "REAL"), ("min_n", "INTEGER"), ("max_n", "INTEGER")):
            if col not in have:
                c.execute(f"ALTER TABLE day_plans ADD COLUMN {col} {typ}")


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse(s):
    return datetime.fromisoformat(s)


def fmt_local(dt):
    return dt.astimezone(TZ).strftime("%a %H:%M")


def get_setting(key, default):
    with db() as c:
        r = c.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return r["value"] if r else default


MEAN_MIN, MEAN_MAX = 0.1, 50.0  # accepted range for /mean
BOUND_MAX = 100                 # largest accepted value for /min and /max


def to_bound(s):
    """Stored/env text -> int, or None for 'none'."""
    s = str(s).strip().lower()
    return None if s in ("", "none") else int(s)


def get_params():
    """Current (mean, min_n, max_n). Falls back to the old pings_per_day setting if /mean was never used."""
    mean = float(get_setting("mean", get_setting("pings_per_day", DEFAULT_MEAN)))
    return mean, to_bound(get_setting("min_n", DEFAULT_MIN)), to_bound(get_setting("max_n", DEFAULT_MAX))


# ---------------------------------------------------------------- scheduler

def window(d):
    """Sampling window for local date d: starts START_H on d, may run past midnight."""
    start = datetime.combine(d, dtime(START_H), tzinfo=TZ).astimezone(timezone.utc)
    hours = (END_H - START_H) % 24 or 24
    return start, start + timedelta(hours=hours)


def ensure_plan(d, now):
    """Generate the day's pings once. Idempotent via day_plans, so restarts keep the schedule."""
    start, end = window(d)
    with db() as c:
        if c.execute("SELECT 1 FROM day_plans WHERE window_date=?", (d.isoformat(),)).fetchone():
            return
        plan = plan_day(*get_params(), start, end, now)
        c.executemany("INSERT INTO pings(scheduled_at) VALUES(?)", [(iso(t),) for t in plan.times])
        c.execute("INSERT INTO day_plans(window_date, n_pings, window_start, window_end, mean, min_n, max_n) "
                  "VALUES(?,?,?,?,?,?,?)",
                  (d.isoformat(), len(plan.times), iso(start), iso(end), plan.mean, plan.min_n, plan.max_n))
        log.info("plan %s: mean=%.3g min=%s max=%s generated=%s",
                 d, plan.mean, plan.min_n, plan.max_n, len(plan.times))


def is_paused(now):
    with db() as c:
        return c.execute("SELECT 1 FROM pauses WHERE start<=? AND end>?", (iso(now), iso(now))).fetchone() is not None


async def tick(ctx: ContextTypes.DEFAULT_TYPE):
    now = utcnow()
    today = now.astimezone(TZ).date()
    for d in (today - timedelta(days=1), today):
        ensure_plan(d, now)
    paused = is_paused(now)
    with db() as c:
        due = c.execute("SELECT * FROM pings WHERE status='pending' AND scheduled_at<=? ORDER BY scheduled_at",
                        (iso(now),)).fetchall()
    for p in due:
        if now - parse(p["scheduled_at"]) > GRACE:
            status = "missed"  # bot was down; don't send a stale ping
        elif paused:
            status = "paused"
        else:
            msg = await ctx.bot.send_message(OWNER, PROMPT)
            with db() as c:
                c.execute("UPDATE pings SET status='sent', sent_at=?, telegram_msg_id=? WHERE id=?",
                          (iso(now), msg.message_id, p["id"]))
            continue
        with db() as c:
            c.execute("UPDATE pings SET status=? WHERE id=?", (status, p["id"]))


# ---------------------------------------------------------- entry handling

NOW_RE = re.compile(r"^now\b[:,]?\s*(.+)$", re.I | re.S)
AT_RE = re.compile(r"^@(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+(.+)$", re.I | re.S)
REL_RE = re.compile(r"^-(?:(\d+)h)?(?:(\d+)m)?\s+(.+)$", re.I | re.S)


def parse_at(h, m, ampm, now):
    """Most recent past occurrence of a local clock time. No am/pm -> try h and h+12."""
    h, m = int(h), int(m or 0)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
        hours = [h]
    else:
        hours = [h, h + 12] if h < 12 else [h]
    local_now = now.astimezone(TZ)
    best = None
    for hh in hours:
        if hh > 23 or m > 59:
            return None
        cand = datetime.combine(local_now.date(), dtime(hh, m), tzinfo=TZ)
        if cand > local_now:
            cand -= timedelta(days=1)
        best = cand if best is None or cand > best else best
    return best.astimezone(timezone.utc)


def oldest_unanswered():
    with db() as c:
        return c.execute("SELECT * FROM pings WHERE status='sent' ORDER BY scheduled_at LIMIT 1").fetchone()


def ping_by_msg(msg_id):
    with db() as c:
        return c.execute("SELECT * FROM pings WHERE telegram_msg_id=?", (msg_id,)).fetchone()


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    m = update.message
    text = (m.text or "").strip()
    now = utcnow()
    replied = ping_by_msg(m.reply_to_message.message_id) if m.reply_to_message else None

    # skip
    if text.lower() == "skip":
        p = replied or oldest_unanswered()
        if not p:
            return await m.reply_text("No unanswered ping to skip.")
        with db() as c:
            c.execute("UPDATE pings SET status='skipped' WHERE id=?", (p["id"],))
        return  # success: stay quiet

    ping, sampled, notice = None, None, None
    if replied:
        ping = replied
    elif mt := NOW_RE.match(text):
        text, sampled = mt.group(1).strip(), now
    elif mt := AT_RE.match(text):
        sampled = parse_at(mt.group(1), mt.group(2), mt.group(3), now)
        if sampled is None:
            return await m.reply_text("Couldn't parse that time.")
        text = mt.group(4).strip()
    elif (mt := REL_RE.match(text)) and (mt.group(1) or mt.group(2)):
        sampled = now - timedelta(hours=int(mt.group(1) or 0), minutes=int(mt.group(2) or 0))
        text = mt.group(3).strip()
    else:
        ping = oldest_unanswered()
        if ping is None:
            sampled = now
            notice = f"No pending ping, logged as spontaneous entry at {fmt_local(sampled)}."

    if ping is not None:
        sampled = parse(ping["sent_at"] or ping["scheduled_at"])

    with db() as c:
        c.execute("INSERT INTO entries(ping_id, sampled_at, entered_at, text, source) VALUES(?,?,?,?, 'text')",
                  (ping["id"] if ping else None, iso(sampled), iso(now), text))
        if ping:
            c.execute("UPDATE pings SET status='answered' WHERE id=?", (ping["id"],))
    if notice:  # only the unexpected path gets a reply; normal successes are silent
        await m.reply_text(notice)


# ----------------------------------------------------------------- commands

def parse_pause(arg, now):
    arg = arg.strip()
    if mt := re.fullmatch(r"(\d+)\s*([dh])", arg, re.I):
        n = int(mt.group(1))
        return now + (timedelta(days=n) if mt.group(2).lower() == "d" else timedelta(hours=n))
    if arg.lower().startswith("until"):
        base = now.astimezone(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
        dt = dparser.parse(arg[5:], default=base)
        dt = dt if dt.tzinfo else dt.replace(tzinfo=TZ)
        if dt <= now:
            dt = dt.replace(year=dt.year + 1)
        return dt.astimezone(timezone.utc)
    return None


async def cmd_pause(update, ctx):
    now = utcnow()
    end = parse_pause(" ".join(ctx.args), now) if ctx.args else None
    if not end:
        return await update.message.reply_text("Usage: /pause 4d | /pause 12h | /pause until nov 15")
    with db() as c:
        c.execute("INSERT INTO pauses(start, end) VALUES(?,?)", (iso(now), iso(end)))
    await update.message.reply_text(f"Paused until {end.astimezone(TZ).strftime('%a %b %d %H:%M')}.")


async def cmd_resume(update, ctx):
    now = utcnow()
    with db() as c:
        c.execute("UPDATE pauses SET end=? WHERE start<=? AND end>?", (iso(now), iso(now), iso(now)))
    await update.message.reply_text("Resumed.")


def save_setting(key, value):
    """Store a setting and log it. Returns the local date it takes effect (tomorrow's plan onward)."""
    eff = (utcnow().astimezone(TZ).date() + timedelta(days=1)).isoformat()
    with db() as c:
        c.execute("INSERT OR REPLACE INTO settings VALUES(?, ?)", (key, value))
        c.execute("INSERT INTO settings_log(setting, value, effective_from) VALUES(?, ?, ?)", (key, value, eff))
    return eff


def parse_bound_arg(arg, lowest):
    """'none' -> (True, None); whole number lowest..BOUND_MAX -> (True, n); anything else -> (False, None)."""
    if arg.lower() == "none":
        return True, None
    if arg.isdigit() and lowest <= int(arg) <= BOUND_MAX:
        return True, int(arg)
    return False, None


def show(v):
    return "none" if v is None else f"{v:g}" if isinstance(v, float) else str(v)


async def cmd_mean(update, ctx):
    mean, _, _ = get_params()
    if not ctx.args:
        return await update.message.reply_text(f"Mean: {mean:g} pings/day.")
    try:
        x = float(ctx.args[0])
    except ValueError:
        x = float("nan")
    if not MEAN_MIN <= x <= MEAN_MAX:  # also rejects nan
        return await update.message.reply_text(f"Mean must be a number from {MEAN_MIN:g} to {MEAN_MAX:g}.")
    eff = save_setting("mean", str(x))
    await update.message.reply_text(f"Mean {x:g} pings/day, effective {eff}.")


async def cmd_min(update, ctx):
    _, lo, hi = get_params()
    if not ctx.args:
        return await update.message.reply_text(f"Min: {show(lo)} pings/day.")
    ok, n = parse_bound_arg(ctx.args[0], 0)
    if not ok:
        return await update.message.reply_text(f"Min must be a whole number from 0 to {BOUND_MAX}, or none.")
    if n is not None and hi is not None and n > hi:
        return await update.message.reply_text(f"Min can't be above the current max ({hi}). Change max first.")
    eff = save_setting("min_n", show(n))
    await update.message.reply_text(f"Min {show(n)}, effective {eff}.")


async def cmd_max(update, ctx):
    _, lo, hi = get_params()
    if not ctx.args:
        return await update.message.reply_text(f"Max: {show(hi)} pings/day.")
    ok, n = parse_bound_arg(ctx.args[0], 1)
    if not ok:
        return await update.message.reply_text(f"Max must be a whole number from 1 to {BOUND_MAX}, or none.")
    if n is not None and lo is not None and n < lo:
        return await update.message.reply_text(f"Max can't be below the current min ({lo}). Change min first.")
    eff = save_setting("max_n", show(n))
    await update.message.reply_text(f"Max {show(n)}, effective {eff}.")


async def cmd_status(update, ctx):
    now = utcnow()
    with db() as c:
        remaining = c.execute("SELECT COUNT(*) FROM pings WHERE status='pending'").fetchone()[0]
        waiting = c.execute("SELECT COUNT(*) FROM pings WHERE status='sent'").fetchone()[0]
        pause = c.execute("SELECT end FROM pauses WHERE start<=? AND end>?", (iso(now), iso(now))).fetchone()
    mean, lo, hi = get_params()
    s = f"Mean: {mean:g} pings/day (min {show(lo)}, max {show(hi)})\nWindow: {START_H:02d}:00-{END_H:02d}:00 {TZ.key}\n"
    s += f"Pending pings: {remaining}\nUnanswered: {waiting}\n"
    s += f"Paused until {fmt_local(parse(pause['end']))}" if pause else "Not paused"
    await update.message.reply_text(s)  # next ping time deliberately not shown (keeps sampling unpredictable)


async def cmd_db(update, ctx):
    """/db [N]: row counts per table plus the last N pings and entries (default 5, max 20)."""
    n = min(int(ctx.args[0]), 20) if ctx.args and ctx.args[0].isdigit() else 5
    with db() as c:
        counts = {t: c.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                  for t in ("pings", "entries", "pauses", "settings_log", "day_plans")}
        by_status = c.execute("SELECT status, COUNT(*) k FROM pings GROUP BY status").fetchall()
        pings = c.execute("SELECT * FROM pings ORDER BY scheduled_at DESC LIMIT ?", (n,)).fetchall()
        entries = c.execute("SELECT * FROM entries ORDER BY id DESC LIMIT ?", (n,)).fetchall()
    s = "Rows: " + ", ".join(f"{t} {k}" for t, k in counts.items()) + "\n"
    s += "Ping status: " + (", ".join(f"{r['status']} {r['k']}" for r in by_status) or "none") + "\n"
    s += f"\nLast {n} pings (scheduled / status):\n"
    s += "\n".join(f"#{p['id']} {fmt_local(parse(p['scheduled_at']))} {p['status']}" for p in pings) or "none"
    s += f"\n\nLast {n} entries (sampled / entered / ping):\n"
    s += "\n".join(f"#{e['id']} {fmt_local(parse(e['sampled_at']))} / {fmt_local(parse(e['entered_at']))} "
                   f"/ ping {e['ping_id'] or '-'}: {e['text'][:60]}" for e in entries) or "none"
    await update.message.reply_text(s)


HELP_TEXT = f"""Life logger commands

Answering pings
(just type)  Reply to the oldest unanswered ping
(reply to a ping)  Answer that specific ping
skip  Skip the oldest unanswered ping (or the one you reply to)

Manual entries
now <text>  Spontaneous entry stamped with the current time
@3:30pm <text>  Entry stamped with the most recent past occurrence of that clock time (am/pm optional)
-1h30m <text>  Entry stamped that long ago (-45m, -2h also work)

Commands
/help  Show this list
/status  Mean, min, max, window, pending and unanswered counts, pause state
/mean [X]  Show or set the Poisson mean pings per day: a number {MEAN_MIN:g}-{MEAN_MAX:g}, decimals ok (e.g. 2.5)
/min [N]  Show or set the fewest pings a day can have: whole number 0-{BOUND_MAX}, or none
/max [N]  Show or set the most pings a day can have: whole number 1-{BOUND_MAX}, or none
          (min can't exceed max; none = no limit; mean/min/max take effect tomorrow)
/pause 4d | 12h | until nov 15  Stop sending pings for a while
/resume  End the current pause early
/db [N]  Row counts plus the last N pings and entries (default 5, max 20)
/export  Send a copy of the SQLite database file"""


async def cmd_help(update, ctx):
    await update.message.reply_text(HELP_TEXT)


async def cmd_export(update, ctx):
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, "sampler-export.db")
        src, dst = sqlite3.connect(DB_PATH), sqlite3.connect(out)
        src.backup(dst)
        src.close(); dst.close()
        with open(out, "rb") as f:
            await update.message.reply_document(f, filename="sampler-export.db")


def main():
    log.info("database file: %s", os.path.abspath(DB_PATH))
    init_db()
    app = Application.builder().token(BOT_TOKEN).build()
    only_me = filters.Chat(chat_id=OWNER)  # everyone else is ignored silently
    for name, fn in [("pause", cmd_pause), ("resume", cmd_resume), ("mean", cmd_mean),
                     ("min", cmd_min), ("max", cmd_max),
                     ("status", cmd_status), ("db", cmd_db), ("export", cmd_export),
                     ("help", cmd_help), ("start", cmd_help)]:
        app.add_handler(CommandHandler(name, fn, filters=only_me))
    app.add_handler(MessageHandler(only_me & filters.TEXT & ~filters.COMMAND, on_text))
    app.job_queue.run_repeating(tick, interval=60, first=1)
    app.run_polling()


if __name__ == "__main__":
    main()