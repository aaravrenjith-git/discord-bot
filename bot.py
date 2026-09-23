import os
import random
import asyncio
import discord
from discord import app_commands
from discord.ext import commands
import typing
import re
import datetime
import time
import html
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

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

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
users_col = db["users"]

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
jail_roles = {}

# economy: economy[guild_id][user_id] = {...}, saved to MongoDB in batches
economy = {}
economy_dirty = set()
economy_loaded = False
economy_tasks_started = False
jail_tasks = {}
active_trivia = set()
channel_msg_count = {}   # channel_id -> running count of (non-bot) messages, used to decide when a game should repost


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
        "jail_role": jail_roles.get(guild_id),
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
    user_docs = list(users_col.find({}))
    return settings_docs, giveaway_docs, user_docs


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
        if doc.get("jail_role"):
            jail_roles[guild_id] = doc["jail_role"]
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


class ConfirmLeaveView(discord.ui.View):
    def __init__(self, giveaway_id, user_id):
        super().__init__(timeout=30)
        self.giveaway_id = giveaway_id
        self.user_id = user_id

    @discord.ui.button(label="Confirm Leave", style=discord.ButtonStyle.danger, emoji="🚪")
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return

        gw = giveaways.get(self.giveaway_id)
        if not gw:
            await interaction.response.edit_message(content="This giveaway no longer exists.", embed=None, view=None)
            return

        gw["joined_users"].discard(self.user_id)
        gw["entries"] = [uid for uid in gw["entries"] if uid != self.user_id]
        save_giveaway(self.giveaway_id)

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content="You left the giveaway and your entries have been reset. 👋", view=self
        )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your confirmation.", ephemeral=True)
            return

        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="Staying in the giveaway. 🎉", view=self)


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
                confirm_view = ConfirmLeaveView(self.giveaway_id, user.id)
                current_entries = gw["entries"].count(user.id)
                await interaction.response.send_message(
                    f"⚠️ Are you sure you want to leave? You currently have **{current_entries}** entr{'y' if current_entries == 1 else 'ies'} — leaving will **reset them to 0**.",
                    view=confirm_view,
                    ephemeral=True
                )
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
        settings_docs, giveaway_docs, user_docs = await asyncio.wait_for(asyncio.to_thread(fetch_all_data_sync), timeout=15)
        await apply_loaded_data(settings_docs, giveaway_docs)
        await apply_user_data(user_docs)
    except asyncio.TimeoutError:
        print("Loading data from database timed out — continuing without it. Check MONGODB_URI / Atlas network access.", flush=True)
    except Exception as e:
        print(f"load_all_data error: {e}", flush=True)
    start_economy_tasks()


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
        color=get_guild_color(ctx.guild.id) if ctx.guild else DEFAULT_COLOR
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(
        name="🎉 Giveaway Commands (Admins or authorized roles)",
        value=(
            "**/start-giveaway** — Create a new giveaway (with a confirm step first)\n"
            "**/end-giveaway** — End a giveaway early and pick winner(s)\n"
            "**/cancel-giveaway** — Cancel a running giveaway without picking a winner\n"
            "**/edit-giveaway** — Fix a mistake in a running giveaway\n"
            "**/remove-participant** — Kick someone out of a giveaway\n"
        ),
        inline=False
    )
    embed.add_field(
        name="💰 Economy & Shop",
        value=(
            "**/balance** — Check how many coins you (or someone else) have\n"
            "**/daily** — Claim a free reward every 24 hours (streaks pay more!)\n"
            "**/work** — Do a mini-job to earn extra coins\n"
            "**/shop** — Spend coins on boosts and power-ups\n"
            "**/trivia** — Multiple-choice quiz for coins\n"
            "**/leaderboard** — Server or global rankings"
        ),
        inline=False
    )
    embed.add_field(
        name="🚔 Moderators",
        value=(
            "**/jail** — Give a member the jailed role for a set time\n"
            "**/unjail** — Let someone out of jail early"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ Admin Only",
        value=(
            "**/setup** — Configure host roles, blacklist, channels, ping role, jailed role, embed color, and log channel\n"
            "**/embed** — Send a custom embed message"
        ),
        inline=False
    )
    embed.add_field(
        name="🎮 Fun & Games",
        value=(
            "**/8ball** — Ask the magic 8-ball a question\n"
            "**/roll** — Roll dice, e.g. 2d20\n"
            "**/meme** — Get a random meme\n"
            "**/coinflip** — Flip a coin\n"
            "**/game** — Play rock-paper-scissors vs ChillBot or a friend\n"
            "**/higher-lower** — Guess if the next card is higher or lower\n"
            "**/minesweeper** — Click cells, don't hit a bomb!\n"
            "**/connect4** — Challenge a friend to Connect 4\n"
            "**/ping** — Check ChillBot's latency"
        ),
        inline=False
    )
    embed.add_field(
        name="🌍 Anywhere (DMs, group chats, or servers)",
        value=(
            "**/translate** — Translate text into another language\n"
            "**/wordle** — Guess the 5-letter word in 6 tries"
        ),
        inline=False
    )
    embed.set_footer(text=f"Owner: {OWNER_NAME} • ChillBot 😎")
    await ctx.send(embed=embed)


# ---------- SETUP COMMAND ----------

@bot.tree.command(name="setup", description="[Admin] Configure giveaway roles, channels, ping role, jailed role, embed color, and logs")
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
    app_commands.Choice(name="🔒 Set the jailed role (used by /jail)", value="set_jail_role"),
    app_commands.Choice(name="🔓 Remove the jailed role", value="remove_jail_role"),
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

    if act in ("add_role", "remove_role", "blacklist_role", "unblacklist_role", "add_multiplier", "remove_multiplier", "set_ping_role", "set_jail_role") and not role:
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

    elif act == "set_jail_role":
        if role.is_default():
            await interaction.response.send_message("You can't use @everyone as the jailed role — pick or create a dedicated role.", ephemeral=True)
            return
        jail_roles[guild_id] = role.id
        save_settings(guild_id)
        reply = f"🔒 {role.mention} is now the jailed role. Moderators can use **/jail** to send members there."
        if role.managed or role >= interaction.guild.me.top_role:
            reply += (
                "\n⚠️ I can't hand out this role yet — in Server Settings → Roles, drag my role above it "
                "(and make sure I have **Manage Roles**)."
            )
        reply += "\n💡 Tip: edit this role's permissions (or your channel permissions) so jailed members can't chat or see other channels."
        await interaction.response.send_message(reply, ephemeral=True)

    elif act == "remove_jail_role":
        jail_roles.pop(guild_id, None)
        save_settings(guild_id)
        await interaction.response.send_message(
            "🔓 Jailed role removed. Anyone already jailed keeps the role until their time is up.", ephemeral=True
        )

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
        jail_role_id = jail_roles.get(guild_id)
        log_channel_id = log_channels.get(guild_id)
        color_hex = embed_colors.get(guild_id, "Default")

        roles_txt = ", ".join(f"<@&{r}>" for r in roles) or "None (Admins only)"
        banned_txt = ", ".join(f"<@&{r}>" for r in banned) or "None"
        chans_txt = ", ".join(f"<#{c}>" for c in chans) or "Default (each giveaway's own channel)"
        mults_txt = ", ".join(f"<@&{r}> ({m}x)" for r, m in mults.items()) or "None"
        ping_txt = f"<@&{ping_role_id}>" if ping_role_id else "None"
        jail_txt = f"<@&{jail_role_id}>" if jail_role_id else "Not set"
        log_txt = f"<#{log_channel_id}>" if log_channel_id else "Not set"

        embed = discord.Embed(title="⚙️ Giveaway Settings", color=get_guild_color(guild_id))
        embed.set_thumbnail(url=interaction.guild.icon.url if interaction.guild.icon else bot.user.display_avatar.url)
        embed.add_field(name="Who can host giveaways", value=roles_txt, inline=False)
        embed.add_field(name="Blocked from joining", value=banned_txt, inline=False)
        embed.add_field(name="Channels that count entries", value=chans_txt, inline=False)
        embed.add_field(name="Bonus entry roles", value=mults_txt, inline=False)
        embed.add_field(name="Default ping role", value=ping_txt, inline=True)
        embed.add_field(name="Jailed role", value=jail_txt, inline=True)
        embed.add_field(name="Embed color", value=color_hex, inline=True)
        embed.add_field(name="Log channel", value=log_txt, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------- GIVEAWAY COMMANDS ----------

def build_giveaway_embed(ctx, prize, winners, timestamp, required_role, blacklist_role, bypass_role, image_url, guild_color, preview=False):
    title = "🔎 GIVEAWAY PREVIEW" if preview else "🎉  G I V E A W A Y  🎉"
    desc_intro = "This is what your giveaway will look like. Confirm to post it!\n\n" if preview else ""
    embed = discord.Embed(
        title=title,
        description=(
            f"{desc_intro}✨ **{prize}** ✨\n\n"
            "Click **🎉 Join Giveaway** below to enter, then chat in the allowed channel(s) — every message earns an entry!\n"
            "*Click Join again anytime to leave.*"
        ),
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

    return embed


async def publish_giveaway(ctx, duration, prize, winners, required_role, blacklist_role, bypass_role, image_url, ping_role, end_time):
    guild_color = get_guild_color(ctx.guild.id)
    timestamp = discord.utils.format_dt(end_time, style="R")

    # a fresh embed is built here, separate from the preview shown during confirmation
    embed = build_giveaway_embed(ctx, prize, winners, timestamp, required_role, blacklist_role, bypass_role, image_url, guild_color)
    embed.set_footer(text="Starting... • ChillBot 😎", icon_url=bot.user.display_avatar.url)

    final_ping_role = ping_role or (ctx.guild.get_role(default_ping_roles[ctx.guild.id]) if ctx.guild.id in default_ping_roles else None)
    content = final_ping_role.mention if final_ping_role else None

    msg = await ctx.channel.send(content=content, embed=embed)
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


class StartGiveawayConfirmView(discord.ui.View):
    def __init__(self, ctx, duration, prize, winners, required_role, blacklist_role, bypass_role, image_url, ping_role, end_time):
        super().__init__(timeout=60)
        self.ctx = ctx
        self.duration = duration
        self.prize = prize
        self.winners = winners
        self.required_role = required_role
        self.blacklist_role = blacklist_role
        self.bypass_role = bypass_role
        self.image_url = image_url
        self.ping_role = ping_role
        self.end_time = end_time
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.ctx.author.id:
            await interaction.response.send_message("Only the person who ran this command can confirm it.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="✅ Confirm & Start", style=discord.ButtonStyle.success)
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="✅ Giveaway posted below!", embed=None, view=self)
        await publish_giveaway(
            self.ctx, self.duration, self.prize, self.winners, self.required_role,
            self.blacklist_role, self.bypass_role, self.image_url, self.ping_role, self.end_time
        )

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.danger)
    async def cancel_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(content="❌ Giveaway cancelled — nothing was posted.", embed=None, view=self)

    async def on_timeout(self):
        if not self.done:
            for child in self.children:
                child.disabled = True
            try:
                await self.ctx.edit(content="⌛ Confirmation timed out — nothing was posted.", embed=None, view=self)
            except Exception:
                pass


@bot.hybrid_command(name="start-giveaway", description="Create a new giveaway (shows a preview to confirm first)")
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

    preview_embed = build_giveaway_embed(
        ctx, prize, winners, timestamp, required_role, blacklist_role, bypass_role, image_url, guild_color, preview=True
    )
    view = StartGiveawayConfirmView(
        ctx, duration, prize, winners, required_role, blacklist_role, bypass_role, image_url, ping_role, end_time
    )
    await ctx.send(
        content="Review your giveaway below, then confirm to post it publicly.",
        embed=preview_embed, view=view, ephemeral=True
    )


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


# ---------- ECONOMY, SHOP, TRIVIA & JAIL ----------

COIN = "🪙"
MEDALS = ["🥇", "🥈", "🥉"]
DAILY_BASE = 100
DAILY_STREAK_BONUS = 25     # extra coins per streak day (up to 10 extra days)
WORK_COOLDOWN = 3600        # 1 hour
TRIVIA_SECONDS = 20         # time to answer each question
TRIVIA_BONUS = [5, 3, 1]    # speed bonus for the 1st, 2nd and 3rd correct answer
JAIL_MAX_SECONDS = 30 * 86400

DEFAULT_USER = {
    "coins": 0,
    "last_daily": 0.0,
    "streak": 0,
    "last_work": 0.0,
    "trivia_points": 0,
    "jailed_until": None,
    "jail_role_id": None,
    "boost_multiplier": 1,
    "boost_until": 0.0,
    "boost_name": None,
}

SHOP_ITEMS = [
    {"id": "boost2x_1h", "name": "🚀 2x Coin Boost — 1 hour", "price": 250, "multiplier": 2, "hours": 1,
     "desc": "Doubles coins from /daily, /work and /trivia for 1 hour."},
    {"id": "boost2x_6h", "name": "🚀 2x Coin Boost — 6 hours", "price": 1200, "multiplier": 2, "hours": 6,
     "desc": "Doubles coins from /daily, /work and /trivia for 6 hours."},
    {"id": "boost3x_24h", "name": "🔥 3x Coin Boost — 24 hours", "price": 4000, "multiplier": 3, "hours": 24,
     "desc": "Triples coins from /daily, /work and /trivia for a full day."},
]


def get_user(guild_id, user_id):
    users = economy.setdefault(guild_id, {})
    u = users.get(user_id)
    if u is None:
        u = dict(DEFAULT_USER)
        users[user_id] = u
    return u


def mark_dirty(guild_id, user_id):
    economy_dirty.add((guild_id, user_id))


def active_multiplier(u):
    if u.get("boost_until", 0) > time.time():
        return u.get("boost_multiplier", 1)
    return 1


def rank_position(guild_id, user_id, key):
    users = economy.get(guild_id, {})
    ranked = sorted(
        ((uid, u[key]) for uid, u in users.items() if u[key] > 0),
        key=lambda x: x[1],
        reverse=True
    )
    for i, (uid, _) in enumerate(ranked, start=1):
        if uid == user_id:
            return i
    return None


def aggregate_global(key):
    totals = {}
    for users in economy.values():
        for uid, u in users.items():
            val = u.get(key, 0)
            if val > 0:
                totals[uid] = totals.get(uid, 0) + val
    return totals


def fmt_ts(ts, style="R"):
    return f"<t:{int(ts)}:{style}>"


# ----- saving / loading -----

def flush_economy_sync(batch):
    """Blocking MongoDB writes — runs in a background thread."""
    for doc in batch:
        users_col.replace_one({"_id": doc["_id"]}, doc, upsert=True)


async def economy_flush_loop():
    while True:
        await asyncio.sleep(15)
        if not economy_loaded or not economy_dirty:
            continue
        keys = list(economy_dirty)
        economy_dirty.clear()
        batch = []
        for gid, uid in keys:
            u = economy.get(gid, {}).get(uid)
            if u is not None:
                doc = {"_id": f"{gid}:{uid}", "guild_id": gid, "user_id": uid}
                doc.update(u)
                batch.append(doc)
        try:
            await asyncio.to_thread(flush_economy_sync, batch)
        except Exception as e:
            print(f"DB economy flush error: {e}", flush=True)
            economy_dirty.update(keys)


def start_economy_tasks():
    global economy_tasks_started
    if economy_tasks_started:
        return
    economy_tasks_started = True
    asyncio.create_task(economy_flush_loop())


async def apply_user_data(user_docs):
    global economy_loaded
    if economy_loaded:
        return
    now = time.time()
    for doc in user_docs:
        gid = doc["guild_id"]
        uid = doc["user_id"]
        u = dict(DEFAULT_USER)
        for key in DEFAULT_USER:
            if key in doc:
                u[key] = doc[key]
        economy.setdefault(gid, {})[uid] = u

        until = u.get("jailed_until")
        if until:
            if until <= now:
                asyncio.create_task(release_jail(gid, uid))
            else:
                schedule_jail_release(gid, uid, until)
    economy_loaded = True
    print(f"Loaded economy data for {len(user_docs)} member(s) from database.", flush=True)


# ----- leaderboard (server + global) -----

@bot.hybrid_command(name="leaderboard", description="See the top members — richest, trivia champs, giveaway winners")
@app_commands.describe(
    category="What to rank people by (default: coins)",
    scope="This server only, or across every server ChillBot is in (default: server)"
)
async def leaderboard(
    ctx,
    category: typing.Literal["coins", "trivia", "giveaway-wins"] = "coins",
    scope: typing.Literal["server", "global"] = "server",
):
    if not ctx.guild and scope == "server":
        await ctx.send("Server leaderboards only work inside a server — try `scope: global` instead.")
        return

    is_global = scope == "global"
    gid = ctx.guild.id if ctx.guild else None

    if category == "coins":
        title = f"{COIN} Richest Members"
        data = aggregate_global("coins") if is_global else {uid: u["coins"] for uid, u in economy.get(gid, {}).items() if u["coins"] > 0}

        def fmt(value):
            return f"**{value:,}** {COIN}"
    elif category == "trivia":
        title = "🧠 Trivia Champions"
        data = aggregate_global("trivia_points") if is_global else {uid: u["trivia_points"] for uid, u in economy.get(gid, {}).items() if u["trivia_points"] > 0}

        def fmt(value):
            return f"**{value:,}** pts"
    else:
        title = "🏆 Giveaway Winners"
        if is_global:
            totals = {}
            for guild_wins in win_counts.values():
                for uid, wins in guild_wins.items():
                    totals[uid] = totals.get(uid, 0) + wins
            data = totals
        else:
            data = dict(win_counts.get(gid, {}))

        def fmt(value):
            return f"**{value}** win{'s' if value != 1 else ''}"

    title += " (Global)" if is_global else " (This Server)"

    if not data:
        await ctx.send("Nobody is on this leaderboard yet — chat, claim `/daily`, or start a `/trivia`!")
        return

    ranked = sorted(data.items(), key=lambda x: x[1], reverse=True)
    lines = []
    for i, (uid, value) in enumerate(ranked[:10]):
        prefix = MEDALS[i] if i < 3 else f"**{i + 1}.**"
        lines.append(f"{prefix} <@{uid}> — {fmt(value)}")

    embed = discord.Embed(title=title, description="\n".join(lines), color=get_guild_color(gid) if gid else DEFAULT_COLOR)
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    your_pos = next((i for i, (uid, _) in enumerate(ranked, start=1) if uid == ctx.author.id), None)
    footer = f"Your rank: #{your_pos}" if your_pos else "You're not ranked yet"
    embed.set_footer(text=f"{footer} • ChillBot 😎")
    await ctx.send(embed=embed)


# ----- balance / daily / work -----

@bot.hybrid_command(name="balance", description="Check how many coins you (or someone else) have")
@app_commands.describe(member="Whose balance to check (default: you)")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def balance_cmd(ctx, member: typing.Optional[discord.Member] = None):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    target = member or ctx.author
    if target.bot:
        await ctx.send("Bots don't have wallets 🤖")
        return

    u = get_user(ctx.guild.id, target.id)
    position = rank_position(ctx.guild.id, target.id, "coins")

    embed = discord.Embed(title=f"{COIN} {target.display_name}'s Balance", color=get_guild_color(ctx.guild.id))
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Coins", value=f"**{u['coins']:,}** {COIN}", inline=True)
    embed.add_field(name="Wealth rank", value=f"#{position}" if position else "Unranked", inline=True)

    if active_multiplier(u) > 1:
        embed.add_field(name="Active boost", value=f"{u.get('boost_name', 'Boost')} — ends {fmt_ts(u['boost_until'])}", inline=True)

    if target.id == ctx.author.id:
        now = time.time()
        if now - u["last_daily"] >= 86400:
            daily_text = "✅ Ready — use **/daily**!"
        else:
            daily_text = f"Ready {fmt_ts(u['last_daily'] + 86400)}"
        if u["streak"] > 1:
            daily_text += f"\n🔥 {u['streak']}-day streak"
        embed.add_field(name="Daily reward", value=daily_text, inline=False)

    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed)


@bot.hybrid_command(name="daily", description="Claim your free daily coins (once every 24 hours)")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def daily_cmd(ctx):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    u = get_user(ctx.guild.id, ctx.author.id)
    now = time.time()

    if now - u["last_daily"] < 86400:
        ready_at = u["last_daily"] + 86400
        embed = discord.Embed(
            description=f"⏳ You already claimed your daily reward! Come back {fmt_ts(ready_at)}.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed, ephemeral=True)
        return

    if u["last_daily"] and now - u["last_daily"] < 172800:
        streak = u["streak"] + 1
    else:
        streak = 1

    base_reward = DAILY_BASE + DAILY_STREAK_BONUS * min(streak - 1, 10)
    mult = active_multiplier(u)
    reward = base_reward * mult
    u["coins"] += reward
    u["last_daily"] = now
    u["streak"] = streak
    mark_dirty(ctx.guild.id, ctx.author.id)

    boost_note = f" (**{mult}x** boost applied!)" if mult > 1 else ""
    embed = discord.Embed(
        title="🎁 Daily Reward",
        description=f"You claimed **{reward}** {COIN}{boost_note}!\n\n**Balance:** {u['coins']:,} {COIN}",
        color=discord.Color.green()
    )
    embed.add_field(name="Streak", value=f"🔥 {streak} day{'s' if streak != 1 else ''}", inline=True)
    embed.add_field(name="Next reward", value=fmt_ts(now + 86400), inline=True)
    embed.set_footer(text="Claim every day to build your streak • ChillBot 😎")
    await ctx.send(embed=embed)


WORK_JOBS = [
    ("You delivered pizzas around town", 40, 90),
    ("You fixed a neighbour's Wi-Fi", 50, 110),
    ("You walked a pack of very excited dogs", 30, 80),
    ("You streamed to 3 viewers (one was your mum)", 20, 70),
    ("You repaired a broken bicycle", 45, 100),
    ("You sold homemade lemonade", 25, 75),
    ("You mowed lawns all afternoon", 50, 110),
    ("You debugged a bot at 3 AM", 60, 130),
    ("You helped move a sofa up four flights of stairs", 55, 120),
    ("You baked cookies for the school bake sale", 35, 85),
]


@bot.hybrid_command(name="work", description="Do a mini-job to earn some coins (once per hour)")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def work_cmd(ctx):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    u = get_user(ctx.guild.id, ctx.author.id)
    now = time.time()

    if now - u["last_work"] < WORK_COOLDOWN:
        ready_at = u["last_work"] + WORK_COOLDOWN
        embed = discord.Embed(
            description=f"😮‍💨 You're tired! You can work again {fmt_ts(ready_at)}.",
            color=discord.Color.orange()
        )
        await ctx.send(embed=embed, ephemeral=True)
        return

    text, low, high = random.choice(WORK_JOBS)
    mult = active_multiplier(u)
    earned = random.randint(low, high) * mult
    u["coins"] += earned
    u["last_work"] = now
    mark_dirty(ctx.guild.id, ctx.author.id)

    boost_note = f" (**{mult}x** boost applied!)" if mult > 1 else ""
    embed = discord.Embed(
        title="💼 Work",
        description=f"{text} and earned **{earned}** {COIN}{boost_note}!\n\n**Balance:** {u['coins']:,} {COIN}",
        color=discord.Color.green()
    )
    embed.set_footer(text="You can work again in 1 hour • ChillBot 😎")
    await ctx.send(embed=embed)


# ----- shop -----

class ShopView(discord.ui.View):
    def __init__(self, guild_id, user_id):
        super().__init__(timeout=60)
        self.guild_id = guild_id
        self.user_id = user_id
        for item in SHOP_ITEMS:
            self.add_item(ShopBuyButton(item))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This isn't your shop menu — run `/shop` yourself!", ephemeral=True)
            return False
        return True


class ShopBuyButton(discord.ui.Button):
    def __init__(self, item):
        super().__init__(label=f"Buy — {item['price']:,} 🪙", style=discord.ButtonStyle.success)
        self.item = item

    async def callback(self, interaction: discord.Interaction):
        view: ShopView = self.view
        u = get_user(view.guild_id, view.user_id)

        if u["coins"] < self.item["price"]:
            await interaction.response.send_message(
                f"You need **{self.item['price']:,}** {COIN} for that, but you only have **{u['coins']:,}** {COIN}.",
                ephemeral=True
            )
            return

        u["coins"] -= self.item["price"]
        u["boost_multiplier"] = self.item["multiplier"]
        u["boost_until"] = time.time() + self.item["hours"] * 3600
        u["boost_name"] = self.item["name"]
        mark_dirty(view.guild_id, view.user_id)

        embed = discord.Embed(
            title="✅ Purchase complete!",
            description=f"You bought **{self.item['name']}**.\n\nActive until {fmt_ts(u['boost_until'])}.\n\n**Balance:** {u['coins']:,} {COIN}",
            color=discord.Color.green()
        )
        embed.set_footer(text="ChillBot 😎")
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.hybrid_command(name="shop", description="Spend your coins on boosts and power-ups")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def shop_cmd(ctx):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    u = get_user(ctx.guild.id, ctx.author.id)
    embed = discord.Embed(
        title="🛒 ChillBot Shop",
        description=f"You have **{u['coins']:,}** {COIN}\n\nPick a boost to buy below:",
        color=get_guild_color(ctx.guild.id)
    )
    for item in SHOP_ITEMS:
        embed.add_field(name=f"{item['name']} — {item['price']:,} {COIN}", value=item["desc"], inline=False)
    if active_multiplier(u) > 1:
        embed.add_field(name="Currently active", value=f"{u.get('boost_name')} — ends {fmt_ts(u['boost_until'])}", inline=False)
    embed.set_footer(text="Buying a new boost replaces any boost currently active • ChillBot 😎")

    view = ShopView(ctx.guild.id, ctx.author.id)
    await ctx.send(embed=embed, view=view)


@bot.hybrid_command(name="give-coins", description="[Owner only] Give yourself coins")
@app_commands.default_permissions(administrator=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(amount="How many coins to add to your own balance")
async def give_coins_cmd(ctx, amount: int):
    if ctx.author.id != OWNER_ID:
        await ctx.send("This command is not available to you.", ephemeral=True)
        return
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return
    if amount < 1 or amount > 1_000_000:
        await ctx.send("Amount must be between 1 and 1,000,000.", ephemeral=True)
        return

    u = get_user(ctx.guild.id, ctx.author.id)
    u["coins"] += amount
    mark_dirty(ctx.guild.id, ctx.author.id)

    embed = discord.Embed(
        description=f"👑 Added **{amount:,}** {COIN} to your balance.\n\n**New balance:** {u['coins']:,} {COIN}",
        color=discord.Color.gold()
    )
    await ctx.send(embed=embed, ephemeral=True)


# ----- trivia -----

TRIVIA_FALLBACK = [
    ("What is the capital of Australia?", "Canberra", ["Sydney", "Melbourne", "Perth"]),
    ("Which planet is known as the Red Planet?", "Mars", ["Venus", "Jupiter", "Mercury"]),
    ("How many continents are there on Earth?", "7", ["5", "6", "8"]),
    ("What is the largest ocean on Earth?", "Pacific Ocean", ["Atlantic Ocean", "Indian Ocean", "Arctic Ocean"]),
    ("Who painted the Mona Lisa?", "Leonardo da Vinci", ["Michelangelo", "Raphael", "Vincent van Gogh"]),
    ("What is the chemical symbol for gold?", "Au", ["Ag", "Gd", "Go"]),
    ("In which country is the Great Pyramid of Giza?", "Egypt", ["Mexico", "Iraq", "Morocco"]),
    ("How many sides does a hexagon have?", "6", ["5", "7", "8"]),
    ("What is the largest mammal in the world?", "Blue whale", ["African elephant", "Giraffe", "Polar bear"]),
    ("Which language has the most native speakers worldwide?", "Mandarin Chinese", ["English", "Spanish", "Hindi"]),
    ("What is the hardest natural substance on Earth?", "Diamond", ["Quartz", "Titanium", "Granite"]),
    ("Who wrote 'Romeo and Juliet'?", "William Shakespeare", ["Charles Dickens", "Jane Austen", "Mark Twain"]),
    ("Which gas do plants absorb from the air for photosynthesis?", "Carbon dioxide", ["Oxygen", "Nitrogen", "Hydrogen"]),
    ("What is the smallest prime number?", "2", ["0", "1", "3"]),
    ("In which year did World War II end?", "1945", ["1939", "1943", "1950"]),
    ("What is the currency of Japan?", "Yen", ["Won", "Yuan", "Ringgit"]),
    ("Which organ pumps blood around the human body?", "Heart", ["Liver", "Lungs", "Kidneys"]),
    ("What is the tallest mountain above sea level?", "Mount Everest", ["K2", "Kangchenjunga", "Kilimanjaro"]),
    ("How many players does one football (soccer) team have on the pitch?", "11", ["9", "10", "12"]),
    ("What is H2O more commonly known as?", "Water", ["Hydrogen peroxide", "Salt", "Ammonia"]),
    ("Which country is home to the kangaroo?", "Australia", ["New Zealand", "South Africa", "Brazil"]),
    ("What is the largest planet in our solar system?", "Jupiter", ["Saturn", "Neptune", "Earth"]),
    ("Which festival is known as the Festival of Lights in India?", "Diwali", ["Holi", "Eid", "Pongal"]),
    ("At sea level, water boils at what temperature?", "100°C", ["90°C", "110°C", "120°C"]),
]


async def fetch_trivia_questions(amount):
    """Gets questions from Open Trivia DB; falls back to a built-in list if that's unavailable."""
    try:
        timeout = aiohttp.ClientTimeout(total=8)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get("https://opentdb.com/api.php", params={"amount": amount, "type": "multiple"}) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if data.get("response_code") == 0 and data.get("results"):
                        questions = []
                        for item in data["results"]:
                            questions.append({
                                "question": html.unescape(item["question"]),
                                "correct": html.unescape(item["correct_answer"]),
                                "wrong": [html.unescape(x) for x in item["incorrect_answers"]],
                                "category": html.unescape(item.get("category", "General Knowledge")),
                            })
                        return questions
    except Exception as e:
        print(f"Trivia API error (using built-in questions): {e}", flush=True)

    picks = random.sample(TRIVIA_FALLBACK, min(amount, len(TRIVIA_FALLBACK)))
    return [
        {"question": q, "correct": correct, "wrong": list(wrong), "category": "General Knowledge"}
        for q, correct, wrong in picks
    ]


class TriviaButton(discord.ui.Button):
    def __init__(self, index):
        super().__init__(label="ABCD"[index], style=discord.ButtonStyle.blurple)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view = self.view
        if view.closed:
            await interaction.response.send_message("⏰ Time's up for this question!", ephemeral=True)
            return
        if interaction.user.id in view.answers:
            await interaction.response.send_message("You already locked in an answer for this question!", ephemeral=True)
            return
        view.answers[interaction.user.id] = (self.index, time.time())
        await interaction.response.send_message(f"✅ You locked in **{self.label}**! Wait for the reveal...", ephemeral=True)


class TriviaView(discord.ui.View):
    def __init__(self, option_count):
        super().__init__(timeout=None)
        self.answers = {}
        self.closed = False
        for i in range(option_count):
            self.add_item(TriviaButton(i))


def scoreboard_text(scores, limit=5):
    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:limit]
    lines = []
    for i, (uid, pts) in enumerate(top):
        prefix = MEDALS[i] if i < 3 else f"{i + 1}."
        lines.append(f"{prefix} <@{uid}> — {pts} pts")
    return "\n".join(lines)


@bot.hybrid_command(name="trivia", description="Start a multiple-choice quiz — earn points and coins!")
@app_commands.describe(rounds="How many questions to play (1-10, default 5)")
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
async def trivia_cmd(ctx, rounds: typing.Optional[int] = 5):
    if not ctx.guild:
        await ctx.send("Trivia only works in servers.")
        return

    if rounds is None:
        rounds = 5
    if rounds < 1 or rounds > 10:
        await ctx.send("Pick between 1 and 10 questions.")
        return

    channel = ctx.channel
    if channel.id in active_trivia:
        await ctx.send("A trivia game is already running in this channel — wait for it to finish!")
        return

    active_trivia.add(channel.id)
    scores = {}
    try:
        await ctx.defer()
        questions = await fetch_trivia_questions(rounds)
        total = len(questions)

        intro = discord.Embed(
            title="🧠 Trivia time!",
            description=(
                f"**{total}** question{'s' if total != 1 else ''}, **{TRIVIA_SECONDS} seconds** each.\n"
                f"Click a button to lock in your answer (you only get one try).\n\n"
                f"✅ Correct answer = **10 points**\n"
                f"⚡ Fastest correct answers get a bonus: +{TRIVIA_BONUS[0]}, +{TRIVIA_BONUS[1]}, +{TRIVIA_BONUS[2]}\n"
                f"{COIN} Every point is also added to your coins (boosts apply)!"
            ),
            color=DEFAULT_COLOR
        )
        intro.set_footer(text=f"Started by {ctx.author.display_name} • ChillBot 😎")
        await ctx.send(embed=intro)
        await asyncio.sleep(4)

        for n, q in enumerate(questions, start=1):
            options = q["wrong"] + [q["correct"]]
            random.shuffle(options)
            correct_index = options.index(q["correct"])
            letters = "ABCD"

            option_lines = "\n".join(f"**{letters[i]})** {opt}" for i, opt in enumerate(options))
            question_embed = discord.Embed(
                title=f"🧠 Trivia — Question {n}/{total}",
                description=f"**{q['question']}**\n\n{option_lines}",
                color=DEFAULT_COLOR
            )
            question_embed.add_field(name="Category", value=q["category"], inline=True)
            question_embed.add_field(name="Time's up", value=fmt_ts(time.time() + TRIVIA_SECONDS), inline=True)
            question_embed.set_footer(text="Click a button to answer • ChillBot 😎")

            view = TriviaView(len(options))
            msg = await channel.send(embed=question_embed, view=view)
            await asyncio.sleep(TRIVIA_SECONDS)

            view.closed = True
            for child in view.children:
                child.disabled = True

            correct_users = sorted(
                (clicked_at, uid) for uid, (idx, clicked_at) in view.answers.items() if idx == correct_index
            )
            winner_lines = []
            for place, (_, uid) in enumerate(correct_users):
                pts = 10 + (TRIVIA_BONUS[place] if place < len(TRIVIA_BONUS) else 0)
                scores[uid] = scores.get(uid, 0) + pts

                player = get_user(ctx.guild.id, uid)
                mult = active_multiplier(player)
                coins_earned = pts * mult
                player["trivia_points"] += pts
                player["coins"] += coins_earned
                mark_dirty(ctx.guild.id, uid)

                if place < 8:
                    boost_tag = f" ({mult}x)" if mult > 1 else ""
                    winner_lines.append(f"<@{uid}> +{pts} pts / +{coins_earned}{COIN}{boost_tag}")

            reveal_options = "\n".join(
                f"{'✅ ' if i == correct_index else ''}**{letters[i]})** {opt}" for i, opt in enumerate(options)
            )
            reveal = discord.Embed(
                title=f"🧠 Trivia — Question {n}/{total}",
                description=f"**{q['question']}**\n\n{reveal_options}",
                color=discord.Color.green() if correct_users else discord.Color.red()
            )
            reveal.add_field(
                name="Got it right",
                value="\n".join(winner_lines) if winner_lines else "Nobody 😅",
                inline=True
            )
            if scores:
                reveal.add_field(name="Scoreboard", value=scoreboard_text(scores), inline=True)
            reveal.set_footer(text=f"{len(view.answers)} player(s) answered • ChillBot 😎")
            try:
                await msg.edit(embed=reveal, view=view)
            except Exception as e:
                print(f"Trivia reveal edit failed: {e}", flush=True)

            if n < total:
                await asyncio.sleep(4)

        if scores:
            description = "**Final standings:**\n" + scoreboard_text(scores, limit=10)
        else:
            description = "Nobody scored this time — better luck next game! 🍀"
        final = discord.Embed(title="🏁 Trivia finished!", description=description, color=discord.Color.gold())
        final.set_footer(text="Points were added to your coins and /leaderboard trivia • ChillBot 😎")
        await channel.send(embed=final)

    except Exception as e:
        print(f"Trivia error: {e}", flush=True)
        try:
            await channel.send("Something went wrong with the trivia game, sorry! Try again in a moment.")
        except Exception:
            pass
    finally:
        active_trivia.discard(channel.id)


# ----- jail -----

def can_jail(member: discord.Member):
    return is_admin_or_owner(member) or member.guild_permissions.moderate_members


def schedule_jail_release(guild_id, user_id, until_ts):
    key = (guild_id, user_id)
    old = jail_tasks.pop(key, None)
    if old:
        old.cancel()

    async def _runner():
        delay = until_ts - time.time()
        if delay > 0:
            await asyncio.sleep(delay)
        await release_jail(guild_id, user_id)

    jail_tasks[key] = asyncio.create_task(_runner())


async def release_jail(guild_id, user_id, released_by=None):
    key = (guild_id, user_id)
    task = jail_tasks.pop(key, None)
    if task and task is not asyncio.current_task():
        task.cancel()

    u = get_user(guild_id, user_id)
    role_id = u.get("jail_role_id") or jail_roles.get(guild_id)
    u["jailed_until"] = None
    u["jail_role_id"] = None
    mark_dirty(guild_id, user_id)

    guild = bot.get_guild(guild_id)
    if guild is None:
        return

    member = guild.get_member(user_id)
    role = guild.get_role(role_id) if role_id else None
    if member and role and role in member.roles:
        try:
            await member.remove_roles(role, reason="Jail time is over" if not released_by else f"Released by {released_by}")
        except Exception as e:
            print(f"Failed to remove jailed role: {e}", flush=True)

    if released_by:
        text = f"<@{user_id}> was let out of jail early by {released_by.mention}."
    else:
        text = f"<@{user_id}> served their time and was released from jail."
    log_embed = discord.Embed(title="🔓 Released from jail", description=text, color=discord.Color.green())
    await send_log(guild, log_embed)


async def economy_on_member_join(member):
    """If someone leaves the server to dodge jail, they get the jailed role back when they rejoin."""
    try:
        u = economy.get(member.guild.id, {}).get(member.id)
        if not u or not u.get("jailed_until"):
            return
        if u["jailed_until"] <= time.time():
            await release_jail(member.guild.id, member.id)
            return
        role_id = u.get("jail_role_id") or jail_roles.get(member.guild.id)
        role = member.guild.get_role(role_id) if role_id else None
        if role:
            await member.add_roles(role, reason="Still serving jail time")
    except Exception as e:
        print(f"economy_on_member_join error: {e}", flush=True)


@bot.hybrid_command(name="jail", description="Send a member to jail (gives them the jailed role for a set time)")
@app_commands.default_permissions(moderate_members=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(
    member="Who to jail",
    duration="How long, e.g. 10m, 2h, 1d, 1h30m",
    reason="Optional reason"
)
async def jail_cmd(ctx, member: discord.Member, duration: str, reason: typing.Optional[str] = None):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    if not can_jail(ctx.author):
        await ctx.send("You need the **Moderate Members** permission (or be an admin) to jail people.", ephemeral=True)
        return

    role_id = jail_roles.get(ctx.guild.id)
    role = ctx.guild.get_role(role_id) if role_id else None
    if role is None:
        await ctx.send(
            "There's no jailed role set up yet. An admin can pick one with **/setup** → *Set the jailed role*.",
            ephemeral=True
        )
        return

    if member.bot:
        await ctx.send("You can't jail a bot 🤖", ephemeral=True)
        return
    if member.id == ctx.author.id:
        await ctx.send("You can't jail yourself!", ephemeral=True)
        return
    if member.id == ctx.guild.owner_id or member.guild_permissions.administrator:
        await ctx.send("You can't jail the server owner or an administrator.", ephemeral=True)
        return
    if ctx.author.id != ctx.guild.owner_id and member.top_role >= ctx.author.top_role:
        await ctx.send("You can only jail members whose highest role is below yours.", ephemeral=True)
        return

    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("Invalid duration. Use formats like `10m`, `2h`, `1d`, or `1h30m`.", ephemeral=True)
        return
    if seconds < 10:
        await ctx.send("Jail time must be at least 10 seconds.", ephemeral=True)
        return
    if seconds > JAIL_MAX_SECONDS:
        await ctx.send("Jail time can't be longer than 30 days.", ephemeral=True)
        return

    if reason and len(reason) > 200:
        await ctx.send("Reason is too long (max 200 characters).", ephemeral=True)
        return

    if role.managed or role >= ctx.guild.me.top_role:
        await ctx.send(
            "I can't hand out the jailed role — in Server Settings → Roles, drag my role above it "
            "and make sure I have **Manage Roles**.",
            ephemeral=True
        )
        return

    try:
        await member.add_roles(role, reason=f"Jailed by {ctx.author} ({ctx.author.id}): {reason or 'no reason given'}")
    except discord.Forbidden:
        await ctx.send("I don't have permission to give that role. Check that I have **Manage Roles**.", ephemeral=True)
        return
    except discord.HTTPException as e:
        print(f"Jail add_roles error: {e}", flush=True)
        await ctx.send("Something went wrong giving the jailed role, try again.", ephemeral=True)
        return

    until_ts = time.time() + seconds
    u = get_user(ctx.guild.id, member.id)
    u["jailed_until"] = until_ts
    u["jail_role_id"] = role.id
    mark_dirty(ctx.guild.id, member.id)
    schedule_jail_release(ctx.guild.id, member.id, until_ts)

    embed = discord.Embed(
        title="🚔 Sent to jail",
        description=f"{member.mention} has been jailed by {ctx.author.mention}.",
        color=discord.Color.red()
    )
    embed.add_field(name="Released", value=fmt_ts(until_ts), inline=True)
    embed.add_field(name="Reason", value=reason or "No reason given", inline=True)
    embed.set_thumbnail(url=member.display_avatar.url)
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed)

    log_embed = discord.Embed(
        title="🚔 Member Jailed",
        description=f"{member.mention} was jailed by {ctx.author.mention} until {fmt_ts(until_ts, 'f')}",
        color=discord.Color.red()
    )
    log_embed.add_field(name="Reason", value=reason or "No reason given", inline=False)
    await send_log(ctx.guild, log_embed)


@bot.hybrid_command(name="unjail", description="Let a member out of jail early")
@app_commands.default_permissions(moderate_members=True)
@app_commands.allowed_installs(guilds=True, users=False)
@app_commands.allowed_contexts(guilds=True, dms=False, private_channels=False)
@app_commands.describe(member="Who to release")
async def unjail_cmd(ctx, member: discord.Member):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    if not can_jail(ctx.author):
        await ctx.send("You need the **Moderate Members** permission (or be an admin) to release people.", ephemeral=True)
        return

    u = economy.get(ctx.guild.id, {}).get(member.id)
    if not u or not u.get("jailed_until"):
        await ctx.send(f"{member.mention} isn't in jail.", ephemeral=True)
        return

    await release_jail(ctx.guild.id, member.id, released_by=ctx.author)
    embed = discord.Embed(
        title="🔓 Released",
        description=f"{member.mention} was let out of jail by {ctx.author.mention}.",
        color=discord.Color.green()
    )
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
    embed = discord.Embed(
        title="🪙 Coin Flip",
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


# ---------- HIGHER OR LOWER (reposts a fresh embed each turn) ----------

REPOST_THRESHOLD = 10  # only post a fresh game embed if this many messages have appeared since the last one


class HigherLowerView(discord.ui.View):
    def __init__(self, player_id, deck, current_card, channel, last_count=None):
        super().__init__(timeout=60)
        self.player_id = player_id
        self.deck = deck
        self.current_card = current_card
        self.score = 0
        self.channel = channel
        self.last_count = last_count if last_count is not None else channel_msg_count.get(channel.id, 0)

    @staticmethod
    def card_label(card):
        rank, suit = card
        return f"{rank}{suit}"

    def build_embed(self, desc, color):
        embed = discord.Embed(title="🎴 Higher or Lower", description=desc, color=color)
        embed.set_footer(text="ChillBot 😎")
        return embed

    async def guess(self, interaction: discord.Interaction, direction: str):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return

        if not self.deck:
            await interaction.response.defer()
            return

        next_card = self.deck.pop()
        current_value = self.current_card[2]
        next_value = next_card[2]

        correct = (direction == "higher" and next_value > current_value) or \
                  (direction == "lower" and next_value < current_value) or \
                  (next_value == current_value)

        current_count = channel_msg_count.get(self.channel.id, 0)
        should_repost = (current_count - self.last_count) >= REPOST_THRESHOLD

        new_view = HigherLowerView(
            self.player_id, self.deck, next_card, self.channel,
            last_count=current_count if should_repost else self.last_count
        )
        new_view.score = self.score

        if correct:
            new_view.score += 1
            desc = f"**{self.card_label(next_card)}** — Correct! 🎉\nScore: **{new_view.score}**\n\nNext card, higher or lower than **{self.card_label(next_card)}**?"
            color = discord.Color.green()
        else:
            desc = f"**{self.card_label(next_card)}** — Wrong! 💀\nFinal score: **{new_view.score}**"
            color = discord.Color.red()
            for child in new_view.children:
                child.disabled = True

        if not new_view.deck:
            for child in new_view.children:
                child.disabled = True
            desc += "\n\n🎉 You cleared the whole deck!"

        embed = new_view.build_embed(desc, color)

        if should_repost:
            for child in self.children:
                child.disabled = True
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
            await interaction.response.send_message(embed=embed, view=new_view)
        else:
            await interaction.response.edit_message(embed=embed, view=new_view)

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
    view = HigherLowerView(ctx.author.id, deck, current_card, ctx.channel)

    embed = discord.Embed(
        title="🎴 Higher or Lower",
        description=f"Current card: **{current_card[0]}{current_card[1]}**\n\nWill the next card be higher or lower?",
        color=DEFAULT_COLOR
    )
    embed.set_footer(text="ChillBot 😎")
    await ctx.send(embed=embed, view=view)


# ---------- MINESWEEPER (interactive — click a bomb and it's game over) ----------

class MinesweeperButton(discord.ui.Button):
    def __init__(self, index):
        super().__init__(label="\u200b", style=discord.ButtonStyle.secondary, row=index // 5)
        self.index = index

    async def callback(self, interaction: discord.Interaction):
        view: MinesweeperView = self.view
        await view.reveal(interaction, self.index, self)


class MinesweeperView(discord.ui.View):
    def __init__(self, player_id, size, bombs):
        super().__init__(timeout=180)
        self.player_id = player_id
        self.size = size
        self.total_cells = size * size
        self.bomb_positions = set(random.sample(range(self.total_cells), bombs))
        self.revealed = set()
        self.over = False
        self.digit_emojis = ["", "1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣"]

        for i in range(self.total_cells):
            self.add_item(MinesweeperButton(i))

    def neighbors(self, pos):
        row, col = divmod(pos, self.size)
        for dr in (-1, 0, 1):
            for dc in (-1, 0, 1):
                if dr == 0 and dc == 0:
                    continue
                r, c = row + dr, col + dc
                if 0 <= r < self.size and 0 <= c < self.size:
                    yield r * self.size + c

    def build_embed(self, status):
        safe_cells = self.total_cells - len(self.bomb_positions)
        embed = discord.Embed(
            title="💣 Minesweeper",
            description=f"Grid: {self.size}x{self.size} • Bombs: {len(self.bomb_positions)}\n\n{status}",
            color=DEFAULT_COLOR
        )
        embed.add_field(name="Cells revealed", value=f"{len(self.revealed)}/{safe_cells}", inline=True)
        embed.set_footer(text="ChillBot 😎")
        return embed

    async def reveal(self, interaction: discord.Interaction, index, button: MinesweeperButton):
        if interaction.user.id != self.player_id:
            await interaction.response.send_message("This isn't your game!", ephemeral=True)
            return
        if self.over or index in self.revealed:
            await interaction.response.defer()
            return

        if index in self.bomb_positions:
            self.over = True
            for child in self.children:
                if isinstance(child, MinesweeperButton):
                    if child.index in self.bomb_positions:
                        child.label = "💣"
                        child.style = discord.ButtonStyle.danger
                    child.disabled = True
            embed = self.build_embed("💥 **BOOM! Game over.**")
            await interaction.response.edit_message(embed=embed, view=self)
            return

        self.revealed.add(index)
        count = sum(1 for n in self.neighbors(index) if n in self.bomb_positions)
        button.label = self.digit_emojis[count] if count else "▫️"
        button.style = discord.ButtonStyle.success
        button.disabled = True

        safe_cells = self.total_cells - len(self.bomb_positions)
        if len(self.revealed) >= safe_cells:
            self.over = True
            for child in self.children:
                child.disabled = True
            embed = self.build_embed("🎉 **You cleared the board!**")
            await interaction.response.edit_message(embed=embed, view=self)
            return

        embed = self.build_embed("Keep going — click a cell!")
        await interaction.response.edit_message(embed=embed, view=self)


@bot.hybrid_command(name="minesweeper", description="Click cells to reveal them — hit a bomb and it's game over!")
@app_commands.describe(size="Grid size (3-5, default 5)", bombs="Number of bombs (default 5)")
@app_commands.allowed_installs(guilds=True, users=True)
@app_commands.allowed_contexts(guilds=True, dms=True, private_channels=True)
async def minesweeper(ctx, size: typing.Optional[int] = 5, bombs: typing.Optional[int] = 5):
    if size < 3 or size > 5:
        await ctx.send("Size must be between 3 and 5 (Discord only allows a 5x5 grid of buttons).")
        return
    total_cells = size * size
    if bombs < 1 or bombs >= total_cells:
        await ctx.send(f"Bombs must be between 1 and {total_cells - 1}.")
        return

    view = MinesweeperView(ctx.author.id, size, bombs)
    embed = view.build_embed("Click a cell to reveal it!")
    await ctx.send(embed=embed, view=view)


# ---------- CONNECT 4 (reposts a fresh embed each turn) ----------

class Connect4View(discord.ui.View):
    def __init__(self, player1_id, player2_id, channel, board=None, turn=None, last_count=None):
        super().__init__(timeout=300)
        self.player1_id = player1_id
        self.player2_id = player2_id
        self.channel = channel
        self.board = board if board is not None else [[0] * 7 for _ in range(6)]
        self.turn = turn if turn is not None else player1_id
        self.over = False
        self.last_count = last_count if last_count is not None else channel_msg_count.get(channel.id, 0)

        for col in range(7):
            self.add_item(Connect4Button(col))

    def render_board(self):
        symbols = {0: "⚪", 1: "🔴", 2: "🟡"}
        return "\n".join("".join(symbols[cell] for cell in row) for row in self.board)

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

        current_count = channel_msg_count.get(view.channel.id, 0)
        should_repost = (current_count - view.last_count) >= REPOST_THRESHOLD

        if view.check_winner(player_num):
            view.over = True
            for child in view.children:
                child.disabled = True
            embed = discord.Embed(
                title="🔴 🟡 Connect 4",
                description=f"{view.render_board()}\n\n🎉 <@{interaction.user.id}> wins!",
                color=discord.Color.green()
            )
            if should_repost:
                await interaction.response.edit_message(view=view)
                await interaction.channel.send(embed=embed)
            else:
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
            if should_repost:
                await interaction.response.edit_message(view=view)
                await interaction.channel.send(embed=embed)
            else:
                await interaction.response.edit_message(embed=embed, view=view)
            return

        next_turn = view.player2_id if interaction.user.id == view.player1_id else view.player1_id

        if should_repost:
            for child in view.children:
                child.disabled = True
            await interaction.response.edit_message(view=view)
            new_view = Connect4View(
                view.player1_id, view.player2_id, view.channel,
                board=view.board, turn=next_turn, last_count=current_count
            )
            embed = discord.Embed(
                title="🔴 🟡 Connect 4",
                description=f"{new_view.render_board()}\n\nIt's <@{next_turn}>'s turn.",
                color=DEFAULT_COLOR
            )
            await interaction.channel.send(embed=embed, view=new_view)
        else:
            view.turn = next_turn
            embed = discord.Embed(
                title="🔴 🟡 Connect 4",
                description=f"{view.render_board()}\n\nIt's <@{next_turn}>'s turn.",
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

    view = Connect4View(ctx.author.id, opponent.id, ctx.channel)
    embed = discord.Embed(
        title="🔴 🟡 Connect 4",
        description=f"{view.render_board()}\n\n{ctx.author.mention} (🔴) vs {opponent.mention} (🟡)\n\nIt's {ctx.author.mention}'s turn.",
        color=DEFAULT_COLOR
    )
    await ctx.send(embed=embed, view=view)


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


HINGLISH_WORDS = {
    "hai", "hain", "hoon", "hun", "kya", "kyu", "kyun", "kaise", "kaisa", "kaisi",
    "nahi", "nahin", "haan", "acha", "accha", "achha", "tum", "tumhe", "tumhara",
    "mujhe", "mera", "meri", "tera", "teri", "uska", "uski", "hum", "humko",
    "kar", "karo", "karta", "karti", "karte", "raha", "rahi", "rahe", "bhai",
    "yaar", "kahan", "kab", "thik", "theek", "abhi", "bahut", "bohot", "matlab",
    "dost", "pyar", "dil", "zindagi", "chahiye", "sahi", "galat", "kuch",
    "kisi", "koi", "sab", "bilkul", "shayad", "waise", "kyunki", "isliye",
    "toh", "bhi", "wala", "wali", "log", "aap", "aapka", "aapki", "mein",
}


def is_hinglish(text: str) -> bool:
    words = re.findall(r"[a-zA-Z]+", text.lower())
    if not words:
        return False
    matches = sum(1 for w in words if w in HINGLISH_WORDS)
    return matches >= 1 and (matches / len(words)) >= 0.2


async def transliterate_to_hindi(session, text: str) -> str:
    """Converts romanized Hindi (Hinglish) to Devanagari script, word by word, using Google's input-tools API."""
    words = text.split()
    result_words = []
    for word in words:
        stripped = re.sub(r"[^\w']", "", word)
        if not stripped:
            result_words.append(word)
            continue
        try:
            async with session.get(
                "https://inputtools.google.com/request",
                params={"text": stripped, "itc": "hi-t-i0-und", "num": "1", "cp": "0", "cs": "1", "ie": "utf-8", "oe": "utf-8"}
            ) as resp:
                data = await resp.json()
                if data[0] == "SUCCESS" and data[1] and data[1][0][1]:
                    hindi_word = data[1][0][1][0]
                    result_words.append(word.replace(stripped, hindi_word))
                    continue
        except Exception as e:
            print(f"Transliteration error for '{word}': {e}", flush=True)
        result_words.append(word)
    return " ".join(result_words)


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
        hinglish_detected = is_hinglish(text)
        display_original = text

        async with aiohttp.ClientSession() as session:
            if hinglish_detected:
                hindi_text = await transliterate_to_hindi(session, text)
                display_original = f"{text}\n*(romanized Hindi → {hindi_text})*"

                if target == "hi":
                    embed = discord.Embed(title="🌐 Translation", color=DEFAULT_COLOR)
                    embed.add_field(name="Original", value=text[:1000], inline=False)
                    embed.add_field(name="Translated (hi)", value=hindi_text[:1000], inline=False)
                    embed.set_footer(text="Detected romanized Hindi • ChillBot 😎")
                    await ctx.send(embed=embed)
                    return

                url = "https://api.mymemory.translated.net/get"
                params = {"q": hindi_text, "langpair": f"hi|{target}"}
            else:
                url = "https://api.mymemory.translated.net/get"
                params = {"q": text, "langpair": f"autodetect|{target}"}

            async with session.get(url, params=params) as resp:
                data = await resp.json()

        translated = data.get("responseData", {}).get("translatedText")
        if not translated:
            await ctx.send("Couldn't translate that — check the language name/code and try again.")
            return

        embed = discord.Embed(title="🌐 Translation", color=DEFAULT_COLOR)
        embed.add_field(name="Original", value=display_original[:1000], inline=False)
        embed.add_field(name=f"Translated ({target})", value=translated[:1000], inline=False)
        if hinglish_detected:
            embed.set_footer(text="Detected romanized Hindi • ChillBot 😎")
        else:
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

        channel_msg_count[message.channel.id] = channel_msg_count.get(message.channel.id, 0) + 1

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

                mult = get_multiplier(message.guild.id, message.author)
                for _ in range(mult):
                    gw["entries"].append(message.author.id)
                save_giveaway(gid)

        await bot.process_commands(message)
    except Exception as e:
        print(f"on_message error: {e}", flush=True)


bot.add_listener(economy_on_member_join, "on_member_join")

bot.run(os.environ["DISCORD_TOKEN"])
