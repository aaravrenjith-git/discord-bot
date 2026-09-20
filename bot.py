import os
import random
import discord
from discord import app_commands
from discord.ext import commands
import typing
import re
import datetime
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

mongo_client = MongoClient(os.environ["MONGODB_URI"])
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
    try:
        giveaways_col.replace_one({"_id": gid}, doc, upsert=True)
    except Exception as e:
        print(f"DB save_giveaway error: {e}", flush=True)


def delete_giveaway_doc(gid):
    try:
        giveaways_col.delete_one({"_id": gid})
    except Exception as e:
        print(f"DB delete_giveaway error: {e}", flush=True)


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
    try:
        settings_col.replace_one({"_id": guild_id}, doc, upsert=True)
    except Exception as e:
        print(f"DB save_settings error: {e}", flush=True)


async def load_all_data():
    """Loads settings and resumes active giveaways from MongoDB on startup."""
    try:
        for doc in settings_col.find({}):
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
        print("Loaded settings from database.", flush=True)
    except Exception as e:
        print(f"DB load settings error: {e}", flush=True)

    try:
        for doc in giveaways_col.find({"active": True}):
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

        print(f"Resumed {len(giveaways)} active giveaway(s) from database.", flush=True)
    except Exception as e:
        print(f"DB load giveaways error: {e}", flush=True)


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

            lines = []
            for uid in gw["joined_users"]:
                count = gw["entries"].count(uid)
                lines.append(f"<@{uid}> — **{count}** entr{'y' if count == 1 else 'ies'}")

            text = f"**Participants ({len(gw['joined_users'])}):**\n" + "\n".join(lines)
            if len(text) > 1900:
                text = text[:1900] + "\n...(list truncated)"
            await interaction.response.send_message(text, ephemeral=True)
        except Exception as e:
            print(f"Participants button error: {e}", flush=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}", flush=True)
    await bot.change_presence(activity=discord.Activity(type=discord.ActivityType.watching, name="for /start-giveaway 🎉"))
    await load_all_data()
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)", flush=True)
    except Exception as e:
        print(f"Slash command sync failed: {e}", flush=True)


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
        value="**/setup** — Configure host roles, blacklist, channels, ping role, embed color, and log channel",
        inline=False
    )
    embed.set_footer(text=f"Owner: {OWNER_NAME} • ChillBot 😎")
    await ctx.send(embed=embed)


# ---------- SETUP COMMAND ----------

@bot.tree.command(name="setup", description="[Admin] Configure giveaway roles, channels, ping role, embed color, and logs")
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
    if not is_admin_or_owner(interaction.user):
        await interaction.response.send_message("This command is for Administrators only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    act = action.value

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
