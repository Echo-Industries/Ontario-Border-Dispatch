import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from aiohttp import web
import asyncio
import re
import os
import datetime
import time
import uuid
import base64
from nacl.signing import VerifyKey
from nacl.exceptions import BadSignatureError
from gtts import gTTS
from dotenv import load_dotenv

# --- DAVE E2EE Voice Support ---
# discord.py automatically detects this import and handles the encryption natively.
try:
    import davey
    DAVEY_INSTALLED = True
except ImportError:
    DAVEY_INSTALLED = False

# Load environment variables from .env file
load_dotenv()

# --- Security & Tokens ---
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
MELONLY_API_KEY = os.getenv("MELONLY_API_KEY")
ERLC_SERVER_KEY = os.getenv("ERLC_SERVER_KEY")

_erlc_der_key = base64.b64decode("MCowBQYDK2VwAyEAjSICb9pp0kHizGQtdG8ySWsDChfGqi+gyFCttigBNOA=")
ERLC_PUBLIC_KEY = VerifyKey(_erlc_der_key[-32:])

# --- Channel Configuration ---
WAITING_ROOM_ID = 1567191463203446804  

WORKER_CONFIGS = {
    "police": {
        "name": "CBSA Border Dispatch",
        "token": os.getenv("CBSA_BOT_TOKEN"),
        "vc_id": 1527738899374280704
    },
    "fire": {
        "name": "OBFS Border Dispatch",
        "token": os.getenv("OBFS_BOT_TOKEN"),
        "vc_id": 1527739186629710086
    },
    "dot": {
        "name": "MOT Border Dispatch",
        "token": os.getenv("MOT_BOT_TOKEN"),
        "vc_id": 1527739511310909663
    }
}

TEXT_CHANNELS = {
    "police": 1523672972764905492,  
    "fire": 1523673131959717938,    
    "dot": 1523673289791377448,     
    "staff_log": 1568057421589643324 
}

ALLOWED_COMMAND_CHANNELS = [
    1521204407687975113, 
    1523575437278576660, 
    1522723233286328484  
]

MELONLY_API_URL = "https://api.melonly.xyz/api/v1"
ERLC_API_URL = "https://api.erlc.gg/v2/server"
EMERGENCY_TEAMS = ["police", "sheriff", "fire", "rescue", "transportation", "dot"]

# --- Thread-Safe TTS Generator ---
def generate_tts_file(text, filename):
    try:
        print(f"[AUDIO] Generating TTS for: {text}")
        tts = gTTS(text=text, lang='en', slow=False)
        tts.save(filename)
    except Exception as e:
        print(f"[AUDIO ERROR] Failed to generate TTS: {e}")

# --- Robust API Fetcher Utility ---
async def fetch_user_game_data(member: discord.Member):
    roblox_username = None
    
    # 1. Try Melonly Fetch
    try:
        async with aiohttp.ClientSession() as session:
            mel_headers = {"Authorization": f"Bearer {MELONLY_API_KEY}"}
            # Attempting to force Melonly to expand the User data object
            async with session.get(f"{MELONLY_API_URL}/server/members?discordId={member.id}&expand=user,robloxAccount", headers=mel_headers) as resp:
                if resp.status == 200:
                    mel_data = await resp.json()
                    members = mel_data.get("data", [])
                    if members:
                        u = members[0]
                        roblox_username = (
                            u.get("robloxUsername") or 
                            u.get("robloxName") or 
                            u.get("robloxAccount", {}).get("username") or 
                            u.get("user", {}).get("username") or 
                            u.get("username")
                        )
    except Exception as e:
        print(f"[SYNC ERROR] Melonly Request Failed: {e}")

    # 2. Check ER:LC Server (With Discord Nickname Fallback)
    try:
        async with aiohttp.ClientSession() as session:
            erlc_headers = {"Server-Key": ERLC_SERVER_KEY}
            async with session.get(ERLC_API_URL, headers=erlc_headers) as resp:
                if resp.status != 200: return None, "erlc_error"
                erlc_data = await resp.json()
                players = erlc_data.get("Players", [])

                # Fast Match: If Melonly successfully provided the exact username
                if roblox_username:
                    for p in players:
                        if p.get("Player", "").split(":")[0].lower() == roblox_username.lower():
                            return {"username": p.get("Player", "").split(":")[0], "team": p.get("Team", "Civilian"), "callsign": p.get("Callsign", None)}, "success"

                # Fallback Match: Melonly failed, so check if any live ER:LC player's name is hiding inside the user's Discord Name
                discord_names = [member.display_name.lower(), member.name.lower()]
                if member.global_name: discord_names.append(member.global_name.lower())

                for p in players:
                    p_name = p.get("Player", "").split(":")[0]
                    p_name_lower = p_name.lower()
                    if any(p_name_lower in d_name for d_name in discord_names):
                        print(f"[SYNC INFO] Melonly fallback used. Matched '{p_name}' via Discord profile.")
                        return {"username": p_name, "team": p.get("Team", "Civilian"), "callsign": p.get("Callsign", None)}, "success"
                
                return None, "not_in_game"
    except Exception as e:
        print(f"[SYNC ERROR] ERLC Request Failed: {e}")
        return None, "erlc_error"

# --- In-Game PM Utility ---
async def send_in_game_pm(player_name: str, message: str):
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Server-Key": ERLC_SERVER_KEY, "Content-Type": "application/json"}
            payload = {"command": f":pm {player_name} {message}"}
            async with session.post(f"{ERLC_API_URL}/command", headers=headers, json=payload) as resp:
                if resp.status == 200:
                    print(f"[API] Successfully sent in-game PM to {player_name}")
                else:
                    print(f"[API ERROR] Failed to send PM to {player_name}. HTTP {resp.status}")
    except Exception as e:
        print(f"[API ERROR] PM Exception: {e}")

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

    async def setup_hook(self):
        self.connection_watcher.start()
        self.loop.create_task(self.audio_processor())
        print(f"[{self.bot_name}] Online and initialized.")

    @tasks.loop(seconds=15)
    async def connection_watcher(self):
        channel = self.get_channel(self.vc_id)
        if not channel:
            try:
                channel = await self.fetch_channel(self.vc_id)
            except Exception:
                return
            
        vc = channel.guild.voice_client
        if not vc or not vc.is_connected():
            try:
                await channel.connect()
                print(f"[{self.bot_name}] ✅ Successfully connected to RTO.")
            except Exception:
                pass # Fail silently, discord rate limits connection attempts dynamically

    async def audio_processor(self):
        await self.wait_until_ready()
        
        while True:
            pre_text, call_text = await self.audio_queue.get()
            channel = self.get_channel(self.vc_id)
            if not channel: continue
            
            vc = channel.guild.voice_client
            me = channel.guild.me
            
            if not vc or not vc.is_connected():
                self.audio_queue.task_done()
                continue

            uid = uuid.uuid4().hex
            pre_filename = f"pre_dispatch_{self.dept_name}_{uid}.mp3"
            call_filename = f"call_dispatch_{self.dept_name}_{uid}.mp3"
            
            await asyncio.to_thread(generate_tts_file, pre_text, pre_filename)
            await asyncio.to_thread(generate_tts_file, call_text, call_filename)

            while vc.is_playing(): await asyncio.sleep(0.5)

            try:
                await channel.set_permissions(me, priority_speaker=True)
            except discord.Forbidden:
                pass

            if os.path.exists(pre_filename) and os.path.getsize(pre_filename) > 100:
                # -loglevel error hides the bitrate warnings but exposes real crashes
                vc.play(discord.FFmpegPCMAudio(pre_filename, options="-loglevel error"))
                while vc.is_playing(): await asyncio.sleep(0.1)

            await asyncio.sleep(2.5)

            if os.path.exists(call_filename) and os.path.getsize(call_filename) > 100:
                vc.play(discord.FFmpegPCMAudio(call_filename, options="-loglevel error"))
                while vc.is_playing(): await asyncio.sleep(0.1)
                
            try:
                await channel.set_permissions(me, priority_speaker=None)
            except discord.Forbidden:
                pass
                
            await asyncio.sleep(1)
            if os.path.exists(pre_filename): os.remove(pre_filename)
            if os.path.exists(call_filename): os.remove(call_filename)

            self.audio_queue.task_done()

# --- Initialize Worker Bots ---
workers = {}
for dept, config in WORKER_CONFIGS.items():
    workers[dept] = DispatchWorker(
        dept_name=dept, 
        vc_id=config["vc_id"], 
        bot_name=config["name"]
    )

# --- Main Bot Class (Ontario Border Dispatch) ---
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

    async def setup_hook(self):
        await self.tree.sync()
        self.auto_sync_loop.start()
        self.waiting_room_timeout_loop.start()
        self.team_monitor_loop.start()
        
        print(f"[MAIN] Ontario Border Dispatch online. (discord.py v{discord.__version__})")
        if not DAVEY_INSTALLED:
            print("[MAIN] CRITICAL WARNING: 'davey' library is missing. E2EE encryption will fail!")

    async def on_ready(self):
        startup_embed = discord.Embed(
            description="## <:signal:1568381854497906738> Dispatch System Online\nOntario Border Dispatch and all departmental worker bots are now active and monitoring frequencies.",
            color=discord.Color.green(),
            timestamp=datetime.datetime.now(datetime.timezone.utc)
        )
        startup_embed.set_footer(text="Ontario Border Dispatch System")

        for dept_name, channel_id in TEXT_CHANNELS.items():
            channel = self.get_channel(channel_id)
            if channel:
                try:
                    await channel.send(embed=startup_embed)
                except discord.Forbidden:
                    pass

    async def play_waiting_room_warning(self, text: str):
        channel = self.get_channel(WAITING_ROOM_ID)
        if not channel: return
        
        self.waiting_room_empty_minutes = 0
        uid = uuid.uuid4().hex
        filename = f"main_warning_{uid}.mp3"
        
        await asyncio.to_thread(generate_tts_file, text, filename)

        vc = channel.guild.voice_client
        if not vc:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        while vc.is_playing(): await asyncio.sleep(0.5)
        
        if os.path.exists(filename) and os.path.getsize(filename) > 100:
            vc.play(discord.FFmpegPCMAudio(filename, options="-loglevel error"))
            while vc.is_playing(): await asyncio.sleep(0.1)
            
            await asyncio.sleep(1)
            if os.path.exists(filename): os.remove(filename)

    @tasks.loop(minutes=1)
    async def waiting_room_timeout_loop(self):
        channel = self.get_channel(WAITING_ROOM_ID)
        if not channel or not channel.guild.voice_client: return
        
        non_bots = [m for m in channel.members if not m.bot]
        if len(non_bots) == 0:
            self.waiting_room_empty_minutes += 1
            if self.waiting_room_empty_minutes >= 15:
                await channel.guild.voice_client.disconnect()
                self.waiting_room_empty_minutes = 0
        else:
            self.waiting_room_empty_minutes = 0

    @tasks.loop(seconds=30)
    async def team_monitor_loop(self):
        if not ERLC_SERVER_KEY: return
        try:
            async with aiohttp.ClientSession() as session:
                headers = {"Server-Key": ERLC_SERVER_KEY}
                async with session.get(ERLC_API_URL, headers=headers) as resp:
                    if resp.status != 200: return
                    erlc_data = await resp.json()
                    players = erlc_data.get("Players", [])
                    
                    current_teams = {}
                    for p in players:
                        player_name = p.get("Player", "").split(":")[0]
                        team = p.get("Team", "Civilian").lower()
                        current_teams[player_name] = team
                        is_emergency = any(k in team for k in EMERGENCY_TEAMS)
                        
                        if player_name in self.cached_player_teams:
                            old_team = self.cached_player_teams[player_name]
                            if old_team != team and is_emergency:
                                await send_in_game_pm(player_name, "DISPATCH: Please wait 45 to 60 seconds for the database to sync before joining the Discord RTO.")
                        else:
                            if self.first_scan_complete and is_emergency:
                                await send_in_game_pm(player_name, "DISPATCH: Please wait 45 to 60 seconds for the database to sync before joining the Discord RTO.")
                                
                    self.cached_player_teams = current_teams
                    self.first_scan_complete = True
        except Exception:
            pass

    async def sync_member_rto(self, member, waiting_room):
        player_data, status = await fetch_user_game_data(member)
        
        if status != "success" or not player_data:
            await member.move_to(waiting_room, reason="Not verified or left game")
            await self.play_waiting_room_warning("Attention player: You must be verified on Melonly and logged in to join RTO.")
            return

        team = player_data["team"].lower()
        callsign = player_data["callsign"]
        username = player_data["username"]
        is_emergency = any(keyword in team for keyword in EMERGENCY_TEAMS)

        if not is_emergency:
            await member.move_to(waiting_room, reason="Switched to Civilian")
            await self.play_waiting_room_warning("Frequency change: You were moved to waiting room because you switched to the civilian team.")
        elif not callsign:
            await member.move_to(waiting_room, reason="Dropped callsign")
            await self.play_waiting_room_warning("Attention unit: Please set your in-game callsign before entering RTO.")
        else:
            new_nick = f"{callsign} | {username}"
            if member.display_name != new_nick:
                try:
                    await member.edit(nick=new_nick)
                    print(f"[SYNC UPDATE] Updated {member.name} to {new_nick}")
                except discord.Forbidden:
                    print(f"[SYNC KICK] Permission denied. {member.name} has a higher discord role than the bot.")
                    await member.move_to(waiting_room, reason="Bot lacks permission to enforce callsign formatting")
                    await self.play_waiting_room_warning("Attention unit: Your Discord role is too high for me to set your callsign. Please set your nickname manually.")

    @tasks.loop(seconds=60)
    async def auto_sync_loop(self):
        for guild in self.guilds:
            for vc in guild.voice_channels:
                if "RTO" in vc.name.upper():
                    for member in vc.members:
                        if member.bot: continue
                        waiting_room = guild.get_channel(WAITING_ROOM_ID)
                        if waiting_room:
                            await self.sync_member_rto(member, waiting_room)

main_bot = MainBot()

# --- Reusable Bot Info Embed Generator ---
def get_botinfo_embed():
    unix_start = int(main_bot.start_time)
    
    desc = (
        "## <:bot:1568382041672917193> Bot Information\n"
        "The core routing and enforcement system for the **Ontario Border Roleplay** communications network.\n\n"
        f"**<:wifihigh:1568381086608662638> Uptime:** <t:{unix_start}:R>\n"
        f"**<:signal:1568381854497906738> Network Ping:** {round(main_bot.latency * 1000)}ms\n"
        "**<:database:1568381596363657216> Architecture:** `discord.py` & `aiohttp` web server\n"
        f"**<:settings:1568382499187462306> Worker Nodes:** {len(workers)} active departmental bots\n"
        "**<:mic:1568379901978091651> Voice Protocol:** `davey` (E2EE Enabled)\n"
    )
    
    embed = discord.Embed(description=desc, color=discord.Color.blurple())
    embed.set_thumbnail(url="attachment://bot_logo.jpg")
    embed.add_field(name="<:github:1568382792385962044> Developer", value="[Krypton Peak Technologies](https://kryptonpeak.com)", inline=True)
    embed.add_field(name="<:shield:1568383105188892823> Monitoring", value=f"{len(EMERGENCY_TEAMS)} Emergency Teams", inline=True)
    embed.set_footer(text="Ontario Border Dispatch System")
    return embed

# --- Main Bot Commands ---
@main_bot.tree.command(name="botinfo", description="Displays system information about the dispatch bot")
async def botinfo(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_COMMAND_CHANNELS:
        await interaction.response.send_message(f"❌ Commands can only be used in designated bot channels (e.g., <#{ALLOWED_COMMAND_CHANNELS[0]}>).", ephemeral=True)
        return

    file = discord.File("Ontario Border Dispatch Logo.jpg", filename="bot_logo.jpg")
    embed = get_botinfo_embed()
    await interaction.response.send_message(embed=embed, file=file)

@main_bot.tree.command(name="botstatus", description="Displays the live connection status of all dispatcher bots")
async def botstatus(interaction: discord.Interaction):
    if interaction.channel_id not in ALLOWED_COMMAND_CHANNELS:
        await interaction.response.send_message(f"❌ Commands can only be used in designated bot channels (e.g., <#{ALLOWED_COMMAND_CHANNELS[0]}>).", ephemeral=True)
        return

    desc = "## <:signal:1568381854497906738> Dispatch Network Status\nLive connection metrics and monitoring statuses for the main router and all departmental workers."
    embed = discord.Embed(description=desc, color=discord.Color.green())
    
    main_ping = round(main_bot.latency * 1000)
    embed.add_field(name="Main Dispatch (Sync & Routing)", value=f"<:goodsignal:1568399560622211143> Online ({main_ping}ms)", inline=False)
    
    for dept, worker in workers.items():
        if worker.is_ready():
            worker_ping = round(worker.latency * 1000)
            status = f"<:goodsignal:1568399560622211143> Online ({worker_ping}ms) - Monitoring <#{worker.vc_id}>"
        else:
            status = "🔴 Offline / Disconnected"
            embed.color = discord.Color.red()
            
        embed.add_field(name=f"{worker.bot_name} ({dept.upper()})", value=status, inline=False)
    
    embed.add_field(name="<:world:1568399069385203722> Webhook Server", value="<:port:1568429510666231938> Online (Port 8080)", inline=False)
    embed.set_footer(text="Ontario Border Dispatch System")
    await interaction.response.send_message(embed=embed)

# --- Message Events (Ping Detection) ---
@main_bot.event
async def on_message(message):
    if message.author.bot:
        return

    if main_bot.user.mentioned_in(message) and not message.mention_everyone:
        file = discord.File("Ontario Border Dispatch Logo.jpg", filename="bot_logo.jpg")
        embed = get_botinfo_embed()
        await message.channel.send(embed=embed, file=file)

    await main_bot.process_commands(message)

# --- Voice Events (Callsign Enforcement) ---
@main_bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return

    if after.channel and "RTO" in after.channel.name.upper():
        waiting_room = member.guild.get_channel(WAITING_ROOM_ID)
        if waiting_room:
            # Trigger sync immediately on join
            await main_bot.sync_member_rto(member, waiting_room)

# --- ER:LC Webhook Server (Audio, Text, & Web Page Routes) ---
async def serve_index(request):
    return web.FileResponse('./index.html')

async def serve_bot_logo(request):
    return web.FileResponse('./Ontario Border Dispatch Logo.jpg')

async def serve_kp_logo(request):
    return web.FileResponse('./KrpytonPeak Logo.png')

async def handle_erlc_webhook(request):
    try:
        raw_body = await request.read()
        signature_header = request.headers.get("X-Signature-Ed25519")
        timestamp_header = request.headers.get("X-Signature-Timestamp")

        if not signature_header or not timestamp_header:
            return web.Response(status=400, text="Missing signature headers")

        message = timestamp_header.encode("utf-8") + raw_body
        
        # ER:LC Webhooks use Base64 for their Ed25519 signature
        try:
            sig_bytes = base64.b64decode(signature_header)
            ERLC_PUBLIC_KEY.verify(message, sig_bytes)
        except (BadSignatureError, ValueError, TypeError):
            try:
                ERLC_PUBLIC_KEY.verify(message, bytes.fromhex(signature_header))
            except (BadSignatureError, ValueError):
                print("[WEBHOOK ERROR] Invalid Ed25519 signature.")
                return web.Response(status=401, text="Invalid signature")

        data = await request.json()
        
        # Parse new ER:LC batched event array format
        events = data.get("events", [data])
        
        for event_payload in events:
            event_type = event_payload.get("event", event_payload.get("Type", "Unknown"))
            
            if event_type == "WebhookProbe":
                print("[WEBHOOK] Received ER:LC Probe. Validated and Acknowledged.")
                continue
                
            payload = event_payload.get("data", event_payload)
            if "Data" in payload:
                payload = payload["Data"]

            if event_type == "Call911" or "Caller" in payload:
                call_caller = payload.get("Caller", "Unknown Caller")
                call_location = payload.get("Location", "Unknown Location")
                call_details = payload.get("Details", "No details provided")
                dept = payload.get("Department", "police").lower()

                if dept == "transportation": dept = "dot"
                if dept == "sheriff": dept = "police" 

                print(f"[DISPATCH] Routing {dept.upper()} call from {call_caller}")

                pre_text = "There is an incoming call, please silence the RTO."
                call_text = f"Caller: {call_caller}. Description: {call_details}. Location: {call_location}."

                if dept in workers:
                    await workers[dept].audio_queue.put((pre_text, call_text))
                elif "police" in workers:
                    await workers["police"].audio_queue.put((pre_text, call_text))

                embed = discord.Embed(
                    description=f"## <:phonecall:1568426067620798526> 911 Dispatch - {dept.upper()}",
                    color=discord.Color.red() if dept == "fire" else discord.Color.blue(),
                    timestamp=datetime.datetime.now(datetime.timezone.utc)
                )
                embed.add_field(name="Caller", value=call_caller, inline=True)
                embed.add_field(name="Location", value=call_location, inline=True)
                embed.add_field(name="Details", value=call_details, inline=False)
                embed.set_footer(text="Ontario Border Roleplay System")

                if dept in TEXT_CHANNELS:
                    dept_channel = main_bot.get_channel(TEXT_CHANNELS[dept])
                    if dept_channel: await dept_channel.send(embed=embed)

                staff_channel = main_bot.get_channel(TEXT_CHANNELS["staff_log"])
                if staff_channel: await staff_channel.send(embed=embed)

        return web.Response(status=200, text="Dispatched")

    except Exception as e:
        print(f"[WEBHOOK CRITICAL ERROR] {e}")
        return web.Response(status=400, text="Bad Request")

async def start_webhook_server():
    app = web.Application()
    
    app.router.add_get("/", serve_index)
    app.router.add_get("/Ontario Border Dispatch Logo.jpg", serve_bot_logo)
    app.router.add_get("/KrpytonPeak Logo.png", serve_kp_logo)
    app.router.add_post("/erlc-webhook", handle_erlc_webhook)
    
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("[SERVER] Webhook server listening on port 8080")

# --- Async Startup Routine ---
async def main():
    await start_webhook_server()

    tasks = [main_bot.start(MAIN_BOT_TOKEN)]
    for dept, config in WORKER_CONFIGS.items():
        if config["token"]:
            tasks.append(workers[dept].start(config["token"]))

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())