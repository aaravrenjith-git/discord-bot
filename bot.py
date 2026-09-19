import os
import discord
from discord import app_commands
from discord.ext import commands
import random
import typing
import re
import datetime
from flask import Flask
from threading import Thread

app = Flask('')

@app.route('/')
def home():
    return "Bot is alive!"

def run_flask():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

Thread(target=run_flask).start()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="?", intents=intents)

OWNER_ID = 1160627021865549976

# ---------- DATA STORES (in memory) ----------

giveaways = {}
authorized_roles = {}
blacklisted_roles = {}
entry_channels = {}
multiplier_roles = {}


def can_manage_giveaways(member: discord.Member):
    if member.guild_permissions.administrator:
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
                await interaction.response.send_message("You left the giveaway.", ephemeral=True)
                return

            if not bypass:
                if is_blacklisted(interaction.guild.id, user, gw["blacklist_role_id"]):
                    await interaction.response.send_message("You're not allowed to join this giveaway.", ephemeral=True)
                    return

                if gw["required_role_id"]:
                    role = interaction.guild.get_role(gw["required_role_id"])
                    if role not in user.roles:
                        await interaction.response.send_message(
                            f"You need the **{role.name}** role to join this giveaway.", ephemeral=True
                        )
                        return

            gw["joined_users"].add(user.id)
            await interaction.response.send_message(
                "You joined! Send messages in the allowed channel(s) to rack up entries. Click again to leave. 🎉",
                ephemeral=True
            )
        except Exception as e:
            print(f"Join button error: {e}")
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
            await interaction.response.send_message(text, ephemeral=True)
        except Exception as e:
            print(f"Participants button error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("Something went wrong, try again.", ephemeral=True)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s)")
    except Exception as e:
        print(f"Slash command sync failed: {e}")


@bot.tree.command(name="setup", description="Admin: configure giveaway roles, channels, and multipliers")
@app_commands.describe(
    action="What setting to change",
    role="Role to use (for role-related actions)",
    channel="Channel to use (for channel-related actions)",
    multiplier="Entry multiplier, e.g. 2 for 2x entries (only for multiplier actions)"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Add authorized host role", value="add_role"),
    app_commands.Choice(name="Remove authorized host role", value="remove_role"),
    app_commands.Choice(name="Blacklist a role from joining (global)", value="blacklist_role"),
    app_commands.Choice(name="Remove role from blacklist", value="unblacklist_role"),
    app_commands.Choice(name="Add channel that counts entries", value="add_channel"),
    app_commands.Choice(name="Remove channel from counting", value="remove_channel"),
    app_commands.Choice(name="Set a role's entry multiplier", value="add_multiplier"),
    app_commands.Choice(name="Remove a role's entry multiplier", value="remove_multiplier"),
    app_commands.Choice(name="Show current settings", value="show"),
])
async def setup_cmd(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    role: typing.Optional[discord.Role] = None,
    channel: typing.Optional[discord.TextChannel] = None,
    multiplier: typing.Optional[int] = None,
):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("This command is for Administrators only.", ephemeral=True)
        return

    guild_id = interaction.guild.id
    act = action.value

    if act in ("add_role", "remove_role", "blacklist_role", "unblacklist_role", "add_multiplier", "remove_multiplier") and not role:
        await interaction.response.send_message("Please pick a role for this action.", ephemeral=True)
        return
    if act in ("add_channel", "remove_channel") and not channel:
        await interaction.response.send_message("Please pick a channel for this action.", ephemeral=True)
        return
    if act == "add_multiplier" and not multiplier:
        await interaction.response.send_message("Please provide a multiplier number, e.g. 2.", ephemeral=True)
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

    elif act == "show":
        roles = authorized_roles.get(guild_id, set())
        banned = blacklisted_roles.get(guild_id, set())
        chans = entry_channels.get(guild_id, set())
        mults = multiplier_roles.get(guild_id, {})

        roles_txt = ", ".join(f"<@&{r}>" for r in roles) or "None (Admins only)"
        banned_txt = ", ".join(f"<@&{r}>" for r in banned) or "None"
        chans_txt = ", ".join(f"<#{c}>" for c in chans) or "Default (each giveaway's own channel)"
        mults_txt = ", ".join(f"<@&{r}> ({m}x)" for r, m in mults.items()) or "None"

        embed = discord.Embed(title="⚙️ Giveaway Setup", color=discord.Color.blurple())
        embed.add_field(name="Authorized host roles", value=roles_txt, inline=False)
        embed.add_field(name="Blacklisted roles", value=banned_txt, inline=False)
        embed.add_field(name="Entry-counting channels", value=chans_txt, inline=False)
        embed.add_field(name="Multiplier roles", value=mults_txt, inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.hybrid_command(name="gstart", description="Start a giveaway")
@app_commands.describe(
    duration="e.g. 30m, 1h, 2d, 1h30m",
    prize="What's being given away",
    winners="How many winners (default 1)",
    required_role="Only members with this role can join",
    blacklist_role="Members with this role cannot join (this giveaway only)",
    bypass_role="Members with this role skip required_role and blacklist checks"
)
async def gstart(
    ctx,
    duration: str,
    prize: str,
    winners: typing.Optional[int] = 1,
    required_role: typing.Optional[discord.Role] = None,
    blacklist_role: typing.Optional[discord.Role] = None,
    bypass_role: typing.Optional[discord.Role] = None,
):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to start giveaways.")
        return

    seconds = parse_duration(duration)
    if seconds is None:
        await ctx.send("Invalid duration. Use formats like `30m`, `1h`, `2d`, or `1h30m`.")
        return

    if winners < 1:
        winners = 1

    end_time = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    timestamp = discord.utils.format_dt(end_time, style="R")

    embed = discord.Embed(
        title="🎉 GIVEAWAY 🎉",
        description=f"**Prize:** {prize}\n\nClick **Join Giveaway** below, then send messages in the allowed channel(s) — each message = 1 entry! Click Join again to leave.",
        color=discord.Color.gold()
    )
    embed.add_field(name="Hosted by", value=ctx.author.mention, inline=True)
    embed.add_field(name="Ends", value=timestamp, inline=True)
    embed.add_field(name="Winners", value=str(winners), inline=True)
    if required_role:
        embed.add_field(name="Required role", value=required_role.mention, inline=True)
    if blacklist_role:
        embed.add_field(name="Blacklisted role", value=blacklist_role.mention, inline=True)
    if bypass_role:
        embed.add_field(name="Bypass role", value=bypass_role.mention, inline=True)

    guild_mults = multiplier_roles.get(ctx.guild.id, {})
    if guild_mults:
        mults_text = "\n".join(f"<@&{rid}> — **{mult}x** entries" for rid, mult in guild_mults.items())
        embed.add_field(name="Bonus entry roles", value=mults_text, inline=False)

    embed.set_footer(text="Starting...")

    msg = await ctx.send(embed=embed)
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
    }

    embed.set_footer(text=f"Giveaway ID: {msg.id}")
    await msg.edit(embed=embed, view=view)

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
        return

    if not gw["entries"]:
        embed = discord.Embed(
            title="🎉 Giveaway Ended",
            description=f"**Prize:** {gw['prize']}\n\nNobody entered — no winner this time.",
            color=discord.Color.red()
        )
        embed.set_footer(text=f"Giveaway ID: {giveaway_id}")
        await channel.send(embed=embed)
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

    winners_text = "\n".join(f"<@{uid}>" for uid in chosen)

    embed = discord.Embed(
        title="🎉 Giveaway Ended",
        description=f"**Prize:** {gw['prize']}\n**Winner(s):**\n{winners_text}",
        color=discord.Color.green()
    )
    embed.add_field(name="Hosted by", value=f"<@{gw['host_id']}>", inline=True)
    embed.set_footer(text=f"Giveaway ID: {giveaway_id}")
    await channel.send(embed=embed)


@bot.hybrid_command(name="gend", description="End a giveaway early using its message ID")
async def gend(ctx, message_id: str):
    if not can_manage_giveaways(ctx.author):
        await ctx.send("You don't have permission to end giveaways.")
        return
    try:
        gid = int(message_id)
    except ValueError:
        await ctx.send("That doesn't look like a valid giveaway ID.")
        return
    if gid not in giveaways or not giveaways[gid]["active"]:
        await ctx.send("No active giveaway found with that ID.")
        return
    await end_giveaway(gid)
    await ctx.send("Giveaway ended.")


@bot.hybrid_command(name="gremove", description="Remove a user from a giveaway")
async def gremove(ctx, message_id: str, member: discord.Member):
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
    await ctx.send(f"Removed {member.mention} from that giveaway.")


@bot.event
async def on_message(message):
    try:
        if message.author.bot:
            return

        if bot.user in message.mentions and message.author.id == OWNER_ID:
            await message.channel.send("Yes, master? 👑")
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

        await bot.process_commands(message)
    except Exception as e:
        print(f"on_message error: {e}")


bot.run(os.environ["DISCORD_TOKEN"])
