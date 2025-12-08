# bot.py
# Single-file tournament scrims bot with a small FastAPI web dashboard.
# Requirements: see requirements.txt
from __future__ import annotations
import asyncio
import aiohttp
import io
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta
from typing import Dict, List, Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands
from zoneinfo import ZoneInfo
from dotenv import load_dotenv

# small web dashboard
from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse, Response
import uvicorn

# load .env
load_dotenv()

# -------------------------
# CONFIG (from .env)
# -------------------------
BOT_TOKEN = os.getenv("BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "0"))
AUDIT_LOG_CHANNEL_ID = int(os.getenv("AUDIT_LOG_CHANNEL_ID", "0"))

# staff payment verification channel (for payment screenshots)
STAFF_VERIFICATION_CHANNEL_ID = int(
    os.getenv("STAFF_VERIFICATION_CHANNEL_ID", "1446817930989010954")
)

# minutes to wait for payment before auto-cancel
PAYMENT_TIMEOUT_MINUTES = int(os.getenv("PAYMENT_TIMEOUT_MINUTES", "10"))
AUTO_BLACKLIST_ON_TIMEOUT = os.getenv("AUTO_BLACKLIST_ON_TIMEOUT", "true").lower() in (
    "1",
    "true",
    "yes",
)
STATE_PATH = os.getenv("STATE_PATH", "state.json")

IST = ZoneInfo("Asia/Kolkata")

# animated emoji IDs (change to your actual emoji ids)
TICK_EMOJI_ID = int(os.getenv("TICK_EMOJI_ID", "1407368712402767952"))
CROSS_EMOJI_ID = int(os.getenv("CROSS_EMOJI_ID", "1438443052170608793"))
TICK_EMOJI = discord.PartialEmoji(name="rga_tick1", id=TICK_EMOJI_ID, animated=True)
CROSS_EMOJI = discord.PartialEmoji(name="animatedCross", id=CROSS_EMOJI_ID, animated=True)

# QR image URL (host on a reliable CDN)
GPay_QR_URL = os.getenv(
    "GPay_QR_URL",
    "https://i.postimg.cc/Px80gCrd/Whats-App-Image-2025-12-06-at-11-07-37-b69f8dd9.jpg",
)

# intents
INTENTS = discord.Intents.default()
INTENTS.message_content = True
INTENTS.members = True
INTENTS.guilds = True
INTENTS.dm_messages = True
INTENTS.reactions = True

# -------------------------
# Bot class
# -------------------------
class ScrimsBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix="$", intents=INTENTS)

    async def setup_hook(self) -> None:
        guild_obj = discord.Object(id=GUILD_ID)
        try:
            self.tree.copy_global_to(guild=guild_obj)
            await self.tree.sync(guild=guild_obj)
        except Exception:
            # if sync fails for guild, still continue
            pass


bot = ScrimsBot()

# -------------------------
# Lobbies & types (update channel IDs in code or put in .env)
# -------------------------
@dataclass
class LobbyConfig:
    key: str
    label: str
    lobby_channel_id: int
    idp_channel_id: int
    match_time: dt_time


@dataclass
class MatchTypeConfig:
    key: str
    display_name: str
    fee_options: List[int]


LOBBIES: Dict[str, LobbyConfig] = {
    "12pm": LobbyConfig(
        "12pm",
        "12:00 PM",
        int(os.getenv("L12_LOBBY", "1446159476737577072")),
        int(os.getenv("L12_IDP", "1446159549152100515")),
        dt_time(12, 0),
    ),
    "03pm": LobbyConfig(
        "03pm",
        "03:00 PM",
        int(os.getenv("L15_LOBBY", "1446159569981280347")),
        int(os.getenv("L15_IDP", "1446159589773934605")),
        dt_time(15, 0),
    ),
    "06pm": LobbyConfig(
        "06pm",
        "06:00 PM",
        int(os.getenv("L18_LOBBY", "1446159681054838834")),
        int(os.getenv("L18_IDP", "1446159712168185997")),
        dt_time(18, 0),
    ),
    "09pm": LobbyConfig(
        "09pm",
        "09:00 PM",
        int(os.getenv("L21_LOBBY", "1446159738864668692")),
        int(os.getenv("L21_IDP", "1446159763124523100")),
        dt_time(21, 0),
    ),
    "12am": LobbyConfig(
        "12am",
        "12:00 AM",
        int(os.getenv("L00_LOBBY", "1446159789640913077")),
        int(os.getenv("L00_IDP", "1446159812646932680")),
        dt_time(0, 0),
    ),
}

MATCH_TYPES: Dict[str, MatchTypeConfig] = {
    "special": MatchTypeConfig("special", "MEGA LIVE B²B ⁶ MATCH", [55]),
    "mini": MatchTypeConfig("mini", "𝗠𝗶𝗻𝗶 B²B³ MATCH", [25, 30]),
    "mega": MatchTypeConfig("mega", "𝗠𝗲𝗴𝗮 B²B⁶ MATCH", [35, 45]),
}

# -------------------------
# Registration data structures
# -------------------------
@dataclass
class Registration:
    reg_id: int
    lobby_key: str
    message_id: int
    team_name: str
    leader_id: int
    player_ids: List[int]
    match_type: Optional[str] = None
    entry_fee: Optional[int] = None
    status: str = "pending"  # pending -> payment_pending -> confirmed
    created_at: Optional[datetime] = None


@dataclass
class LobbyRuntime:
    slot_status_message_id: Optional[int] = None
    liveboard_message_id: Optional[int] = None


# in-memory stores
REG_COUNTER: int = 0
REG_BY_ID: Dict[int, Registration] = {}
REG_BY_MESSAGE: Dict[int, Registration] = {}
RUNTIME: Dict[str, LobbyRuntime] = {k: LobbyRuntime() for k in LOBBIES.keys()}

# pending tasks
PENDING_EXPIRE_TASKS: Dict[int, asyncio.Task] = {}
PENDING_PAYMENT_TASKS: Dict[int, asyncio.Task] = {}

# blacklist persist
BLACKLIST: Dict[str, dict] = {}

# throttles
LAST_STATUS_UPDATE: Dict[str, datetime] = {}
LAST_LIVE_UPDATE: Dict[str, datetime] = {}

# track which lobbies have registration open (for 15-min updater)
LOBBY_ACTIVE: Dict[str, bool] = {k: False for k in LOBBIES.keys()}

# payment verification request mapping: verification_message_id -> reg_id
PAYMENT_VERIFICATION_REQUESTS: Dict[int, int] = {}

# -------------------------
# persistence helpers
# -------------------------
def load_state():
    global BLACKLIST, REG_COUNTER
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
                BLACKLIST = d.get("blacklist", {})
                REG_COUNTER = int(d.get("reg_counter", 0))
        except Exception:
            BLACKLIST = {}
            REG_COUNTER = 0
    else:
        BLACKLIST = {}
        REG_COUNTER = 0


def save_state():
    try:
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "blacklist": BLACKLIST,
                    "reg_counter": REG_COUNTER,
                },
                f,
                default=str,
                indent=2,
            )
    except Exception:
        pass


# -------------------------
# helper functions
# -------------------------
def next_reg_id() -> int:
    global REG_COUNTER
    REG_COUNTER += 1
    save_state()
    return REG_COUNTER


def get_lobby_key_by_channel(channel_id: int) -> Optional[str]:
    for key, cfg in LOBBIES.items():
        if cfg.lobby_channel_id == channel_id:
            return key
    return None


def registrations_for(
    lobby_key: str,
    match_type: Optional[str] = None,
    statuses: Optional[List[str]] = None,
) -> List[Registration]:
    results: List[Registration] = []
    for reg in REG_BY_ID.values():
        if reg.lobby_key != lobby_key:
            continue
        if match_type is not None and reg.match_type != match_type:
            continue
        if statuses is not None and reg.status not in statuses:
            continue
        results.append(reg)
    return results


def total_confirmed_slots(lobby_key: str) -> int:
    return len(registrations_for(lobby_key, None, ["confirmed"]))


def reserved_slots_count(lobby_key: str) -> int:
    return len(registrations_for(lobby_key, None, ["payment_pending", "confirmed"]))


def confirmed_slots_for_type(lobby_key: str, match_type: str) -> int:
    return len(registrations_for(lobby_key, match_type, ["confirmed"]))


def is_duplicate_registration(
    lobby_key: str, team_name: str, player_ids: List[int]
) -> bool:
    now = datetime.now(tz=IST)
    for reg in REG_BY_ID.values():
        if reg.lobby_key != lobby_key:
            continue
        # ignore old pending registrations older than 10 minutes
        if reg.status == "pending" and reg.created_at:
            if (now - reg.created_at) > timedelta(minutes=10):
                continue
        if (
            reg.team_name.lower() == team_name.lower()
            and reg.status in {"payment_pending", "confirmed"}
        ):
            return True
        for pid in player_ids:
            if pid in reg.player_ids and reg.status in {
                "payment_pending",
                "confirmed",
            }:
                return True
    return False


async def audit_log(
    guild: discord.Guild,
    title: str,
    description: str,
    color: discord.Color = discord.Color.dark_grey(),
):
    try:
        ch = guild.get_channel(AUDIT_LOG_CHANNEL_ID)
        if not isinstance(ch, discord.TextChannel):
            return
        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=datetime.now(tz=IST),
        )
        await ch.send(embed=embed)
    except Exception:
        pass


def lobby_description(label: str) -> str:
    return f"""SS ESPORTS {label} LOBBIES ⏰

🕒 {label} SPECIAL LIVE LOBBY
★ MEGA LIVE B²B ⁶ MATCH ★ 

➤ Entry Fee: ₹55 
➤ Prizepool: 520 
➤ Distribution ⁵⁵: 250 / 160 / 110     

━━━━━━━━━━━━━━━
🕒 {label}
★ 𝗠𝗶𝗻𝗶 B²B³ MATCH ★ 

➤ Entry Fee: ₹25 / ₹30 
➤ Prizepool: 220 / 270 
➤ Distribution ²⁵: 100 / 70 / 50 — ³⁰: 120 / 90 / 60 
━━━━━━━━━━━━━━━
🕒 {label}
★ 𝗠𝗲𝗴𝗮 B²B⁶ MATCH ★ 

➤ Entry Fee: ₹35 / ₹45
➤ Prizepool: 320 / 420
➤ Distribution ³⁵: 150 / 100 / 70 — ⁴⁵: 200 / 130 / 90 
━━━━━━━━━━━━━━━━━━━━
🚀 Book Fast. Play Fast. Win Big — SS Esports"""


def strict_reject_text() -> str:
    return (
        "❌ **Invalid registration.**\n\n"
        "Register in this format only:\n"
        "`Team Name :- <your team name>`\n"
        "`@Member1`\n"
        "`@Member2`\n"
        "`@Member3`\n"
        "`@Member4`\n\n"
        "• Exactly **4 members** must be mentioned.\n"
        "• No duplicate players across teams.\n"
        "Please **re-register with only your squad**."
    )


# -------------------------
# permissions & channel helpers
# -------------------------
async def set_active_lobby_permissions(
    guild: discord.Guild, active_key: Optional[str]
) -> None:
    """
    Enable sending messages only in the currently active lobby (for registration),
    but keep all lobby channels visible with read history.
    """
    for key, cfg in LOBBIES.items():
        ch = guild.get_channel(cfg.lobby_channel_id)
        if not isinstance(ch, discord.TextChannel):
            continue
        overwrites = ch.overwrites.copy()
        default_ow = overwrites.get(guild.default_role, discord.PermissionOverwrite())
        default_ow.view_channel = True
        default_ow.read_message_history = True
        default_ow.send_messages = key == active_key
        overwrites[guild.default_role] = default_ow
        try:
            await ch.edit(overwrites=overwrites, reason="Adjust scrim lobby permissions")
        except discord.Forbidden:
            pass


async def init_idp_locks(guild: discord.Guild) -> None:
    """
    Lock all IDP channels to be invisible for @everyone by default.
    Access will be opened per-role when team gets confirmed.
    """
    for cfg in LOBBIES.values():
        ch = guild.get_channel(cfg.idp_channel_id)
        if not isinstance(ch, discord.TextChannel):
            continue
        overwrites = ch.overwrites.copy()
        default_ow = overwrites.get(guild.default_role, discord.PermissionOverwrite())
        default_ow.view_channel = False
        default_ow.send_messages = False
        overwrites[guild.default_role] = default_ow
        try:
            await ch.edit(overwrites=overwrites, reason="Lock IDP channels by default")
        except discord.Forbidden:
            pass


# -------------------------
# QR helper
# -------------------------
async def fetch_qr_file_from_url(url: str) -> Optional[discord.File]:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                data = await resp.read()
                return discord.File(io.BytesIO(data), filename="payment_qr.png")
    except Exception:
        return None


# -------------------------
# status & liveboard
# -------------------------
async def update_slot_status(
    lobby_key: str, guild: discord.Guild, *, force: bool = False
) -> None:
    now = datetime.now(tz=IST)
    last = LAST_STATUS_UPDATE.get(lobby_key)
    if not force and last and (now - last) < timedelta(minutes=15):
        return

    cfg = LOBBIES[lobby_key]
    rt = RUNTIME[lobby_key]
    lobby_channel = guild.get_channel(cfg.lobby_channel_id)
    if not isinstance(lobby_channel, discord.TextChannel):
        return

    special = confirmed_slots_for_type(lobby_key, "special")
    mini = confirmed_slots_for_type(lobby_key, "mini")
    mega = confirmed_slots_for_type(lobby_key, "mega")
    total = special + mini + mega
    reserved = reserved_slots_count(lobby_key)

    embed = discord.Embed(
        title=f"📊 Slot Status — {cfg.label}",
        description=(
            f"**MEGA LIVE B²B ⁶ MATCH**: `{special}/12`\n"
            f"**Mini B²B³ MATCH**: `{mini}/12`\n"
            f"**Mega B²B⁶ MATCH**: `{mega}/12`\n\n"
            f"**Total Confirmed:** `{total}/36`\n"
            f"**Reserved (payment pending + confirmed):** `{reserved}/36`\n"
            "🔔 Lobby closes automatically when 36/36 are filled."
        ),
        color=discord.Color.teal(),
    )
    embed.set_footer(text="SS Esports • Live Slot Monitor")

    try:
        if rt.slot_status_message_id:
            msg = await lobby_channel.fetch_message(rt.slot_status_message_id)
            await msg.edit(embed=embed)
        else:
            msg = await lobby_channel.send(embed=embed)
            rt.slot_status_message_id = msg.id
    except (discord.NotFound, discord.Forbidden):
        try:
            msg = await lobby_channel.send(embed=embed)
            rt.slot_status_message_id = msg.id
        except Exception:
            pass

    LAST_STATUS_UPDATE[lobby_key] = now


async def update_liveboard(
    lobby_key: str, guild: discord.Guild, *, force: bool = False
) -> None:
    now = datetime.now(tz=IST)
    last = LAST_LIVE_UPDATE.get(lobby_key)
    if not force and last and (now - last) < timedelta(minutes=15):
        return

    cfg = LOBBIES[lobby_key]
    rt = RUNTIME[lobby_key]
    idp_channel = guild.get_channel(cfg.idp_channel_id)
    if not isinstance(idp_channel, discord.TextChannel):
        return

    lines: List[str] = []
    for mt_key, mt_cfg in MATCH_TYPES.items():
        regs = registrations_for(lobby_key, mt_key, ["confirmed"])
        if not regs:
            continue
        lines.append(f"__**{mt_cfg.display_name}**__")
        for idx, reg in enumerate(regs, start=1):
            lines.append(f"`{idx:02}`  **{reg.team_name}** — <@{reg.leader_id}>")
        lines.append("")

    description = "\n".join(lines) if lines else "No confirmed squads yet."
    embed = discord.Embed(
        title=f"📜 Live Board — {cfg.label}",
        description=description,
        color=discord.Color.blurple(),
    )
    embed.set_footer(text="SS Esports • Confirmed Squads")

    try:
        if rt.liveboard_message_id:
            msg = await idp_channel.fetch_message(rt.liveboard_message_id)
            await msg.edit(embed=embed)
        else:
            msg = await idp_channel.send(embed=embed)
            rt.liveboard_message_id = msg.id
    except (discord.NotFound, discord.Forbidden):
        try:
            msg = await idp_channel.send(embed=embed)
            rt.liveboard_message_id = msg.id
        except Exception:
            pass

    LAST_LIVE_UPDATE[lobby_key] = now


@tasks.loop(minutes=15)
async def slot_status_broadcaster():
    """
    Every 15 minutes, post a fresh slot status embed in active lobbies ONLY.
    'Active' = registration for that lobby has been opened (scheduler or $post_lobbies)
    and not manually closed.
    """
    await bot.wait_until_ready()
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    for lobby_key, cfg in LOBBIES.items():
        if not LOBBY_ACTIVE.get(lobby_key, False):
            continue  # only broadcast for lobbies with active registration

        channel = guild.get_channel(cfg.lobby_channel_id)
        if not isinstance(channel, discord.TextChannel):
            continue

        special = confirmed_slots_for_type(lobby_key, "special")
        mini = confirmed_slots_for_type(lobby_key, "mini")
        mega = confirmed_slots_for_type(lobby_key, "mega")
        total = special + mini + mega

        embed = discord.Embed(
            title=f"📊 Slot Status — {cfg.label}",
            description=(
                f"**MEGA LIVE B²B ⁶ MATCH**: `{special}/12`\n"
                f"**Mini B²B³ MATCH**: `{mini}/12`\n"
                f"**Mega B²B⁶ MATCH**: `{mega}/12`\n\n"
                f"**Total Confirmed:** `{total}/36`\n"
                "🔔 Next update in 15 minutes."
            ),
            color=discord.Color.teal(),
        )
        embed.set_footer(text="SS Esports • Live Slot Monitor")
        try:
            await channel.send(embed=embed)
        except discord.Forbidden:
            pass


# -------------------------
# registration handling
# -------------------------
async def delete_after_delay(msg: discord.Message, delay: int = 7) -> None:
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except (discord.NotFound, discord.Forbidden):
        pass


async def expire_pending(reg_id: int):
    await asyncio.sleep(300)  # 5 minutes for initial DM flow
    reg = REG_BY_ID.get(reg_id)
    if not reg:
        return
    if reg.status == "pending":
        REG_BY_ID.pop(reg.reg_id, None)
        REG_BY_MESSAGE.pop(reg.message_id, None)
        PENDING_EXPIRE_TASKS.pop(reg.reg_id, None)
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await audit_log(
                guild,
                "⌛ Registration Expired",
                f"Team **{reg.team_name}** did not complete DM flow in 5 minutes.",
                discord.Color.orange(),
            )


async def auto_cancel_payment_timeout(reg_id: int, timeout_minutes: int):
    await asyncio.sleep(timeout_minutes * 60)
    reg = REG_BY_ID.get(reg_id)
    if not reg:
        return
    if reg.status == "payment_pending":
        REG_BY_ID.pop(reg.reg_id, None)
        REG_BY_MESSAGE.pop(reg.message_id, None)
        PENDING_PAYMENT_TASKS.pop(reg.reg_id, None)
        guild = bot.get_guild(GUILD_ID)
        if guild:
            await audit_log(
                guild,
                "⌛ Payment Timeout - Registration Cancelled",
                f"Team **{reg.team_name}** (leader <@{reg.leader_id}>) payment not received in time.",
                discord.Color.orange(),
            )
            try:
                leader = guild.get_member(reg.leader_id)
                if leader:
                    await leader.send(
                        f"Your registration for **{reg.team_name}** was cancelled: payment not received in time."
                    )
            except Exception:
                pass
            if AUTO_BLACKLIST_ON_TIMEOUT:
                BLACKLIST[str(reg.leader_id)] = {
                    "leader_id": reg.leader_id,
                    "team": reg.team_name,
                    "reason": "Payment Timeout",
                    "created_at": datetime.now(tz=IST).isoformat(),
                }
                save_state()
                await audit_log(
                    guild,
                    "🚫 Auto Blacklist",
                    f"Leader <@{reg.leader_id}> auto-blacklisted for payment timeout.",
                    discord.Color.red(),
                )




# -------------------------
# PAYMENT CONFIRMATION CORE (used by markpaid, dashboard, reactions)
# -------------------------
async def confirm_registration(
    reg: Registration,
    guild: discord.Guild,
    *,
    verifier_id: Optional[int] = None,
    source: str = "manual",
) -> None:
    """
    Centralized confirmation logic:
    - cancel payment timeout
    - set status=confirmed
    - create/assign role
    - open IDP channel for that role
    - send squad card
    - update slot status & liveboard
    - audit log
    """
    # cancel payment timeout
    t = PENDING_PAYMENT_TASKS.pop(reg.reg_id, None)
    if t:
        try:
            t.cancel()
        except Exception:
            pass

    reg.status = "confirmed"

    cfg = LOBBIES[reg.lobby_key]
    mt_cfg = MATCH_TYPES[reg.match_type]
    role_name = f"{cfg.label} — {mt_cfg.display_name}"

    role = discord.utils.get(guild.roles, name=role_name)
    if role is None:
        try:
            role = await guild.create_role(
                name=role_name, reason="Tournament lobby role"
            )
        except discord.Forbidden:
            role = None

    # assign role to all players
    if role:
        for pid in reg.player_ids:
            member = guild.get_member(pid)
            if member:
                try:
                    await member.add_roles(
                        role, reason=f"Payment verified - assign role ({source})"
                    )
                except discord.Forbidden:
                    pass

    # open IDP channel for that role + post squad card
    idp_channel = guild.get_channel(cfg.idp_channel_id)
    if isinstance(idp_channel, discord.TextChannel) and role:
        overwrites = idp_channel.overwrites.copy()
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True, read_message_history=True, send_messages=False
        )
        default_ow = overwrites.get(guild.default_role, discord.PermissionOverwrite())
        default_ow.view_channel = False
        overwrites[guild.default_role] = default_ow

        try:
            await idp_channel.edit(
                overwrites=overwrites, reason=f"Open IDP for paid players ({source})"
            )
        except discord.Forbidden:
            pass

        lines = [
            f"✧ 𝐓𝐞𝐚𝐦 𝐍𝐚𝐦𝐞 :- **{reg.team_name}**",
            f"• 𝐋𝐞𝐚𝐝𝐞𝐫 :- <@{reg.player_ids[0]}>",
            f"• 𝐌𝐞𝐦𝐛𝐞𝐫𝟏 :- <@{reg.player_ids[1]}>",
            f"• 𝐌𝐞𝐦𝐛𝐞𝐫𝟐 :- <@{reg.player_ids[2]}>",
            f"• 𝐌𝐞𝐦𝐛𝐞𝐫𝟑 :- <@{reg.player_ids[3]}>",
        ]
        try:
            await idp_channel.send(
                embed=discord.Embed(
                    title=f"{cfg.label} — {mt_cfg.display_name}",
                    description="\n".join(lines),
                    color=discord.Color.green(),
                )
            )
        except discord.Forbidden:
            pass

    # update status boards
    await update_slot_status(reg.lobby_key, guild, force=True)
    await update_liveboard(reg.lobby_key, guild, force=True)

    # audit log
    verifier_text = f"<@{verifier_id}>" if verifier_id else source
    await audit_log(
        guild,
        "✅ Payment Verified",
        f"Team: **{reg.team_name}**\nVerified by: {verifier_text}",
        discord.Color.green(),
    )


# -------------------------
# DM PAYMENT PROOF HANDLING
# -------------------------
async def handle_dm_payment_message(message: discord.Message):
    """
    Handle DMs from players:
    - If leader has a payment_pending registration and sends screenshot + 'paid',
      forward proof to staff verification channel with ✅/❌ reactions.
    """
    if message.author.bot:
        return

    content_lower = (message.content or "").lower()
    has_paid_word = "paid" in content_lower or "payment done" in content_lower

    # Require at least one attachment AND "paid" keyword to avoid spam
    if not message.attachments or not has_paid_word:
        return

    # find latest payment_pending registration for this leader
    candidate_regs = [
        r
        for r in REG_BY_ID.values()
        if r.leader_id == message.author.id and r.status == "payment_pending"
    ]
    if not candidate_regs:
        try:
            await message.channel.send(
                "⚠ No active payment-pending registration found for you.\n"
                "If you just registered, please wait for the payment QR DM first."
            )
        except Exception:
            pass
        return

    reg = max(
        candidate_regs, key=lambda r: r.created_at or datetime.now(tz=IST)
    )  # latest

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    staff_channel = guild.get_channel(STAFF_VERIFICATION_CHANNEL_ID)
    if not isinstance(staff_channel, discord.TextChannel):
        try:
            await message.channel.send(
                "⚠ Payment proof received, but staff verification channel is not configured. Please contact management."
            )
        except Exception:
            pass
        return

    cfg = LOBBIES[reg.lobby_key]
    mt_cfg = MATCH_TYPES[reg.match_type]

    embed = discord.Embed(
        title="💳 Payment Proof Received",
        description=(
            f"**Team:** {reg.team_name}\n"
            f"**Lobby:** {cfg.label}\n"
            f"**Match Type:** {mt_cfg.display_name}\n"
            f"**Entry Fee:** ₹{reg.entry_fee}\n\n"
            f"**Leader:** <@{reg.leader_id}> (`{reg.leader_id}`)\n"
            f"**Players:** " + ", ".join(f"<@{pid}>" for pid in reg.player_ids) + "\n\n"
            f"**Leader Message:**\n{message.content or '*No text*'}"
        ),
        color=discord.Color.orange(),
        timestamp=datetime.now(tz=IST),
    )
    embed.set_footer(text="React ✅ to approve, ❌ to reject • SS Esports Staff Panel")

    # re-upload up to 4 attachments as files
    files: List[discord.File] = []
    for att in message.attachments[:4]:
        try:
            files.append(await att.to_file())
        except Exception:
            continue

    try:
        staff_msg = await staff_channel.send(embed=embed, files=files or None)
        try:
            await staff_msg.add_reaction("✅")
            await staff_msg.add_reaction("❌")
        except Exception:
            pass

        PAYMENT_VERIFICATION_REQUESTS[staff_msg.id] = reg.reg_id

        await message.channel.send(
            "✅ **Payment proof received.**\n"
            "Your screenshot has been sent to SS Esports staff for verification.\n"
            "Please wait for confirmation."
        )

        await audit_log(
            guild,
            "💳 Payment Proof Forwarded",
            f"Team **{reg.team_name}** (leader <@{reg.leader_id}>) uploaded proof.\n"
            f"Forwarded to staff verification channel.",
            discord.Color.orange(),
        )
    except Exception:
        try:
            await message.channel.send(
                "⚠ Failed to forward your payment proof to staff. Please contact management."
            )
        except Exception:
            pass


@bot.event
async def on_raw_reaction_add(payload: discord.RawReactionActionEvent):
    """
    Handle staff reactions on payment verification messages:
    - ✅ approve -> confirm registration
    - ❌ reject -> cancel registration
    """
    if payload.user_id == bot.user.id:
        return
    if payload.channel_id != STAFF_VERIFICATION_CHANNEL_ID:
        return

    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    member = guild.get_member(payload.user_id)
    if member is None:
        return

    # require staff permission
    if not (member.guild_permissions.manage_guild or member.guild_permissions.administrator):
        return

    reg_id = PAYMENT_VERIFICATION_REQUESTS.get(payload.message_id)
    if not reg_id:
        return

    emoji_str = str(payload.emoji)
    # fetch message to edit embed
    channel = guild.get_channel(payload.channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    try:
        msg = await channel.fetch_message(payload.message_id)
    except discord.NotFound:
        return
    except discord.Forbidden:
        return

    reg = REG_BY_ID.get(reg_id)
    if not reg:
        return

    # APPROVE
    if emoji_str == "✅":
        await confirm_registration(reg, guild, verifier_id=member.id, source="reaction")
        # notify leader
        try:
            user = await bot.fetch_user(reg.leader_id)
            await user.send(
                f"✅ Your payment for **{reg.team_name}** has been **approved**.\n"
                f"You are now confirmed in **{LOBBIES[reg.lobby_key].label} — {MATCH_TYPES[reg.match_type].display_name}**."
            )
        except Exception:
            pass

        # update embed
        try:
            if msg.embeds:
                e = msg.embeds[0]
                e.color = discord.Color.green()
                e.add_field(
                    name="Status",
                    value=f"✅ Approved by {member.mention}",
                    inline=False,
                )
                await msg.edit(embed=e)
        except Exception:
            pass

        # once approved, we can remove from map
        PAYMENT_VERIFICATION_REQUESTS.pop(payload.message_id, None)

    # REJECT
    elif emoji_str == "❌":
        # cancel registration + timeout
        t = PENDING_PAYMENT_TASKS.pop(reg.reg_id, None)
        if t:
            try:
                t.cancel()
            except Exception:
                pass
        REG_BY_ID.pop(reg.reg_id, None)
        REG_BY_MESSAGE.pop(reg.message_id, None)

        await audit_log(
            guild,
            "❌ Payment Rejected",
            f"Team **{reg.team_name}** (leader <@{reg.leader_id}>) payment rejected by <@{member.id}>.",
            discord.Color.red(),
        )

        try:
            user = await bot.fetch_user(reg.leader_id)
            await user.send(
                "❌ Your payment proof was **rejected** by SS Esports management.\n"
                "If you think this is a mistake, contact staff with proper proof."
            )
        except Exception:
            pass

        try:
            if msg.embeds:
                e = msg.embeds[0]
                e.color = discord.Color.red()
                e.add_field(
                    name="Status",
                    value=f"❌ Rejected by {member.mention}",
                    inline=False,
                )
                await msg.edit(embed=e)
        except Exception:
            pass

        PAYMENT_VERIFICATION_REQUESTS.pop(payload.message_id, None)


# -------------------------
# MESSAGE HANDLER (guild + DM)
# -------------------------
@bot.event
async def on_message(message: discord.Message):
    # allow prefix commands to work
    await bot.process_commands(message)

    if message.author.bot:
        return

    # -------------------------
    # DM handling (payment proofs)
    # -------------------------
    if isinstance(message.channel, discord.DMChannel):
        await handle_dm_payment_message(message)
        return

    # only guild messages beyond this point
    if not message.guild:
        return

    # -------------------------
    # blacklist check
    # -------------------------
    if str(message.author.id) in BLACKLIST:
        try:
            await message.add_reaction(CROSS_EMOJI)
        except Exception:
            pass

        warn = await message.channel.send(
            "🚫 **Registration Denied**\n"
            "You are **blacklisted** from SS Esports.\n"
            "Contact management if this is a mistake."
        )
        bot.loop.create_task(delete_after_delay(message))
        bot.loop.create_task(delete_after_delay(warn))
        return

    # -------------------------
    # lobby channel check
    # -------------------------
    lobby_key = get_lobby_key_by_channel(message.channel.id)
    if lobby_key is None:
        return

    # -------------------------
    # FLEXIBLE REGISTRATION RULES
    # -------------------------
    mentions = message.mentions

    # ✅ Exactly 4 mentions required
    if len(mentions) != 4:
        try:
            await message.add_reaction(CROSS_EMOJI)
        except Exception:
            pass

        warn = await message.reply(
            "❌ **Invalid Registration**\n\n"
            "• Exactly **4 members must be mentioned**\n"
            "• Message format can be **anything**\n"
            "• **Leader is automatically the sender**\n\n"
            "✅ Register again with only 4 mentions.",
            mention_author=False,
        )
        bot.loop.create_task(delete_after_delay(message))
        bot.loop.create_task(delete_after_delay(warn))
        return

    # -------------------------
    # extract players / leader
    # -------------------------
    player_ids = [m.id for m in mentions]
    leader_id = message.author.id

    # auto team name (safe & unique)
    team_name = f"Team-{message.author.display_name}"

    # -------------------------
    # duplicate protection
    # -------------------------
    if is_duplicate_registration(lobby_key, team_name, player_ids):
        try:
            await message.add_reaction(CROSS_EMOJI)
        except Exception:
            pass

        warn = await message.reply(
            "❌ **Duplicate Registration Detected**\n\n"
            "• One or more players are already registered\n"
            "• Multiple entries are not allowed\n\n"
            "✅ Use a different squad.",
            mention_author=False,
        )
        bot.loop.create_task(delete_after_delay(message))
        bot.loop.create_task(delete_after_delay(warn))
        return

    # -------------------------
    # create registration
    # -------------------------
    try:
        await message.add_reaction(TICK_EMOJI)
    except Exception:
        pass

    reg = Registration(
        reg_id=next_reg_id(),
        lobby_key=lobby_key,
        message_id=message.id,
        team_name=team_name,
        leader_id=leader_id,
        player_ids=player_ids,
        status="pending",
        created_at=datetime.now(tz=IST),
    )

    REG_BY_ID[reg.reg_id] = reg
    REG_BY_MESSAGE[message.id] = reg

    # expiry timer for unfinished DM flow
    expire_task = asyncio.create_task(expire_pending(reg.reg_id))
    PENDING_EXPIRE_TASKS[reg.reg_id] = expire_task

    info = await message.reply(
        "✅ **Registration Accepted**\n"
        "📩 Check the **Leader’s DM** to continue.",
        mention_author=False,
    )
    bot.loop.create_task(delete_after_delay(info, delay=8))

    # -------------------------
    # start DM flow
    # -------------------------
    leader_member = message.guild.get_member(leader_id)
    if not leader_member:
        REG_BY_ID.pop(reg.reg_id, None)
        REG_BY_MESSAGE.pop(reg.message_id, None)
        expire_task.cancel()
        return

    dm_sent = await start_dm_flow(leader_member, reg, message.guild)
    if not dm_sent:
        REG_BY_ID.pop(reg.reg_id, None)
        REG_BY_MESSAGE.pop(reg.message_id, None)
        PENDING_EXPIRE_TASKS.pop(reg.reg_id, None)
        expire_task.cancel()

        try:
            await message.add_reaction(CROSS_EMOJI)
        except Exception:
            pass

        warn = await message.reply(
            "⚠ **DM Failed**\n\n"
            "Enable DMs:\n"
            "`Server Settings → Privacy → Allow Direct Messages`\n\n"
            "Then **register again**.",
            mention_author=False,
        )
        bot.loop.create_task(delete_after_delay(warn, 15))
        return

    # -------------------------
    # audit log
    # -------------------------
    await audit_log(
        message.guild,
        "📝 Registration Submitted",
        f"Lobby: **{LOBBIES[lobby_key].label}**\n"
        f"Team: **{team_name}**\n"
        f"Leader: <@{leader_id}>",
        discord.Color.blue(),
    )



# -------------------------
# DM flow
# -------------------------
class MatchTypeView(discord.ui.View):
    def __init__(self, reg_id: int, lobby_key: str):
        super().__init__(timeout=300)
        self.reg_id = reg_id
        self.lobby_key = lobby_key

    async def handle_choice(self, interaction: discord.Interaction, mt_key: str):
        guild = interaction.guild or bot.get_guild(GUILD_ID)
        if guild is None:
            await interaction.response.send_message(
                "Internal error: guild not found.", ephemeral=True
            )
            return

        reg = REG_BY_ID.get(self.reg_id)
        if reg is None or reg.status not in {"pending", "payment_pending"}:
            await interaction.response.send_message(
                "Registration not found or already processed.", ephemeral=True
            )
            return

        # check capacity (reserved count includes payment_pending)
        if reserved_slots_count(self.lobby_key) >= 36:
            await interaction.response.send_message(
                "❌ All slots are full. Registration closed.", ephemeral=True
            )
            return

        reg.match_type = mt_key
        fees = MATCH_TYPES[mt_key].fee_options
        if len(fees) == 1:
            reg.entry_fee = fees[0]
            reg.status = "payment_pending"
            # reserve slot by changing status and start payment flow
            await interaction.response.defer(ephemeral=True)
            await send_payment_qr_and_start_timeout(interaction.user, reg, guild)
            self.stop()
            return

        # multiple fee choices: show fee view
        fee_view = FeeView(self.reg_id, self.lobby_key, mt_key)
        embed = discord.Embed(
            title="💰 Choose Entry Fee",
            description="Select your entry fee for this lobby.",
            color=discord.Color.gold(),
        )
        await interaction.response.send_message(
            embed=embed, view=fee_view, ephemeral=True
        )
        self.stop()

    @discord.ui.button(label="Special Live", style=discord.ButtonStyle.danger)
    async def special_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.handle_choice(interaction, "special")

    @discord.ui.button(label="Mini B²B³", style=discord.ButtonStyle.secondary)
    async def mini_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.handle_choice(interaction, "mini")

    @discord.ui.button(label="Mega B²B⁶", style=discord.ButtonStyle.success)
    async def mega_button(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ):
        await self.handle_choice(interaction, "mega")


class FeeView(discord.ui.View):
    def __init__(self, reg_id: int, lobby_key: str, mt_key: str):
        super().__init__(timeout=300)
        self.reg_id = reg_id
        self.lobby_key = lobby_key
        self.mt_key = mt_key
        for fee in MATCH_TYPES[mt_key].fee_options:
            self.add_item(FeeButton(label=f"₹{fee}", fee_value=fee, parent=self))


class FeeButton(discord.ui.Button):
    def __init__(self, label: str, fee_value: int, parent: FeeView):
        super().__init__(label=label, style=discord.ButtonStyle.primary)
        self.fee_value = fee_value
        self.parent_view = parent

    async def callback(self, interaction: discord.Interaction):
        guild = interaction.guild or bot.get_guild(GUILD_ID)
        if guild is None:
            await interaction.response.send_message(
                "Internal error: guild not found.", ephemeral=True
            )
            return

        reg = REG_BY_ID.get(self.parent_view.reg_id)
        if reg is None or reg.status not in {"pending", "payment_pending"}:
            await interaction.response.send_message(
                "Registration not found or already processed.", ephemeral=True
            )
            return

        reg.entry_fee = self.fee_value
        reg.status = "payment_pending"
        await interaction.response.defer(ephemeral=True)
        await send_payment_qr_and_start_timeout(interaction.user, reg, guild)
        self.parent_view.stop()


async def start_dm_flow(
    leader: discord.Member, reg: Registration, guild: discord.Guild
) -> bool:
    cfg = LOBBIES[reg.lobby_key]
    embed = discord.Embed(
        title=f"🎮 SS ESPORTS {cfg.label} SCRIMS",
        description=(
            lobby_description(cfg.label)
            + "\n\n✅ **Choose your lobby type to continue.**"
        ),
        color=discord.Color.purple(),
    )
    embed.set_footer(text="SS Esports • Paid Scrims System")
    view = MatchTypeView(reg.reg_id, reg.lobby_key)
    try:
        await leader.send(embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        return False

    await audit_log(
        guild,
        "📩 DM Sent Automatically",
        f"Team: **{reg.team_name}**\nLeader: <@{leader.id}>",
        discord.Color.blue(),
    )
    return True


async def send_payment_qr_and_start_timeout(
    user: discord.abc.User, reg: Registration, guild: discord.Guild
):
    # send payment DM with QR (attachment preferred), start auto-cancel timer
    warn = (
        "⚠ **STRICT PAYMENT RULES**\n\n"
        f"• Lobby: **{LOBBIES[reg.lobby_key].label} — {MATCH_TYPES[reg.match_type].display_name}**\n"
        f"• Entry Fee: **₹{reg.entry_fee}**\n\n"
        "Payment must be completed immediately using the QR below.\n"
        "**Fake / delayed payments = permanent blacklist from SS Esports.**\n\n"
        "**After payment:**\n"
        "• Send your **payment success screenshot** to this DM\n"
        "• Type `paid` in the same message\n"
        "→ Bot will forward proof to staff for approval."
    )
    embed = discord.Embed(
        title="SS ESPORTS — Payment", description=warn, color=discord.Color.red()
    )
    embed.set_footer(text="SS Esports • Payment instructions")

    qr_file = await fetch_qr_file_from_url(GPay_QR_URL)
    try:
        if qr_file:
            embed.set_image(url="attachment://payment_qr.png")
            await user.send(file=qr_file, embed=embed)
        else:
            # fallback to remote url in embed image
            embed.set_image(url=GPay_QR_URL)
            await user.send(embed=embed)

        await user.send(
            "📸 After successful payment, send the **screenshot + `paid`** in this DM."
        )
    except (discord.Forbidden, discord.HTTPException):
        # couldn't DM; we should rollback registration
        REG_BY_ID.pop(reg.reg_id, None)
        REG_BY_MESSAGE.pop(reg.message_id, None)
        t = PENDING_EXPIRE_TASKS.pop(reg.reg_id, None)
        if t:
            t.cancel()
        return

    # start auto cancel timer
    task = asyncio.create_task(
        auto_cancel_payment_timeout(reg.reg_id, PAYMENT_TIMEOUT_MINUTES)
    )
    PENDING_PAYMENT_TASKS[reg.reg_id] = task

    # audit + update boards (reserve slot)
    await audit_log(
        guild,
        "💳 Payment Pending",
        f"Team **{reg.team_name}** is payment_pending. Leader: <@{reg.leader_id}>",
        discord.Color.orange(),
    )
    await update_slot_status(reg.lobby_key, guild, force=True)
    await update_liveboard(reg.lobby_key, guild, force=False)
    
class CmdHelpView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=120)
        self.page = 0

    def make_embed(self):
        data = CMDHELP_PAGES[self.page]
        embed = discord.Embed(
            title=data["title"],
            description=data["desc"],
            color=discord.Color.purple()
        )
        embed.set_footer(text=f"SS Esports • Command Guide | Page {self.page+1}/{len(CMDHELP_PAGES)}")
        return embed

    @discord.ui.button(label="⬅️ Back", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page > 0:
            self.page -= 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label="➡️ Next", style=discord.ButtonStyle.primary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.page < len(CMDHELP_PAGES) - 1:
            self.page += 1
        await interaction.response.edit_message(embed=self.make_embed(), view=self)

    
CMDHELP_PAGES = [
    {
        "title": "🔥 SS ESPORTS — COMMAND GUIDE",
        "desc": (
            "**Welcome to SS Esports Automation Bot**\n\n"
            "This help system explains:\n"
            "• All available commands\n"
            "• Who can use them\n"
            "• How the scrims system works\n\n"
            "**Navigation:**\n"
            "Use the ⬅️ / ➡️ buttons below."
        )
    },
    {
        "title": "📝 REGISTRATION SYSTEM",
        "desc": (
            "**Player Flow (Automatic):**\n"
            "• Players send squad message in lobby channel\n"
            "• Format validated automatically\n"
            "• Duplicate players blocked\n"
            "• Leader receives DM automatically\n\n"
            "**Required Format:**\n"
            "`Team Name : XYZ`\n"
            "`@Member1`\n"
            "`@Member2`\n"
            "`@Member3`\n"
            "`@Member4`"
        )
    },
    {
        "title": "💳 PAYMENT & SLOT SYSTEM",
        "desc": (
            "**Payment Flow:**\n"
            "• Leader selects match type in DM\n"
            "• Entry fee selection\n"
            "• QR code sent automatically\n"
            "• Slot reserved (payment pending)\n"
            "• Auto-cancel if timeout\n\n"
            "**Slot Logic:**\n"
            "• 12 slots per match type\n"
            "• 36 total per lobby\n"
            "• Live updates every 15 minutes"
        )
    },
    {
        "title": "🧑‍💼 ADMIN PREFIX COMMANDS",
        "desc": (
            "`$post_lobbies <lobby>`\n"
            "→ Opens registration manually\n\n"
            "`$markpaid <team>`\n"
            "→ Confirm payment & assign roles\n\n"
            "`$cancel_slot <team>`\n"
            "→ Cancel registration\n\n"
            "`$sync`\n"
            "→ Sync slash commands"
        )
    },
    {
        "title": "🚫 BLACKLIST SYSTEM",
        "desc": (
            "`$blacklist add <id|team> [reason]`\n"
            "→ Permanently block a leader\n\n"
            "`$blacklist remove <id|team>`\n"
            "→ Remove blacklist\n\n"
            "`$blacklist list`\n"
            "→ View all blacklisted users\n\n"
            "**Auto-blacklist:**\n"
            "• Triggered on fake / delayed payments"
        )
    },
    {
        "title": "🧾 INFO & HELP COMMANDS",
        "desc": (
            "`$info`\n"
            "→ Show information hub\n\n"
            "`$infoba <name>`\n"
            "→ Add info button\n\n"
            "`$infodb <name>`\n"
            "→ Remove info button\n\n"
            "`$reghelp`\n"
            "→ Manager guide"
        )
    },
    {
        "title": "⚔️ SLASH COMMANDS",
        "desc": (
            "`/idp`\n"
            "→ Send Room ID & Password via DM\n\n"
            "`/winner`\n"
            "→ Declare winners & schedule cleanup\n\n"
            "**Note:**\n"
            "Run `$sync` if slash commands don't appear."
        )
    },
    {
        "title": "🌐 AUTOMATION & DASHBOARD",
        "desc": (
            "**Automatic Systems:**\n"
            "• Lobby scheduler\n"
            "• Slot monitoring\n"
            "• Payment timeout\n"
            "• Role cleanup\n\n"
            "**Web Dashboard:**\n"
            "• View registrations\n"
            "• Blacklist monitoring\n"
            "• Mark paid from browser"
        )
    },
]

@bot.command(name="cmdhelp")
async def cmdhelp_cmd(ctx: commands.Context):
    view = CmdHelpView()
    await ctx.reply(
        embed=view.make_embed(),
        view=view,
        mention_author=False
    )

async def close_registration_automatically(lobby_key: str, guild: discord.Guild):
    cfg = LOBBIES[lobby_key]
    channel = guild.get_channel(cfg.lobby_channel_id)

    if not isinstance(channel, discord.TextChannel):
        return

    # Stop updater
    LOBBY_ACTIVE[lobby_key] = False

    total = len(registrations_for(lobby_key))

    if total > 0:
        desc = f"✅ **Registration Closed**\n\n" \
               f"📋 **Total Teams Registered:** `{total}`"
    else:
        desc = (
            "🚫 **Registration Closed**\n\n"
            "No teams registered.\n"
            "**Channel locked automatically.**"
        )

    embed = discord.Embed(
        title=f"🔒 {cfg.label} Registration Closed",
        description=desc,
        color=discord.Color.red()
    )

    msg = await channel.send(embed=embed)

    # Lock channel
    overwrites = channel.overwrites.copy()
    overwrites[guild.default_role] = discord.PermissionOverwrite(send_messages=False)
    await channel.edit(overwrites=overwrites)

    # Auto-delete summary after 1 hour
    await asyncio.sleep(3600)
    try:
        await msg.delete()
    except:
        pass


# -------------------------
# staff command: markpaid
# -------------------------
@bot.command(name="markpaid")
@commands.has_permissions(manage_guild=True)
async def markpaid_cmd(ctx: commands.Context, *, team_name: str):
    guild = ctx.guild
    if guild is None:
        await ctx.reply("Use this command in the server.")
        return

    reg = None
    for r in REG_BY_ID.values():
        if r.team_name.lower() == team_name.lower():
            reg = r
            break

    if reg is None:
        await ctx.reply("❌ Team not found.")
        return

    if reg.status == "confirmed":
        await ctx.reply("✅ This team is already marked as paid/confirmed.")
        return

    await confirm_registration(reg, guild, verifier_id=ctx.author.id, source="markpaid")
    await ctx.reply(f"✅ Team **{reg.team_name}** marked as PAID and confirmed.")


# -------------------------
# blacklist group
# -------------------------
@bot.group(name="blacklist", invoke_without_command=True)
@commands.has_permissions(manage_guild=True)
async def blacklist_group(ctx: commands.Context):
    await ctx.reply(
        "Usage: $blacklist add <leader_id|team_name> [reason] | "
        "$blacklist remove <leader_id|team_name> | $blacklist list"
    )


@blacklist_group.command(name="add")
@commands.has_permissions(manage_guild=True)
async def blacklist_add(
    ctx: commands.Context, identifier: str, *, reason: str = "Manual blacklist"
):
    guild = ctx.guild
    if guild is None:
        return

    try:
        lid = int(identifier)
        BLACKLIST[str(lid)] = {
            "leader_id": lid,
            "team": None,
            "reason": reason,
            "created_at": datetime.now(tz=IST).isoformat(),
        }
        save_state()
        await ctx.reply(f"Added leader `{lid}` to blacklist.")
        await audit_log(
            guild,
            "🚫 Blacklist Added",
            f"Leader <@{lid}> blacklisted by <@{ctx.author.id}>: {reason}",
            discord.Color.red(),
        )
        return
    except ValueError:
        found = None
        for reg in REG_BY_ID.values():
            if reg.team_name.lower() == identifier.lower():
                found = reg
                break

        if found:
            BLACKLIST[str(found.leader_id)] = {
                "leader_id": found.leader_id,
                "team": found.team_name,
                "reason": reason,
                "created_at": datetime.now(tz=IST).isoformat(),
            }
            save_state()
            await ctx.reply(
                f"Added team `{found.team_name}` leader <@{found.leader_id}> to blacklist."
            )
            await audit_log(
                guild,
                "🚫 Blacklist Added",
                f"Team {found.team_name} blacklisted by <@{ctx.author.id}>: {reason}",
                discord.Color.red(),
            )
        else:
            await ctx.reply("No team or leader found with that identifier.")


@blacklist_group.command(name="remove")
@commands.has_permissions(manage_guild=True)
async def blacklist_remove(ctx: commands.Context, identifier: str):
    try:
        lid = int(identifier)
        if str(lid) in BLACKLIST:
            BLACKLIST.pop(str(lid), None)
            save_state()
            await ctx.reply(f"Removed leader `{lid}` from blacklist.")
            return
    except ValueError:
        key_to_delete = None
        for k, v in BLACKLIST.items():
            if v.get("team") and v["team"].lower() == identifier.lower():
                key_to_delete = k
                break

        if key_to_delete:
            BLACKLIST.pop(key_to_delete, None)
            save_state()
            await ctx.reply("Removed entry from blacklist.")
            return

    await ctx.reply("No matching blacklist entry found.")


@blacklist_group.command(name="list")
@commands.has_permissions(manage_guild=True)
async def blacklist_list(ctx: commands.Context):
    if not BLACKLIST:
        await ctx.reply("Blacklist is empty.")
        return

    lines = []
    for k, v in BLACKLIST.items():
        lines.append(
            f"- <@{v['leader_id']}> — team: `{v.get('team')}` — "
            f"{v.get('reason')} ({v.get('created_at')})"
        )
    await ctx.reply("\n".join(lines))


# -------------------------
# other commands: post_lobbies, close_lobby, cancel_slot, reghelp, info, helpme
# -------------------------
@bot.command(name="post_lobbies")
@commands.has_permissions(manage_guild=True)
async def post_lobbies(ctx: commands.Context, lobby: str):
    lobby_key = lobby.strip().lower()
    if lobby_key not in LOBBIES:
        valid = ", ".join(LOBBIES.keys())
        await ctx.reply(f"❌ Invalid lobby key. Valid: `{valid}`")
        return

    guild = ctx.guild
    if guild is None:
        await ctx.reply("Use this command in the server.")
        return

    cfg = LOBBIES[lobby_key]
    channel = guild.get_channel(cfg.lobby_channel_id)
    if not isinstance(channel, discord.TextChannel):
        await ctx.reply("❌ Lobby channel not found.")
        return

    desc = lobby_description(cfg.label) + (
        "\n\n**🟢 Registration is open now.**\n"
        "**Send your squad in this format:**\n\n"
        "`Team Name :- <your team name>`\n"
        "`@Member1`\n"
        "`@Member2`\n"
        "`@Member3`\n"
        "`@Member4`"
    )
    embed = discord.Embed(
        title=f"🔥 Daily Paid Scrims — {cfg.label}",
        description=desc,
        color=discord.Color.blue(),
    )
    embed.set_footer(text="SS Esports • Manual Lobby Post")

    try:
        await channel.send(embed=embed)
    except Exception:
        await ctx.reply("❌ Could not post in lobby channel (missing permissions).")
        return

    LOBBY_ACTIVE[lobby_key] = True
    await set_active_lobby_permissions(guild, lobby_key)
    await update_slot_status(lobby_key, guild, force=True)
    await ctx.reply(f"✅ Registration panel posted for **{cfg.label}**.")


@bot.command(name="close_lobby")
@commands.has_permissions(manage_guild=True)
async def close_lobby(ctx: commands.Context, lobby: str):
    lobby_key = lobby.strip().lower()
    if lobby_key not in LOBBIES:
        valid = ", ".join(LOBBIES.keys())
        await ctx.reply(f"❌ Invalid lobby key. Valid: `{valid}`")
        return

    guild = ctx.guild
    if guild is None:
        await ctx.reply("Use this command in the server.")
        return

    LOBBY_ACTIVE[lobby_key] = False
    # disable sending in all lobbies, or specifically this one
    await set_active_lobby_permissions(guild, active_key=None)
    await ctx.reply(
        f"🔒 Lobby **{LOBBIES[lobby_key].label}** registration has been closed.\n"
        "15-minute slot status posts will stop for this lobby."
    )


@bot.command(name="cancel_slot")
@commands.has_permissions(manage_guild=True)
async def cancel_slot(ctx: commands.Context, *, team_name: str):
    guild = ctx.guild
    if guild is None:
        return

    reg = None
    for r in REG_BY_ID.values():
        if r.team_name.lower() == team_name.lower():
            reg = r
            break

    if reg is None:
        await ctx.reply("Team not found.")
        return

    t1 = PENDING_PAYMENT_TASKS.pop(reg.reg_id, None)
    if t1:
        try:
            t1.cancel()
        except Exception:
            pass

    REG_BY_ID.pop(reg.reg_id, None)
    REG_BY_MESSAGE.pop(reg.message_id, None)

    await update_slot_status(reg.lobby_key, ctx.guild, force=True)
    await update_liveboard(reg.lobby_key, ctx.guild, force=True)
    await ctx.reply(f"✅ Slot cancelled for team **{reg.team_name}**.")
    await audit_log(
        guild,
        "❌ Slot Cancelled",
        f"Lobby: **{LOBBIES[reg.lobby_key].label}**\n"
        f"Team: **{reg.team_name}**\n"
        f"Cancelled by: <@{ctx.author.id}>",
        discord.Color.red(),
    )


INFO_PAGES: Dict[str, str] = {}


class InfoView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        for name in INFO_PAGES.keys():
            self.add_item(InfoButton(name))


class InfoButton(discord.ui.Button):
    def __init__(self, name: str):
        # ✅ Discord hard limit: 80 chars for button labels
        safe_label = name[:80]

        super().__init__(
            label=safe_label,
            style=discord.ButtonStyle.primary,
            custom_id=f"info:{safe_label.lower().replace(' ', '_')}"
        )

    async def callback(self, interaction: discord.Interaction):
        content = INFO_PAGES.get(self.label)

        if not content:
            await interaction.response.send_message(
                "❌ No content found for this info panel.",
                ephemeral=True
            )
            return

        # ✅ Split content into 2000-char chunks (Discord limit)
        chunks = [
            content[i:i + 2000]
            for i in range(0, len(content), 2000)
        ]

        # ✅ Send first chunk
        embed = discord.Embed(
            title=f"ℹ️ {self.label}",
            description=chunks[0],
            color=discord.Color.blurple(),
        )
        embed.set_footer(text="SS Esports • Official Information")

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True
        )

        # ✅ Send remaining chunks as follow-ups
        for chunk in chunks[1:]:
            followup_embed = discord.Embed(
                description=chunk,
                color=discord.Color.blurple(),
            )
            await interaction.followup.send(
                embed=followup_embed,
                ephemeral=True
            )



@bot.command(name="infoba")
@commands.has_permissions(manage_guild=True)
async def info_add_button(ctx: commands.Context, *, button_name: str):
    await ctx.reply(
        f"✍️ Send the **details content** for button **{button_name}**.\n"
        "You have **60 seconds**.",
        mention_author=False,
    )

    def check(m: discord.Message):
        return m.author == ctx.author and m.channel == ctx.channel

    try:
        msg = await bot.wait_for("message", timeout=60, check=check)
    except asyncio.TimeoutError:
        await ctx.send("⏱️ Timed out. Info button not added.")
        return

    # ✅ Discord limits
    MAX_BUTTON_LABEL = 80
    MAX_INFO_CHARS = 2000

    # ✅ Safe processing
    safe_label = button_name.strip()[:MAX_BUTTON_LABEL]
    content = msg.content.strip()

    if len(content) > MAX_INFO_CHARS:
        content = content[:MAX_INFO_CHARS]

    # ✅ Save info
    INFO_PAGES[safe_label] = content

    await ctx.send(
        f"✅ Info button **{safe_label}** added successfully.\n"
        f"📏 Content length: `{len(content)}/{MAX_INFO_CHARS}` chars"
    )




@bot.command(name="infodb")
@commands.has_permissions(manage_guild=True)
async def info_delete_button(ctx: commands.Context, *, button_name: str):
    if button_name not in INFO_PAGES:
        await ctx.reply("❌ Button not found.")
        return

    del INFO_PAGES[button_name]
    await ctx.reply(f"🗑️ Info button **{button_name}** deleted.")


@bot.command(name="info")
async def info_cmd(ctx: commands.Context):
    if not INFO_PAGES:
        await ctx.reply("ℹ️ No information pages added yet.")
        return

    embed = discord.Embed(
        title="🔥 SS ESPORTS — INFORMATION HUB",
        description="Welcome to **SS Esports Official Bot**\n\nClick the buttons below to explore:",
        color=discord.Color.purple(),
    )
    embed.set_footer(text="Rockstar Development • Legendary Automation")
    await ctx.reply(embed=embed, view=InfoView(), mention_author=False)
    
@bot.command(name="sync")
@commands.has_permissions(manage_guild=True)
async def sync_cmd(ctx: commands.Context):
    guild = ctx.guild
    if guild is None:
        await ctx.reply("❌ Use this command inside a server.")
        return

    try:
        synced = await bot.tree.sync(guild=discord.Object(id=guild.id))
        await ctx.reply(
            f"✅ **Slash Commands Synced Successfully**\n"
            f"📡 Total Commands: `{len(synced)}`"
        )
    except Exception as e:
        await ctx.reply(f"❌ Sync failed:\n```{e}```")



@bot.command(name="reghelp")
@commands.has_permissions(manage_guild=True)
async def reghelp_cmd(ctx: commands.Context):
    embed = discord.Embed(
        title="📘 SS Esports • Scrims Manager Guide",
        description=(
            "**This bot fully automates Paid Scrims.**\n\n"
            "🔹 **REGISTRATION FLOW**\n"
            "• Players send squad message in lobby channel\n"
            "• Bot auto-validates format\n"
            "• Bot auto-DMs leader\n"
            "• Leader selects lobby & entry fee\n"
            "• Slot reserved; payment pending\n"
            "• Leader sends payment screenshot + `paid` in DM\n"
            "• Staff approves via ✅ or uses $markpaid\n\n"
            "🔹 **SLOT SYSTEM**\n"
            "• 12 slots each (Special / Mini / Mega)\n"
            "• Total 36 slots per lobby\n"
            "• Auto lock when slots full\n\n"
            "🔹 **PAYMENT SYSTEM**\n"
            "• Entry fee selected in DM\n"
            "• QR sent in DM\n"
            "• Screenshot + `paid` → forwarded to staff\n"
            "• Fake / delayed = blacklist\n\n"
            "🔹 **MATCH DAY**\n"
            "• Use /idp to send room ID & password\n"
            "• Only registered (paid) players receive IDP\n\n"
            "🔹 **RESULT SYSTEM**\n"
            "• Use /winner command\n"
            "• Auto role cleanup after 30 mins\n"
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="SS Esports • Powerful Scrims Automation")
    await ctx.reply(embed=embed, mention_author=False)


@bot.command(name="helpme")
async def helpme_cmd(ctx: commands.Context):
    """
    Detailed help for players + staff, with all core commands.
    """
    embed = discord.Embed(
        title="🧠 SS ESPORTS — Legendary Scrims Bot Help",
        color=discord.Color.dark_purple(),
        description=(
            "Welcome to the **SS Esports Official Scrims Bot**.\n\n"
            "This bot handles **registration, payments, slot management, IDP, results, and dashboard** "
            "for your daily paid scrims."
        ),
    )

    embed.add_field(
        name="👥 Player Flow",
        value=(
            "1️⃣ Watch for **Registration Panel** in your lobby channel (12 PM / 3 PM / 6 PM / 9 PM / 12 AM).\n"
            "2️⃣ Register in that channel using **exact format**:\n"
            "`Team Name :- <your team name>`\n"
            "`@Member1`\n`@Member2`\n`@Member3`\n`@Member4`\n"
            "3️⃣ Leader will receive a **DM from the bot**.\n"
            "4️⃣ In DM, select **Match Type** and **Entry Fee**.\n"
            "5️⃣ Pay using the **QR** sent by bot.\n"
            "6️⃣ After payment, send **screenshot + type `paid`** in the same DM.\n"
            "7️⃣ Bot forwards it to staff; once approved you are **confirmed**."
        ),
        inline=False,
    )

    embed.add_field(
        name="🛠 Staff Commands (Prefix `$`)",
        value=(
            "`$post_lobbies <key>` — Post registration panel and open that lobby.\n"
            "`$close_lobby <key>` — Close registration for that lobby.\n"
            "`$markpaid <team_name>` — Manually mark team paid.\n"
            "`$cancel_slot <team_name>` — Cancel a reserved slot.\n"
            "`$blacklist add <id|team> [reason]` — Add to blacklist.\n"
            "`$blacklist remove <id|team>` — Remove from blacklist.\n"
            "`$blacklist list` — Show all blacklisted leaders.\n"
            "`$infoba <button>` — Add info button content.\n"
            "`$infodb <button>` — Delete info button.\n"
            "`$info` — Show info hub with buttons.\n"
            "`$reghelp` — Manager-focused quick guide.\n"
        ),
        inline=False,
    )

    embed.add_field(
        name="📡 Slash Commands",
        value=(
            "`/winner` — Declare winners for a lobby + schedule cleanup.\n"
            "`/idp` — Send Room ID & Password via DM to all **confirmed** players.\n"
        ),
        inline=False,
    )

    embed.add_field(
        name="📊 Web Dashboard",
        value=(
            "A small **HTTP dashboard (FastAPI)** runs with the bot.\n"
            "• Shows live registrations & blacklist.\n"
            "• `/api/markpaid` form lets you mark a team paid via browser.\n"
            "Use a reverse proxy + auth in production."
        ),
        inline=False,
    )

    embed.set_footer(text="Rockstar Development • SS Esports • Hardcoded Legendary System")
    await ctx.reply(embed=embed, mention_author=False)
    
@bot.command(name="stopup")
@commands.has_permissions(manage_guild=True)
async def stop_updater_cmd(ctx: commands.Context, lobby: str):
    key = lobby.strip().lower()

    if key not in LOBBIES:
        await ctx.reply(
            f"❌ Invalid lobby key.\nValid: `{', '.join(LOBBIES.keys())}`"
        )
        return

    if not LOBBY_ACTIVE.get(key):
        await ctx.reply(
            f"ℹ️ Auto-updater already stopped for **{LOBBIES[key].label}**."
        )
        return

    LOBBY_ACTIVE[key] = False

    await ctx.reply(
        f"⛔ **Auto Slot Updater Stopped**\n"
        f"Lobby: **{LOBBIES[key].label}**\n"
        f"• 15-minute slot updates disabled\n"
        f"• Registration messages blocked (if not active)"
    )



# presence rotator
PRESENCE_TEXTS = [
    "🔥 Managing SS PAID SCRIMS",
    "⚔️ Powerful Management System",
    "🚀 Rockstar Development",
]


@tasks.loop(seconds=15)
async def presence_rotator():
    for text in PRESENCE_TEXTS:
        try:
            await bot.change_presence(
                activity=discord.Streaming(
                    name=text, url="https://twitch.tv/rockstardevelopment"
                ),
                status=discord.Status.dnd,
            )
        except Exception:
            pass
        await asyncio.sleep(15)


# -------------------------
# winner & idp commands
# -------------------------
LOBBY_CHOICES = [
    app_commands.Choice(name="12:00 PM", value="12pm"),
    app_commands.Choice(name="03:00 PM", value="03pm"),
    app_commands.Choice(name="06:00 PM", value="06pm"),
    app_commands.Choice(name="09:00 PM", value="09pm"),
    app_commands.Choice(name="12:00 AM", value="12am"),
]

MATCH_CHOICES = [
    app_commands.Choice(name="Special Live", value="special"),
    app_commands.Choice(name="Mini B²B³", value="mini"),
    app_commands.Choice(name="Mega B²B⁶", value="mega"),
]


@bot.tree.command(name="winner", description="Declare winners for a lobby and schedule cleanup.")
@app_commands.choices(lobby=LOBBY_CHOICES, match_type=MATCH_CHOICES)
async def winner_cmd(
    interaction: discord.Interaction,
    lobby: app_commands.Choice[str],
    match_type: app_commands.Choice[str],
    first_team: str,
    second_team: str,
    third_team: str,
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ Only management can use this command.", ephemeral=True
        )
        return

    cfg = LOBBIES[lobby.value]
    mt_cfg = MATCH_TYPES[match_type.value]
    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Use this in the server.", ephemeral=True
        )
        return

    idp_channel = guild.get_channel(cfg.idp_channel_id)
    if not isinstance(idp_channel, discord.TextChannel):
        await interaction.response.send_message("IDP channel not found.", ephemeral=True)
        return

    embed = discord.Embed(
        title=f"🏆 Official Result — {cfg.label} ({mt_cfg.display_name})",
        description=(
            f"🥇 **1st Place:** {first_team}\n"
            f"🥈 **2nd Place:** {second_team}\n"
            f"🥉 **3rd Place:** {third_team}\n\n"
            "Congratulations to all winners!\n"
            "Prize will be distributed as per SS Esports rules."
        ),
        color=discord.Color.gold(),
    )
    embed.set_footer(text="SS Esports • Official Result")

    try:
        await idp_channel.send(embed=embed)
    except Exception:
        pass

    await interaction.response.send_message(
        f"✅ Winners announced for **{cfg.label} — {mt_cfg.display_name}**.\n"
        "Lobby roles will be cleaned and IDP restricted in **30 minutes**.",
        ephemeral=True,
    )
    await audit_log(
        guild,
        "🏆 Winners Declared",
        f"Lobby: **{cfg.label}**\n"
        f"Match: **{mt_cfg.display_name}**\n"
        f"1st: {first_team}\n2nd: {second_team}\n3rd: {third_team}",
        discord.Color.gold(),
    )

    asyncio.create_task(cleanup_after_winner(lobby.value, match_type.value))


async def cleanup_after_winner(lobby_key: str, match_type: str):
    await asyncio.sleep(30 * 60)
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    cfg = LOBBIES[lobby_key]
    mt_cfg = MATCH_TYPES[match_type]
    role_name = f"{cfg.label} — {mt_cfg.display_name}"
    role = discord.utils.get(guild.roles, name=role_name)
    if role:
        for member in guild.members:
            if role in member.roles:
                try:
                    await member.remove_roles(
                        role, reason="Lobby completed (winner declared)."
                    )
                except Exception:
                    pass

    idp_channel = guild.get_channel(cfg.idp_channel_id)
    if isinstance(idp_channel, discord.TextChannel) and role:
        overwrites = idp_channel.overwrites.copy()
        if role in overwrites:
            del overwrites[role]
        try:
            await idp_channel.edit(
                overwrites=overwrites, reason="Lobby completed (winner declared)."
            )
        except Exception:
            pass

    await audit_log(
        guild,
        "🧹 Lobby Cleaned",
        f"Lobby: **{cfg.label}**\nMatch: **{mt_cfg.display_name}**\nRoles removed & IDP restricted.",
    )


@bot.tree.command(
    name="idp", description="Send room ID & password to all players of a lobby type."
)
@app_commands.choices(lobby=LOBBY_CHOICES, match_type=MATCH_CHOICES)
async def idp_cmd(
    interaction: discord.Interaction,
    lobby: app_commands.Choice[str],
    match_type: app_commands.Choice[str],
    room_id: str,
    password: str,
):
    if not interaction.user.guild_permissions.manage_guild:
        await interaction.response.send_message(
            "❌ Only management can send room ID & password.", ephemeral=True
        )
        return

    guild = interaction.guild
    if guild is None:
        await interaction.response.send_message(
            "Use this in the server.", ephemeral=True
        )
        return

    cfg = LOBBIES[lobby.value]
    mt_cfg = MATCH_TYPES[match_type.value]
    regs = registrations_for(lobby.value, match_type.value, ["confirmed"])
    if not regs:
        await interaction.response.send_message(
            "No confirmed squads for this lobby type.", ephemeral=True
        )
        return

    player_ids = {pid for reg in regs for pid in reg.player_ids}
    sent = 0
    for pid in player_ids:
        member = guild.get_member(pid)
        if member is None:
            continue
        try:
            await member.send(
                embed=discord.Embed(
                    title=f"Room Details — {cfg.label} ({mt_cfg.display_name})",
                    description=f"**ID:** `{room_id}`\n**Password:** `{password}`",
                    color=discord.Color.dark_blue(),
                )
            )
            sent += 1
        except Exception:
            continue

    await interaction.response.send_message(
        f"✅ Sent room ID/PW to **{sent} players** for **{cfg.label} — {mt_cfg.display_name}**.",
        ephemeral=True,
    )
    await audit_log(
        guild,
        "🔑 IDP Sent",
        f"Lobby: **{cfg.label}**\nMatch: **{mt_cfg.display_name}**\nPlayers reached: `{sent}`",
        discord.Color.blue(),
    )


# -------------------------
# scheduler
# -------------------------
@tasks.loop(minutes=1)
async def lobby_scheduler():
    await bot.wait_until_ready()
    now = datetime.now(tz=IST)
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    for key, cfg in LOBBIES.items():
        reg_hour = (cfg.match_time.hour - 1) % 24
        reg_minute = cfg.match_time.minute
        if now.hour == reg_hour and now.minute == reg_minute:
            channel = guild.get_channel(cfg.lobby_channel_id)
            if isinstance(channel, discord.TextChannel):
                desc = lobby_description(cfg.label) + (
                    "\n\n**🟢 Registration is open now.**\n"
                    "**Send your squad in this format:**\n\n"
                    "`Team Name :- <your team name>`\n"
                    "`@Member1`\n"
                    "`@Member2`\n"
                    "`@Member3`\n"
                    "`@Member4`"
                )
                embed = discord.Embed(
                    title=f"🔥 Daily Paid Scrims — {cfg.label}",
                    description=desc,
                    color=discord.Color.blue(),
                )
                embed.set_footer(
                    text="SS Esports • Registration opens 1 hour before match time"
                )
                try:
                    await channel.send("<@&1407623189848133724>")
                    await channel.send(embed=embed)
                except Exception:
                    pass

                LOBBY_ACTIVE[key] = True
                await set_active_lobby_permissions(guild, key)
                await update_slot_status(key, guild, force=True)
                
                asyncio.create_task(auto_close_after_time(key))

            

async def auto_close_after_time(lobby_key: str):
    """
    Auto-close registration exactly at match time.
    Stops slot updater, locks the channel, and posts final status.
    """
    cfg = LOBBIES[lobby_key]
    guild = bot.get_guild(GUILD_ID)
    if guild is None:
        return

    # sleep until match time
    now = datetime.now(tz=IST)
    match_dt = datetime.combine(now.date(), cfg.match_time, tzinfo=IST)

    if match_dt <= now:
        match_dt += timedelta(days=1)

    await asyncio.sleep((match_dt - now).total_seconds())

    # stop updater
    LOBBY_ACTIVE[lobby_key] = False

    channel = guild.get_channel(cfg.lobby_channel_id)
    if not isinstance(channel, discord.TextChannel):
        return

    # lock channel
    overwrites = channel.overwrites.copy()
    ow = overwrites.get(guild.default_role, discord.PermissionOverwrite())
    ow.send_messages = False
    overwrites[guild.default_role] = ow

    try:
        await channel.edit(overwrites=overwrites, reason="Registration closed")
    except discord.Forbidden:
        pass

    total = total_confirmed_slots(lobby_key)

    embed = discord.Embed(
        title="🔒 Registration Closed",
        description=(
            f"**Lobby:** {cfg.label}\n"
            f"**Total Registered:** `{total}`\n\n"
            + ("✅ Matching will begin shortly." if total else "❌ No squads registered.")
        ),
        color=discord.Color.red(),
    )
    embed.set_footer(text="SS Esports • Registration Window Closed")

    try:
        msg = await channel.send(embed=embed)
        await msg.delete(delay=3600)  # auto-delete after 1 hour
    except Exception:
        pass



# -------------------------
# on_ready
# -------------------------
@bot.event
async def on_ready():
    load_state()
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")

    if not lobby_scheduler.is_running():
        lobby_scheduler.start()
    if not presence_rotator.is_running():
        presence_rotator.start()
    if not slot_status_broadcaster.is_running():
        slot_status_broadcaster.start()

    guild = bot.get_guild(GUILD_ID)
    if guild:
        await init_idp_locks(guild)
        await set_active_lobby_permissions(guild, active_key=None)
        for key in LOBBIES.keys():
            await update_slot_status(key, guild, force=True)
            await update_liveboard(key, guild, force=True)
        await audit_log(
            guild,
            "✅ Bot Online",
            "SS Esports Scrims Bot has started successfully.",
            discord.Color.green(),
        )


# -------------------------
# Web dashboard (FastAPI)
# -------------------------
app = FastAPI()


def start_dashboard_thread():
    # run uvicorn in a thread to avoid blocking the bot
    def _run():
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=int(os.getenv("DASHBOARD_PORT", "8000")),
            log_level="info",
        )

    t = threading.Thread(target=_run, daemon=True)
    t.start()


@app.get("/", response_class=HTMLResponse)
async def dashboard_index():
    # ----- REGISTRATION TABLE -----
    reg_rows = []
    for r in REG_BY_ID.values():
        status_color = {
            "pending": "#facc15",          # yellow
            "payment_pending": "#fb923c",  # orange
            "confirmed": "#22c55e",        # green
        }.get(r.status, "#64748b")

        reg_rows.append(
            f"""
            <tr>
              <td>{r.reg_id}</td>
              <td>{r.team_name}</td>
              <td>{LOBBIES[r.lobby_key].label}</td>
              <td>{MATCH_TYPES[r.match_type].display_name if r.match_type else "-"}</td>
              <td>{f"₹{r.entry_fee}" if r.entry_fee else "-"}</td>
              <td><span class="badge" style="background:{status_color}">{r.status}</span></td>
              <td>{", ".join(str(pid) for pid in r.player_ids)}</td>
            </tr>
            """
        )

    if not reg_rows:
        reg_rows.append(
            "<tr><td colspan='7' class='muted'>No registrations yet</td></tr>"
        )

    # ----- BLACKLIST TABLE -----
    bl_rows = []
    for v in BLACKLIST.values():
        bl_rows.append(
            f"""
            <tr>
              <td>{v['leader_id']}</td>
              <td>{v.get('team') or '-'}</td>
              <td>{v['reason']}</td>
              <td>{v['created_at']}</td>
            </tr>
            """
        )
    if not bl_rows:
        bl_rows.append(
            "<tr><td colspan='4' class='muted'>Blacklist empty</td></tr>"
        )

    # ----- LOBBY CARDS WITH PROGRESS BARS -----
    lobby_cards = []
    for key, cfg in LOBBIES.items():
        confirmed = total_confirmed_slots(key)
        reserved = reserved_slots_count(key)
        percent = int((confirmed / 36) * 100) if confirmed > 0 else 0
        percent = max(0, min(percent, 100))

        active = LOBBY_ACTIVE.get(key, False)
        status_label = "REGISTRATION OPEN" if active else "REGISTRATION CLOSED"
        status_color = "#22c55e" if active else "#ef4444"

        lobby_cards.append(
            f"""
            <div class="lobby-card">
              <div class="lobby-header">
                <div>
                  <div class="lobby-title">⚔️ {cfg.label}</div>
                  <div class="lobby-sub">Daily Paid Scrims</div>
                </div>
                <span class="lobby-status" style="border-color:{status_color};color:{status_color};">
                  {status_label}
                </span>
              </div>

              <div class="lobby-metrics">
                <div>
                  <div class="metric-label">Confirmed</div>
                  <div class="metric-value">{confirmed}/36</div>
                </div>
                <div>
                  <div class="metric-label">Reserved</div>
                  <div class="metric-value">{reserved}/36</div>
                </div>
              </div>

              <div class="progress-shell">
                <div class="progress-bar" style="width:{percent}%;"></div>
              </div>
              <div class="progress-caption">{percent}% of lobby filled</div>

              <div class="lobby-actions">
                <form method="post" action="/api/close_lobby">
                  <input type="hidden" name="lobby_key" value="{key}">
                  <button class="btn-secondary" type="submit">Close Registration 🔒</button>
                </form>
                <form method="post" action="/api/stopup">
                  <input type="hidden" name="lobby_key" value="{key}">
                  <button class="btn-ghost" type="submit">Stop Updater ⏹</button>
                </form>
              </div>
            </div>
            """
        )

    # ----- TOP STATS -----
    total_regs = len(REG_BY_ID)
    confirmed_count = len([r for r in REG_BY_ID.values() if r.status == "confirmed"])
    payment_pending_count = len(
        [r for r in REG_BY_ID.values() if r.status == "payment_pending"]
    )
    blacklist_count = len(BLACKLIST)

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>SS Esports • Admin Dashboard</title>
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
:root {{
  --bg: #020617;
  --bg-elevated: #020617;
  --panel: #020617;
  --border-soft: #1e293b;
  --accent: #6366f1;
  --accent-soft: rgba(99,102,241,0.35);
  --text-main: #e5e7eb;
  --text-muted: #64748b;
}}

* {{
  box-sizing: border-box;
}}

body {{
  margin: 0;
  min-height: 100vh;
  font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
  background: radial-gradient(circle at top, #1e293b 0, #020617 55%);
  color: var(--text-main);
}}

.header {{
  padding: 16px 28px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  background: linear-gradient(90deg, #4c1d95, #7c3aed, #ec4899);
  box-shadow: 0 12px 40px rgba(15,23,42,0.8);
}}

.header-left {{
  display: flex;
  align-items: center;
  gap: 12px;
}}

.header-logo {{
  width: 36px;
  height: 36px;
  border-radius: 50%;
  background: radial-gradient(circle at 30% 20%, #facc15, #f97316 45%, #7c2d12 70%);
  box-shadow: 0 0 30px rgba(250,204,21,0.8);
}}

.header-title {{
  font-size: 20px;
  font-weight: 700;
}}

.header-sub {{
  font-size: 12px;
  color: #e5e7eb;
  opacity: .9;
}}

.header-pill {{
  padding: 8px 14px;
  border-radius: 999px;
  background: rgba(15,23,42,0.3);
  border: 1px solid rgba(248,250,252,0.25);
  font-size: 12px;
}}

.container {{
  padding: 22px 28px 32px;
}}

.grid-cards {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
  gap: 16px;
  margin-bottom: 26px;
}}

.card {{
  background: radial-gradient(circle at top left, #1d2333, #020617);
  border-radius: 18px;
  padding: 14px 16px 16px;
  box-shadow: 0 0 0 1px rgba(148,163,184,0.15), 0 18px 40px rgba(15,23,42,0.85);
  position: relative;
  overflow: hidden;
  transform: translateY(0);
  transition: transform .18s ease-out, box-shadow .18s ease-out, border-color .18s ease-out;
  border: 1px solid rgba(148,163,184,0.18);
}}

.card::after {{
  content: "";
  position: absolute;
  inset: -40%;
  background: radial-gradient(circle at top, rgba(96,165,250,0.12), transparent 60%);
  opacity: 0;
  transition: opacity .3s ease-out;
}}

.card:hover {{
  transform: translateY(-3px);
  box-shadow: 0 0 0 1px rgba(129,140,248,0.6), 0 24px 60px rgba(15,23,42,0.9);
  border-color: rgba(129,140,248,0.65);
}}

.card:hover::after {{
  opacity: 1;
}}

.card-label {{
  font-size: 13px;
  color: var(--text-muted);
  margin-bottom: 4px;
}}

.card-value {{
  font-size: 26px;
  font-weight: 700;
}}

.card-foot {{
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
}}

.section-title {{
  font-size: 15px;
  font-weight: 600;
  margin: 14px 0 10px;
  display: flex;
  align-items: center;
  gap: 8px;
  color: #a5b4fc;
}}

.section-title span.icon {{
  font-size: 18px;
}}

.layout-main {{
  display: grid;
  grid-template-columns: 1.1fr 1.2fr;
  gap: 20px;
}}

@media (max-width: 980px) {{
  .layout-main {{
    grid-template-columns: 1fr;
  }}
}}

.lobby-grid {{
  display: grid;
  grid-template-columns: 1fr;
  gap: 14px;
}}

.lobby-card {{
  background: radial-gradient(circle at top right, #111827, #020617);
  border-radius: 16px;
  padding: 14px 16px 16px;
  box-shadow: 0 0 0 1px rgba(30,64,175,0.5), 0 18px 36px rgba(15,23,42,0.85);
  position: relative;
  overflow: hidden;
}}

.lobby-card::before {{
  content: "";
  position: absolute;
  inset: -40%;
  background:
    radial-gradient(circle at 10% 0%, rgba(59,130,246,0.18), transparent 55%),
    radial-gradient(circle at 80% 0%, rgba(244,114,182,0.25), transparent 60%);
  opacity: 0.9;
  mix-blend-mode: screen;
  pointer-events: none;
}}

.lobby-header {{
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 8px;
  position: relative;
  z-index: 1;
}}

.lobby-title {{
  font-weight: 600;
  font-size: 15px;
}}

.lobby-sub {{
  font-size: 12px;
  color: var(--text-muted);
}}

.lobby-status {{
  font-size: 11px;
  padding: 5px 10px;
  border-radius: 999px;
  border: 1px solid;
  background: rgba(15,23,42,0.75);
  backdrop-filter: blur(8px);
}}

.lobby-metrics {{
  display: flex;
  gap: 16px;
  font-size: 12px;
  margin-bottom: 8px;
  position: relative;
  z-index: 1;
}}

.metric-label {{
  color: var(--text-muted);
  font-size: 11px;
}}

.metric-value {{
  font-weight: 600;
  font-size: 14px;
}}

.progress-shell {{
  position: relative;
  height: 8px;
  border-radius: 999px;
  background: rgba(15,23,42,0.85);
  border: 1px solid rgba(148,163,184,0.5);
  overflow: hidden;
  box-shadow: inset 0 0 6px rgba(15,23,42,0.9);
  z-index: 1;
}}

.progress-bar {{
  height: 100%;
  background: linear-gradient(90deg,#22c55e,#a3e635,#22c55e);
  box-shadow: 0 0 12px rgba(34,197,94,0.9);
  width: 0;
  animation: grow-bar 1.1s ease-out forwards;
}}

@keyframes grow-bar {{
  from {{ width: 0; }}
  to   {{ width: inherit; }}
}}

.progress-caption {{
  font-size: 11px;
  color: var(--text-muted);
  margin-top: 4px;
  position: relative;
  z-index: 1;
}}

.lobby-actions {{
  margin-top: 10px;
  display: flex;
  gap: 8px;
  position: relative;
  z-index: 1;
}}

button {{
  font-family: inherit;
}}

.btn-secondary {{
  padding: 7px 12px;
  border-radius: 9px;
  border: none;
  font-size: 12px;
  font-weight: 600;
  background: linear-gradient(135deg,#f97316,#ec4899);
  color: #f9fafb;
  cursor: pointer;
  box-shadow: 0 8px 18px rgba(248,113,113,0.55);
}}

.btn-ghost {{
  padding: 7px 12px;
  border-radius: 9px;
  border: 1px solid rgba(148,163,184,0.6);
  font-size: 12px;
  font-weight: 500;
  background: rgba(15,23,42,0.9);
  color: #e5e7eb;
  cursor: pointer;
}}

.btn-export {{
  padding: 7px 14px;
  border-radius: 999px;
  border: 1px solid rgba(129,140,248,0.8);
  background: rgba(15,23,42,0.95);
  color: #c7d2fe;
  font-size: 12px;
  font-weight: 600;
  cursor: pointer;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}}

.table-shell {{
  background: rgba(15,23,42,0.9);
  border-radius: 16px;
  box-shadow: 0 0 0 1px rgba(15,23,42,0.8), 0 20px 40px rgba(15,23,42,0.9);
  overflow: hidden;
}}

table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;
}}

th, td {{
  padding: 8px 10px;
  border-bottom: 1px solid #111827;
  text-align: left;
}}

th {{
  background: linear-gradient(90deg,#020617,#020617);
  color: #a5b4fc;
  font-weight: 600;
  font-size: 11px;
  position: sticky;
  top: 0;
  z-index: 1;
}}

tbody tr:hover {{
  background: rgba(17,24,39,0.7);
}}

.badge {{
  padding: 3px 8px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 600;
  color: #020617;
}}

.muted {{
  color: var(--text-muted);
  text-align: center;
  padding: 16px 10px;
}}

.footer {{
  margin-top: 20px;
  text-align: center;
  font-size: 11px;
  color: var(--text-muted);
}}

</style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="header-logo"></div>
    <div>
      <div class="header-title">SS ESPORTS • CONTROL ROOM</div>
      <div class="header-sub">Live registrations, slots, blacklist & payments — LAN internal dashboard</div>
    </div>
  </div>
  <div class="header-pill">
    Live Auto-Refresh • Last updated <span id="timestamp"></span>
  </div>
</div>

<div class="container">

  <div class="grid-cards">
    <div class="card">
      <div class="card-label">Total Registrations</div>
      <div class="card-value">{total_regs}</div>
      <div class="card-foot">All lobbies combined</div>
    </div>
    <div class="card">
      <div class="card-label">Confirmed Teams</div>
      <div class="card-value">{confirmed_count}</div>
      <div class="card-foot">Payment verified</div>
    </div>
    <div class="card">
      <div class="card-label">Pending Payments</div>
      <div class="card-value">{payment_pending_count}</div>
      <div class="card-foot">Awaiting proof / approval</div>
    </div>
    <div class="card">
      <div class="card-label">Blacklisted Leaders</div>
      <div class="card-value">{blacklist_count}</div>
      <div class="card-foot">Auto + manual blacklist</div>
    </div>
  </div>

  <div class="layout-main">
    <!-- LEFT: LOBBIES -->
    <div>
      <div class="section-title"><span class="icon">🎮</span>Live Lobby Status</div>
      <div class="lobby-grid">
        {''.join(lobby_cards)}
      </div>
    </div>

    <!-- RIGHT: REGISTRATIONS + BLACKLIST -->
    <div>
      <div class="section-title">
        <span class="icon">📜</span>Registrations
      </div>

      <form method="get" action="/api/export_csv">
        <button class="btn-export" type="submit">⬇ Export CSV</button>
      </form>

      <div class="table-shell" style="max-height: 260px; overflow:auto;">
        <table>
          <thead>
            <tr>
              <th>ID</th>
              <th>Team</th>
              <th>Lobby</th>
              <th>Type</th>
              <th>Fee</th>
              <th>Status</th>
              <th>Players (IDs)</th>
            </tr>
          </thead>
          <tbody>
            {''.join(reg_rows)}
          </tbody>
        </table>
      </div>

      <div class="section-title" style="margin-top:18px;">
        <span class="icon">🚫</span>Blacklist
      </div>

      <div class="table-shell" style="max-height: 150px; overflow:auto;">
        <table>
          <thead>
            <tr>
              <th>Leader ID</th>
              <th>Team</th>
              <th>Reason</th>
              <th>Created</th>
            </tr>
          </thead>
          <tbody>
            {''.join(bl_rows)}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="section-title" style="margin-top:26px;">
    <span class="icon">✅</span>Mark Team Paid (Quick Action)
  </div>
  <form method="post" action="/api/markpaid" style="display:flex;gap:10px;max-width:420px;">
    <input placeholder="Team Name" name="team_name" />
    <button class="btn-secondary" type="submit">Mark Paid</button>
  </form>

  <div class="footer">
    SS Esports • Internal LAN Dashboard • Rockstar Development Automation
  </div>
</div>

<script>
// Auto-refresh every 15 seconds
setInterval(function() {{
  window.location.reload();
}}, 15000);

// Simple timestamp
document.getElementById("timestamp").innerText = new Date().toLocaleTimeString();
</script>

</body>
</html>
"""
    return HTMLResponse(content=html)


@app.post("/api/markpaid")
async def api_markpaid(request: Request, background: BackgroundTasks):
    form = await request.form()
    team_name = form.get("team_name")
    if not team_name:
        return JSONResponse({"ok": False, "error": "team_name required"}, status_code=400)

    reg = None
    for r in REG_BY_ID.values():
        if r.team_name.lower() == team_name.lower():
            reg = r
            break

    if not reg:
        return JSONResponse({"ok": False, "error": "team not found"}, status_code=404)

    def _run_markpaid():
        coro = _markpaid_by_reg_id(reg.reg_id)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    background.add_task(_run_markpaid)
    return JSONResponse({"ok": True, "message": "markpaid scheduled"})


@app.post("/api/close_lobby")
async def api_close_lobby(request: Request, background: BackgroundTasks):
    form = await request.form()
    lobby_key = (form.get("lobby_key") or "").strip().lower()
    if lobby_key not in LOBBIES:
        return JSONResponse({"ok": False, "error": "invalid lobby_key"}, status_code=400)

    def _run_close():
        coro = _close_lobby_from_api(lobby_key)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    background.add_task(_run_close)
    return JSONResponse({"ok": True, "message": f"close_lobby scheduled for {lobby_key}"})


@app.post("/api/stopup")
async def api_stopup(request: Request, background: BackgroundTasks):
    form = await request.form()
    lobby_key = (form.get("lobby_key") or "").strip().lower()
    if lobby_key not in LOBBIES:
        return JSONResponse({"ok": False, "error": "invalid lobby_key"}, status_code=400)

    def _run_stop():
        coro = _stopup_from_api(lobby_key)
        asyncio.run_coroutine_threadsafe(coro, bot.loop)

    background.add_task(_run_stop)
    return JSONResponse({"ok": True, "message": f"stopup scheduled for {lobby_key}"})


@app.get("/api/export_csv")
async def api_export_csv():
    import csv
    import io

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["reg_id", "team_name", "lobby_key", "match_type", "entry_fee", "status", "leader_id", "player_ids"]
    )
    for r in REG_BY_ID.values():
        writer.writerow(
            [
                r.reg_id,
                r.team_name,
                r.lobby_key,
                r.match_type or "",
                r.entry_fee or "",
                r.status,
                r.leader_id,
                ",".join(str(pid) for pid in r.player_ids),
            ]
        )

    data = buf.getvalue()
    return Response(
        content=data,
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="ss_esports_registrations.csv"'},
    )


async def _markpaid_by_reg_id(reg_id: int):
    reg = REG_BY_ID.get(reg_id)
    if not reg:
        return

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    await confirm_registration(reg, guild, verifier_id=None, source="dashboard")


async def _close_lobby_from_api(lobby_key: str):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    LOBBY_ACTIVE[lobby_key] = False
    await set_active_lobby_permissions(guild, active_key=None)
    await audit_log(
        guild,
        "🔒 Lobby Closed (Dashboard)",
        f"Lobby **{LOBBIES[lobby_key].label}** closed from dashboard.",
        discord.Color.red(),
    )


async def _stopup_from_api(lobby_key: str):
    guild = bot.get_guild(GUILD_ID)
    if not guild:
        return

    LOBBY_ACTIVE[lobby_key] = False
    await audit_log(
        guild,
        "⏹ Slot Updater Stopped (Dashboard)",
        f"15-minute slot updater stopped for **{LOBBIES[lobby_key].label}** from dashboard.",
        discord.Color.orange(),
    )
# start dashboard thread when bot is starting
def ensure_dashboard_started():
    start_dashboard_thread()


# -------------------------
# run
# -------------------------
if __name__ == "__main__":
    if not BOT_TOKEN:
        print("ERROR: BOT_TOKEN not set in .env (see .env.example)")
        raise SystemExit(1)

    # start dashboard thread before bot event loop so it's available
    ensure_dashboard_started()
    bot.run(BOT_TOKEN)
