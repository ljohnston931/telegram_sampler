"""Tests for the non-Telegram logic in bot.py (parsing, scheduling, DB, command handlers)."""
import asyncio
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# bot.py reads env vars at import time, so set them before importing it.
os.environ.setdefault("BOT_TOKEN", "123:test")
os.environ.setdefault("OWNER_CHAT_ID", "42")
os.environ.setdefault("TZ_NAME", "America/New_York")

import bot  # noqa: E402

UTC = timezone.utc
NY = bot.TZ


@pytest.fixture(autouse=True)
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(bot, "DB_PATH", str(tmp_path / "test.db"))
    bot.init_db()


def run(coro):
    return asyncio.run(coro)


def rows(sql, *args):
    with bot.db() as c:
        return c.execute(sql, args).fetchall()


def add_ping(scheduled, status="pending", sent_at=None, msg_id=None):
    with bot.db() as c:
        cur = c.execute("INSERT INTO pings(scheduled_at, status, sent_at, telegram_msg_id) VALUES(?,?,?,?)",
                        (bot.iso(scheduled), status, bot.iso(sent_at) if sent_at else None, msg_id))
        return cur.lastrowid


def make_update(text, reply_to_msg_id=None, args=None):
    msg = SimpleNamespace(
        text=text,
        reply_text=AsyncMock(),
        reply_document=AsyncMock(),
        reply_to_message=SimpleNamespace(message_id=reply_to_msg_id) if reply_to_msg_id else None,
    )
    return SimpleNamespace(message=msg), SimpleNamespace(args=args or [])


# ------------------------------------------------------------------- helpers

class TestHelpers:
    def test_iso_parse_roundtrip(self):
        dt = datetime(2026, 11, 9, 12, 30, tzinfo=UTC)
        assert bot.parse(bot.iso(dt)) == dt

    def test_iso_converts_to_utc(self):
        local = datetime(2026, 11, 9, 12, 0, tzinfo=NY)
        assert bot.iso(local).endswith("+00:00")
        assert bot.parse(bot.iso(local)) == local

    @pytest.mark.parametrize("s,expected", [("none", None), ("None", None), ("", None), (" 5 ", 5), ("0", 0)])
    def test_to_bound(self, s, expected):
        assert bot.to_bound(s) == expected

    @pytest.mark.parametrize("v,expected", [(None, "none"), (2.5, "2.5"), (3.0, "3"), (7, "7")])
    def test_show(self, v, expected):
        assert bot.show(v) == expected

    @pytest.mark.parametrize("arg,lowest,expected", [
        ("none", 0, (True, None)), ("NONE", 1, (True, None)),
        ("0", 0, (True, 0)), ("0", 1, (False, None)),
        ("100", 1, (True, 100)), ("101", 1, (False, None)),
        ("abc", 0, (False, None)), ("-1", 0, (False, None)), ("2.5", 0, (False, None)),
    ])
    def test_parse_bound_arg(self, arg, lowest, expected):
        assert bot.parse_bound_arg(arg, lowest) == expected


# ------------------------------------------------------------------ settings

class TestSettings:
    def test_defaults(self):
        mean, lo, hi = bot.get_params()
        assert mean == float(bot.DEFAULT_MEAN)
        assert lo is None and hi is None

    def test_legacy_pings_per_day_fallback(self):
        with bot.db() as c:
            c.execute("INSERT INTO settings VALUES('pings_per_day', '5')")
        assert bot.get_params()[0] == 5.0

    def test_mean_overrides_legacy(self):
        with bot.db() as c:
            c.execute("INSERT INTO settings VALUES('pings_per_day', '5')")
        bot.save_setting("mean", "2.5")
        assert bot.get_params()[0] == 2.5

    def test_save_setting_logs_and_returns_tomorrow(self):
        eff = bot.save_setting("min_n", "2")
        tomorrow = (bot.utcnow().astimezone(NY).date() + timedelta(days=1)).isoformat()
        assert eff == tomorrow
        log = rows("SELECT setting, value, effective_from FROM settings_log")
        assert [tuple(r) for r in log] == [("min_n", "2", tomorrow)]

    def test_save_setting_none_roundtrip(self):
        bot.save_setting("max_n", bot.show(None))
        assert bot.get_params()[2] is None


# ------------------------------------------------------------------- window

class TestWindow:
    def test_crosses_midnight(self, monkeypatch):
        monkeypatch.setattr(bot, "START_H", 8)
        monkeypatch.setattr(bot, "END_H", 2)
        s, e = bot.window(datetime(2026, 11, 9).date())
        assert s.astimezone(NY).hour == 8
        assert e - s == timedelta(hours=18)
        assert e.astimezone(NY).day == 10

    def test_same_day(self, monkeypatch):
        monkeypatch.setattr(bot, "START_H", 8)
        monkeypatch.setattr(bot, "END_H", 22)
        s, e = bot.window(datetime(2026, 11, 9).date())
        assert e - s == timedelta(hours=14)

    def test_equal_hours_is_24h(self, monkeypatch):
        monkeypatch.setattr(bot, "START_H", 8)
        monkeypatch.setattr(bot, "END_H", 8)
        s, e = bot.window(datetime(2026, 11, 9).date())
        assert e - s == timedelta(hours=24)

    def test_dst_fall_back_day_starts_at_local_8(self):
        # US DST ended 2026-11-01; the window should still start at 08:00 local
        s, _ = bot.window(datetime(2026, 11, 1).date())
        assert s.astimezone(NY).hour == 8


# -------------------------------------------------------------- ensure_plan

class TestEnsurePlan:
    def test_idempotent(self):
        d = datetime(2026, 11, 9).date()
        now = datetime(2026, 11, 9, 5, 0, tzinfo=UTC)  # before window start
        bot.ensure_plan(d, now)
        first = [tuple(r) for r in rows("SELECT scheduled_at FROM pings")]
        bot.ensure_plan(d, now)
        assert [tuple(r) for r in rows("SELECT scheduled_at FROM pings")] == first
        assert len(rows("SELECT * FROM day_plans")) == 1

    def test_records_plan_row(self):
        bot.save_setting("min_n", "2")
        bot.save_setting("max_n", "2")
        d = datetime(2026, 11, 9).date()
        bot.ensure_plan(d, datetime(2026, 11, 9, 5, 0, tzinfo=UTC))
        plan = rows("SELECT * FROM day_plans")[0]
        assert plan["n_pings"] == 2
        assert (plan["min_n"], plan["max_n"]) == (2, 2)
        assert len(rows("SELECT * FROM pings")) == 2

    def test_partial_window_drops_bounds(self):
        bot.save_setting("min_n", "5")
        d = datetime(2026, 11, 9).date()
        start, end = bot.window(d)
        bot.ensure_plan(d, end - timedelta(minutes=30))
        plan = rows("SELECT * FROM day_plans")[0]
        assert plan["min_n"] is None and plan["max_n"] is None
        assert plan["mean"] < float(bot.DEFAULT_MEAN)

    def test_finished_window_records_empty_plan(self):
        d = datetime(2026, 11, 1).date()
        bot.ensure_plan(d, datetime(2026, 11, 9, tzinfo=UTC))
        assert rows("SELECT n_pings FROM day_plans")[0][0] == 0
        assert rows("SELECT * FROM pings") == []


# ------------------------------------------------------------------- pauses

class TestPause:
    NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)

    def test_parse_hours_and_days(self):
        assert bot.parse_pause("12h", self.NOW) == self.NOW + timedelta(hours=12)
        assert bot.parse_pause("4d", self.NOW) == self.NOW + timedelta(days=4)
        assert bot.parse_pause("4 D", self.NOW) == self.NOW + timedelta(days=4)

    def test_parse_until(self):
        end = bot.parse_pause("until nov 15", self.NOW)
        local = end.astimezone(NY)
        assert (local.month, local.day, local.hour) == (11, 15, 0)

    def test_parse_until_past_date_rolls_to_next_year(self):
        end = bot.parse_pause("until nov 1", self.NOW)
        assert end.astimezone(NY).year == 2027

    def test_parse_garbage(self):
        assert bot.parse_pause("soon", self.NOW) is None

    def test_is_paused(self):
        now = bot.utcnow()
        with bot.db() as c:
            c.execute("INSERT INTO pauses(start, end) VALUES(?,?)",
                      (bot.iso(now - timedelta(hours=1)), bot.iso(now + timedelta(hours=1))))
        assert bot.is_paused(now)
        assert not bot.is_paused(now + timedelta(hours=2))

    def test_pause_and_resume_commands(self):
        u, c = make_update("/pause", args=["2h"])
        run(bot.cmd_pause(u, c))
        assert bot.is_paused(bot.utcnow())
        u, c = make_update("/resume")
        run(bot.cmd_resume(u, c))
        assert not bot.is_paused(bot.utcnow())

    def test_pause_usage_on_bad_arg(self):
        u, c = make_update("/pause", args=["nonsense"])
        run(bot.cmd_pause(u, c))
        assert "Usage" in u.message.reply_text.call_args[0][0]
        assert rows("SELECT * FROM pauses") == []


# --------------------------------------------------------------------- tick

class TestTick:
    def make_ctx(self):
        bot_mock = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(message_id=777)))
        return SimpleNamespace(bot=bot_mock)

    @pytest.fixture(autouse=True)
    def no_planning(self, monkeypatch):
        monkeypatch.setattr(bot, "ensure_plan", lambda d, now: None)

    def test_sends_due_ping(self):
        pid = add_ping(bot.utcnow() - timedelta(minutes=1))
        ctx = self.make_ctx()
        run(bot.tick(ctx))
        ctx.bot.send_message.assert_awaited_once_with(bot.OWNER, bot.PROMPT)
        p = rows("SELECT * FROM pings WHERE id=?", pid)[0]
        assert p["status"] == "sent" and p["telegram_msg_id"] == 777 and p["sent_at"]

    def test_future_ping_untouched(self):
        pid = add_ping(bot.utcnow() + timedelta(hours=1))
        ctx = self.make_ctx()
        run(bot.tick(ctx))
        ctx.bot.send_message.assert_not_awaited()
        assert rows("SELECT status FROM pings WHERE id=?", pid)[0][0] == "pending"

    def test_stale_ping_marked_missed(self):
        pid = add_ping(bot.utcnow() - bot.GRACE - timedelta(minutes=5))
        ctx = self.make_ctx()
        run(bot.tick(ctx))
        ctx.bot.send_message.assert_not_awaited()
        assert rows("SELECT status FROM pings WHERE id=?", pid)[0][0] == "missed"

    def test_paused_ping_not_sent(self):
        now = bot.utcnow()
        with bot.db() as c:
            c.execute("INSERT INTO pauses(start, end) VALUES(?,?)",
                      (bot.iso(now - timedelta(hours=1)), bot.iso(now + timedelta(hours=1))))
        pid = add_ping(now - timedelta(minutes=1))
        ctx = self.make_ctx()
        run(bot.tick(ctx))
        ctx.bot.send_message.assert_not_awaited()
        assert rows("SELECT status FROM pings WHERE id=?", pid)[0][0] == "paused"

    def test_stale_beats_paused(self):
        now = bot.utcnow()
        with bot.db() as c:
            c.execute("INSERT INTO pauses(start, end) VALUES(?,?)",
                      (bot.iso(now - timedelta(days=1)), bot.iso(now + timedelta(days=1))))
        pid = add_ping(now - timedelta(hours=2))
        run(bot.tick(self.make_ctx()))
        assert rows("SELECT status FROM pings WHERE id=?", pid)[0][0] == "missed"


# ----------------------------------------------------------------- parse_at

class TestParseAt:
    # 2026-11-09 15:00 local (EST, UTC-5) = 20:00 UTC
    NOW = datetime(2026, 11, 9, 15, 0, tzinfo=NY).astimezone(UTC)

    def local(self, dt):
        return dt.astimezone(NY)

    def test_pm_explicit(self):
        r = self.local(bot.parse_at("2", "30", "pm", self.NOW))
        assert (r.day, r.hour, r.minute) == (9, 14, 30)

    def test_am_explicit(self):
        assert self.local(bot.parse_at("9", None, "am", self.NOW)).hour == 9

    def test_12am_is_midnight_and_12pm_is_noon(self):
        assert self.local(bot.parse_at("12", None, "am", self.NOW)).hour == 0
        assert self.local(bot.parse_at("12", None, "pm", self.NOW)).hour == 12

    def test_future_time_rolls_to_yesterday(self):
        r = self.local(bot.parse_at("4", None, "pm", self.NOW))
        assert (r.day, r.hour) == (8, 16)

    def test_ambiguous_picks_most_recent(self):
        # 3pm now: "2" -> 2pm today (not 2am)
        assert self.local(bot.parse_at("2", None, None, self.NOW)).hour == 14
        # "4" -> 4pm yesterday vs 4am today -> 4am today is more recent
        r = self.local(bot.parse_at("4", None, None, self.NOW))
        assert (r.day, r.hour) == (9, 4)

    def test_24h_style_hour(self):
        assert self.local(bot.parse_at("14", "15", None, self.NOW)).hour == 14

    def test_invalid(self):
        assert bot.parse_at("25", None, None, self.NOW) is None
        assert bot.parse_at("3", "75", "pm", self.NOW) is None


# ----------------------------------------------------------------- regexes

class TestRegexes:
    def test_now(self):
        assert bot.NOW_RE.match("now: coffee").group(1) == "coffee"
        assert bot.NOW_RE.match("now coffee").group(1) == "coffee"
        assert bot.NOW_RE.match("nowhere") is None

    def test_at(self):
        m = bot.AT_RE.match("@3:30pm reading")
        assert m.groups() == ("3", "30", "pm", "reading")
        assert bot.AT_RE.match("@15 lunch").group(4) == "lunch"

    def test_rel(self):
        m = bot.REL_RE.match("-1h30m walking")
        assert m.groups() == ("1", "30", "walking")
        assert bot.REL_RE.match("-45m x").group(2) == "45"


# ------------------------------------------------------------------ on_text

class TestOnText:
    def entries(self):
        return rows("SELECT * FROM entries ORDER BY id")

    def test_plain_text_answers_oldest_unanswered(self):
        t1 = bot.utcnow() - timedelta(hours=3)
        t2 = bot.utcnow() - timedelta(hours=1)
        p1 = add_ping(t1, "sent", sent_at=t1, msg_id=1)
        p2 = add_ping(t2, "sent", sent_at=t2, msg_id=2)
        u, c = make_update("writing code")
        run(bot.on_text(u, c))
        e = self.entries()[0]
        assert e["ping_id"] == p1 and e["text"] == "writing code"
        assert bot.parse(e["sampled_at"]) == t1  # stamped with the ping time, not now
        assert rows("SELECT status FROM pings WHERE id=?", p1)[0][0] == "answered"
        assert rows("SELECT status FROM pings WHERE id=?", p2)[0][0] == "sent"
        u.message.reply_text.assert_not_awaited()

    def test_reply_targets_specific_ping(self):
        t1 = bot.utcnow() - timedelta(hours=3)
        t2 = bot.utcnow() - timedelta(hours=1)
        add_ping(t1, "sent", sent_at=t1, msg_id=1)
        p2 = add_ping(t2, "sent", sent_at=t2, msg_id=2)
        u, c = make_update("lunch", reply_to_msg_id=2)
        run(bot.on_text(u, c))
        assert self.entries()[0]["ping_id"] == p2

    def test_no_pending_ping_logs_spontaneous_with_notice(self):
        u, c = make_update("hello")
        run(bot.on_text(u, c))
        e = self.entries()[0]
        assert e["ping_id"] is None
        assert "spontaneous" in u.message.reply_text.call_args[0][0]

    def test_now_prefix(self):
        t = bot.utcnow() - timedelta(hours=1)
        pid = add_ping(t, "sent", sent_at=t, msg_id=1)
        u, c = make_update("now: snack")
        run(bot.on_text(u, c))
        e = self.entries()[0]
        assert e["ping_id"] is None and e["text"] == "snack"
        assert rows("SELECT status FROM pings WHERE id=?", pid)[0][0] == "sent"  # ping left alone
        u.message.reply_text.assert_not_awaited()

    def test_relative_prefix(self):
        u, c = make_update("-1h30m napping")
        run(bot.on_text(u, c))
        e = self.entries()[0]
        assert e["text"] == "napping"
        delta = bot.parse(e["entered_at"]) - bot.parse(e["sampled_at"])
        assert delta == pytest.approx(timedelta(minutes=90), abs=timedelta(seconds=2))

    def test_relative_without_numbers_is_plain_text(self):
        u, c = make_update("- not a time")
        run(bot.on_text(u, c))
        assert self.entries()[0]["text"] == "- not a time"

    def test_at_prefix(self):
        u, c = make_update("@3:30pm reading")
        run(bot.on_text(u, c))
        e = self.entries()[0]
        assert e["text"] == "reading"
        local = bot.parse(e["sampled_at"]).astimezone(NY)
        assert (local.hour, local.minute) == (15, 30)

    def test_at_prefix_bad_time(self):
        u, c = make_update("@25:99 nope")
        run(bot.on_text(u, c))
        assert "Couldn't parse" in u.message.reply_text.call_args[0][0]
        assert self.entries() == []

    def test_skip_oldest(self):
        t = bot.utcnow() - timedelta(hours=1)
        pid = add_ping(t, "sent", sent_at=t, msg_id=1)
        u, c = make_update("Skip")
        run(bot.on_text(u, c))
        assert rows("SELECT status FROM pings WHERE id=?", pid)[0][0] == "skipped"
        assert self.entries() == []
        u.message.reply_text.assert_not_awaited()

    def test_skip_replied_ping(self):
        t1 = bot.utcnow() - timedelta(hours=3)
        t2 = bot.utcnow() - timedelta(hours=1)
        add_ping(t1, "sent", sent_at=t1, msg_id=1)
        p2 = add_ping(t2, "sent", sent_at=t2, msg_id=2)
        u, c = make_update("skip", reply_to_msg_id=2)
        run(bot.on_text(u, c))
        assert rows("SELECT status FROM pings WHERE id=?", p2)[0][0] == "skipped"

    def test_skip_with_nothing_to_skip(self):
        u, c = make_update("skip")
        run(bot.on_text(u, c))
        assert "No unanswered" in u.message.reply_text.call_args[0][0]


# ------------------------------------------------------------ /mean /min /max

class TestParamCommands:
    def reply(self, u):
        return u.message.reply_text.call_args[0][0]

    def test_mean_show_and_set(self):
        u, c = make_update("/mean")
        run(bot.cmd_mean(u, c))
        assert "pings/day" in self.reply(u)
        u, c = make_update("/mean", args=["2.5"])
        run(bot.cmd_mean(u, c))
        assert bot.get_params()[0] == 2.5

    @pytest.mark.parametrize("arg", ["0", "0.05", "51", "abc", "nan", "inf"])
    def test_mean_rejects_out_of_range(self, arg):
        u, c = make_update("/mean", args=[arg])
        run(bot.cmd_mean(u, c))
        assert "must be" in self.reply(u)
        assert rows("SELECT * FROM settings") == []

    def test_min_set_and_clear(self):
        u, c = make_update("/min", args=["2"])
        run(bot.cmd_min(u, c))
        assert bot.get_params()[1] == 2
        u, c = make_update("/min", args=["none"])
        run(bot.cmd_min(u, c))
        assert bot.get_params()[1] is None

    def test_min_cannot_exceed_max(self):
        bot.save_setting("max_n", "3")
        u, c = make_update("/min", args=["5"])
        run(bot.cmd_min(u, c))
        assert "can't be above" in self.reply(u)
        assert bot.get_params()[1] is None

    def test_max_cannot_be_below_min(self):
        bot.save_setting("min_n", "4")
        u, c = make_update("/max", args=["2"])
        run(bot.cmd_max(u, c))
        assert "can't be below" in self.reply(u)
        assert bot.get_params()[2] is None

    def test_max_rejects_zero(self):
        u, c = make_update("/max", args=["0"])
        run(bot.cmd_max(u, c))
        assert "must be" in self.reply(u)

    def test_min_allows_zero(self):
        u, c = make_update("/min", args=["0"])
        run(bot.cmd_min(u, c))
        assert bot.get_params()[1] == 0


# ------------------------------------------------------- status/db/help/export

class TestInfoCommands:
    def test_status(self):
        add_ping(bot.utcnow() + timedelta(hours=1))
        t = bot.utcnow() - timedelta(hours=1)
        add_ping(t, "sent", sent_at=t, msg_id=1)
        u, c = make_update("/status")
        run(bot.cmd_status(u, c))
        text = u.message.reply_text.call_args[0][0]
        assert "Pending pings: 1" in text
        assert "Unanswered: 1" in text
        assert "Not paused" in text

    def test_db_caps_n_and_handles_empty(self):
        u, c = make_update("/db", args=["999"])
        run(bot.cmd_db(u, c))
        text = u.message.reply_text.call_args[0][0]
        assert "Last 20 pings" in text
        assert "none" in text

    def test_db_lists_entries(self):
        u, c = make_update("hello")
        run(bot.on_text(u, c))
        u, c = make_update("/db")
        run(bot.cmd_db(u, c))
        assert "hello" in u.message.reply_text.call_args[0][0]

    def test_help_mentions_commands(self):
        u, c = make_update("/help")
        run(bot.cmd_help(u, c))
        text = u.message.reply_text.call_args[0][0]
        for cmd in ("/mean", "/min", "/max", "/pause", "/export"):
            assert cmd in text

    def test_export_sends_valid_sqlite_copy(self):
        add_ping(bot.utcnow())
        sent = {}

        async def fake_reply_document(f, filename):
            sent["filename"] = filename
            sent["data"] = f.read()

        u, c = make_update("/export")
        u.message.reply_document = fake_reply_document
        run(bot.cmd_export(u, c))
        assert sent["filename"] == "sampler-export.db"
        assert sent["data"].startswith(b"SQLite format 3")


# ------------------------------------------------------------------ migration

def test_init_db_migrates_old_day_plans(tmp_path, monkeypatch):
    path = str(tmp_path / "old.db")
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE day_plans(window_date TEXT PRIMARY KEY, n_pings INTEGER, "
                "window_start TEXT, window_end TEXT)")
    con.execute("INSERT INTO day_plans VALUES('2026-01-01', 3, 'a', 'b')")
    con.commit()
    con.close()
    monkeypatch.setattr(bot, "DB_PATH", path)
    bot.init_db()
    with bot.db() as c:
        cols = {r["name"] for r in c.execute("PRAGMA table_info(day_plans)")}
        old = c.execute("SELECT * FROM day_plans").fetchone()
    assert {"mean", "min_n", "max_n"} <= cols
    assert old["n_pings"] == 3 and old["mean"] is None
