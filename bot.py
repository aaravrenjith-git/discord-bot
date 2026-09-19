import os
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands
import typing
import re
import datetime
from flask import Flask
from threading import Thread

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

# ---------- EMOJIS ----------
# Want animated emojis? In Discord, type \:youremoji: and send it. It turns into a code like
#   <a:party:123456789012345678>     (the "a:" means animated)
# Paste that code between the quotes below in place of the normal emoji.
# The emoji must come from a server the bot is in, or be uploaded to your app's Emojis page
# in the Developer Portal. Leave a line as-is to keep the normal emoji.
# (Emojis show up in titles, descriptions, field text and buttons, but not in embed footers.)
EMOJI = {
    "party": "🎉",     # giveaway titles + Join button
    "host": "🎤",      # "Hosted by"
    "clock": "⏰",     # "Ends"
    "trophy": "🏆",    # winners
    "people": "👥",    # Participants button
}

# ---------- DATA STORES (in memory) ----------

giveaways = {}
authorized_roles = {}
blacklisted_roles = {}
entry_channels = {}
multiplier_roles = {}
win_counts = {}
log_channels = {}       # guild_id -> channel_id
default_ping_roles = {}  # guild_id -> role_id
embed_colors = {}        # guild_id -> hex string


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


def join_limited(items, sep=", ", limit=1000):
    """Joins text pieces but stops before hitting Discord's embed field size limit."""
    out = ""
    for i, item in enumerate(items):
        piece = item if not out else sep + item
        if len(out) + len(piece) > limit:
            out += f"{sep}…and {len(items) - i} more"
            break
        out += piece
    return out or "None"


async def refresh_giveaway_posts(guild_id):
    """Re-draws every running giveaway post in a server (used when /setup changes blacklist or bonus roles)."""
    for gid, gw in list(giveaways.items()):
        if gw["guild_id"] != guild_id or not gw["active"]:
            continue
        try:
            channel = bot.get_channel(gw["channel_id"])
            post = await channel.fetch_message(gid)
            await post.edit(embed=build_giveaway_embed(gw, gid))
        except Exception as e:
            print(f"Failed to refresh giveaway post {gid}: {e}", flush=True)


def build_giveaway_embed(gw, giveaway_id=None):
    """Builds the giveaway post from the stored giveaway data (used when starting AND editing)."""
    embed = discord.Embed(
        title=f"{EMOJI['party']}  G I V E A W A Y  {EMOJI['party']}",
        description=f"**Prize -** {gw['prize']}\n\nClick **Join Giveaway** below to enter — you get **1 entry** instantly! Then chat in the allowed channel(s): every message earns you another entry.\n*Click Join again anytime to leave (your entries are removed).*\n\n⚠️ **Spamming can get you blacklisted from giveaways.**",
        color=get_guild_color(gw["guild_id"])
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name=f"{EMOJI['host']} Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    embed.add_field(name=f"{EMOJI['clock']} Ends", value=discord.utils.format_dt(gw["end_time"], style="R"), inline=True)
    embed.add_field(name=f"{EMOJI['trophy']} Winners", value=str(gw["winners"]), inline=True)
    if gw["required_role_id"]:
        embed.add_field(name="🔑 Required role", value=f"<@&{gw['required_role_id']}>", inline=True)
    if gw.get("min_messages"):
        embed.add_field(
            name="📝 Messages required",
            value=f"**{gw['min_messages']}** message(s) in the allowed channel(s) to be eligible to win",
            inline=True
        )
    blocked_ids = list(blacklisted_roles.get(gw["guild_id"], set()))
    if gw["blacklist_role_id"] and gw["blacklist_role_id"] not in blocked_ids:
        blocked_ids.append(gw["blacklist_role_id"])
    if blocked_ids:
        embed.add_field(
            name="🚫 Blacklisted roles (can't join)",
            value=join_limited([f"<@&{rid}>" for rid in blocked_ids]),
            inline=False
        )
    if gw["bypass_role_id"]:
        embed.add_field(name="⚡ Bypass role", value=f"<@&{gw['bypass_role_id']}>", inline=True)

    guild_mults = multiplier_roles.get(gw["guild_id"], {})
    if guild_mults:
        mults_text = join_limited([f"<@&{rid}> — **{mult}x** entries" for rid, mult in guild_mults.items()], sep="\n")
        embed.add_field(name="⭐ Bonus entry roles (extra entries)", value=mults_text, inline=False)

    if gw.get("image_url"):
        embed.set_image(url=gw["image_url"])

    if giveaway_id:
        footer = f"Giveaway ID: {giveaway_id} • ChillBot 😎"
    else:
        footer = "Starting... • ChillBot 😎"
    embed.set_footer(text=footer, icon_url=bot.user.display_avatar.url)
    return embed


class GiveawayView(discord.ui.View):
    def __init__(self, giveaway_id):
        super().__init__(timeout=None)
        self.giveaway_id = giveaway_id

    @discord.ui.button(label="Join Giveaway", emoji=EMOJI["party"], style=discord.ButtonStyle.green, custom_id="join_btn")
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
                gw["entries"] = [uid for uid in gw["entries"] if uid != user.id]
                gw["msg_counts"].pop(user.id, None)
                await interaction.response.send_message(
                    "You left the giveaway and your entries were removed. 👋 Click again to rejoin — you'll start over with your starting entry.",
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
            starting_entries = get_multiplier(interaction.guild.id, user)
            gw["entries"].extend([user.id] * starting_entries)
            entry_word = "entry" if starting_entries == 1 else "entries"
            join_text = (
                f"✅ You joined and got **{starting_entries}** {entry_word} right away! "
                "Send messages in the allowed channel(s) to earn more. Click again to leave."
            )
            if gw.get("min_messages"):
                join_text += f"\n📝 You need to send **{gw['min_messages']}** message(s) in the allowed channel(s) to be eligible to win."
            await interaction.response.send_message(join_text, ephemeral=True)
        except Exception as e:
            print(f"Join button error: {e}", flush=True)
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)

    @discord.ui.button(label="Participants", emoji=EMOJI["people"], style=discord.ButtonStyle.blurple, custom_id="participants_btn")
    async def participants_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        try:
            gw = giveaways.get(self.giveaway_id)
            if not gw or not gw["joined_users"]:
                await interaction.response.send_message("No one has joined yet.", ephemeral=True)
                return

            lines = []
            for uid in gw["joined_users"]:
                count = gw["entries"].count(uid)
                line = f"<@{uid}> — **{count}** entr{'y' if count == 1 else 'ies'}"
                need = gw.get("min_messages", 0)
                if need:
                    sent = gw["msg_counts"].get(uid, 0)
                    line += f" • {min(sent, need)}/{need} messages {'✅' if sent >= need else '⏳'}"
                lines.append(line)

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
            "**/edit-giveaway** — Fix a mistake in a running giveaway\n"
            "**/reroll** — Pick new winner(s) for a finished giveaway\n"
            "**/cancel-giveaway** — Cancel a running giveaway without a winner\n"
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
            "**/embed** — Send a custom embed with your own title and text"
        ),
        inline=False
    )
    embed.set_footer(text=f"Owner: {OWNER_NAME} • ChillBot 😎")
    await ctx.send(embed=embed)


# ---------- SETUP COMMAND ----------

@bot.tree.command(name="setup", description="[Admin] Configure giveaway roles, channels, ping role, embed color, and logs")
@app_commands.default_permissions(administrator=True)
@app_commands.guild_only()
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
    app_commands.Choice(name="📣 Set the role to ping on new giveaways", value="set_ping_role"),
    app_commands.Choice(name="🔇 Remove the giveaway ping role", value="remove_ping_role"),
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
        await interaction.response.send_message(f"✅ {role.mention} can now host giveaways.", ephemeral=True)

    elif act == "remove_role":
        authorized_roles.setdefault(guild_id, set()).discard(role.id)
        await interaction.response.send_message(f"❌ {role.mention} can no longer host giveaways.", ephemeral=True)

    elif act == "blacklist_role":
        blacklisted_roles.setdefault(guild_id, set()).add(role.id)
        await interaction.response.send_message(f"🚫 {role.mention} can no longer join giveaways.", ephemeral=True)

    elif act == "unblacklist_role":
        blacklisted_roles.setdefault(guild_id, set()).discard(role.id)
        await interaction.response.send_message(f"✅ {role.mention} can join giveaways again.", ephemeral=True)

    elif act == "add_channel":
        entry_channels.setdefault(guild_id, set()).add(channel.id)
        await interaction.response.send_message(f"✅ Messages in {channel.mention} now count as entries.", ephemeral=True)

    elif act == "remove_channel":
        entry_channels.setdefault(guild_id, set()).discard(channel.id)
        await interaction.response.send_message(f"❌ Messages in {channel.mention} no longer count.", ephemeral=True)

    elif act == "add_multiplier":
        multiplier_roles.setdefault(guild_id, {})[role.id] = multiplier
        await interaction.response.send_message(f"✅ {role.mention} now gets **{multiplier}x** entries.", ephemeral=True)

    elif act == "remove_multiplier":
        multiplier_roles.setdefault(guild_id, {}).pop(role.id, None)
        await interaction.response.send_message(f"❌ {role.mention} no longer has a multiplier.", ephemeral=True)

    elif act == "set_ping_role":
        default_ping_roles[guild_id] = role.id
        reply = f"✅ {role.mention} will now be pinged automatically on new giveaways."
        if not role.mentionable and not interaction.guild.me.guild_permissions.mention_everyone:
            reply += (
                "\n⚠️ Heads up: this role isn't mentionable and I don't have the "
                "**Mention @everyone, @here, and All Roles** permission, so the ping won't actually notify anyone. "
                "Either turn on *Allow anyone to @mention this role* in the role's settings, or give me that permission."
            )
        await interaction.response.send_message(reply, ephemeral=True)

    elif act == "remove_ping_role":
        default_ping_roles.pop(guild_id, None)
        await interaction.response.send_message("❌ Giveaway ping role removed. New giveaways won't ping anyone.", ephemeral=True)

    elif act == "set_embed_color":
        hex_clean = color if color.startswith("#") else f"#{color}"
        embed_colors[guild_id] = hex_clean
        preview = discord.Embed(description="This is your new embed color! 🎨", color=discord.Color.from_str(hex_clean))
        await interaction.response.send_message(embed=preview, ephemeral=True)

    elif act == "reset_embed_color":
        embed_colors.pop(guild_id, None)
        await interaction.response.send_message("✅ Embed color reset to default.", ephemeral=True)

    elif act == "set_log_channel":
        log_channels[guild_id] = channel.id
        await interaction.response.send_message(f"✅ Giveaway logs will now be sent to {channel.mention}.", ephemeral=True)

    elif act == "remove_log_channel":
        log_channels.pop(guild_id, None)
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
        embed.add_field(name="Giveaway ping role", value=ping_txt, inline=True)
        embed.add_field(name="Embed color", value=color_hex, inline=True)
        embed.add_field(name="Log channel", value=log_txt, inline=True)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    if act in ("blacklist_role", "unblacklist_role", "add_multiplier", "remove_multiplier"):
        await refresh_giveaway_posts(guild_id)


# ---------- CUSTOM EMBED COMMAND ----------

@bot.hybrid_command(name="embed", description="Send a custom embed message (Admins only)")
@app_commands.describe(
    title="The embed's title",
    content="The embed's text. Use \\n for a new line. Paste <a:name:id> for animated emojis",
    color="Optional hex color, e.g. #FF5733 (defaults to the server's embed color)",
    image_url="Optional direct image link (.png, .jpg, .gif, .webp)",
    channel="Optional channel to send it in (defaults to this channel)"
)
async def embed_cmd(
    ctx,
    title: str,
    content: str,
    color: typing.Optional[str] = None,
    image_url: typing.Optional[str] = None,
    channel: typing.Optional[discord.TextChannel] = None,
):
    if not ctx.guild:
        await ctx.send("This command only works in servers.")
        return

    if not is_admin_or_owner(ctx.author):
        await ctx.send("This command is for Administrators only.", ephemeral=True)
        return

    if len(title) > 256:
        await ctx.send("Title is too long (max 256 characters).", ephemeral=True)
        return

    text = content.replace("\\n", "\n")
    if len(text) > 4096:
        await ctx.send("Content is too long (max 4096 characters).", ephemeral=True)
        return

    if color:
        if not re.match(r"^#?[0-9A-Fa-f]{6}$", color):
            await ctx.send("Please provide a valid hex color, e.g. #FF5733.", ephemeral=True)
            return
        embed_color = discord.Color.from_str(color if color.startswith("#") else f"#{color}")
    else:
        embed_color = get_guild_color(ctx.guild.id)

    if image_url and not re.match(r"^https?://\S+\.(png|jpg|jpeg|gif|webp)$", image_url, re.IGNORECASE):
        await ctx.send("Image URL must be a direct link ending in .png, .jpg, .jpeg, .gif, or .webp.", ephemeral=True)
        return

    embed = discord.Embed(title=title, description=text, color=embed_color)
    if image_url:
        embed.set_image(url=image_url)

    target = channel or ctx.channel
    perms = target.permissions_for(ctx.guild.me)
    if not (perms.view_channel and perms.send_messages and perms.embed_links):
        await ctx.send(
            f"I need **View Channel**, **Send Messages** and **Embed Links** in {target.mention} to post there.",
            ephemeral=True
        )
        return

    try:
        await target.send(embed=embed)
    except discord.Forbidden:
        await ctx.send(f"I don't have permission to send messages in {target.mention}.", ephemeral=True)
        return

    await ctx.send(f"✅ Embed sent in {target.mention}.", ephemeral=True)


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
    min_messages="Messages people must send to be eligible to win (default: none)"
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
    min_messages: typing.Optional[int] = 0,
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

    if min_messages is None:
        min_messages = 0
    if min_messages < 0 or min_messages > 1000:
        await ctx.send("The message requirement must be between 0 and 1000.")
        return

    end_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    guild_color = get_guild_color(ctx.guild.id)

    gw = {
        "channel_id": ctx.channel.id,
        "guild_id": ctx.guild.id,
        "host_id": ctx.author.id,
        "prize": prize,
        "winners": winners,
        "required_role_id": required_role.id if required_role else None,
        "blacklist_role_id": blacklist_role.id if blacklist_role else None,
        "bypass_role_id": bypass_role.id if bypass_role else None,
        "image_url": image_url,
        "min_messages": min_messages,
        "msg_counts": {},
        "winner_ids": [],
        "cancelled": False,
        "end_time": end_time,
        "joined_users": set(),
        "entries": [],
        "active": True,
        "wake": asyncio.Event(),
    }

    ping_role_id = default_ping_roles.get(ctx.guild.id)
    final_ping_role = ctx.guild.get_role(ping_role_id) if ping_role_id else None
    content = final_ping_role.mention if final_ping_role else None

    msg = await ctx.send(content=content, embed=build_giveaway_embed(gw))
    giveaways[msg.id] = gw
    view = GiveawayView(msg.id)
    await msg.edit(embed=build_giveaway_embed(gw, msg.id), view=view)

    # Private confirmation for the host (slash commands only)
    if ctx.interaction:
        try:
            jump_url = f"https://discord.com/channels/{ctx.guild.id}/{ctx.channel.id}/{msg.id}"
            confirm = discord.Embed(
                title="✅ Your giveaway is live!",
                description=(
                    f"**Prize:** {prize}\n"
                    f"**Winners:** {winners}\n"
                    f"**Ends:** {discord.utils.format_dt(end_time, style='R')}\n"
                    f"**Giveaway ID:** `{msg.id}`\n\n"
                    f"[Jump to the giveaway]({jump_url})\n\n"
                    f"Made a mistake? Use **/edit-giveaway** with that ID to fix it, "
                    f"or **/end-giveaway** to end it early."
                ),
                color=discord.Color.green()
            )
            await ctx.send(embed=confirm, ephemeral=True)
        except Exception as e:
            print(f"Confirmation message failed: {e}", flush=True)

    log_embed = discord.Embed(
        title="🎉 Giveaway Started",
        description=f"**Prize:** {prize}\n**Duration:** {duration}\n**Winners:** {winners}",
        color=guild_color
    )
    log_embed.add_field(name="Hosted by", value=ctx.author.mention, inline=True)
    log_embed.add_field(name="Channel", value=ctx.channel.mention, inline=True)
    log_embed.set_footer(text=f"Giveaway ID: {msg.id}")
    await send_log(ctx.guild, log_embed)

    # Wait until the (possibly edited) end time, or until the giveaway is ended early
    while gw["active"]:
        remaining = (gw["end_time"] - datetime.datetime.now(datetime.timezone.utc)).total_seconds()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(gw["wake"].wait(), timeout=remaining)
            gw["wake"].clear()
        except asyncio.TimeoutError:
            break

    if gw["active"]:
        await end_giveaway(msg.id)


def eligible_entries(gw, exclude=()):
    """Entries that can still win: joined, met the message requirement, still in the server, not already a winner."""
    guild = bot.get_guild(gw["guild_id"])
    need = gw.get("min_messages", 0)
    counts = gw.get("msg_counts", {})
    pool = []
    for uid in gw["entries"]:
        if uid in exclude or uid not in gw["joined_users"]:
            continue
        if need and counts.get(uid, 0) < need:
            continue
        if guild is not None and guild.get_member(uid) is None:
            continue
        pool.append(uid)
    return pool


def pick_winners(gw, count, exclude=()):
    pool = eligible_entries(gw, exclude)
    chosen = []
    for _ in range(min(count, len(set(pool)))):
        pick = random.choice(pool)
        chosen.append(pick)
        pool = [uid for uid in pool if uid != pick]
    return chosen


def winners_lines(gw, chosen):
    lines = []
    for uid in chosen:
        n = gw["entries"].count(uid)
        lines.append(f"{EMOJI['trophy']} <@{uid}> — **{n}** {'entry' if n == 1 else 'entries'}")
    return "\n".join(lines)


USERS_ONLY = discord.AllowedMentions(users=True, roles=False, everyone=False)


async def end_giveaway(giveaway_id):
    gw = giveaways.get(giveaway_id)
    if not gw or not gw["active"]:
        return

    gw["active"] = False
    wake = gw.get("wake")
    if wake:
        wake.set()
    channel = bot.get_channel(gw["channel_id"])
    if channel is None:
        return

    chosen = pick_winners(gw, gw["winners"])

    if not chosen:
        need = gw.get("min_messages", 0)
        if gw["entries"] and need:
            reason = f"😢 Nobody sent the required **{need}** message(s) — no winner this time."
        else:
            reason = "😢 Nobody entered — no winner this time."
        embed = discord.Embed(
            title=f"{EMOJI['party']} Giveaway Ended",
            description=f"**Prize:** {gw['prize']}\n\n{reason}",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Giveaway ID: {giveaway_id} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
        await channel.send(embed=embed)

        log_embed = discord.Embed(
            title="🎉 Giveaway Ended (No Winner)",
            description=f"**Prize:** {gw['prize']}",
            color=discord.Color.red()
        )
        log_embed.set_footer(text=f"Giveaway ID: {giveaway_id}")
        await send_log(channel.guild, log_embed)
        return

    gw["winner_ids"] = list(chosen)
    for uid in chosen:
        record_win(gw["guild_id"], uid)

    winners_text = winners_lines(gw, chosen)
    mentions = " ".join(f"<@{uid}>" for uid in chosen)

    embed = discord.Embed(
        title=f"{EMOJI['party']} Giveaway Ended!",
        description=f"**Prize:** {gw['prize']}\n\n**Winner(s):**\n{winners_text}\n\nCongratulations! {EMOJI['party']}\nBetter luck next time to everyone else! 🍀",
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name=f"{EMOJI['host']} Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    embed.set_footer(text=f"Giveaway ID: {giveaway_id} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
    await channel.send(content=f"🎉 Congratulations {mentions}!", embed=embed, allowed_mentions=USERS_ONLY)

    log_embed = discord.Embed(
        title="🎉 Giveaway Ended",
        description=f"**Prize:** {gw['prize']}\n**Winner(s):**\n{winners_text}",
        color=discord.Color.green()
    )
    log_embed.add_field(name="Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    log_embed.set_footer(text=f"Giveaway ID: {giveaway_id}")
    await send_log(channel.guild, log_embed)


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


@bot.tree.command(name="reroll", description="Pick new winner(s) for a finished giveaway")
@app_commands.describe(
    message_id="The giveaway's ID, shown at the bottom of the giveaway post",
    winners="How many new winners to pick (default: 1)"
)
@app_commands.guild_only()
async def reroll(interaction: discord.Interaction, message_id: str, winners: typing.Optional[int] = 1):
    if not can_manage_giveaways(interaction.user):
        await interaction.response.send_message("You don't have permission to reroll giveaways.", ephemeral=True)
        return

    try:
        gid = int(message_id.strip())
    except ValueError:
        await interaction.response.send_message("That doesn't look like a valid giveaway ID.", ephemeral=True)
        return

    gw = giveaways.get(gid)
    if not gw or gw["guild_id"] != interaction.guild.id:
        await interaction.response.send_message("No giveaway found with that ID.", ephemeral=True)
        return
    if gw["active"]:
        await interaction.response.send_message(
            "That giveaway is still running. Wait for it to finish (or use /end-giveaway) before rerolling.", ephemeral=True
        )
        return
    if gw.get("cancelled"):
        await interaction.response.send_message("That giveaway was cancelled, so there's nothing to reroll.", ephemeral=True)
        return

    if winners is None or winners < 1:
        winners = 1
    if winners > 20:
        await interaction.response.send_message("Max 20 winners per reroll.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    chosen = pick_winners(gw, winners, exclude=set(gw.get("winner_ids", [])))
    if not chosen:
        await interaction.followup.send(
            "There's nobody else eligible to win — everyone who qualified has already won.", ephemeral=True
        )
        return

    channel = bot.get_channel(gw["channel_id"])
    if channel is None:
        await interaction.followup.send("I couldn't find the channel this giveaway was in.", ephemeral=True)
        return

    winners_text = winners_lines(gw, chosen)
    mentions = " ".join(f"<@{uid}>" for uid in chosen)

    embed = discord.Embed(
        title=f"{EMOJI['party']} Giveaway Rerolled!",
        description=f"**Prize:** {gw['prize']}\n\n**New winner(s):**\n{winners_text}\n\nCongratulations! {EMOJI['party']}\nBetter luck next time to everyone else! 🍀",
        color=discord.Color.green()
    )
    embed.set_thumbnail(url=bot.user.display_avatar.url)
    embed.add_field(name=f"{EMOJI['host']} Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    embed.set_footer(text=f"Giveaway ID: {gid} • ChillBot 😎", icon_url=bot.user.display_avatar.url)

    try:
        await channel.send(content=f"🎉 Congratulations {mentions}!", embed=embed, allowed_mentions=USERS_ONLY)
    except Exception as e:
        print(f"Reroll announcement failed: {e}", flush=True)
        await interaction.followup.send(
            f"I couldn't post in {channel.mention}. Check that I can send messages and embeds there.", ephemeral=True
        )
        return

    gw.setdefault("winner_ids", []).extend(chosen)
    for uid in chosen:
        record_win(gw["guild_id"], uid)

    await interaction.followup.send(f"✅ Rerolled! New winner(s) announced in {channel.mention}.", ephemeral=True)

    log_embed = discord.Embed(
        title="🔁 Giveaway Rerolled",
        description=f"Rerolled by {interaction.user.mention}\n\n**New winner(s):**\n{winners_text}",
        color=discord.Color.orange()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(interaction.guild, log_embed)


@bot.tree.command(name="cancel-giveaway", description="Cancel a running giveaway without picking a winner")
@app_commands.describe(message_id="The giveaway's ID, shown at the bottom of the giveaway post")
@app_commands.guild_only()
async def cancel_giveaway(interaction: discord.Interaction, message_id: str):
    if not can_manage_giveaways(interaction.user):
        await interaction.response.send_message("You don't have permission to cancel giveaways.", ephemeral=True)
        return

    try:
        gid = int(message_id.strip())
    except ValueError:
        await interaction.response.send_message("That doesn't look like a valid giveaway ID.", ephemeral=True)
        return

    gw = giveaways.get(gid)
    if not gw or not gw["active"] or gw["guild_id"] != interaction.guild.id:
        await interaction.response.send_message("No active giveaway found with that ID.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    gw["active"] = False
    gw["cancelled"] = True
    wake = gw.get("wake")
    if wake:
        wake.set()

    channel = bot.get_channel(gw["channel_id"])

    # turn the original post into a "cancelled" post and remove its buttons
    try:
        post = await channel.fetch_message(gid)
        cancelled_post = discord.Embed(
            title="❌ GIVEAWAY CANCELLED",
            description=f"**Prize -** {gw['prize']}\n\nThis giveaway was cancelled by a host.",
            color=discord.Color.dark_grey()
        )
        cancelled_post.set_footer(text=f"Giveaway ID: {gid} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
        await post.edit(embed=cancelled_post, view=None)
    except Exception as e:
        print(f"Failed to update cancelled giveaway post: {e}", flush=True)

    try:
        announce = discord.Embed(
            title="❌ Giveaway Cancelled",
            description=f"**Prize:** {gw['prize']}\n\nThis giveaway was cancelled — no winner will be picked.",
            color=discord.Color.red()
        )
        announce.add_field(name=f"{EMOJI['host']} Hosted by", value=f"<@{gw['host_id']}>", inline=True)
        announce.set_footer(text=f"Giveaway ID: {gid} • ChillBot 😎", icon_url=bot.user.display_avatar.url)
        await channel.send(embed=announce)
    except Exception as e:
        print(f"Failed to announce cancellation: {e}", flush=True)

    await interaction.followup.send("✅ Giveaway cancelled.", ephemeral=True)

    log_embed = discord.Embed(
        title="❌ Giveaway Cancelled",
        description=f"**Prize:** {gw['prize']}\nCancelled by {interaction.user.mention}",
        color=discord.Color.orange()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(interaction.guild, log_embed)


@bot.tree.command(name="edit-giveaway", description="Fix a mistake in a running giveaway")
@app_commands.describe(
    message_id="The giveaway's ID, shown at the bottom of the giveaway post",
    prize="New prize text",
    winners="New number of winners (1-20)",
    duration="New time left from now, e.g. 30m, 2h, 1d (replaces the current end time)",
    required_role="New role required to join",
    blacklist_role="New blacklisted role for this giveaway",
    bypass_role="New bypass role for this giveaway",
    image_url="New direct image link (.png, .jpg, .jpeg, .gif, .webp)",
    min_messages="New messages needed to be eligible to win (0 = no requirement)",
    remove="Remove one of the optional extras from this giveaway"
)
@app_commands.choices(remove=[
    app_commands.Choice(name="Remove the required role", value="required_role"),
    app_commands.Choice(name="Remove the blacklisted role", value="blacklist_role"),
    app_commands.Choice(name="Remove the bypass role", value="bypass_role"),
    app_commands.Choice(name="Remove the image", value="image"),
])
async def edit_giveaway(
    interaction: discord.Interaction,
    message_id: str,
    prize: typing.Optional[str] = None,
    winners: typing.Optional[int] = None,
    duration: typing.Optional[str] = None,
    required_role: typing.Optional[discord.Role] = None,
    blacklist_role: typing.Optional[discord.Role] = None,
    bypass_role: typing.Optional[discord.Role] = None,
    image_url: typing.Optional[str] = None,
    min_messages: typing.Optional[int] = None,
    remove: typing.Optional[app_commands.Choice[str]] = None,
):
    if not interaction.guild:
        await interaction.response.send_message("This command only works in servers.", ephemeral=True)
        return

    if not can_manage_giveaways(interaction.user):
        await interaction.response.send_message("You don't have permission to edit giveaways.", ephemeral=True)
        return

    try:
        gid = int(message_id.strip())
    except ValueError:
        await interaction.response.send_message("That doesn't look like a valid giveaway ID.", ephemeral=True)
        return

    gw = giveaways.get(gid)
    if not gw or not gw["active"] or gw["guild_id"] != interaction.guild.id:
        await interaction.response.send_message(
            "No active giveaway found with that ID. Ended giveaways can't be edited.", ephemeral=True
        )
        return

    if all(v is None for v in (prize, winners, duration, required_role, blacklist_role, bypass_role, image_url, min_messages, remove)):
        await interaction.response.send_message("Pick at least one thing to change.", ephemeral=True)
        return

    # ----- validate everything before changing anything -----
    if prize is not None and (not prize.strip() or len(prize) > 200):
        await interaction.response.send_message("Prize must be 1-200 characters.", ephemeral=True)
        return

    if winners is not None and (winners < 1 or winners > 20):
        await interaction.response.send_message("Winners must be between 1 and 20.", ephemeral=True)
        return

    new_end = None
    if duration is not None:
        seconds = parse_duration(duration)
        if seconds is None:
            await interaction.response.send_message("Invalid duration. Use formats like `30m`, `1h`, `2d`, or `1h30m`.", ephemeral=True)
            return
        if seconds > 30 * 86400:
            await interaction.response.send_message("Duration can't be longer than 30 days.", ephemeral=True)
            return
        new_end = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)

    if image_url is not None and not re.match(r"^https?://\S+\.(png|jpg|jpeg|gif|webp)$", image_url, re.IGNORECASE):
        await interaction.response.send_message("Image URL must be a direct link ending in .png, .jpg, .jpeg, .gif, or .webp.", ephemeral=True)
        return

    if min_messages is not None and (min_messages < 0 or min_messages > 1000):
        await interaction.response.send_message("The message requirement must be between 0 and 1000.", ephemeral=True)
        return

    # ----- apply -----
    changes = []
    role_changed = False

    if remove:
        if remove.value == "required_role" and gw["required_role_id"]:
            gw["required_role_id"] = None
            changes.append("Removed the required role")
            role_changed = True
        elif remove.value == "blacklist_role" and gw["blacklist_role_id"]:
            gw["blacklist_role_id"] = None
            changes.append("Removed the blacklisted role")
            role_changed = True
        elif remove.value == "bypass_role" and gw["bypass_role_id"]:
            gw["bypass_role_id"] = None
            changes.append("Removed the bypass role")
            role_changed = True
        elif remove.value == "image" and gw.get("image_url"):
            gw["image_url"] = None
            changes.append("Removed the image")

    if prize is not None:
        gw["prize"] = prize
        changes.append(f"Prize → **{prize}**")

    if winners is not None:
        gw["winners"] = winners
        changes.append(f"Winners → **{winners}**")

    if new_end is not None:
        gw["end_time"] = new_end
        gw["wake"].set()
        changes.append(f"Ends → {discord.utils.format_dt(new_end, style='R')}")

    if required_role:
        gw["required_role_id"] = required_role.id
        changes.append(f"Required role → {required_role.mention}")
        role_changed = True

    if blacklist_role:
        gw["blacklist_role_id"] = blacklist_role.id
        changes.append(f"Blacklisted role → {blacklist_role.mention}")
        role_changed = True

    if bypass_role:
        gw["bypass_role_id"] = bypass_role.id
        changes.append(f"Bypass role → {bypass_role.mention}")
        role_changed = True

    if image_url is not None:
        gw["image_url"] = image_url
        changes.append("Image updated")

    if min_messages is not None:
        gw["min_messages"] = min_messages
        changes.append(f"Messages required → **{min_messages}**" if min_messages else "Removed the message requirement")

    if not changes:
        await interaction.response.send_message("Nothing to change — that extra isn't set on this giveaway.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=True)

    # ----- refresh the giveaway post -----
    post_updated = True
    try:
        channel = bot.get_channel(gw["channel_id"])
        giveaway_msg = await channel.fetch_message(gid)
        await giveaway_msg.edit(embed=build_giveaway_embed(gw, gid))
    except Exception as e:
        print(f"Failed to update giveaway post: {e}", flush=True)
        post_updated = False

    description = "\n".join(f"• {c}" for c in changes)
    if role_changed:
        description += "\n\n*Role changes only affect people who join from now on.*"
    if not post_updated:
        description += "\n\n⚠️ I saved the changes, but couldn't update the giveaway post (was it deleted?)."

    result = discord.Embed(title="✏️ Giveaway updated", description=description, color=discord.Color.green())
    result.set_footer(text=f"Giveaway ID: {gid}")
    await interaction.followup.send(embed=result, ephemeral=True)

    log_embed = discord.Embed(
        title="✏️ Giveaway Edited",
        description=f"Edited by {interaction.user.mention}\n\n" + "\n".join(f"• {c}" for c in changes),
        color=discord.Color.orange()
    )
    log_embed.set_footer(text=f"Giveaway ID: {gid}")
    await send_log(interaction.guild, log_embed)


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
    gw.get("msg_counts", {}).pop(member.id, None)
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

                gw["msg_counts"][message.author.id] = gw["msg_counts"].get(message.author.id, 0) + 1
                mult = get_multiplier(message.guild.id, message.author)
                for _ in range(mult):
                    gw["entries"].append(message.author.id)

    except Exception as e:
        print(f"on_message error: {e}", flush=True)


bot.run(os.environ["DISCORD_TOKEN"])
