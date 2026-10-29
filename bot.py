"""
RAJ MUSIC PRO — Ultimate Professional Edition
Fast. Reliable. Feature-complete. No cookies.
"""

import os
import asyncio
import json
import logging
import math
import random
import re
import signal
import time
from collections import deque, OrderedDict
from functools import lru_cache
from pathlib import Path
from typing import Optional

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import yt_dlp

load_dotenv()

# =============================================================================
# CONFIG
# =============================================================================
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID", "").strip()
AUTO_DISCONNECT_MINUTES = int(os.getenv("AUTO_DISCONNECT_MINUTES", "5"))
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}
SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID", "")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET", "")
DATA_DIR = Path(os.getenv("DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

# =============================================================================
# LOGGING
# =============================================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
)
log = logging.getLogger("RajMusicPro")
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("yt_dlp").setLevel(logging.WARNING)

# =============================================================================
# COLORS
# =============================================================================
CYAN = 0x06B6D4
RED = 0xEF4444
GREEN = 0x22C55E
PURPLE = 0x8B5CF6
YELLOW = 0xF59E0B
GRAY = 0x6B7280
PINK = 0xEC4899

# =============================================================================
# BOT
# =============================================================================
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

FFMPEG_BEFORE = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin"

# Filter presets — FFmpeg audio filters
FILTERS = {
    "off":       "",
    "bassboost": "bass=g=15",
    "nightcore": "asetrate=48000*1.25,aresample=48000,atempo=1.0",
    "vaporwave": "asetrate=48000*0.8,aresample=48000,atempo=1.0",
    "8d":        "apulsator=hz=0.08",
    "karaoke":   "pan=stereo|c0=c0|c1=-1*c1",
    "treble":    "treble=g=10",
    "loud":      "loudnorm=I=-16:TP=-1.5:LRA=11",
    "soft":      "volume=0.6",
}

# =============================================================================
# COOKIE-FREE yt-dlp CONFIG
# =============================================================================
def build_ydl_opts():
    return {
        "format": "bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "default_search": "ytsearch1:",
        "socket_timeout": 20,
        "retries": 3,
        "fragment_retries": 3,
        "extractor_retries": 2,
        "file_access_retries": 2,
        "source_address": "0.0.0.0",
        "cachedir": False,
        "geo_bypass": True,
        "nocheckcertificate": True,
        "extract_flat": False,
        "concurrent_fragment_downloads": 3,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
        "extractor_args": {
            "youtube": {
                "player_client": ["android", "ios", "tv_embedded", "web_safari", "mweb"],
                "skip": ["hls", "dash"],
            },
        },
    }

YDL_OPTIONS = build_ydl_opts()

# =============================================================================
# GLOBAL STATE
# =============================================================================
START_TIME = time.time()
STATS = {"tracks_played": 0, "searches": 0, "cache_hits": 0, "errors": 0}
CIRCUIT = {"failures": 0, "open_until": 0.0}

search_cache: "OrderedDict[str, dict]" = OrderedDict()
url_cache:    "OrderedDict[str, tuple[str, float]]" = OrderedDict()
SEARCH_CACHE_MAX = 100
URL_CACHE_TTL = 5 * 3600  # 5 hours

players: dict[int, dict] = {}

http_session: Optional[aiohttp.ClientSession] = None
spotify_token: Optional[dict] = None


# =============================================================================
# HELPERS
# =============================================================================
def make_embed(title, description="", color=CYAN):
    e = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    e.set_footer(text="RAJ MUSIC PRO • Ultimate Edition")
    return e


def duration_text(seconds) -> str:
    if seconds is None:
        return "🔴 LIVE"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02}:{sec:02}" if h else f"{m}:{sec:02}"


def progress_bar(elapsed, total, length=20):
    if not total:
        return "🔴 Live stream"
    pct = min(1.0, elapsed / total)
    filled = int(pct * length)
    bar = "█" * filled + "░" * (length - filled)
    return f"{bar} {duration_text(elapsed)} / {duration_text(total)}"


def get_player(guild_id):
    if guild_id not in players:
        players[guild_id] = {
            "queue": deque(),
            "current": None,
            "loop": "off",
            "volume": 0.5,
            "text_channel": None,
            "lock": asyncio.Lock(),
            "suppress_end": False,
            "skip_requested": False,
            "autoplay": False,
            "last_track": None,
            "history": deque(maxlen=25),
            "seek_seconds": 0,
            "idle_since": time.time(),
            "vote_skip": set(),
            "started_at": 0.0,
            "filter": "off",
            "speed": 1.0,
            "stay_247": False,
            "dj_role": None,
            "prefetch": None,
            "prefetch_task": None,
            "track_duration": 0,
            "paused_at": 0.0,
        }
    return players[guild_id]


def get_text_channel(player):
    cid = player["text_channel"]
    return bot.get_channel(cid) if cid else None


async def safe_send(channel, **kwargs):
    if not channel:
        return None
    try:
        return await channel.send(**kwargs)
    except discord.HTTPException:
        return None


def cache_search_put(key, value):
    search_cache[key] = value
    search_cache.move_to_end(key)
    while len(search_cache) > SEARCH_CACHE_MAX:
        search_cache.popitem(last=False)


def cache_url_put(key, url):
    url_cache[key] = (url, time.time())
    url_cache.move_to_end(key)
    while len(url_cache) > 200:
        url_cache.popitem(last=False)


def cache_url_get(key):
    entry = url_cache.get(key)
    if not entry:
        return None
    url, ts = entry
    if time.time() - ts > URL_CACHE_TTL:
        url_cache.pop(key, None)
        return None
    return url


def circuit_check():
    if CIRCUIT["open_until"] > time.time():
        raise RuntimeError("YouTube temporarily unavailable — retrying soon.")
    return True


def circuit_fail():
    CIRCUIT["failures"] += 1
    if CIRCUIT["failures"] >= 3:
        CIRCUIT["open_until"] = time.time() + 60
        CIRCUIT["failures"] = 0
        log.warning("Circuit opened for 60s after repeated failures")


def circuit_ok():
    CIRCUIT["failures"] = 0
    CIRCUIT["open_until"] = 0.0


# =============================================================================
# PERSISTENCE
# =============================================================================
def _save_path(guild_id):
    return DATA_DIR / f"guild_{guild_id}.json"


def save_guild_state(guild_id):
    p = players.get(guild_id)
    if not p:
        return
    try:
        data = {
            "volume": p["volume"],
            "loop": p["loop"],
            "autoplay": p["autoplay"],
            "filter": p["filter"],
            "speed": p["speed"],
            "stay_247": p["stay_247"],
            "dj_role": p["dj_role"],
            "queue": [
                {k: t.get(k) for k in ("title", "webpage_url", "duration", "thumbnail", "requester", "channel")}
                for t in p["queue"]
            ],
        }
        _save_path(guild_id).write_text(json.dumps(data, indent=2))
    except Exception:
        log.debug("save_guild_state failed", exc_info=True)


def load_guild_state(guild_id):
    path = _save_path(guild_id)
    if not path.exists():
        return
    try:
        data = json.loads(path.read_text())
        p = get_player(guild_id)
        p["volume"] = data.get("volume", 0.5)
        p["loop"] = data.get("loop", "off")
        p["autoplay"] = data.get("autoplay", False)
        p["filter"] = data.get("filter", "off")
        p["speed"] = data.get("speed", 1.0)
        p["stay_247"] = data.get("stay_247", False)
        p["dj_role"] = data.get("dj_role")
        for t in data.get("queue", []):
            p["queue"].append({**t, "url": None})
    except Exception:
        log.debug("load_guild_state failed", exc_info=True)


# =============================================================================
# HTTP SESSION
# =============================================================================
async def get_http():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": YDL_OPTIONS["http_headers"]["User-Agent"]},
        )
    return http_session


# =============================================================================
# SEARCH
# =============================================================================
async def search_track(query, requester, *, use_cache=True):
    key = query.strip().lower()

    if use_cache and key in search_cache:
        STATS["cache_hits"] += 1
        cached = dict(search_cache[key])
        cached["requester"] = str(requester)
        return cached

    loop = asyncio.get_running_loop()
    STATS["searches"] += 1

    def extract():
        circuit_check()
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(query, download=False)
        if info and "entries" in info:
            info = next((e for e in info["entries"] if e), None)
        if not info or not info.get("url"):
            raise ValueError("No playable audio found.")
        return {
            "title": info.get("title", "Unknown title"),
            "url": info.get("url"),
            "webpage_url": info.get("webpage_url") or info.get("original_url") or query,
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "requester": str(requester),
            "channel": info.get("uploader"),
            "views": info.get("view_count"),
        }

    try:
        track = await loop.run_in_executor(None, extract)
        circuit_ok()
    except Exception:
        circuit_fail()
        raise

    cache_search_put(key, {k: v for k, v in track.items() if k != "requester"})
    cache_url_put(track["webpage_url"], track["url"])
    return track


async def refresh_stream_url(track):
    cached = cache_url_get(track["webpage_url"])
    if cached:
        return cached

    loop = asyncio.get_running_loop()

    def extract():
        circuit_check()
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(track["webpage_url"], download=False)
        if info and "entries" in info:
            info = next((e for e in info["entries"] if e), None)
        if not info or not info.get("url"):
            raise ValueError("No playable stream.")
        return info["url"]

    try:
        url = await loop.run_in_executor(None, extract)
        circuit_ok()
        cache_url_put(track["webpage_url"], url)
        return url
    except Exception:
        circuit_fail()
        raise


async def search_multi(query, limit=5):
    loop = asyncio.get_running_loop()
    opts = dict(YDL_OPTIONS)
    opts["default_search"] = f"ytsearch{limit}:"

    def extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        results = []
        for entry in (info.get("entries") or []):
            if entry:
                results.append({
                    "title": entry.get("title"),
                    "webpage_url": entry.get("webpage_url"),
                    "duration": entry.get("duration"),
                    "thumbnail": entry.get("thumbnail"),
                    "channel": entry.get("uploader"),
                })
        return results

    return await loop.run_in_executor(None, extract)


# =============================================================================
# LYRICS
# =============================================================================
async def fetch_lyrics(query: str):
    try:
        s = await get_http()
        async with s.get("https://lrclib.net/api/search", params={"q": query}) as r:
            if r.status != 200:
                return None
            data = await r.json()
            return data[0].get("plainLyrics") if data else None
    except Exception:
        return None


# =============================================================================
# SPOTIFY
# =============================================================================
async def spotify_auth():
    global spotify_token
    if not SPOTIFY_CLIENT_ID or not SPOTIFY_CLIENT_SECRET:
        return None
    if spotify_token and spotify_token["expires"] > time.time():
        return spotify_token["token"]

    try:
        s = await get_http()
        async with s.post(
            "https://accounts.spotify.com/api/token",
            data={"grant_type": "client_credentials"},
            auth=aiohttp.BasicAuth(SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET),
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            spotify_token = {"token": data["access_token"], "expires": time.time() + 3500}
            return data["access_token"]
    except Exception:
        return None


async def spotify_resolve(url: str):
    token = await spotify_auth()
    if not token:
        return None
    try:
        s = await get_http()
        track_id = re.search(r"track/([a-zA-Z0-9]+)", url)
        if not track_id:
            return None
        async with s.get(
            f"https://api.spotify.com/v1/tracks/{track_id.group(1)}",
            headers={"Authorization": f"Bearer {token}"},
        ) as r:
            if r.status != 200:
                return None
            data = await r.json()
            artists = " ".join(a["name"] for a in data["artists"])
            return f"{artists} {data['name']}"
    except Exception:
        return None


# =============================================================================
# EMBEDS
# =============================================================================
def track_embed(track, title="🎶 Now Playing"):
    e = make_embed(
        title,
        f"### [{track['title']}]({track['webpage_url']})\n\n"
        f"⏱️ **{duration_text(track['duration'])}** • "
        f"👤 **{track['requester']}**",
        GREEN,
    )
    if track.get("thumbnail"):
        e.set_thumbnail(url=track["thumbnail"])
    if track.get("channel"):
        e.add_field(name="🎤 Channel", value=track["channel"], inline=True)
    if track.get("views"):
        e.add_field(name="👀 Views", value=f"{track['views']:,}", inline=True)
    return e


def dashboard_embed(guild):
    p = get_player(guild.id)
    track = p["current"]
    vc = guild.voice_client
    if track:
        elapsed = time.time() - p["started_at"] if p["started_at"] else 0
        status = "⏸️ Paused" if vc and vc.is_paused() else "▶️ Playing"
        e = make_embed("🎧 RAJ MUSIC PRO • LIVE PLAYER", color=CYAN)
        e.description = (
            f"### [{track['title']}]({track['webpage_url']})\n"
            f"{progress_bar(elapsed, p['track_duration'])}\n\n"
            f"**{status}** • **Vol** `{round(p['volume'] * 100)}%` • "
            f"**Loop** `{p['loop'].title()}` • **Filter** `{p['filter']}`\n"
            f"**Up next:** `{len(p['queue'])}` • **Autoplay:** `{'On' if p['autoplay'] else 'Off'}`"
        )
        if track.get("thumbnail"):
            e.set_thumbnail(url=track["thumbnail"])
        e.set_footer(text="RAJ MUSIC PRO • Ultimate Edition")
        return e
    return make_embed(
        "🎧 RAJ MUSIC PRO • DASHBOARD",
        "**Ready to play.** Use `/play <song>` to start.\n\n"
        "Use the buttons below to control playback, queue, and filters.",
        PURPLE,
    )


# =============================================================================
# DASHBOARD VIEW
# =============================================================================
class MusicDashboard(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction):
        if not interaction.guild:
            await interaction.response.send_message("Server only.", ephemeral=True)
            return False
        return True

    async def _reply(self, interaction, msg, embed=None):
        if not interaction.response.is_done():
            await interaction.response.send_message(msg, embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(msg, embed=embed, ephemeral=True)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary,
                       custom_id="raj:pause_resume", row=0)
    async def pause_resume(self, interaction, button):
        vc = interaction.guild.voice_client
        if not vc:
            return await self._reply(interaction, "Not connected.")
        if vc.is_playing():
            vc.pause()
            button.label, button.emoji = "Resume", "▶️"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("⏸️ Paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            button.label, button.emoji = "Pause", "⏸️"
            await interaction.response.edit_message(view=self)
            await interaction.followup.send("▶️ Resumed.", ephemeral=True)
        else:
            await self._reply(interaction, "Nothing playing.")

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary,
                       custom_id="raj:skip", row=0)
    async def skip_btn(self, interaction, button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            get_player(interaction.guild.id)["skip_requested"] = True
            vc.stop()
            await self._reply(interaction, "⏭️ Skipped.")
        else:
            await self._reply(interaction, "Nothing playing.")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger,
                       custom_id="raj:stop", row=0)
    async def stop_btn(self, interaction, button):
        p = get_player(interaction.guild.id)
        p["queue"].clear()
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            p["suppress_end"] = True
            vc.stop()
        p["current"] = None
        await self._reply(interaction, "⏹️ Stopped & queue cleared.")

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary,
                       custom_id="raj:queue", row=0)
    async def queue_btn(self, interaction, button):
        await send_queue_page(interaction, interaction.guild.id, 0, ephemeral=True)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary,
                       custom_id="raj:shuffle", row=1)
    async def shuffle_btn(self, interaction, button):
        p = get_player(interaction.guild.id)
        tracks = list(p["queue"])
        random.shuffle(tracks)
        p["queue"] = deque(tracks)
        await self._reply(interaction, f"🔀 Shuffled **{len(tracks)}** tracks.")

    @discord.ui.button(label="Loop: Off", emoji="🔁", style=discord.ButtonStyle.secondary,
                       custom_id="raj:loop", row=1)
    async def loop_btn(self, interaction, button):
        p = get_player(interaction.guild.id)
        modes = ["off", "song", "queue"]
        p["loop"] = modes[(modes.index(p["loop"]) + 1) % 3]
        button.label = f"Loop: {p['loop'].title()}"
        await interaction.response.edit_message(view=self)
        save_guild_state(interaction.guild.id)

    @discord.ui.button(label="Vol −", emoji="🔉", style=discord.ButtonStyle.secondary,
                       custom_id="raj:vol_down", row=1)
    async def vol_down(self, interaction, button):
        await self._vol(interaction, -10)

    @discord.ui.button(label="Vol +", emoji="🔊", style=discord.ButtonStyle.secondary,
                       custom_id="raj:vol_up", row=1)
    async def vol_up(self, interaction, button):
        await self._vol(interaction, 10)

    @discord.ui.button(label="Autoplay: Off", emoji="♾️", style=discord.ButtonStyle.secondary,
                       custom_id="raj:autoplay", row=2)
    async def autoplay_btn(self, interaction, button):
        p = get_player(interaction.guild.id)
        p["autoplay"] = not p["autoplay"]
        button.label = f"Autoplay: {'On' if p['autoplay'] else 'Off'}"
        await interaction.response.edit_message(view=self)
        save_guild_state(interaction.guild.id)

    @discord.ui.button(label="History", emoji="📚", style=discord.ButtonStyle.secondary,
                       custom_id="raj:history", row=2)
    async def history_btn(self, interaction, button):
        p = get_player(interaction.guild.id)
        if not p["history"]:
            return await self._reply(interaction, "No history yet.")
        lines = [
            f"`{i}.` [{t['title']}]({t['webpage_url']})"
            for i, t in enumerate(list(p["history"])[:10], 1)
        ]
        await self._reply(interaction, "", embed=make_embed("📚 Recently Played", "\n".join(lines), PURPLE))

    async def _vol(self, interaction, delta):
        p = get_player(interaction.guild.id)
        lvl = max(0, min(100, round(p["volume"] * 100) + delta))
        p["volume"] = lvl / 100
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = p["volume"]
        await self._reply(interaction, f"🔊 Volume: **{lvl}%**")
        save_guild_state(interaction.guild.id)


# =============================================================================
# QUEUE PAGINATION
# =============================================================================
class QueuePaginator(discord.ui.View):
    def __init__(self, guild_id, page=0):
        super().__init__(timeout=120)
        self.guild_id = guild_id
        self.page = page

    async def interaction_check(self, interaction):
        return True

    def build_embed(self):
        p = get_player(self.guild_id)
        tracks = list(p["queue"])
        per_page = 10
        max_page = max(0, (len(tracks) - 1) // per_page)
        self.page = min(self.page, max_page)
        start = self.page * per_page
        chunk = tracks[start:start + per_page]

        lines = []
        if p["current"] and self.page == 0:
            t = p["current"]
            lines.append(f"🎶 **Now:** [{t['title']}]({t['webpage_url']}) `[{duration_text(t['duration'])}]`")
            lines.append("")

        for i, t in enumerate(chunk, start + 1):
            lines.append(
                f"`{i:>2}.` [{t['title']}]({t['webpage_url']}) `[{duration_text(t['duration'])}]`"
            )

        if not tracks and not p["current"]:
            lines = ["*Queue is empty.*"]

        total_sec = sum(int(t.get("duration") or 0) for t in tracks)
        e = make_embed(
            f"📜 Queue — Page {self.page + 1}/{max_page + 1}",
            "\n".join(lines),
            PURPLE,
        )
        e.add_field(name="Waiting", value=f"`{len(tracks)}`", inline=True)
        e.add_field(name="Total time", value=f"`{duration_text(total_sec)}`", inline=True)
        e.add_field(name="Loop", value=f"`{p['loop'].title()}`", inline=True)
        return e

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction, button):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, button):
        self.page += 1
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    @discord.ui.button(label="Close", emoji="✖️", style=discord.ButtonStyle.danger)
    async def close(self, interaction, button):
        await interaction.message.delete()


async def send_queue_page(interaction, guild_id, page=0, ephemeral=False):
    view = QueuePaginator(guild_id, page)
    embed = view.build_embed()
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=ephemeral)


# =============================================================================
# PLAYBACK ENGINE
# =============================================================================
def _build_ffmpeg_opts(p, seek=0):
    parts = [FFMPEG_BEFORE]
    if seek > 0:
        parts.append(f"-ss {seek}")
    filt = FILTERS.get(p["filter"], "")
    if p["speed"] != 1.0:
        filt = (filt + "," if filt else "") + f"atempo={p['speed']}"
    if filt:
        parts.append(f"-af \"{filt}\"")
    return " ".join(parts)


async def prefetch_next(guild_id):
    """Resolve the next track's stream URL in the background."""
    p = players.get(guild_id)
    if not p or not p["queue"]:
        return
    nxt = p["queue"][0]
    if cache_url_get(nxt["webpage_url"]):
        return
    try:
        url = await refresh_stream_url(nxt)
        nxt["url"] = url
    except Exception:
        log.debug("prefetch failed", exc_info=True)


async def start_next(guild):
    p = get_player(guild.id)

    async with p["lock"]:
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            p["current"] = None
            return

        if vc.is_playing() or vc.is_paused():
            return

        if not p["queue"]:
            if p["autoplay"] and p.get("last_track"):
                prev = p["last_track"]
                try:
                    q = f"{prev.get('channel') or ''} {prev['title']} mix".strip()
                    related = await search_track(q, "🎵 Autoplay")
                    if related.get("webpage_url") != prev.get("webpage_url"):
                        p["queue"].append(related)
                except Exception:
                    log.exception("Autoplay failed")

            if not p["queue"]:
                p["current"] = None
                p["idle_since"] = time.time()
                ch = get_text_channel(p)
                await safe_send(
                    ch,
                    embed=make_embed(
                        "📭 Queue Finished",
                        "The queue is empty. Use `/play` to add more.\n"
                        "Enable `/autoplay` or `/247` to keep the party going.",
                        PURPLE,
                    ),
                )
                return

        track = p["queue"].popleft()
        p["current"] = track
        p["track_duration"] = int(track.get("duration") or 0)

        try:
            fresh_url = track.get("url") if cache_url_get(track["webpage_url"]) else None
            if not fresh_url:
                fresh_url = await refresh_stream_url(track)
            track["url"] = fresh_url

            seek = p.get("seek_seconds", 0)
            before = _build_ffmpeg_opts(p, seek)
            audio = discord.FFmpegPCMAudio(fresh_url, before_options=before, options="-vn")
            source = discord.PCMVolumeTransformer(audio, volume=p["volume"])
            p["seek_seconds"] = 0
            p["started_at"] = time.time()

            def after_play(error, _gid=guild.id, _t=track):
                if error:
                    log.error("Playback error: %s", error)
                    STATS["errors"] += 1
                if bot.loop.is_closed():
                    return
                asyncio.run_coroutine_threadsafe(
                    track_finished(_gid, _t, error), bot.loop
                )

            vc.play(source, after=after_play)
            STATS["tracks_played"] += 1
            p["history"].appendleft(track)
            save_guild_state(guild.id)

            ch = get_text_channel(p)
            await safe_send(ch, embed=track_embed(track), view=MusicDashboard())

            # Pre-fetch next track URL in background for instant transition
            if p["queue"]:
                if p["prefetch_task"] and not p["prefetch_task"].done():
                    p["prefetch_task"].cancel()
                p["prefetch_task"] = asyncio.create_task(prefetch_next(guild.id))

        except Exception:
            log.exception("Failed to start track")
            STATS["errors"] += 1
            p["current"] = None
            asyncio.create_task(start_next(guild))


async def track_finished(guild_id, finished_track, error=None):
    p = get_player(guild_id)
    async with p["lock"]:
        if p["suppress_end"]:
            p["suppress_end"] = False
            p["current"] = None
            p["skip_requested"] = False
            return

        skipped = p["skip_requested"]
        p["skip_requested"] = False
        p["last_track"] = p.get("current") or finished_track

        if not skipped and p["loop"] == "song" and p["current"] is finished_track:
            p["queue"].appendleft(finished_track)
        elif p["loop"] == "queue" and not skipped:
            p["queue"].append(finished_track)

        p["current"] = None
        p["vote_skip"] = set()

    if error:
        ch = get_text_channel(p)
        await safe_send(
            ch,
            embed=make_embed("⚠️ Playback Error", "Skipping to next track...", RED),
        )

    guild = bot.get_guild(guild_id)
    if guild:
        await start_next(guild)


async def require_voice(interaction):
    if not interaction.guild:
        await interaction.followup.send("Server only.", ephemeral=True)
        return None
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice:
        await interaction.followup.send("Join a voice channel first.", ephemeral=True)
        return None
    channel = member.voice.channel
    vc = interaction.guild.voice_client
    try:
        if vc is None:
            vc = await channel.connect(self_deaf=True)
        elif vc.channel != channel:
            await vc.move_to(channel)
    except Exception as e:
        log.exception("Voice join failed")
        await interaction.followup.send(f"Could not join voice: `{type(e).__name__}`", ephemeral=True)
        return None
    p = get_player(interaction.guild.id)
    p["text_channel"] = interaction.channel_id
    p["suppress_end"] = False
    p["skip_requested"] = False
    return vc


def is_dj(interaction):
    p = get_player(interaction.guild.id)
    role_id = p.get("dj_role")
    if not role_id:
        return True
    if isinstance(interaction.user, discord.Member):
        if interaction.user.guild_permissions.manage_guild:
            return True
        return any(r.id == role_id for r in interaction.user.roles)
    return False


async def dj_guard(interaction):
    if not is_dj(interaction):
        await interaction.followup.send(
            "🚫 DJ role required for this action.", ephemeral=True
        )
        return False
    return True


# =============================================================================
# EVENTS
# =============================================================================
@bot.event
async def on_ready():
    if not getattr(bot, "_music_view_registered", False):
        bot.add_view(MusicDashboard())
        bot._music_view_registered = True

    try:
        if GUILD_ID.isdigit():
            g = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=g)
            synced = await bot.tree.sync(guild=g)
            log.info("Synced %s commands to guild %s", len(synced), GUILD_ID)
        else:
            synced = await bot.tree.sync()
            log.info("Synced %s global commands", len(synced))
    except Exception:
        log.exception("Sync failed")

    if not idle_watcher.is_running():
        idle_watcher.start()
    if not progress_updater.is_running():
        progress_updater.start()

    log.info("✅ RAJ MUSIC PRO online as %s", bot.user)


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id or not member.guild:
        return
    if before.channel and after.channel is None:
        p = get_player(member.guild.id)
        async with p["lock"]:
            p["current"] = None
            p["queue"].clear()
            p["suppress_end"] = True
            p["skip_requested"] = False
            p["last_track"] = None


@tasks.loop(minutes=1)
async def idle_watcher():
    if AUTO_DISCONNECT_MINUTES <= 0:
        return
    now = time.time()
    for gid, p in list(players.items()):
        if p.get("stay_247"):
            continue
        guild = bot.get_guild(gid)
        if not guild:
            continue
        vc = guild.voice_client
        if not vc or not vc.is_connected():
            continue
        if vc.is_playing() or vc.is_paused() or p["queue"]:
            p["idle_since"] = now
            continue
        if now - p["idle_since"] >= AUTO_DISCONNECT_MINUTES * 60:
            log.info("Auto-disconnect guild %s", gid)
            p["suppress_end"] = True
            try:
                await vc.disconnect(force=True)
            except Exception:
                pass
            p["current"] = None
            p["queue"].clear()


@tasks.loop(seconds=15)
async def progress_updater():
    """Update the live now-playing message every 15s."""
    for gid, p in list(players.items()):
        if not p.get("current") or not p.get("live_msg"):
            continue
        guild = bot.get_guild(gid)
        if not guild:
            continue
        vc = guild.voice_client
        if not vc or not vc.is_playing():
            continue
        try:
            elapsed = time.time() - p["started_at"]
            e = make_embed("🎧 NOW PLAYING", color=CYAN)
            t = p["current"]
            e.description = (
                f"### [{t['title']}]({t['webpage_url']})\n"
                f"{progress_bar(elapsed, p['track_duration'])}\n\n"
                f"👤 **{t['requester']}**"
            )
            if t.get("thumbnail"):
                e.set_thumbnail(url=t["thumbnail"])
            await p["live_msg"].edit(embed=e)
        except Exception:
            pass


async def shutdown():
    log.info("Shutting down gracefully...")
    for gid in list(players.keys()):
        save_guild_state(gid)
    if http_session and not http_session.closed:
        await http_session.close()
    await bot.close()


def _on_signal():
    asyncio.run_coroutine_threadsafe(shutdown(), bot.loop)


# =============================================================================
# COMMANDS — PLAYBACK
# =============================================================================
@bot.tree.command(name="play", description="Play a song (search or URL)")
@app_commands.describe(query="Song name, YouTube URL, or Spotify URL")
async def play(interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await require_voice(interaction)
    if not vc:
        return
    p = get_player(interaction.guild.id)

    if "open.spotify.com" in query:
        resolved = await spotify_resolve(query)
        if resolved:
            query = resolved

    try:
        track = await search_track(query, interaction.user)
    except Exception as e:
        log.warning("Search failed: %s", e)
        return await interaction.followup.send(
            embed=make_embed("❌ Search Failed", "Could not find audio. Try again.", RED)
        )

    if p["current"] or vc.is_playing() or vc.is_paused():
        if len(p["queue"]) >= 100:
            return await interaction.followup.send("Queue full (100).", ephemeral=True)
        p["queue"].append(track)
        save_guild_state(interaction.guild.id)
        e = track_embed(track, "➕ Added to Queue")
        e.description += f"\n\n📌 Position: `{len(p['queue'])}`"
        return await interaction.followup.send(embed=e)

    p["queue"].append(track)
    await interaction.followup.send(embed=make_embed("🔎 Found", "Starting...", GREEN))
    await start_next(interaction.guild)


@bot.tree.command(name="search", description="Search and pick from 5 results")
@app_commands.describe(query="Search query")
async def search_cmd(interaction, query: str):
    await interaction.response.defer()
    vc = await require_voice(interaction)
    if not vc:
        return
    try:
        results = await search_multi(query, 5)
    except Exception:
        return await interaction.followup.send("Search failed.", ephemeral=True)

    if not results:
        return await interaction.followup.send("No results.", ephemeral=True)

    view = SearchPicker(results, interaction.user.id, interaction.guild.id)
    e = make_embed("🔎 Search Results", "Pick a track to add:", CYAN)
    for i, r in enumerate(results, 1):
        e.add_field(
            name=f"{i}. {r['title'][:60]}",
            value=f"`{duration_text(r['duration'])}` • {r.get('channel') or '—'}",
            inline=False,
        )
    await interaction.followup.send(embed=e, view=view)


class SearchPicker(discord.ui.View):
    def __init__(self, results, user_id, guild_id):
        super().__init__(timeout=60)
        self.results = results
        self.user_id = user_id
        self.guild_id = guild_id
        for i in range(len(results)):
            self.add_item(self.make_button(i))

    def make_button(self, i):
        btn = discord.ui.Button(label=str(i + 1), style=discord.ButtonStyle.primary)

        async def cb(interaction):
            if interaction.user.id != self.user_id:
                return await interaction.response.send_message("Not your search.", ephemeral=True)
            track = await search_track(self.results[i]["webpage_url"], interaction.user)
            p = get_player(self.guild_id)
            p["queue"].append(track)
            vc = interaction.guild.voice_client
            await interaction.response.send_message(
                embed=track_embed(track, "➕ Queued"), ephemeral=True
            )
            if not p["current"] and not (vc and vc.is_playing()):
                await start_next(interaction.guild)

        btn.callback = cb
        return btn


@bot.tree.command(name="playnext", description="Play a track next")
@app_commands.describe(query="Song or URL")
async def playnext(interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await require_voice(interaction)
    if not vc:
        return
    p = get_player(interaction.guild.id)
    try:
        track = await search_track(query, interaction.user)
    except Exception:
        return await interaction.followup.send("Search failed.", ephemeral=True)
    p["queue"].appendleft(track)
    await interaction.followup.send(embed=track_embed(track, "⏫ Queued Next"))
    if not p["current"] and not vc.is_playing():
        await start_next(interaction.guild)


@bot.tree.command(name="pause", description="Pause playback")
async def pause(interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message(embed=make_embed("⏸️ Paused", "", YELLOW))
    await interaction.response.send_message("Nothing playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume playback")
async def resume(interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message(embed=make_embed("▶️ Resumed", "", GREEN))
    await interaction.response.send_message("Nothing paused.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip current track")
async def skip(interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        get_player(interaction.guild.id)["skip_requested"] = True
        vc.stop()
        return await interaction.response.send_message(embed=make_embed("⏭️ Skipped", "", CYAN))
    await interaction.response.send_message("Nothing playing.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop and clear queue")
async def stop(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["queue"].clear()
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        p["suppress_end"] = True
        vc.stop()
    else:
        p["current"] = None
    await interaction.response.send_message(embed=make_embed("⏹️ Stopped", "Queue cleared.", RED))


@bot.tree.command(name="leave", description="Disconnect from voice")
async def leave(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["queue"].clear()
    vc = interaction.guild.voice_client
    if vc:
        p["suppress_end"] = True
        await vc.disconnect(force=True)
        p["current"] = None
        return await interaction.response.send_message(embed=make_embed("👋 Disconnected", "", GRAY))
    await interaction.response.send_message("Not connected.", ephemeral=True)


@bot.tree.command(name="replay", description="Restart current track")
async def replay(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    if not p["current"]:
        return await interaction.response.send_message("Nothing playing.", ephemeral=True)
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Not connected.", ephemeral=True)
    p["seek_seconds"] = 0
    p["suppress_end"] = True
    vc.stop()
    await interaction.response.send_message(embed=make_embed("🔁 Replaying", "", GREEN))


@bot.tree.command(name="previous", description="Play previous track")
async def previous(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Not connected.", ephemeral=True)
    if len(p["history"]) < 2:
        return await interaction.response.send_message("No previous track.", ephemeral=True)
    prev = p["history"][1]
    p["queue"].appendleft(prev)
    if vc.is_playing() or vc.is_paused():
        p["skip_requested"] = True
        vc.stop()
    else:
        await start_next(interaction.guild)
    await interaction.response.send_message(embed=track_embed(prev, "⏮️ Previous"))


@bot.tree.command(name="seek", description="Seek to a position (seconds)")
@app_commands.describe(seconds="Seconds to jump to")
async def seek(interaction, seconds: app_commands.Range[int, 0, 36000]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing playing.", ephemeral=True)
    p["seek_seconds"] = int(seconds)
    p["suppress_end"] = True
    vc.stop()
    await interaction.response.send_message(
        embed=make_embed("⏩ Seek", f"→ `{duration_text(seconds)}`", GREEN)
    )


# =============================================================================
# COMMANDS — QUEUE
# =============================================================================
@bot.tree.command(name="queue", description="Show queue")
async def queue_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    await send_queue_page(interaction, interaction.guild.id, 0)


@bot.tree.command(name="nowplaying", description="Current track")
async def nowplaying(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    if not p["current"]:
        return await interaction.response.send_message("Nothing playing.", ephemeral=True)
    elapsed = time.time() - p["started_at"] if p["started_at"] else 0
    e = track_embed(p["current"])
    e.description += f"\n\n{progress_bar(elapsed, p['track_duration'])}"
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="history", description="Recently played")
async def history_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    if not p["history"]:
        return await interaction.response.send_message("No history.", ephemeral=True)
    lines = [f"`{i}.` [{t['title']}]({t['webpage_url']})" for i, t in enumerate(list(p["history"])[:15], 1)]
    await interaction.response.send_message(
        embed=make_embed("📚 Recently Played", "\n".join(lines), PURPLE)
    )


@bot.tree.command(name="remove", description="Remove from queue")
@app_commands.describe(position="Queue position")
async def remove(interaction, position: app_commands.Range[int, 1, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = list(p["queue"])
    if position > len(tracks):
        return await interaction.response.send_message(f"Invalid (max {len(tracks)}).", ephemeral=True)
    removed = tracks.pop(position - 1)
    p["queue"] = deque(tracks)
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed("🗑️ Removed", f"**{removed['title']}**")
    )


@bot.tree.command(name="move", description="Move in queue")
async def move_cmd(interaction, src: app_commands.Range[int, 1, 100], dst: app_commands.Range[int, 1, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = list(p["queue"])
    if src > len(tracks) or dst > len(tracks):
        return await interaction.response.send_message("Invalid.", ephemeral=True)
    t = tracks.pop(src - 1)
    tracks.insert(dst - 1, t)
    p["queue"] = deque(tracks)
    await interaction.response.send_message(
        embed=make_embed("📋 Moved", f"**{t['title']}** → pos **{dst}**")
    )


@bot.tree.command(name="skipto", description="Skip ahead")
async def skipto(interaction, position: app_commands.Range[int, 1, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not p["queue"]:
        return await interaction.response.send_message("Queue empty.", ephemeral=True)
    if position > len(p["queue"]):
        return await interaction.response.send_message("Invalid.", ephemeral=True)
    for _ in range(position - 1):
        p["queue"].popleft()
    if vc and (vc.is_playing() or vc.is_paused()):
        p["skip_requested"] = True
        vc.stop()
    else:
        await start_next(interaction.guild)
    await interaction.response.send_message(
        embed=make_embed("⏩ Skipped", f"→ pos **{position}**", GREEN)
    )


@bot.tree.command(name="shuffle", description="Shuffle queue")
async def shuffle_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = list(p["queue"])
    random.shuffle(tracks)
    p["queue"] = deque(tracks)
    await interaction.response.send_message(
        embed=make_embed("🔀 Shuffled", f"**{len(tracks)}** tracks")
    )


@bot.tree.command(name="clearqueue", description="Clear queue")
async def clearqueue(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    n = len(p["queue"])
    p["queue"].clear()
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed("🧹 Cleared", f"Removed **{n}** tracks.", YELLOW)
    )


# =============================================================================
# COMMANDS — SETTINGS & CONTROL
# =============================================================================
@bot.tree.command(name="volume", description="Set volume 0-100")
async def volume_cmd(interaction, level: app_commands.Range[int, 0, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["volume"] = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p["volume"]
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed("🔊 Volume", f"**{level}%**", GREEN)
    )


@bot.tree.command(name="loop", description="Loop mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Song", value="song"),
    app_commands.Choice(name="Queue", value="queue"),
])
async def loop_cmd(interaction, mode: app_commands.Choice[str]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    get_player(interaction.guild.id)["loop"] = mode.value
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed("🔁 Loop", f"**{mode.name}**", PURPLE)
    )


@bot.tree.command(name="autoplay", description="Toggle autoplay")
async def autoplay_cmd(interaction, enabled: bool):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["autoplay"] = enabled
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed(
            "♾️ Autoplay",
            f"**{'ON' if enabled else 'OFF'}**",
            GREEN if enabled else PURPLE,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="filter", description="Apply audio filter")
@app_commands.choices(name=[
    app_commands.Choice(name=name, value=name) for name in FILTERS
])
async def filter_cmd(interaction, name: app_commands.Choice[str]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["filter"] = name.value
    save_guild_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        p["suppress_end"] = True
        vc.stop()
    await interaction.response.send_message(
        embed=make_embed("🎛️ Filter", f"Applied **{name.name}**", CYAN)
    )


@bot.tree.command(name="speed", description="Playback speed")
@app_commands.choices(speed=[
    app_commands.Choice(name="0.75x", value=75),
    app_commands.Choice(name="1.0x", value=100),
    app_commands.Choice(name="1.25x", value=125),
    app_commands.Choice(name="1.5x", value=150),
    app_commands.Choice(name="2.0x", value=200),
])
async def speed_cmd(interaction, speed: app_commands.Choice[int]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["speed"] = speed.value / 100
    save_guild_state(interaction.guild.id)
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        p["suppress_end"] = True
        vc.stop()
    await interaction.response.send_message(
        embed=make_embed("⚡ Speed", f"**{speed.name}**", GREEN)
    )


@bot.tree.command(name="247", description="Toggle 24/7 mode")
async def stay_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["stay_247"] = not p["stay_247"]
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed(
            "📻 24/7 Mode",
            f"**{'ON' if p['stay_247'] else 'OFF'}**",
            GREEN if p["stay_247"] else GRAY,
        )
    )


@bot.tree.command(name="dj", description="Set DJ role")
@app_commands.describe(role="Role required for control commands (leave empty to clear)")
async def dj_cmd(interaction, role: discord.Role = None):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    if not interaction.user.guild_permissions.manage_guild:
        return await interaction.response.send_message("Manage Server required.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["dj_role"] = role.id if role else None
    save_guild_state(interaction.guild.id)
    await interaction.response.send_message(
        embed=make_embed(
            "🎧 DJ Role",
            f"**{role.name}**" if role else "Cleared",
            PURPLE,
        )
    )


@bot.tree.command(name="voteskip", description="Vote to skip")
async def voteskip(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc or not vc.is_playing():
        return await interaction.response.send_message("Nothing playing.", ephemeral=True)
    listeners = [m for m in vc.channel.members if not m.bot]
    if interaction.user.id in p["vote_skip"]:
        return await interaction.response.send_message("Already voted.", ephemeral=True)
    p["vote_skip"].add(interaction.user.id)
    needed = max(1, len(listeners) // 2 + 1)
    if len(p["vote_skip"]) >= needed:
        p["skip_requested"] = True
        vc.stop()
        return await interaction.response.send_message(
            embed=make_embed("⏭️ Skipped", "Vote passed!", GREEN)
        )
    await interaction.response.send_message(
        embed=make_embed(
            "🗳️ Vote Skip",
            f"**{len(p['vote_skip'])}/{needed}** — `/voteskip` again",
            YELLOW,
        )
    )


# =============================================================================
# COMMANDS — PLAYLISTS
# =============================================================================
def _playlist_path(guild_id, name):
    safe = re.sub(r"[^a-zA-Z0-9_-]", "_", name.lower())
    return DATA_DIR / f"playlist_{guild_id}_{safe}.json"


@bot.tree.command(name="save", description="Save current queue as playlist")
@app_commands.describe(name="Playlist name")
async def save_cmd(interaction, name: str):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = []
    if p["current"]:
        tracks.append({k: p["current"].get(k) for k in ("title", "webpage_url", "duration", "thumbnail")})
    for t in p["queue"]:
        tracks.append({k: t.get(k) for k in ("title", "webpage_url", "duration", "thumbnail")})
    if not tracks:
        return await interaction.response.send_message("Nothing to save.", ephemeral=True)
    _playlist_path(interaction.guild.id, name).write_text(json.dumps(tracks, indent=2))
    await interaction.response.send_message(
        embed=make_embed("💾 Saved", f"**{name}** ({len(tracks)} tracks)", GREEN)
    )


@bot.tree.command(name="load", description="Load a saved playlist")
@app_commands.describe(name="Playlist name")
async def load_cmd(interaction, name: str):
    await interaction.response.defer()
    vc = await require_voice(interaction)
    if not vc:
        return
    path = _playlist_path(interaction.guild.id, name)
    if not path.exists():
        return await interaction.followup.send("Playlist not found.", ephemeral=True)
    try:
        tracks = json.loads(path.read_text())
    except Exception:
        return await interaction.followup.send("Corrupt playlist.", ephemeral=True)
    p = get_player(interaction.guild.id)
    for t in tracks:
        p["queue"].append({**t, "url": None, "requester": str(interaction.user)})
    save_guild_state(interaction.guild.id)
    await interaction.followup.send(
        embed=make_embed("📥 Loaded", f"**{name}** ({len(tracks)} tracks)", GREEN)
    )
    if not p["current"] and not vc.is_playing():
        await start_next(interaction.guild)


@bot.tree.command(name="playlists", description="List saved playlists")
async def playlists_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    files = list(DATA_DIR.glob(f"playlist_{interaction.guild.id}_*.json"))
    if not files:
        return await interaction.response.send_message("No playlists yet.", ephemeral=True)
    lines = []
    for f in files[:20]:
        try:
            n = len(json.loads(f.read_text()))
            name = f.stem.replace(f"playlist_{interaction.guild.id}_", "")
            lines.append(f"`{name}` — {n} tracks")
        except Exception:
            pass
    await interaction.response.send_message(
        embed=make_embed("📚 Playlists", "\n".join(lines), PURPLE)
    )


# =============================================================================
# COMMANDS — MISC
# =============================================================================
@bot.tree.command(name="join", description="Join voice")
async def join_cmd(interaction):
    await interaction.response.defer(ephemeral=True)
    vc = await require_voice(interaction)
    if vc:
        await interaction.followup.send(f"✅ Joined **{vc.channel.name}**", ephemeral=True)


@bot.tree.command(name="dashboard", description="Open dashboard")
async def dashboard_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    await interaction.response.send_message(
        embed=dashboard_embed(interaction.guild),
        view=MusicDashboard(),
    )


@bot.tree.command(name="ping", description="Check latency")
async def ping_cmd(interaction):
    lat = round(bot.latency * 1000)
    await interaction.response.send_message(
        embed=make_embed("🏓 Pong!", f"Gateway: **{lat}ms**", GREEN if lat < 100 else YELLOW),
        ephemeral=True,
    )


@bot.tree.command(name="stats", description="Bot stats")
async def stats_cmd(interaction):
    uptime = int(time.time() - START_TIME)
    h, r = divmod(uptime, 3600)
    m, s = divmod(r, 60)
    e = make_embed("📊 RAJ MUSIC PRO • Stats", color=CYAN)
    e.add_field(name="Uptime", value=f"`{h}h {m}m {s}s`", inline=True)
    e.add_field(name="Guilds", value=f"`{len(bot.guilds)}`", inline=True)
    e.add_field(name="Latency", value=f"`{round(bot.latency * 1000)}ms`", inline=True)
    e.add_field(name="Tracks played", value=f"`{STATS['tracks_played']}`", inline=True)
    e.add_field(name="Searches", value=f"`{STATS['searches']}`", inline=True)
    e.add_field(name="Cache hits", value=f"`{STATS['cache_hits']}`", inline=True)
    e.add_field(name="Errors", value=f"`{STATS['errors']}`", inline=True)
    e.add_field(name="Active", value=f"`{sum(1 for p in players.values() if p['current'])}`", inline=True)
    e.add_field(name="Cached URLs", value=f"`{len(url_cache)}`", inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="musicinfo", description="Player status")
async def musicinfo_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    e = make_embed("🎛️ Player Status", color=CYAN)
    e.add_field(name="Voice", value=f"`{vc.channel.name}`" if vc and vc.is_connected() else "`Disconnected`", inline=True)
    e.add_field(name="Playback", value="`Paused`" if vc and vc.is_paused() else ("`Playing`" if vc and vc.is_playing() else "`Idle`"), inline=True)
    e.add_field(name="Volume", value=f"`{round(p['volume'] * 100)}%`", inline=True)
    e.add_field(name="Queue", value=f"`{len(p['queue'])}`", inline=True)
    e.add_field(name="Loop", value=f"`{p['loop'].title()}`", inline=True)
    e.add_field(name="Autoplay", value=f"`{'On' if p['autoplay'] else 'Off'}`", inline=True)
    e.add_field(name="Filter", value=f"`{p['filter']}`", inline=True)
    e.add_field(name="Speed", value=f"`{p['speed']}x`", inline=True)
    e.add_field(name="24/7", value=f"`{'On' if p['stay_247'] else 'Off'}`", inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="lyrics", description="Fetch lyrics")
@app_commands.describe(query="Optional song title")
async def lyrics_cmd(interaction, query: str = None):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    title = query or (p["current"]["title"] if p["current"] else None)
    if not title:
        return await interaction.response.send_message("Nothing playing. Pass a title.", ephemeral=True)
    await interaction.response.defer()
    text = await fetch_lyrics(title)
    if not text:
        return await interaction.followup.send(
            embed=make_embed("🎤 Lyrics", f"No lyrics for **{title}**", YELLOW)
        )
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    await interaction.followup.send(embed=make_embed("🎤 Lyrics", f"**{title}**\n\n{chunks[0][:3900]}", PURPLE))
    for c in chunks[1:4]:
        await interaction.followup.send(embed=make_embed("🎤 Lyrics (cont.)", c, PURPLE))


@bot.tree.command(name="healthcheck", description="Test YouTube")
async def healthcheck_cmd(interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        t = await search_track("never gonna give you up", "healthcheck", use_cache=False)
        await refresh_stream_url(t)
    except Exception as e:
        return await interaction.followup.send(
            embed=make_embed("❌ Failed", f"`{type(e).__name__}`\nRun `pip install -U yt-dlp`", RED),
            ephemeral=True,
        )
    await interaction.followup.send(
        embed=make_embed("✅ Passed", f"Resolved **{t['title']}**", GREEN),
        ephemeral=True,
    )


@bot.tree.command(name="botinfo", description="Bot information")
async def botinfo_cmd(interaction):
    e = make_embed("🤖 RAJ MUSIC PRO", "Ultimate Edition", CYAN)
    e.set_thumbnail(url=bot.user.display_avatar.url)
    e.add_field(name="Servers", value=f"`{len(bot.guilds)}`", inline=True)
    e.add_field(name="Users", value=f"`{sum(g.member_count or 0 for g in bot.guilds):,}`", inline=True)
    e.add_field(name="Commands", value=f"`{len(bot.tree.get_commands())}`", inline=True)
    e.add_field(name="Python", value=f"`{os.sys.version.split()[0]}`", inline=True)
    e.add_field(name="discord.py", value=f"`{discord.__version__}`", inline=True)
    e.add_field(name="yt-dlp", value=f"`{yt_dlp.version.__version__}`", inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="invite", description="Get bot invite link")
async def invite_cmd(interaction):
    url = discord.utils.oauth_url(
        bot.user.id,
        permissions=discord.Permissions(
            connect=True, speak=True, use_voice_activation=True, send_messages=True, embed_links=True
        ),
    )
    await interaction.response.send_message(
        embed=make_embed("🔗 Invite Me", f"[Click here]({url})", GREEN),
        ephemeral=True,
    )


@bot.tree.command(name="help", description="All commands")
async def help_cmd(interaction):
    e = make_embed("🎵 RAJ MUSIC PRO — Commands", color=CYAN)
    e.add_field(
        name="▶️ Playback",
        value="`/play` `/search` `/playnext` `/pause` `/resume` `/skip` `/stop` `/replay` `/previous` `/seek` `/voteskip`",
        inline=False,
    )
    e.add_field(
        name="📜 Queue",
        value="`/queue` `/nowplaying` `/history` `/remove` `/move` `/skipto` `/shuffle` `/clearqueue`",
        inline=False,
    )
    e.add_field(
        name="🎛️ Settings",
        value="`/volume` `/loop` `/autoplay` `/filter` `/speed` `/247` `/dj` `/dashboard`",
        inline=False,
    )
    e.add_field(
        name="💾 Playlists",
        value="`/save` `/load` `/playlists`",
        inline=False,
    )
    e.add_field(
        name="ℹ️ Info",
        value="`/join` `/leave` `/musicinfo` `/lyrics` `/stats` `/ping` `/botinfo` `/invite` `/healthcheck` `/help`",
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


# =============================================================================
# RUN
# =============================================================================
if __name__ == "__main__":
    try:
        bot.run(TOKEN, log_handler=None)
    except KeyboardInterrupt:
        log.info("Shutting down.")