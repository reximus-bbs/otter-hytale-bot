"""Otter Nonsense Hytale status bot.

Polls the Nitrado Query plugin and surfaces server state in Discord:
- Pinned live status embed (edits in place every poll).
- Join/leave announcements in #activity.
- Voice-channel name as live counter.
- SQLite history powering /profile, /stats, and a 9am daily recap.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import ssl
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo

import discord
import httpx
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
)
log = logging.getLogger("otter-bot")

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
QUERY_URL = os.environ["QUERY_URL"]
STATUS_CHANNEL_ID = int(os.environ["STATUS_CHANNEL_ID"])
ACTIVITY_CHANNEL_ID = int(os.environ.get("ACTIVITY_CHANNEL_ID") or 0) or None
COUNTER_VOICE_CHANNEL_ID = int(os.environ.get("COUNTER_VOICE_CHANNEL_ID") or 0) or None
RECAP_CHANNEL_ID = int(os.environ.get("RECAP_CHANNEL_ID") or 0) or None
GUILD_ID = int(os.environ["GUILD_ID"]) if os.environ.get("GUILD_ID") else None

POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
INSECURE_TLS = os.environ.get("INSECURE_TLS", "true").lower() == "true"
QUERY_USER = os.environ.get("QUERY_USER")
QUERY_PASS = os.environ.get("QUERY_PASS")
RECAP_TZ = ZoneInfo(os.environ.get("RECAP_TIMEZONE", "America/Chicago"))
RECAP_HOUR = int(os.environ.get("RECAP_HOUR", "9"))
COUNTER_TEMPLATE = os.environ.get("COUNTER_TEMPLATE", "🟢 {current}/{max} playing")
COUNTER_OFFLINE_TEMPLATE = os.environ.get("COUNTER_OFFLINE_TEMPLATE", "🔴 server offline")
COUNTER_MIN_INTERVAL_S = int(os.environ.get("COUNTER_MIN_INTERVAL_S", "300"))

DB_PATH = Path(os.environ.get("DB_PATH", "otter.db"))
STATE_FILE = Path(os.environ.get("STATE_FILE", "state.json"))
ACCEPT_HEADER = "application/x.hytale.nitrado.query+json;version=1"

SERVER_TAGLINE = os.environ.get(
    "SERVER_TAGLINE",
    "A chill community Hytale server. Everyone welcome — bring a friend.",
)
THUMBNAIL_URL = os.environ.get("THUMBNAIL_URL") or None
BANNER_URL = os.environ.get("BANNER_URL") or None

SCHEMA = """
CREATE TABLE IF NOT EXISTS players (
  uuid TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  first_seen TEXT NOT NULL,
  last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_players_name ON players(name);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  uuid TEXT NOT NULL,
  name TEXT NOT NULL,
  world TEXT,
  start TEXT NOT NULL,
  end TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_uuid ON sessions(uuid);
CREATE INDEX IF NOT EXISTS idx_sessions_start ON sessions(start);
CREATE TABLE IF NOT EXISTS snapshots (
  ts TEXT PRIMARY KEY,
  current_players INTEGER NOT NULL,
  max_players INTEGER NOT NULL
);
"""


@contextmanager
def db() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db() as conn:
        conn.executescript(SCHEMA)
        # If we crashed/restarted, close any sessions left open at the last
        # snapshot we have on record (or now if we have none).
        last_ts = conn.execute("SELECT MAX(ts) AS ts FROM snapshots").fetchone()["ts"]
        end = last_ts or utc_now_iso()
        conn.execute("UPDATE sessions SET end = ? WHERE end IS NULL", (end,))


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt_duration(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 60:
        return f"{secs}s"
    mins, secs = divmod(secs, 60)
    if mins < 60:
        return f"{mins}m"
    hrs, mins = divmod(mins, 60)
    if hrs < 24:
        return f"{hrs}h {mins}m"
    days, hrs = divmod(hrs, 24)
    return f"{days}d {hrs}h"


@dataclass
class Snapshot:
    online: bool
    name: str
    version: str
    address: str
    world: str
    current_players: int
    max_players: int
    players: list[dict[str, str]]
    fetched_at: datetime
    error: str | None = None


async def fetch_snapshot(client: httpx.AsyncClient) -> Snapshot:
    auth = (QUERY_USER, QUERY_PASS) if QUERY_USER and QUERY_PASS else None
    try:
        r = await client.get(
            QUERY_URL,
            headers={"Accept": ACCEPT_HEADER},
            auth=auth,
            timeout=10.0,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:
        log.warning("query failed: %s", e)
        return Snapshot(
            online=False, name="Otter Nonsense - Hytale", version="?",
            address="?", world="?", current_players=0, max_players=0,
            players=[], fetched_at=utc_now(), error=str(e),
        )
    basic = data.get("Basic") or {}
    universe = data.get("Universe") or {}
    return Snapshot(
        online=True,
        name=basic.get("Name", "Hytale Server"),
        version=basic.get("Version", "?"),
        address=basic.get("Address", "?"),
        world=universe.get("DefaultWorld", "?"),
        current_players=basic.get("CurrentPlayers", 0),
        max_players=basic.get("MaxPlayers", 0),
        players=data.get("Players") or [],
        fetched_at=utc_now(),
    )


def progress_bar(current: int, maximum: int, width: int = 10) -> str:
    if maximum <= 0:
        return ""
    filled = min(width, round((current / maximum) * width))
    return "🟩" * filled + "⬛" * (width - filled)


def hype_text(snap: Snapshot) -> str:
    if not snap.online:
        return "_We'll be back soon. Hang tight._"
    n = snap.current_players
    if n == 0:
        return "_Be the first one in tonight._ ✨"
    if n == 1:
        return "**1 player online** — go say hi! 👋"
    if n <= 3:
        return f"**{n} on right now** — perfect for a small crew."
    if n <= 6:
        return f"🔥 **{n} playing right now** — hop in!"
    return f"🚀 **{n} online** — big night, get in here!"


def today_stats() -> dict:
    """Stats since 00:00 today in RECAP_TZ."""
    now = utc_now()
    start_local = now.astimezone(RECAP_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc = start_local.astimezone(timezone.utc)
    s_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    with db() as conn:
        peak = conn.execute(
            "SELECT COALESCE(MAX(current_players), 0) AS p FROM snapshots WHERE ts >= ?",
            (s_iso,),
        ).fetchone()["p"]
        sessions = conn.execute(
            "SELECT uuid, start, end FROM sessions "
            "WHERE start >= ? OR end IS NULL OR end >= ?",
            (s_iso, s_iso),
        ).fetchall()
    seen: set[str] = set()
    seconds = 0.0
    for s in sessions:
        seen.add(s["uuid"])
        st = max(parse_iso(s["start"]), start_utc)
        en = parse_iso(s["end"]) if s["end"] else now
        if en > st:
            seconds += (en - st).total_seconds()
    return {"peak": peak, "unique": len(seen), "hours": seconds / 3600}


def build_status_embed(snap: Snapshot) -> discord.Embed:
    if not snap.online:
        embed = discord.Embed(
            title="Otter Nonsense — Hytale",
            description=(
                f"_{SERVER_TAGLINE}_\n\n"
                f"🔴 **Offline** — {snap.error or 'server unreachable'}"
            ),
            color=discord.Color.from_str("#ed4245"),
            timestamp=snap.fetched_at,
        )
        if THUMBNAIL_URL:
            embed.set_thumbnail(url=THUMBNAIL_URL)
        embed.set_footer(text="Auto-refresh every minute • /status for details")
        return embed

    color = (
        discord.Color.from_str("#57f287")
        if snap.current_players > 0
        else discord.Color.from_str("#5865f2")
    )
    embed = discord.Embed(
        title=snap.name,
        description=(
            f"_{SERVER_TAGLINE}_\n\n"
            f"🟢 **Online** · `{snap.address}`\n"
            f"{hype_text(snap)}"
        ),
        color=color,
        timestamp=snap.fetched_at,
    )
    if THUMBNAIL_URL:
        embed.set_thumbnail(url=THUMBNAIL_URL)

    bar = progress_bar(snap.current_players, snap.max_players)
    embed.add_field(
        name="Players",
        value=f"**{snap.current_players}** / {snap.max_players}\n{bar}",
        inline=True,
    )
    embed.add_field(name="World", value=f"`{snap.world}`", inline=True)
    embed.add_field(name="Version", value=f"`{snap.version}`", inline=True)

    if snap.players:
        names = "\n".join(
            f"⚔️ **{p.get('Name', '?')}** · `{p.get('World', '?')}`"
            for p in snap.players[:25]
        )
        if len(snap.players) > 25:
            names += f"\n…and {len(snap.players) - 25} more"
        embed.add_field(name=f"Online now ({len(snap.players)})", value=names, inline=False)

    today = today_stats()
    if today["hours"] >= 1:
        played = f"{today['hours']:.1f}h"
    else:
        played = f"{int(today['hours'] * 60)}m"
    embed.add_field(
        name="📊 Today so far",
        value=(
            f"Peak **{today['peak']}** · "
            f"Unique players **{today['unique']}** · "
            f"Played **{played}**"
        ),
        inline=False,
    )

    if BANNER_URL:
        embed.set_image(url=BANNER_URL)
    embed.set_footer(text="Auto-refresh every minute • /status /players /profile /stats")
    return embed


def lookup_player(name: str) -> sqlite3.Row | None:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM players WHERE LOWER(name) = LOWER(?) ORDER BY last_seen DESC LIMIT 1",
            (name,),
        ).fetchone()


def player_stats(uuid: str) -> dict:
    with db() as conn:
        sessions = conn.execute(
            "SELECT start, end FROM sessions WHERE uuid = ? ORDER BY start",
            (uuid,),
        ).fetchall()
    now = utc_now()
    total = timedelta()
    longest = timedelta()
    completed = 0
    for s in sessions:
        start = parse_iso(s["start"])
        end = parse_iso(s["end"]) if s["end"] else now
        dur = end - start
        total += dur
        if dur > longest:
            longest = dur
        if s["end"]:
            completed += 1
    return {
        "session_count": len(sessions),
        "completed_count": completed,
        "total": total,
        "longest": longest,
    }


def server_stats() -> dict:
    now = utc_now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_start = today_start - timedelta(days=7)
    with db() as conn:
        peak_today = conn.execute(
            "SELECT COALESCE(MAX(current_players), 0) AS p FROM snapshots WHERE ts >= ?",
            (today_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchone()["p"]
        peak_week = conn.execute(
            "SELECT COALESCE(MAX(current_players), 0) AS p FROM snapshots WHERE ts >= ?",
            (week_start.strftime("%Y-%m-%dT%H:%M:%SZ"),),
        ).fetchone()["p"]
        peak_all = conn.execute(
            "SELECT COALESCE(MAX(current_players), 0) AS p FROM snapshots"
        ).fetchone()["p"]
        unique_total = conn.execute("SELECT COUNT(*) AS c FROM players").fetchone()["c"]
        sessions = conn.execute("SELECT start, end FROM sessions").fetchall()
    total_seconds = 0
    for s in sessions:
        start = parse_iso(s["start"])
        end = parse_iso(s["end"]) if s["end"] else now
        total_seconds += (end - start).total_seconds()
    return {
        "peak_today": peak_today,
        "peak_week": peak_week,
        "peak_all": peak_all,
        "unique_total": unique_total,
        "total_play_hours": total_seconds / 3600,
    }


def recap_window_stats() -> dict:
    """Stats for yesterday in the configured timezone."""
    now_local = utc_now().astimezone(RECAP_TZ)
    end_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_local = end_local - timedelta(days=1)
    start_utc = start_local.astimezone(timezone.utc)
    end_utc = end_local.astimezone(timezone.utc)
    s_iso = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    e_iso = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

    with db() as conn:
        peak = conn.execute(
            "SELECT COALESCE(MAX(current_players), 0) AS p FROM snapshots WHERE ts >= ? AND ts < ?",
            (s_iso, e_iso),
        ).fetchone()["p"]
        sessions = conn.execute(
            "SELECT uuid, name, start, end FROM sessions "
            "WHERE start < ? AND (end IS NULL OR end > ?)",
            (e_iso, s_iso),
        ).fetchall()

    seen: dict[str, str] = {}
    seconds = 0.0
    for s in sessions:
        seen.setdefault(s["uuid"], s["name"])
        st = max(parse_iso(s["start"]), start_utc)
        en = min(parse_iso(s["end"]) if s["end"] else utc_now(), end_utc)
        if en > st:
            seconds += (en - st).total_seconds()
    return {
        "date_label": start_local.strftime("%A %b %d"),
        "peak": peak,
        "unique": len(seen),
        "names": list(seen.values()),
        "play_hours": seconds / 3600,
    }


class OtterBot(discord.Client):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.http_client: httpx.AsyncClient | None = None
        self.latest: Snapshot | None = None
        self.live_uuids: set[str] = set()
        self.bootstrapped = False
        self.last_counter_text: str | None = None
        self.last_counter_edit: datetime | None = None

    async def setup_hook(self) -> None:
        verify: bool | ssl.SSLContext = not INSECURE_TLS
        self.http_client = httpx.AsyncClient(verify=verify)
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
        else:
            await self.tree.sync()
        self.poll.start()
        if RECAP_CHANNEL_ID:
            self.daily_recap.start()

    async def close(self) -> None:
        if self.http_client:
            await self.http_client.aclose()
        await super().close()

    async def _channel(self, cid: int) -> discord.abc.GuildChannel | None:
        ch = self.get_channel(cid)
        if ch is None:
            try:
                ch = await self.fetch_channel(cid)
            except discord.NotFound:
                log.error("channel %s not found", cid)
                return None
            except discord.Forbidden:
                log.error("missing access to channel %s", cid)
                return None
        return ch

    @tasks.loop(seconds=POLL_SECONDS)
    async def poll(self) -> None:
        assert self.http_client
        snap = await fetch_snapshot(self.http_client)
        self.latest = snap
        if snap.online:
            try:
                self._record_snapshot(snap)
            except Exception:
                log.exception("record_snapshot failed")
            try:
                await self._handle_player_diff(snap)
            except Exception:
                log.exception("handle_player_diff failed")
        try:
            await self._update_presence(snap)
        except Exception:
            log.exception("update_presence failed")
        try:
            await self._update_status_message(snap)
        except Exception:
            log.exception("update_status_message failed")
        try:
            await self._update_counter(snap)
        except Exception:
            log.exception("update_counter failed")

    @poll.before_loop
    async def before_poll(self) -> None:
        await self.wait_until_ready()

    def _record_snapshot(self, snap: Snapshot) -> None:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO snapshots (ts, current_players, max_players) VALUES (?, ?, ?)",
                (utc_now_iso(), snap.current_players, snap.max_players),
            )

    async def _handle_player_diff(self, snap: Snapshot) -> None:
        current = {p["UUID"]: p for p in snap.players if "UUID" in p}
        # On first successful poll after startup, accept whoever's online as
        # already-present — start sessions for them but don't announce, since
        # they didn't "just join".
        if not self.bootstrapped:
            now_iso = utc_now_iso()
            with db() as conn:
                for uuid, p in current.items():
                    name = p.get("Name", "?")
                    world = p.get("World")
                    conn.execute(
                        "INSERT INTO players (uuid, name, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
                        (uuid, name, now_iso, now_iso),
                    )
                    conn.execute(
                        "INSERT INTO sessions (uuid, name, world, start) VALUES (?, ?, ?, ?)",
                        (uuid, name, world, now_iso),
                    )
            self.live_uuids = set(current)
            self.bootstrapped = True
            return

        joined = set(current) - self.live_uuids
        left = self.live_uuids - set(current)
        now_iso = utc_now_iso()

        left_session_info: dict[str, sqlite3.Row | None] = {}

        if joined or left:
            with db() as conn:
                for uuid in joined:
                    p = current[uuid]
                    name = p.get("Name", "?")
                    world = p.get("World")
                    conn.execute(
                        "INSERT INTO players (uuid, name, first_seen, last_seen) VALUES (?, ?, ?, ?) "
                        "ON CONFLICT(uuid) DO UPDATE SET name=excluded.name, last_seen=excluded.last_seen",
                        (uuid, name, now_iso, now_iso),
                    )
                    conn.execute(
                        "INSERT INTO sessions (uuid, name, world, start) VALUES (?, ?, ?, ?)",
                        (uuid, name, world, now_iso),
                    )
                for uuid in left:
                    row = conn.execute(
                        "SELECT name, start FROM sessions WHERE uuid = ? AND end IS NULL ORDER BY id DESC LIMIT 1",
                        (uuid,),
                    ).fetchone()
                    left_session_info[uuid] = row
                    conn.execute(
                        "UPDATE sessions SET end = ? WHERE uuid = ? AND end IS NULL",
                        (now_iso, uuid),
                    )
                    conn.execute("UPDATE players SET last_seen = ? WHERE uuid = ?", (now_iso, uuid))

        self.live_uuids = set(current)

        if not ACTIVITY_CHANNEL_ID:
            return
        ch = await self._channel(ACTIVITY_CHANNEL_ID)
        if ch is None:
            return

        for uuid in joined:
            p = current[uuid]
            world = p.get("World")
            await ch.send(embed=discord.Embed(  # type: ignore[union-attr]
                description=f"🟢 **{p.get('Name', '?')}** joined" + (f" — `{world}`" if world else ""),
                color=discord.Color.green(),
                timestamp=utc_now(),
            ))
        for uuid in left:
            row = left_session_info.get(uuid)
            name = row["name"] if row else "?"
            tail = ""
            if row and row["start"]:
                start = parse_iso(row["start"])
                tail = f" — was on for {fmt_duration(utc_now() - start)}"
            await ch.send(embed=discord.Embed(  # type: ignore[union-attr]
                description=f"🔴 **{name}** left{tail}",
                color=discord.Color.red(),
                timestamp=utc_now(),
            ))

    async def _update_presence(self, snap: Snapshot) -> None:
        if not snap.online:
            await self.change_presence(
                status=discord.Status.dnd,
                activity=discord.Activity(type=discord.ActivityType.watching, name="server offline"),
            )
            return
        await self.change_presence(
            status=discord.Status.online,
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name=f"{snap.current_players}/{snap.max_players} on Otter Nonsense",
            ),
        )

    async def _update_status_message(self, snap: Snapshot) -> None:
        ch = await self._channel(STATUS_CHANNEL_ID)
        if ch is None:
            return
        embed = build_status_embed(snap)
        state = load_state()
        msg_id = state.get("status_message_id")

        # Fast path: edit the cached message.
        if msg_id:
            try:
                msg = await ch.fetch_message(int(msg_id))  # type: ignore[union-attr]
                await msg.edit(embed=embed)
                return
            except (discord.NotFound, discord.Forbidden):
                log.info("cached status message id %s unusable, self-healing", msg_id)

        # Self-heal: find any status message this bot already pinned in the
        # channel. If we find one, edit the newest, delete older duplicates,
        # and refresh state.json. Survives lost state.json or message_id drift.
        own_id = self.user.id if self.user else None
        own_pins: list[discord.Message] = []
        if own_id is not None:
            try:
                pins = await ch.pins()  # type: ignore[union-attr]
                own_pins = [m for m in pins if m.author and m.author.id == own_id]
            except discord.HTTPException as e:
                log.warning("could not list pins for status channel: %s", e)

        if own_pins:
            own_pins.sort(key=lambda m: m.created_at, reverse=True)
            keeper = own_pins[0]
            try:
                await keeper.edit(embed=embed)
                state["status_message_id"] = keeper.id
                save_state(state)
                for stale in own_pins[1:]:
                    try:
                        await stale.delete()
                    except discord.HTTPException:
                        pass
                return
            except discord.HTTPException as e:
                log.warning("failed to edit reclaimed status message: %s", e)

        # No existing message to reuse — post a fresh one.
        msg = await ch.send(embed=embed)  # type: ignore[union-attr]
        try:
            await msg.pin(reason="Otter Nonsense status")
        except discord.Forbidden:
            log.warning("missing manage_messages perm; cannot pin status")
        state["status_message_id"] = msg.id
        save_state(state)

    async def _update_counter(self, snap: Snapshot) -> None:
        if not COUNTER_VOICE_CHANNEL_ID:
            return
        text = (
            COUNTER_TEMPLATE.format(current=snap.current_players, max=snap.max_players)
            if snap.online else COUNTER_OFFLINE_TEMPLATE
        )
        if text == self.last_counter_text:
            return
        if self.last_counter_edit is not None:
            since = (utc_now() - self.last_counter_edit).total_seconds()
            if since < COUNTER_MIN_INTERVAL_S:
                return
        ch = await self._channel(COUNTER_VOICE_CHANNEL_ID)
        if ch is None:
            return
        try:
            await ch.edit(name=text, reason="Otter Nonsense live counter")  # type: ignore[union-attr]
            self.last_counter_text = text
            self.last_counter_edit = utc_now()
        except discord.Forbidden:
            log.warning("missing manage_channels perm on counter channel")
        except discord.HTTPException as e:
            log.warning("counter rename failed: %s", e)

    @tasks.loop(time=time(hour=RECAP_HOUR, minute=0, tzinfo=RECAP_TZ))
    async def daily_recap(self) -> None:
        if not RECAP_CHANNEL_ID:
            return
        ch = await self._channel(RECAP_CHANNEL_ID)
        if ch is None:
            return
        s = recap_window_stats()
        embed = discord.Embed(
            title=f"📊 {s['date_label']} on Otter Nonsense",
            color=discord.Color.blurple(),
            timestamp=utc_now(),
        )
        embed.add_field(name="Peak players", value=str(s["peak"]), inline=True)
        embed.add_field(name="Unique players", value=str(s["unique"]), inline=True)
        embed.add_field(name="Player-hours", value=f"{s['play_hours']:.1f}", inline=True)
        if s["names"]:
            shown = ", ".join(s["names"][:30])
            if len(s["names"]) > 30:
                shown += f", and {len(s['names']) - 30} more"
            embed.add_field(name="Who played", value=shown, inline=False)
        else:
            embed.description = "Quiet day — nobody logged on."
        await ch.send(embed=embed)  # type: ignore[union-attr]

    @daily_recap.before_loop
    async def before_recap(self) -> None:
        await self.wait_until_ready()


bot = OtterBot()


@bot.tree.command(name="status", description="Show current Otter Nonsense Hytale server status.")
async def cmd_status(interaction: discord.Interaction) -> None:
    snap = bot.latest
    if snap is None:
        assert bot.http_client
        snap = await fetch_snapshot(bot.http_client)
    await interaction.response.send_message(embed=build_status_embed(snap), ephemeral=True)


@bot.tree.command(name="players", description="List players currently online.")
async def cmd_players(interaction: discord.Interaction) -> None:
    snap = bot.latest
    if snap is None:
        assert bot.http_client
        snap = await fetch_snapshot(bot.http_client)
    if not snap.online:
        await interaction.response.send_message("🔴 Server is offline.", ephemeral=True)
        return
    if not snap.players:
        await interaction.response.send_message(
            f"No one online right now ({snap.current_players}/{snap.max_players}).",
            ephemeral=True,
        )
        return
    lines = [f"**{snap.current_players}/{snap.max_players} online**"]
    for p in snap.players:
        lines.append(f"• {p.get('Name', '?')} — `{p.get('World', '?')}`")
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


@bot.tree.command(name="serverinfo", description="Show server address, version and world.")
async def cmd_serverinfo(interaction: discord.Interaction) -> None:
    snap = bot.latest
    if snap is None:
        assert bot.http_client
        snap = await fetch_snapshot(bot.http_client)
    if not snap.online:
        await interaction.response.send_message("🔴 Server is offline.", ephemeral=True)
        return
    msg = (
        f"**{snap.name}**\n"
        f"Address: `{snap.address}`\n"
        f"Version: `{snap.version}`\n"
        f"Default world: `{snap.world}`\n"
        f"Slots: {snap.max_players}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


@bot.tree.command(name="profile", description="Show a player's history on Otter Nonsense.")
@app_commands.describe(name="In-game player name (case-insensitive)")
async def cmd_profile(interaction: discord.Interaction, name: str) -> None:
    row = lookup_player(name)
    if row is None:
        await interaction.response.send_message(
            f"No record of a player named `{name}`. Names are tracked once they join after the bot starts watching.",
            ephemeral=True,
        )
        return
    s = player_stats(row["uuid"])
    first = parse_iso(row["first_seen"])
    last = parse_iso(row["last_seen"])
    is_online = row["uuid"] in bot.live_uuids
    embed = discord.Embed(
        title=f"👤 {row['name']}",
        color=discord.Color.green() if is_online else discord.Color.greyple(),
        timestamp=utc_now(),
    )
    embed.add_field(name="Status", value="🟢 Online now" if is_online else "Offline", inline=True)
    embed.add_field(name="First seen", value=f"<t:{int(first.timestamp())}:R>", inline=True)
    embed.add_field(name="Last seen", value=f"<t:{int(last.timestamp())}:R>", inline=True)
    embed.add_field(name="Sessions", value=str(s["session_count"]), inline=True)
    embed.add_field(name="Total time", value=fmt_duration(s["total"]), inline=True)
    embed.add_field(name="Longest session", value=fmt_duration(s["longest"]), inline=True)
    embed.set_footer(text=f"UUID {row['uuid']}")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="stats", description="Server-wide stats for Otter Nonsense.")
async def cmd_stats(interaction: discord.Interaction) -> None:
    s = server_stats()
    snap = bot.latest
    online_now = snap.current_players if snap and snap.online else 0
    max_now = snap.max_players if snap and snap.online else 0
    embed = discord.Embed(
        title="📊 Otter Nonsense — server stats",
        color=discord.Color.blurple(),
        timestamp=utc_now(),
    )
    embed.add_field(name="Online now", value=f"{online_now}/{max_now}", inline=True)
    embed.add_field(name="Peak today", value=str(s["peak_today"]), inline=True)
    embed.add_field(name="Peak this week", value=str(s["peak_week"]), inline=True)
    embed.add_field(name="All-time peak", value=str(s["peak_all"]), inline=True)
    embed.add_field(name="Unique players ever", value=str(s["unique_total"]), inline=True)
    embed.add_field(name="Total play-hours", value=f"{s['total_play_hours']:.1f}", inline=True)
    await interaction.response.send_message(embed=embed)


if __name__ == "__main__":
    init_db()
    bot.run(DISCORD_TOKEN, log_handler=None)
