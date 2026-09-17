"""
Discord Streak Bot
===================
A single-file, production-oriented Discord bot that recreates the feeling of
"streaks" for a server: every member keeps a personal streak alive by posting
at least one message, image, or video anywhere in the server each day.

Run with:  python bot.py
Requires a .env file (see README.md).
"""

from __future__ import annotations

# =====================================================================
# CONFIGURATION
# =====================================================================

import os
import sys
import re
import html
import random
import sqlite3
import asyncio
import logging
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timedelta, date, time as dtime
from zoneinfo import ZoneInfo, available_timezones
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
GUILD_ID = os.getenv("GUILD_ID")
STREAK_DASHBOARD_CHANNEL_ID = os.getenv("STREAK_DASHBOARD_CHANNEL_ID")

REQUIRED_ENV = {
    "DISCORD_TOKEN": DISCORD_TOKEN,
    "CLIENT_ID": CLIENT_ID,
    "GUILD_ID": GUILD_ID,
    "STREAK_DASHBOARD_CHANNEL_ID": STREAK_DASHBOARD_CHANNEL_ID,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("streakbot")

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "streaks.db")

MILESTONES = [3, 7, 14, 30, 50, 100, 365]
MONTHLY_FREEZE_ALLOWANCE = 3

# Detailed daily_activity rows older than this are eligible for automatic
# cleanup. This ONLY affects the short-term evidence table. The persistent
# streak aggregates (current_streak, longest_streak, started_at,
# last_completed_date, status, and all lifetime statistics) are never
# touched by cleanup and are the sole source of truth for streak length.
ACTIVITY_RETENTION_DAYS = 90

DEFAULT_TIMEZONE = "Asia/Karachi"
DEFAULT_DEADLINE_HOUR = 0
DEFAULT_DEADLINE_MINUTE = 0

DASHBOARD_TITLE = "Discord Streaks"
FOOTER_TEXT = "Personal responses are private and visible only to you."


def validate_env() -> None:
    missing = [k for k, v in REQUIRED_ENV.items() if not v]
    if missing:
        raise RuntimeError(
            f"Missing required environment variable(s): {', '.join(missing)}. "
            "Check your .env file."
        )


# =====================================================================
# DATABASE
# =====================================================================

class Database:
    """Thin synchronous SQLite wrapper. All public methods are safe to call
    via asyncio.to_thread(...) from async code so the event loop never blocks
    on disk I/O."""

    def __init__(self, path: str):
        self._path = path
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(self._path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    # -- schema -------------------------------------------------------
    def _init_schema(self) -> None:
        with self._lock, self._conn:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS guild_config (
                    guild_id TEXT PRIMARY KEY,
                    daily_channel_id TEXT,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Karachi',
                    deadline_hour INTEGER NOT NULL DEFAULT 0,
                    deadline_minute INTEGER NOT NULL DEFAULT 0,
                    warning_2h_enabled INTEGER NOT NULL DEFAULT 1,
                    warning_30m_enabled INTEGER NOT NULL DEFAULT 1,
                    streak_enabled INTEGER NOT NULL DEFAULT 1,
                    last_day_key TEXT,
                    last_2h_warn_period TEXT,
                    last_30m_warn_period TEXT,
                    quote_channel_id TEXT,
                    quote_feed_1 TEXT,
                    quote_feed_2 TEXT,
                    quote_feed_3 TEXT,
                    quotes_enabled INTEGER NOT NULL DEFAULT 1,
                    quote_rotation_index INTEGER NOT NULL DEFAULT 0,
                    last_quote_period TEXT
                );

                CREATE TABLE IF NOT EXISTS dashboard_state (
                    guild_id TEXT PRIMARY KEY,
                    dashboard_channel_id TEXT NOT NULL,
                    dashboard_message_id TEXT
                );

                CREATE TABLE IF NOT EXISTS users (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    current_streak INTEGER NOT NULL DEFAULT 0,
                    longest_streak INTEGER NOT NULL DEFAULT 0,
                    streak_start_date TEXT,
                    last_completed_date TEXT,
                    status TEXT NOT NULL DEFAULT 'inactive',
                    total_active_days INTEGER NOT NULL DEFAULT 0,
                    total_completed_days INTEGER NOT NULL DEFAULT 0,
                    total_streaks INTEGER NOT NULL DEFAULT 0,
                    broken_streaks INTEGER NOT NULL DEFAULT 0,
                    freezes_used_total INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (guild_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS streak_participants (
                    guild_id TEXT NOT NULL,
                    group_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    PRIMARY KEY (guild_id, group_id, user_id)
                );

                CREATE TABLE IF NOT EXISTS daily_activity (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id, day_key)
                );
                CREATE INDEX IF NOT EXISTS idx_daily_activity_day
                    ON daily_activity (guild_id, day_key);

                CREATE TABLE IF NOT EXISTS freeze_balances (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    year_month TEXT NOT NULL,
                    balance INTEGER NOT NULL DEFAULT 3,
                    PRIMARY KEY (guild_id, user_id, year_month)
                );

                CREATE TABLE IF NOT EXISTS freeze_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE (guild_id, user_id, day_key)
                );

                CREATE TABLE IF NOT EXISTS milestones (
                    guild_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    milestone INTEGER NOT NULL,
                    achieved_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id, milestone)
                );

                CREATE TABLE IF NOT EXISTS daily_evaluations (
                    guild_id TEXT NOT NULL,
                    day_key TEXT NOT NULL,
                    evaluated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, day_key)
                );
                """
            )
        self._migrate_schema()

    def _migrate_schema(self) -> None:
        """Adds columns introduced after initial release to pre-existing
        databases. This never touches or recomputes existing streak
        aggregates — it only ensures the new persistent-state columns exist,
        defaulting new columns to values that reflect current data."""
        with self._lock, self._conn:
            existing_cols = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(users)").fetchall()
            }
            if "last_completed_date" not in existing_cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN last_completed_date TEXT")
            if "status" not in existing_cols:
                self._conn.execute("ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'inactive'")
                # Backfill a reasonable status for rows that already existed
                # before this column was introduced, without altering any
                # streak counters.
                self._conn.execute(
                    "UPDATE users SET status = CASE WHEN current_streak > 0 THEN 'active' ELSE 'inactive' END"
                )

            guild_cols = {
                row["name"] for row in self._conn.execute("PRAGMA table_info(guild_config)").fetchall()
            }
            guild_config_additions = {
                "quote_channel_id": "TEXT",
                "quote_feed_1": "TEXT",
                "quote_feed_2": "TEXT",
                "quote_feed_3": "TEXT",
                "quotes_enabled": "INTEGER NOT NULL DEFAULT 1",
                "quote_rotation_index": "INTEGER NOT NULL DEFAULT 0",
                "last_quote_period": "TEXT",
            }
            for col_name, col_def in guild_config_additions.items():
                if col_name not in guild_cols:
                    self._conn.execute(f"ALTER TABLE guild_config ADD COLUMN {col_name} {col_def}")

    # -- guild config ---------------------------------------------------
    def get_or_create_guild_config(self, guild_id: str) -> sqlite3.Row:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
            ).fetchone()
            if row is None:
                self._conn.execute(
                    """INSERT INTO guild_config
                       (guild_id, timezone, deadline_hour, deadline_minute)
                       VALUES (?, ?, ?, ?)""",
                    (guild_id, DEFAULT_TIMEZONE, DEFAULT_DEADLINE_HOUR, DEFAULT_DEADLINE_MINUTE),
                )
                row = self._conn.execute(
                    "SELECT * FROM guild_config WHERE guild_id = ?", (guild_id,)
                ).fetchone()
            return row

    def update_guild_config(self, guild_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [guild_id]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE guild_config SET {cols} WHERE guild_id = ?", values
            )

    # -- dashboard --------------------------------------------------
    def get_dashboard_state(self, guild_id: str) -> Optional[sqlite3.Row]:
        with self._lock, self._conn:
            return self._conn.execute(
                "SELECT * FROM dashboard_state WHERE guild_id = ?", (guild_id,)
            ).fetchone()

    def upsert_dashboard_state(
        self, guild_id: str, channel_id: str, message_id: Optional[str]
    ) -> None:
        with self._lock, self._conn:
            self._conn.execute(
                """INSERT INTO dashboard_state (guild_id, dashboard_channel_id, dashboard_message_id)
                   VALUES (?, ?, ?)
                   ON CONFLICT(guild_id) DO UPDATE SET
                     dashboard_channel_id = excluded.dashboard_channel_id,
                     dashboard_message_id = excluded.dashboard_message_id""",
                (guild_id, channel_id, message_id),
            )

    # -- users --------------------------------------------------------
    def get_or_create_user(self, guild_id: str, user_id: str) -> sqlite3.Row:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT * FROM users WHERE guild_id = ? AND user_id = ?",
                (guild_id, user_id),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO users (guild_id, user_id) VALUES (?, ?)",
                    (guild_id, user_id),
                )
                row = self._conn.execute(
                    "SELECT * FROM users WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
            return row

    def update_user(self, guild_id: str, user_id: str, **fields) -> None:
        if not fields:
            return
        cols = ", ".join(f"{k} = ?" for k in fields)
        values = list(fields.values()) + [guild_id, user_id]
        with self._lock, self._conn:
            self._conn.execute(
                f"UPDATE users SET {cols} WHERE guild_id = ? AND user_id = ?", values
            )

    def list_tracked_users(self, guild_id: str) -> list[sqlite3.Row]:
        with self._lock, self._conn:
            return self._conn.execute(
                "SELECT * FROM users WHERE guild_id = ?", (guild_id,)
            ).fetchall()

    # -- daily activity -------------------------------------------------
    def has_activity(self, guild_id: str, user_id: str, day_key: str) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM daily_activity WHERE guild_id=? AND user_id=? AND day_key=?",
                (guild_id, user_id, day_key),
            ).fetchone()
            return row is not None

    def mark_activity(self, guild_id: str, user_id: str, day_key: str) -> bool:
        """Returns True if this call newly recorded activity for the day."""
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO daily_activity (guild_id, user_id, day_key, created_at) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, day_key, datetime.utcnow().isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # -- freezes ----------------------------------------------------
    def get_or_create_freeze_balance(self, guild_id: str, user_id: str, year_month: str) -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT balance FROM freeze_balances WHERE guild_id=? AND user_id=? AND year_month=?",
                (guild_id, user_id, year_month),
            ).fetchone()
            if row is None:
                self._conn.execute(
                    "INSERT INTO freeze_balances (guild_id, user_id, year_month, balance) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, year_month, MONTHLY_FREEZE_ALLOWANCE),
                )
                return MONTHLY_FREEZE_ALLOWANCE
            return row["balance"]

    def consume_freeze(self, guild_id: str, user_id: str, year_month: str, day_key: str) -> bool:
        """Atomically consumes one freeze if available and records usage.
        Returns True if a freeze was consumed."""
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT balance FROM freeze_balances WHERE guild_id=? AND user_id=? AND year_month=?",
                (guild_id, user_id, year_month),
            ).fetchone()
            balance = row["balance"] if row else MONTHLY_FREEZE_ALLOWANCE
            if row is None:
                self._conn.execute(
                    "INSERT INTO freeze_balances (guild_id, user_id, year_month, balance) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, year_month, MONTHLY_FREEZE_ALLOWANCE),
                )
            if balance <= 0:
                return False
            try:
                self._conn.execute(
                    "INSERT INTO freeze_usage (guild_id, user_id, day_key, created_at) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, day_key, datetime.utcnow().isoformat()),
                )
            except sqlite3.IntegrityError:
                # Already recorded for this day (idempotency guard).
                return False
            self._conn.execute(
                "UPDATE freeze_balances SET balance = balance - 1 WHERE guild_id=? AND user_id=? AND year_month=?",
                (guild_id, user_id, year_month),
            )
            return True

    def freeze_usage_count(self, guild_id: str, user_id: str) -> int:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM freeze_usage WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchone()
            return row["c"] if row else 0

    def recent_freeze_usage(self, guild_id: str, user_id: str, limit: int = 5) -> list[sqlite3.Row]:
        with self._lock, self._conn:
            return self._conn.execute(
                "SELECT * FROM freeze_usage WHERE guild_id=? AND user_id=? ORDER BY day_key DESC LIMIT ?",
                (guild_id, user_id, limit),
            ).fetchall()

    # -- milestones ---------------------------------------------------
    def has_milestone(self, guild_id: str, user_id: str, milestone: int) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM milestones WHERE guild_id=? AND user_id=? AND milestone=?",
                (guild_id, user_id, milestone),
            ).fetchone()
            return row is not None

    def record_milestone(self, guild_id: str, user_id: str, milestone: int) -> bool:
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO milestones (guild_id, user_id, milestone, achieved_at) VALUES (?, ?, ?, ?)",
                    (guild_id, user_id, milestone, datetime.utcnow().isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def achieved_milestones(self, guild_id: str, user_id: str) -> set[int]:
        with self._lock, self._conn:
            rows = self._conn.execute(
                "SELECT milestone FROM milestones WHERE guild_id=? AND user_id=?",
                (guild_id, user_id),
            ).fetchall()
            return {r["milestone"] for r in rows}

    # -- daily evaluation idempotency --------------------------------
    def is_day_evaluated(self, guild_id: str, day_key: str) -> bool:
        with self._lock, self._conn:
            row = self._conn.execute(
                "SELECT 1 FROM daily_evaluations WHERE guild_id=? AND day_key=?",
                (guild_id, day_key),
            ).fetchone()
            return row is not None

    def mark_day_evaluated(self, guild_id: str, day_key: str) -> bool:
        with self._lock, self._conn:
            try:
                self._conn.execute(
                    "INSERT INTO daily_evaluations (guild_id, day_key, evaluated_at) VALUES (?, ?, ?)",
                    (guild_id, day_key, datetime.utcnow().isoformat()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    # -- retention cleanup --------------------------------------------
    def cleanup_old_activity(self, retention_days: int = ACTIVITY_RETENTION_DAYS) -> int:
        """Deletes detailed daily_activity rows older than retention_days.

        This is the ONLY table this method touches. It never modifies or
        deletes: users (streak aggregates), streak_participants, freeze
        balances, freeze usage history, milestones, guild_config, or
        dashboard_state. Deleting old raw activity rows has no effect on
        current_streak / longest_streak / streak_start_date /
        last_completed_date / total_completed_days / total_streaks /
        broken_streaks / freezes_used_total, because none of those are ever
        derived from this table — they are persistent aggregates updated
        incrementally at evaluation time.
        """
        cutoff = (date.today() - timedelta(days=retention_days)).isoformat()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "DELETE FROM daily_activity WHERE day_key < ?", (cutoff,)
            )
            return cur.rowcount if cur.rowcount is not None else 0


db = Database(DB_PATH)


async def db_call(func, *args, **kwargs):
    """Run a blocking Database method off the event loop."""
    return await asyncio.to_thread(func, *args, **kwargs)


# =====================================================================
# MODELS / HELPERS
# =====================================================================

@dataclass
class GuildSettings:
    guild_id: str
    daily_channel_id: Optional[str]
    timezone: str
    deadline_hour: int
    deadline_minute: int
    warning_2h_enabled: bool
    warning_30m_enabled: bool
    streak_enabled: bool
    last_day_key: Optional[str]
    last_2h_warn_period: Optional[str]
    last_30m_warn_period: Optional[str]
    quote_channel_id: Optional[str]
    quote_feed_1: Optional[str]
    quote_feed_2: Optional[str]
    quote_feed_3: Optional[str]
    quotes_enabled: bool
    quote_rotation_index: int
    last_quote_period: Optional[str]

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "GuildSettings":
        return cls(
            guild_id=row["guild_id"],
            daily_channel_id=row["daily_channel_id"],
            timezone=row["timezone"],
            deadline_hour=row["deadline_hour"],
            deadline_minute=row["deadline_minute"],
            warning_2h_enabled=bool(row["warning_2h_enabled"]),
            warning_30m_enabled=bool(row["warning_30m_enabled"]),
            streak_enabled=bool(row["streak_enabled"]),
            last_day_key=row["last_day_key"],
            last_2h_warn_period=row["last_2h_warn_period"],
            last_30m_warn_period=row["last_30m_warn_period"],
            quote_channel_id=row["quote_channel_id"],
            quote_feed_1=row["quote_feed_1"],
            quote_feed_2=row["quote_feed_2"],
            quote_feed_3=row["quote_feed_3"],
            quotes_enabled=bool(row["quotes_enabled"]),
            quote_rotation_index=row["quote_rotation_index"] or 0,
            last_quote_period=row["last_quote_period"],
        )

    def quote_feeds(self) -> list[str]:
        """Configured feed URLs in rotation order, skipping any left blank."""
        return [f for f in (self.quote_feed_1, self.quote_feed_2, self.quote_feed_3) if f]

    def tzinfo(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.timezone)
        except Exception:
            return ZoneInfo(DEFAULT_TIMEZONE)


def safe_zone(name: str) -> Optional[ZoneInfo]:
    try:
        if name not in available_timezones():
            return None
        return ZoneInfo(name)
    except Exception:
        return None


def period_key_for(now_local: datetime, deadline_h: int, deadline_m: int) -> str:
    """Returns the ISO date string identifying the streak 'day' that `now_local`
    currently belongs to, given a configurable deadline time. The day rolls
    over at the deadline time rather than strictly at midnight."""
    if (now_local.hour, now_local.minute) < (deadline_h, deadline_m):
        return (now_local.date() - timedelta(days=1)).isoformat()
    return now_local.date().isoformat()


def next_deadline_dt(now_local: datetime, deadline_h: int, deadline_m: int) -> datetime:
    """Next wall-clock occurrence of the deadline time at or after `now_local`."""
    candidate = now_local.replace(hour=deadline_h, minute=deadline_m, second=0, microsecond=0)
    if candidate <= now_local:
        candidate += timedelta(days=1)
    return candidate


def format_deadline(hour: int, minute: int) -> str:
    return dtime(hour=hour, minute=minute).strftime("%I:%M %p").lstrip("0")


def year_month_of(day_key: str) -> str:
    return day_key[:7]


def is_qualifying_message(message: discord.Message) -> bool:
    """A qualifying activity is any normal user message anywhere in the
    guild — plain text, an image attachment, a video attachment, or any
    combination of these. Discord treats attachments (images, videos, GIFs,
    files, etc.) as part of a single message object, so no separate
    attachment-type check is needed here: any non-bot, non-webhook, non-
    system message that reaches this point already counts, regardless of
    whether it carries text, media, or both."""
    if message.author.bot:
        return False
    if message.webhook_id is not None:
        return False
    if message.type not in (discord.MessageType.default, discord.MessageType.reply):
        return False
    if message.guild is None:
        return False
    return True


# =====================================================================
# EMBEDS
# =====================================================================

BRAND_COLOR = discord.Color.from_rgb(255, 90, 31)
GOOD_COLOR = discord.Color.green()
WARN_COLOR = discord.Color.orange()
BAD_COLOR = discord.Color.red()
INFO_COLOR = discord.Color.blurple()


def build_dashboard_embed() -> discord.Embed:
    embed = discord.Embed(
        title=f"Discord Streaks",
        description=(
            "Keep your daily streak alive by being active in the server.\n\n"
            "**How it works**\n"
            "Send at least one message, image, or video anywhere in this server "
            "every day to maintain your streak.\n\n"
            "**Streak Freeze**\n"
            "Every member receives 3 Streak Freezes per month. If you miss a "
            "day, an available freeze is used automatically."
        ),
        color=BRAND_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def my_streak_embed(display_name: str, user_row: sqlite3.Row, settings: GuildSettings, completed_today: bool) -> discord.Embed:
    status = "Active" if user_row["current_streak"] > 0 else "No active streak"
    embed = discord.Embed(title="My Streak", color=BRAND_COLOR)
    embed.add_field(name="Current Streak", value=f"{user_row['current_streak']} days", inline=True)
    embed.add_field(name="Longest Streak", value=f"{user_row['longest_streak']} days", inline=True)
    embed.add_field(name="Status", value=status, inline=True)
    start = user_row["streak_start_date"] or "Not started"
    embed.add_field(name="Start Date", value=start, inline=True)
    embed.add_field(name="Deadline", value=format_deadline(settings.deadline_hour, settings.deadline_minute), inline=True)
    embed.add_field(
        name="Today's Activity",
        value="Completed" if completed_today else "Not yet completed",
        inline=True,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def statistics_embed(user_row: sqlite3.Row) -> discord.Embed:
    embed = discord.Embed(title="Statistics", color=INFO_COLOR)
    embed.add_field(name="Current Streak", value=str(user_row["current_streak"]), inline=True)
    embed.add_field(name="Longest Streak", value=str(user_row["longest_streak"]), inline=True)
    embed.add_field(name="Total Active Days", value=str(user_row["total_active_days"]), inline=True)
    embed.add_field(name="Total Completed Days", value=str(user_row["total_completed_days"]), inline=True)
    embed.add_field(name="Total Streaks", value=str(user_row["total_streaks"]), inline=True)
    embed.add_field(name="Broken Streaks", value=str(user_row["broken_streaks"]), inline=True)
    embed.add_field(name="Freezes Used", value=str(user_row["freezes_used_total"]), inline=True)
    embed.add_field(name="Current Streak Started", value=user_row["streak_start_date"] or "N/A", inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def achievements_embed(achieved: set[int]) -> discord.Embed:
    embed = discord.Embed(title="Achievements", color=discord.Color.gold())
    lines = []
    for m in MILESTONES:
        mark = "Unlocked" if m in achieved else "Locked"
        lines.append(f"{m} Days — {mark}")
    embed.description = "\n".join(lines)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def freezes_embed(balance: int, next_reset_label: str, history: list[sqlite3.Row]) -> discord.Embed:
    embed = discord.Embed(title="Streak Freezes", color=discord.Color.teal())
    embed.add_field(name="Available", value=f"{balance} / {MONTHLY_FREEZE_ALLOWANCE}", inline=True)
    embed.add_field(name="Monthly Allowance", value=str(MONTHLY_FREEZE_ALLOWANCE), inline=True)
    embed.add_field(name="Next Reset", value=next_reset_label, inline=True)
    embed.description = "Freezes are used automatically when you miss a daily requirement."
    if history:
        hist_lines = "\n".join(f"Used on {h['day_key']}" for h in history)
        embed.add_field(name="Recent History", value=hist_lines, inline=False)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def how_it_works_embed() -> discord.Embed:
    embed = discord.Embed(
        title="How It Works",
        description=(
            "Send at least one message, image, or video anywhere in the server each day.\n\n"
            "Every participant must complete the requirement individually.\n\n"
            "If you miss a day and have a freeze available, it is used automatically.\n\n"
            "If you miss a day with no freeze available, the streak breaks.\n\n"
            "Only your first qualifying activity of the day is needed."
        ),
        color=INFO_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def new_day_embed(day_number_label: str, settings: GuildSettings) -> discord.Embed:
    embed = discord.Embed(
        title=f"Day {day_number_label} Started",
        description="A new streak day has begun.",
        color=BRAND_COLOR,
    )
    embed.add_field(name="Deadline", value=format_deadline(settings.deadline_hour, settings.deadline_minute), inline=True)
    embed.add_field(
        name="Requirement",
        value="Send at least one message, image, or video anywhere in the server.",
        inline=False,
    )
    return embed


def warning_embed(hours_label: str, pending_mentions: str) -> discord.Embed:
    embed = discord.Embed(
        title="Streak At Risk",
        description=(
            f"Only {hours_label} remain.\n\n"
            "Some participants still need to complete today's activity.\n\n"
            "Send one message, image, or video anywhere in the server to keep the streak alive."
        ),
        color=WARN_COLOR,
    )
    if pending_mentions:
        embed.add_field(name="Pending", value=pending_mentions, inline=False)
    return embed


def streak_secured_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Streak Secured",
        description="Today's requirement has been completed. The streak continues.\n\nSee you tomorrow.",
        color=GOOD_COLOR,
    )
    return embed


def streak_broken_embed(mention: str) -> discord.Embed:
    embed = discord.Embed(
        title="Streak Broken",
        description=f"{mention} did not complete today's requirement and no freeze was available.\n\nThe streak has ended.",
        color=BAD_COLOR,
    )
    return embed


def freeze_activated_embed(mention: str, remaining: int) -> discord.Embed:
    embed = discord.Embed(
        title="Streak Freeze Used",
        description=f"A missed daily activity for {mention} was automatically protected by a streak freeze.",
        color=discord.Color.teal(),
    )
    embed.add_field(name="Freeze Remaining", value=f"{remaining} / {MONTHLY_FREEZE_ALLOWANCE}")
    return embed


def milestone_embed(mention: str, milestone: int) -> discord.Embed:
    embed = discord.Embed(
        title="Milestone Unlocked",
        description=f"{mention} reached a {milestone} day streak. Congratulations.",
        color=discord.Color.gold(),
    )
    return embed


def admin_settings_embed(settings: GuildSettings, channel_mention: str, quote_channel_mention: str) -> discord.Embed:
    embed = discord.Embed(title="Streak Configuration", color=INFO_COLOR)
    embed.add_field(name="Daily Updates Channel", value=channel_mention, inline=False)
    embed.add_field(name="Timezone", value=settings.timezone, inline=True)
    embed.add_field(name="Daily Deadline", value=format_deadline(settings.deadline_hour, settings.deadline_minute), inline=True)
    embed.add_field(name="2-Hour Reminder", value="On" if settings.warning_2h_enabled else "Off", inline=True)
    embed.add_field(name="30-Minute Reminder", value="On" if settings.warning_30m_enabled else "Off", inline=True)
    embed.add_field(name="Streak System", value="Enabled" if settings.streak_enabled else "Disabled", inline=True)
    embed.add_field(name="Quote Channel", value=quote_channel_mention, inline=False)
    configured_feeds = len(settings.quote_feeds())
    embed.add_field(name="Quote Sources Configured", value=f"{configured_feeds} / 3", inline=True)
    embed.add_field(name="Hourly Quotes", value="Enabled" if settings.quotes_enabled else "Disabled", inline=True)
    embed.set_footer(text=FOOTER_TEXT)
    return embed


def user_settings_embed() -> discord.Embed:
    embed = discord.Embed(
        title="Personal Settings",
        description="Notification preferences are managed through your Discord notification "
        "settings for this server. There are no additional personal settings at this time.",
        color=INFO_COLOR,
    )
    embed.set_footer(text=FOOTER_TEXT)
    return embed


# =====================================================================
# STREAK ENGINE
# =====================================================================

class StreakEngine:
    def __init__(self, bot: "StreakBot"):
        self.bot = bot

    async def record_activity(self, guild_id: str, user_id: str, day_key: str) -> None:
        await db_call(db.get_or_create_user, guild_id, user_id)
        newly_marked = await db_call(db.mark_activity, guild_id, user_id, day_key)
        if newly_marked:
            user_row = await db_call(db.get_or_create_user, guild_id, user_id)
            updates = {"total_active_days": user_row["total_active_days"] + 1}
            if not user_row["streak_start_date"] and user_row["current_streak"] == 0:
                # A brand new streak is beginning. started_at is a
                # permanent field from this point on — it will not be
                # touched by activity-retention cleanup, and only changes
                # again if this streak eventually breaks and a new one
                # starts.
                updates["streak_start_date"] = day_key
                updates["status"] = "active"
            await db_call(db.update_user, guild_id, user_id, **updates)

    async def evaluate_day(self, guild: discord.Guild, settings: GuildSettings, day_key: str) -> None:
        guild_id = str(guild.id)
        already = await db_call(db.is_day_evaluated, guild_id, day_key)
        if already:
            return
        marked = await db_call(db.mark_day_evaluated, guild_id, day_key)
        if not marked:
            # Another process/tick beat us to it.
            return

        tracked_users = await db_call(db.list_tracked_users, guild_id)
        if not tracked_users:
            return

        year_month = year_month_of(day_key)
        all_completed = True
        broken_mentions: list[str] = []
        freeze_events: list[tuple[str, int]] = []
        milestone_events: list[tuple[str, int]] = []

        for user_row in tracked_users:
            user_id = user_row["user_id"]

            # Defensive guard: the streak aggregates are the source of
            # truth, never a COUNT of daily_activity rows. If this exact
            # day was already applied to this user's streak (e.g. a rare
            # double-tick around the deadline), skip re-applying it rather
            # than incrementing/consuming a freeze a second time.
            if user_row["last_completed_date"] == day_key:
                continue

            completed = await db_call(db.has_activity, guild_id, user_id, day_key)
            if completed:
                new_streak = user_row["current_streak"] + 1
                new_longest = max(new_streak, user_row["longest_streak"])
                await db_call(
                    db.update_user,
                    guild_id,
                    user_id,
                    current_streak=new_streak,
                    longest_streak=new_longest,
                    total_completed_days=user_row["total_completed_days"] + 1,
                    last_completed_date=day_key,
                    status="active",
                )
                achieved = await db_call(db.achieved_milestones, guild_id, user_id)
                if new_streak in MILESTONES and new_streak not in achieved:
                    recorded = await db_call(db.record_milestone, guild_id, user_id, new_streak)
                    if recorded:
                        milestone_events.append((user_id, new_streak))
            else:
                all_completed = False
                froze = await db_call(db.consume_freeze, guild_id, user_id, year_month, day_key)
                if froze:
                    # A freeze protects the streak: current_streak is left
                    # untouched (never recomputed from activity rows), but
                    # last_completed_date advances so the day is not
                    # re-evaluated and so retention cleanup of the
                    # underlying activity rows can never affect the count.
                    await db_call(
                        db.update_user,
                        guild_id,
                        user_id,
                        freezes_used_total=user_row["freezes_used_total"] + 1,
                        last_completed_date=day_key,
                        status="active",
                    )
                    remaining = await db_call(db.get_or_create_freeze_balance, guild_id, user_id, year_month)
                    freeze_events.append((user_id, remaining))
                else:
                    if user_row["current_streak"] > 0:
                        await db_call(
                            db.update_user,
                            guild_id,
                            user_id,
                            current_streak=0,
                            broken_streaks=user_row["broken_streaks"] + 1,
                            streak_start_date=None,
                            last_completed_date=None,
                            status="broken",
                        )
                        broken_mentions.append(user_id)

        channel = await self.bot.get_daily_channel(guild, settings)
        if channel is None:
            return

        try:
            if all_completed:
                await channel.send(embed=streak_secured_embed())
            for user_id, remaining in freeze_events:
                await channel.send(embed=freeze_activated_embed(f"<@{user_id}>", remaining))
            for user_id in broken_mentions:
                await channel.send(embed=streak_broken_embed(f"<@{user_id}>"))
            for user_id, milestone in milestone_events:
                await channel.send(embed=milestone_embed(f"<@{user_id}>", milestone))
        except discord.HTTPException as exc:
            log.warning("Failed to send daily evaluation update in guild %s: %s", guild_id, exc)

    async def announce_new_day(self, guild: discord.Guild, settings: GuildSettings, day_key: str) -> None:
        channel = await self.bot.get_daily_channel(guild, settings)
        if channel is None:
            return
        tracked_users = await db_call(db.list_tracked_users, str(guild.id))
        best = max((u["current_streak"] for u in tracked_users), default=0) + 1
        try:
            await channel.send(embed=new_day_embed(str(best), settings))
        except discord.HTTPException as exc:
            log.warning("Failed to announce new day in guild %s: %s", guild.id, exc)

    async def send_warning(self, guild: discord.Guild, settings: GuildSettings, day_key: str, hours_label: str) -> None:
        channel = await self.bot.get_daily_channel(guild, settings)
        if channel is None:
            return
        tracked_users = await db_call(db.list_tracked_users, str(guild.id))
        pending = []
        for user_row in tracked_users:
            done = await db_call(db.has_activity, str(guild.id), user_row["user_id"], day_key)
            if not done:
                pending.append(user_row["user_id"])
        if not pending:
            return
        mentions = " ".join(f"<@{uid}>" for uid in pending)
        try:
            await channel.send(embed=warning_embed(hours_label, mentions))
        except discord.HTTPException as exc:
            log.warning("Failed to send warning in guild %s: %s", guild.id, exc)


# =====================================================================
# QUOTE SYSTEM
# =====================================================================

QUOTE_COLOR = discord.Color.dark_gold()
QUOTE_SOURCE_LABELS = ["Site 1", "Site 2", "Site 3"]
QUOTE_FETCH_TIMEOUT_SECONDS = 8

# A small curated fallback pool (mixed inspirational and funny) used
# whenever every configured feed is unreachable, empty, or unconfigured,
# so the hourly post never silently fails to appear.
FALLBACK_QUOTES: list[tuple[str, str]] = [
    ("The only way to do great work is to love what you do.", "Steve Jobs"),
    ("I have not failed. I've just found 10,000 ways that won't work.", "Thomas Edison"),
    ("Do or do not. There is no try.", "Yoda"),
    ("I'm not lazy, I'm just on my energy-saving mode.", "Unknown"),
    ("The future belongs to those who believe in the beauty of their dreams.", "Eleanor Roosevelt"),
    ("I am so clever that sometimes I don't understand a single word of what I am saying.", "Oscar Wilde"),
    ("Whether you think you can or you think you can't, you're right.", "Henry Ford"),
    ("My bed is a magical place where I suddenly remember everything I forgot to do.", "Unknown"),
    ("It always seems impossible until it's done.", "Nelson Mandela"),
    ("I used to think I was indecisive, but now I'm not so sure.", "Unknown"),
    ("Believe you can and you're halfway there.", "Theodore Roosevelt"),
    ("Behind every great person is a substantial amount of coffee.", "Unknown"),
    ("Success is not final, failure is not fatal: it is the courage to continue that counts.", "Winston Churchill"),
    ("I told my computer I needed a break, and now it won't stop sending me error messages.", "Unknown"),
    ("The best time to plant a tree was 20 years ago. The second best time is now.", "Chinese Proverb"),
]


def _strip_html(raw: str) -> str:
    """Removes HTML tags and unescapes entities from RSS item text."""
    text = re.sub(r"<[^>]+>", "", raw or "")
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def _split_quote_and_author(text: str) -> tuple[str, Optional[str]]:
    """Many quote feeds format items as 'Quote text - Author'. Splits that
    out when the pattern is clearly present; otherwise returns the text as
    the quote with no author."""
    match = re.match(r"^(.*\S)\s+[-\u2013\u2014]\s+([^-\u2013\u2014]{2,60})$", text)
    if match:
        return match.group(1).strip(" \"'"), match.group(2).strip()
    return text.strip(" \"'"), None


async def fetch_quote_from_feed(url: str) -> Optional[tuple[str, Optional[str]]]:
    """Fetches an RSS/Atom feed and returns (quote_text, author_or_none) from
    a randomly chosen item, or None if the feed could not be fetched or
    parsed. This is deliberately tolerant: any failure just returns None so
    the caller can fall back to another source."""
    try:
        timeout = aiohttp.ClientTimeout(total=QUOTE_FETCH_TIMEOUT_SECONDS)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "Mozilla/5.0 (StreakBot QuoteFetcher)"}) as resp:
                if resp.status != 200:
                    return None
                raw = await resp.text()
    except Exception as exc:
        log.warning("Quote feed fetch failed for %s: %s", url, exc)
        return None

    try:
        root = ET.fromstring(raw)
    except ET.ParseError:
        return None

    candidates: list[str] = []
    # Standard RSS 2.0: rss > channel > item > (title|description)
    for item in root.findall(".//item"):
        title_el = item.find("title")
        desc_el = item.find("description")
        text = ""
        if desc_el is not None and desc_el.text:
            text = _strip_html(desc_el.text)
        if not text and title_el is not None and title_el.text:
            text = _strip_html(title_el.text)
        if text:
            candidates.append(text)

    # Atom fallback: feed > entry > (title|summary)
    if not candidates:
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        for entry in root.findall(".//atom:entry", ns):
            title_el = entry.find("atom:title", ns)
            summary_el = entry.find("atom:summary", ns)
            text = ""
            if summary_el is not None and summary_el.text:
                text = _strip_html(summary_el.text)
            if not text and title_el is not None and title_el.text:
                text = _strip_html(title_el.text)
            if text:
                candidates.append(text)

    if not candidates:
        return None

    chosen = random.choice(candidates)
    return _split_quote_and_author(chosen)


def quote_embed(text: str, author: Optional[str], source_label: str) -> discord.Embed:
    embed = discord.Embed(
        title="Quote of the Hour",
        description=f"\u201c{text}\u201d",
        color=QUOTE_COLOR,
    )
    if author:
        embed.add_field(name="Author", value=author, inline=True)
    embed.set_footer(text=f"Source: {source_label}")
    return embed


class QuoteEngine:
    def __init__(self, bot: "StreakBot"):
        self.bot = bot

    async def post_hourly_quote(self, guild: discord.Guild, settings: GuildSettings) -> None:
        channel = await self.bot.get_quote_channel(guild, settings)
        if channel is None:
            return

        feeds = settings.quote_feeds()
        text: Optional[str] = None
        author: Optional[str] = None
        source_label = "Local Collection"

        if feeds:
            # Rotate: 1st post from feed 1, 2nd from feed 2, 3rd from feed
            # 3, then repeat — falling through to the next feed, then the
            # local fallback pool, if a source is unreachable or empty.
            start_index = settings.quote_rotation_index % len(feeds)
            for offset in range(len(feeds)):
                idx = (start_index + offset) % len(feeds)
                result = await fetch_quote_from_feed(feeds[idx])
                if result and result[0]:
                    text, author = result
                    source_label = (
                        QUOTE_SOURCE_LABELS[idx] if idx < len(QUOTE_SOURCE_LABELS) else f"Feed {idx + 1}"
                    )
                    break

        if text is None:
            text, author = random.choice(FALLBACK_QUOTES)

        try:
            await channel.send(embed=quote_embed(text, author, source_label))
        except discord.HTTPException as exc:
            log.warning("Failed to post hourly quote in guild %s: %s", guild.id, exc)
            return

        next_index = (settings.quote_rotation_index + 1) % max(len(feeds), 1)
        await db_call(db.update_guild_config, str(guild.id), quote_rotation_index=next_index)


# =====================================================================
# UI COMPONENTS (dashboard)
# =====================================================================

class DashboardView(discord.ui.View):
    """The persistent public dashboard view. Fixed custom_ids so it survives
    bot restarts."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="My Streak", style=discord.ButtonStyle.primary, custom_id="streak:my_streak", row=0)
    async def my_streak(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.handle_my_streak(interaction)

    @discord.ui.button(label="Statistics", style=discord.ButtonStyle.secondary, custom_id="streak:statistics", row=0)
    async def statistics(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.handle_statistics(interaction)

    @discord.ui.button(label="Achievements", style=discord.ButtonStyle.secondary, custom_id="streak:achievements", row=0)
    async def achievements(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.handle_achievements(interaction)

    @discord.ui.button(label="Streak Freezes", style=discord.ButtonStyle.secondary, custom_id="streak:freezes", row=1)
    async def freezes(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.handle_freezes(interaction)

    @discord.ui.button(label="How It Works", style=discord.ButtonStyle.secondary, custom_id="streak:how", row=1)
    async def how_it_works(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(embed=how_it_works_embed(), ephemeral=True)

    @discord.ui.button(label="Settings", style=discord.ButtonStyle.secondary, custom_id="streak:settings", row=1)
    async def settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.handle_settings_entry(interaction)


# -- settings entry (user vs admin) -------------------------------------

class SettingsEntryView(discord.ui.View):
    def __init__(self, is_admin: bool):
        super().__init__(timeout=120)
        if not is_admin:
            self.remove_item(self.server_settings)

    @discord.ui.button(label="User Settings", style=discord.ButtonStyle.primary)
    async def user_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.edit_message(embed=user_settings_embed(), view=None)

    @discord.ui.button(label="Server Settings", style=discord.ButtonStyle.danger)
    async def server_settings(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.client.show_admin_settings(interaction)


# -- admin settings ------------------------------------------------------

class DeadlineModal(discord.ui.Modal, title="Set Daily Deadline"):
    deadline_input = discord.ui.TextInput(
        label="Deadline (24h HH:MM)",
        placeholder="00:00",
        min_length=4,
        max_length=5,
    )

    def __init__(self, bot: "StreakBot", guild_id: str):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.deadline_input.value.strip()
        try:
            hour_str, minute_str = raw.split(":")
            hour, minute = int(hour_str), int(minute_str)
            if not (0 <= hour <= 23 and 0 <= minute <= 59):
                raise ValueError
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "Invalid time format. Use 24-hour HH:MM, e.g. 22:30.", ephemeral=True
            )
            return
        await db_call(db.update_guild_config, self.guild_id, deadline_hour=hour, deadline_minute=minute)
        await interaction.response.send_message(
            f"Daily deadline updated to {format_deadline(hour, minute)}.", ephemeral=True
        )


class TimezoneModal(discord.ui.Modal, title="Set Server Timezone"):
    tz_input = discord.ui.TextInput(
        label="IANA timezone name",
        placeholder="Asia/Karachi",
        min_length=3,
        max_length=64,
    )

    def __init__(self, bot: "StreakBot", guild_id: str):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id

    async def on_submit(self, interaction: discord.Interaction):
        name = self.tz_input.value.strip()
        if safe_zone(name) is None:
            await interaction.response.send_message(
                f"'{name}' is not a recognized IANA timezone name.", ephemeral=True
            )
            return
        await db_call(db.update_guild_config, self.guild_id, timezone=name)
        await interaction.response.send_message(f"Timezone updated to {name}.", ephemeral=True)


class DailyChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="Select the daily updates channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await db_call(db.update_guild_config, self.guild_id, daily_channel_id=str(channel.id))
        await interaction.response.send_message(
            f"Daily updates channel set to {channel.mention}.", ephemeral=True
        )


class ChannelSelectView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=120)
        self.add_item(DailyChannelSelect(guild_id))


class QuoteChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, guild_id: str):
        super().__init__(
            placeholder="Select the quote channel",
            channel_types=[discord.ChannelType.text],
            min_values=1,
            max_values=1,
        )
        self.guild_id = guild_id

    async def callback(self, interaction: discord.Interaction):
        channel = self.values[0]
        await db_call(db.update_guild_config, self.guild_id, quote_channel_id=str(channel.id))
        await interaction.response.send_message(
            f"Quote channel set to {channel.mention}.", ephemeral=True
        )


class QuoteChannelSelectView(discord.ui.View):
    def __init__(self, guild_id: str):
        super().__init__(timeout=120)
        self.add_item(QuoteChannelSelect(guild_id))


class QuoteFeedsModal(discord.ui.Modal, title="Set Quote Sources"):
    feed_1 = discord.ui.TextInput(
        label="Site 1 RSS feed URL",
        placeholder="https://www.brainyquote.com/feeds/todays_quote",
        required=False,
        max_length=300,
    )
    feed_2 = discord.ui.TextInput(
        label="Site 2 RSS feed URL",
        placeholder="https://example.com/feed-2.rss",
        required=False,
        max_length=300,
    )
    feed_3 = discord.ui.TextInput(
        label="Site 3 RSS feed URL",
        placeholder="https://example.com/feed-3.rss",
        required=False,
        max_length=300,
    )

    def __init__(self, bot: "StreakBot", guild_id: str, settings: GuildSettings):
        super().__init__()
        self.bot = bot
        self.guild_id = guild_id
        self.feed_1.default = settings.quote_feed_1 or ""
        self.feed_2.default = settings.quote_feed_2 or ""
        self.feed_3.default = settings.quote_feed_3 or ""

    async def on_submit(self, interaction: discord.Interaction):
        def clean(value: str) -> Optional[str]:
            value = value.strip()
            return value or None

        urls = [clean(self.feed_1.value), clean(self.feed_2.value), clean(self.feed_3.value)]
        for url in urls:
            if url and not (url.startswith("http://") or url.startswith("https://")):
                await interaction.response.send_message(
                    f"'{url}' doesn't look like a valid URL. Feed URLs must start with http:// or https://.",
                    ephemeral=True,
                )
                return

        await db_call(
            db.update_guild_config,
            self.guild_id,
            quote_feed_1=urls[0],
            quote_feed_2=urls[1],
            quote_feed_3=urls[2],
            quote_rotation_index=0,
        )
        configured = sum(1 for u in urls if u)
        await interaction.response.send_message(
            f"Quote sources updated ({configured} / 3 configured). "
            "Posts rotate through them in order; any unreachable or unconfigured "
            "source falls back to the next one, then to a built-in quote collection.",
            ephemeral=True,
        )


class AdminSettingsView(discord.ui.View):
    def __init__(self, bot: "StreakBot", guild_id: str, settings: GuildSettings):
        super().__init__(timeout=180)
        self.bot = bot
        self.guild_id = guild_id
        self.settings = settings

    @discord.ui.button(label="Change Channel", style=discord.ButtonStyle.primary, row=0)
    async def change_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select the new daily updates channel:", view=ChannelSelectView(self.guild_id), ephemeral=True
        )

    @discord.ui.button(label="Change Timezone", style=discord.ButtonStyle.primary, row=0)
    async def change_timezone(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TimezoneModal(self.bot, self.guild_id))

    @discord.ui.button(label="Change Deadline", style=discord.ButtonStyle.primary, row=0)
    async def change_deadline(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(DeadlineModal(self.bot, self.guild_id))

    @discord.ui.button(label="Toggle 2h Reminder", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_2h(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = 0 if self.settings.warning_2h_enabled else 1
        await db_call(db.update_guild_config, self.guild_id, warning_2h_enabled=new_val)
        await interaction.response.send_message(
            f"2-hour reminder is now {'on' if new_val else 'off'}.", ephemeral=True
        )

    @discord.ui.button(label="Toggle 30m Reminder", style=discord.ButtonStyle.secondary, row=1)
    async def toggle_30m(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = 0 if self.settings.warning_30m_enabled else 1
        await db_call(db.update_guild_config, self.guild_id, warning_30m_enabled=new_val)
        await interaction.response.send_message(
            f"30-minute reminder is now {'on' if new_val else 'off'}.", ephemeral=True
        )

    @discord.ui.button(label="Toggle Streak System", style=discord.ButtonStyle.danger, row=1)
    async def toggle_system(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = 0 if self.settings.streak_enabled else 1
        await db_call(db.update_guild_config, self.guild_id, streak_enabled=new_val)
        await interaction.response.send_message(
            f"Streak system is now {'enabled' if new_val else 'disabled'}.", ephemeral=True
        )

    @discord.ui.button(label="Change Quote Channel", style=discord.ButtonStyle.primary, row=2)
    async def change_quote_channel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "Select the channel for hourly quotes:", view=QuoteChannelSelectView(self.guild_id), ephemeral=True
        )

    @discord.ui.button(label="Change Quote Sources", style=discord.ButtonStyle.primary, row=2)
    async def change_quote_sources(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(QuoteFeedsModal(self.bot, self.guild_id, self.settings))

    @discord.ui.button(label="Toggle Hourly Quotes", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_quotes(self, interaction: discord.Interaction, button: discord.ui.Button):
        new_val = 0 if self.settings.quotes_enabled else 1
        await db_call(db.update_guild_config, self.guild_id, quotes_enabled=new_val)
        await interaction.response.send_message(
            f"Hourly quotes are now {'enabled' if new_val else 'disabled'}.", ephemeral=True
        )


# =====================================================================
# BOT
# =====================================================================

class StreakBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.guilds = True
        intents.messages = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.engine = StreakEngine(self)
        self.quote_engine = QuoteEngine(self)
        self._scheduler_started = False
        self._cleanup_started = False

    async def setup_hook(self) -> None:
        self.add_view(DashboardView())
        guild_obj = discord.Object(id=int(GUILD_ID)) if GUILD_ID else None
        try:
            if guild_obj:
                self.tree.copy_global_to(guild=guild_obj)
                await self.tree.sync(guild=guild_obj)
            else:
                await self.tree.sync()
        except discord.HTTPException as exc:
            log.warning("Slash command sync failed: %s", exc)

    async def on_ready(self):
        log.info("Logged in as %s (%s)", self.user, self.user.id if self.user else "?")
        for guild in self.guilds:
            await self.ensure_dashboard(guild)
        if not self._scheduler_started:
            self.scheduler_loop.start()
            self._scheduler_started = True
        if not self._cleanup_started:
            self.cleanup_loop.start()
            self._cleanup_started = True

    # -- dashboard lifecycle ------------------------------------------
    async def ensure_dashboard(self, guild: discord.Guild) -> None:
        guild_id = str(guild.id)
        channel = guild.get_channel(int(STREAK_DASHBOARD_CHANNEL_ID))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(STREAK_DASHBOARD_CHANNEL_ID))
            except discord.HTTPException:
                log.error("Dashboard channel %s not found for guild %s.", STREAK_DASHBOARD_CHANNEL_ID, guild_id)
                return

        state = await db_call(db.get_dashboard_state, guild_id)
        embed = build_dashboard_embed()
        view = DashboardView()

        if state and state["dashboard_message_id"]:
            try:
                message = await channel.fetch_message(int(state["dashboard_message_id"]))
                await message.edit(embed=embed, view=view)
                return
            except discord.NotFound:
                log.info("Stored dashboard message missing for guild %s; recreating.", guild_id)
            except discord.HTTPException as exc:
                log.warning("Could not edit dashboard message for guild %s: %s", guild_id, exc)

        try:
            message = await channel.send(embed=embed, view=view)
            await db_call(db.upsert_dashboard_state, guild_id, str(channel.id), str(message.id))
        except discord.HTTPException as exc:
            log.error("Could not create dashboard message for guild %s: %s", guild_id, exc)

    async def get_daily_channel(self, guild: discord.Guild, settings: GuildSettings) -> Optional[discord.TextChannel]:
        if not settings.daily_channel_id:
            return None
        channel = guild.get_channel(int(settings.daily_channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(settings.daily_channel_id))
            except discord.HTTPException:
                log.warning("Configured daily channel missing for guild %s.", guild.id)
                return None
        return channel

    async def get_quote_channel(self, guild: discord.Guild, settings: GuildSettings) -> Optional[discord.TextChannel]:
        if not settings.quote_channel_id:
            return None
        channel = guild.get_channel(int(settings.quote_channel_id))
        if channel is None:
            try:
                channel = await self.fetch_channel(int(settings.quote_channel_id))
            except discord.HTTPException:
                log.warning("Configured quote channel missing for guild %s.", guild.id)
                return None
        return channel

    # -- button handlers -------------------------------------------------
    async def handle_my_streak(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)
        config_row = await db_call(db.get_or_create_guild_config, guild_id)
        settings = GuildSettings.from_row(config_row)
        user_row = await db_call(db.get_or_create_user, guild_id, user_id)
        now_local = datetime.now(settings.tzinfo())
        current_period = period_key_for(now_local, settings.deadline_hour, settings.deadline_minute)
        completed_today = await db_call(db.has_activity, guild_id, user_id, current_period)
        embed = my_streak_embed(interaction.user.display_name, user_row, settings, completed_today)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def handle_statistics(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)
        user_row = await db_call(db.get_or_create_user, guild_id, user_id)
        await interaction.response.send_message(embed=statistics_embed(user_row), ephemeral=True)

    async def handle_achievements(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)
        achieved = await db_call(db.achieved_milestones, guild_id, user_id)
        await interaction.response.send_message(embed=achievements_embed(achieved), ephemeral=True)

    async def handle_freezes(self, interaction: discord.Interaction):
        guild_id = str(interaction.guild_id)
        user_id = str(interaction.user.id)
        config_row = await db_call(db.get_or_create_guild_config, guild_id)
        settings = GuildSettings.from_row(config_row)
        now_local = datetime.now(settings.tzinfo())
        year_month = now_local.strftime("%Y-%m")
        balance = await db_call(db.get_or_create_freeze_balance, guild_id, user_id, year_month)
        history = await db_call(db.recent_freeze_usage, guild_id, user_id)
        if now_local.month == 12:
            reset_dt = now_local.replace(year=now_local.year + 1, month=1, day=1)
        else:
            reset_dt = now_local.replace(month=now_local.month + 1, day=1)
        reset_label = reset_dt.strftime("%B %-d") if os.name != "nt" else reset_dt.strftime("%B %d")
        await interaction.response.send_message(
            embed=freezes_embed(balance, reset_label, history), ephemeral=True
        )

    async def handle_settings_entry(self, interaction: discord.Interaction):
        is_admin = isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild
        await interaction.response.send_message(
            "Choose which settings to view:", view=SettingsEntryView(is_admin), ephemeral=True
        )

    async def show_admin_settings(self, interaction: discord.Interaction):
        if not (isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.manage_guild):
            await interaction.response.edit_message(content="You do not have permission to view this.", embed=None, view=None)
            return
        guild_id = str(interaction.guild_id)
        config_row = await db_call(db.get_or_create_guild_config, guild_id)
        settings = GuildSettings.from_row(config_row)
        channel_mention = f"<#{settings.daily_channel_id}>" if settings.daily_channel_id else "Not configured"
        quote_channel_mention = f"<#{settings.quote_channel_id}>" if settings.quote_channel_id else "Not configured"
        embed = admin_settings_embed(settings, channel_mention, quote_channel_mention)
        view = AdminSettingsView(self, guild_id, settings)
        await interaction.response.edit_message(content=None, embed=embed, view=view)

    # -- scheduler --------------------------------------------------
    @tasks.loop(seconds=30)
    async def scheduler_loop(self):
        for guild in list(self.guilds):
            try:
                await self._tick_guild(guild)
            except Exception:
                log.exception("Scheduler tick failed for guild %s", guild.id)

    @scheduler_loop.before_loop
    async def before_scheduler(self):
        await self.wait_until_ready()

    @tasks.loop(hours=24)
    async def cleanup_loop(self):
        """Removes detailed daily_activity rows older than the retention
        window. Streak aggregates (current_streak, longest_streak,
        started_at, last_completed_date, status) and all other tables are
        untouched — see Database.cleanup_old_activity."""
        try:
            removed = await db_call(db.cleanup_old_activity, ACTIVITY_RETENTION_DAYS)
            if removed:
                log.info("Retention cleanup removed %d daily_activity row(s) older than %d days.", removed, ACTIVITY_RETENTION_DAYS)
        except Exception:
            log.exception("Retention cleanup failed; streak aggregates are unaffected regardless.")

    @cleanup_loop.before_loop
    async def before_cleanup(self):
        await self.wait_until_ready()

    async def _tick_guild(self, guild: discord.Guild) -> None:
        guild_id = str(guild.id)
        config_row = await db_call(db.get_or_create_guild_config, guild_id)
        settings = GuildSettings.from_row(config_row)

        if settings.streak_enabled:
            await self._tick_streak(guild, guild_id, settings)

        if settings.quotes_enabled:
            await self._tick_quote(guild, guild_id, settings)

    async def _tick_streak(self, guild: discord.Guild, guild_id: str, settings: GuildSettings) -> None:
        tz = settings.tzinfo()
        now_local = datetime.now(tz)
        current_period = period_key_for(now_local, settings.deadline_hour, settings.deadline_minute)

        if settings.last_day_key is None:
            # First run ever for this guild: just initialize, nothing to evaluate.
            await db_call(db.update_guild_config, guild_id, last_day_key=current_period)
            settings.last_day_key = current_period

        elif current_period != settings.last_day_key:
            ended_period = settings.last_day_key
            await self.engine.evaluate_day(guild, settings, ended_period)
            await db_call(
                db.update_guild_config,
                guild_id,
                last_day_key=current_period,
                last_2h_warn_period=None,
                last_30m_warn_period=None,
            )
            await self.engine.announce_new_day(guild, settings, current_period)
            settings.last_day_key = current_period
            settings.last_2h_warn_period = None
            settings.last_30m_warn_period = None

        # Warnings for the period currently in progress.
        upcoming_deadline = next_deadline_dt(now_local, settings.deadline_hour, settings.deadline_minute)
        two_hour_mark = upcoming_deadline - timedelta(hours=2)
        thirty_min_mark = upcoming_deadline - timedelta(minutes=30)

        if (
            settings.warning_2h_enabled
            and settings.last_2h_warn_period != current_period
            and two_hour_mark <= now_local < upcoming_deadline
        ):
            await self.engine.send_warning(guild, settings, current_period, "2 hours")
            await db_call(db.update_guild_config, guild_id, last_2h_warn_period=current_period)

        if (
            settings.warning_30m_enabled
            and settings.last_30m_warn_period != current_period
            and thirty_min_mark <= now_local < upcoming_deadline
        ):
            await self.engine.send_warning(guild, settings, current_period, "30 minutes")
            await db_call(db.update_guild_config, guild_id, last_30m_warn_period=current_period)

    async def _tick_quote(self, guild: discord.Guild, guild_id: str, settings: GuildSettings) -> None:
        if not settings.quote_channel_id:
            return
        tz = settings.tzinfo()
        now_local = datetime.now(tz)
        current_hour_key = now_local.strftime("%Y-%m-%d-%H")
        if settings.last_quote_period == current_hour_key:
            return
        await db_call(db.update_guild_config, guild_id, last_quote_period=current_hour_key)
        await self.quote_engine.post_hourly_quote(guild, settings)


bot = StreakBot()


# =====================================================================
# EVENT HANDLERS
# =====================================================================

@bot.event
async def on_guild_join(guild: discord.Guild):
    await bot.ensure_dashboard(guild)


@bot.event
async def on_message(message: discord.Message):
    if not is_qualifying_message(message):
        return

    guild_id = str(message.guild.id)
    config_row = await db_call(db.get_or_create_guild_config, guild_id)
    settings = GuildSettings.from_row(config_row)
    if not settings.streak_enabled:
        return

    now_local = datetime.now(settings.tzinfo())
    current_period = period_key_for(now_local, settings.deadline_hour, settings.deadline_minute)
    await bot.engine.record_activity(guild_id, str(message.author.id), current_period)


# Minimal admin slash command, kept intentionally secondary to the dashboard.
@bot.tree.command(name="streak-admin", description="Open the streak server configuration panel.")
@app_commands.checks.has_permissions(manage_guild=True)
async def streak_admin(interaction: discord.Interaction):
    guild_id = str(interaction.guild_id)
    config_row = await db_call(db.get_or_create_guild_config, guild_id)
    settings = GuildSettings.from_row(config_row)
    channel_mention = f"<#{settings.daily_channel_id}>" if settings.daily_channel_id else "Not configured"
    quote_channel_mention = f"<#{settings.quote_channel_id}>" if settings.quote_channel_id else "Not configured"
    embed = admin_settings_embed(settings, channel_mention, quote_channel_mention)
    view = AdminSettingsView(bot, guild_id, settings)
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


@streak_admin.error
async def streak_admin_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, app_commands.MissingPermissions):
        await interaction.response.send_message(
            "You need the Manage Server permission to use this.", ephemeral=True
        )
    else:
        log.exception("Unhandled app command error", exc_info=error)
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong.", ephemeral=True)


# =====================================================================
# BOT STARTUP
# =====================================================================

def _run_cleanup_persistence_self_test() -> bool:
    """Development-only verification of the requirement this update exists
    for: a 3-month retention cleanup of daily_activity must never reset,
    reduce, or otherwise affect an active streak. Runs against a disposable
    temporary database — never touches streaks.db. Invoke with:

        python bot.py --self-test-cleanup
    """
    tmp_path = os.path.join(tempfile.gettempdir(), "streakbot_selftest.db")
    if os.path.exists(tmp_path):
        os.remove(tmp_path)
    test_db = Database(tmp_path)

    guild_id, user_id = "test-guild", "test-user"
    old_day = (date.today() - timedelta(days=200)).isoformat()

    test_db.get_or_create_user(guild_id, user_id)
    test_db.mark_activity(guild_id, user_id, old_day)
    test_db.update_user(
        guild_id,
        user_id,
        current_streak=258,
        longest_streak=258,
        streak_start_date=old_day,
        last_completed_date=old_day,
        total_completed_days=742,
        status="active",
    )

    removed = test_db.cleanup_old_activity(retention_days=90)
    assert removed >= 1, "Expected cleanup to remove the old daily_activity row."
    assert test_db.has_activity(guild_id, user_id, old_day) is False, "Old activity row should be gone."

    row = test_db.get_or_create_user(guild_id, user_id)
    assert row["current_streak"] == 258, f"current_streak changed to {row['current_streak']}"
    assert row["longest_streak"] == 258, f"longest_streak changed to {row['longest_streak']}"
    assert row["streak_start_date"] == old_day, "started_at was altered by cleanup."
    assert row["total_completed_days"] == 742, "total_completed_days was altered by cleanup."

    # Simulate the next successful day continuing the streak.
    new_streak = row["current_streak"] + 1
    test_db.update_user(
        guild_id,
        user_id,
        current_streak=new_streak,
        longest_streak=max(new_streak, row["longest_streak"]),
        last_completed_date=date.today().isoformat(),
        total_completed_days=row["total_completed_days"] + 1,
    )
    row = test_db.get_or_create_user(guild_id, user_id)
    assert row["current_streak"] == 259, f"Expected 258 -> 259, got {row['current_streak']}"

    os.remove(tmp_path)
    print("Self-test passed: retention cleanup did not affect the active streak (258 -> 259).")
    return True


def main():
    if "--self-test-cleanup" in sys.argv:
        _run_cleanup_persistence_self_test()
        return
    validate_env()
    log.info("Starting streak bot. Database: %s", DB_PATH)
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
