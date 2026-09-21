import os
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import typing
import re
import datetime
import aiohttp
import certifi
from flask import Flask
from threading import Thread
from pymongo import MongoClient

app = Flask('')

@app.route('/')
def home():
    return "ChillBot is alive! 😎"

def run_flask():
    try:
        port = int(os.environ.get("PORT", 8080))
        print(f"Starting Flask on port {port}", flush=True)
        app.run(host='0.0.0.0', port=port)
    except Exception as e:
        print(f"FLASK CRASHED: {e}", flush=True)

Thread(target=run_flask, daemon=True).start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="?", intents=intents, help_command=None)

OWNER_ID = 1160627021865549976
OWNER_NAME = "Aarav"
DEFAULT_COLOR = discord.Color.from_str("#5865F2")
BOT_START_TIME = datetime.datetime.now(datetime.timezone.utc)

OWNER_REPLIES = [
    "Yes, master? 👑",
    "At your service, master! 😎",
    "You rang, master? 🔔",
    "ChillBot reporting for duty, master! 🎉",
    "What can I do for you, master?",
    "Always watching, master. 👀",
]

# ---------- DATABASE ----------

mongo_client = MongoClient(os.environ["MONGODB_URI"], tlsCAFile=certifi.where(), serverSelectionTimeoutMS=8000)
db = mongo_client["chillbot"]
giveaways_col = db["giveaways"]
settings_col = db["settings"]

# ---------- DATA STORES (in memory, backed by MongoDB) ----------

giveaways = {}
authorized_roles = {}
blacklisted_roles = {}
entry_channels = {}
multiplier_roles = {}
win_counts = {}
log_channels = {}
default_ping_roles = {}
embed_colors = {}


def save_giveaway(gid):
    gw = giveaways.get(gid)
    if not gw:
        return
    doc = {
        "_id": gid,
        "channel_id": gw["channel_id"],
        "guild_id": gw["guild_id"],
        "host_id": gw["host_id"],
        "prize": gw["prize"],
        "winners": gw["winners"],
        "required_role_id": gw["required_role_id"],
        "blacklist_role_id": gw["blacklist_role_id"],
        "bypass_role_id": gw["bypass_role_id"],
        "joined_users": list(gw["joined_users"]),
        "entries": gw["entries"],
        "active": gw["active"],
        "end_time": gw["end_time"].isoformat(),
    }

    def _write():
        try:
            giveaways_col.replace_one({"_id": gid}, doc, upsert=True)
        except Exception as e:
            print(f"DB save_giveaway error: {e}", flush=True)

    try:
        bot.loop.run_in_executor(None, _write)
    except Exception as e:
        print(f"DB save_giveaway schedule error: {e}", flush=True)


def delete_giveaway_doc(gid):
    def _delete():
        try:
            giveaways_col.delete_one({"_id": gid})
        except Exception as e:
            print(f"DB delete_giveaway error: {e}", flush=True)

    try:
        bot.loop.run_in_executor(None, _delete)
    except Exception as e:
        print(f"DB delete_giveaway schedule error: {e}", flush=True)


def save_settings(guild_id):
    doc = {
        "_id": guild_id,
        "authorized_roles": list(authorized_roles.get(guild_id, set())),
        "blacklisted_roles": list(blacklisted_roles.get(guild_id, set())),
        "entry_channels": list(entry_channels.get(guild_id, set())),
        "multiplier_roles": {str(k): v for k, v in multiplier_roles.get(guild_id, {}).items()},
        "win_counts": {str(k): v for k, v in win_counts.get(guild_id, {}).items()},
        "log_channel": log_channels.get(guild_id),
        "default_ping_role": default_ping_roles.get(guild_id),
        "embed_color": embed_colors.get(guild_id),
    }

    def _write():
        try:
            settings_col.replace_one({"_id": guild_id}, doc, upsert=True)
        except Exception as e:
            print(f"DB save_settings error: {e}", flush=True)

    try:
        bot.loop.run_in_executor(None, _write)
    except Exception as e:
        print(f"DB save_settings schedule error: {e}", flush=True)


def fetch_all_data_sync():
    """Blocking MongoDB reads — safe to run in a background thread."""
    settings_docs = list(settings_col.find({}))
    giveaway_docs = list(giveaways_col.find({"active": True}))
    return settings_docs, giveaway_docs


async def apply_loaded_data(settings_docs, giveaway_docs):
    """Takes raw DB documents and rebuilds bot state + Discord views on the main event loop."""
    for doc in settings_docs:
        guild_id = doc["_id"]
        authorized_roles[guild_id] = set(doc.get("authorized_roles", []))
        blacklisted_roles[guild_id] = set(doc.get("blacklisted_roles", []))
        entry_channels[guild_id] = set(doc.get("entry_channels", []))
        multiplier_roles[guild_id] = {int(k): v for k, v in doc.get("multiplier_roles", {}).items()}
        win_counts[guild_id] = {int(k): v for k, v in doc.get("win_counts", {}).items()}
        if doc.get("log_channel"):
            log_channels[guild_id] = doc["log_channel"]
        if doc.get("default_ping_role"):
            default_ping_roles[guild_id] = doc["default_ping_role"]
        if doc.get("embed_color"):
            embed_colors[guild_id] = doc["embed_color"]
    print(f"Loaded settings for {len(settings_docs)} server(s) from database.", flush=True)

    for doc in giveaway_docs:
        gid = doc["_id"]
        end_time = datetime.datetime.fromisoformat(doc["end_time"])

        giveaways[gid] = {
            "channel_id": doc["channel_id"],
            "guild_id": doc["guild_id"],
            "host_id": doc["host_id"],
            "prize": doc["prize"],
            "winners": doc["winners"],
            "required_role_id": doc.get("required_role_id"),
            "blacklist_role_id": doc.get("blacklist_role_id"),
            "bypass_role_id": doc.get("bypass_role_id"),
            "joined_users": set(doc.get("joined_users", [])),
            "entries": doc.get("entries", []),
            "active": True,
            "end_time": end_time,
        }

        view = GiveawayView(gid)
        bot.add_view(view, message_id=gid)

        now = datetime.datetime.now(datetime.timezone.utc)
        if end_time <= now:
            bot.loop.create_task(end_giveaway(gid))
        else:
            bot.loop.create_task(resume_giveaway_timer(gid, end_time))

    print(f"Resumed {len(giveaway_docs)} active giveaway(s) from database.", flush=True)


async def resume_giveaway_timer(gid, end_time):
    await discord.utils.sleep_until(end_time)
    if giveaways.get(gid, {}).get("active"):
        await end_giveaway(gid)


def is_admin_or_owner(member: discord.Member):
    if member.guild_permissions.administrator:
        return True
    if member.guild.owner_id == member.id:
        return True
    return False


def can_manage_giveaways(member: discord.Member):
    if is_admin_or_owner(member):
        return True
    allowed = authorized_roles.get(member.guild.id, set())
    user_role_ids = {r.id for r in member.roles}
    return len(allowed & user_role_ids) > 0


def has_bypass(member: discord.Member, bypass_role_id):
    if not bypass_role_id:
        return False
    return any(r.id == bypass_role_id for r in member.roles)


def is_blacklisted(guild_id, member, gw_blacklist_role_id):
    banned = set(blacklisted_roles.get(guild_id, set()))
    if gw_blacklist_role_id:
        banned.add(gw_blacklist_role_id)
    user_role_ids = {r.id for r in member.roles}
    return len(banned & user_role_ids) > 0


def channel_counts(guild_id, channel_id, giveaway_channel_id):
    allowed = entry_channels.get(guild_id)
    if not allowed:
        return channel_id == giveaway_channel_id
    return channel_id in allowed


def get_multiplier(guild_id, member):
    mults = multiplier_roles.get(guild_id, {})
    if not mults:
        return 1
    user_role_ids = {r.id for r in member.roles}
    best = 1
    for role_id, mult in mults.items():
        if role_id in user_role_ids and mult > best:
            best = mult
    return best


def record_win(guild_id, user_id):
    guild_wins = win_counts.setdefault(guild_id, {})
    guild_wins[user_id] = guild_wins.get(user_id, 0) + 1
    save_settings(guild_id)


def get_guild_color(guild_id):
    hex_code = embed_colors.get(guild_id)
    if hex_code:
        try:
            return discord.Color.from_str(hex_code)
        except Exception:
            pass
    return DEFAULT_COLOR


def parse_duration(text: str):
    text = text.strip().lower()
    if text.isdigit():
        return int(text) * 60

    pattern = r"(\d+)\s*(d|h|m|s)"
    matches = re.findall(pattern, text)
    if not matches:
        return None

    total_seconds = 0
    unit_seconds = {"d": 86400, "h": 3600, "m": 60, "s": 1}
    for amount, unit in matches:
        total_seconds += int(amount) * unit_seconds[unit]

    return total_seconds if total_seconds > 0 else None


async def send_log(guild: discord.Guild, embed: discord.Embed):
    channel_id = log_channels.get(guild.id)
    if not channel_id:
        return
    channel = guild.get_channel(channel_id)
    if channel:
        try:
            await channel.send(embed=embed)
        except Exception as e:
            print(f"Failed to send log: {e}", flush=True)


class ParticipantsView(discord.ui.View):
    def __init__(self, giveaway_id, page=1):
        super().__init__(timeout=180)
        self.giveaway_id = giveaway_id
        self.page = page
        self.per_page = 10

    def build_embed(self, viewer_id, guild_color):
        gw = giveaways.get(self.giveaway_id)
        if not gw:
            return discord.Embed(description="This giveaway no longer exists.", color=guild_color), 1

        sorted_users = sorted(
            gw["joined_users"],
            key=lambda uid: (-gw["entries"].count(uid), uid)
        )

        total_participants = len(sorted_users)
        total_entries = len(gw["entries"])
        total_pages = max(1, (total_participants + self.per_page - 1) // self.per_page)
        self.page = max(1, min(self.page, total_pages))

        start = (self.page - 1) * self.per_page
        end = start + self.per_page
        page_users = sorted_users[start:end]

        lines = []
        for i, uid in enumerate(page_users, start=start + 1):
            count = gw["entries"].count(uid)
            lines.append(f"{i}. <@{uid}> ({count} entr{'y' if count == 1 else 'ies'})")

        your_entries = gw["entries"].count(viewer_id)
        chance = round((your_entries / total_entries) * 100, 1) if total_entries else 0

        embed = discord.Embed(
            title=f"🎉 Giveaway Participants (Page {self.page}/{total_pages})",
            description=(
                f"These are the members that have participated in the giveaway of **{gw['prize']}**:\n\n"
                + ("\n".join(lines) if lines else "No participants on this page.")
                + f"\n\n**Total Participants:** {total_participants}\n**Total Entries:** {total_entries}\n\n"
                + f"**Your Entries:** {your_entries}\n**Your Chance of Winning:** {chance}%"
            ),
            color=guild_color
        )

        self.prev_button.disabled = self.page <= 1
        self.next_button.disabled = self.page >= total_pages

        return embed, total_pages

    @discord.ui.button(label="◀ Previous", style=discord.ButtonStyle.grey)
    async def prev_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page -= 1
        embed, _ = self.build_embed(interaction.user.id, get_guild_color(interaction.guild.id))
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Next ▶", style=discord.ButtonStyle.grey)
    async def next_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.page += 1
        embed, _ = self.build_embed(interaction.user.id, get_guild_color(interaction.guild.id))
        await interaction.response.edit_message(embed=embed, view=self)


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="🎉 Join Giveaway", style=discord.ButtonStyle.green, custom_id="join_btn")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            gw = giveaways.get(self.giveaway_id)
            if not gw or not gw["active"]:
                await interaction.response.send_message("This giveaway has ended.", ephemeral=True)
                return

            user = interaction.user
            bypass = has_bypass(user, gw["bypass_role_id"])

            if user.id in gw["joined_users"]:
                gw["joined_users"].discard(user.id)
                save_giveaway(self.giveaway_id)
                await interaction.response.send_message("You left the giveaway. 👋", ephemeral=True)
                return

            if not bypass:
                if is_blacklisted(interaction.guild.id, user, gw["blacklist_role_id"]):
                    await interaction.response.send_message("🚫 You're not allowed to join this giveaway.", ephemeral=True)
                    return

                if gw["required_role_id"]:
                    role = interaction.guild.get_role(gw["required_role_id"])
                    if role not in user.roles:
                        await interaction.response.send_message(
                            f"You need the **{role.name}** role to join this giveaway.", ephemeral=True
                        )
                        return

            gw["joined_users"].add(user.id)
            save_giveaway(self.giveaway_id)
            await interaction.response.send_message(
                "✅ You joined! Send messages in the allowed channel(s) to rack up entries. Click again to leave.",
                ephemeral=True
            )
        except Exception as e:
            print(f"Join button error: {e}", flush=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)

    @discord.ui.button(label="👥 Participants", style=discord.ButtonStyle.blurple, custom_id="participants_btn")
    async def participants_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            gw = giveaways.get(self.giveaway_id)
            if not gw or not gw["joined_users"]:
                await interaction.response.send_message("No one has joined yet.", ephemeral=True)
                return

            view = ParticipantsView(self.giveaway_id, page=1)
            embed, _ = view.build_embed(interaction.user.id, get_guild_color(interaction.guild.id))
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        except Exception as e:
            print(f"Participants button error: {e}", flush=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}", flush=True)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="for /start-giveaway 🎉"))
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)", flush=True)
    except Exception as e:
        print(f"Slash command sync failed: {e}", flush=True)
    try:
        settings_docs, giveaway_docs = await asyncio.wait_for(asyncio.to_thread(fetch_all_data_sync), timeout=15)
        await apply_loaded_data(settings_docs, giveaway_docs)
    except asyncio.TimeoutError:
        print("Loading data from database timed out — continuing without it. Check MONGODB_URI / Atlas network access.", flush=True)
    except Exception as e:
        print(f"load_all_data error: {e}", flush=True)


@bot.event
async def on_command_error(ctx, error):
    print(f"Command error: {error}", flush=True)
    try:
        await ctx.send("Something went wrong running that command.")
    except Exception:
        pass


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    print(f"Slash command error: {error}", flush=True)
    try:
        if not interaction.response.is_done():
            await interaction.response.send_message("Something went wrong running that command.", ephemeral=True)
    except Exception:
        pass


# ---------- HELP COMMAND ----------

@bot.hybrid_command(name="help", description="Show all ChillBot commands and what they do")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def help_cmd(ctx):
    embed = discord.Embed(
        title="😎 ChillBot — Help",
        description="Your all-in-one giveaway bot. Here's everything you can do:",
        color=get_guild_color(ctx.guild.id)
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(
        name="🎉 Giveaway Commands (Admins or authorized roles)",
        value=(
            "**/start-giveaway** — Create a new giveaway with a join button\n"
            "**/end-giveaway** — End a giveaway early and pick winner(s)\n"
            "**/cancel-giveaway** — Cancel a running giveaway without picking a winner\n"
            "**/edit-giveaway** — Fix a mistake in a running giveaway\n"
            "**/remove-participant** — Kick someone out of a giveaway\n"
        ),
        inline=False
    )
    embed.add_field(
        name="🏆 Everyone",
        value="**/leaderboard** — See who's won the most giveaways in this server",
        inline=False
    )
    embed.add_field(
        name="⚙️ Admin Only",
        value=(
            "**/setup** — Configure host roles, blacklist, channels, ping role, embed color, and log channel\n"
            "**/embed** — Send a custom embed message"
        ),
        inline=False
    )
    embed.add_field(
        name="🎮 Fun",
        value=(
            "**/8ball** — Ask the magic 8-ball a question\n"
            "**/roll** — Roll dice, e.g. 2d20\n"
            "**/meme** — Get a random meme\n"
            "**/coinflip** — Flip a coin\n"
            "**/game** — Play rock-paper-scissors vs ChillBot or a friend\n"
            "**/ping** — Check ChillBot's latency"
        ),
        inline=False
    )
    embed.add_field(
        name="🌍 Anywhere (DMs, group chats, or servers)",
        value=(
            "**/translate** — Translate text into another language\n"
            "**/wordle** — Guess the 5-letter word in 6 tries\n"
            "**/minesweeper** — Generate a spoiler-tag minesweeper grid\n"
            "**/higher-lower** — Guess if the next card is higher or lower\n"
            "**/connect4** — Challenge a friend to Connect 4"
        ),
        inline=False
    )
    embed.set_footer(text=f"Owner: {OWNER_NAME} • ChillBot 😎")
    await ctx.send(embed=embed)


# ---------- SETUP COMMAND ----------

@bot.tree.command(name="setup", description="[Admin] Configure giveaway roles, channels, ping role, embed color, and logs")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    action="Which setting do you want to change?",
    role="The role to apply this action to (needed for role-based actions)",
    channel="The channel to apply this action to (needed for channel-based actions)",
    multiplier="How many entries per message this role should get, e.g. 2 for double entries",
    color="Hex color code for giveaway embeds, e.g. #FF5733 (needed for the color action)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="➕ Allow a role to host giveaways", value="add_role"),
    app_commands.Choice(name="➖ Remove a role's hosting permission", value="remove_role"),
    app_commands.Choice(name="🚫 Block a role from joining giveaways", value="blacklist_role"),
    app_commands.Choice(name="✅ Unblock a role from joining giveaways", value="unblacklist_role"),
    app_commands.Choice(name="📢 Let messages in a channel count as entries", value="add_channel"),
    app_commands.Choice(name="🔕 Stop a channel's messages from counting", value="remove_channel"),
    app_commands.Choice(name="⭐ Give a role bonus entries (multiplier)", value="add_multiplier"),
    app_commands.Choice(name="✖️ Remove a role's bonus entries", value="remove_multiplier"),
    app_commands.Choice(name="📣 Set a default role to ping on new giveaways", value="set_ping_role"),
    app_commands.Choice(name="🔇 Remove the default ping role", value="remove_ping_role"),
    app_commands.Choice(name="🎨 Set a custom embed color", value="set_embed_color"),
    app_commands.Choice(name="🎨 Reset embed color to default", value="reset_embed_color"),
    app_commands.Choice(name="📝 Set the log channel", value="set_log_channel"),
    app_commands.Choice(name="🔕 Remove the log channel", value="remove_log_channel"),
    app_commands.Choice(name="📋 Show all current giveaway settings", value="show"),
])
async def setup_cmd(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    role: typing.Optional[discord.Role] = None,
    channel: typing.Optional[discord.TextChannel] = None,
    multiplier: typing.Optional[int] = None,
    color: typing.Optional[str] = None,
):
    guild_id = interaction.guild.id
    act = action.value

    is_owner_color_override = (interaction.user.id == OWNER_ID and act in ("set_embed_color", "reset_embed_color"))

    if not is_admin_or_owner(interaction.user) and not is_owner_color_override:
        await interaction.response.send_message("This command is for Administrators only.", ephemeral=True)
        return

    if act in ("add_role", "remove_role", "blacklist_role", "unblacklist_role", "add_multiplier", "remove_multiplier", "set_ping_role") and not role:
        await interaction.response.send_message("Please pick a role for this action.", ephemeral=True)
        return
    if act in ("add_channel", "remove_channel", "set_log_channel") and not channel:
        await interaction.response.send_message("Please pick a channel for this action.", ephemeral=True)
        return
    if act == "add_multiplier":
        if not multiplier:
            await interaction.response.send_message("Please provide a multiplier number, e.g. 2.", ephemeral=True)
            return
        if multiplier < 1 or multiplier > 20:
            await interaction.response.send_message("Multiplier must be between 1 and 20.", ephemeral=True)
            return
    if act == "set_embed_color":
        if not color or not re.match(r"^#?[0-9A-Fa-f]{6}$", color):
            await interaction.response.send_message("Please provide a valid hex color, e.g. #FF5733.", ephemeral=True)
            return

    if act == "add_role":
        authorized_roles.setdefault(guild_id, set()).add(role.id)
        save_settings(guild_id)
        await interaction.response.send_message(f"✅ {role.mention} can now host giveaways.", ephemeral=True)

    elif act == "remove_role":
        authorized_roles.setdefault(guild_id, set()).discard(role.id)
        save_settings(guild_id)
        await interaction.response.send_message(f"❌ {role.mention} can no longer host giveaways.", ephemeral=True)

    elif act == "blacklist_role":
        blacklisted_roles.setdefault(guild_id, set()).add(role.id)
        save_settings(guild_id)
        await interaction.response.send_message(f"🚫 {role.mention} can no longer join giveaways.", ephemeral=True)

    elif act == "unblacklist_role":
        blacklisted_roles.setdefault(guild_id, set()).discard(role.id)
        save_settings(guild_id)
        await interaction.response.send_message(f"✅ {role.mention} can join giveaways again.", ephemeral=True)

    elif act == "add_channel":
        entry_channels.setdefault(guild_id, set()).add(channel.id)
        save_settings(guild_id)
        await interaction.response.send_message(f"✅ Messages in {channel.mention} now count as entries.", ephemeral=True)

    elif act == "remove_channel":
        entry_channels.setdefault(guild_id, set()).discard(channel.id)
        save_settings(guild_id)
        await interaction.response.send_message(f"❌ Messages in {channel.mention} no longer count.", ephemeral=True)

    elif act == "add_multiplier":
        multiplier_roles.setdefault(guild_id, {})[role.id] = multiplier
        save_settings(guild_id)
        await interaction.response.send_message(f"✅ {role.mention} now gets **{multiplier}x** entries.", ephemeral=True)

    elif act == "remove_multiplier":
        multiplier_roles.setdefault(guild_id, {}).pop(role.id, None)
        save_settings(guild_id)
        await interaction.response.send_message(f"❌ {role.mention} no longer has a multiplier.", ephemeral=True)

    elif act == "set_ping_role":
        default_ping_roles[guild_id] = role.id
        save_settings(guild_id)
        await interaction.response.send_message(f"✅ {role.mention} will now be pinged automatically on new giveaways.", ephemeral=True)

    elif act == "remove_ping_role":
        default_ping_roles.pop(guild_id, None)
        save_settings(guild_id)
        await interaction.response.send_message("❌ Default ping role removed.", ephemeral=True)

    elif act == "set_embed_color":
        hex_clean = color if color.startswith("#") else f"#{color}"
        embed_colors[guild_id] = hex_clean
        save_settings(guild_id)
        preview = discord.Embed(description="This is your new embed color! 🎨", color=discord.Color.from_str(hex_clean))
        await interaction.response.send_message(embed=preview, ephemeral=True)

    elif act == "reset_embed_color":
        embed_colors.pop(guild_id, None)
        save_settings(guild_id)
        await interaction.response.send_message("✅ Embed color reset to default.", ephemeral=True)

    elif act == "set_log_channel":
        log_channels[guild_id] = channel.id
        save_settings(guild_id)
        await interaction.response.send_message(f"✅ Giveaway logs will now be sent to {channel.mention}.", ephemeral=True)

    elif act == "remove_log_channel":
        log_channels.pop(guild_id, None)
        save_settings(guild_id)
        await interaction.response.send_message("❌ Log channel removed. Logs will no longer be sent anywhere.", ephemeral=True)

    elif act == "show":
        roles = authorized_roles.get(guild_id, set())
        banned = blacklisted_roles.get(guild_id, set())
        chans = entry_channels.get(guild_id, set())
        mults = multiplier_roles.get(guild_id, {})
        ping_role_id = default_ping_roles.get(guild_id)
        log_channel_id = log_channels.get(guild_id)
        color_hex = embed_colors.get(guild_id, "Default")

        roles_txt = ", ".join(f"<@&{r}>" for r in roles) or "None (Admins only)"
        banned_txt = ", ".join(f"<@&{r}>" for r in banned) or "None"
        chans_txt = ", ".join(f"<#{c}>" for c in chans) or "Default (each giveaway's own channel)"
        mults_txt = ", ".join(f"<@&{r}> ({m}x)" for r, m in mults.items()) or "None"
        ping_txt = f"<@&{ping_role_id}>" if ping_role_id else "None"
        log_txt = f"<#{log_channel_id}>" if log_channel_id else "Not set"

        embed = discord.Embed(title="⚙️ Giveaway Settings", color=get_guild_color(guild_id))
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else bot.user.display_avatar.url)
        embed.add_field(name="Who can host giveaways", value=roles_txt, inline=False)
        embed.add_field(name="Blocked from joining", value=banned_txt, inline=False)
        embed.add_field(name="Channels that count entries", value=chans_txt, inline=False)
        embed.add_field(name="Bonus entry roles", value=mults_txt, inline=False)
        embed.add_field(name="Default ping role", value=ping_txt, inline=True)
        embed.add_field(name="Embed color", value=color_hex, inline=True)
        embed.add_field(name="Log channel", value=log_txt, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- GIVEAWAY COMMANDS ----------

@bot.hybrid_command(name="start-giveaway", description="Create a new giveaway with a join button")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    duration="How long the giveaway runs, e.g. 30m, 1h, 2d, 1h30m",
    prize="What you're giving away",
    winners="Number of winners to pick (default: 1)",
    required_role="Only members with this role are allowed to join",
    blacklist_role="Members with this role are blocked from joining (this giveaway only)",
    bypass_role="Members with this role skip the required role and blacklist checks",
    image_url="A direct image link to display in the giveaway post",
    ping_role="A role to mention (overrides the server's default ping role, if any set in /setup)"
)
async def start_giveaway(
    ctx,
    duration: str,
    prize: str,
    winners: typing.Optional[int] = 1,
    required_role: typing.Optional[discord.Role] = None,
    blacklist_role: typing.Optional[discord.Role] = None,
    bypass_role: typing.Optional[discord.Role] = None,
    image_url: typing.Optional[str] = None,
    ping_role: typing.Optional[discord.Role] = None,
):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to start giveaways.")
        return

    if len(prize) > 200:
        await ctx.send("Prize text is too long (max 200 characters).")
        return

    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("Invalid duration. Use formats like `30m`, `1h`, `2d`, or `1h30m`.")
        return
    if seconds > 30 * 86400:
        await ctx.send("Duration can't be longer than 30 days.")
        return

    if winners < 1:
        winners = 1
    if winners > 20:
        await ctx.send("Max 20 winners per giveaway.")
        return

    if image_url and not re.match(r"^https?://\S+\.(png|jpg|jpeg|gif|webp)$", image_url, re.IGNORECASE):
        await ctx.send("Image URL must be a direct link ending in .png, .jpg, .jpeg, .gif, or .webp.")
        return

    end_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    timestamp = discord.utils.format_dt(end_time, style="R")
    guild_color = get_guild_color(ctx.guild.id)

    embed = discord.Embed(
        title="🎉  G I V E A W A Y  🎉",
        description=f"✨ **{prize}** ✨\n\nClick **🎉 Join Giveaway** below to enter, then chat in the allowed channel(s) — every message earns an entry!\n*Click Join again anytime to leave.*",
        color=guild_color
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="🎤 Hosted by", value=ctx.author.mention, inline=True)
    embed.add_field(name="⏰ Ends", value=timestamp, inline=True)
    embed.add_field(name="🏆 Winners", value=str(winners), inline=True)
    if required_role:
        embed.add_field(name="🔑 Required role", value=required_role.mention, inline=True)
    if blacklist_role:
        embed.add_field(name="🚫 Blacklisted role", value=blacklist_role.mention, inline=True)
    if bypass_role:
        embed.add_field(name="⚡ Bypass role", value=bypass_role.mention, inline=True)

    guild_mults = multiplier_roles.get(ctx.guild.id, {})
    if guild_mults:
        mults_text = "\n".join(f"<@&{rid}> — **{mult}x** entries" for rid, mult in guild_mults.items())
        embed.add_field(name="⭐ Bonus entry roles", value=mults_text, inline=False)

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(text="Starting... • ChillBot 😎", icon_url=bot.user.display_avatar.url)

    final_ping_role = ping_role or (ctx.guild.get_role(default_ping_roles[ctx.guild.id]) if ctx.guild.id in default_ping_roles else None)
    content = final_ping_role.mention if final_ping_role else None

    msg = await ctx.send(content=content, embed=embed)
    view = GiveawayView(msg.id)

    giveaways[msg.id] = {
        "channel_id": ctx.channel.id,
        "guild_id": ctx.guild.id,
        "host_id": ctx.author.id,
        "prize": prize,
        "winners": winners,
        "required_role_id": required_role.id if required_role else None,
        "blacklist_role_id": blacklist_role.id if blacklist_role else None,
        "bypass_role_id": bypass_role.id if bypass_role else None,
        "joined_users": set(),
        "entries": [],
        "active": True,
        "end_time": end_time,
    }
    save_giveaway(msg.id)

    embed.set_footer(text=f"Giveaway ID: {msg.id} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
    await msg.edit(embed=embed, view=view)

    log_embed = discord.Embed(
        title="🎉 Giveaway Started",
        description=f"**Prize:** {prize}\n**Duration:** {duration}\n**Winners:** {winners}",
        color=guild_color
    )
    log_embed.add_field(name="Hosted by", value=ctx.author.mention, inline=True)
    log_embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
    log_embed.set_footer(text=f"Giveaway ID: {msg.id}")
    await send_log(ctx.guild, log_embed)

    await discord.utils.sleep_until(end_time)
    if giveaways.get(msg.id, {}).get("active"):
        await end_giveaway(msg.id)


async def end_giveaway(giveaway_id):
    gw = giveaways.get(giveaway_id)
    if not gw or not gw["active"]:
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    if now < gw["end_time"]:
        # end_time was pushed later (e.g. via /edit-giveaway) — wait again instead of ending early
        bot.loop.create_task(resume_giveaway_timer(giveaway_id, gw["end_time"]))
        return

    gw["active"] = False
    channel = bot.get_channel(gw["channel_id"])
    if channel is None:
        delete_giveaway_doc(giveaway_id)
        return

    guild_color = get_guild_color(gw["guild_id"])

    if not gw["entries"]:
        embed = discord.Embed(
            title="🎉 Giveaway Ended",
            description=f"**Prize:** {gw['prize']}\n\n😢 Nobody entered — no winner this time.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Giveaway ID: {giveaway_id} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
        await channel.send(embed=embed)

        log_embed = discord.Embed(
            title="🎉 Giveaway Ended (No Entries)",
            description=f"**Prize:** {gw['prize']}",
            color=discord.Color.red()
        )
        log_embed.set_footer(text=f"Giveaway ID: {giveaway_id}")
        await send_log(channel.guild, log_embed)
        delete_giveaway_doc(giveaway_id)
        return

    pool = list(gw["entries"])
    chosen = []
    num_winners = min(gw["winners"], len(set(pool)))

    for _ in range(num_winners):
        if not pool:
            break
        pick = random.choice(pool)
        chosen.append(pick)
        pool = [uid for uid in pool if uid != pick]
        record_win(gw["guild_id"], pick)

    winners_text = "\n".join(f"🏆 <@{uid}>" for uid in chosen)

    embed = discord.Embed(
        title="🎉 Giveaway Ended!",
        description=f"**Prize:** {gw['prize']}\n\n**Winner(s):**\n{winners_text}\n\nCongratulations! 🎊",
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name="🎤 Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    embed.set_footer(text=f"Giveaway ID: {giveaway_id} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
    await channel.send(embed=embed)

    log_embed = discord.Embed(
        title="🎉 Giveaway Ended",
        description=f"**Prize:** {gw['prize']}\n**Winner(s):** {winners_text}",
        color=discord.Color.green()
    )
    log_embed.add_field(name="Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    log_embed.set_footer(text=f"Giveaway ID: {giveaway_id}")
    await send_log(channel.guild, log_embed)

    delete_giveaway_doc(giveaway_id)


@bot.hybrid_command(name="end-giveaway", description="End a giveaway early and pick the winner(s) now")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(message_id="The giveaway's ID, shown in small text at the bottom of the giveaway post")
async def end_giveaway_cmd(ctx, message_id: str):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to end giveaways.")
        return
    try:
        gid = int(message_id)
    except ValueError:
        await ctx.send("That doesn't look like a valid giveaway ID.")
        return

    gw = giveaways.get(gid)
    if not gw or not gw["active"]:
        await ctx.send("No active giveaway found with that ID.")
        return

    if gw["host_id"] != ctx.author.id and not is_admin_or_owner(ctx.author):
        allowed = authorized_roles.get(ctx.guild.id, set())
        user_role_ids = {r.id for r in ctx.author.roles}
        if not (allowed & user_role_ids):
            await ctx.send("You can only end giveaways you hosted, unless you're an admin or authorized role.")
            return

    await end_giveaway(gid)
    await ctx.send("Giveaway ended.")

    log_embed = discord.Embed(
        title="⏹️ Giveaway Ended Early",
        description=f"Ended manually by {ctx.author.mention}",
        color=discord.Color.orange()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(ctx.guild, log_embed)


@bot.hybrid_command(name="remove-participant", description="Remove someone from an active or ended giveaway")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    message_id="The giveaway's ID, shown in small text at the bottom of the giveaway post",
    member="The person to remove"
)
async def remove_participant(ctx, message_id: str, member: discord.Member):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to manage giveaway entries.")
        return
    try:
        gid = int(message_id)
    except ValueError:
        await ctx.send("That doesn't look like a valid giveaway ID.")
        return
    gw = giveaways.get(gid)
    if not gw:
        await ctx.send("No giveaway found with that ID.")
        return
    gw["joined_users"].discard(member.id)
    gw["entries"] = [uid for uid in gw["entries"] if uid != member.id]
    save_giveaway(gid)
    await ctx.send(f"Removed {member.mention} from that giveaway.")

    log_embed = discord.Embed(
        title="🚫 Participant Removed",
        description=f"{member.mention} was removed from a giveaway by {ctx.author.mention}",
        color=discord.Color.orange()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(ctx.guild, log_embed)


@bot.hybrid_command(name="cancel-giveaway", description="Cancel a running giveaway without picking a winner")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(message_id="The giveaway's ID, shown in small text at the bottom of the giveaway post")
async def cancel_giveaway(ctx, message_id: str):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to cancel giveaways.")
        return
    try:
        gid = int(message_id)
    except ValueError:
        await ctx.send("That doesn't look like a valid giveaway ID.")
        return

    gw = giveaways.get(gid)
    if not gw or not gw["active"]:
        await ctx.send("No active giveaway found with that ID.")
        return

    if gw["host_id"] != ctx.author.id and not is_admin_or_owner(ctx.author):
        allowed = authorized_roles.get(ctx.guild.id, set())
        user_role_ids = {r.id for r in ctx.author.roles}
        if not (allowed & user_role_ids):
            await ctx.send("You can only cancel giveaways you hosted, unless you're an admin or authorized role.")
            return

    gw["active"] = False
    channel = bot.get_channel(gw["channel_id"])

    embed = discord.Embed(
        title="🚫 Giveaway Cancelled",
        description=f"**Prize:** {gw['prize']}\n\nThis giveaway was cancelled by {ctx.author.mention} — no winner was picked.",
        color=discord.Color.red()
    )
    embed.set_footer(text=f"Giveaway ID: {gid} • ChillBot 😎")

    if channel:
        try:
            msg = await channel.fetch_message(gid)
            await msg.edit(embed=embed, view=None)
        except Exception:
            await channel.send(embed=embed)

    delete_giveaway_doc(gid)
    giveaways.pop(gid, None)
    await ctx.send("Giveaway cancelled.")

    log_embed = discord.Embed(
        title="🚫 Giveaway Cancelled",
        description=f"Cancelled by {ctx.author.mention}, no winner picked",
        color=discord.Color.red()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(ctx.guild, log_embed)


@bot.hybrid_command(name="edit-giveaway", description="Fix a mistake in a running giveaway")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    message_id="The giveaway's ID, shown in small text at the bottom of the giveaway post",
    prize="New prize text (leave blank to keep current)",
    duration="New remaining time from now, e.g. 30m, 1h, 2d (leave blank to keep current end time)",
    winners="New number of winners (leave blank to keep current)"
)
async def edit_giveaway(
    ctx,
    message_id: str,
    prize: typing.Optional[str] = None,
    duration: typing.Optional[str] = None,
    winners: typing.Optional[int] = None,
):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to edit giveaways.")
        return
    try:
        gid = int(message_id)
    except ValueError:
        await ctx.send("That doesn't look like a valid giveaway ID.")
        return

    gw = giveaways.get(gid)
    if not gw or not gw["active"]:
        await ctx.send("No active giveaway found with that ID.")
        return

    if gw["host_id"] != ctx.author.id and not is_admin_or_owner(ctx.author):
        allowed = authorized_roles.get(ctx.guild.id, set())
        user_role_ids = {r.id for r in ctx.author.roles}
        if not (allowed & user_role_ids):
            await ctx.send("You can only edit giveaways you hosted, unless you're an admin or authorized role.")
            return

    if prize:
        if len(prize) > 200:
            await ctx.send("Prize text is too long (max 200 characters).")
            return
        gw["prize"] = prize

    if duration:
        seconds = parse_duration(duration)
        if seconds is None:
            await ctx.send("Invalid duration. Use formats like `30m`, `1h`, `2d`, or `1h30m`.")
            return
        gw["end_time"] = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)

    if winners:
        if winners < 1 or winners > 20:
            await ctx.send("Winners must be between 1 and 20.")
            return
        gw["winners"] = winners

    save_giveaway(gid)

    channel = bot.get_channel(gw["channel_id"])
    if channel:
        try:
            msg = await channel.fetch_message(gid)
            timestamp = discord.utils.format_dt(gw["end_time"], style="R")
            embed = msg.embeds[0]
            embed.description = f"✨ **{gw['prize']}** ✨\n\nClick **🎉 Join Giveaway** below to enter, then chat in the allowed channel(s) — every message earns an entry!\n*Click Join again anytime to leave.*"
            for i, field in enumerate(embed.fields):
                if field.name == "⏰ Ends":
                    embed.set_field_at(i, name="⏰ Ends", value=timestamp, inline=True)
                elif field.name == "🏆 Winners":
                    embed.set_field_at(i, name="🏆 Winners", value=str(gw["winners"]), inline=True)
            await msg.edit(embed=embed)
        except Exception as e:
            print(f"Edit giveaway message error: {e}", flush=True)

    await ctx.send("✅ Giveaway updated.")

    log_embed = discord.Embed(
        title="✏️ Giveaway Edited",
        description=f"Edited by {ctx.author.mention}",
        color=discord.Color.orange()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(ctx.guild, log_embed)


@bot.hybrid_command(name="embed", description="[Admin] Send a custom embed message")
@app_commands.default_permissions(manage_guild=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    title="The embed's title",
    description="The embed's main text",
    color="Optional hex color, e.g. #FF5733 (defaults to this server's embed color)",
    channel="Optional channel to send it to (defaults to the current channel)"
)
async def embed_cmd(
    ctx,
    title: str,
    description: str,
    color: typing.Optional[str] = None,
    channel: typing.Optional[discord.TextChannel] = None,
):
    if not is_admin_or_owner(ctx.author):
        await ctx.send("This command is for Administrators only.")
        return

    if len(title) > 256:
        await ctx.send("Title is too long (max 256 characters).")
        return
    if len(description) > 4000:
        await ctx.send("Description is too long (max 4000 characters).")
        return

    embed_color = get_guild_color(ctx.guild.id)
    if color:
        if not re.match(r"^#?[0-9A-Fa-f]{6}$", color):
            await ctx.send("Invalid hex color. Use a format like #FF5733.")
            return
        embed_color = discord.Color.from_str(color if color.startswith("#") else f"#{color}")

    embed = discord.Embed(title=title, description=description, color=embed_color)
    embed.set_footer(text="ChillBot 😎")

    target_channel = channel or ctx.channel
    try:
        await target_channel.send(embed=embed)
        if target_channel.id != ctx.channel.id:
            await ctx.send(f"✅ Sent to {target_channel.mention}.")
        else:
            await ctx.send("✅ Sent.")
    except discord.Forbidden:
        await ctx.send("I don't have permission to send messages in that channel.")


@bot.hybrid_command(name="leaderboard", description="See who's won the most giveaways in this server")
async def leaderboard(ctx):
    guild_wins = win_counts.get(ctx.guild.id, {})
    if not guild_wins:
        await ctx.send("No giveaways have been won yet in this server.")
        return

    sorted_wins = sorted(guild_wins.items(), key=lambda x: x[1], reverse=True)[:10]

    medals = ["🥇", "🥈", "🥉"]
    lines = []
    for i, (uid, wins) in enumerate(sorted_wins):
        prefix = medals[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} <@{uid}> — **{wins}** win{'s' if wins != 1 else ''}")

    embed = discord.Embed(
        title="🏆 Giveaway Leaderboard",
        description="\n".join(lines),
        color=get_guild_color(ctx.guild.id)
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed)


# ---------- FUN COMMANDS ----------

EIGHT_BALL_ANSWERS = [
    "It is certain. 🔮",
    "Without a doubt. ✨",
    "Yes, definitely. ✅",
    "You may rely on it. 🌟",
    "Most likely. 👍",
    "Outlook good. 🌤️",
    "Signs point to yes. ➡️",
    "Reply hazy, try again. 🌫️",
    "Ask again later. ⏳",
    "Better not tell you now. 🤐",
    "Cannot predict now. 🔮",
    "Concentrate and ask again. 🧘",
    "Don't count on it. ❌",
    "My reply is no. 🚫",
    "My sources say no. 📉",
    "Outlook not so good. ☁️",
    "Very doubtful. 🤨",
]


@bot.hybrid_command(name="8ball", description="Ask the magic 8-ball a question")
@app_commands.describe(question="What do you want to ask?")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def eight_ball(ctx, *, question: str):
    embed = discord.Embed(
        title="🎱 Magic 8-Ball",
        color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
    )
    embed.add_field(name="Question", value=question, inline=False)
    embed.add_field(name="Answer", value=random.choice(EIGHT_BALL_ANSWERS), inline=False)
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="roll", description="Roll dice, e.g. 2d20")
@app_commands.describe(dice="Format: [count]d[sides], e.g. 2d20 or 1d6")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def roll(ctx, dice: str = "1d6"):
    match = re.match(r"^(\d+)d(\d+)$", dice.strip().lower())
    if not match:
        await ctx.send("Invalid format. Use something like `2d20` (2 dice, 20 sides each).")
        return

    count, sides = int(match.group(1)), int(match.group(2))
    if count < 1 or count > 100:
        await ctx.send("Dice count must be between 1 and 100.")
        return
    if sides < 2 or sides > 1000:
        await ctx.send("Sides must be between 2 and 1000.")
        return

    rolls = [random.randint(1, sides) for _ in range(count)]
    total = sum(rolls)

    embed = discord.Embed(
        title="🎲 Dice Roll",
        description=f"Rolling **{dice}**...",
        color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
    )
    rolls_text = ", ".join(str(r) for r in rolls)
    if len(rolls_text) > 1000:
        rolls_text = rolls_text[:1000] + "..."
    embed.add_field(name="Results", value=rolls_text, inline=False)
    embed.add_field(name="Total", value=str(total), inline=False)
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="meme", description="Get a random meme")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def meme(ctx):
    await ctx.defer()
    try:
        async with aiohttp.ClientSession() as session:
            for _ in range(3):
                async with session.get("https://meme-api.com/gimme") as resp:
                    if resp.status != 200:
                        await ctx.send("Couldn't fetch a meme right now, try again in a bit.")
                        return
                    data = await resp.json()
                    if not data.get("nsfw"):
                        break
            else:
                await ctx.send("Couldn't find a clean meme right now, try again.")
                return

        embed = discord.Embed(
            title=data.get("title", "Random Meme"),
            color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
        )
        embed.set_image(url=data["url"])
        embed.set_footer(text=f"r/{data.get('subreddit', 'memes')} • ChillBot 😎")
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Meme command error: {e}", flush=True)
        await ctx.send("Something went wrong fetching a meme, try again.")


@bot.hybrid_command(name="coinflip", description="Flip a coin")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def coinflip(ctx):
    result = random.choice(["Heads", "Tails"])
    emoji = "🪙"
    embed = discord.Embed(
        title=f"{emoji} Coin Flip",
        description=f"The coin landed on **{result}**!",
        color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
    )
    await ctx.send(embed=embed)


RPS_EMOJIS = {"rock": "🪨", "paper": "📄", "scissors": "✂️"}
RPS_BEATS = {"rock": "scissors", "paper": "rock", "scissors": "paper"}


def rps_winner(choice1, choice2):
    if choice1 == choice2:
        return 0
    if RPS_BEATS[choice1] == choice2:
        return 1
    return 2


class RPSView(discord.ui.View):
    def __init__(self, player1_id, player2_id=None):
        super().__init__(timeout=60)
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.choices = {}

    async def handle_choice(self, interaction: discord.Interaction, choice: str):
        user_id = interaction.user.id

        if user_id != self.player1_id and user_id != self.player2_id:
            if self.player2_id is not None:
                await interaction.response.send_message("This isn't your game!", ephemeral=True)
                return

        if self.player2_id is None and user_id != self.player1_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return

        self.choices[user_id] = choice
        await interaction.response.send_message(f"You picked **{choice}** {RPS_EMOJIS[choice]}", ephemeral=True)

        opponent_id = self.player2_id if self.player2_id else "BOT"

        if self.player2_id is None:
            bot_choice = random.choice(list(RPS_EMOJIS.keys()))
            await self.reveal(interaction, self.choices[user_id], bot_choice, interaction.user.mention, "ChillBot 🤖")
        elif self.player1_id in self.choices and self.player2_id in self.choices:
            p1_choice = self.choices[self.player1_id]
            p2_choice = self.choices[self.player2_id]
            p1_mention = f"<@{self.player1_id}>"
            p2_mention = f"<@{self.player2_id}>"
            await self.reveal(interaction, p1_choice, p2_choice, p1_mention, p2_mention)

    async def reveal(self, interaction, choice1, choice2, name1, name2):
        result = rps_winner(choice1, choice2)
        if result == 0:
            outcome = "🤝 It's a tie!"
        elif result == 1:
            outcome = f"🎉 {name1} wins!"
        else:
            outcome = f"🎉 {name2} wins!"

        embed = discord.Embed(
            title="✊ 📄 ✂️ Rock Paper Scissors",
            description=(
                f"{name1} picked {RPS_EMOJIS[choice1]}\n"
                f"{name2} picked {RPS_EMOJIS[choice2]}\n\n"
                f"**{outcome}**"
            ),
            color=get_guild_color(interaction.guild.id) if interaction.guild else DEFAULT_COLOR
        )
        for child in self.children:
            child.disabled = True
        try:
            await interaction.message.edit(embed=embed, view=self)
        except Exception:
            pass
        self.stop()

    @discord.ui.button(label="Rock 🪨", style=discord.ButtonStyle.grey)
    async def rock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "rock")

    @discord.ui.button(label="Paper 📄", style=discord.ButtonStyle.grey)
    async def paper_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "paper")

    @discord.ui.button(label="Scissors ✂️", style=discord.ButtonStyle.grey)
    async def scissors_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.handle_choice(interaction, "scissors")


@bot.hybrid_command(name="game", description="Play rock-paper-scissors against ChillBot or challenge a friend")
@app_commands.describe(opponent="Optional: challenge this member instead of playing against ChillBot")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def game(ctx, opponent: typing.Optional[discord.Member] = None):
    if opponent and opponent.bot:
        await ctx.send("You can't challenge a bot to this game.")
        return
    if opponent and opponent.id == ctx.author.id:
        await ctx.send("You can't challenge yourself!")
        return

    if opponent:
        description = f"{ctx.author.mention} has challenged {opponent.mention} to Rock, Paper, Scissors!\nBoth players, pick your move below (your pick stays hidden until both choose)."
        view = RPSView(ctx.author.id, opponent.id)
    else:
        description = f"{ctx.author.mention} is playing against ChillBot! Pick your move:"
        view = RPSView(ctx.author.id, None)

    embed = discord.Embed(
        title="✊ 📄 ✂️ Rock Paper Scissors",
        description=description,
        color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
    )
    await ctx.send(embed=embed, view=view)


@bot.hybrid_command(name="ping", description="Check ChillBot's latency")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def ping(ctx):
    latency = round(bot.latency * 1000)
    embed = discord.Embed(
        description=f"🏓 Pong! **{latency}ms**",
        color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
    )
    await ctx.send(embed=embed)


# ---------- ANYWHERE COMMANDS (work in DMs, group DMs, and servers) ----------

LANGUAGE_CODES = {
    "english": "en", "spanish": "es", "french": "fr", "german": "de",
    "italian": "it", "portuguese": "pt", "dutch": "nl", "russian": "ru",
    "japanese": "ja", "korean": "ko", "chinese": "zh", "arabic": "ar",
    "hindi": "hi", "turkish": "tr", "polish": "pl", "swedish": "sv",
    "greek": "el", "hebrew": "he", "vietnamese": "vi", "thai": "th",
    "indonesian": "id", "tamil": "ta", "malayalam": "ml", "bengali": "bn",
    "urdu": "ur",
}


@bot.hybrid_command(name="translate", description="Translate text into another language")
@app_commands.describe(text="The text to translate", language="Target language, e.g. Spanish or es")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def translate(ctx, language: str, *, text: str):
    if len(text) > 500:
        await ctx.send("Text is too long (max 500 characters).")
        return

    target = LANGUAGE_CODES.get(language.strip().lower(), language.strip().lower())

    await ctx.defer()
    try:
        async with aiohttp.ClientSession() as session:
            url = "https://api.mymemory.translated.net/get"
            params = {"q": text, "langpair": f"autodetect|{target}"}
            async with session.get(url, params=params) as resp:
                data = await resp.json()

        translated = data.get("responseData", {}).get("translatedText")
        if not translated:
            await ctx.send("Couldn't translate that — check the language name/code and try again.")
            return

        embed = discord.Embed(title="🌐 Translation", color=DEFAULT_COLOR)
        embed.add_field(name="Original", value=text[:1000], inline=False)
        embed.add_field(name=f"Translated ({target})", value=translated[:1000], inline=False)
        embed.set_footer(text="ChillBot 😎")
        await ctx.send(embed=embed)
    except Exception as e:
        print(f"Translate error: {e}", flush=True)
        await ctx.send("Something went wrong translating that, try again.")


WORDLE_WORDS = [
    "apple", "beach", "chair", "dance", "eagle", "flame", "grape", "house",
    "input", "joker", "knife", "lemon", "mango", "night", "ocean", "piano",
    "queen", "river", "storm", "tiger", "umbra", "vivid", "world", "xenon",
    "yield", "zebra", "bread", "cloud", "dream", "earth", "frost", "ghost",
    "heart", "ideal", "jolly", "koala", "light", "money", "noble", "olive",
    "power", "quick", "robot", "smile", "train", "unity", "voice", "water",
]

wordle_games = {}


def wordle_feedback(guess, word):
    result = ["⬛"] * 5
    word_chars = list(word)

    for i in range(5):
        if guess[i] == word[i]:
            result[i] = "🟩"
            word_chars[i] = None

    for i in range(5):
        if result[i] == "⬛" and guess[i] in word_chars:
            result[i] = "🟨"
            word_chars[word_chars.index(guess[i])] = None

    return result


@bot.hybrid_command(name="wordle", description="Play a game of Wordle — guess the 5-letter word in 6 tries")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def wordle(ctx):
    existing = wordle_games.get(ctx.author.id)
    if existing and not existing["over"]:
        await ctx.send(f"You already have a game in progress! You've used {len(existing['guesses'])}/6 guesses. Just type a 5-letter word to guess.")
        return

    word = random.choice(WORDLE_WORDS)
    wordle_games[ctx.author.id] = {"word": word, "guesses": [], "over": False}

    embed = discord.Embed(
        title="🟩 Wordle",
        description="I picked a 5-letter word! Just type your guess as a normal message (no slash command needed) — you get 6 tries.",
        color=DEFAULT_COLOR
    )
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="minesweeper", description="Generate a spoiler-tag minesweeper grid")
@app_commands.describe(size="Grid size, e.g. 5 for a 5x5 board (default 5, max 8)", bombs="Number of bombs (default 5)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def minesweeper(ctx, size: typing.Optional[int] = 5, bombs: typing.Optional[int] = 5):
    if size < 3 or size > 8:
        await ctx.send("Size must be between 3 and 8.")
        return
    total_cells = size * size
    if bombs < 1 or bombs >= total_cells:
        await ctx.send(f"Bombs must be between 1 and {total_cells - 1}.")
        return

    bomb_positions = set(random.sample(range(total_cells), bombs))
    digit_emojis = ["⬛", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]

    def neighbors(pos):
        row, col = divmod(pos, size)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                r, c = row + dr, col + dc
                if 0 <= r < size and 0 <= c < size:
                    yield r * size + c

    rows_out = []
    for row in range(size):
        cells = []
        for col in range(size):
            pos = row * size + col
            if pos in bomb_positions:
                cells.append("||💣||")
            else:
                count = sum(1 for n in neighbors(pos) if n in bomb_positions)
                cells.append(f"||{digit_emojis[count]}||")
        rows_out.append("".join(cells))

    grid_text = "\n".join(rows_out)
    embed = discord.Embed(
        title="💣 Minesweeper",
        description=f"{grid_text}\n\n**Bombs:** {bombs} • **Grid:** {size}x{size}",
        color=DEFAULT_COLOR
    )
    embed.set_footer(text="Tap a spoiler to reveal it • ChillBot 😎")
    await ctx.send(embed=embed)


class HigherLowerView(discord.ui.View):
    def __init__(self, player_id, deck, current_card):
        super().__init__(timeout=60)
        self.player_id = player_id
        self.deck = deck
        self.current_card = current_card
        self.score = 0

    @staticmethod
    def card_label(card):
        rank, suit = card
        return f"{rank}{suit}"

    async def guess(self, interaction: discord.Interaction, direction: str):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return

        if not self.deck:
            for child in self.children:
                child.disabled = True
            await interaction.response.edit_message(view=self)
            return

        next_card = self.deck.pop()
        current_value = self.current_card[2]
        next_value = next_card[2]

        correct = (direction == "higher" and next_value > current_value) or \
                  (direction == "lower" and next_value < current_value) or \
                  (next_value == current_value)

        if correct:
            self.score += 1
            self.current_card = next_card
            desc = f"**{self.card_label(next_card)}** — Correct! 🎉\nScore: **{self.score}**\n\nNext card, higher or lower than **{self.card_label(next_card)}**?"
            color = discord.Color.green()
        else:
            desc = f"**{self.card_label(next_card)}** — Wrong! 💀\nFinal score: **{self.score}**"
            color = discord.Color.red()
            for child in self.children:
                child.disabled = True

        if not self.deck:
            for child in self.children:
                child.disabled = True
            desc += "\n\n🎉 You cleared the whole deck!"

        embed = discord.Embed(title="🎴 Higher or Lower", description=desc, color=color)
        embed.set_footer(text="ChillBot 😎")
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="⬆️ Higher", style=discord.ButtonStyle.green)
    async def higher_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.guess(interaction, "higher")

    @discord.ui.button(label="⬇️ Lower", style=discord.ButtonStyle.red)
    async def lower_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.guess(interaction, "lower")


@bot.hybrid_command(name="higher-lower", description="Guess if the next card is higher or lower")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def higher_lower(ctx):
    ranks = [("2", 2), ("3", 3), ("4", 4), ("5", 5), ("6", 6), ("7", 7), ("8", 8),
             ("9", 9), ("10", 10), ("J", 11), ("Q", 12), ("K", 13), ("A", 14)]
    suits = ["♠️", "♥️", "♦️", "♣️"]
    deck = [(rank, suit, value) for rank, value in ranks for suit in suits]
    random.shuffle(deck)

    current_card = deck.pop()
    view = HigherLowerView(ctx.author.id, deck, current_card)

    embed = discord.Embed(
        title="🎴 Higher or Lower",
        description=f"Current card: **{current_card[0]}{current_card[1]}**\n\nWill the next card be higher or lower?",
        color=DEFAULT_COLOR
    )
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed, view=view)


class Connect4View(discord.ui.View):
    def __init__(self, player1_id, player2_id):
        super().__init__(timeout=300)
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.board = [[0] * 7 for _ in range(6)]  # 0 empty, 1 player1, 2 player2
        self.turn = player1_id
        self.over = False

        for col in range(7):
            self.add_item(Connect4Button(col))

    def render_board(self):
        symbols = {0: "⚪", 1: "🔴", 2: "🟡"}
        lines = []
        for row in self.board:
            lines.append("".join(symbols[cell] for cell in row))
        return "\n".join(lines)

    def drop_piece(self, col, player_num):
        for row in range(5, -1, -1):
            if self.board[row][col] == 0:
                self.board[row][col] = player_num
                return row
        return None

    def check_winner(self, player_num):
        board = self.board
        for r in range(6):
            for c in range(4):
                if all(board[r][c + i] == player_num for i in range(4)):
                    return True
        for r in range(3):
            for c in range(7):
                if all(board[r + i][c] == player_num for i in range(4)):
                    return True
        for r in range(3):
            for c in range(4):
                if all(board[r + i][c + i] == player_num for i in range(4)):
                    return True
        for r in range(3, 6):
            for c in range(4):
                if all(board[r - i][c + i] == player_num for i in range(4)):
                    return True
        return False

    def is_full(self):
        return all(self.board[0][c] != 0 for c in range(7))


class Connect4Button(discord.ui.Button):
    def __init__(self, col):
        super().__init__(label=str(col + 1), style=discord.ButtonStyle.blurple, row=col // 4)
        self.col = col

    async def callback(self, interaction: discord.Interaction):
        view: Connect4View = self.view

        if view.over:
            await interaction.response.send_message("This game has ended.", ephemeral=True)
            return
        if interaction.user.id != view.turn:
            await interaction.response.send_message("It's not your turn!", ephemeral=True)
            return

        player_num = 1 if interaction.user.id == view.player1_id else 2
        row = view.drop_piece(self.col, player_num)
        if row is None:
            await interaction.response.send_message("That column is full!", ephemeral=True)
            return

        if row == 0:
            self.disabled = True

        if view.check_winner(player_num):
            view.over = True
            for child in view.children:
                child.disabled = True
            embed = discord.Embed(
                title="🔴 🟡 Connect 4",
                description=f"{view.render_board()}\n\n🎉 <@{interaction.user.id}> wins!",
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=view)
            return

        if view.is_full():
            view.over = True
            for child in view.children:
                child.disabled = True
            embed = discord.Embed(
                title="🔴 🟡 Connect 4",
                description=f"{view.render_board()}\n\n🤝 It's a draw!",
                color=discord.Color.orange()
            )
            await interaction.response.edit_message(embed=embed, view=view)
            return

        view.turn = view.player2_id if interaction.user.id == view.player1_id else view.player1_id
        embed = discord.Embed(
            title="🔴 🟡 Connect 4",
            description=f"{view.render_board()}\n\nIt's <@{view.turn}>'s turn.",
            color=DEFAULT_COLOR
        )
        await interaction.response.edit_message(embed=embed, view=view)


@bot.hybrid_command(name="connect4", description="Play Connect 4 against a friend")
@app_commands.describe(opponent="Who do you want to challenge?")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def connect4(ctx, opponent: discord.Member):
    if opponent.bot:
        await ctx.send("You can't challenge a bot to this game.")
        return
    if opponent.id == ctx.author.id:
        await ctx.send("You can't challenge yourself!")
        return

    view = Connect4View(ctx.author.id, opponent.id)
    embed = discord.Embed(
        title="🔴 🟡 Connect 4",
        description=f"{view.render_board()}\n\n{ctx.author.mention} (🔴) vs {opponent.mention} (🟡)\n\nIt's {ctx.author.mention}'s turn.",
        color=DEFAULT_COLOR
    )
    await ctx.send(embed=embed, view=view)


# ---------- MENTION HANDLING ----------

def get_uptime_string():
    delta = datetime.datetime.now(datetime.timezone.utc) - BOT_START_TIME
    days, remainder = divmod(int(delta.total_seconds()), 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, _ = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    parts.append(f"{minutes}m")
    return " ".join(parts)


async def handle_mention(message):
    if message.author.id == OWNER_ID:
        await message.channel.send(random.choice(OWNER_REPLIES))
        return

    if message.guild and is_admin_or_owner(message.author):
        active_count = sum(1 for gw in giveaways.values() if gw["active"] and gw["guild_id"] == message.guild.id)
        embed = discord.Embed(
            title="😎 ChillBot Status",
            color=get_guild_color(message.guild.id)
        )
        embed.set_thumbnail(url=bot.user.display_avatar.url)
        embed.add_field(name="Status", value="🟢 Online", inline=True)
        embed.add_field(name="Latency", value=f"{round(bot.latency * 1000)}ms", inline=True)
        embed.add_field(name="Uptime", value=get_uptime_string(), inline=True)
        embed.add_field(name="Active giveaways here", value=str(active_count), inline=True)
        embed.add_field(name="Servers", value=str(len(bot.guilds)), inline=True)
        embed.add_field(name="Owner", value=OWNER_NAME, inline=True)
        embed.set_footer(text="ChillBot 😎")
        await message.channel.send(embed=embed)
        return

    embed = discord.Embed(
        description="Hey there! 👋 Use **/help** to see what I can do.",
        color=get_guild_color(message.guild.id) if message.guild else DEFAULT_COLOR
    )
    await message.channel.send(embed=embed)


# ---------- MESSAGE HANDLING ----------

@bot.event
async def on_message(message):
    try:
        if message.author.bot:
            return

        if bot.user in message.mentions:
            await handle_mention(message)
            return

        if message.author.id in wordle_games and not wordle_games[message.author.id]["over"]:
            content = message.content.strip().lower()
            if len(content) == 5 and content.isalpha():
                game = wordle_games[message.author.id]
                feedback = wordle_feedback(content, game["word"])
                game["guesses"].append((content, feedback))

                board_text = "\n".join(
                    f"{' '.join(fb)}\n{' '.join(g.upper())}" for g, fb in game["guesses"]
                )

                if content == game["word"]:
                    game["over"] = True
                    embed = discord.Embed(
                        title="🟩 Wordle — You Won!",
                        description=f"{board_text}\n\n🎉 You guessed it in {len(game['guesses'])}/6 tries!",
                        color=discord.Color.green()
                    )
                    await message.channel.send(embed=embed)
                elif len(game["guesses"]) >= 6:
                    game["over"] = True
                    embed = discord.Embed(
                        title="🟩 Wordle — Out of Tries",
                        description=f"{board_text}\n\nThe word was **{game['word'].upper()}**. Try `/wordle` again!",
                        color=discord.Color.red()
                    )
                    await message.channel.send(embed=embed)
                else:
                    embed = discord.Embed(
                        title=f"🟩 Wordle — Guess {len(game['guesses'])}/6",
                        description=board_text,
                        color=DEFAULT_COLOR
                    )
                    await message.channel.send(embed=embed)
                return

        if message.guild:
            for gid, gw in giveaways.items():
                if not gw["active"]:
                    continue
                if gw["guild_id"] != message.guild.id:
                    continue
                if message.author.id not in gw["joined_users"]:
                    continue
                if not channel_counts(message.guild.id, message.channel.id, gw["channel_id"]):
                    continue
                if message.content.startswith("?"):
                    continue

                mult = get_multiplier(message.guild.id, message.author)
                for _ in range(mult):
                    gw["entries"].append(message.author.id)
                save_giveaway(gid)

        await bot.process_commands(message)
    except Exception as e:
        print(f"on_message error: {e}", flush=True)


bot.run(os.environ["DISCORD_TOKEN"])
