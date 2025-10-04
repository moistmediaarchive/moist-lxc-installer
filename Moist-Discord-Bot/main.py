import discord
from discord import app_commands
from discord.ext import commands
import logging
import os
import subprocess
import json
from dotenv import load_dotenv
import asyncio
from functools import partial

# === CONFIG ===
load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
SERVER_BASE = os.getenv("SERVER_BASE")
CONTROLLER_SCRIPT = os.getenv("CONTROLLER_SCRIPT")
STATE_FILE = os.getenv("STATE_FILE")
GUILD_ID = int(os.getenv("GUILD_ID"))
GUILD = discord.Object(id=GUILD_ID)

DELETE_DELAY = 30  # seconds
ADMIN_ROLE_NAME = "Game Admin"

# === LOGGING ===
handler = logging.FileHandler(filename="discord.log", encoding="utf-8", mode="w")

# === INTENTS ===
intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)
tree = bot.tree

# Runtime state
current_track = None
current_link = None


# === HELPERS ===

def save_state():
    """Save current server state to disk."""
    data = {"track": current_track, "link": current_link}
    with open(STATE_FILE, "w") as f:
        json.dump(data, f)


def load_state():
    """Load last known server state."""
    global current_track, current_link
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r") as f:
                data = json.load(f)
            current_track = data.get("track")
            current_link = data.get("link")
        except Exception:
            current_track = None
            current_link = None


def list_tracks():
    """Return list of track folder names under SERVER_BASE."""
    try:
        return [t for t in os.listdir(SERVER_BASE) if os.path.isdir(os.path.join(SERVER_BASE, t))]
    except Exception:
        return []


async def update_bot_status(track_name: str | None):
    """Set presence to show the current track or 'No Server Running'."""
    if track_name:
        await bot.change_presence(activity=discord.Game(name=f"🏁 {track_name}"))
    else:
        await bot.change_presence(
            status=discord.Status.idle,
            activity=discord.Game(name="🛑 No Server Running")
        )


async def delete_later(msg: discord.Message, delay: int):
    """Delete a message after a delay."""
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass


async def send_ephemeral_followup(interaction: discord.Interaction, embed: discord.Embed):
    """Send followup message and schedule deletion."""
    msg = await interaction.followup.send(embed=embed)
    try:
        msg = await msg.fetch()
    except Exception:
        pass
    asyncio.create_task(delete_later(msg, DELETE_DELAY))
    return msg


async def run_controller_command(*args, timeout: int):
    """Run the controller script asynchronously."""
    cmd = ["python3", CONTROLLER_SCRIPT] + list(args)
    return await asyncio.to_thread(partial(subprocess.run, cmd, capture_output=True, text=True, timeout=timeout))


def has_game_admin_role(interaction: discord.Interaction):
    """Check if user has the Game Admin role."""
    if not interaction.user or not isinstance(interaction.user, discord.Member):
        return False
    return any(role.name == ADMIN_ROLE_NAME for role in interaction.user.roles)


# === AUTOCOMPLETE ===
async def track_autocomplete(interaction: discord.Interaction, current: str):
    tracks = list_tracks()
    filtered = [t for t in tracks if current.lower() in t.lower()]
    return [app_commands.Choice(name=t, value=t) for t in filtered][:25]


# === EVENTS ===
@bot.event
async def on_ready():
    load_state()
    print(f"✅ Logged in as {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync(guild=GUILD)
        print(f"🔁 Synced {len(synced)} commands to guild {GUILD_ID}")
    except Exception as e:
        print("❌ Command sync failed:", e)
    await update_bot_status(current_track)


@bot.event
async def on_message(message):
    if message.content.startswith("/"):
        try:
            await message.delete()
        except Exception:
            pass
    await bot.process_commands(message)


# === SLASH COMMANDS ===

@tree.command(name="serverlist", description="List available tracks.", guild=GUILD)
async def serverlist(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)
    tracks = list_tracks()

    embed = discord.Embed(title="📂 Available Tracks", color=0x3498db)
    if not tracks:
        embed.description = "⚠️ No tracks found."
    else:
        embed.description = "\n".join(f"• {t}" for t in tracks)

    await send_ephemeral_followup(interaction, embed)


@tree.command(name="currenttrack", description="Show the currently running track.", guild=GUILD)
async def currenttrack(interaction: discord.Interaction):
    await interaction.response.defer(thinking=True)

    if current_track:
        embed = discord.Embed(title="🏁 Current Track", color=0x2ecc71)
        embed.description = f"**{current_track}**"
        if current_link:
            embed.add_field(name="Join Link", value=current_link, inline=False)
    else:
        embed = discord.Embed(title="⚠️ No Active Server", color=0xf1c40f)
        embed.description = "No Assetto Corsa server is currently running."

    await send_ephemeral_followup(interaction, embed)


@tree.command(name="start", description="Start an Assetto Corsa server for a chosen track.", guild=GUILD)
@app_commands.describe(track_name="The name of the track to start.")
@app_commands.autocomplete(track_name=track_autocomplete)
async def start(interaction: discord.Interaction, track_name: str):
    global current_track, current_link

    if not has_game_admin_role(interaction):
        await interaction.response.send_message("🚫 You do not have permission to start a server.", ephemeral=True)
        return

    await interaction.response.defer(thinking=False)

    embed = discord.Embed(title="🏁 Starting Server", color=0x3498db)
    embed.add_field(name="Track", value=track_name, inline=True)
    embed.add_field(name="Status", value="⚙️ Preparing...", inline=False)
    msg = await interaction.followup.send(embed=embed)

    tracks = list_tracks()
    matched = next((t for t in tracks if t.lower() == track_name.lower()), None)

    if not matched:
        embed.color = 0xe74c3c
        embed.set_field_at(1, name="Status", value=f"❌ Track `{track_name}` not found.")
        await msg.edit(embed=embed)
        asyncio.create_task(delete_later(msg, DELETE_DELAY))
        return

    embed.set_field_at(1, name="Status", value="🧹 Shutting down any existing server...")
    await msg.edit(embed=embed)

    try:
        stop_res = await run_controller_command("stop", timeout=15)
    except Exception as e:
        embed.set_field_at(1, name="Status", value=f"⚠️ Error stopping old server: `{e}`")
        await msg.edit(embed=embed)

    embed.set_field_at(1, name="Status", value=f"🚀 Starting **{matched}**...")
    await msg.edit(embed=embed)

    try:
        start_res = await run_controller_command(matched, timeout=45)
    except subprocess.TimeoutExpired:
        embed.color = 0xf1c40f
        embed.set_field_at(1, name="Status", value="⚠️ Timeout — server might still be starting.")
        await msg.edit(embed=embed)
        asyncio.create_task(delete_later(msg, DELETE_DELAY))
        return

    stdout = (start_res.stdout or "").strip()
    join_url = None
    for line in stdout.splitlines():
        if "JOIN_URL:" in line:
            join_url = line.split("JOIN_URL:")[1].strip()
            break

    current_track = matched
    current_link = join_url
    save_state()
    await update_bot_status(matched)

    embed.color = 0x2ecc71
    embed.set_field_at(1, name="Status", value="✅ Server started successfully!")
    embed.add_field(name="Join Link", value=join_url or "⚠️ No link found.", inline=False)
    await msg.edit(embed=embed)
    asyncio.create_task(delete_later(msg, DELETE_DELAY))


@tree.command(name="shutdown", description="Stop the Assetto Corsa server.", guild=GUILD)
async def shutdown(interaction: discord.Interaction):
    global current_track, current_link

    if not has_game_admin_role(interaction):
        await interaction.response.send_message("🚫 You do not have permission to shut down the server.", ephemeral=True)
        return

    await interaction.response.defer(thinking=False)

    embed = discord.Embed(title="🛑 Shutting Down Server", color=0x3498db)
    embed.add_field(name="Status", value="Stopping server...", inline=False)
    msg = await interaction.followup.send(embed=embed)

    try:
        stop_res = await run_controller_command("stop", timeout=30)
    except Exception as e:
        embed.color = 0xe74c3c
        embed.set_field_at(0, name="Status", value=f"❌ Error: `{e}`")
        await msg.edit(embed=embed)
        asyncio.create_task(delete_later(msg, DELETE_DELAY))
        return

    prev = current_track
    current_track = None
    current_link = None
    save_state()
    await update_bot_status(None)

    embed.color = 0x2ecc71 if stop_res.returncode == 0 else 0xe74c3c
    embed.set_field_at(0, name="Status", value=f"✅ Server stopped ({prev or 'unknown'}).")
    await msg.edit(embed=embed)
    asyncio.create_task(delete_later(msg, DELETE_DELAY))


# === RUN BOT ===
bot.run(TOKEN, log_handler=handler, log_level=logging.DEBUG)