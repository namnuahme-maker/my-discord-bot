import asyncio
import io
import math
import os
import re
import sys
import json
import discord

if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)
        sys.stderr.reconfigure(encoding="utf-8", line_buffering=True)
    except Exception:
        pass
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone, time as dt_time
from urllib.parse import quote
from PIL import Image, ImageDraw

# Icons and emojis for embed buttons and headers
EMOJI = {
    "join": "✅",
    "tentative": "🤔",
    "leave": "❌",
    "note": "📝",
    "notify": "🔔",
    "edit": "✏️",
    "checkin": "📍",
    "confirm": "📢",
    "waitlist": "⏳",
    "count": "🔢",
    "location": "📍",
    "time": "⏰",
}

def _theme_color(info: dict) -> int:
    """Return embed theme color based on meetup status:
    - Emerald Green (0x2ECC71): Check-in mode (meetup time now).
    - Orange (0xE67E22): Max limit reached (full / waitlist).
    - Blurple (0x5865F2): Normal sign-up open (default).
    """
    if info.get("checkin_mode"):
        return 0x2ECC71
    max_limit = info.get("max_limit")
    if max_limit and max_limit > 0 and len(info.get("going", [])) >= max_limit:
        return 0xE67E22
    return 0x5865F2

def _progress_bar(current: int, total: int = None, length: int = 8) -> str:
    """Create sleek text progress bar (e.g. ▰▰▰▰▱▱▱▱ 3/5 (60%))"""
    if total and total > 0:
        ratio = max(0.0, min(1.0, current / total))
        filled = int(round(ratio * length))
        empty = length - filled
        bar = "▰" * filled + "▱" * empty
        status_suffix = " (Full)" if current >= total else f" ({int(round(ratio * 100))}%)"
        return f"{bar} {current}/{total}{status_suffix}"
    else:
        filled = max(0, min(length, current))
        empty = length - filled
        bar = "▰" * filled + "▱" * empty
        return f"{bar} {current}"

from keep_alive import keep_alive
try:
    from keep_alive import set_bot_status
except ImportError:
    def set_bot_status(connected: bool, message: str, bot_name: str = None):
        pass

# ==================== Timezone & Environment ====================

# Thailand time (UTC+7) for consistent schedule across cloud servers
THAI_TZ = timezone(timedelta(hours=7))

def now_thai() -> datetime:
    """Return current datetime in Thailand timezone (UTC+7)."""
    return datetime.now(THAI_TZ)

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(DATA_DIR, ".env")
HISTORY_FILE = os.path.join(DATA_DIR, "meetup_history.json")
MOVIE_CACHE_FILE = os.path.join(DATA_DIR, "movie_cache.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

def _load_dotenv_fallback(filepath: str):
    """Load .env file into os.environ without third-party library."""
    if not os.path.exists(filepath):
        return
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                if k and k not in os.environ:
                    os.environ[k] = v
    except Exception as e:
        print(f"Warning loading .env: {e}")

_load_dotenv_fallback(ENV_FILE)
TOKEN = os.environ.get("DISCORD_TOKEN", "")

# ==================== Data Management Functions ====================

def load_json(filepath, default=None):
    if default is None:
        default = []
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r', encoding='utf-8') as f:
                return json.load(f)
    except Exception:
        pass
    return default

def save_json(filepath, data):
    try:
        with open(filepath, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"Error saving {filepath}: {e}")

def load_config():
    data = load_json(CONFIG_FILE, {})
    return data if isinstance(data, dict) else {}

def save_config(config):
    save_json(CONFIG_FILE, config)

def load_movie_cache():
    return set(load_json(MOVIE_CACHE_FILE, []))

def save_movie_cache(cache_set):
    save_json(MOVIE_CACHE_FILE, list(cache_set))

# ==================== Time Helper & Meetup Embed Management ====================

def clean_topic_title(title: str) -> str:
    """Strip leading emojis and extra whitespace from meetup topic."""
    if not title:
        return "Untitled"
    cleaned = re.sub(r'^[📅🎬\s]+', '', title).strip()
    return cleaned if cleaned else "Untitled"

def parse_thai_meetup_datetime(time_str: str, ref_dt: datetime = None):
    """
    Parse meetup time string (e.g. 'today 20:00', 'tomorrow 14:30', '28th 11:30')
    into datetime (THAI_TZ) for auto-reminder scheduling.
    """
    if not time_str:
        return None
    if any(k in time_str.lower() for k in ("tbd", "tba", "unknown", "not set", "not sure")):
        return None

    if ref_dt is None:
        ref_dt = now_thai()

    text = time_str.strip()
    target_date = ref_dt.date()
    explicit_date = False

    # 1. Match date DD/MM/YYYY or DD/MM
    date_slash = re.search(r'(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?', text)
    if date_slash:
        day_val = int(date_slash.group(1))
        month_val = int(date_slash.group(2))
        year_val = ref_dt.year
        if date_slash.group(3):
            y_raw = int(date_slash.group(3))
            if y_raw >= 2500:
                year_val = y_raw - 543
            elif y_raw < 100:
                year_val = 2000 + y_raw
            else:
                year_val = y_raw
        try:
            target_date = ref_dt.replace(year=year_val, month=month_val, day=day_val).date()
            explicit_date = True
        except ValueError:
            pass
        text_for_time = text[:date_slash.start()] + " " + text[date_slash.end():]
    else:
        text_for_time = text
        if "tomorrow" in text.lower():
            target_date = ref_dt.date() + timedelta(days=1)
            explicit_date = True
        elif "day after tomorrow" in text.lower():
            target_date = ref_dt.date() + timedelta(days=2)
            explicit_date = True
        elif "today" in text.lower() or "tonight" in text.lower():
            target_date = ref_dt.date()
            explicit_date = True
        else:
            day_match = re.search(r'(?:day|date|at|on)?\s*(\d{1,2})', text, re.IGNORECASE)
            if day_match:
                day_num = int(day_match.group(1))
                year_val = ref_dt.year
                month_val = ref_dt.month
                try:
                    candidate = ref_dt.replace(year=year_val, month=month_val, day=day_num).date()
                    if candidate < ref_dt.date():
                        if month_val == 12:
                            candidate = ref_dt.replace(year=year_val + 1, month=1, day=day_num).date()
                        else:
                            candidate = ref_dt.replace(year=year_val, month=month_val + 1, day=day_num).date()
                    target_date = candidate
                    explicit_date = True
                except ValueError:
                    pass
                text_for_time = text[:day_match.start()] + " " + text[day_match.end():]

    # 2. Match time HH:MM or HH.MM
    hour = None
    minute = 0
    time_match = re.search(r'(?:time|at)?\s*(\d{1,2})\s*[:.]\s*(\d{2})', text_for_time, re.IGNORECASE)
    if time_match:
        h_val = int(time_match.group(1))
        m_val = int(time_match.group(2))
        if 0 <= h_val <= 23 and 0 <= m_val <= 59:
            hour, minute = h_val, m_val
    else:
        # Match spoken numbers (Thai + English)
        thai_num_map = [
            ("eleven", "11"), ("ten", "10"), ("nine", "9"), ("eight", "8"),
            ("seven", "7"), ("six", "6"), ("five", "5"), ("four", "4"),
            ("three", "3"), ("two", "2"), ("one", "1"),
        ]
        norm_time_text = text_for_time
        for word, digit in thai_num_map:
            norm_time_text = norm_time_text.replace(word, digit)

        if "midnight" in norm_time_text.lower():
            hour, minute = 0, 0
        elif "noon" in norm_time_text.lower() or "midday" in norm_time_text.lower():
            hour, minute = 12, 0
        elif "1pm" in norm_time_text.lower():
            hour, minute = 13, 0
        else:
            afternoon_m = re.search(r'(\d{1,2})\s*pm', norm_time_text, re.IGNORECASE)
            night_m = None
            evening_m = None
            morning_m = re.search(r'(\d{1,2})\s*am', norm_time_text, re.IGNORECASE)
            if afternoon_m:
                hour = 12 + int(afternoon_m.group(1))
            elif night_m:
                hour = 18 + int(night_m.group(1))
            elif "7pm" in norm_time_text.lower():
                hour = 19
            elif evening_m:
                hour = 12 + int(evening_m.group(1))
            elif morning_m:
                hour = int(morning_m.group(1))
            if "half" in norm_time_text.lower() and hour is not None:
                minute = 30

    if hour is None or not (0 <= hour <= 23):
        return None

    result_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, tzinfo=THAI_TZ
    )
    # If no explicit date and time passed today, advance to tomorrow
    if not explicit_date and result_dt < ref_dt:
        result_dt += timedelta(days=1)

    return result_dt

def extract_mentions(text: str) -> list[str]:
    """Extract unique <@user_id> mentions preserving order."""
    if not text:
        return []
    found = re.findall(r'<@!?\d+>', text)
    unique = []
    for m in found:
        if m not in unique:
            unique.append(m)
    return unique

def parse_meetup_embed(embed: discord.Embed) -> dict:
    """Read all data from meetup embed card."""
    is_movie = "🎬" in (embed.title or "")
    topic = clean_topic_title(embed.title or "")
    location = ""
    time_val = ""
    pin_note = None
    going = []
    waitlist = []
    tentative = []
    checkins = []
    checkin_mode = False
    max_limit = None
    friend_notes = "*No notes yet*"
    created_by = "Unknown"

    for field in embed.fields:
        fname = field.name or ""
        fval = field.value or ""
        if "Location" in fname:
            location = fval.replace("```", "").replace("**", "").strip()
        elif "Time" in fname:
            time_val = fval.replace("```", "").replace("**", "").strip()
        elif "📌 Note" in fname:
            pin_note = re.sub(r'^>\s*', '', fval).strip()
        elif "People" in fname or "Count" in fname:
            m_limit = re.search(r'(\d+)\s*/\s*(\d+)', fval)
            if m_limit:
                max_limit = int(m_limit.group(2))
        elif "Going" in fname:
            going = extract_mentions(fval)
        elif "Waitlist" in fname or (fname.startswith("⏳") and ("Queue" in fval or "#" in fval)):
            waitlist = extract_mentions(fval)
        elif "Tentative" in fname:
            tentative = extract_mentions(fval)
        elif "Checked In" in fname:
            checkin_mode = True
        elif "Arrived" in fname:
            checkin_mode = True
            for m in extract_mentions(fval):
                if not any(c["mention"] == m for c in checkins):
                    checkins.append({"mention": m, "rest": ""})
        elif "Friend Notes" in fname:
            friend_notes = fval

    if embed.footer and embed.footer.text:
        created_by = embed.footer.text.replace("🎯 Created by", "").split("•")[0].strip()

    return {
        "is_movie": is_movie,
        "topic": topic,
        "location": location,
        "time_val": time_val,
        "pin_note": pin_note,
        "going": going,
        "waitlist": waitlist,
        "tentative": tentative,
        "checkins": checkins,
        "checkin_mode": checkin_mode,
        "max_limit": max_limit,
        "friend_notes": friend_notes,
        "created_by": created_by,
    }

def rebuild_meetup_embed(embed: discord.Embed, info: dict) -> discord.Embed:
    """Update and format fields in meetup card with clean visual hierarchy."""
    location = info.get("location", "-")
    time_val = info.get("time_val", "-")
    pin_note = info.get("pin_note")
    going = info.get("going", [])
    waitlist = info.get("waitlist", [])
    tentative = info.get("tentative", [])
    checkins = info.get("checkins", [])
    checkin_mode = info.get("checkin_mode", False)
    max_limit = info.get("max_limit")
    friend_notes = info.get("friend_notes") or "*No notes yet*"
    count = len(going)

    if checkin_mode:
        if count > 0 and len(checkins) >= count:
            status_line = "> 🟢 **Status:** Event active • All attendees arrived! 🎉"
        else:
            status_line = "> 🟢 **Status:** Event active • Check-in is open 📍"
    elif max_limit and max_limit > 0 and count >= max_limit:
        status_line = "> 🟠 **Status:** Capacity reached • New sign-ups join waitlist ⏳"
    else:
        status_line = "> 🔵 **Status:** Open for sign-ups"

    new_embed = discord.Embed(
        title=embed.title,
        description=status_line,
        color=_theme_color(info)
    )
    if embed.author and embed.author.name:
        new_embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url)
    if embed.thumbnail and embed.thumbnail.url:
        new_embed.set_thumbnail(url=embed.thumbnail.url)
    if embed.footer and embed.footer.text:
        new_embed.set_footer(text=embed.footer.text)

    # Core Event Metadata
    new_embed.add_field(name=f"{EMOJI['location']} Location", value=f"**{location}**", inline=True)
    new_embed.add_field(name=f"{EMOJI['time']} Time", value=f"**{time_val}**", inline=True)

    # Attendance & Progress
    prog = _progress_bar(count, max_limit, length=8)
    new_embed.add_field(name=f"{EMOJI['count']} Attendance", value=f"`{prog}`", inline=True)

    if checkin_mode:
        checkin_bar = _progress_bar(len(checkins), count, length=8) if count > 0 else "0/0"
        new_embed.add_field(name=f"{EMOJI['checkin']} Checked In", value=f"`{checkin_bar}`", inline=True)
    else:
        new_embed.add_field(name=f"{EMOJI['tentative']} Tentative", value=f"` {len(tentative)} `", inline=True)
        if max_limit and max_limit > 0:
            new_embed.add_field(name=f"{EMOJI['waitlist']} Waitlist", value=f"` {len(waitlist)} `", inline=True)

    if pin_note:
        new_embed.add_field(name="📌 Pinned Note", value=f"> {pin_note}", inline=False)

    # Confirmed participants
    if going:
        going_val = "\n".join([f"`{idx:02d}.` {m}" for idx, m in enumerate(going, 1)])
    else:
        going_val = "*No one signed up yet — be the first to join!*"
    new_embed.add_field(name="👥 Going", value=going_val[:1024], inline=False)

    # Show waitlist and tentative only in normal sign-up mode
    if not checkin_mode:
        if (max_limit and max_limit > 0) or waitlist:
            if waitlist:
                waitlist_val = "\n".join([f"`Queue #{idx}:` {m}" for idx, m in enumerate(waitlist, 1)])
            else:
                waitlist_val = "*Empty (No waitlist)*"
            new_embed.add_field(name="⏳ Waitlist (Auto-promotes on drop)", value=waitlist_val[:1024], inline=False)

        if tentative:
            tentative_val = "\n".join([f"• {m}" for m in tentative])
        else:
            tentative_val = "*None*"
        new_embed.add_field(name="🤔 Tentative", value=tentative_val[:1024], inline=False)

    # Arrived participants
    if checkin_mode or checkins:
        if checkins:
            checkin_lines = [
                f"`{idx:02d}.` {c['mention']}"
                for idx, c in enumerate(checkins, 1)
            ]
            if count > 0 and len(checkins) >= count:
                checkin_lines.append("🎉 **All attendees arrived!**")
            checkin_val = "\n".join(checkin_lines)
        else:
            checkin_val = "*No one checked in yet*"
        new_embed.add_field(name="📍 Arrived", value=checkin_val[:1024], inline=False)

    new_embed.add_field(name="📋 Friend Notes", value=friend_notes[:1024], inline=False)

    return new_embed

def upsert_meetup_history(
    embed: discord.Embed,
    guild_id,
    message_id: int = None,
    channel_id: int = None,
    custom_remind_at: datetime = None,
    reset_reminded: bool = False
):
    """Save or update meetup record in meetup_history.json (confirmed only)."""
    info = parse_meetup_embed(embed)
    topic = info["topic"]
    location = info["location"]
    time_val = info["time_val"]
    count = len(info["going"])
    created_by = info["created_by"]
    guild_str = str(guild_id) if guild_id else ""
    msg_str = str(message_id) if message_id else ""
    chan_str = str(channel_id) if channel_id else ""

    meetup_dt = parse_thai_meetup_datetime(time_val)
    meetup_iso = meetup_dt.isoformat() if meetup_dt else None

    if custom_remind_at is not None:
        remind_at_iso = custom_remind_at.isoformat()
    elif meetup_dt is not None:
        # Schedule auto reminder 30 minutes before meetup time
        auto_remind_dt = meetup_dt - timedelta(minutes=30)
        remind_at_iso = auto_remind_dt.isoformat()
    else:
        remind_at_iso = None

    history = load_json(HISTORY_FILE, [])
    found_entry = None

    # Match by message_id first; fallback to topic + location + guild_id
    for entry in reversed(history):
        if msg_str and entry.get("message_id") == msg_str:
            found_entry = entry
            break
    if not found_entry:
        for entry in reversed(history):
            if (
                clean_topic_title(entry.get("topic", "")) == topic
                and str(entry.get("guild_id", "")) == guild_str
                and (entry.get("location") == location or entry.get("time") == time_val)
            ):
                found_entry = entry
                break

    checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]

    if found_entry is not None:
        old_time = found_entry.get("time")
        found_entry["topic"] = topic
        found_entry["location"] = location
        found_entry["time"] = time_val
        found_entry["participants"] = count
        found_entry["tentative_count"] = len(info["tentative"])
        found_entry["waitlist_count"] = len(info["waitlist"])
        found_entry["checked_in_count"] = len(checked_in_mentions)
        found_entry["max_limit"] = info["max_limit"]
        found_entry["going_mentions"] = info["going"]
        found_entry["tentative_mentions"] = info["tentative"]
        found_entry["waitlist_mentions"] = info["waitlist"]
        found_entry["checked_in_mentions"] = checked_in_mentions
        if msg_str:
            found_entry["message_id"] = msg_str
        if chan_str:
            found_entry["channel_id"] = chan_str
        if meetup_iso:
            found_entry["meetup_iso"] = meetup_iso
        if custom_remind_at is not None or old_time != time_val:
            found_entry["remind_at_iso"] = remind_at_iso
            found_entry["reminded"] = False
        elif reset_reminded:
            found_entry["reminded"] = False
    else:
        now = now_thai()
        already_passed = False
        if remind_at_iso and custom_remind_at is None:
            try:
                if datetime.fromisoformat(remind_at_iso) <= now:
                    already_passed = True
            except Exception:
                pass

        history.append({
            "topic": topic,
            "location": location,
            "time": time_val,
            "participants": count,
            "tentative_count": len(info["tentative"]),
            "waitlist_count": len(info["waitlist"]),
            "checked_in_count": len(checked_in_mentions),
            "max_limit": info["max_limit"],
            "going_mentions": info["going"],
            "tentative_mentions": info["tentative"],
            "waitlist_mentions": info["waitlist"],
            "checked_in_mentions": checked_in_mentions,
            "created_by": created_by,
            "created_at": now.isoformat(),
            "guild_id": guild_str,
            "channel_id": chan_str,
            "message_id": msg_str,
            "meetup_iso": meetup_iso,
            "remind_at_iso": remind_at_iso,
            "reminded": already_passed,
        })

    history = history[-50:]
    save_json(HISTORY_FILE, history)

# ==================== Views & Modals for Meetups ====================

async def _open_notify_panel(interaction: discord.Interaction):
    """Open notification settings panel for meetup card."""
    embed = interaction.message.embeds[0]
    info = parse_meetup_embed(embed)

    history = load_json(HISTORY_FILE, [])
    msg_str = str(interaction.message.id)
    remind_status = "Not scheduled"
    for entry in reversed(history):
        if entry.get("message_id") == msg_str or (
            clean_topic_title(entry.get("topic", "")) == info["topic"]
            and str(entry.get("guild_id", "")) == str(interaction.guild_id)
        ):
            r_iso = entry.get("remind_at_iso")
            reminded = entry.get("reminded", False)
            if r_iso:
                try:
                    r_dt = datetime.fromisoformat(r_iso).astimezone(THAI_TZ)
                    state_txt = "✅ Sent" if reminded else "⏳ Scheduled"
                    remind_status = f"`{r_dt.strftime('%d/%m/%Y %H:%M')}` ({state_txt})"
                except Exception:
                    pass
            break

    panel_embed = discord.Embed(
        title=f"🔔 Notification Panel • {info['topic']}",
        description=(
            "> Select an action below to notify attendees without server-wide spam.\n\n"
            f"👥 **Going:** `{len(info['going'])}` attendees (Checked in: `{len(info['checkins'])}`)\n"
            f"🤔 **Tentative / Waitlist:** `{len(info['tentative']) + len(info['waitlist'])}` members\n"
            f"⏰ **Auto Reminder:** {remind_status}"
        ),
        color=0x5865F2
    )
    view = SmartNotifyView(interaction.message)
    await interaction.response.send_message(embed=panel_embed, view=view, ephemeral=True)


async def _open_update_options(interaction: discord.Interaction):
    """Open options for '✏️ Update' button to toggle check-in mode or edit details."""
    embed = interaction.message.embeds[0]
    info = parse_meetup_embed(embed)
    mode_str = "📍 Check-in Mode (Time now)" if info["checkin_mode"] else "📝 Normal Sign-up Mode"
    panel_embed = discord.Embed(
        title=f"✏️ Manage Meetup • {info['topic']}",
        description=(
            f"> Current Status: **{mode_str}**\n\n"
            "• **📍 Time now (Open Check-in):** Switch to live arrival check-in mode.\n"
            "• **📝 Edit Details:** Update location, time, or participant limit."
        ),
        color=0x5865F2
    )
    view = UpdateMeetupOptionsView(interaction.message, is_checkin_mode=info["checkin_mode"])
    await interaction.response.send_message(embed=panel_embed, view=view, ephemeral=True)


class MeetupView(discord.ui.View):
    """Buttons for meetup card during normal sign-up mode:
    - Row 0: Attendance actions (Join / Tentative / Leave)
    - Row 1: Management tools (Add Note / Remind / Update)
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Join", style=discord.ButtonStyle.green, custom_id="join_meetup", row=0, emoji=EMOJI["join"])
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        user_mention = interaction.user.mention

        if user_mention in info["going"]:
            await interaction.response.send_message("❌ You already joined!", ephemeral=True)
            return
        if user_mention in info["waitlist"]:
            pos = info["waitlist"].index(user_mention) + 1
            await interaction.response.send_message(
                f"⏳ You are already in **Waitlist #{pos}** (auto-promotes if someone leaves).",
                ephemeral=True
            )
            return

        # Remove from tentative if present
        if user_mention in info["tentative"]:
            info["tentative"].remove(user_mention)

        max_limit = info["max_limit"]
        joined_waitlist = False
        if max_limit and max_limit > 0 and len(info["going"]) >= max_limit:
            info["waitlist"].append(user_mention)
            joined_waitlist = True
        else:
            info["going"].append(user_mention)

        new_embed = rebuild_meetup_embed(embed, info)
        await interaction.response.edit_message(embed=new_embed)

        upsert_meetup_history(
            new_embed,
            interaction.guild_id,
            message_id=interaction.message.id,
            channel_id=interaction.channel_id
        )

        if joined_waitlist:
            pos = len(info["waitlist"])
            await interaction.followup.send(
                f"⚠️ Full (`{max_limit}/{max_limit}`)!\n"
                f"Added to **⏳ Waitlist #{pos}**. Auto-promotes and pings if someone drops!",
                ephemeral=True
            )

    @discord.ui.button(label="Leave", style=discord.ButtonStyle.red, custom_id="leave_meetup", row=0, emoji=EMOJI["leave"])
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        user_mention = interaction.user.mention
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]

        if (
            user_mention not in info["going"]
            and user_mention not in info["waitlist"]
            and user_mention not in info["tentative"]
            and user_mention not in checked_in_mentions
        ):
            await interaction.response.send_message("❌ You have not joined this meetup.", ephemeral=True)
            return

        if user_mention in info["going"]:
            info["going"].remove(user_mention)
        if user_mention in info["waitlist"]:
            info["waitlist"].remove(user_mention)
        if user_mention in info["tentative"]:
            info["tentative"].remove(user_mention)
        info["checkins"] = [c for c in info.get("checkins", []) if c["mention"] != user_mention]

        promoted_users = []
        max_limit = info["max_limit"]
        while info["waitlist"] and (not max_limit or len(info["going"]) < max_limit):
            promoted = info["waitlist"].pop(0)
            info["going"].append(promoted)
            promoted_users.append(promoted)

        new_embed = rebuild_meetup_embed(embed, info)
        await interaction.response.edit_message(embed=new_embed)

        upsert_meetup_history(
            new_embed,
            interaction.guild_id,
            message_id=interaction.message.id,
            channel_id=interaction.channel_id
        )

        if promoted_users and interaction.channel:
            mentions_str = " ".join(promoted_users)
            await interaction.channel.send(
                f"🎉 {mentions_str} Slot opened! Promoted from **Waitlist** to **{info['topic']}**.",
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    @discord.ui.button(label="Tentative", style=discord.ButtonStyle.primary, custom_id="tentative_meetup", row=0, emoji=EMOJI["tentative"])
    async def tentative_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        user_mention = interaction.user.mention

        if user_mention in info["tentative"]:
            await interaction.response.send_message("🤔 You are already on the **Tentative** list.", ephemeral=True)
            return

        promoted_users = []
        if user_mention in info["going"]:
            info["going"].remove(user_mention)
            max_limit = info["max_limit"]
            while info["waitlist"] and (not max_limit or len(info["going"]) < max_limit):
                promoted = info["waitlist"].pop(0)
                info["going"].append(promoted)
                promoted_users.append(promoted)

        if user_mention in info["waitlist"]:
            info["waitlist"].remove(user_mention)

        info["checkins"] = [c for c in info.get("checkins", []) if c["mention"] != user_mention]
        info["tentative"].append(user_mention)

        new_embed = rebuild_meetup_embed(embed, info)
        await interaction.response.edit_message(embed=new_embed)

        upsert_meetup_history(
            new_embed,
            interaction.guild_id,
            message_id=interaction.message.id,
            channel_id=interaction.channel_id
        )

        if promoted_users and interaction.channel:
            mentions_str = " ".join(promoted_users)
            await interaction.channel.send(
                f"🎉 {mentions_str} Slot opened! Promoted from **Waitlist** to **{info['topic']}**.",
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    @discord.ui.button(label="Add Note", style=discord.ButtonStyle.secondary, custom_id="note_meetup", row=1, emoji=EMOJI["note"])
    async def note_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NoteModal())

    @discord.ui.button(label="Remind", style=discord.ButtonStyle.secondary, custom_id="notify_meetup", row=1, emoji=EMOJI["notify"])
    async def notify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_notify_panel(interaction)

    @discord.ui.button(label="Update", style=discord.ButtonStyle.secondary, custom_id="edit_meetup", row=1, emoji=EMOJI["edit"])
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_update_options(interaction)


class CheckinMeetupView(discord.ui.View):
    """
    Buttons for meetup card in check-in mode:
    - Row 0: Check-in button (📍 I'm Here!)
    - Row 1: Tools (🔔 Remind / ✏️ Update)
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="I'm Here!", style=discord.ButtonStyle.green, custom_id="checkin_meetup_btn", row=0, emoji=EMOJI["checkin"])
    async def checkin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        info["checkin_mode"] = True
        user_mention = interaction.user.mention
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]

        # If already checked in
        if user_mention in checked_in_mentions:
            not_arrived = [m for m in info["going"] if m not in checked_in_mentions]
            if not_arrived:
                await interaction.response.send_message(
                    f"📍 Already checked in. Arrived: **{len(checked_in_mentions)}/{len(info['going'])}**\n"
                    f"🏃‍♂️ Missing: **{len(not_arrived)}** — click below to ping friends!",
                    view=PingNotArrivedView(interaction.message),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "📍 You already checked in. Everyone is here! 🎉",
                    ephemeral=True
                )
            return

        # Auto-add to going upon arrival if not on list
        if user_mention in info["tentative"]:
            info["tentative"].remove(user_mention)
        if user_mention in info["waitlist"]:
            info["waitlist"].remove(user_mention)
        if user_mention not in info["going"]:
            info["going"].append(user_mention)

        info["checkins"].append({
            "mention": user_mention,
            "rest": ""
        })

        new_embed = rebuild_meetup_embed(embed, info)
        await interaction.response.edit_message(embed=new_embed)

        upsert_meetup_history(
            new_embed,
            interaction.guild_id,
            message_id=interaction.message.id,
            channel_id=interaction.channel_id
        )

        checked_in_mentions = [c["mention"] for c in info["checkins"]]
        not_arrived = [m for m in info["going"] if m not in checked_in_mentions]

        if not_arrived:
            await interaction.followup.send(
                f"📍 **Checked in!**\n"
                f"👥 Arrived: **{len(checked_in_mentions)}/{len(info['going'])}** • Missing: **{len(not_arrived)}**\n"
                f"💡 Click button below to ping missing friends!",
                view=PingNotArrivedView(interaction.message),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "📍 **Checked in!**\n"
                "🎉 **Everyone arrived!**",
                ephemeral=True
            )
            if len(info["going"]) > 1 and interaction.channel:
                await interaction.channel.send(
                    f"🎉 **Full squad arrived!** All **{len(info['going'])}** participants for **{info['topic']}** checked in at `{info['location']}`."
                )

    @discord.ui.button(label="Remind", style=discord.ButtonStyle.secondary, custom_id="checkin_notify_btn", row=1, emoji=EMOJI["notify"])
    async def notify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_notify_panel(interaction)

    @discord.ui.button(label="Update", style=discord.ButtonStyle.secondary, custom_id="checkin_edit_btn", row=1, emoji=EMOJI["edit"])
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_update_options(interaction)


class UpdateMeetupOptionsView(discord.ui.View):
    """Submenu when clicking '✏️ Update'"""
    def __init__(self, meetup_message: discord.Message, is_checkin_mode: bool = False):
        super().__init__(timeout=180)
        self.meetup_message = meetup_message
        self.is_checkin_mode = is_checkin_mode
        if is_checkin_mode:
            self.toggle_mode_btn.label = "🔄 Switch to Normal Sign-ups"
            self.toggle_mode_btn.style = discord.ButtonStyle.primary
        else:
            self.toggle_mode_btn.label = "📍 Time now (Open Check-in)"
            self.toggle_mode_btn.style = discord.ButtonStyle.success

    @discord.ui.button(label="📍 Time now (Open Check-in)", style=discord.ButtonStyle.success, row=0)
    async def toggle_mode_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)

        if not self.is_checkin_mode:
            info["checkin_mode"] = True
            new_embed = rebuild_meetup_embed(embed, info)
            await self.meetup_message.edit(embed=new_embed, view=CheckinMeetupView())
            upsert_meetup_history(
                new_embed,
                interaction.guild_id,
                message_id=self.meetup_message.id,
                channel_id=self.meetup_message.channel.id
            )
            await interaction.response.edit_message(
                content=(
                    "✅ **Switched to Check-in mode.**\n"
                    "Added **📍 I'm Here!** button. Normal sign-up buttons removed."
                ),
                embed=None,
                view=None
            )
        else:
            info["checkin_mode"] = False
            new_embed = rebuild_meetup_embed(embed, info)
            await self.meetup_message.edit(embed=new_embed, view=MeetupView())
            upsert_meetup_history(
                new_embed,
                interaction.guild_id,
                message_id=self.meetup_message.id,
                channel_id=self.meetup_message.channel.id
            )
            await interaction.response.edit_message(
                content="✅ **Switched back to Normal Sign-up mode.**",
                embed=None,
                view=None
            )

    @discord.ui.button(label="📝 Edit Location / Time / Limit", style=discord.ButtonStyle.secondary, row=1)
    async def open_edit_modal_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        max_str = str(info["max_limit"]) if info["max_limit"] else ""
        await interaction.response.send_modal(
            EditMeetupModal(
                info["location"],
                info["time_val"],
                max_str,
                is_draft=False,
                meetup_message=self.meetup_message
            )
        )


class PingNotArrivedView(discord.ui.View):
    """Quick button to ping friends who marked Going but have not checked in."""
    def __init__(self, meetup_message: discord.Message):
        super().__init__(timeout=180)
        self.meetup_message = meetup_message

    @discord.ui.button(label="🏃‍♂️ Ping Missing Friends!", style=discord.ButtonStyle.primary)
    async def ping_not_arrived(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]
        not_arrived = [m for m in info["going"] if m not in checked_in_mentions]

        if not not_arrived:
            await interaction.response.edit_message(
                content="🎉 **Everyone has already checked in!**",
                view=None
            )
            return

        mentions_str = " ".join(not_arrived)
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🏃‍♂️ Roll Call • {info['topic']}",
            description=(
                f"> 📍 **Location:** `{info['location']}`\n"
                f"> ⏰ **Time:** `{info['time_val']}`\n\n"
                f"✅ **Checked In ({len(checked_in_mentions)}/{len(info['going'])}):** {' '.join(checked_in_mentions) if checked_in_mentions else '*None yet*'}\n"
                f"🏃‍♂️ **Missing ({len(not_arrived)}):** {' '.join(not_arrived)}\n\n"
                f"🔗 [Open Meetup Card to Check In]({jump_url})"
            ),
            color=0xE67E22
        )
        await interaction.channel.send(
            content=f"📣 {interaction.user.mention} is waiting at **`{info['location']}`**! Hurry up: {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **Pinged missing friends.**",
            view=None
        )


async def _send_dm_to_mentions(client: discord.Client, mentions: list[str], embed: discord.Embed, content: str = None) -> tuple[int, int]:
    """Send DM alert to mention list (<@user_id>). Returns (success_count, total_count)."""
    sent_count = 0
    total = 0
    seen_ids = set()
    for m in mentions:
        m_id = re.search(r'\d+', m)
        if not m_id:
            continue
        uid = int(m_id.group(0))
        if uid in seen_ids:
            continue
        seen_ids.add(uid)
        total += 1
        try:
            user = client.get_user(uid) or await client.fetch_user(uid)
            if user:
                await user.send(content=content, embed=embed)
                sent_count += 1
        except Exception:
            pass
    return sent_count, total


class SmartNotifyView(discord.ui.View):
    """Menu to ping Going, ping Tentative, send DMs, or set auto-reminders."""
    def __init__(self, meetup_message: discord.Message):
        super().__init__(timeout=180)
        self.meetup_message = meetup_message

    @discord.ui.button(label="🎯 Ping Going", style=discord.ButtonStyle.green, row=0)
    async def ping_going(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        if not info["going"]:
            await interaction.response.send_message("❌ No one signed up as Going yet.", ephemeral=True)
            return

        mentions_str = " ".join(info["going"])
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🔔 Meetup Alert • {info['topic']}",
            description=(
                f"> 📍 **Location:** `{info['location']}`\n"
                f"> ⏰ **Time:** `{info['time_val']}`\n\n"
                f"🔗 [Open Meetup Card]({jump_url})"
            ),
            color=0x2ECC71
        )
        await interaction.channel.send(
            content=f"📣 **Meetup Alert for Going participants!** {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **Alert sent to Going participants.**",
            embed=None,
            view=None
        )

    @discord.ui.button(label="🤔 Ping Going + Tentative", style=discord.ButtonStyle.primary, row=0)
    async def ping_going_and_tentative(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        targets = []
        for m in info["going"] + info["tentative"] + info["waitlist"]:
            if m not in targets:
                targets.append(m)

        if not targets:
            await interaction.response.send_message("❌ No participants found in this meetup.", ephemeral=True)
            return

        mentions_str = " ".join(targets)
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🔔 Attendance Check • {info['topic']}",
            description=(
                f"> 📍 **Location:** `{info['location']}`\n"
                f"> ⏰ **Time:** `{info['time_val']}`\n\n"
                f"💡 If marked **Tentative**, please confirm your spot on the meetup card!\n"
                f"🔗 [Open Meetup Card]({jump_url})"
            ),
            color=0x5865F2
        )
        await interaction.channel.send(
            content=f"📣 **Meetup Alert!** {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **Alert sent to Going and Tentative participants.**",
            embed=None,
            view=None
        )

    @discord.ui.button(label="🏃‍♂️ Ping Missing Check-ins", style=discord.ButtonStyle.secondary, row=0)
    async def ping_not_checked_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]
        not_arrived = [m for m in info["going"] if m not in checked_in_mentions]

        if not info["going"]:
            await interaction.response.send_message("❌ No one signed up as Going.", ephemeral=True)
            return
        if not not_arrived:
            await interaction.response.send_message("🎉 All participants checked in!", ephemeral=True)
            return

        mentions_str = " ".join(not_arrived)
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🏃‍♂️ Arrival Check • {info['topic']}",
            description=(
                f"> 📍 **Location:** `{info['location']}`\n"
                f"> ⏰ **Time:** `{info['time_val']}`\n\n"
                f"✅ **Checked In:** `{len(checked_in_mentions)}/{len(info['going'])}`\n"
                f"🔗 [Open Meetup Card to Check In]({jump_url})"
            ),
            color=0xE67E22
        )
        await interaction.channel.send(
            content=f"📣 **Missing Check-ins!** Hurry up: {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **Pinged missing check-ins.**",
            embed=None,
            view=None
        )

    @discord.ui.button(label="💌 Send Direct Message (DM)", style=discord.ButtonStyle.success, row=1)
    async def send_dm_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]
        targets = [m for m in info["going"] if m not in checked_in_mentions] if info.get("checkin_mode") else info["going"]

        if not targets:
            msg_txt = "🎉 Everyone already checked in!" if info.get("checkin_mode") else "❌ No one signed up as Going yet."
            await interaction.response.send_message(msg_txt, ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"⏳ **Sending DM alert to `{len(targets)}` friends...**",
            embed=None,
            view=None
        )

        jump_url = self.meetup_message.jump_url
        guild_name = interaction.guild.name if interaction.guild else "Discord Server"
        dm_embed = discord.Embed(
            title=f"🔔 Meetup Alert: {info['topic']}",
            description=(
                f"🏠 **Server:** `{guild_name}`\n"
                f"📍 **Location:** `{info['location']}`\n"
                f"⏰ **Time:** `{info['time_val']}`\n"
                f"👤 **Sent by:** {interaction.user.mention}\n\n"
                f"🔗 [Open Meetup Card]({jump_url})"
            ),
            color=_theme_color(info)
        )
        dm_embed.set_footer(text="💬 Automated DM notification")

        sent_ok, total_cnt = await _send_dm_to_mentions(
            interaction.client,
            targets,
            dm_embed,
            content=f"💌 **Meetup alert: `{info['topic']}`**"
        )
        await interaction.edit_original_response(
            content=f"✅ **Sent DM to `{sent_ok}/{total_cnt}` friends.**"
        )

    @discord.ui.button(label="⏰ Set Auto Reminder", style=discord.ButtonStyle.secondary, row=1)
    async def set_auto_reminder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetReminderModal(self.meetup_message))

    @discord.ui.button(label="📢 Bump + Ping @everyone", style=discord.ButtonStyle.danger, row=2)
    async def ping_everyone_bump(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        target_view = CheckinMeetupView() if info.get("checkin_mode") else MeetupView()
        await interaction.response.edit_message(content="✅ **Bumped with @everyone.**", embed=None, view=None)
        try:
            new_msg = await interaction.channel.send(
                content="@everyone 🔔 **Meetup Alert!**",
                embed=embed,
                view=target_view,
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )
            # Update message_id in history for accurate future reminders
            old_id = str(self.meetup_message.id)
            history = load_json(HISTORY_FILE, [])
            for entry in history:
                if entry.get("message_id") == old_id:
                    entry["message_id"] = str(new_msg.id)
            save_json(HISTORY_FILE, history)

            await self.meetup_message.delete()
        except discord.errors.Forbidden:
            await interaction.followup.send(
                "❌ **Bot lacks permission to send or manage messages here.**\n"
                "Please grant `Send Messages` and `Manage Messages` to the bot.",
                ephemeral=True
            )
        except Exception as e:
            print(f"Error in ping_everyone_bump: {e}")


class SetReminderModal(discord.ui.Modal, title='⏰ Set Auto Reminder'):
    remind_input = discord.ui.TextInput(
        label='Reminder time (minutes from now, or HH:MM)',
        style=discord.TextStyle.short,
        placeholder='e.g. 30 (in 30 mins), or 19:30, or today 18:00',
        default='30',
        required=True,
        max_length=50,
    )

    def __init__(self, meetup_message: discord.Message):
        super().__init__()
        self.meetup_message = meetup_message

    async def on_submit(self, interaction: discord.Interaction):
        raw = self.remind_input.value.strip()
        now = now_thai()
        target_dt = None

        m_mins = re.match(r'^(?:in\s*)?(\d+)\s*(?:mins?|minutes?|m)?$', raw, re.IGNORECASE)
        if m_mins:
            mins = int(m_mins.group(1))
            if mins <= 0 or mins > 10080:
                await interaction.response.send_message("❌ Minutes must be between 1 and 10080 (7 days).", ephemeral=True)
                return
            target_dt = now + timedelta(minutes=mins)
        else:
            target_dt = parse_thai_meetup_datetime(raw, now)

        if not target_dt or target_dt <= now:
            await interaction.response.send_message(
                "❌ Cannot parse time or time in the past.\n"
                "💡 **Supported:** `30` (in 30 mins), `19:30`, `today 18:00`, `tomorrow 11:00`",
                ephemeral=True
            )
            return

        embed = self.meetup_message.embeds[0]
        upsert_meetup_history(
            embed,
            interaction.guild_id,
            message_id=self.meetup_message.id,
            channel_id=self.meetup_message.channel.id,
            custom_remind_at=target_dt,
            reset_reminded=True
        )

        await interaction.response.send_message(
            f"✅ **Auto reminder scheduled!**\n"
            f"⏰ Bot will ping participants at **`{target_dt.strftime('%d/%m/%Y %H:%M')}`**.",
            ephemeral=True
        )


class DraftMeetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Edit Draft", style=discord.ButtonStyle.secondary, custom_id="draft_edit_meetup", row=0, emoji=EMOJI["edit"])
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        max_str = str(info["max_limit"]) if info["max_limit"] else ""
        await interaction.response.send_modal(EditMeetupModal(info["location"], info["time_val"], max_str, is_draft=True))

    @discord.ui.button(label="Confirm & Post", style=discord.ButtonStyle.success, custom_id="draft_confirm_meetup", row=0, emoji=EMOJI["confirm"])
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)

        # Set theme color according to meetup status
        embed.color = _theme_color(info)

        # Calculate auto reminder (30 minutes before meetup)
        meetup_dt = parse_thai_meetup_datetime(info["time_val"])
        now = now_thai()
        auto_remind_note = ""
        if meetup_dt and (meetup_dt - timedelta(minutes=30)) > now:
            remind_dt = meetup_dt - timedelta(minutes=30)
            auto_remind_note = f"  •  🔔 Auto-remind {remind_dt.strftime('%H:%M')}"

        # Restore footer and author to normal
        footer_text = embed.footer.text.replace("Click confirm to post", "Click buttons below to join!") if embed.footer else ""
        embed.set_footer(text=footer_text + auto_remind_note)

        author_name = embed.author.name.replace("Draft", "").strip() or "🔔 Meetup"
        icon_url = embed.author.icon_url if embed.author else None
        embed.set_author(name=author_name, icon_url=icon_url)

        try:
            confirm_msg = "✅ **Meetup posted to channel!**"
            if auto_remind_note:
                confirm_msg += f"\n⏰ *(Auto-reminder scheduled 30 mins before meetup)*"
            await interaction.response.edit_message(content=confirm_msg, embed=None, view=None)

            sent_msg = await interaction.channel.send(
                content="@everyone 📢 **New Meetup!**",
                embed=embed,
                view=MeetupView(),
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )

            upsert_meetup_history(
                embed,
                interaction.guild_id,
                message_id=sent_msg.id,
                channel_id=sent_msg.channel.id
            )

            async def delete_after():
                await asyncio.sleep(5)
                try:
                    await interaction.delete_original_response()
                except Exception:
                    pass
            asyncio.create_task(delete_after())

        except discord.errors.Forbidden:
            await interaction.followup.send(
                "❌ **Bot lacks permission to send messages here.**\nPlease grant `Send Messages` to the bot.",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **Failed to send message:** {e}", ephemeral=True)
            print(f"Error in confirm_button: {e}")


class EditMeetupModal(discord.ui.Modal, title='✏️ Edit Meetup Details'):
    location = discord.ui.TextInput(
        label='Location',
        style=discord.TextStyle.short,
        placeholder='e.g. Dorm A, Regular Spot, Mall',
        required=True,
        max_length=100,
    )
    time_str = discord.ui.TextInput(
        label='Time (HH:MM for 30m auto-remind)',
        style=discord.TextStyle.short,
        placeholder='e.g. Today 20:00, Tomorrow 14:30',
        required=True,
        max_length=50,
    )
    max_participants = discord.ui.TextInput(
        label='Max People (leave blank for unlimited)',
        style=discord.TextStyle.short,
        placeholder='e.g. 4, 5, 10 (full goes to waitlist)',
        required=False,
        max_length=5,
    )

    def __init__(
        self,
        current_location: str,
        current_time: str,
        current_max: str = "",
        is_draft: bool = False,
        meetup_message: discord.Message = None
    ):
        super().__init__()
        self.location.default = current_location
        self.time_str.default = current_time
        self.max_participants.default = current_max
        self.is_draft = is_draft
        self.meetup_message = meetup_message

    async def on_submit(self, interaction: discord.Interaction):
        target_msg = self.meetup_message if self.meetup_message else interaction.message
        embed = target_msg.embeds[0]
        info = parse_meetup_embed(embed)
        info["location"] = self.location.value.strip()
        info["time_val"] = self.time_str.value.strip()

        max_raw = self.max_participants.value.strip()
        if max_raw:
            if not max_raw.isdigit() or int(max_raw) <= 0:
                await interaction.response.send_message("❌ Max people must be a positive integer like `4` or `5`.", ephemeral=True)
                return
            info["max_limit"] = int(max_raw)
        else:
            info["max_limit"] = None

        # Promote from waitlist if limit increased, or move to waitlist if decreased
        promoted_users = []
        max_limit = info["max_limit"]
        if max_limit is None:
            while info["waitlist"]:
                p = info["waitlist"].pop(0)
                info["going"].append(p)
                promoted_users.append(p)
        else:
            while info["waitlist"] and len(info["going"]) < max_limit:
                p = info["waitlist"].pop(0)
                info["going"].append(p)
                promoted_users.append(p)
            while len(info["going"]) > max_limit:
                overflow = info["going"].pop()
                info["waitlist"].insert(0, overflow)

        new_embed = rebuild_meetup_embed(embed, info)
        if self.meetup_message:
            await self.meetup_message.edit(embed=new_embed)
            await interaction.response.edit_message(
                content="✅ **Meetup details updated!**",
                embed=None,
                view=None
            )
        else:
            await interaction.response.edit_message(embed=new_embed)

        if not self.is_draft:
            upsert_meetup_history(
                new_embed,
                interaction.guild_id,
                message_id=target_msg.id,
                channel_id=target_msg.channel.id
            )
            if promoted_users and interaction.channel:
                mentions_str = " ".join(promoted_users)
                await interaction.channel.send(
                    f"🎉 {mentions_str} Seat limit expanded! Promoted from **Waitlist** to **{info['topic']}**.",
                    allowed_mentions=discord.AllowedMentions(users=True)
                )


class NoteModal(discord.ui.Modal, title='📝 Add Note'):
    note_text = discord.ui.TextInput(
        label='Your Note',
        style=discord.TextStyle.long,
        placeholder='e.g. 10 mins late, bringing 2 cars...',
        required=True,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        note_str = f"💬 **{interaction.user.display_name}:** {self.note_text.value}"

        current_notes = info["friend_notes"]
        if current_notes == "*No notes yet*" or not current_notes:
            new_notes = note_str
        else:
            new_notes = current_notes + f"\n{note_str}"

        # Prevent exceeding 1024 char Discord embed field limit
        if len(new_notes) > 1024:
            lines = new_notes.split("\n")
            while len("\n".join(lines)) > 1024 and len(lines) > 1:
                lines.pop(0)
            new_notes = "\n".join(lines)[:1024]

        info["friend_notes"] = new_notes
        new_embed = rebuild_meetup_embed(embed, info)
        await interaction.response.edit_message(embed=new_embed)

# ==================== Movie Selection Dropdown ====================

class MovieSelect(discord.ui.Select):
    def __init__(self, movies):
        self.movies_data = movies
        options = []
        for i, movie in enumerate(movies[:25]):  # Discord limit: 25 items
            showtimes_preview = ", ".join(movie['showtimes'][:3])
            options.append(discord.SelectOption(
                label=movie['name'][:100],
                description=f"Showtimes: {showtimes_preview}"[:100],
                value=str(i),
                emoji="🎬"
            ))
        super().__init__(placeholder="🎬 Select movie...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        movie_idx = int(self.values[0])
        movie = self.movies_data[movie_idx]

        view = ShowtimeSelectView(movie)
        embed = discord.Embed(
            title=f"🎬 {movie['name']}",
            description="Select desired showtime:",
            color=0xE50914
        )
        if movie['image']:
            embed.set_thumbnail(url=movie['image'])

        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ All Showtimes", value=showtimes_fmt[:1024], inline=False)

        await interaction.response.edit_message(embed=embed, view=view)

class MovieSelectView(discord.ui.View):
    def __init__(self, movies):
        super().__init__(timeout=120)
        self.add_item(MovieSelect(movies))

class MovieDateModal(discord.ui.Modal, title='📅 Movie Date & Participant Limit'):
    date_str = discord.ui.TextInput(
        label='Date',
        style=discord.TextStyle.short,
        placeholder='e.g. Today, Tomorrow, Saturday',
        default='Today',
        required=True,
        max_length=50,
    )
    max_participants = discord.ui.TextInput(
        label='Max People (leave blank for unlimited)',
        style=discord.TextStyle.short,
        placeholder='e.g. 4 (one car), 6',
        required=False,
        max_length=5,
    )

    def __init__(self, movie, selected_time):
        super().__init__()
        self.movie = movie
        self.selected_time = selected_time

    async def on_submit(self, interaction: discord.Interaction):
        max_raw = self.max_participants.value.strip()
        max_limit = None
        if max_raw:
            if not max_raw.isdigit() or int(max_raw) <= 0:
                await interaction.response.send_message("❌ Max people must be a positive integer like `4` or `6`.", ephemeral=True)
                return
            max_limit = int(max_raw)

        if self.selected_time == "Time TBD":
            final_time = f"{self.date_str.value} (Time TBD)"
        else:
            final_time = f"{self.date_str.value} at {self.selected_time}"

        base_embed = discord.Embed(
            title=f"📅  🎬 {self.movie['name']}",
            description="> 🔵 **Status:** Movie Meetup Draft (Review before posting)",
            color=0x5865F2
        )
        if self.movie['image']:
            base_embed.set_thumbnail(url=self.movie['image'])
        base_embed.set_footer(text=f"🎯 Created by {interaction.user.display_name} • Click confirm to publish")
        base_embed.set_author(name="🔔 Movie Meetup Draft", icon_url=interaction.user.display_avatar.url)

        info = {
            "location": "Major Cineplex Big C Lopburi",
            "time_val": final_time,
            "pin_note": None,
            "going": [],
            "waitlist": [],
            "tentative": [],
            "max_limit": max_limit,
            "friend_notes": "*No notes yet*",
        }
        embed = rebuild_meetup_embed(base_embed, info)
        view = DraftMeetupView()

        await interaction.response.edit_message(
            content="**Meetup Draft** (only you see this)\nWhen ready, click **📢 Confirm & Post** to publish to channel.",
            embed=embed,
            view=view
        )

class ShowtimeSelect(discord.ui.Select):
    def __init__(self, movie):
        self.movie = movie
        options = []
        # Limit to 24 showtimes to reserve 1 slot for 'Not sure yet' within Discord 25 options limit
        for t in movie['showtimes'][:24]:
            options.append(discord.SelectOption(
                label=f"🕐 {t}",
                value=t,
                emoji="🎟️"
            ))

        options.append(discord.SelectOption(
            label="🤔 Not sure yet",
            description="Schedule first, decide time later",
            value="Time TBD",
            emoji="⏳"
        ))

        super().__init__(placeholder="🕐 Select showtime...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_time = self.values[0]
        await interaction.response.send_modal(MovieDateModal(self.movie, selected_time))

def get_trailer_url(movie_name: str) -> str:
    """Generate YouTube trailer search link."""
    query = quote(f"{movie_name} Official Trailer")
    return f"https://www.youtube.com/results?search_query={query}"

class ShowtimeSelectView(discord.ui.View):
    def __init__(self, movie):
        super().__init__(timeout=120)
        self.add_item(ShowtimeSelect(movie))
        self.add_item(discord.ui.Button(
            label="▶️ Movie Trailer (YouTube)",
            style=discord.ButtonStyle.link,
            url=get_trailer_url(movie['name'])
        ))

# ==================== Bot Class & Background Tasks ====================

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.movie_cache = load_movie_cache()
        self.movie_channel_id = None
        self.weather_channel_id = None
        self.weather_lat = None
        self.weather_lon = None
        self.last_weather_alert_time = None

        config = load_config()
        channel_id = config.get("movie_channel_id")
        if channel_id:
            self.movie_channel_id = int(channel_id)

        w_channel_id = config.get("weather_channel_id")
        if w_channel_id:
            self.weather_channel_id = int(w_channel_id)
        self.weather_lat = config.get("weather_lat")
        self.weather_lon = config.get("weather_lon")

    async def setup_hook(self):
        self.add_view(MeetupView())
        self.add_view(CheckinMeetupView())
        self.check_movies.start()
        self.check_weather.start()
        self.check_reminders.start()
        self.morning_briefing.start()

        async def _sync_commands():
            try:
                synced = await self.tree.sync()
                print(f"✅ Synced Slash Commands ({len(synced)} commands)", flush=True)
            except Exception as e:
                print(f"⚠️ Warning syncing commands: {e}", flush=True)

        asyncio.create_task(_sync_commands())

    async def on_ready(self):
        set_bot_status(True, "✅ Connected to Discord and ready!", f"{self.user} (ID: {self.user.id})")
        print(f'✅ Bot online! Logged in as {self.user} (ID: {self.user.id})', flush=True)
        print('------', flush=True)

    async def _resolve_channel(self, channel_id: int):
        if not channel_id:
            return None
        ch = self.get_channel(channel_id)
        if ch is None:
            try:
                ch = await self.fetch_channel(channel_id)
            except Exception:
                ch = None
        return ch

    # ---------- Loop 1: Check new movies hourly ----------
    @tasks.loop(hours=1)
    async def check_movies(self):
        try:
            movies = await fetch_major_movies()
            new_movies = []
            for movie in movies:
                if movie['name'] not in self.movie_cache:
                    new_movies.append(movie)
                    self.movie_cache.add(movie['name'])

            if new_movies:
                save_movie_cache(self.movie_cache)

            if new_movies and self.movie_channel_id:
                channel = await self._resolve_channel(self.movie_channel_id)
                if channel:
                    for movie in new_movies:
                        trailer_url = get_trailer_url(movie['name'])
                        embed = discord.Embed(
                            title="🆕 New Movie in Theaters!",
                            description=f"# 🎬 {movie['name']}",
                            color=0xE50914
                        )
                        if movie['image']:
                            embed.set_thumbnail(url=movie['image'])

                        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
                        embed.add_field(name="⏰ Showtimes", value=showtimes_fmt[:1024], inline=False)
                        embed.add_field(name="📍 Cinema", value="Major Cineplex Big C Lopburi", inline=True)
                        embed.add_field(name="🎞️ Trailer", value=f"[▶️ Watch on YouTube]({trailer_url})", inline=True)
                        embed.set_footer(text="Type /meetup to invite friends!")

                        trailer_view = discord.ui.View()
                        trailer_view.add_item(discord.ui.Button(
                            label=f"▶️ Trailer: {movie['name'][:40]}",
                            style=discord.ButtonStyle.link,
                            url=trailer_url
                        ))

                        await channel.send(
                            content="🍿 **New movie released!**",
                            embed=embed,
                            view=trailer_view
                        )
        except Exception as e:
            print(f"Error checking movies: {e}")

    @check_movies.before_loop
    async def before_check_movies(self):
        await self.wait_until_ready()

    # ---------- Loop 2: Check weather and alert rain every 5 minutes ----------
    @tasks.loop(minutes=5)
    async def check_weather(self):
        if not self.weather_channel_id or self.weather_lat is None or self.weather_lon is None:
            return

        now = now_thai()
        if self.last_weather_alert_time and (now - self.last_weather_alert_time).total_seconds() < 1800:
            return

        try:
            data, _ = await _fetch_weather_and_aq_data(self.weather_lat, self.weather_lon, forecast_days=2)
            if not data:
                return

            now_str = now.strftime("%Y-%m-%dT%H:00")
            hourly_data = data.get("hourly", {})
            times = hourly_data.get("time", [])
            probs = hourly_data.get("precipitation_probability", [])
            codes = hourly_data.get("weather_code", [])

            start_idx = 0
            for idx, t_str in enumerate(times):
                if t_str >= now_str:
                    start_idx = idx
                    break

            will_rain = False
            rain_time = ""
            max_prob = 0
            rain_code = 0

            for i in range(start_idx, min(start_idx + 4, len(times))):
                prob = probs[i] if i < len(probs) else 0
                code = codes[i] if i < len(codes) else 0
                if prob >= 60 or code in (51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99):
                    will_rain = True
                    if prob >= max_prob:
                        max_prob = prob
                        rain_time = times[i].split("T")[1]
                        rain_code = code

            if will_rain:
                channel = await self._resolve_channel(self.weather_channel_id)
                if channel:
                    current = data.get("current", {})
                    temp = current.get("temperature_2m", "-")
                    humidity = current.get("relative_humidity_2m", "-")
                    weather_desc = translate_weather_code(rain_code or current.get("weather_code", 0))

                    embed = discord.Embed(
                        title="🌧️ Rain Warning Alert",
                        description=(
                            f"> ⚠️ **Forecast expects rain in your area.**\n"
                            f"> Weather Condition: **{weather_desc}**"
                        ),
                        color=0x3498DB
                    )
                    embed.add_field(name="⏰ Estimated Time", value=f"**`{rain_time}`**", inline=True)
                    embed.add_field(name="☔ Rain Chance", value=f"**`{max_prob}%`**", inline=True)
                    embed.add_field(name="🌡️ Temp / Humidity", value=f"**`{temp}°C`** • `{humidity}%`", inline=True)
                    embed.set_footer(text=f"📍 Coordinates: {self.weather_lat}, {self.weather_lon} • 30-min cooldown active")

                    map_buf = await generate_weather_map(self.weather_lat, self.weather_lon)
                    view = WeatherMapView(self.weather_lat, self.weather_lon)
                    if map_buf:
                        file = discord.File(fp=map_buf, filename="weather_map.png")
                        embed.set_image(url="attachment://weather_map.png")
                        await channel.send(
                            content="☔ **Grab your umbrella!** High chance of rain.",
                            embed=embed,
                            file=file,
                            view=view
                        )
                    else:
                        await channel.send(
                            content="☔ **Grab your umbrella!** High chance of rain.",
                            embed=embed,
                            view=view
                        )
                    self.last_weather_alert_time = now
        except Exception as e:
            print(f"Error checking weather: {e}")

    @check_weather.before_loop
    async def before_check_weather(self):
        await self.wait_until_ready()

    # ---------- Loop 3: Auto Meetup Reminders ----------
    @tasks.loop(minutes=1)
    async def check_reminders(self):
        try:
            history = load_json(HISTORY_FILE, [])
            if not history:
                return

            now = now_thai()
            changed = False

            for entry in history:
                if entry.get("reminded", False):
                    continue
                remind_iso = entry.get("remind_at_iso")
                if not remind_iso:
                    continue

                try:
                    remind_dt = datetime.fromisoformat(remind_iso).astimezone(THAI_TZ)
                except Exception:
                    continue

                if now >= remind_dt:
                    entry["reminded"] = True
                    changed = True

                    # Skip if reminder time is over 2 hours ago (e.g. bot was offline)
                    if (now - remind_dt).total_seconds() > 7200:
                        continue

                    chan_id_str = entry.get("channel_id")
                    msg_id_str = entry.get("message_id")
                    if not chan_id_str:
                        continue

                    channel = await self._resolve_channel(int(chan_id_str))
                    if not channel:
                        continue

                    going_list = entry.get("going_mentions", [])
                    tentative_list = entry.get("tentative_mentions", [])
                    topic = entry.get("topic", "Meetup")
                    location = entry.get("location", "-")
                    time_val = entry.get("time", "-")
                    jump_url = None

                    # Fetch latest participant list from message card
                    if msg_id_str:
                        try:
                            msg = await channel.fetch_message(int(msg_id_str))
                            if msg and msg.embeds:
                                info = parse_meetup_embed(msg.embeds[0])
                                going_list = info["going"]
                                tentative_list = info["tentative"]
                                topic = info["topic"]
                                location = info["location"]
                                time_val = info["time_val"]
                                jump_url = msg.jump_url
                        except Exception:
                            pass

                    all_pings = []
                    for m in going_list + tentative_list:
                        if m not in all_pings:
                            all_pings.append(m)

                    if not all_pings:
                        continue

                    mentions_str = " ".join(all_pings)
                    desc_lines = [
                        f"📍 **Location:** `{location}`",
                        f"⏰ **Time:** `{time_val}`",
                        f"👥 **Going ({len(going_list)}):** {' '.join(going_list) if going_list else '-'}",
                    ]
                    if tentative_list:
                        desc_lines.append(f"🤔 **Tentative ({len(tentative_list)}):** {' '.join(tentative_list)}")
                    if jump_url:
                        desc_lines.append(f"\n🔗 [Open Meetup Card]({jump_url})")

                    rem_embed = discord.Embed(
                        title=f"⏰ Auto Reminder: '{topic}' starting soon!",
                        description="\n".join(desc_lines),
                        color=0xF1C40F
                    )
                    rem_embed.set_footer(text="Automated meetup reminder system")

                    await channel.send(
                        content=f"🔔 **Get ready!** Meetup reminder for participants: {mentions_str}",
                        embed=rem_embed,
                        allowed_mentions=discord.AllowedMentions(users=True)
                    )

                    # Send DM alert in parallel
                    await _send_dm_to_mentions(
                        self,
                        all_pings,
                        rem_embed,
                        content=f"💌 **Meetup Reminder: `{topic}` starting soon!**"
                    )

            if changed:
                save_json(HISTORY_FILE, history)
        except Exception as e:
            print(f"Error in check_reminders: {e}")

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.wait_until_ready()

    # ---------- Loop 4: Daily Morning Briefing (07:30) ----------
    @tasks.loop(time=dt_time(hour=7, minute=30, tzinfo=THAI_TZ))
    async def morning_briefing(self):
        target_channel_id = self.weather_channel_id or self.movie_channel_id
        if not target_channel_id:
            return
        channel = await self._resolve_channel(target_channel_id)
        if not channel:
            return

        guild_id_str = str(channel.guild.id) if channel.guild else None
        embed, map_buf, view = await build_morning_briefing_card(
            lat=self.weather_lat if self.weather_lat is not None else 14.7995,
            lon=self.weather_lon if self.weather_lon is not None else 100.6534,
            guild_id_str=guild_id_str
        )
        if not embed:
            return

        if map_buf:
            file = discord.File(fp=map_buf, filename="morning_weather_map.png")
            embed.set_image(url="attachment://morning_weather_map.png")
            await channel.send(
                content="🌅 **Good morning! Here is today's weather, PM 2.5, and meetup briefing.**",
                embed=embed,
                file=file,
                view=view
            )
        else:
            await channel.send(
                content="🌅 **Good morning! Here is today's weather, PM 2.5, and meetup briefing.**",
                embed=embed,
                view=view
            )

    @morning_briefing.before_loop
    async def before_morning_briefing(self):
        await self.wait_until_ready()


async def build_morning_briefing_card(lat: float, lon: float, guild_id_str: str = None):
    """Build morning briefing card (Weather + PM 2.5 + Rain Chance + Today's Meetups)."""
    now = now_thai()
    date_header = now.strftime("%A, %d %B %Y")

    data, aq_data = await _fetch_weather_and_aq_data(lat, lon, forecast_days=1)

    current = data.get("current", {})
    temp = current.get("temperature_2m", "-")
    feels_like = current.get("apparent_temperature", "-")
    humidity = current.get("relative_humidity_2m", "-")
    w_code = current.get("weather_code", 0)
    weather_desc = translate_weather_code(w_code)

    daily = data.get("daily", {})
    t_max = (daily.get("temperature_2m_max") or ["-"])[0]
    t_min = (daily.get("temperature_2m_min") or ["-"])[0]
    rain_prob_max = (daily.get("precipitation_probability_max") or [0])[0] or 0

    # Check peak rain chance hours (07:00 - 23:00)
    hourly = data.get("hourly", {})
    h_times = hourly.get("time", [])
    h_probs = hourly.get("precipitation_probability", [])
    rainy_hours = []
    for idx, t_str in enumerate(h_times):
        p = h_probs[idx] if idx < len(h_probs) else 0
        if p and p >= 50:
            hh = t_str.split("T")[1] if "T" in t_str else t_str
            rainy_hours.append(f"{hh} ({p}%)")

    # PM 2.5 Air Quality
    aq_current = aq_data.get("current", {})
    pm25_val = aq_current.get("pm2_5")
    aqi_val = aq_current.get("us_aqi", "-")
    aq_status, aq_warning, is_dust_high = evaluate_air_quality(pm25_val, aqi_val)
    pm25_str = f"{pm25_val} µg/m³" if pm25_val is not None else "No data"

    # Today's meetups
    history = load_json(HISTORY_FILE, [])
    today_meetups = []
    today_date = now.date()
    day_token_patterns = [f" {now.day} ", f"/{now.day}/", f"{now.day:02d}/{now.month:02d}"]

    for entry in history:
        if guild_id_str and entry.get("guild_id") and str(entry.get("guild_id")) != guild_id_str:
            continue
        is_today = False
        m_iso = entry.get("meetup_iso")
        t_val = entry.get("time", "")
        c_iso = entry.get("created_at", "")

        if m_iso:
            try:
                m_dt = datetime.fromisoformat(m_iso).astimezone(THAI_TZ)
                if m_dt.date() == today_date:
                    is_today = True
            except Exception:
                pass
        else:
            if any(pat in t_val for pat in day_token_patterns):
                is_today = True
            elif c_iso:
                try:
                    c_dt = datetime.fromisoformat(c_iso)
                    if c_dt.date() == today_date and "tomorrow" not in t_val.lower():
                        is_today = True
                except Exception:
                    pass

        if is_today:
            today_meetups.append(entry)

    # Daily advice
    advice_parts = []
    if rain_prob_max >= 60:
        advice_parts.append("☔ **High rain chance today.** Grab an umbrella before leaving home.")
    elif rain_prob_max >= 35:
        advice_parts.append("🌦️ **Scattered rain possible today.** Keep a compact umbrella handy.")
    else:
        advice_parts.append("☀️ **Low rain chance today.** Great weather for outdoor activities.")

    if aq_warning:
        advice_parts.append(aq_warning)

    desc_quote = "> " + "\n> ".join(advice_parts)

    embed = discord.Embed(
        title=f"🌅 Morning Briefing • {date_header}",
        description=desc_quote,
        color=0xF39C12 if not is_dust_high else 0xE74C3C
    )

    embed.add_field(
        name="🌤️ Today's Weather",
        value=(
            f"**{weather_desc}**\n"
            f"Now **`{temp}°C`** (Feels like **`{feels_like}°C`**)\n"
            f"Range: `{t_min}°C – {t_max}°C` • Humidity: `{humidity}%`"
        ),
        inline=True
    )

    rain_detail = f"Peak Chance **`{rain_prob_max}%`**"
    if rainy_hours:
        rain_detail += f"\nRisk Window: `{', '.join(rainy_hours[:3])}`"
    else:
        rain_detail += "\nNo heavy rain expected"
    embed.add_field(
        name="☔ Rain Risk",
        value=rain_detail,
        inline=True
    )

    embed.add_field(
        name="😷 PM 2.5 & Air Quality",
        value=f"PM 2.5: **`{pm25_str}`** • AQI: **`{aqi_val}`**\nStatus: {aq_status}",
        inline=False
    )

    if today_meetups:
        meetup_lines = []
        for idx, m in enumerate(today_meetups[-5:], 1):
            topic = clean_topic_title(m.get("topic", "Untitled"))
            loc = m.get("location", "-")
            t_str = m.get("time", "-")
            p_cnt = m.get("participants", 0)
            max_l = m.get("max_limit")
            p_txt = f"{p_cnt}/{max_l}" if max_l else f"{p_cnt}"
            meetup_lines.append(f"`{idx:02d}.` **{topic}** — 📍 {loc} • ⏰ `{t_str}` (👥 {p_txt})")
        meetup_summary = "\n".join(meetup_lines)
    else:
        meetup_summary = "> 📭 *No meetups scheduled for today. Type `/meetup` to organize one!*"

    embed.add_field(
        name=f"📅 Today's Meetups ({len(today_meetups)})",
        value=meetup_summary[:1024],
        inline=False
    )

    embed.set_footer(text=f"📍 Coordinates: {lat}, {lon} • Sent daily at 07:30")

    map_buf = await generate_weather_map(lat, lon)
    view = WeatherMapView(lat, lon)
    return embed, map_buf, view


bot = MyBot()

# ==================== Slash Commands ====================

class CreateMeetupModal(discord.ui.Modal, title='📅 Create Meetup'):
    topic = discord.ui.TextInput(
        label='Topic',
        style=discord.TextStyle.short,
        placeholder='e.g. Gaming, Dinner, Movie',
        required=True,
        max_length=100,
    )
    location = discord.ui.TextInput(
        label='Location',
        style=discord.TextStyle.short,
        placeholder='e.g. Dorm A, Regular Spot, Mall',
        required=True,
        max_length=100,
    )
    time_str = discord.ui.TextInput(
        label='Time (HH:MM for 30m auto-remind)',
        style=discord.TextStyle.short,
        placeholder='e.g. Today 20:00, Tomorrow 14:30',
        required=True,
        max_length=50,
    )
    max_participants = discord.ui.TextInput(
        label='Max People (leave blank for unlimited)',
        style=discord.TextStyle.short,
        placeholder='e.g. 4, 5, 10 (full goes to waitlist)',
        required=False,
        max_length=5,
    )
    note = discord.ui.TextInput(
        label='Note (optional)',
        style=discord.TextStyle.long,
        placeholder='e.g. Late arrivals pay, wear black...',
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        topic_val = self.topic.value.strip()
        location_val = self.location.value.strip()
        time_val = self.time_str.value.strip()
        note_val = self.note.value.strip() if self.note.value else None
        max_raw = self.max_participants.value.strip() if self.max_participants.value else ""

        max_limit = None
        if max_raw:
            if not max_raw.isdigit() or int(max_raw) <= 0:
                await interaction.response.send_message("❌ Max people must be a positive integer like `4` or `5`.", ephemeral=True)
                return
            max_limit = int(max_raw)

        base_embed = discord.Embed(
            title=f"📅  {topic_val}",
            description="> 🔵 **Status:** Meetup Draft (Review before posting)",
            color=0x5865F2
        )
        base_embed.set_footer(text=f"🎯 Created by {interaction.user.display_name} • Click confirm to publish")
        base_embed.set_author(name="🔔 Meetup Draft", icon_url=interaction.user.display_avatar.url)

        info = {
            "location": location_val,
            "time_val": time_val,
            "pin_note": note_val,
            "going": [],
            "waitlist": [],
            "tentative": [],
            "max_limit": max_limit,
            "friend_notes": "*No notes yet*",
        }
        embed = rebuild_meetup_embed(base_embed, info)
        view = DraftMeetupView()

        await interaction.response.send_message(
            content="**Meetup Draft** (only you see this)\nWhen ready, click **📢 Confirm & Post** to publish to channel.",
            embed=embed,
            view=view,
            ephemeral=True
        )


@bot.tree.command(name="meetup", description="Create meetup (Normal or Movie, with waitlist & limits)")
@app_commands.describe(type="Select meetup type")
@app_commands.choices(type=[
    app_commands.Choice(name="📝 Normal Meetup (custom location, time, and limit)", value="normal"),
    app_commands.Choice(name="🎬 Movie Meetup (pick movie from Major Cineplex)", value="movie"),
])
async def meetup(interaction: discord.Interaction, type: app_commands.Choice[str]):
    if type.value == "normal":
        await interaction.response.send_modal(CreateMeetupModal())
    elif type.value == "movie":
        await interaction.response.defer(ephemeral=True)
        movies = await fetch_major_movies()

        if not movies:
            await interaction.followup.send("❌ No movie showtime data available right now.", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎬 Select a Movie",
            description=(
                "> Choose a movie from the dropdown menu below.\n"
                "> Next, select a date and showtime to auto-create your meetup card!"
            ),
            color=0xE50914
        )
        embed.set_footer(text="📍 Major Cineplex Big C Lopburi")

        view = MovieSelectView(movies)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ==================== Feature: Meetup History ====================

@bot.tree.command(name="history", description="View past meetup history in this server")
async def meetup_history(interaction: discord.Interaction):
    all_history = load_json(HISTORY_FILE, [])

    # Filter history for current guild (prevents cross-server data leak)
    guild_str = str(interaction.guild_id) if interaction.guild_id else ""
    if guild_str:
        history = [h for h in all_history if str(h.get("guild_id", "")) == guild_str]
    else:
        history = all_history

    if not history:
        await interaction.response.send_message("📜 No meetup history in this server yet.", ephemeral=True)
        return

    recent = list(reversed(history[-10:]))

    embed = discord.Embed(
        title="📜 Meetup History",
        description="> Overview of the last 10 meetups organized in this server",
        color=0x5865F2
    )

    for i, entry in enumerate(recent, 1):
        try:
            dt = datetime.fromisoformat(entry.get("created_at", ""))
            date_str = dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            date_str = "Date unknown"

        topic = clean_topic_title(entry.get("topic", "Untitled"))
        location = entry.get("location", "Not specified")
        time_val = entry.get("time", "Not specified")
        participants = entry.get("participants", 0)
        max_limit = entry.get("max_limit")
        tentative_cnt = entry.get("tentative_count", 0)
        created_by = entry.get("created_by", "Unknown")

        p_str = f"`{participants}/{max_limit}`" if max_limit else f"`{participants}`"
        if tentative_cnt:
            p_str += f" • Tentative `{tentative_cnt}`"

        embed.add_field(
            name=f"`{i:02d}.` {topic}",
            value=(
                f"📍 `{location}`  •  ⏰ `{time_val}`\n"
                f"👥 Attendees: {p_str}  •  🗓️ `{date_str}`\n"
                f"👤 Organized by: **{created_by}**"
            ),
            inline=False
        )

    embed.set_footer(text=f"Total: {len(history)} meetups recorded in this server")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== Feature: Daily Morning Briefing ====================

@bot.tree.command(name="daily", description="Daily briefing card: weather, PM 2.5, rain chance, and today's meetups")
async def daily_briefing_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    target_lat = bot.weather_lat if bot.weather_lat is not None else 14.7995
    target_lon = bot.weather_lon if bot.weather_lon is not None else 100.6534
    guild_str = str(interaction.guild_id) if interaction.guild_id else None

    embed, map_buf, view = await build_morning_briefing_card(target_lat, target_lon, guild_str)
    if not embed:
        await interaction.followup.send("❌ Cannot get daily briefing right now.")
        return

    if map_buf:
        file = discord.File(fp=map_buf, filename="morning_weather_map.png")
        embed.set_image(url="attachment://morning_weather_map.png")
        await interaction.followup.send(embed=embed, file=file, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view)


# ==================== Feature: Movie Alert Channel Setting ====================

@bot.tree.command(name="set-movie-channel", description="Set channel for new movie release alerts")
@app_commands.describe(channel="Select channel (current channel if empty)")
async def set_movie_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    try:
        target_channel = channel if channel else interaction.channel
        bot.movie_channel_id = target_channel.id

        config = load_config()
        config["movie_channel_id"] = str(target_channel.id)
        save_config(config)

        embed = discord.Embed(
            title="🎬 Movie Channel Configured",
            description=(
                f"> ✅ Broadcast channel set to {target_channel.mention}\n\n"
                "• **Frequency:** Auto-checks Major Cineplex hourly\n"
                "• **Content:** New releases, posters, and YouTube trailer links"
            ),
            color=0x57F287
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

# ==================== Fetch Movie Data ====================

async def fetch_major_movies():
    url = "https://www.majorcineplex.com/cinema/bigc-lopburi/"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) DiscordMeetupBot/1.0"}
    async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(ssl=False)) as session:
        async with session.get(url, timeout=15) as response:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')

            scripts = soup.find_all('script', type='application/ld+json')

            movie_dict = {}
            for script in scripts:
                if not script.string:
                    continue
                try:
                    data = json.loads(script.string)
                    if '@context' in data and '@graph' in data:
                        for item in data['@graph']:
                            if item.get('@type') == 'ScreeningEvent':
                                work = item.get('workPresented', {})
                                movie_name = work.get('alternateName')
                                if not movie_name:
                                    movie_name = work.get('name', 'Unknown')
                                movie_image = work.get('image', '')

                                start_date = item.get('startDate')
                                if start_date:
                                    dt = datetime.fromisoformat(start_date)
                                    time_str = dt.strftime("%H:%M")
                                else:
                                    time_str = "Unknown"

                                if movie_name not in movie_dict:
                                    movie_dict[movie_name] = {
                                        "name": movie_name,
                                        "image": movie_image,
                                        "showtimes": []
                                    }
                                if time_str not in movie_dict[movie_name]["showtimes"]:
                                    movie_dict[movie_name]["showtimes"].append(time_str)
                except json.JSONDecodeError:
                    continue

            for k in movie_dict:
                movie_dict[k]['showtimes'].sort()

            return list(movie_dict.values())

# ==================== Movie Showtimes ====================

class MovieTrailerView(discord.ui.View):
    def __init__(self, movies):
        super().__init__(timeout=None)
        for movie in movies[:25]:
            trailer_url = get_trailer_url(movie['name'])
            self.add_item(discord.ui.Button(
                label=f"▶️ Trailer: {movie['name'][:35]}",
                style=discord.ButtonStyle.link,
                url=trailer_url
            ))

@bot.tree.command(name="movies", description="Check movie showtimes and trailers at Major Cineplex Big C Lopburi")
async def check_movies_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    movies = await fetch_major_movies()

    if not movies:
        await interaction.followup.send("No movie showtime data available right now.")
        return

    embeds = []
    intro_embed = discord.Embed(
        title="🍿 Today's Movies at Major Cineplex Big C Lopburi",
        description="Click buttons below or links in cards to watch trailers on YouTube!",
        color=discord.Color.red()
    )
    embeds.append(intro_embed)

    display_movies = movies[:9]
    for movie in display_movies:
        trailer_url = get_trailer_url(movie['name'])
        embed = discord.Embed(title=f"🎬 {movie['name']}", color=discord.Color.red())

        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ Showtimes", value=showtimes_fmt[:1024], inline=False)
        embed.add_field(name="🎞️ Trailer", value=f"[▶️ Watch on YouTube]({trailer_url})", inline=False)

        if movie['image']:
            embed.set_thumbnail(url=movie['image'])

        embeds.append(embed)

    view = MovieTrailerView(display_movies)
    await interaction.followup.send(embeds=embeds[:10], view=view)

# ==================== Rain Alert & Weather Check ====================

def _metno_symbol_to_wmo(symbol: str) -> int:
    """Convert MET Norway symbol_code to WMO code."""
    s = (symbol or "").lower()
    if "thunder" in s:
        return 95
    if "heavyrain" in s:
        return 65
    if "rainshowers" in s:
        return 80
    if "lightrain" in s or "drizzle" in s:
        return 51
    if "rain" in s or "sleet" in s:
        return 61
    if "fog" in s:
        return 45
    if "cloudy" in s:
        return 3
    if "partlycloudy" in s:
        return 2
    if "fair" in s:
        return 1
    return 0


async def _fetch_weather_and_aq_data(lat: float, lon: float, forecast_days: int = 2) -> tuple[dict, dict]:
    """Fetch weather and PM 2.5 data (Open-Meteo with MET Norway fallback on Render rate limits)."""
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
        f"&hourly=temperature_2m,precipitation_probability,weather_code"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&timezone=Asia%2FBangkok&forecast_days={forecast_days}"
    )
    aq_url = (
        f"https://air-quality-api.open-meteo.com/v1/air-quality?"
        f"latitude={lat}&longitude={lon}"
        f"&current=pm2_5,pm10,us_aqi&timezone=Asia%2FBangkok"
    )
    headers = {"User-Agent": "DiscordMeetupWeatherBot/1.0 (https://github.com/namnuahme-maker/my-discord-bot)"}
    data = {}
    aq_data = {}

    async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(ssl=False)) as session:
        w_resp, aq_resp = await asyncio.gather(
            session.get(weather_url, timeout=12),
            session.get(aq_url, timeout=12),
            return_exceptions=True
        )
        if not isinstance(w_resp, Exception) and w_resp.status == 200:
            try:
                data = await w_resp.json()
            except Exception:
                data = {}
        if not isinstance(aq_resp, Exception) and aq_resp.status == 200:
            try:
                aq_data = await aq_resp.json()
            except Exception:
                aq_data = {}

        # Fallback: If Open-Meteo hits rate limit (429) on Render, fetch from MET Norway immediately
        if not data or "current" not in data:
            try:
                met_url = f"https://api.met.no/weatherapi/locationforecast/2.0/compact?lat={lat:.4f}&lon={lon:.4f}"
                async with session.get(met_url, timeout=12) as m_resp:
                    if m_resp.status == 200:
                        m_json = await m_resp.json()
                        ts_list = m_json.get("properties", {}).get("timeseries", [])
                        if ts_list:
                            first_det = ts_list[0].get("data", {}).get("instant", {}).get("details", {})
                            first_next = (
                                ts_list[0].get("data", {}).get("next_1_hours", {})
                                or ts_list[0].get("data", {}).get("next_6_hours", {})
                            )
                            sym = first_next.get("summary", {}).get("symbol_code", "")
                            temp_c = first_det.get("air_temperature", 28.0)
                            rh = first_det.get("relative_humidity", 70.0)
                            wind_ms = first_det.get("wind_speed", 2.0)
                            wind_kmh = round(wind_ms * 3.6, 1)
                            feels = round(temp_c + (0.05 * rh - 2.0), 1)

                            times_out = []
                            temps_out = []
                            probs_out = []
                            codes_out = []
                            for item in ts_list[:24]:
                                utc_dt = datetime.fromisoformat(item["time"].replace("Z", "+00:00"))
                                th_dt = utc_dt.astimezone(THAI_TZ)
                                times_out.append(th_dt.strftime("%Y-%m-%dT%H:00"))
                                det = item.get("data", {}).get("instant", {}).get("details", {})
                                nxt = item.get("data", {}).get("next_1_hours", {}) or item.get("data", {}).get("next_6_hours", {})
                                t_v = det.get("air_temperature", temp_c)
                                precip = nxt.get("details", {}).get("precipitation_amount", 0.0) or 0.0
                                s_code = nxt.get("summary", {}).get("symbol_code", "")
                                w_c = _metno_symbol_to_wmo(s_code)
                                prob_v = min(95, int(precip * 40) + (50 if w_c >= 51 else 10)) if (precip > 0 or w_c >= 51) else 10
                                temps_out.append(t_v)
                                probs_out.append(prob_v)
                                codes_out.append(w_c)

                            data = {
                                "current": {
                                    "temperature_2m": temp_c,
                                    "apparent_temperature": feels,
                                    "relative_humidity_2m": rh,
                                    "wind_speed_10m": wind_kmh,
                                    "weather_code": _metno_symbol_to_wmo(sym),
                                },
                                "hourly": {
                                    "time": times_out,
                                    "temperature_2m": temps_out,
                                    "precipitation_probability": probs_out,
                                    "weather_code": codes_out,
                                },
                                "daily": {
                                    "temperature_2m_max": [max(temps_out) if temps_out else temp_c],
                                    "temperature_2m_min": [min(temps_out) if temps_out else temp_c],
                                    "precipitation_probability_max": [max(probs_out) if probs_out else 0],
                                },
                            }
            except Exception as e:
                print(f"Fallback weather API error: {e}", flush=True)

    return data, aq_data


def translate_weather_code(code: int) -> str:
    """Translate WMO weather code to English description with emoji."""
    weather_map = {
        0: "☀️ Clear Sky",
        1: "🌤️ Mainly Clear",
        2: "⛅ Partly Cloudy",
        3: "☁️ Overcast",
        45: "🌫️ Fog",
        48: "🌫️ Depositing Rime Fog",
        51: "🌦️ Light Drizzle",
        53: "🌦️ Moderate Drizzle",
        55: "🌧️ Dense Drizzle",
        61: "🌧️ Slight Rain",
        63: "🌧️ Moderate Rain",
        65: "🌧️ Heavy Rain",
        80: "🌦️ Slight Rain Showers",
        81: "🌧️ Moderate Rain Showers",
        82: "⛈️ Violent Rain Showers",
        95: "⛈️ Thunderstorm",
        96: "⛈️ Thunderstorm with Slight Hail",
        99: "⛈️ Thunderstorm with Heavy Hail",
    }
    return weather_map.get(code, f"🌤️ Weather Code {code}")

class WeatherMapView(discord.ui.View):
    def __init__(self, lat: float, lon: float):
        super().__init__(timeout=None)
        windy_url = f"https://www.windy.com/{lat}/{lon}?radar,{lat},{lon},9"
        gmaps_url = f"https://www.google.com/maps?q={lat},{lon}"
        self.add_item(discord.ui.Button(
            label="🗺️ Live Interactive Radar (Windy)",
            style=discord.ButtonStyle.link,
            url=windy_url
        ))
        self.add_item(discord.ui.Button(
            label="📍 Open in Google Maps",
            style=discord.ButtonStyle.link,
            url=gmaps_url
        ))

def _compose_weather_map_sync(coords, tile_xy, base_tiles, radar_parent_map, offset_x, offset_y):
    """Process radar image with Pillow in background thread to avoid blocking event loop."""
    canvas = Image.new("RGBA", (256 * 3, 256 * 3), (230, 236, 240, 255))
    for idx, (gx, gy) in enumerate(coords):
        tile_img = base_tiles[idx]
        if tile_img:
            canvas.paste(tile_img, (gx * 256, gy * 256))

    if radar_parent_map:
        radar_layer = Image.new("RGBA", (256 * 3, 256 * 3), (0, 0, 0, 0))
        for idx, (gx, gy) in enumerate(coords):
            tx, ty = tile_xy[idx]
            parent_img = radar_parent_map.get((tx // 2, ty // 2))
            if parent_img and parent_img.size == (512, 512):
                qx = (tx % 2) * 256
                qy = (ty % 2) * 256
                r_img = parent_img.crop((qx, qy, qx + 256, qy + 256))
                r, g, b, a = r_img.split()
                a = a.point(lambda p: int(p * 0.72))
                r_img = Image.merge("RGBA", (r, g, b, a))
                radar_layer.paste(r_img, (gx * 256, gy * 256))
        canvas = Image.alpha_composite(canvas, radar_layer)

    px = 256 + offset_x
    py = 256 + offset_y
    w, h = 640, 380
    left = max(0, min(canvas.width - w, px - w // 2))
    top = max(0, min(canvas.height - h, py - h // 2))
    cropped = canvas.crop((left, top, left + w, top + h))

    pin_x = px - left
    pin_y = py - top

    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    for radius in (45, 95):
        draw.ellipse(
            (pin_x - radius, pin_y - radius, pin_x + radius, pin_y + radius),
            outline=(52, 152, 219, 170),
            width=2
        )

    draw.line((pin_x - 16, pin_y, pin_x + 16, pin_y), fill=(231, 76, 60, 200), width=2)
    draw.line((pin_x, pin_y - 16, pin_x, pin_y + 16), fill=(231, 76, 60, 200), width=2)

    draw.ellipse((pin_x - 9, pin_y - 9, pin_x + 9, pin_y + 9), fill=(255, 255, 255, 255))
    draw.ellipse((pin_x - 6, pin_y - 6, pin_x + 6, pin_y + 6), fill=(231, 76, 60, 255))

    final_img = Image.alpha_composite(cropped, overlay).convert("RGB")
    buf = io.BytesIO()
    final_img.save(buf, format="PNG")
    buf.seek(0)
    return buf

async def generate_weather_map(lat: float, lon: float, zoom: int = 8):
    """Generate map image with live rain radar layer (RainViewer + OpenStreetMap) and location pin."""
    try:
        lat_rad = math.radians(lat)
        n = 2.0 ** zoom
        xtile_f = (lon + 180.0) / 360.0 * n
        ytile_f = (1.0 - math.asinh(math.tan(lat_rad)) / math.pi) / 2.0 * n

        center_tx = int(xtile_f)
        center_ty = int(ytile_f)
        offset_x = int((xtile_f - center_tx) * 256)
        offset_y = int((ytile_f - center_ty) * 256)

        headers = {"User-Agent": "DiscordWeatherBot/1.0"}
        async with aiohttp.ClientSession(headers=headers, connector=aiohttp.TCPConnector(ssl=False)) as session:
            radar_path = None
            try:
                async with session.get("https://api.rainviewer.com/public/weather-maps.json", timeout=8) as r:
                    if r.status == 200:
                        rv_data = await r.json()
                        past_frames = rv_data.get("radar", {}).get("past", [])
                        if past_frames:
                            radar_path = past_frames[-1].get("path")
            except Exception:
                radar_path = None

            async def fetch_tile(url):
                try:
                    async with session.get(url, timeout=8) as resp:
                        if resp.status == 200:
                            data = await resp.read()
                            return Image.open(io.BytesIO(data)).convert("RGBA")
                except Exception:
                    pass
                return None

            base_tasks = []
            coords = []
            tile_xy = []
            parent_radar_coords = set()
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    tx = (center_tx + dx) % int(n)
                    ty = center_ty + dy
                    coords.append((dx + 1, dy + 1))
                    tile_xy.append((tx, ty))
                    base_url = f"https://tile.openstreetmap.org/{zoom}/{tx}/{ty}.png"
                    base_tasks.append(fetch_tile(base_url))
                    if radar_path:
                        parent_radar_coords.add((tx // 2, ty // 2))

            parent_list = list(parent_radar_coords)
            radar_parent_tasks = [
                fetch_tile(f"https://tilecache.rainviewer.com{radar_path}/512/{zoom - 1}/{ptx}/{pty}/2/1_1.png")
                for (ptx, pty) in parent_list
            ] if radar_path else []

            base_tiles = await asyncio.gather(*base_tasks)
            radar_parent_imgs = await asyncio.gather(*radar_parent_tasks) if radar_parent_tasks else []
            radar_parent_map = dict(zip(parent_list, radar_parent_imgs))

        return await asyncio.to_thread(
            _compose_weather_map_sync,
            coords, tile_xy, base_tiles, radar_parent_map, offset_x, offset_y
        )
    except Exception as e:
        print(f"Error generating weather map: {e}")
        return None

@bot.tree.command(name="set-weather-channel", description="Set rain alert and morning briefing (07:30) channel and coordinates")
@app_commands.describe(
    lat="Latitude, e.g. 14.7995",
    lon="Longitude, e.g. 100.6534",
    channel="Select channel (current channel if empty)"
)
async def set_weather_channel(interaction: discord.Interaction, lat: float, lon: float, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    try:
        target_channel = channel if channel else interaction.channel
        bot.weather_channel_id = target_channel.id
        bot.weather_lat = lat
        bot.weather_lon = lon
        bot.last_weather_alert_time = None

        config = load_config()
        config["weather_channel_id"] = str(target_channel.id)
        config["weather_lat"] = lat
        config["weather_lon"] = lon
        save_config(config)

        embed = discord.Embed(
            title="🌦️ Weather & Morning Briefing Configured",
            description=(
                f"> ✅ Broadcast channel set to {target_channel.mention}\n\n"
                f"📍 **Target Coordinates:** `{lat:.4f}, {lon:.4f}`\n"
                "🌅 **Daily Morning Briefing:** Scheduled at `07:30` (UTC+7)\n"
                "🌧️ **Live Rain Alerts:** Instant alerts with 30-min storm cooldown"
            ),
            color=0x3498DB
        )
        map_buf = await generate_weather_map(lat, lon)
        view = WeatherMapView(lat, lon)
        if map_buf:
            file = discord.File(fp=map_buf, filename="weather_map.png")
            embed.set_image(url="attachment://weather_map.png")
            await interaction.followup.send(embed=embed, file=file, view=view, ephemeral=True)
        else:
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)

        if bot.check_weather.is_running():
            bot.check_weather.restart()
        else:
            bot.check_weather.start()
    except Exception as e:
        await interaction.followup.send(f"❌ Error: {e}", ephemeral=True)

def evaluate_air_quality(pm25: float, aqi: int):
    """Evaluate air quality and PM 2.5 levels according to standard thresholds."""
    if pm25 is None:
        return "No data", None, False
    if pm25 <= 15.0:
        return "🟦 Excellent (Clean Air)", None, False
    elif pm25 <= 25.0:
        return "🟩 Good (Normal)", None, False
    elif pm25 <= 37.5:
        return "🟨 Moderate", None, False
    elif pm25 <= 75.0:
        return (
            "🟧 Unhealthy for Sensitive Groups!",
            "⚠️ **PM 2.5 warning!** (`> 37.5 µg/m³`) Wear a dust mask outdoors.",
            True
        )
    else:
        return (
            "🟥 Very Unhealthy / Hazardous!",
            "🚨 **Severe PM 2.5 warning!** (`> 75.0 µg/m³`) Avoid outdoor activities and wear N95 mask.",
            True
        )

@bot.tree.command(name="weather", description="Check current weather, PM 2.5, and live rain radar")
@app_commands.describe(
    lat="Latitude (default: saved config or Lopburi)",
    lon="Longitude (default: saved config or Lopburi)"
)
async def check_weather_cmd(interaction: discord.Interaction, lat: float = None, lon: float = None):
    await interaction.response.defer()

    target_lat = lat if lat is not None else (bot.weather_lat if bot.weather_lat is not None else 14.7995)
    target_lon = lon if lon is not None else (bot.weather_lon if bot.weather_lon is not None else 100.6534)

    try:
        data, aq_data = await _fetch_weather_and_aq_data(target_lat, target_lon, forecast_days=2)
        if not data or "current" not in data:
            await interaction.followup.send("❌ Cannot get weather data right now.")
            return

        current = data.get("current", {})
        temp = current.get("temperature_2m", "-")
        feels_like = current.get("apparent_temperature", "-")
        humidity = current.get("relative_humidity_2m", "-")
        wind = current.get("wind_speed_10m", "-")
        w_code = current.get("weather_code", 0)
        weather_desc = translate_weather_code(w_code)

        aq_current = aq_data.get("current", {})
        pm25_val = aq_current.get("pm2_5")
        aqi_val = aq_current.get("us_aqi", "-")
        aq_status, aq_warning, is_dust_high = evaluate_air_quality(pm25_val, aqi_val)
        pm25_str = f"{pm25_val} µg/m³" if pm25_val is not None else "No data"

        now = now_thai()
        now_str = now.strftime("%Y-%m-%dT%H:00")
        hourly = data.get("hourly", {})
        times = hourly.get("time", [])
        probs = hourly.get("precipitation_probability", [])
        h_temps = hourly.get("temperature_2m", [])
        h_codes = hourly.get("weather_code", [])

        start_idx = 0
        for idx, t_str in enumerate(times):
            if t_str >= now_str:
                start_idx = idx
                break

        forecast_lines = []
        max_rain_prob = 0
        for i in range(start_idx, min(start_idx + 4, len(times))):
            t_label = times[i].split("T")[1]
            p_val = probs[i] if i < len(probs) else 0
            t_val = h_temps[i] if i < len(h_temps) else "-"
            c_val = h_codes[i] if i < len(h_codes) else 0
            icon = translate_weather_code(c_val).split(" ")[0]
            if p_val > max_rain_prob:
                max_rain_prob = p_val
            forecast_lines.append(f"` {t_label} ` {icon} **{t_val}°C** • ☔ Rain `{p_val}%`")

        forecast_text = "\n".join(forecast_lines) if forecast_lines else "No forecast data"

        if is_dust_high:
            card_color = 0xE67E22 if (pm25_val and pm25_val <= 75.0) else 0xE74C3C
        else:
            card_color = 0x3498DB if max_rain_prob >= 60 else (0xF1C40F if max_rain_prob >= 30 else 0x2ECC71)

        desc_parts = [f"> ### **{weather_desc}**"]
        if aq_warning:
            desc_parts.append(f"> ⚠️ **Air Quality Warning:** {aq_warning}")
        desc_text = "\n".join(desc_parts)

        embed = discord.Embed(
            title="🌤️ Live Weather & Radar Report",
            description=desc_text,
            color=card_color
        )
        embed.add_field(
            name="🌡️ Temperature",
            value=f"**{temp}°C**\nFeels like **`{feels_like}°C`**",
            inline=True
        )
        embed.add_field(
            name="💧 Conditions",
            value=f"Humidity: **`{humidity}%`**\nWind: **`{wind} km/h`**",
            inline=True
        )
        embed.add_field(
            name="☔ Rain Risk",
            value=f"Peak Chance: **`{max_rain_prob}%`**\nCoords: `{target_lat:.4f}, {target_lon:.4f}`",
            inline=True
        )
        embed.add_field(
            name="😷 Air Quality (AQI & PM 2.5)",
            value=f"PM 2.5: **`{pm25_str}`** • AQI: **`{aqi_val}`**\nStatus: {aq_status}",
            inline=False
        )
        embed.add_field(name="🕒 12-Hour Forecast", value=forecast_text, inline=False)
        embed.set_footer(text=f"🗺️ Live Rain Radar • Updated {now.strftime('%d/%m/%Y %H:%M')}")

        map_buf = await generate_weather_map(target_lat, target_lon)
        view = WeatherMapView(target_lat, target_lon)
        if map_buf:
            file = discord.File(fp=map_buf, filename="weather_map.png")
            embed.set_image(url="attachment://weather_map.png")
            await interaction.followup.send(embed=embed, file=file, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"❌ Weather check error: {e}")

# ==================== Runner ====================

def _reset_bot_session():
    """Reset HTTP session and internal bot state to allow clean reconnection."""
    try:
        bot._closing_task = None
        bot.http.connector = discord.utils.MISSING
        bot.clear()
    except Exception:
        pass

if __name__ == '__main__':
    keep_alive()
    clean_token = TOKEN.strip().strip('"').strip("'")
    if clean_token.startswith("DISCORD_TOKEN="):
        clean_token = clean_token.split("=", 1)[1].strip().strip('"').strip("'")
    if clean_token.lower().startswith("bot "):
        clean_token = clean_token[4:].strip()

    if clean_token == 'YOUR_BOT_TOKEN_HERE' or not clean_token:
        err_msg = "❌ DISCORD_TOKEN not found! Set DISCORD_TOKEN in Render environment."
        print(err_msg, flush=True)
        set_bot_status(False, err_msg)
        import time
        while True:
            time.sleep(60)
    else:
        import time
        discord.utils.setup_logging()
        attempt = 0
        while True:
            attempt += 1
            try:
                bot.run(clean_token, log_handler=None)
                break
            except discord.LoginFailure as e:
                err_msg = f"❌ Invalid DISCORD_TOKEN: {e}"
                print(err_msg, flush=True)
                set_bot_status(False, err_msg)
                while True:
                    time.sleep(60)
            except discord.HTTPException as e:
                status_code = getattr(e, 'status', 429)
                err_msg = (
                    f"⚠️ Discord IP rate limit on Render (HTTP {status_code} - attempt {attempt}) "
                    f"— waiting 60s to retry automatically... "
                    f"(💡 Tip: recreate Render Web Service with Region Singapore or Frankfurt to switch IP)"
                )
                print(err_msg, flush=True)
                set_bot_status(False, err_msg)
                time.sleep(60)
                _reset_bot_session()
            except Exception as e:
                err_msg = f"❌ Discord connection failed (attempt {attempt}): {e} — retrying in 30s..."
                print(err_msg, flush=True)
                set_bot_status(False, err_msg)
                time.sleep(30)
                _reset_bot_session()
