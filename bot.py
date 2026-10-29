"""
RAJ MUSIC PRO — Professional Discord Music Bot
No cookies required. Uses multi-client yt-dlp extraction.
"""

import os
import re
import asyncio
import logging
import random
import time
from collections import deque

import discord
from discord import app_commands
from discord.ext import commands, tasks
from dotenv import load_dotenv
import yt_dlp
import aiohttp

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID", "").strip()
AUTO_DISCONNECT_MINUTES = int(os.getenv("AUTO_DISCONNECT_MINUTES", "5"))
OWNER_IDS = {int(x) for x in os.getenv("OWNER_IDS", "").split(",") if x.strip().isdigit()}

if not TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN environment variable")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("RajMusicPro")

CYAN = 0x06B6D4
RED = 0xEF4444
GREEN = 0x22C55E
PURPLE = 0x8B5CF6
YELLOW = 0xF59E0B
GRAY = 0x6B7280

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

FFMPEG_BEFORE = (
    "-reconnect 1 -reconnect_streamed 1 "
    "-reconnect_delay_max 5 -rw_timeout 15000000 -nostdin"
)

# URL cache TTL: re-extract only if URL is older than this
STREAM_URL_TTL = 300  # seconds

YDL_OPTIONS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch1:",
    "socket_timeout": 30,
    "retries": 5,
    "fragment_retries": 5,
    "extractor_retries": 3,
    "file_access_retries": 3,
    "source_address": "0.0.0.0",
    "cachedir": False,
    "geo_bypass": True,
    "nocheckcertificate": True,
    "extract_flat": False,
    "http_headers": {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
    },
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "ios", "web_safari"],
            "skip": ["hls"],
        },
    },
}

# Autoplay uses ytsearch5 to pick a genuinely different result
AUTOPLAY_OPTIONS = dict(YDL_OPTIONS)
AUTOPLAY_OPTIONS["default_search"] = "ytsearch5:"

players: dict[int, dict] = {}
START_TIME = time.time()
STATS = {"tracks_played": 0, "searches": 0}
SYNC_HASH = "v2-fixed"  # bump to force re-sync of slash commands


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def make_embed(title, description="", color=CYAN):
    e = discord.Embed(
        title=title,
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    e.set_footer(text="RAJ MUSIC PRO • Premium Music Experience")
    return e


def duration_text(seconds):
    if seconds is None:
        return "Live / Unknown"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


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
            "replay_current": False,   # NEW: replay / seek intent
            "pending_seek": 0,         # NEW: seek offset for next playback
            "autoplay": False,
            "last_track": None,
            "history": deque(maxlen=25),
            "idle_since": time.time(),
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
        log.debug("safe_send failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# yt-dlp wrappers
# ---------------------------------------------------------------------------
async def search_track(query, requester, options=None, exclude_url=None):
    loop = asyncio.get_running_loop()
    STATS["searches"] += 1
    opts = options or YDL_OPTIONS

    def extract():
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(query, download=False)
        if info and "entries" in info:
            entries = [e for e in info["entries"] if e]
            if exclude_url:
                entries = [e for e in entries if e.get("webpage_url") != exclude_url]
            info = entries[0] if entries else None
        if not info or not info.get("url"):
            raise ValueError("No playable audio found.")
        return {
            "title": info.get("title", "Unknown title"),
            "url": info.get("url"),
            "url_ts": time.time(),
            "webpage_url": info.get("webpage_url") or info.get("original_url") or query,
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
            "requester": str(requester),
            "channel": info.get("uploader"),
        }

    return await loop.run_in_executor(None, extract)


async def refresh_stream_url(track):
    # Use cached URL if it's still fresh
    if track.get("url") and (time.time() - track.get("url_ts", 0)) < STREAM_URL_TTL:
        return track["url"]

    loop = asyncio.get_running_loop()

    def extract():
        with yt_dlp.YoutubeDL(YDL_OPTIONS) as ydl:
            info = ydl.extract_info(track["webpage_url"], download=False)
        if info and "entries" in info:
            info = next((e for e in info["entries"] if e), None)
        if not info or not info.get("url"):
            raise ValueError("Source did not return a playable stream.")
        return info["url"]

    url = await loop.run_in_executor(None, extract)
    track["url"] = url
    track["url_ts"] = time.time()
    return url


def clean_title_for_lyrics(title: str) -> str:
    """Strip common YouTube suffixes before searching lyrics."""
    title = re.sub(
        r"\s*[\(\[\{][^\)\]\}]*(official|lyrics?|video|audio|hd|hq|mv|m/v)[^\)\]\}]*[\)\]\}]",
        "", title, flags=re.I,
    )
    title = re.sub(r"\s*-\s*(topic|vevo)\s*$", "", title, flags=re.I)
    return title.strip()


async def fetch_lyrics(title: str, artist: str = None):
    url = "https://lrclib.net/api/search"
    params = {"q": clean_title_for_lyrics(title)}
    if artist:
        params["artist_name"] = artist
    try:
        async with aiohttp.ClientSession() as s:
            async with s.get(url, params=params, timeout=10) as r:
                if r.status != 200:
                    return None
                data = await r.json()
                if not data:
                    return None
                return data[0].get("plainLyrics")
    except Exception:
        log.debug("lyrics fetch failed", exc_info=True)
        return None


# ---------------------------------------------------------------------------
# Embeds
# ---------------------------------------------------------------------------
def track_embed(track, title="🎶 Now Playing"):
    e = make_embed(
        title,
        f"**[{track['title']}]({track['webpage_url']})**\n\n"
        f"⏱️ Duration: `{duration_text(track['duration'])}`\n"
        f"👤 Requested by: `{track['requester']}`",
        GREEN,
    )
    if track.get("thumbnail"):
        e.set_thumbnail(url=track["thumbnail"])
    if track.get("channel"):
        e.add_field(name="Channel", value=track["channel"], inline=True)
    return e


def dashboard_embed(guild):
    p = get_player(guild.id)
    track = p["current"]
    if track:
        e = track_embed(track, "🎧 RAJ MUSIC PRO • LIVE PLAYER")
        vc = guild.voice_client
        status = "⏸️ Paused" if vc and vc.is_paused() else "▶️ Active"
        e.description += (
            f"\n\n**Playback**  {status}"
            f"  ·  **Volume** `{round(p['volume'] * 100)}%`"
            f"  ·  **Loop** `{p['loop'].title()}`"
            f"\n**Autoplay**  `{'On' if p['autoplay'] else 'Off'}`"
            f"  ·  **Up next:** `{len(p['queue'])}` track(s)"
        )
        return e
    return make_embed(
        "🎧 RAJ MUSIC PRO • MUSIC DASHBOARD",
        "Your premium music controls are ready.\n\n"
        "**Get started:** use `/play` to add a track, then manage playback below.\n"
        "Use the buttons below for queue, shuffle, loop, and volume controls.",
        PURPLE,
    )


# ---------------------------------------------------------------------------
# Persistent dashboard view
# ---------------------------------------------------------------------------
class MusicDashboard(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def interaction_check(self, interaction):
        if not interaction.guild:
            await interaction.response.send_message(
                "These controls can only be used inside a server.", ephemeral=True
            )
            return False
        return True

    async def _reply(self, interaction, message, *, embed=None):
        if not interaction.response.is_done():
            await interaction.response.send_message(message, embed=embed, ephemeral=True)
        else:
            await interaction.followup.send(message, embed=embed, ephemeral=True)

    async def _refresh_embed(self, interaction):
        """Re-render the dashboard embed after state changes."""
        try:
            await interaction.message.edit(
                embed=dashboard_embed(interaction.guild), view=self
            )
        except discord.HTTPException:
            log.debug("dashboard refresh failed", exc_info=True)

    @discord.ui.button(label="Pause", emoji="⏸️", style=discord.ButtonStyle.primary,
                       custom_id="rajmusic:pause_resume", row=0)
    async def pause_resume(self, interaction, button):
        vc = interaction.guild.voice_client
        if not vc:
            return await self._reply(interaction, "I’m not connected to a voice channel.")
        if vc.is_playing():
            vc.pause()
            button.label, button.emoji = "Resume", "▶️"
            await interaction.response.edit_message(view=self)
            await self._refresh_embed(interaction)
            await interaction.followup.send("Playback paused.", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            button.label, button.emoji = "Pause", "⏸️"
            await interaction.response.edit_message(view=self)
            await self._refresh_embed(interaction)
            await interaction.followup.send("Playback resumed.", ephemeral=True)
        else:
            await self._reply(interaction, "Nothing is currently playing.")

    @discord.ui.button(label="Skip", emoji="⏭️", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:skip", row=0)
    async def skip_track(self, interaction, button):
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            get_player(interaction.guild.id)["skip_requested"] = True
            vc.stop()
            await self._reply(interaction, "Skipped. Moving to the next track.")
        else:
            await self._reply(interaction, "Nothing is currently playing.")

    @discord.ui.button(label="Stop", emoji="⏹️", style=discord.ButtonStyle.danger,
                       custom_id="rajmusic:stop", row=0)
    async def stop_playback(self, interaction, button):
        p = get_player(interaction.guild.id)
        p["queue"].clear()
        vc = interaction.guild.voice_client
        if vc and (vc.is_playing() or vc.is_paused()):
            p["suppress_end"] = True
            p["replay_current"] = False
            p["pending_seek"] = 0
            vc.stop()
        p["current"] = None
        await self._reply(interaction, "Playback stopped and the queue was cleared.")

    @discord.ui.button(label="Queue", emoji="📜", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:queue", row=0)
    async def show_queue(self, interaction, button):
        p = get_player(interaction.guild.id)
        lines = []
        if p["current"]:
            t = p["current"]
            lines.append(f"🎶 **Now:** [{t['title']}]({t['webpage_url']})")
        for i, track in enumerate(list(p["queue"])[:12], 1):
            lines.append(
                f"`{i}.` [{track['title']}]({track['webpage_url']}) — `{duration_text(track['duration'])}`"
            )
        if not lines:
            lines = ["The queue is empty. Use `/play` to add music."]
        e = make_embed("📜 Music Queue", "\n".join(lines), PURPLE)
        e.add_field(name="Waiting tracks", value=str(len(p["queue"])), inline=True)
        e.add_field(name="Loop mode", value=p["loop"].title(), inline=True)
        await self._reply(interaction, "", embed=e)

    @discord.ui.button(label="Shuffle", emoji="🔀", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:shuffle", row=1)
    async def shuffle_queue(self, interaction, button):
        p = get_player(interaction.guild.id)
        tracks = list(p["queue"])
        random.shuffle(tracks)
        p["queue"] = deque(tracks)
        await self._reply(interaction, f"Shuffled **{len(tracks)}** waiting track(s).")

    @discord.ui.button(label="Loop: Off", emoji="🔁", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:loop", row=1)
    async def cycle_loop(self, interaction, button):
        p = get_player(interaction.guild.id)
        modes = ["off", "song", "queue"]
        p["loop"] = modes[(modes.index(p["loop"]) + 1) % len(modes)]
        button.label = f"Loop: {p['loop'].title()}"
        await interaction.response.edit_message(view=self)
        await self._refresh_embed(interaction)
        await interaction.followup.send(
            f"Loop mode set to **{p['loop'].title()}**.", ephemeral=True
        )

    @discord.ui.button(label="Volume −", emoji="🔉", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:volume_down", row=1)
    async def volume_down(self, interaction, button):
        await self._change_volume(interaction, -10)

    @discord.ui.button(label="Volume +", emoji="🔊", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:volume_up", row=1)
    async def volume_up(self, interaction, button):
        await self._change_volume(interaction, 10)

    @discord.ui.button(label="Autoplay: Off", emoji="♾️", style=discord.ButtonStyle.secondary,
                       custom_id="rajmusic:autoplay", row=2)
    async def toggle_autoplay(self, interaction, button):
        p = get_player(interaction.guild.id)
        p["autoplay"] = not p.get("autoplay", False)
        button.label = f"Autoplay: {'On' if p['autoplay'] else 'Off'}"
        await interaction.response.edit_message(view=self)
        await self._refresh_embed(interaction)
        await interaction.followup.send(
            f"Autoplay is now **{'enabled' if p['autoplay'] else 'disabled'}**.",
            ephemeral=True,
        )

    async def _change_volume(self, interaction, amount):
        p = get_player(interaction.guild.id)
        level = max(0, min(100, round(p["volume"] * 100) + amount))
        p["volume"] = level / 100
        vc = interaction.guild.voice_client
        if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
            vc.source.volume = p["volume"]
        await self._reply(interaction, f"Volume set to **{level}%**.")
        await self._refresh_embed(interaction)


# ---------------------------------------------------------------------------
# Playback engine
# ---------------------------------------------------------------------------
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
                    query = f"{prev.get('channel') or ''} {prev['title']}".strip()
                    related = await search_track(
                        query,
                        "RAJ MUSIC PRO Autoplay",
                        options=AUTOPLAY_OPTIONS,
                        exclude_url=prev.get("webpage_url"),
                    )
                    if related.get("webpage_url") != prev.get("webpage_url"):
                        p["queue"].append(related)
                except Exception:
                    log.exception("Autoplay could not find a related track")

            if not p["queue"]:
                p["current"] = None
                p["idle_since"] = time.time()
                channel = get_text_channel(p)
                await safe_send(
                    channel,
                    embed=make_embed(
                        "📭 Queue Finished",
                        "The queue is empty. I'm leaving voice shortly.\n"
                        "Use `/play` to start again, or enable `/autoplay`.",
                        PURPLE,
                    ),
                )
                return

        track = p["queue"].popleft()
        p["current"] = track
        seek_offset = p.get("pending_seek", 0)
        p["pending_seek"] = 0

        try:
            fresh_url = await refresh_stream_url(track)
            before = FFMPEG_BEFORE + (f" -ss {seek_offset}" if seek_offset > 0 else "")
            audio = discord.FFmpegPCMAudio(fresh_url, before_options=before, options="-vn")
            source = discord.PCMVolumeTransformer(audio, volume=p["volume"])

            def after_play(error, _gid=guild.id, _t=track):
                if error:
                    log.error("Playback error: %s", error)
                if bot.loop.is_closed():
                    return
                fut = asyncio.run_coroutine_threadsafe(
                    track_finished(_gid, _t, error), bot.loop
                )
                # Do NOT block the FFmpeg thread with .result()
                fut.add_done_callback(
                    lambda f: f.exception()
                    and log.error("track_finished failed: %s", f.exception())
                )

            vc.play(source, after=after_play)
            STATS["tracks_played"] += 1
            p["history"].appendleft(track)
            p["idle_since"] = time.time()

            channel = get_text_channel(p)
            await safe_send(channel, embed=track_embed(track), view=MusicDashboard())

        except Exception:
            log.exception("Failed to start track")
            p["current"] = None
            asyncio.create_task(start_next(guild))


async def track_finished(guild_id, finished_track, error=None):
    p = get_player(guild_id)

    async with p["lock"]:
        # Handle stop / replay / seek paths first
        if p["suppress_end"]:
            p["suppress_end"] = False
            p["skip_requested"] = False
            replay = p.get("replay_current", False)
            p["replay_current"] = False
            p["current"] = None
            if replay:
                p["queue"].appendleft(finished_track)
            # fall through to start_next

        else:
            skipped = p["skip_requested"]
            p["skip_requested"] = False

            p["last_track"] = finished_track

            if not skipped and p["loop"] == "song":
                p["queue"].appendleft(finished_track)
            elif not skipped and p["loop"] == "queue":
                p["queue"].append(finished_track)

            p["current"] = None

    if error:
        channel = get_text_channel(p)
        await safe_send(
            channel,
            embed=make_embed(
                "⚠️ Playback Error",
                f"**{finished_track.get('title', 'Unknown track')}** failed.\n"
                "Trying the next queued track.",
                RED,
            ),
        )

    guild = bot.get_guild(guild_id)
    if guild:
        await start_next(guild)


# ---------------------------------------------------------------------------
# Voice helpers
# ---------------------------------------------------------------------------
async def require_voice(interaction):
    if not interaction.guild:
        await interaction.followup.send("This command can only be used in a server.", ephemeral=True)
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
    except Exception as error:
        log.exception("Voice connection failed")
        await interaction.followup.send(
            f"Could not join voice: `{type(error).__name__}`", ephemeral=True
        )
        return None

    p = get_player(interaction.guild.id)
    p["text_channel"] = interaction.channel_id
    p["suppress_end"] = False
    p["skip_requested"] = False
    return vc


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------
@bot.event
async def on_ready():
    if not getattr(bot, "_music_view_registered", False):
        bot.add_view(MusicDashboard())
        bot._music_view_registered = True

    if getattr(bot, "_sync_hash", None) != SYNC_HASH:
        try:
            if GUILD_ID.isdigit():
                guild = discord.Object(id=int(GUILD_ID))
                bot.tree.copy_global_to(guild=guild)
                synced = await bot.tree.sync(guild=guild)
                log.info("Synced %s commands to guild %s", len(synced), GUILD_ID)
            else:
                synced = await bot.tree.sync()
                log.info("Synced %s global commands", len(synced))
            bot._sync_hash = SYNC_HASH
        except Exception:
            log.exception("Slash command sync failed")

    if not idle_watcher.is_running():
        idle_watcher.start()

    log.info("RAJ MUSIC PRO online as %s", bot.user)


@bot.event
async def on_voice_state_update(member, before, after):
    if member.id != bot.user.id:
        return
    if not member.guild:
        return
    if before.channel and after.channel is None:
        p = get_player(member.guild.id)
        async with p["lock"]:
            p["current"] = None
            p["queue"].clear()
            p["suppress_end"] = True
            p["skip_requested"] = False
            p["replay_current"] = False
            p["pending_seek"] = 0
            p["last_track"] = None


@tasks.loop(minutes=1)
async def idle_watcher():
    if AUTO_DISCONNECT_MINUTES <= 0:
        return
    now = time.time()
    for gid, p in list(players.items()):
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
            log.info("Auto-disconnecting from guild %s (idle)", gid)
            p["suppress_end"] = True
            try:
                await vc.disconnect(force=True)
            except Exception:
                log.debug("idle disconnect failed", exc_info=True)
            p["current"] = None
            p["queue"].clear()


# ---------------------------------------------------------------------------
# Commands — playback
# ---------------------------------------------------------------------------
@bot.tree.command(name="play", description="Search for a song and play it")
@app_commands.describe(query="Song name or supported audio URL")
async def play(interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await require_voice(interaction)
    if not vc:
        return
    p = get_player(interaction.guild.id)

    try:
        track = await search_track(query, interaction.user)
    except Exception as error:
        log.warning("Search failed: %s", error)
        return await interaction.followup.send(
            embed=make_embed(
                "❌ Search Failed",
                "Could not find playable audio. Try another query or URL.",
                RED,
            )
        )

    if p["current"] or vc.is_playing() or vc.is_paused():
        if len(p["queue"]) >= 100:
            return await interaction.followup.send("Queue limit reached (100 tracks).", ephemeral=True)
        p["queue"].append(track)
        e = track_embed(track, "➕ Added to Queue")
        e.description += f"\n\n📌 Position: `{len(p['queue'])}`"
        return await interaction.followup.send(embed=e)

    p["queue"].append(track)
    await interaction.followup.send(
        embed=make_embed("🔎 Track Found", "Starting playback...", GREEN)
    )
    await start_next(interaction.guild)


@bot.tree.command(name="playnext", description="Insert a track at the front of the queue")
@app_commands.describe(query="Song name or URL")
async def playnext(interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await require_voice(interaction)
    if not vc:
        return
    p = get_player(interaction.guild.id)
    try:
        track = await search_track(query, interaction.user)
    except Exception:
        return await interaction.followup.send(
            embed=make_embed("❌ Search Failed", "Could not find playable audio.", RED)
        )
    p["queue"].appendleft(track)
    await interaction.followup.send(embed=track_embed(track, "⏫ Queued Next"))
    if not p["current"] and not vc.is_playing():
        await start_next(interaction.guild)


@bot.tree.command(name="pause", description="Pause the current song")
async def pause(interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_playing():
        vc.pause()
        return await interaction.response.send_message(
            embed=make_embed("⏸️ Paused", "Playback has been paused.")
        )
    await interaction.response.send_message("Nothing is currently playing.", ephemeral=True)


@bot.tree.command(name="resume", description="Resume paused music")
async def resume(interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and vc.is_paused():
        vc.resume()
        return await interaction.response.send_message(
            embed=make_embed("▶️ Resumed", "Playback has resumed.", GREEN)
        )
    await interaction.response.send_message("There is no paused song.", ephemeral=True)


@bot.tree.command(name="skip", description="Skip the current song")
async def skip(interaction):
    vc = interaction.guild.voice_client if interaction.guild else None
    if vc and (vc.is_playing() or vc.is_paused()):
        p = get_player(interaction.guild.id)
        p["skip_requested"] = True
        vc.stop()
        return await interaction.response.send_message(
            embed=make_embed("⏭️ Skipped", "Moving to the next track.")
        )
    await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(name="stop", description="Stop music and clear the queue")
async def stop(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["queue"].clear()
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        p["suppress_end"] = True
        p["replay_current"] = False
        p["pending_seek"] = 0
        vc.stop()
    else:
        p["current"] = None
    await interaction.response.send_message(
        embed=make_embed("⏹️ Stopped", "Playback stopped; queue cleared.", RED)
    )


@bot.tree.command(name="leave", description="Disconnect from voice")
async def leave(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["queue"].clear()
    vc = interaction.guild.voice_client
    if vc:
        p["suppress_end"] = True
        p["replay_current"] = False
        p["pending_seek"] = 0
        await vc.disconnect(force=True)
        p["current"] = None
        return await interaction.response.send_message(
            embed=make_embed("👋 Disconnected", "Left the voice channel.")
        )
    await interaction.response.send_message("I am not connected to voice.", ephemeral=True)


@bot.tree.command(name="replay", description="Restart the current track")
async def replay(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    track = p["current"]
    if not track:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    vc = interaction.guild.voice_client
    if not vc:
        return await interaction.response.send_message("Not connected.", ephemeral=True)
    p["pending_seek"] = 0
    p["replay_current"] = True
    p["suppress_end"] = True
    vc.stop()
    await interaction.response.send_message(
        embed=make_embed("🔁 Replaying", f"Restarting **{track['title']}**.", GREEN)
    )


@bot.tree.command(name="previous", description="Play the last-played track")
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
    await interaction.response.send_message(embed=track_embed(prev, "⏮️ Previous Track"))


@bot.tree.command(name="seek", description="Seek to a timestamp (seconds)")
@app_commands.describe(seconds="Seconds to jump to")
async def seek(interaction, seconds: app_commands.Range[int, 0, 36000]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not vc or not (vc.is_playing() or vc.is_paused()):
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    p["pending_seek"] = int(seconds)
    p["replay_current"] = True   # <- key: re-queue current track
    p["suppress_end"] = True
    vc.stop()
    await interaction.response.send_message(
        embed=make_embed("⏩ Seek", f"Jumping to `{duration_text(seconds)}`.", GREEN)
    )


# ---------------------------------------------------------------------------
# Commands — queue management
# ---------------------------------------------------------------------------
@bot.tree.command(name="queue", description="Show the music queue")
async def queue_cmd(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    lines = []
    if p["current"]:
        t = p["current"]
        lines.append(f"🎶 **Now:** [{t['title']}]({t['webpage_url']})")
    for i, track in enumerate(list(p["queue"])[:15], 1):
        lines.append(
            f"`{i}.` [{track['title']}]({track['webpage_url']}) — `{duration_text(track['duration'])}`"
        )
    if not lines:
        lines = ["The queue is empty. Use `/play` to add music."]
    e = make_embed("📜 Music Queue", "\n".join(lines))
    e.add_field(name="Waiting tracks", value=str(len(p["queue"])), inline=True)
    e.add_field(name="Loop mode", value=p["loop"].title(), inline=True)
    e.add_field(
        name="Total length",
        value=f"~{sum(int(t.get('duration') or 0) for t in p['queue']) // 60} min",
        inline=True,
    )
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="nowplaying", description="Show the current track")
async def nowplaying(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    track = get_player(interaction.guild.id)["current"]
    if not track:
        return await interaction.response.send_message("Nothing is playing.", ephemeral=True)
    await interaction.response.send_message(embed=track_embed(track))


@bot.tree.command(name="history", description="Show recently played tracks")
async def history(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    if not p["history"]:
        return await interaction.response.send_message("No history yet.", ephemeral=True)
    lines = [
        f"`{i}.` [{t['title']}]({t['webpage_url']})"
        for i, t in enumerate(list(p["history"])[:15], 1)
    ]
    await interaction.response.send_message(
        embed=make_embed("📚 Recently Played", "\n".join(lines), PURPLE)
    )


@bot.tree.command(name="remove", description="Remove a track from the queue")
@app_commands.describe(position="Queue position, starting from 1")
async def remove(interaction, position: app_commands.Range[int, 1, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = list(p["queue"])
    if position > len(tracks):
        return await interaction.response.send_message(
            f"Invalid position. Queue has **{len(tracks)}** track(s).", ephemeral=True
        )
    removed = tracks.pop(position - 1)
    p["queue"] = deque(tracks)
    await interaction.response.send_message(
        embed=make_embed("🗑️ Track Removed", f"Removed **{removed['title']}**.")
    )


@bot.tree.command(name="move", description="Move a track within the queue")
@app_commands.describe(src="From position (1-indexed)", dst="To position (1-indexed)")
async def move_cmd(
    interaction,
    src: app_commands.Range[int, 1, 100],
    dst: app_commands.Range[int, 1, 100],
):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = list(p["queue"])
    if src > len(tracks) or dst > len(tracks):
        return await interaction.response.send_message("Invalid position.", ephemeral=True)
    track = tracks.pop(src - 1)
    tracks.insert(dst - 1, track)
    p["queue"] = deque(tracks)
    await interaction.response.send_message(
        embed=make_embed("📋 Reordered", f"Moved **{track['title']}** to position **{dst}**.")
    )


@bot.tree.command(name="skipto", description="Skip ahead to a queue position")
@app_commands.describe(position="Position in the queue (1 = next)")
async def skipto(interaction, position: app_commands.Range[int, 1, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    if not p["queue"]:
        return await interaction.response.send_message("Queue is empty.", ephemeral=True)
    if position > len(p["queue"]):
        return await interaction.response.send_message("Invalid position.", ephemeral=True)
    for _ in range(position - 1):
        p["queue"].popleft()
    if vc and (vc.is_playing() or vc.is_paused()):
        p["skip_requested"] = True
        vc.stop()
    else:
        await start_next(interaction.guild)
    await interaction.response.send_message(
        embed=make_embed("⏩ Skipped Ahead", f"Jumped to position **{position}**.", GREEN)
    )


@bot.tree.command(name="shuffle", description="Shuffle the waiting queue")
async def shuffle(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    tracks = list(p["queue"])
    random.shuffle(tracks)
    p["queue"] = deque(tracks)
    await interaction.response.send_message(
        embed=make_embed("🔀 Queue Shuffled", f"Shuffled **{len(tracks)}** tracks.")
    )


@bot.tree.command(name="clearqueue", description="Clear all waiting tracks")
async def clearqueue(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    count = len(p["queue"])
    p["queue"].clear()
    await interaction.response.send_message(
        embed=make_embed("🧹 Queue Cleared", f"Removed **{count}** waiting tracks.")
    )


# ---------------------------------------------------------------------------
# Commands — settings
# ---------------------------------------------------------------------------
@bot.tree.command(name="volume", description="Set volume from 0 to 100")
@app_commands.describe(level="Volume percentage")
async def volume(interaction, level: app_commands.Range[int, 0, 100]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["volume"] = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p["volume"]
    await interaction.response.send_message(
        embed=make_embed("🔊 Volume Updated", f"Volume set to **{level}%**.")
    )


@bot.tree.command(name="loop", description="Change loop mode")
@app_commands.choices(mode=[
    app_commands.Choice(name="Off", value="off"),
    app_commands.Choice(name="Current Song", value="song"),
    app_commands.Choice(name="Queue", value="queue"),
])
async def loop(interaction, mode: app_commands.Choice[str]):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    get_player(interaction.guild.id)["loop"] = mode.value
    await interaction.response.send_message(
        embed=make_embed("🔁 Loop Updated", f"Mode: **{mode.name}**")
    )


@bot.tree.command(name="autoplay", description="Toggle automatic related-song playback")
@app_commands.describe(enabled="Turn autoplay on or off")
async def autoplay(interaction, enabled: bool):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    p["autoplay"] = enabled
    await interaction.response.send_message(
        embed=make_embed(
            "♾️ Autoplay Updated",
            f"Autoplay is now **{'enabled' if enabled else 'disabled'}**.",
            GREEN if enabled else PURPLE,
        ),
        ephemeral=True,
    )


# ---------------------------------------------------------------------------
# Commands — misc
# ---------------------------------------------------------------------------
@bot.tree.command(name="join", description="Join your voice channel")
async def join(interaction):
    await interaction.response.defer(ephemeral=True)
    vc = await require_voice(interaction)
    if vc:
        await interaction.followup.send(
            f"Connected to **{vc.channel.name}**.", ephemeral=True
        )


@bot.tree.command(name="rejoin", description="Move the bot to your current voice channel")
async def rejoin(interaction):
    await interaction.response.defer(ephemeral=True)
    vc = await require_voice(interaction)
    if vc:
        await interaction.followup.send(
            f"Moved to **{vc.channel.name}**.", ephemeral=True
        )


@bot.tree.command(name="dashboard", description="Open the interactive music dashboard")
async def dashboard(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    await interaction.response.send_message(
        embed=dashboard_embed(interaction.guild),
        view=MusicDashboard(),
    )


@bot.tree.command(name="ping", description="Check the bot's latency")
async def ping(interaction):
    latency = round(bot.latency * 1000)
    await interaction.response.send_message(
        embed=make_embed("🏓 Pong!", f"Gateway latency: **{latency} ms**", GREEN),
        ephemeral=True,
    )


@bot.tree.command(name="stats", description="Show bot statistics")
async def stats(interaction):
    uptime_sec = int(time.time() - START_TIME)
    h, rem = divmod(uptime_sec, 3600)
    m, s = divmod(rem, 60)
    e = make_embed("📊 RAJ MUSIC PRO • Stats", color=CYAN)
    e.add_field(name="Uptime", value=f"{h}h {m}m {s}s", inline=True)
    e.add_field(name="Guilds", value=str(len(bot.guilds)), inline=True)
    e.add_field(
        name="Active players",
        value=str(sum(1 for p in players.values() if p["current"])),
        inline=True,
    )
    e.add_field(name="Tracks played", value=str(STATS["tracks_played"]), inline=True)
    e.add_field(name="Searches", value=str(STATS["searches"]), inline=True)
    e.add_field(name="Latency", value=f"{round(bot.latency * 1000)} ms", inline=True)
    await interaction.response.send_message(embed=e)


@bot.tree.command(name="musicinfo", description="Show music player status and settings")
async def musicinfo(interaction):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    vc = interaction.guild.voice_client
    e = make_embed("🎛️ RAJ MUSIC PRO • System Status", color=CYAN)
    e.add_field(
        name="Voice",
        value=f"Connected to **{vc.channel.name}**" if vc and vc.is_connected() else "Disconnected",
        inline=True,
    )
    e.add_field(
        name="Playback",
        value="Paused" if vc and vc.is_paused() else ("Playing" if vc and vc.is_playing() else "Idle"),
        inline=True,
    )
    e.add_field(name="Volume", value=f"{round(p['volume'] * 100)}%", inline=True)
    e.add_field(name="Queue", value=f"{len(p['queue'])} waiting", inline=True)
    e.add_field(name="Loop", value=p["loop"].title(), inline=True)
    e.add_field(name="Autoplay", value="On" if p.get("autoplay") else "Off", inline=True)
    await interaction.response.send_message(embed=e, ephemeral=True)


@bot.tree.command(name="lyrics", description="Fetch lyrics for the current or given track")
@app_commands.describe(query="Optional song title; uses current track if empty")
async def lyrics(interaction, query: str = None):
    if not interaction.guild:
        return await interaction.response.send_message("Server only.", ephemeral=True)
    p = get_player(interaction.guild.id)
    if query:
        title, artist = query, None
    elif p["current"]:
        title, artist = p["current"]["title"], p["current"].get("channel")
    else:
        return await interaction.response.send_message(
            "Nothing is playing. Pass a song title.", ephemeral=True
        )
    await interaction.response.defer()
    text = await fetch_lyrics(title, artist)
    if not text:
        return await interaction.followup.send(
            embed=make_embed("🎤 Lyrics", f"No lyrics found for **{title}**.", YELLOW)
        )
    chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
    e = make_embed("🎤 Lyrics", f"**{title}**\n\n{chunks[0][:3900]}", PURPLE)
    await interaction.followup.send(embed=e)
    for extra in chunks[1:4]:
        await interaction.followup.send(embed=make_embed("🎤 Lyrics (cont.)", extra, PURPLE))


@bot.tree.command(name="healthcheck", description="Verify YouTube playback works")
async def healthcheck(interaction):
    await interaction.response.defer(ephemeral=True)
    try:
        track = await search_track("rick astley never gonna give you up", "healthcheck")
        await refresh_stream_url(track)
    except Exception as e:
        return await interaction.followup.send(
            embed=make_embed(
                "❌ Health Check Failed",
                f"`{type(e).__name__}: {e}`\n\n"
                "yt-dlp may need updating: `pip install -U yt-dlp`",
                RED,
            ),
            ephemeral=True,
        )
    await interaction.followup.send(
        embed=make_embed(
            "✅ Health Check Passed",
            f"Resolved **{track['title']}** — playback is working.",
            GREEN,
        ),
        ephemeral=True,
    )


@bot.tree.command(name="help", description="Show all music commands")
async def help_command(interaction):
    e = make_embed(
        "🎵 RAJ MUSIC PRO — Help",
        "Search and control music using slash commands.",
    )
    e.add_field(
        name="🎶 Playback",
        value="`/play` `/playnext` `/pause` `/resume` `/skip` `/stop` `/replay` `/previous` `/seek`",
        inline=False,
    )
    e.add_field(
        name="📜 Queue",
        value="`/queue` `/nowplaying` `/history` `/remove` `/move` `/skipto` `/clearqueue` `/shuffle`",
        inline=False,
    )
    e.add_field(
        name="🎛️ Controls",
        value="`/volume` `/loop` `/autoplay` `/join` `/leave` `/rejoin` `/dashboard`",
        inline=False,
    )
    e.add_field(
        name="ℹ️ Info",
        value="`/musicinfo` `/lyrics` `/stats` `/ping` `/healthcheck` `/help`",
        inline=False,
    )
    await interaction.response.send_message(embed=e, ephemeral=True)


# ---------------------------------------------------------------------------
# Run
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        log.info("Shutting down.")
