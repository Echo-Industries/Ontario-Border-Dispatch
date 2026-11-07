import discord
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web
import asyncio
import re
import os
import shutil
import logging
import datetime
import time
import uuid
import base64
from pathlib import Path
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from gtts import gTTS
from dotenv import load_dotenv

# --- DAVE E2EE Voice Support ---
# discord.py >= 2.7.0 detects `davey` automatically and handles the encryption.
try:
    import davey  # noqa: F401
    DAVEY_INSTALLED = True
except ImportError:
    DAVEY_INSTALLED = False

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
INDEX_PATH = BASE_DIR / "index.html"
LOGO_PATH = BASE_DIR / "Ontario Border Dispatch Logo.jpg"
KP_LOGO_PATH = BASE_DIR / "KryptonPeak Logo.png"

# --- Security & Tokens ---
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
MELONLY_API_KEY = os.getenv("MELONLY_API_KEY")
ERLC_SERVER_KEY = os.getenv("ERLC_SERVER_KEY")

_erlc_der_key = base64.b64decode("MCowBQYDK2VwAyEAjSICb9pp0kHizGQtdG8ySWsDChfGqi+gyFCttigBNOA=")
ERLC_PUBLIC_KEY = VerifyKey(_erlc_der_key[-32:])

# --- Channel Configuration ---
WAITING_ROOM_ID = 1567191463203446804

WORKER_CONFIGS = {
    "police": {"name": "CBSA Border Dispatch", "token": os.getenv("CBSA_BOT_TOKEN"), "vc_id": 1527738899374280704},
    "fire": {"name": "OBFS Border Dispatch", "token": os.getenv("OBFS_BOT_TOKEN"), "vc_id": 1527739186629710086},
    "dot": {"name": "MOT Border Dispatch", "token": os.getenv("MOT_BOT_TOKEN"), "vc_id": 1527739511310909663},
}

TEXT_CHANNELS = {
    "police": 1523672972764905492,
    "fire": 1523673131959717938,
    "dot": 1523673289791377448,
    "staff_log": 1568057421589643324,
}

ALLOWED_COMMAND_CHANNELS = [
    1521204407687975113,
    1523575437278576660,
    1522723233286328484,
]

MELONLY_API_URL = "https://api.melonly.xyz/api/v1"
ERLC_API_URL = "https://api.erlc.gg/v2/server"
# ER:LC v2 only includes the player list when Players=true is requested.
ERLC_PLAYERS_URL = f"{ERLC_API_URL}?Players=true"
EMERGENCY_TEAMS = ["police", "sheriff", "fire", "rescue", "transportation", "dot"]

# -vn: ignore any embedded cover art/video stream. -loglevel error: hide harmless bitrate warnings.
FFMPEG_KWARGS = {"options": "-vn -loglevel error"}
HTTP_TIMEOUT = aiohttp.ClientTimeout(total=10)


# --- TTS ---
def generate_tts_file(text, filename) -> bool:
    try:
        print(f"[AUDIO] Generating TTS for: {text}")
        gTTS(text=text, lang="en", slow=False).save(filename)
        return True
    except Exception as e:
        print(f"[AUDIO ERROR] Failed to generate TTS: {e}")
        return False


async def play_file(vc: discord.VoiceClient, filename: str) -> bool:
    """Plays an MP3 and waits until it finishes. Surfaces ffmpeg errors via `after`."""
    if not (os.path.exists(filename) and os.path.getsize(filename) > 100):
        print(f"[AUDIO ERROR] {filename} missing or too small, skipping.")
        return False

    while vc.is_playing():
        await asyncio.sleep(0.2)

    await wait_for_dave(vc)
    loop = asyncio.get_running_loop()
    done = asyncio.Event()

    def after(err):
        if err:
            print(f"[AUDIO ERROR] Playback error: {err}")
        loop.call_soon_threadsafe(done.set)

    vc.play(discord.FFmpegPCMAudio(filename, **FFMPEG_KWARGS), after=after)
    try:
        await asyncio.wait_for(done.wait(), timeout=180)
    except asyncio.TimeoutError:
        print("[AUDIO ERROR] Playback timed out, stopping.")
        vc.stop()
    return True


async def wait_for_dave(vc, timeout=8.0):
    """Logs DAVE state and waits briefly for the E2EE session to be ready before sending audio."""
    conn = getattr(vc, "_connection", None)
    sess = getattr(conn, "dave_session", None)
    print(f"[DAVE] protocol={getattr(conn, 'dave_protocol_version', '?')} session={'yes' if sess else 'NONE'} ready={getattr(sess, 'ready', '?')}")
    if sess is None:
        return
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if getattr(sess, "ready", True):
            return
        await asyncio.sleep(0.25)
    print("[DAVE] Session still not ready; audio may be silent.")


def safe_remove(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


# --- ER:LC Player Fetch (shared + cached so one scan serves every member) ---
ERLC_CACHE_TTL = 8
_erlc_cache = {"ts": 0.0, "players": None}
_erlc_lock = asyncio.Lock()


def parse_player_name(p: dict) -> str:
    return str(p.get("Player", "")).split(":")[0]


async def fetch_erlc_players():
    """Returns the live player list, or None if the API call failed."""
    if not ERLC_SERVER_KEY:
        return None
    async with _erlc_lock:
        if _erlc_cache["players"] is not None and time.monotonic() - _erlc_cache["ts"] < ERLC_CACHE_TTL:
            return _erlc_cache["players"]
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                async with session.get(ERLC_PLAYERS_URL, headers={"server-key": ERLC_SERVER_KEY}) as resp:
                    if resp.status != 200:
                        print(f"[ERLC ERROR] HTTP {resp.status} from player fetch.")
                        return None
                    data = await resp.json(content_type=None)
        except Exception as e:
            print(f"[ERLC ERROR] Player fetch failed: {e}")
            return None
        players = data.get("Players") or []
        _erlc_cache.update(ts=time.monotonic(), players=players)
        return players


# --- Melonly Lookup (robust extraction + cache) ---
MELONLY_TTL_FOUND = 600
MELONLY_TTL_MISSING = 60
_melonly_cache = {}  # discord_id -> (timestamp, username | None)
_melonly_snapshot = {"ts": 0.0, "map": {}}
_melonly_lock = asyncio.Lock()
_melonly_debug_printed = False
_melonly_unusable_until = 0.0  # backoff when the API exposes no Roblox/Discord link data


def _str(v):
    return v.strip() if isinstance(v, str) and v.strip() else None


def extract_roblox_username(rec):
    """Checks every shape Melonly has been seen to use. Safe against null/non-dict values."""
    if not isinstance(rec, dict):
        return None
    for k in ("robloxUsername", "robloxName", "roblox_username"):
        if _str(rec.get(k)):
            return _str(rec.get(k))
    for k in ("robloxAccount", "roblox", "user", "account", "profile"):
        sub = rec.get(k)
        if isinstance(sub, dict):
            for sk in ("robloxUsername", "username", "name"):
                if _str(sub.get(sk)):
                    return _str(sub.get(sk))
    return _str(rec.get("username"))  # last resort


def extract_discord_id(rec):
    if not isinstance(rec, dict):
        return None
    for k in ("discordId", "discord_id", "discordID"):
        if rec.get(k):
            return str(rec[k])
    for k in ("user", "discord", "account"):
        sub = rec.get(k)
        if isinstance(sub, dict):
            for sk in ("discordId", "discord_id", "id"):
                if sub.get(sk):
                    return str(sub[sk])
    return None


def _unwrap(data):
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    if isinstance(data, list):
        return data
    return data


async def _melonly_get(session, path, params=None):
    headers = {"Authorization": f"Bearer {MELONLY_API_KEY}"}
    async with session.get(f"{MELONLY_API_URL}{path}", headers=headers, params=params) as resp:
        if resp.status == 200:
            return await resp.json(content_type=None)
        if resp.status == 429:
            print("[MELONLY] Rate limited (429).")
        return None


async def _refresh_melonly_snapshot(session):
    """Pages through /server/members and maps discord_id -> roblox username."""
    global _melonly_debug_printed
    found, page, total = {}, 1, 1
    while page <= min(total, 10):
        data = await _melonly_get(session, "/server/members", {"page": page})
        if data is None:
            break
        if isinstance(data, dict):
            total = int(data.get("totalPages", 1) or 1)
        records = _unwrap(data)
        if isinstance(records, dict):
            records = [records]
        for rec in records or []:
            did, name = extract_discord_id(rec), extract_roblox_username(rec)
            if did and name:
                found[did] = name
        if records and not _melonly_debug_printed and not found:
            _melonly_debug_printed = True
            first = records[0]
            print(f"[MELONLY DEBUG] No usable record. Sample keys: {list(first.keys()) if isinstance(first, dict) else type(first)}")
        if records and not found:
            global _melonly_unusable_until
            _melonly_unusable_until = time.monotonic() + 3600
            break
        page += 1
    _melonly_snapshot.update(ts=time.monotonic(), map=found)


async def fetch_melonly_username(discord_id: int):
    key = str(discord_id)
    if time.monotonic() < _melonly_unusable_until:
        return None
    async with _melonly_lock:
        cached = _melonly_cache.get(key)
        if cached:
            ttl = MELONLY_TTL_FOUND if cached[1] else MELONLY_TTL_MISSING
            if time.monotonic() - cached[0] < ttl:
                return cached[1]

        username = None
        try:
            async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
                # 1. Direct lookup by Discord ID
                data = await _melonly_get(session, f"/server/members/discord/{key}")
                rec = _unwrap(data) if data is not None else None
                if isinstance(rec, list):
                    rec = next((r for r in rec if extract_discord_id(r) == key), None)
                username = extract_roblox_username(rec)

                # 2. Fall back to scanning the member list and matching Discord IDs ourselves
                if not username:
                    if time.monotonic() - _melonly_snapshot["ts"] > MELONLY_TTL_FOUND or key not in _melonly_snapshot["map"]:
                        if time.monotonic() - _melonly_snapshot["ts"] > MELONLY_TTL_MISSING:
                            await _refresh_melonly_snapshot(session)
                    username = _melonly_snapshot["map"].get(key)
        except Exception as e:
            print(f"[SYNC ERROR] Melonly request failed: {e}")
            return None  # don't cache failures

        _melonly_cache[key] = (time.monotonic(), username)
        return username


# --- Combined Lookup ---
def _name_tokens(member: discord.Member):
    names = {member.display_name, member.name}
    if member.global_name:
        names.add(member.global_name)
    tokens = set()
    for n in names:
        tokens.update(t for t in re.split(r"[^a-z0-9_]+", n.lower()) if t)
    return tokens


def _player_dict(p):
    return {
        "username": parse_player_name(p),
        "team": p.get("Team") or "Civilian",
        "callsign": p.get("Callsign") or None,
    }


async def fetch_user_game_data(member: discord.Member):
    """Returns (data, status). status: success | not_in_game | erlc_error"""
    players = await fetch_erlc_players()
    if players is None:
        return None, "erlc_error"

    roblox_username = await fetch_melonly_username(member.id)

    if roblox_username:
        for p in players:
            if parse_player_name(p).lower() == roblox_username.lower():
                return _player_dict(p), "success"

    # Fallback: exact token match against Discord names ("[Callsign] | Username" splits cleanly)
    tokens = _name_tokens(member)
    for p in players:
        name = parse_player_name(p)
        if len(name) >= 3 and name.lower() in tokens:
            print(f"[SYNC INFO] Melonly fallback used. Matched '{name}' via Discord profile.")
            return _player_dict(p), "success"

    return None, "not_in_game"


# --- In-Game PM Utility ---
async def send_in_game_pm(player_name: str, message: str):
    if not ERLC_SERVER_KEY:
        return
    try:
        async with aiohttp.ClientSession(timeout=HTTP_TIMEOUT) as session:
            headers = {"server-key": ERLC_SERVER_KEY, "Content-Type": "application/json"}
            payload = {"command": f":pm {player_name} {message}"}
            async with session.post(f"{ERLC_API_URL}/command", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    print(f"[API] Sent in-game PM to {player_name}")
                else:
                    print(f"[API ERROR] Failed PM to {player_name}. HTTP {resp.status}")
    except Exception as e:
        print(f"[API ERROR] PM exception: {e}")


# --- Worker Bot Class ---
class DispatchWorker(discord.Client):
    def __init__(self, dept_name, vc_id, bot_name):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(intents=intents)
        self.dept_name = dept_name
        self.vc_id = vc_id
        self.bot_name = bot_name
        self.audio_queue = asyncio.Queue()
        self.connect_lock = asyncio.Lock()
        self._last_error = None
        self._processor_task = None
        self._connect_count = 0
        self._last_connect = 0.0

    async def setup_hook(self):
        self.connection_watcher.start()
        self._processor_task = asyncio.create_task(self.audio_processor())
        print(f"[{self.bot_name}] Online and initialized.")

    async def _get_channel(self):
        channel = self.get_channel(self.vc_id)
        if channel:
            return channel
        try:
            return await self.fetch_channel(self.vc_id)
        except Exception:
            return None

    async def ensure_connected(self, channel):
        async with self.connect_lock:
            vc = channel.guild.voice_client
            if vc and vc.is_connected():
                if vc.channel.id != channel.id:
                    await vc.move_to(channel)
                return vc
            if vc:
                # discord.py may be mid auto-reconnect; give it time before tearing it down
                for _ in range(30):
                    await asyncio.sleep(0.5)
                    if vc.is_connected():
                        return vc
                print(f"[{self.bot_name}] Voice client stuck disconnected for 15s; recreating.")
                await vc.disconnect(force=True)
            vc = await channel.connect(timeout=20.0, reconnect=True)
            await asyncio.sleep(1.0)  # let the DAVE/E2EE handshake settle before audio
            now = time.monotonic()
            self._connect_count += 1
            gap = f"{now - self._last_connect:.0f}s since last" if self._last_connect else "first"
            self._last_connect = now
            print(f"[{self.bot_name}] ✅ Connected to RTO (#{self._connect_count}, {gap}).")
            return vc

    async def on_voice_state_update(self, member, before, after):
        # Shows whether Discord/someone is removing THIS bot from the channel.
        if self.user and member.id == self.user.id and before.channel != after.channel:
            print(f"[{self.bot_name}] Voice state: {before.channel} -> {after.channel}")

    @tasks.loop(seconds=15)
    async def connection_watcher(self):
        try:
            channel = await self._get_channel()
            if channel:
                await self.ensure_connected(channel)
                self._last_error = None
        except Exception as e:
            msg = f"{type(e).__name__}: {e}"
            if msg != self._last_error:  # avoid log spam every 15s
                self._last_error = msg
                print(f"[{self.bot_name}] Voice connect failed: {msg}")

    @connection_watcher.before_loop
    async def before_watcher(self):
        await self.wait_until_ready()

    async def play_dispatch(self, pre_text, call_text):
        channel = await self._get_channel()
        if not channel:
            print(f"[{self.bot_name}] RTO channel not found; dropping dispatch.")
            return
        vc = await self.ensure_connected(channel)

        uid = uuid.uuid4().hex
        pre_file = f"pre_dispatch_{self.dept_name}_{uid}.mp3"
        call_file = f"call_dispatch_{self.dept_name}_{uid}.mp3"
        me = channel.guild.me
        try:
            await asyncio.gather(
                asyncio.to_thread(generate_tts_file, pre_text, pre_file),
                asyncio.to_thread(generate_tts_file, call_text, call_file),
            )
            try:
                await channel.set_permissions(me, priority_speaker=True)
            except discord.HTTPException:
                pass
            try:
                await play_file(vc, pre_file)
                await asyncio.sleep(2.5)
                await play_file(vc, call_file)
            finally:
                try:
                    await channel.set_permissions(me, priority_speaker=None)
                except discord.HTTPException:
                    pass
        finally:
            safe_remove(pre_file, call_file)

    async def audio_processor(self):
        await self.wait_until_ready()
        while True:
            pre_text, call_text = await self.audio_queue.get()
            try:
                await self.play_dispatch(pre_text, call_text)
            except Exception as e:  # never let one bad call kill the queue
                print(f"[{self.bot_name}] Dispatch playback failed: {type(e).__name__}: {e}")
            finally:
                self.audio_queue.task_done()


# --- Initialize Worker Bots ---
workers = {}
for _dept, _config in WORKER_CONFIGS.items():
    workers[_dept] = DispatchWorker(dept_name=_dept, vc_id=_config["vc_id"], bot_name=_config["name"])


# --- Main Bot Class ---
class MainBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)
        self.start_time = time.time()
        self.waiting_room_empty_minutes = 0
        self.cached_player_teams = {}
        self.first_scan_complete = False
        self.warning_lock = asyncio.Lock()
        self.syncing = set()
        self._announced = False

    async def setup_hook(self):
        await self.tree.sync()
        self.auto_sync_loop.start()
        self.waiting_room_timeout_loop.start()
        self.team_monitor_loop.start()

        print(f"[MAIN] Ontario Border Dispatch online. (discord.py v{discord.__version__})")
        if not DAVEY_INSTALLED:
            print("[MAIN] CRITICAL WARNING: 'davey' is missing. E2EE voice will fail (silent audio)!")

    async def on_ready(self):
        if self._announced:  # on_ready fires on every reconnect; announce once
            return
        self._announced = True
        embed = discord.Embed(
            description="## <:signal:1568381854497906738> Dispatch System Online\nOntario Border Dispatch and all departmental worker bots are now active and monitoring frequencies.",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc),
        )
        embed.set_footer(text="Ontario Border Dispatch System")
        for channel_id in TEXT_CHANNELS.values():
            channel = self.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=embed)
                except discord.HTTPException:
                    pass

    async def play_waiting_room_warning(self, text: str):
        channel = self.get_channel(WAITING_ROOM_ID)
        if not channel:
            return
        async with self.warning_lock:  # serialize: concurrent connect()/play() calls raise errors
            self.waiting_room_empty_minutes = 0
            filename = f"main_warning_{uuid.uuid4().hex}.mp3"
            try:
                if not await asyncio.to_thread(generate_tts_file, text, filename):
                    return
                vc = channel.guild.voice_client
                if vc is None or not vc.is_connected():
                    if vc:
                        await vc.disconnect(force=True)
                    vc = await channel.connect(timeout=20.0, reconnect=True)
                    await asyncio.sleep(1.0)  # DAVE handshake settle
                elif vc.channel.id != channel.id:
                    await vc.move_to(channel)
                if await play_file(vc, filename):
                    await asyncio.sleep(1)
            except Exception as e:
                print(f"[WARNING AUDIO ERROR] {type(e).__name__}: {e}")
            finally:
                safe_remove(filename)

    @tasks.loop(minutes=1)
    async def waiting_room_timeout_loop(self):
        try:
            channel = self.get_channel(WAITING_ROOM_ID)
            if not channel or not channel.guild.voice_client:
                return
            if any(not m.bot for m in channel.members):
                self.waiting_room_empty_minutes = 0
                return
            self.waiting_room_empty_minutes += 1
            if self.waiting_room_empty_minutes >= 15:
                await channel.guild.voice_client.disconnect()
                self.waiting_room_empty_minutes = 0
        except Exception as e:
            print(f"[LOOP ERROR] waiting_room_timeout_loop: {e}")

    @tasks.loop(seconds=30)
    async def team_monitor_loop(self):
        try:
            players = await fetch_erlc_players()
            if players is None:
                return
            msg = "DISPATCH: Please wait 45 to 60 seconds for the database to sync before joining the Discord RTO."
            current_teams = {}
            for p in players:
                name = parse_player_name(p)
                team = str(p.get("Team") or "Civilian").lower()
                current_teams[name] = team
                is_emergency = any(k in team for k in EMERGENCY_TEAMS)
                if name in self.cached_player_teams:
                    if self.cached_player_teams[name] != team and is_emergency:
                        await send_in_game_pm(name, msg)
                elif self.first_scan_complete and is_emergency:
                    await send_in_game_pm(name, msg)
            self.cached_player_teams = current_teams
            self.first_scan_complete = True
        except Exception as e:
            print(f"[LOOP ERROR] team_monitor_loop: {e}")

    async def kick_to_waiting_room(self, member, waiting_room, reason, warning):
        # They may have left voice while we were doing API lookups.
        if not member.voice or not member.voice.channel or "RTO" not in member.voice.channel.name.upper():
            return
        try:
            await member.move_to(waiting_room, reason=reason)
        except discord.HTTPException as e:
            print(f"[SYNC ERROR] Could not move {member.name}: {e}")
            return
        await self.play_waiting_room_warning(warning)

    async def sync_member_rto(self, member, waiting_room):
        if member.id in self.syncing:  # join event + 60s loop can overlap
            return
        self.syncing.add(member.id)
        try:
            await self._sync_member_rto(member, waiting_room)
        except Exception as e:
            print(f"[SYNC ERROR] {member.name}: {type(e).__name__}: {e}")
        finally:
            self.syncing.discard(member.id)

    async def _sync_member_rto(self, member, waiting_room):
        data, status = await fetch_user_game_data(member)

        if status == "erlc_error":
            # API hiccup is NOT the player's fault; never kick on it.
            print(f"[SYNC] ER:LC unavailable, skipping enforcement for {member.name}.")
            return

        if status != "success" or not data:
            await self.kick_to_waiting_room(
                member, waiting_room, "Not verified or left game",
                "Attention player: You must be verified on Melonly and logged in. Additionally, you must be on the appropriate team in-game to join the team's RTO.")
            return

        team, callsign, username = data["team"].lower(), data["callsign"], data["username"]

        if not any(k in team for k in EMERGENCY_TEAMS):
            await self.kick_to_waiting_room(
                member, waiting_room, "Switched to Civilian",
                "Frequency change: You were moved to waiting room because you switched to the civilian team.")
        elif not callsign:
            await self.kick_to_waiting_room(
                member, waiting_room, "Dropped callsign",
                "Attention unit: Please set your in-game callsign before entering RTO.")
        else:
            new_nick = f"{callsign} | {username}"[:32]  # Discord nickname limit
            if member.nick != new_nick:
                try:
                    await member.edit(nick=new_nick, reason="Callsign sync")
                    print(f"[SYNC UPDATE] Updated {member.name} to {new_nick}")
                except discord.Forbidden:
                    print(f"[SYNC] Cannot edit {member.name}: higher role than the bot (or server owner).")
                    await send_in_game_pm(username, f"DISPATCH: I cannot set your Discord nickname. Please change it manually to: {new_nick}")
                    await self.kick_to_waiting_room(
                        member, waiting_room, "Bot lacks permission to enforce callsign formatting",
                        "Attention unit: Your Discord role is too high for me to set your callsign. Please set your nickname manually.")
                except discord.HTTPException as e:
                    print(f"[SYNC ERROR] Nickname edit failed for {member.name}: {e}")

    @tasks.loop(seconds=60)
    async def auto_sync_loop(self):
        try:
            for guild in self.guilds:
                waiting_room = guild.get_channel(WAITING_ROOM_ID)
                if not waiting_room:
                    continue
                for vc in guild.voice_channels:
                    if vc.id == WAITING_ROOM_ID or "RTO" not in vc.name.upper():
                        continue
                    for member in list(vc.members):
                        if not member.bot:
                            await self.sync_member_rto(member, waiting_room)
        except Exception as e:
            print(f"[LOOP ERROR] auto_sync_loop: {e}")

    @auto_sync_loop.before_loop
    @team_monitor_loop.before_loop
    @waiting_room_timeout_loop.before_loop
    async def before_loops(self):
        await self.wait_until_ready()


main_bot = MainBot()


# --- Bot Info ---
def logo_file():
    return discord.File(LOGO_PATH, filename="bot_logo.jpg") if LOGO_PATH.exists() else None


def get_botinfo_embed():
    unix_start = int(main_bot.start_time)
    desc = (
        "## <:bot:1568382041672917193> Bot Information\n"
        "The core routing and enforcement system for the **Ontario Border Roleplay** communications network.\n\n"
        f"**<:wifihigh:1568381086608662638> Uptime:** <t:{unix_start}:R>\n"
        f"**<:signal:1568381854497906738> Network Ping:** {round(main_bot.latency * 1000)}ms\n"
        "**<:database:1568381596363657216> Architecture:** `discord.py` & `aiohttp` web server\n"
        f"**<:settings:1568382499187462306> Worker Nodes:** {len(workers)} active departmental bots\n"
        f"**<:mic:1568379901978091651> Voice Protocol:** `davey` (E2EE {'Enabled' if DAVEY_INSTALLED else 'MISSING'})\n"
    )
    embed = discord.Embed(description=desc, color=discord.Color.blurple())
    if LOGO_PATH.exists():
        embed.set_thumbnail(url="attachment://bot_logo.jpg")
    embed.add_field(name="<:github:1568382792385962044> Developer", value="[Krypton Peak Technologies](https://kryptonpeak.com)", inline=True)
    embed.add_field(name="<:shield:1568383105188892823> Monitoring", value=f"{len(EMERGENCY_TEAMS)} Emergency Teams", inline=True)
    embed.set_footer(text="Ontario Border Dispatch System")
    return embed


@main_bot.tree.command(name="botinfo", description="Displays system information about the dispatch bot")
async def botinfo(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_COMMAND_CHANNELS:
        await interaction.response.send_message(f"❌ Commands can only be used in designated bot channels (e.g., <#{ALLOWED_COMMAND_CHANNELS[0]}>).", ephemeral=True)
        return
    file = logo_file()
    kwargs = {"embed": get_botinfo_embed()}
    if file:
        kwargs["file"] = file
    await interaction.response.send_message(**kwargs)


@main_bot.tree.command(name="botstatus", description="Displays the live connection status of all dispatcher bots")
async def botstatus(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_COMMAND_CHANNELS:
        await interaction.response.send_message(f"❌ Commands can only be used in designated bot channels (e.g., <#{ALLOWED_COMMAND_CHANNELS[0]}>).", ephemeral=True)
        return

    desc = "## <:signal:1568381854497906738> Dispatch Network Status\nLive connection metrics and monitoring statuses for the main router and all departmental workers."
    embed = discord.Embed(description=desc, color=discord.Color.green())
    embed.add_field(name="Main Dispatch (Sync & Routing)", value=f"<:goodsignal:1568399560622211143> Online ({round(main_bot.latency * 1000)}ms)", inline=False)

    for dept, worker in workers.items():
        if worker.is_ready():
            status = f"<:goodsignal:1568399560622211143> Online ({round(worker.latency * 1000)}ms) - Monitoring <#{worker.vc_id}>"
            channel = worker.get_channel(worker.vc_id)
            vc = channel.guild.voice_client if channel else None
            if not vc or not vc.is_connected():
                status += "\n⚠️ Not connected to voice"
        else:
            status = "🔴 Offline / Disconnected"
            embed.color = discord.Color.red()
        embed.add_field(name=f"{worker.bot_name} ({dept.upper()})", value=status, inline=False)

    embed.add_field(name="<:world:1568399069385203722> Webhook Server", value="<:port:1568429510666231938> Online (Port 8080)", inline=False)
    embed.set_footer(text="Ontario Border Dispatch System")
    await interaction.response.send_message(embed=embed)


@main_bot.event
async def on_message(message):
    if message.author.bot:
        return
    if main_bot.user.mentioned_in(message) and not message.mention_everyone:
        file = logo_file()
        kwargs = {"embed": get_botinfo_embed()}
        if file:
            kwargs["file"] = file
        await message.channel.send(**kwargs)
    await main_bot.process_commands(message)


@main_bot.event
async def on_voice_state_update(member, before, after):
    if member.bot:
        return
    # Only react to actual channel changes (not mute/deafen toggles)
    if before.channel == after.channel:
        return
    if after.channel and after.channel.id != WAITING_ROOM_ID and "RTO" in after.channel.name.upper():
        waiting_room = member.guild.get_channel(WAITING_ROOM_ID)
        if waiting_room:
            await main_bot.sync_member_rto(member, waiting_room)


# --- Web Server ---
def file_handler(path: Path):
    async def handler(request):
        if not path.exists():
            return web.Response(status=404, text="Not found")
        return web.FileResponse(path)
    return handler


def verify_erlc_signature(signature: str, timestamp: str, body: bytes) -> bool:
    """ER:LC currently sends Base64 Ed25519 signatures; older deliveries used Hex. Accept both."""
    message = timestamp.encode("utf-8") + body
    candidates = []
    try:
        candidates.append(bytes.fromhex(signature))
    except ValueError:
        pass
    try:
        candidates.append(base64.b64decode(signature, validate=True))
    except ValueError:
        pass
    for sig in candidates:
        if len(sig) != 64:
            continue
        try:
            ERLC_PUBLIC_KEY.verify(message, sig)
            return True
        except BadSignatureError:
            continue
    return False


async def handle_911_event(payload: dict):
    call_caller = str(payload.get("Caller", "Unknown Caller"))
    call_location = str(payload.get("Location", "Unknown Location"))
    call_details = str(payload.get("Details", "No details provided"))
    dept = str(payload.get("Department") or "police").lower()

    if dept == "transportation":
        dept = "dot"
    if dept == "sheriff":
        dept = "police"

    print(f"[DISPATCH] Routing {dept.upper()} call from {call_caller}")

    pre_text = "There is an incoming call, please silence the RTO."
    call_text = f"Caller: {call_caller}. Description: {call_details}. Location: {call_location}."

    target = workers.get(dept) or workers.get("police")
    if target:
        await target.audio_queue.put((pre_text, call_text))

    embed = discord.Embed(
        description=f"## <:phonecall:1568426067620798526> 911 Dispatch - {dept.upper()}",
        color=discord.Color.red() if dept == "fire" else discord.Color.blue(),
        timestamp=datetime.datetime.now(datetime.timezone.utc),
    )
    embed.add_field(name="Caller", value=call_caller, inline=True)
    embed.add_field(name="Location", value=call_location, inline=True)
    embed.add_field(name="Details", value=call_details, inline=False)
    embed.set_footer(text="Ontario Border Roleplay System")

    for key in ([dept] if dept in TEXT_CHANNELS else []) + ["staff_log"]:
        channel = main_bot.get_channel(TEXT_CHANNELS[key])
        if channel:
            try:
                await channel.send(embed=embed)
            except discord.HTTPException as e:
                print(f"[WEBHOOK] Could not post to {key} log: {e}")


async def handle_erlc_webhook(request):
    try:
        raw_body = await request.read()
        signature = request.headers.get("X-Signature-Ed25519")
        timestamp = request.headers.get("X-Signature-Timestamp")
        if not signature or not timestamp:
            return web.Response(status=400, text="Missing signature headers")

        if not verify_erlc_signature(signature, timestamp, raw_body):
            print("[WEBHOOK ERROR] Invalid Ed25519 signature.")
            return web.Response(status=401, text="Invalid signature")

        data = await request.json()
        events = data.get("events", [data]) if isinstance(data, dict) else data

        for event_payload in events:
            # One malformed event must not drop the rest of the batch.
            try:
                if not isinstance(event_payload, dict):
                    continue
                event_type = event_payload.get("event", event_payload.get("Type", "Unknown"))
                if event_type == "WebhookProbe":
                    print("[WEBHOOK] Received ER:LC probe. Validated and acknowledged.")
                    continue

                payload = event_payload.get("data", event_payload)
                if isinstance(payload, dict) and isinstance(payload.get("Data"), dict):
                    payload = payload["Data"]
                if not isinstance(payload, dict):
                    continue

                if event_type == "Call911" or "Caller" in payload:
                    await handle_911_event(payload)
            except Exception as e:
                print(f"[WEBHOOK EVENT ERROR] {type(e).__name__}: {e}")

        return web.Response(status=200, text="Dispatched")
    except Exception as e:
        print(f"[WEBHOOK CRITICAL ERROR] {e}")
        return web.Response(status=400, text="Bad Request")


async def start_webhook_server():
    app = web.Application()
    app.router.add_get("/", file_handler(INDEX_PATH))
    app.router.add_get("/Ontario Border Dispatch Logo.jpg", file_handler(LOGO_PATH))
    app.router.add_get("/KryptonPeak Logo.png", file_handler(KP_LOGO_PATH))
    app.router.add_post("/erlc-webhook", handle_erlc_webhook)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", 8080).start()
    print("[SERVER] Webhook server listening on port 8080")


# --- Startup ---
async def run_bot(name, client, token):
    try:
        await client.start(token)
    except discord.LoginFailure:
        print(f"[STARTUP ERROR] {name}: invalid token.")
    except Exception as e:
        print(f"[STARTUP ERROR] {name} crashed: {type(e).__name__}: {e}")


async def main():
    discord.utils.setup_logging(level=logging.INFO)
    if os.getenv("VOICE_DEBUG"):
        logging.getLogger("discord.voice_state").setLevel(logging.DEBUG)
        logging.getLogger("discord.voice_client").setLevel(logging.DEBUG)
    if not shutil.which("ffmpeg"):
        print("[STARTUP] CRITICAL: ffmpeg not found on PATH. No audio will play.")
    for var, val in (("MAIN_BOT_TOKEN", MAIN_BOT_TOKEN), ("ERLC_SERVER_KEY", ERLC_SERVER_KEY), ("MELONLY_API_KEY", MELONLY_API_KEY)):
        if not val:
            print(f"[STARTUP] WARNING: {var} is not set.")
    if not MAIN_BOT_TOKEN:
        return

    await start_webhook_server()

    jobs = [run_bot("Main bot", main_bot, MAIN_BOT_TOKEN)]
    for dept, config in WORKER_CONFIGS.items():
        if config["token"]:
            jobs.append(run_bot(config["name"], workers[dept], config["token"]))
        else:
            print(f"[STARTUP] WARNING: no token for {config['name']}; worker disabled.")
    await asyncio.gather(*jobs)


if __name__ == "__main__":
    asyncio.run(main())