import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from aiohttp import web
import asyncio
import re
import os
import datetime
from gtts import gTTS
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# --- Security & Tokens ---
MAIN_BOT_TOKEN = os.getenv("MAIN_BOT_TOKEN")
MELONLY_API_KEY = os.getenv("MELONLY_API_KEY")
ERLC_SERVER_KEY = os.getenv("ERLC_SERVER_KEY")

# --- Channel Configuration ---
# Replace these with your actual Discord Channel IDs in Ontario Border Roleplay
WAITING_ROOM_ID = 1567191463203446804  # VC for Frequency Change

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
    "police": 1523672972764905492,  # CBSA text log
    "fire": 1523673131959717938,    # OBFS text log
    "dot": 1523673289791377448,     # MOT text log
    "staff_log": 1568057421589643324 # All-calls staff log
}

# --- Constants & APIs ---
MELONLY_API_URL = "https://api.melonly.xyz/api/v1"
ERLC_API_URL = "https://api.erlc.gg/v2/server"
EMERGENCY_TEAMS = ["police", "sheriff", "fire", "rescue", "transportation", "dot"]
CALLSIGN_REGEX = re.compile(r"^[A-Z0-9\-]+\s\|\s.+")

# --- API Fetcher Utility ---
async def fetch_user_game_data(discord_id: int):
    """Checks Melonly and ER:LC for player data."""
    async with aiohttp.ClientSession() as session:
        mel_headers = {"Authorization": f"Bearer {MELONLY_API_KEY}"}
        async with session.get(f"{MELONLY_API_URL}/server/members?discordId={discord_id}", headers=mel_headers) as resp:
            if resp.status != 200: return None, "melonly_error"
            mel_data = await resp.json()
            members = mel_data.get("data", [])
            if not members: return None, "not_verified"
            
            roblox_username = members[0].get("robloxUsername") or members[0].get("user", {}).get("username")
            if not roblox_username: return None, "no_roblox_user"

        erlc_headers = {"Server-Key": ERLC_SERVER_KEY}
        async with session.get(ERLC_API_URL, headers=erlc_headers) as resp:
            if resp.status != 200: return None, "erlc_error"
            erlc_data = await resp.json()
            players = erlc_data.get("Players", [])

            for p in players:
                player_name = p.get("Player", "").split(":")[0]
                if player_name.lower() == roblox_username.lower():
                    return {
                        "username": roblox_username,
                        "team": p.get("Team", "Civilian"),
                        "callsign": p.get("Callsign", None)
                    }, "success"

            return None, "not_in_game"

# --- Worker Bot Class ---
class DispatchWorker(discord.Client):
    def __init__(self, dept_name, vc_id, bot_name):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.dept_name = dept_name
        self.vc_id = vc_id
        self.bot_name = bot_name
        self.audio_queue = asyncio.Queue()

    async def setup_hook(self):
        self.loop.create_task(self.audio_processor())
        print(f"{self.bot_name} online and monitoring {self.dept_name.upper()} RTO.")

    async def audio_processor(self):
        await self.wait_until_ready()
        channel = self.get_channel(self.vc_id)
        if not channel: return
            
        vc = await channel.connect()
        me = channel.guild.me

        while True:
            pre_text, call_text = await self.audio_queue.get()
            
            pre_filename = f"pre_dispatch_{self.dept_name}.mp3"
            call_filename = f"call_dispatch_{self.dept_name}.mp3"
            
            gTTS(text=pre_text, lang='en', slow=False).save(pre_filename)
            gTTS(text=call_text, lang='en', slow=False).save(call_filename)

            while vc.is_playing(): await asyncio.sleep(0.5)

            try:
                await channel.set_permissions(me, priority_speaker=True)
            except discord.Forbidden:
                pass

            vc.play(discord.FFmpegPCMAudio(pre_filename))
            while vc.is_playing(): await asyncio.sleep(0.1)

            await asyncio.sleep(2.5)

            vc.play(discord.FFmpegPCMAudio(call_filename))
            while vc.is_playing(): await asyncio.sleep(0.1)
                
            try:
                await channel.set_permissions(me, priority_speaker=None)
            except discord.Forbidden:
                pass

            self.audio_queue.task_done()

# --- Initialize Worker Bots ---
workers = {}
for dept, config in WORKER_CONFIGS.items():
    workers[dept] = DispatchWorker(dept_name=dept, vc_id=config["vc_id"], bot_name=config["name"])

# --- Main Bot Class (Ontario Border Dispatch) ---
class MainBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        self.auto_sync_loop.start()
        print("Ontario Border Dispatch online.")

    async def play_waiting_room_warning(self, text: str):
        channel = self.get_channel(WAITING_ROOM_ID)
        if not channel: return
        
        filename = "main_warning.mp3"
        gTTS(text=text, lang='en', slow=False).save(filename)

        vc = channel.guild.voice_client
        if not vc:
            vc = await channel.connect()
        elif vc.channel != channel:
            await vc.move_to(channel)

        while vc.is_playing(): await asyncio.sleep(0.5)
        vc.play(discord.FFmpegPCMAudio(filename))

    @tasks.loop(seconds=60)
    async def auto_sync_loop(self):
        for guild in self.guilds:
            for vc in guild.voice_channels:
                if "RTO" in vc.name.upper():
                    for member in vc.members:
                        if member.bot: continue
                        player_data, status = await fetch_user_game_data(member.id)
                        waiting_room = guild.get_channel(WAITING_ROOM_ID)

                        if status == "success" and player_data and waiting_room:
                            team = player_data["team"].lower()
                            is_emergency = any(keyword in team for keyword in EMERGENCY_TEAMS)

                            if not is_emergency:
                                await member.move_to(waiting_room, reason="Switched to Civilian")
                                await self.play_waiting_room_warning("Frequency change: You were moved to waiting room because you switched to the civilian team.")

main_bot = MainBot()

# --- Main Bot Commands ---
@main_bot.tree.command(name="sync", description="Sync your team status and callsign with Discord")
async def sync(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    player_data, status = await fetch_user_game_data(interaction.user.id)

    if status == "not_verified":
        await interaction.followup.send("❌ You are not verified with Melonly in this server.")
    elif status == "not_in_game":
        await interaction.followup.send("❌ You are not currently logged into the ER:LC private server.")
    elif status == "success":
        team = player_data["team"]
        username = player_data["username"]
        callsign = player_data["callsign"]
        is_emergency = any(keyword in team.lower() for keyword in EMERGENCY_TEAMS)

        if is_emergency:
            if not callsign:
                await interaction.followup.send(f"⚠️ Verified as **{username}** on team **{team}**, but no callsign is set in-game.")
            else:
                new_nick = f"{callsign} | {username}"
                try:
                    await interaction.user.edit(nick=new_nick)
                    await interaction.followup.send(f"✅ Synced! Nickname updated to **{new_nick}**.")
                except discord.Forbidden:
                    await interaction.followup.send(f"✅ Verified as **{new_nick}**, but bot lacks permission to update your nickname.")
        else:
            await interaction.followup.send(f"ℹ️ Verified as **{username}** (Civilian). No callsign required.")

@main_bot.tree.command(name="frequency", description="Tune your radio to a specific tactical frequency")
@app_commands.describe(freq="Frequency number (e.g., TAC-1, 101.1)")
async def frequency(interaction: discord.Interaction, freq: str):
    if not interaction.user.voice:
        await interaction.response.send_message("❌ You must be in a voice channel to change frequencies.", ephemeral=True)
        return

    guild = interaction.guild
    category = interaction.user.voice.channel.category
    channel_name = f"Frequency {freq}"

    target_channel = discord.utils.get(guild.voice_channels, name=channel_name)

    if not target_channel:
        target_channel = await guild.create_voice_channel(
            name=channel_name, 
            category=category,
            reason=f"Frequency initiated by {interaction.user.display_name}"
        )
        await target_channel.set_permissions(interaction.user, speak=True)

    await interaction.user.move_to(target_channel)
    await interaction.response.send_message(f"📻 Tuned to **{channel_name}**.", ephemeral=True)

# --- Voice Events (Callsign Enforcement) ---
@main_bot.event
async def on_voice_state_update(member, before, after):
    if member.bot: return

    # Cleanup empty frequencies
    if before.channel and before.channel.name.startswith("Frequency"):
        if len(before.channel.members) == 0:
            await before.channel.delete(reason="Frequency empty")

    if after.channel and "RTO" in after.channel.name.upper():
        waiting_room = member.guild.get_channel(WAITING_ROOM_ID)
        if not waiting_room: return

        player_data, status = await fetch_user_game_data(member.id)

        if status != "success" or not player_data:
            await member.move_to(waiting_room)
            await main_bot.play_waiting_room_warning("Attention player: You must be verified on Melonly and logged in to join RTO.")
            return

        team = player_data["team"].lower()
        callsign = player_data["callsign"]
        username = player_data["username"]
        is_emergency = any(k in team for k in EMERGENCY_TEAMS)

        if not is_emergency:
            await member.move_to(waiting_room)
            await main_bot.play_waiting_room_warning("Frequency change: Civilians cannot join emergency radio channels.")
            return
        if not callsign:
            await member.move_to(waiting_room)
            await main_bot.play_waiting_room_warning("Attention unit: Please set your in-game callsign before entering RTO.")
            return

        new_nick = f"{callsign} | {username}"
        try:
            if member.display_name != new_nick:
                await member.edit(nick=new_nick)
        except discord.Forbidden:
            pass

# --- ER:LC Webhook Server (Audio & Text Embeds) ---
async def handle_erlc_webhook(request):
    try:
        data = await request.json()
        call_caller = data.get("Caller", "Unknown Caller")
        call_location = data.get("Location", "Unknown Location")
        call_details = data.get("Details", "No details provided")
        dept = data.get("Department", "police").lower()

        # Route Dot to Transportation
        if dept == "transportation": dept = "dot"
        if dept == "sheriff": dept = "police" 

        pre_text = "There is an incoming call, please silence the RTO."
        call_text = f"Caller: {call_caller}. Description: {call_details}. Location: {call_location}."

        # 1. Voice Routing
        if dept in workers:
            await workers[dept].audio_queue.put((pre_text, call_text))
        elif "police" in workers:
            await workers["police"].audio_queue.put((pre_text, call_text))

        # 2. Text Logging
        embed = discord.Embed(
            title=f"🚨 911 Dispatch - {dept.upper()}",
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
        print(f"Webhook error: {e}")
        return web.Response(status=400, text="Bad Request")

async def start_webhook_server():
    app = web.Application()
    app.router.add_post("/erlc-webhook", handle_erlc_webhook)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", 8080)
    await site.start()
    print("Webhook server listening on port 8080")

# --- Async Startup Routine ---
async def main():
    await start_webhook_server()

    tasks = [main_bot.start(MAIN_BOT_TOKEN)]
    for dept, config in WORKER_CONFIGS.items():
        if config["token"]:
            tasks.append(workers[dept].start(config["token"]))

    await asyncio.gather(*tasks)

if __name__ == "__main__":
    # Note: Ensure FFmpeg is installed on your host OS
    asyncio.run(main())