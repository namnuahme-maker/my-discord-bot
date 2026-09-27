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
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
from bs4 import BeautifulSoup
from datetime import datetime, timedelta, timezone, time as dt_time
from urllib.parse import quote
from PIL import Image, ImageDraw

# ไอคอนและอีโมจิสม่ำเสมอสำหรับปุ่มและหัวข้อใน Embed
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
    """คืนค่าสีธีมของ Embed ให้สอดคล้องกับสถานะการนัดหมาย:
    - สีเขียวมรกต (0x2ECC71) : โหมดเช็คอิน (ถึงเวลานัดหมายแล้ว)
    - สีส้มแจ้งเตือน (0xE67E22): จำนวนคนครบเต็มจำนวนสูงสุดแล้ว (Full / คิวสำรอง)
    - สีน้ำเงินคราม (0x5865F2): เปิดรับลงชื่อปกติ (Default)
    """
    if info.get("checkin_mode"):
        return 0x2ECC71
    max_limit = info.get("max_limit")
    if max_limit and max_limit > 0 and len(info.get("going", [])) >= max_limit:
        return 0xE67E22
    return 0x5865F2

def _progress_bar(current: int, total: int = None, length: int = 10) -> str:
    """สร้าง Progress Bar แบบข้อความ (เช่น ██████░░░░ 3/5 คน (60%))"""
    if total and total > 0:
        ratio = max(0.0, min(1.0, current / total))
        filled = int(round(ratio * length))
        empty = length - filled
        bar = "█" * filled + "░" * empty
        status_suffix = " (เต็ม!)" if current >= total else f" ({int(round(ratio * 100))}%)"
        return f"{bar} {current}/{total} คน{status_suffix}"
    else:
        filled = max(0, min(length, current))
        empty = length - filled
        bar = "█" * filled + "░" * empty
        return f"{bar} {current} คน"

from keep_alive import keep_alive

# ==================== ตั้งค่า Timezone & Environment ====================

# เวลาประเทศไทย (UTC+7) เพื่อให้ทำงานถูกต้องเสมอแม้รันบนเซิร์ฟเวอร์ Cloud (เช่น Render ที่ใช้ UTC)
THAI_TZ = timezone(timedelta(hours=7))

def now_thai() -> datetime:
    """คืนค่าวันและเวลาปัจจุบันตามเวลาประเทศไทย (UTC+7)"""
    return datetime.now(THAI_TZ)

DATA_DIR = os.path.dirname(os.path.abspath(__file__))
ENV_FILE = os.path.join(DATA_DIR, ".env")
HISTORY_FILE = os.path.join(DATA_DIR, "meetup_history.json")
MOVIE_CACHE_FILE = os.path.join(DATA_DIR, "movie_cache.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

def _load_dotenv_fallback(filepath: str):
    """โหลดค่าจากไฟล์ .env เข้าสู่ os.environ โดยไม่ต้องพึ่งไลบรารีภายนอก"""
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

# ==================== ฟังก์ชันจัดการข้อมูล ====================

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

# ==================== ฟังก์ชันช่วยแปลงเวลา & จัดการ Embed นัดหมาย ====================

def clean_topic_title(title: str) -> str:
    """ลบอีโมตนำหน้า (เช่น 📅, 🎬) และช่องว่างส่วนเกินออกจากชื่อหัวข้อนัดหมาย"""
    if not title:
        return "ไม่ระบุ"
    cleaned = re.sub(r'^[📅🎬\s]+', '', title).strip()
    return cleaned if cleaned else "ไม่ระบุ"

def parse_thai_meetup_datetime(time_str: str, ref_dt: datetime = None):
    """
    แปลงข้อความเวลานัดภาษาไทย เช่น 'วันนี้ 20:00', 'พรุ่งนี้ เวลา 14:30', 'จันทร์ ที่28 ตอน 11:30'
    ให้เป็น datetime (THAI_TZ) เพื่อใช้ตั้งเวลาแจ้งเตือนล่วงหน้าอัตโนมัติ
    """
    if not time_str:
        return None
    if any(k in time_str for k in ("ยังไม่ระบุเวลา", "รอนัดหมาย", "ยังไม่แน่ใจ")):
        return None

    if ref_dt is None:
        ref_dt = now_thai()

    text = time_str.strip()
    target_date = ref_dt.date()
    explicit_date = False

    # 1. ตรวจหาวันที่แบบ DD/MM/YYYY หรือ DD/MM
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
        if "พรุ่งนี้" in text:
            target_date = ref_dt.date() + timedelta(days=1)
            explicit_date = True
        elif "มะรืน" in text:
            target_date = ref_dt.date() + timedelta(days=2)
            explicit_date = True
        elif "วันนี้" in text:
            target_date = ref_dt.date()
            explicit_date = True
        else:
            day_match = re.search(r'(?:วันที่|ที่)\s*(\d{1,2})', text)
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

    # 2. ตรวจหาเวลา HH:MM หรือ HH.MM
    hour = None
    minute = 0
    time_match = re.search(r'(?:เวลา|ตอน|เมื่อ)?\s*(\d{1,2})\s*[:.]\s*(\d{2})', text_for_time)
    if time_match:
        h_val = int(time_match.group(1))
        m_val = int(time_match.group(2))
        if 0 <= h_val <= 23 and 0 <= m_val <= 59:
            hour, minute = h_val, m_val
    else:
        # ตรวจหาคำบอกเวลาภาษาพูดทั่วไป (รองรับทั้งตัวเลขและคำอ่านตัวเลขไทย)
        thai_num_map = [
            ("สิบเอ็ด", "11"), ("สิบ", "10"), ("เก้า", "9"), ("แปด", "8"),
            ("เจ็ด", "7"), ("หก", "6"), ("ห้า", "5"), ("สี่", "4"),
            ("สาม", "3"), ("สอง", "2"), ("หนึ่ง", "1"),
        ]
        norm_time_text = text_for_time
        for word, digit in thai_num_map:
            norm_time_text = norm_time_text.replace(word, digit)

        if "เที่ยงคืน" in norm_time_text:
            hour, minute = 0, 0
        elif "เที่ยง" in norm_time_text:
            hour, minute = 12, 0
        elif "บ่ายโมง" in norm_time_text:
            hour, minute = 13, 0
        else:
            afternoon_m = re.search(r'บ่าย\s*(\d{1,2})', norm_time_text)
            night_m = re.search(r'(\d{1,2})\s*ทุ่ม', norm_time_text)
            evening_m = re.search(r'(\d{1,2})\s*โมงเย็น', norm_time_text)
            morning_m = re.search(r'(\d{1,2})\s*โมงเช้า', norm_time_text)
            if afternoon_m:
                hour = 12 + int(afternoon_m.group(1))
            elif night_m:
                hour = 18 + int(night_m.group(1))
            elif "ทุ่มนึง" in norm_time_text or "1ทุ่ม" in norm_time_text:
                hour = 19
            elif evening_m:
                hour = 12 + int(evening_m.group(1))
            elif morning_m:
                hour = int(morning_m.group(1))
            if "ครึ่ง" in norm_time_text and hour is not None:
                minute = 30

    if hour is None or not (0 <= hour <= 23):
        return None

    result_dt = datetime(
        target_date.year, target_date.month, target_date.day,
        hour, minute, tzinfo=THAI_TZ
    )
    # ถ้าไม่ได้ระบุวันชัดเจน (พิมพ์แค่เวลา) แล้วเวลานั้นของวันนี้ผ่านไปแล้ว ให้เลื่อนเป็นวันพรุ่งนี้
    if not explicit_date and result_dt < ref_dt:
        result_dt += timedelta(days=1)

    return result_dt

def extract_mentions(text: str) -> list[str]:
    """ดึงรายการ <@user_id> ทั้งหมดจากข้อความโดยรักษาลำดับและไม่ซ้ำกัน"""
    if not text:
        return []
    found = re.findall(r'<@!?\d+>', text)
    unique = []
    for m in found:
        if m not in unique:
            unique.append(m)
    return unique

def parse_meetup_embed(embed: discord.Embed) -> dict:
    """อ่านข้อมูลทั้งหมดจากการ์ด Embed นัดหมาย"""
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
    friend_notes = "*ยังไม่มีหมายเหตุ*"
    created_by = "ไม่ทราบ"

    for field in embed.fields:
        fname = field.name or ""
        fval = field.value or ""
        if "สถานที่" in fname:
            location = fval.replace("```", "").strip()
        elif "เวลานัด" in fname:
            time_val = fval.replace("```", "").strip()
        elif "📌 หมายเหตุ" in fname:
            pin_note = re.sub(r'^>\s*', '', fval).strip()
        elif "จำนวนคน" in fname:
            m_limit = re.search(r'(\d+)\s*/\s*(\d+)', fval)
            if m_limit:
                max_limit = int(m_limit.group(2))
        elif "ใครไปบ้าง" in fname:
            going = extract_mentions(fval)
        elif "รายชื่อคิวสำรอง" in fname or (fname.startswith("⏳") and "คิวที่" in fval):
            waitlist = extract_mentions(fval)
        elif "ไม่แน่ใจ / ขอดูก่อน" in fname:
            tentative = extract_mentions(fval)
        elif "เช็คอินแล้ว" in fname:
            checkin_mode = True
        elif "เช็คอินถึงที่นัดแล้ว" in fname:
            checkin_mode = True
            for m in extract_mentions(fval):
                if not any(c["mention"] == m for c in checkins):
                    checkins.append({"mention": m, "rest": ""})
        elif "หมายเหตุจากเพื่อนๆ" in fname:
            friend_notes = fval

    if embed.footer and embed.footer.text:
        created_by = embed.footer.text.replace("🎯 สร้างโดย", "").split("•")[0].strip()

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
    """อัปเดตหรือจัดเรียง Fields ในการ์ดนัดหมายให้สวยงามและรองรับโหมดลงชื่อ / โหมดเช็คอิน"""
    location = info.get("location", "-")
    time_val = info.get("time_val", "-")
    pin_note = info.get("pin_note")
    going = info.get("going", [])
    waitlist = info.get("waitlist", [])
    tentative = info.get("tentative", [])
    checkins = info.get("checkins", [])
    checkin_mode = info.get("checkin_mode", False)
    max_limit = info.get("max_limit")
    friend_notes = info.get("friend_notes") or "*ยังไม่มีหมายเหตุ*"
    count = len(going)

    if checkin_mode:
        if count > 0 and len(checkins) >= count:
            status_line = "🟢 **สถานะ:** ถึงเวลานัดหมายแล้ว (มากันครบทุกคนแล้ว! 🎉)"
        else:
            status_line = "🟢 **สถานะ:** ถึงเวลานัดหมายแล้ว (กำลังเปิดเช็คอินหน้างาน 📍)"
    elif max_limit and max_limit > 0 and count >= max_limit:
        status_line = "🟠 **สถานะ:** จำนวนคนเต็มแล้ว (กดไปด้วยเพื่อเข้าคิวสำรอง ⏳)"
    else:
        status_line = "🔵 **สถานะ:** เปิดรับลงชื่อเข้าร่วมนัดหมาย"

    new_embed = discord.Embed(
        title=embed.title,
        description=f"{status_line}\n━━━━━━━━━━━━━━━━━━━━━━",
        color=_theme_color(info)
    )
    if embed.author and embed.author.name:
        new_embed.set_author(name=embed.author.name, icon_url=embed.author.icon_url)
    if embed.thumbnail and embed.thumbnail.url:
        new_embed.set_thumbnail(url=embed.thumbnail.url)
    if embed.footer and embed.footer.text:
        new_embed.set_footer(text=embed.footer.text)

    new_embed.add_field(name=f"{EMOJI['location']} สถานที่", value=f"```{location}```", inline=True)
    new_embed.add_field(name=f"{EMOJI['time']} เวลานัด", value=f"```{time_val}```", inline=True)

    if pin_note:
        new_embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        new_embed.add_field(name="📌 หมายเหตุ", value=f"> {pin_note}", inline=False)

    new_embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)

    # จำนวนคน & จำกัดจำนวนคน & สถานะเช็คอิน (แสดงแบบ Progress Bar เสมอ)
    prog = _progress_bar(count, max_limit, length=10)
    count_str = f"` {prog} `"

    new_embed.add_field(name=f"{EMOJI['count']} จำนวนคน", value=count_str, inline=True)
    if checkin_mode:
        checkin_bar = _progress_bar(len(checkins), count, length=8) if count > 0 else "0/0 คน"
        new_embed.add_field(name=f"{EMOJI['checkin']} เช็คอินแล้ว", value=f"` {checkin_bar} `", inline=True)
        new_embed.add_field(name="\u200b", value="\u200b", inline=True)
    else:
        new_embed.add_field(name=f"{EMOJI['tentative']} ไม่แน่ใจ", value=f"` {len(tentative)} คน `", inline=True)
        if max_limit and max_limit > 0:
            new_embed.add_field(name=f"{EMOJI['waitlist']} คิวสำรอง", value=f"` {len(waitlist)} คน `", inline=True)
        else:
            new_embed.add_field(name="\u200b", value="\u200b", inline=True)

    # รายชื่อคนที่ไปแน่นอน
    if going:
        going_val = ">>> " + "\n".join(going)
    else:
        going_val = "*ยังไม่มีใครลงชื่อ*"
    new_embed.add_field(name="👥 ใครไปบ้าง (ไปแน่นอน)", value=going_val[:1024], inline=False)

    # แสดงรายชื่อคิวสำรอง และ ไม่แน่ใจ / ขอดูก่อน เฉพาะตอนเปิดรับลงชื่อปกติ (เมื่อถึงเวลานัดหมายแล้วจะซ่อนออก)
    if not checkin_mode:
        if (max_limit and max_limit > 0) or waitlist:
            if waitlist:
                waitlist_val = "\n".join([f"`คิวที่ {idx}:` {m}" for idx, m in enumerate(waitlist, 1)])
            else:
                waitlist_val = "*ว่าง (ยังไม่มีคิวสำรอง)*"
            new_embed.add_field(name="⏳ รายชื่อคิวสำรอง (เลื่อนขึ้นอัตโนมัติเมื่อมีคนสละสิทธิ์)", value=waitlist_val[:1024], inline=False)

        if tentative:
            tentative_val = ">>> " + "\n".join(tentative)
        else:
            tentative_val = "*ยังไม่มี*"
        new_embed.add_field(name="🤔 ไม่แน่ใจ / ขอดูก่อน", value=tentative_val[:1024], inline=False)

    # รายชื่อคนที่เช็คอินถึงที่นัดแล้ว (แสดงเฉพาะเมื่อเปิดโหมดเช็คอินแล้ว หรือมีคนเช็คอิน)
    if checkin_mode or checkins:
        if checkins:
            checkin_lines = [
                f"`{idx}.` {c['mention']}"
                for idx, c in enumerate(checkins, 1)
            ]
            if count > 0 and len(checkins) >= count:
                checkin_lines.append("🎉 **มากันครบทุกคนแล้ว!**")
            checkin_val = "\n".join(checkin_lines)
        else:
            checkin_val = "*ยังไม่มีใครกดเช็คอิน*"
        new_embed.add_field(name="📍 เช็คอินถึงที่นัดแล้ว", value=checkin_val[:1024], inline=False)

    new_embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
    new_embed.add_field(name="📋 หมายเหตุจากเพื่อนๆ", value=friend_notes[:1024], inline=False)

    return new_embed

def upsert_meetup_history(
    embed: discord.Embed,
    guild_id,
    message_id: int = None,
    channel_id: int = None,
    custom_remind_at: datetime = None,
    reset_reminded: bool = False
):
    """บันทึกหรืออัปเดตข้อมูลนัดหมายลง meetup_history.json (บันทึกเฉพาะนัดหมายที่ยืนยันแล้วเท่านั้น)"""
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
        # ตั้งเตือนล่วงหน้าอัตโนมัติ 30 นาทีก่อนถึงเวลานัด
        auto_remind_dt = meetup_dt - timedelta(minutes=30)
        remind_at_iso = auto_remind_dt.isoformat()
    else:
        remind_at_iso = None

    history = load_json(HISTORY_FILE, [])
    found_entry = None

    # ค้นหาจาก message_id ก่อน ถ้าไม่มีค่อยหาจาก topic + location + guild_id
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

# ==================== Views & Modals สำหรับการนัดหมาย ====================

async def _open_notify_panel(interaction: discord.Interaction):
    """เปิดแผงควบคุมการแจ้งเตือนสำหรับการ์ดนัดหมาย"""
    embed = interaction.message.embeds[0]
    info = parse_meetup_embed(embed)

    history = load_json(HISTORY_FILE, [])
    msg_str = str(interaction.message.id)
    remind_status = "ยังไม่ได้ตั้งเวลาเตือน (หรือรูปแบบเวลาไม่ระบุชัดเจน)"
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
                    state_txt = "✅ แจ้งเตือนไปแล้ว" if reminded else "⏳ รอแจ้งเตือนอัตโนมัติ"
                    remind_status = f"`{r_dt.strftime('%d/%m/%Y %H:%M')} น.` ({state_txt})"
                except Exception:
                    pass
            break

    panel_embed = discord.Embed(
        title=f"🔔 จัดการการแจ้งเตือน: {info['topic']}",
        description=(
            "เลือกรูปแบบการแจ้งเตือนด้านล่าง เพื่อลดการรบกวนคนอื่นในเซิร์ฟเวอร์:\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 **คนที่ไปแน่นอน:** `{len(info['going'])} คน` (เช็คอินแล้ว `{len(info['checkins'])} คน`)\n"
            f"🤔 **ไม่แน่ใจ / คิวสำรอง:** `{len(info['tentative']) + len(info['waitlist'])} คน`\n"
            f"⏰ **ตั้งเวลาเตือนอัตโนมัติ:** {remind_status}"
        ),
        color=0x5865F2
    )
    view = SmartNotifyView(interaction.message)
    await interaction.response.send_message(embed=panel_embed, view=view, ephemeral=True)


async def _open_update_options(interaction: discord.Interaction):
    """เปิดแผงตัวเลือกในปุ่ม '✏️ อัพเดทข้อมูล' เพื่อเลือกเปลี่ยนเป็นโหมดถึงเวลานัด (เช็คอิน) หรือแก้ไขข้อมูล"""
    embed = interaction.message.embeds[0]
    info = parse_meetup_embed(embed)
    mode_str = "📍 โหมดเช็คอินหน้างาน (ถึงเวลานัดหมายแล้ว)" if info["checkin_mode"] else "📝 โหมดเปิดรับลงชื่อปกติ"
    panel_embed = discord.Embed(
        title=f"✏️ อัพเดทข้อมูลนัดหมาย: {info['topic']}",
        description=(
            f"สถานะปัจจุบัน: **{mode_str}**\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• กด **📍 ถึงเวลามาที่นัดหมายแล้ว** เพื่อเปิดปุ่มเช็คอิน และซ่อนปุ่ม ไปด้วย / ไม่ไปแล้ว / ไม่แน่ใจ\n"
            "• กด **📝 แก้ไขสถานที่ / เวลา / จำนวนคน** เพื่อแก้ไขรายละเอียดนัดหมาย"
        ),
        color=0x5865F2
    )
    view = UpdateMeetupOptionsView(interaction.message, is_checkin_mode=info["checkin_mode"])
    await interaction.response.send_message(embed=panel_embed, view=view, ephemeral=True)


class MeetupView(discord.ui.View):
    """ปุ่มสำหรับการ์ดนัดหมายในช่วงเปิดรับลงชื่อปกติ (ก่อนถึงเวลานัด)
    จัดกลุ่มปุ่มเป็น Row ชัดเจน:
    - Row 0: ปุ่มตัดสินใจหลัก (ไปด้วย! / ไม่ไปแล้ว)
    - Row 1: ปุ่มสถานะสำรอง (ไม่แน่ใจ / ขอดูก่อน)
    - Row 2: ปุ่มเครื่องมือจัดการ (เพิ่มหมายเหตุ / แจ้งเตือนอีกครั้ง / อัพเดทข้อมูล)
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ไปด้วย!", style=discord.ButtonStyle.green, custom_id="join_meetup", row=0, emoji=EMOJI["join"])
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        user_mention = interaction.user.mention

        if user_mention in info["going"]:
            await interaction.response.send_message("❌ คุณลงชื่อว่า **ไปด้วย** อยู่แล้วนะ!", ephemeral=True)
            return
        if user_mention in info["waitlist"]:
            pos = info["waitlist"].index(user_mention) + 1
            await interaction.response.send_message(
                f"⏳ คุณอยู่ใน **คิวสำรองลำดับที่ {pos}** อยู่แล้วครับ (หากมีคนสละสิทธิ์ ระบบจะเลื่อนขึ้นให้อัตโนมัติ)",
                ephemeral=True
            )
            return

        # ลบออกจากรายชื่อไม่แน่ใจ (ถ้ามี)
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
                f"⚠️ จำนวนคนเต็มแล้ว (`{max_limit}/{max_limit} คน`)!\n"
                f"บอทได้เพิ่มคุณเข้าสู่ **⏳ คิวสำรองลำดับที่ {pos}** เรียบร้อยครับ หากมีคนกดยกเลิก ระบบจะเลื่อนชื่อคุณขึ้นและแท็กแจ้งเตือนอัตโนมัติ!",
                ephemeral=True
            )

    @discord.ui.button(label="ไม่ไปแล้ว", style=discord.ButtonStyle.red, custom_id="leave_meetup", row=0, emoji=EMOJI["leave"])
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
            await interaction.response.send_message("❌ คุณยังไม่ได้ลงชื่อในนัดหมายนี้เลยนะ!", ephemeral=True)
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
                f"🎉 {mentions_str} มีที่ว่างแล้ว! คุณได้รับการเลื่อนจาก **คิวสำรอง** เข้าสู่รายชื่อผู้เข้าร่วมนัดหมาย **{info['topic']}** อัตโนมัติครับ",
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    @discord.ui.button(label="ไม่แน่ใจ / ขอดูก่อน", style=discord.ButtonStyle.primary, custom_id="tentative_meetup", row=1, emoji=EMOJI["tentative"])
    async def tentative_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        user_mention = interaction.user.mention

        if user_mention in info["tentative"]:
            await interaction.response.send_message("🤔 คุณอยู่ในรายชื่อ **ไม่แน่ใจ / ขอดูก่อน** อยู่แล้วนะ!", ephemeral=True)
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
                f"🎉 {mentions_str} มีที่ว่างแล้ว! คุณได้รับการเลื่อนจาก **คิวสำรอง** เข้าสู่รายชื่อผู้เข้าร่วมนัดหมาย **{info['topic']}** อัตโนมัติครับ",
                allowed_mentions=discord.AllowedMentions(users=True)
            )

    @discord.ui.button(label="เพิ่มหมายเหตุ", style=discord.ButtonStyle.secondary, custom_id="note_meetup", row=2, emoji=EMOJI["note"])
    async def note_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NoteModal())

    @discord.ui.button(label="แจ้งเตือนอีกครั้ง", style=discord.ButtonStyle.secondary, custom_id="notify_meetup", row=2, emoji=EMOJI["notify"])
    async def notify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_notify_panel(interaction)

    @discord.ui.button(label="อัพเดทข้อมูล", style=discord.ButtonStyle.secondary, custom_id="edit_meetup", row=2, emoji=EMOJI["edit"])
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_update_options(interaction)


class CheckinMeetupView(discord.ui.View):
    """
    ปุ่มสำหรับการ์ดนัดหมายเมื่อเปิดโหมด 'ถึงเวลามาที่นัดหมายแล้ว'
    จัดกลุ่มปุ่มเป็น Row ชัดเจน:
    - Row 0: ปุ่มเช็คอินหน้างาน (📍 ถึงที่นัดแล้ว!)
    - Row 1: ปุ่มเครื่องมือจัดการ (🔔 แจ้งเตือนอีกครั้ง / ✏️ อัพเดทข้อมูล)
    """
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="ถึงที่นัดแล้ว!", style=discord.ButtonStyle.green, custom_id="checkin_meetup_btn", row=0, emoji=EMOJI["checkin"])
    async def checkin_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        info["checkin_mode"] = True
        user_mention = interaction.user.mention
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]

        # กรณีเช็คอินไปแล้ว
        if user_mention in checked_in_mentions:
            not_arrived = [m for m in info["going"] if m not in checked_in_mentions]
            if not_arrived:
                await interaction.response.send_message(
                    f"📍 คุณเช็คอินไปแล้วครับ! ตอนนี้มาถึงแล้ว **{len(checked_in_mentions)}/{len(info['going'])} คน**\n"
                    f"🏃‍♂️ ยังมาไม่ถึงอีก **{len(not_arrived)} คน** — กดปุ่มด้านล่างเพื่อแท็กตามเพื่อนได้เลย!",
                    view=PingNotArrivedView(interaction.message),
                    ephemeral=True
                )
            else:
                await interaction.response.send_message(
                    "📍 คุณเช็คอินไปแล้ว และตอนนี้ทุกคนมาถึงครบแล้วครับ! 🎉",
                    ephemeral=True
                )
            return

        # หากยังไม่ได้อยู่ในรายชื่อ going ให้เพิ่มเข้า going อัตโนมัติเมื่อมาถึงหน้างานจริง
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
                f"📍 **เช็คอินถึงที่นัดหมายสำเร็จ!**\n"
                f"👥 มาถึงแล้ว **{len(checked_in_mentions)}/{len(info['going'])} คน** • ยังมาไม่ถึงอีก **{len(not_arrived)} คน**\n"
                f"💡 กดปุ่มด้านล่างเพื่อแท็กตามเฉพาะเพื่อนที่ยังมาไม่ถึงได้เลยครับ!",
                view=PingNotArrivedView(interaction.message),
                ephemeral=True
            )
        else:
            await interaction.followup.send(
                "📍 **เช็คอินสำเร็จ!**\n"
                "🎉 **ตอนนี้ทุกคนมาถึงที่นัดหมายครบแล้วครับ!**",
                ephemeral=True
            )
            if len(info["going"]) > 1 and interaction.channel:
                await interaction.channel.send(
                    f"🎉 **มากันครบแก๊งแล้ว!** ผู้เข้าร่วมนัดหมาย **{info['topic']}** เช็คอินถึง `{info['location']}` ครบทั้ง **{len(info['going'])} คน** แล้วครับ ลุยยย!"
                )

    @discord.ui.button(label="แจ้งเตือนอีกครั้ง", style=discord.ButtonStyle.secondary, custom_id="checkin_notify_btn", row=1, emoji=EMOJI["notify"])
    async def notify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_notify_panel(interaction)

    @discord.ui.button(label="อัพเดทข้อมูล", style=discord.ButtonStyle.secondary, custom_id="checkin_edit_btn", row=1, emoji=EMOJI["edit"])
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await _open_update_options(interaction)


class UpdateMeetupOptionsView(discord.ui.View):
    """เมนูย่อยเมื่อกดปุ่ม '✏️ อัพเดทข้อมูล'"""
    def __init__(self, meetup_message: discord.Message, is_checkin_mode: bool = False):
        super().__init__(timeout=180)
        self.meetup_message = meetup_message
        self.is_checkin_mode = is_checkin_mode
        if is_checkin_mode:
            self.toggle_mode_btn.label = "🔄 กลับไปโหมดลงชื่อปกติ (ไปด้วย/ไม่ไปแล้ว)"
            self.toggle_mode_btn.style = discord.ButtonStyle.primary
        else:
            self.toggle_mode_btn.label = "📍 ถึงเวลามาที่นัดหมายแล้ว (เปิดเช็คอิน)"
            self.toggle_mode_btn.style = discord.ButtonStyle.success

    @discord.ui.button(label="📍 ถึงเวลามาที่นัดหมายแล้ว (เปิดเช็คอิน)", style=discord.ButtonStyle.success, row=0)
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
                    "✅ **เปลี่ยนเป็นโหมดถึงเวลานัดหมายแล้ว!**\n"
                    "ตอนนี้ในการ์ดเพิ่มปุ่ม **📍 ถึงที่นัดแล้ว!** และลบปุ่ม ไปด้วย / ไม่ไปแล้ว / ไม่แน่ใจ ออกเรียบร้อยครับ"
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
                content="✅ **เปลี่ยนกลับเป็นโหมดเปิดรับลงชื่อปกติเรียบร้อยครับ!**",
                embed=None,
                view=None
            )

    @discord.ui.button(label="📝 แก้ไขสถานที่ / เวลา / จำนวนคน", style=discord.ButtonStyle.secondary, row=1)
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
    """ปุ่มด่วนสำหรับแท็กตามเฉพาะเพื่อนที่ลงชื่อว่าไป แต่ยังไม่ได้กดเช็คอิน"""
    def __init__(self, meetup_message: discord.Message):
        super().__init__(timeout=180)
        self.meetup_message = meetup_message

    @discord.ui.button(label="🏃‍♂️ แท็กตามเพื่อนที่ยังไม่ถึง!", style=discord.ButtonStyle.primary)
    async def ping_not_arrived(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]
        not_arrived = [m for m in info["going"] if m not in checked_in_mentions]

        if not not_arrived:
            await interaction.response.edit_message(
                content="🎉 **ทุกคนเช็คอินถึงที่นัดหมายครบหมดแล้วครับ!**",
                view=None
            )
            return

        mentions_str = " ".join(not_arrived)
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🏃‍♂️ ตามเพื่อนเข้าตี้: {info['topic']}",
            description=(
                f"📍 **สถานที่:** `{info['location']}`\n"
                f"⏰ **เวลานัด:** `{info['time_val']}`\n"
                f"✅ **มาถึงแล้ว ({len(checked_in_mentions)}/{len(info['going'])} คน):** {' '.join(checked_in_mentions) if checked_in_mentions else '-'}\n"
                f"💡 *ใครมาถึงแล้วอย่าลืมกดปุ่ม **📍 ถึงที่นัดแล้ว!** ในการ์ดนัดหมายนะครับ*\n"
                f"🔗 [คลิกเพื่อเปิดการ์ดนัดหมาย]({jump_url})"
            ),
            color=0xE67E22
        )
        await interaction.channel.send(
            content=f"📣 {interaction.user.mention} รออยู่ที่ **`{info['location']}`** แล้ว! ตามมาเร็วๆ ครับ: {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **แท็กตามเพื่อนที่ยังมาไม่ถึงเรียบร้อยแล้วครับ!**",
            view=None
        )


async def _send_dm_to_mentions(client: discord.Client, mentions: list[str], embed: discord.Embed, content: str = None) -> tuple[int, int]:
    """ส่งข้อความแจ้งเตือนทาง DM ไปยังรายการ Mention (<@user_id>) คืนค่า (จำนวนที่ส่งสำเร็จ, จำนวนทั้งหมด)"""
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
    """เมนูเลือกแท็กเฉพาะคนที่ลงชื่อไป ส่ง DM หรือตั้งเวลาเตือนล่วงหน้าอัตโนมัติ"""
    def __init__(self, meetup_message: discord.Message):
        super().__init__(timeout=180)
        self.meetup_message = meetup_message

    @discord.ui.button(label="🎯 แท็กเฉพาะคนที่ไป (Going)", style=discord.ButtonStyle.green, row=0)
    async def ping_going(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        if not info["going"]:
            await interaction.response.send_message("❌ ยังไม่มีใครกด **✅ ไปด้วย!** ในนัดหมายนี้เลยครับ", ephemeral=True)
            return

        mentions_str = " ".join(info["going"])
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🔔 แจ้งเตือนผู้เข้าร่วมนัดหมาย: {info['topic']}",
            description=(
                f"📍 **สถานที่:** `{info['location']}`\n"
                f"⏰ **เวลานัด:** `{info['time_val']}`\n"
                f"🔗 [คลิกเพื่อดูการ์ดนัดหมาย]({jump_url})"
            ),
            color=0x2ECC71
        )
        await interaction.channel.send(
            content=f"📣 **แจ้งเตือนนัดหมายสำหรับผู้ที่ลงชื่อไว้!** {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **ส่งแจ้งเตือนเฉพาะคนที่กด 'ไปด้วย' เรียบร้อยแล้วครับ!**",
            embed=None,
            view=None
        )

    @discord.ui.button(label="🤔 แท็กคนที่ไป + คนที่ไม่แน่ใจ", style=discord.ButtonStyle.primary, row=0)
    async def ping_going_and_tentative(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        targets = []
        for m in info["going"] + info["tentative"] + info["waitlist"]:
            if m not in targets:
                targets.append(m)

        if not targets:
            await interaction.response.send_message("❌ ยังไม่มีใครลงชื่อหรือกดไม่แน่ใจในนัดหมายนี้เลยครับ", ephemeral=True)
            return

        mentions_str = " ".join(targets)
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🔔 แจ้งเตือนและเช็คชื่อนัดหมาย: {info['topic']}",
            description=(
                f"📍 **สถานที่:** `{info['location']}`\n"
                f"⏰ **เวลานัด:** `{info['time_val']}`\n"
                f"💬 สำหรับคนที่กด **🤔 ไม่แน่ใจ** ไว้ อย่าลืมแวะมายืนยันในการ์ดนัดหมายนะครับ!\n"
                f"🔗 [คลิกเพื่อไปที่การ์ดนัดหมาย]({jump_url})"
            ),
            color=0x5865F2
        )
        await interaction.channel.send(
            content=f"📣 **แจ้งเตือนนัดหมาย!** {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **ส่งแจ้งเตือนคนที่ไปและคนที่ไม่แน่ใจเรียบร้อยแล้วครับ!**",
            embed=None,
            view=None
        )

    @discord.ui.button(label="🏃‍♂️ แท็กตามคนที่ยังไม่เช็คอิน", style=discord.ButtonStyle.secondary, row=0)
    async def ping_not_checked_in(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]
        not_arrived = [m for m in info["going"] if m not in checked_in_mentions]

        if not info["going"]:
            await interaction.response.send_message("❌ ยังไม่มีใครลงชื่อว่าไปในนัดหมายนี้เลยครับ", ephemeral=True)
            return
        if not not_arrived:
            await interaction.response.send_message("🎉 ผู้เข้าร่วมทุกคนเช็คอินถึงที่นัดหมายครบหมดแล้วครับ!", ephemeral=True)
            return

        mentions_str = " ".join(not_arrived)
        jump_url = self.meetup_message.jump_url
        alert_embed = discord.Embed(
            title=f"🏃‍♂️ ตามเพื่อนที่ยังมาไม่ถึง: {info['topic']}",
            description=(
                f"📍 **สถานที่:** `{info['location']}`\n"
                f"⏰ **เวลานัด:** `{info['time_val']}`\n"
                f"✅ **เช็คอินถึงแล้ว:** `{len(checked_in_mentions)}/{len(info['going'])} คน`\n"
                f"🔗 [คลิกเพื่อกดเช็คอินในการ์ดนัดหมาย]({jump_url})"
            ),
            color=0xE67E22
        )
        await interaction.channel.send(
            content=f"📣 **ตามหาคนยังไม่ถึงที่นัดหมาย!** รีบตามมาเร็วๆ ครับ: {mentions_str}",
            embed=alert_embed,
            allowed_mentions=discord.AllowedMentions(users=True)
        )
        await interaction.response.edit_message(
            content="✅ **แท็กตามคนที่ยังไม่กดเช็คอินเรียบร้อยแล้วครับ!**",
            embed=None,
            view=None
        )

    @discord.ui.button(label="💌 ส่งแจ้งเตือนทางแชทส่วนตัว (DM)", style=discord.ButtonStyle.success, row=1)
    async def send_dm_alert(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        checked_in_mentions = [c["mention"] for c in info.get("checkins", [])]
        targets = [m for m in info["going"] if m not in checked_in_mentions] if info.get("checkin_mode") else info["going"]

        if not targets:
            msg_txt = "🎉 ทุกคนเช็คอินถึงที่นัดหมายครบหมดแล้วครับ!" if info.get("checkin_mode") else "❌ ยังไม่มีใครลงชื่อว่า **ไปด้วย** ในนัดหมายนี้เลยครับ"
            await interaction.response.send_message(msg_txt, ephemeral=True)
            return

        await interaction.response.edit_message(
            content=f"⏳ **กำลังส่งข้อความแจ้งเตือนทาง DM ให้เพื่อน `{len(targets)} คน`...**",
            embed=None,
            view=None
        )

        jump_url = self.meetup_message.jump_url
        guild_name = interaction.guild.name if interaction.guild else "เซิร์ฟเวอร์ Discord"
        dm_embed = discord.Embed(
            title=f"🔔 แจ้งเตือนนัดหมาย: {info['topic']}",
            description=(
                f"🏠 **เซิร์ฟเวอร์:** `{guild_name}`\n"
                f"📍 **สถานที่:** `{info['location']}`\n"
                f"⏰ **เวลานัด:** `{info['time_val']}`\n"
                f"👤 **ส่งแจ้งเตือนโดย:** {interaction.user.mention}\n\n"
                f"🔗 [คลิกที่นี่เพื่อเปิดการ์ดนัดหมาย]({jump_url})"
            ),
            color=_theme_color(info)
        )
        dm_embed.set_footer(text="💬 ข้อความแจ้งเตือนอัตโนมัติทางแชทส่วนตัว (DM)")

        sent_ok, total_cnt = await _send_dm_to_mentions(
            interaction.client,
            targets,
            dm_embed,
            content=f"💌 **แจ้งเตือนนัดหมาย `{info['topic']}` ครับ!**"
        )
        await interaction.edit_original_response(
            content=f"✅ **ส่งแจ้งเตือนทาง DM สำเร็จ `{sent_ok}/{total_cnt} คน` เรียบร้อยแล้วครับ!**"
        )

    @discord.ui.button(label="⏰ ตั้งเวลาเตือนล่วงหน้าอัตโนมัติ", style=discord.ButtonStyle.secondary, row=1)
    async def set_auto_reminder(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(SetReminderModal(self.meetup_message))

    @discord.ui.button(label="📢 ดันโพสต์ + แท็ก @everyone", style=discord.ButtonStyle.danger, row=2)
    async def ping_everyone_bump(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = self.meetup_message.embeds[0]
        info = parse_meetup_embed(embed)
        target_view = CheckinMeetupView() if info.get("checkin_mode") else MeetupView()
        await interaction.response.edit_message(content="✅ **ดันโพสต์แจ้งเตือน @everyone เรียบร้อยครับ**", embed=None, view=None)
        try:
            new_msg = await interaction.channel.send(
                content="@everyone 🔔 **มีการแจ้งเตือนนัดหมาย!**",
                embed=embed,
                view=target_view,
                allowed_mentions=discord.AllowedMentions(everyone=True)
            )
            # อัปเดต message_id ใหม่ในประวัติเพื่อให้ระบบเตือนอัตโนมัติอ้างอิงข้อความใหม่ได้ถูกต้อง
            old_id = str(self.meetup_message.id)
            history = load_json(HISTORY_FILE, [])
            for entry in history:
                if entry.get("message_id") == old_id:
                    entry["message_id"] = str(new_msg.id)
            save_json(HISTORY_FILE, history)

            await self.meetup_message.delete()
        except discord.errors.Forbidden:
            await interaction.followup.send(
                "❌ **บอทไม่มีสิทธิ์ (Permission) ในการส่งหรือลบข้อความในห้องนี้ครับ**\n"
                "โปรดให้สิทธิ์ `Send Messages` และ `Manage Messages` กับบอทด้วยครับ",
                ephemeral=True
            )
        except Exception as e:
            print(f"Error in ping_everyone_bump: {e}")


class SetReminderModal(discord.ui.Modal, title='⏰ ตั้งเวลาแจ้งเตือนผู้เข้าร่วมอัตโนมัติ'):
    remind_input = discord.ui.TextInput(
        label='เวลาแจ้งเตือน (นาทีจากตอนนี้ หรือ เวลา HH:MM)',
        style=discord.TextStyle.short,
        placeholder='เช่น 30 (อีก 30 นาที), หรือ 19:30, หรือ วันนี้ 18:00',
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

        m_mins = re.match(r'^(?:อีก\s*)?(\d+)\s*(?:นาที|min|mins|m)?$', raw, re.IGNORECASE)
        if m_mins:
            mins = int(m_mins.group(1))
            if mins <= 0 or mins > 10080:
                await interaction.response.send_message("❌ กรุณาระบุจำนวนนาทีระหว่าง 1 ถึง 10080 นาที (7 วัน) ครับ", ephemeral=True)
                return
            target_dt = now + timedelta(minutes=mins)
        else:
            target_dt = parse_thai_meetup_datetime(raw, now)

        if not target_dt or target_dt <= now:
            await interaction.response.send_message(
                "❌ ไม่สามารถแปลงเวลาได้ หรือเวลาที่ระบุผ่านไปแล้วครับ\n"
                "💡 **ตัวอย่างที่รองรับ:** `30` (อีก 30 นาที), `19:30`, `วันนี้ 18:00`, `พรุ่งนี้ 11:00`",
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
            f"✅ **ตั้งเวลาแจ้งเตือนอัตโนมัติสำเร็จ!**\n"
            f"⏰ บอทจะแท็กแจ้งเตือนรายชื่อคนที่ลงชื่อไว้เมื่อถึงเวลา **`{target_dt.strftime('%d/%m/%Y %H:%M')} น.`** ครับ",
            ephemeral=True
        )


class DraftMeetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="แก้ไขข้อมูล", style=discord.ButtonStyle.secondary, custom_id="draft_edit_meetup", row=0, emoji=EMOJI["edit"])
    async def edit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        max_str = str(info["max_limit"]) if info["max_limit"] else ""
        await interaction.response.send_modal(EditMeetupModal(info["location"], info["time_val"], max_str, is_draft=True))

    @discord.ui.button(label="ยืนยันข้อมูลนัด", style=discord.ButtonStyle.success, custom_id="draft_confirm_meetup", row=0, emoji=EMOJI["confirm"])
    async def confirm_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)

        # กำหนดสีธีมให้สอดคล้องกับสถานะการนัดหมาย
        embed.color = _theme_color(info)

        # คำนวณเวลาเตือนล่วงหน้าอัตโนมัติ (ก่อนเวลานัด 30 นาที)
        meetup_dt = parse_thai_meetup_datetime(info["time_val"])
        now = now_thai()
        auto_remind_note = ""
        if meetup_dt and (meetup_dt - timedelta(minutes=30)) > now:
            remind_dt = meetup_dt - timedelta(minutes=30)
            auto_remind_note = f"  •  🔔 เตือนอัตโนมัติ {remind_dt.strftime('%H:%M')} น."

        # เปลี่ยนข้อความ Footer และ Author กลับเป็นแบบปกติ
        footer_text = embed.footer.text.replace("กดยืนยันเพื่อส่งลงห้องแชท", "กดปุ่มด้านล่างเพื่อลงชื่อ!") if embed.footer else ""
        embed.set_footer(text=footer_text + auto_remind_note)

        author_name = embed.author.name.replace("ร่าง", "") if (embed.author and embed.author.name) else "🔔 การนัดหมาย"
        icon_url = embed.author.icon_url if embed.author else None
        embed.set_author(name=author_name, icon_url=icon_url)

        try:
            confirm_msg = "✅ **ส่งการนัดหมายลงช่องแชทแล้วครับ!**"
            if auto_remind_note:
                confirm_msg += f"\n⏰ *(ระบบตั้งเวลาแจ้งเตือนผู้เข้าร่วมอัตโนมัติก่อนเวลานัด 30 นาทีแล้ว)*"
            await interaction.response.edit_message(content=confirm_msg, embed=None, view=None)

            sent_msg = await interaction.channel.send(
                content="@everyone 📢 **มีการนัดหมายใหม่!**",
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
                "❌ **บอทไม่มีสิทธิ์ (Permission) ส่งข้อความในห้องนี้ครับ**\nโปรดให้สิทธิ์ `Send Messages` กับบอทด้วยครับ",
                ephemeral=True
            )
        except Exception as e:
            await interaction.followup.send(f"❌ **เกิดข้อผิดพลาดในการส่งข้อความ:** {e}", ephemeral=True)
            print(f"Error in confirm_button: {e}")


class EditMeetupModal(discord.ui.Modal, title='✏️ แก้ไขข้อมูลนัดหมาย'):
    location = discord.ui.TextInput(
        label='สถานที่',
        style=discord.TextStyle.short,
        placeholder='เช่น หอ A, ร้านประจำ, Major ลพบุรี',
        required=True,
        max_length=100,
    )
    time_str = discord.ui.TextInput(
        label='เวลา (ระบุ HH:MM เพื่อเตือนล่วงหน้า 30 นาที)',
        style=discord.TextStyle.short,
        placeholder='เช่น วันนี้ 20:00, พรุ่งนี้ 14:30',
        required=True,
        max_length=50,
    )
    max_participants = discord.ui.TextInput(
        label='จำนวนคนสูงสุด (เว้นว่าง = ไม่จำกัด)',
        style=discord.TextStyle.short,
        placeholder='เช่น 4, 5, 10 (ถ้าเต็มจะเข้าคิวสำรองอัตโนมัติ)',
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
                await interaction.response.send_message("❌ จำนวนคนสูงสุดต้องเป็นตัวเลขจำนวนเต็มบวก เช่น `4` หรือ `5` ครับ", ephemeral=True)
                return
            info["max_limit"] = int(max_raw)
        else:
            info["max_limit"] = None

        # จัดการเลื่อนคิวสำรองกรณีขยายจำนวนคน หรือย้ายไปคิวสำรองกรณีลดจำนวนคนสูงสุด
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
                content="✅ **บันทึกการแก้ไขข้อมูลนัดหมายเรียบร้อยครับ!**",
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
                    f"🎉 {mentions_str} มีการขยายจำนวนที่นั่ง! คุณได้รับการเลื่อนจาก **คิวสำรอง** เข้าสู่รายชื่อผู้เข้าร่วมนัดหมาย **{info['topic']}** แล้วครับ",
                    allowed_mentions=discord.AllowedMentions(users=True)
                )


class NoteModal(discord.ui.Modal, title='📝 เพิ่มหมายเหตุ'):
    note_text = discord.ui.TextInput(
        label='พิมพ์หมายเหตุของคุณ',
        style=discord.TextStyle.long,
        placeholder='เช่น ขอไปสาย 10 นาทีนะ, เอารถไป 2 คัน...',
        required=True,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]
        info = parse_meetup_embed(embed)
        note_str = f"💬 **{interaction.user.display_name}:** {self.note_text.value}"

        current_notes = info["friend_notes"]
        if current_notes == "*ยังไม่มีหมายเหตุ*" or not current_notes:
            new_notes = note_str
        else:
            new_notes = current_notes + f"\n{note_str}"

        # ป้องกันข้อความเกินขีดจำกัด 1024 ตัวอักษรของ Discord Embed Field
        if len(new_notes) > 1024:
            lines = new_notes.split("\n")
            while len("\n".join(lines)) > 1024 and len(lines) > 1:
                lines.pop(0)
            new_notes = "\n".join(lines)[:1024]

        info["friend_notes"] = new_notes
        new_embed = rebuild_meetup_embed(embed, info)
        await interaction.response.edit_message(embed=new_embed)

# ==================== Dropdown เลือกหนัง ====================

class MovieSelect(discord.ui.Select):
    def __init__(self, movies):
        self.movies_data = movies
        options = []
        for i, movie in enumerate(movies[:25]):  # Discord จำกัดที่ 25 ตัวเลือก
            showtimes_preview = ", ".join(movie['showtimes'][:3])
            options.append(discord.SelectOption(
                label=movie['name'][:100],
                description=f"รอบ: {showtimes_preview}"[:100],
                value=str(i),
                emoji="🎬"
            ))
        super().__init__(placeholder="🎬 เลือกหนังที่อยากดู...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        movie_idx = int(self.values[0])
        movie = self.movies_data[movie_idx]

        view = ShowtimeSelectView(movie)
        embed = discord.Embed(
            title=f"🎬 {movie['name']}",
            description="เลือกรอบฉายที่ต้องการ:",
            color=0xE50914
        )
        if movie['image']:
            embed.set_thumbnail(url=movie['image'])

        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ รอบฉายทั้งหมด", value=showtimes_fmt[:1024], inline=False)

        await interaction.response.edit_message(embed=embed, view=view)

class MovieSelectView(discord.ui.View):
    def __init__(self, movies):
        super().__init__(timeout=120)
        self.add_item(MovieSelect(movies))

class MovieDateModal(discord.ui.Modal, title='📅 เลือกวันที่และจำนวนคนดูหนัง'):
    date_str = discord.ui.TextInput(
        label='วันที่ต้องการดู',
        style=discord.TextStyle.short,
        placeholder='เช่น วันนี้, พรุ่งนี้, วันเสาร์ที่ 28',
        default='วันนี้',
        required=True,
        max_length=50,
    )
    max_participants = discord.ui.TextInput(
        label='จำกัดจำนวนคนสูงสุด (เว้นว่าง = ไม่จำกัด)',
        style=discord.TextStyle.short,
        placeholder='เช่น 4 (นั่งรถคันเดียวกัน), 6',
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
                await interaction.response.send_message("❌ จำนวนคนสูงสุดต้องเป็นตัวเลขจำนวนเต็มบวก เช่น `4` หรือ `6` ครับ", ephemeral=True)
                return
            max_limit = int(max_raw)

        if self.selected_time == "ยังไม่ระบุเวลา":
            final_time = f"{self.date_str.value} (ยังไม่ระบุเวลา)"
        else:
            final_time = f"{self.date_str.value} เวลา {self.selected_time}"

        base_embed = discord.Embed(
            title=f"📅  🎬 {self.movie['name']}",
            description="━━━━━━━━━━━━━━━━━━━━━━",
            color=0x5865F2
        )
        if self.movie['image']:
            base_embed.set_thumbnail(url=self.movie['image'])
        base_embed.set_footer(text=f"🎯 สร้างโดย {interaction.user.display_name}  •  กดยืนยันเพื่อส่งลงห้องแชท")
        base_embed.set_author(name="🔔 ร่างนัดดูหนัง", icon_url=interaction.user.display_avatar.url)

        info = {
            "location": "เมเจอร์ บิ๊กซี ลพบุรี",
            "time_val": final_time,
            "pin_note": None,
            "going": [],
            "waitlist": [],
            "tentative": [],
            "max_limit": max_limit,
            "friend_notes": "*ยังไม่มีหมายเหตุ*",
        }
        embed = rebuild_meetup_embed(base_embed, info)
        view = DraftMeetupView()

        await interaction.response.edit_message(
            content="นี่คือ **ร่างการนัดหมาย** (มีแค่คุณที่เห็น) \nเมื่อแก้ไขจนพอใจแล้ว ให้กดปุ่ม **📢 ยืนยันข้อมูลนัด** เพื่อส่งเข้าห้องแชทรวมครับ",
            embed=embed,
            view=view
        )

class ShowtimeSelect(discord.ui.Select):
    def __init__(self, movie):
        self.movie = movie
        options = []
        # จำกัดไม่เกิน 24 รอบฉาย เพื่อเผื่อที่ให้ตัวเลือก "ยังไม่แน่ใจ" ไม่เกินลิมิต 25 ตัวเลือกของ Discord
        for t in movie['showtimes'][:24]:
            options.append(discord.SelectOption(
                label=f"🕐 {t}",
                value=t,
                emoji="🎟️"
            ))

        options.append(discord.SelectOption(
            label="🤔 ยังไม่แน่ใจว่าจะดูเมื่อไหร่",
            description="นัดหมายไว้ก่อน ค่อยตกลงเวลากันทีหลัง",
            value="ยังไม่ระบุเวลา",
            emoji="⏳"
        ))

        super().__init__(placeholder="🕐 เลือกรอบฉาย...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_time = self.values[0]
        await interaction.response.send_modal(MovieDateModal(self.movie, selected_time))

def get_trailer_url(movie_name: str) -> str:
    """สร้างลิงก์ค้นหาตัวอย่างหนังบน YouTube"""
    query = quote(f"{movie_name} Official Trailer ตัวอย่างหนัง")
    return f"https://www.youtube.com/results?search_query={query}"

class ShowtimeSelectView(discord.ui.View):
    def __init__(self, movie):
        super().__init__(timeout=120)
        self.add_item(ShowtimeSelect(movie))
        self.add_item(discord.ui.Button(
            label="▶️ ดูตัวอย่างหนัง (YouTube Trailer)",
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
        await self.tree.sync()
        self.add_view(MeetupView())
        self.add_view(CheckinMeetupView())
        self.check_movies.start()
        self.check_weather.start()
        self.check_reminders.start()
        self.morning_briefing.start()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

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

    # ---------- ลูป 1: เช็คหนังใหม่ทุก 1 ชั่วโมง ----------
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
                            title="🆕 หนังเข้าใหม่!",
                            description=f"# 🎬 {movie['name']}",
                            color=0xE50914
                        )
                        if movie['image']:
                            embed.set_thumbnail(url=movie['image'])

                        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
                        embed.add_field(name="⏰ รอบฉาย", value=showtimes_fmt[:1024], inline=False)
                        embed.add_field(name="📍 โรง", value="เมเจอร์ บิ๊กซี ลพบุรี", inline=True)
                        embed.add_field(name="🎞️ ตัวอย่างหนัง", value=f"[▶️ คลิกดูบน YouTube]({trailer_url})", inline=True)
                        embed.set_footer(text="พิมพ์ /นัด เพื่อนัดเพื่อนไปดูด้วยกัน!")

                        trailer_view = discord.ui.View()
                        trailer_view.add_item(discord.ui.Button(
                            label=f"▶️ ดูตัวอย่าง: {movie['name'][:40]}",
                            style=discord.ButtonStyle.link,
                            url=trailer_url
                        ))

                        await channel.send(
                            content="🍿 **มีหนังใหม่เข้าโรงแล้ว!**",
                            embed=embed,
                            view=trailer_view
                        )
        except Exception as e:
            print(f"Error checking movies: {e}")

    @check_movies.before_loop
    async def before_check_movies(self):
        await self.wait_until_ready()

    # ---------- ลูป 2: เช็คสภาพอากาศและเตือนฝนตกทุก 5 นาที ----------
    @tasks.loop(minutes=5)
    async def check_weather(self):
        if not self.weather_channel_id or self.weather_lat is None or self.weather_lon is None:
            return

        now = now_thai()
        if self.last_weather_alert_time and (now - self.last_weather_alert_time).total_seconds() < 1800:
            return

        try:
            url = (
                f"https://api.open-meteo.com/v1/forecast?"
                f"latitude={self.weather_lat}&longitude={self.weather_lon}"
                f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
                f"&hourly=precipitation_probability,weather_code"
                f"&timezone=Asia%2FBangkok&forecast_days=2"
            )
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return
                    data = await response.json()

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
                        title="🌧️ แจ้งเตือนระวังฝนตก!",
                        description=(
                            f"━━━━━━━━━━━━━━━━━━━━━━\n"
                            f"พยากรณ์พบแนวโน้มฝนตกในพื้นที่ของคุณ\n"
                            f"สภาพอากาศ: **{weather_desc}**\n"
                            f"━━━━━━━━━━━━━━━━━━━━━━"
                        ),
                        color=0x3498DB
                    )
                    embed.add_field(name="⏰ เวลาที่คาดการณ์", value=f"```{rain_time} น.```", inline=True)
                    embed.add_field(name="☔ โอกาสฝนตก", value=f"```{max_prob}%```", inline=True)
                    embed.add_field(name="🌡️ อุณหภูมิ / ความชื้น", value=f"```{temp}°C / {humidity}%```", inline=True)
                    embed.set_footer(text=f"📍 พิกัด: {self.weather_lat}, {self.weather_lon} • แจ้งเตือนซ้ำอีกครั้งในอีก 30 นาทีหากยังมีฝน")

                    map_buf = await generate_weather_map(self.weather_lat, self.weather_lon)
                    view = WeatherMapView(self.weather_lat, self.weather_lon)
                    if map_buf:
                        file = discord.File(fp=map_buf, filename="weather_map.png")
                        embed.set_image(url="attachment://weather_map.png")
                        await channel.send(
                            content="☔ **เตรียมเก็บผ้าพกร่ม!** มีโอกาสฝนตกสูงครับ",
                            embed=embed,
                            file=file,
                            view=view
                        )
                    else:
                        await channel.send(
                            content="☔ **เตรียมเก็บผ้าพกร่ม!** มีโอกาสฝนตกสูงครับ",
                            embed=embed,
                            view=view
                        )
                    self.last_weather_alert_time = now
        except Exception as e:
            print(f"Error checking weather: {e}")

    @check_weather.before_loop
    async def before_check_weather(self):
        await self.wait_until_ready()

    # ---------- ลูป 3: ระบบเตือนนัดหมายล่วงหน้าอัตโนมัติ (Auto-Reminder) ----------
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

                    # ห่างจากเวลาเตือนเกิน 2 ชั่วโมง (เช่น บอทปิดไปนาน) ให้ข้ามเพื่อไม่ให้แจ้งเตือนย้อนหลังมั่ว
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
                    topic = entry.get("topic", "การนัดหมาย")
                    location = entry.get("location", "-")
                    time_val = entry.get("time", "-")
                    jump_url = None

                    # พยายามดึงรายชื่อล่าสุดจากข้อความการ์ดนัดหมายจริง
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
                        f"📍 **สถานที่:** `{location}`",
                        f"⏰ **เวลานัด:** `{time_val}`",
                        f"👥 **ผู้เข้าร่วม ({len(going_list)} คน):** {' '.join(going_list) if going_list else '-'}",
                    ]
                    if tentative_list:
                        desc_lines.append(f"🤔 **ยังไม่แน่ใจ ({len(tentative_list)} คน):** {' '.join(tentative_list)}")
                    if jump_url:
                        desc_lines.append(f"\n🔗 [คลิกเพื่อเปิดการ์ดนัดหมาย]({jump_url})")

                    rem_embed = discord.Embed(
                        title=f"⏰ แจ้งเตือนอัตโนมัติ: ใกล้ถึงเวลานัด '{topic}' แล้ว!",
                        description="\n".join(desc_lines),
                        color=0xF1C40F
                    )
                    rem_embed.set_footer(text="ระบบแจ้งเตือนผู้เข้าร่วมอัตโนมัติ")

                    await channel.send(
                        content=f"🔔 **เตรียมตัวให้พร้อม!** แจ้งเตือนรายชื่อผู้ลงชื่อนัดหมาย: {mentions_str}",
                        embed=rem_embed,
                        allowed_mentions=discord.AllowedMentions(users=True)
                    )

                    # ส่งการแจ้งเตือนแบบ DM ให้ผู้ลงชื่อทุกคนควบคู่กัน
                    await _send_dm_to_mentions(
                        self,
                        all_pings,
                        rem_embed,
                        content=f"💌 **แจ้งเตือนนัดหมายอัตโนมัติ: `{topic}` ใกล้ถึงเวลานัดแล้วครับ!**"
                    )

            if changed:
                save_json(HISTORY_FILE, history)
        except Exception as e:
            print(f"Error in check_reminders: {e}")

    @check_reminders.before_loop
    async def before_check_reminders(self):
        await self.wait_until_ready()

    # ---------- ลูป 4: สรุปข่าวสารประจำเช้าวันใหม่ (Morning Briefing 07:30 น.) ----------
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
                content="🌅 **อรุณสวัสดิ์ครับ! สรุปสภาพอากาศ ค่าฝุ่น และนัดหมายประจำวันนี้มาแล้วครับ**",
                embed=embed,
                file=file,
                view=view
            )
        else:
            await channel.send(
                content="🌅 **อรุณสวัสดิ์ครับ! สรุปสภาพอากาศ ค่าฝุ่น และนัดหมายประจำวันนี้มาแล้วครับ**",
                embed=embed,
                view=view
            )

    @morning_briefing.before_loop
    async def before_morning_briefing(self):
        await self.wait_until_ready()


async def build_morning_briefing_card(lat: float, lon: float, guild_id_str: str = None):
    """สร้างการ์ดสรุปข่าวสารประจำเช้าวันใหม่ (สภาพอากาศ + PM 2.5 + โอกาสฝนตก + นัดหมายวันนี้)"""
    now = now_thai()
    thai_days = ["จันทร์", "อังคาร", "พุธ", "พฤหัสบดี", "ศุกร์", "เสาร์", "อาทิตย์"]
    thai_months = [
        "", "มกราคม", "กุมภาพันธ์", "มีนาคม", "เมษายน", "พฤษภาคม", "มิถุนายน",
        "กรกฎาคม", "สิงหาคม", "กันยายน", "ตุลาคม", "พฤศจิกายน", "ธันวาคม"
    ]
    day_name = thai_days[now.weekday()]
    month_name = thai_months[now.month]
    buddhist_year = now.year + 543
    date_header = f"วัน{day_name}ที่ {now.day} {month_name} {buddhist_year}"

    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}"
        f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,weather_code,wind_speed_10m"
        f"&hourly=temperature_2m,precipitation_probability,weather_code"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        f"&timezone=Asia%2FBangkok&forecast_days=1"
    )
    aq_url = (
        f"https://air-quality-api.open-meteo.com/v1/air-quality?"
        f"latitude={lat}&longitude={lon}"
        f"&current=pm2_5,pm10,us_aqi&timezone=Asia%2FBangkok"
    )

    data = {}
    aq_data = {}
    try:
        async with aiohttp.ClientSession() as session:
            w_resp, aq_resp = await asyncio.gather(
                session.get(weather_url),
                session.get(aq_url),
                return_exceptions=True
            )
            if not isinstance(w_resp, Exception) and w_resp.status == 200:
                data = await w_resp.json()
            if not isinstance(aq_resp, Exception) and aq_resp.status == 200:
                aq_data = await aq_resp.json()
    except Exception as e:
        print(f"Error fetching morning briefing data: {e}")

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

    # เช็คช่วงเวลาที่มีโอกาสฝนตกสูงในวันนี้ (07:00 - 23:00)
    hourly = data.get("hourly", {})
    h_times = hourly.get("time", [])
    h_probs = hourly.get("precipitation_probability", [])
    rainy_hours = []
    for idx, t_str in enumerate(h_times):
        p = h_probs[idx] if idx < len(h_probs) else 0
        if p and p >= 50:
            hh = t_str.split("T")[1] if "T" in t_str else t_str
            rainy_hours.append(f"{hh} ({p}%)")

    # ข้อมูลฝุ่น PM 2.5
    aq_current = aq_data.get("current", {})
    pm25_val = aq_current.get("pm2_5")
    aqi_val = aq_current.get("us_aqi", "-")
    aq_status, aq_warning, is_dust_high = evaluate_air_quality(pm25_val, aqi_val)
    pm25_str = f"{pm25_val} µg/m³" if pm25_val is not None else "ไม่มีข้อมูล"

    # ดึงนัดหมายที่มีในวันนี้
    history = load_json(HISTORY_FILE, [])
    today_meetups = []
    today_date = now.date()
    day_token_patterns = [f"ที่ {now.day}", f"ที่{now.day}", f"{now.day:02d}/{now.month:02d}"]

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
                    if c_dt.date() == today_date and "พรุ่งนี้" not in t_val and "มะรืน" not in t_val:
                        is_today = True
                except Exception:
                    pass

        if is_today:
            today_meetups.append(entry)

    # คำแนะนำประจำวัน
    advice_parts = []
    if rain_prob_max >= 60:
        advice_parts.append("☔ **วันนี้มีโอกาสฝนตกสูง** อย่าลืมพกร่มและเก็บผ้าก่อนออกจากบ้านนะครับ")
    elif rain_prob_max >= 35:
        advice_parts.append("🌦️ **วันนี้อาจมีฝนตกบางช่วง** ติดร่มพับไว้สักคันจะอุ่นใจกว่าครับ")
    else:
        advice_parts.append("☀️ **วันนี้โอกาสฝนตกต่ำ** สภาพอากาศเหมาะกับการเดินทางและทำกิจกรรมครับ")

    if aq_warning:
        advice_parts.append(aq_warning)

    embed = discord.Embed(
        title=f"🌅 สรุปข่าวสารประจำเช้าวันใหม่ • {date_header}",
        description=(
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            + "\n".join(advice_parts)
            + "\n━━━━━━━━━━━━━━━━━━━━━━"
        ),
        color=0xF39C12 if not is_dust_high else 0xE74C3C
    )

    embed.add_field(
        name="🌤️ สภาพอากาศวันนี้",
        value=f"**{weather_desc}**\nปัจจุบัน `{temp}°C` (รู้สึก `{feels_like}°C`)\nต่ำสุด-สูงสุด `{t_min}°C - {t_max}°C` • ความชื้น `{humidity}%`",
        inline=True
    )

    rain_detail = f"โอกาสสูงสุด **`{rain_prob_max}%`**"
    if rainy_hours:
        rain_detail += f"\nช่วงเสี่ยงฝน: `{', '.join(rainy_hours[:3])}`"
    else:
        rain_detail += "\nไม่มีช่วงเสี่ยงฝนตกหนัก"
    embed.add_field(
        name="☔ โอกาสฝนตก",
        value=rain_detail,
        inline=True
    )

    embed.add_field(
        name="😷 ค่าฝุ่น PM 2.5 & AQI",
        value=f"PM 2.5: **`{pm25_str}`**\nAQI `{aqi_val}` • {aq_status}",
        inline=False
    )

    embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)

    if today_meetups:
        meetup_lines = []
        for idx, m in enumerate(today_meetups[-5:], 1):
            topic = clean_topic_title(m.get("topic", "ไม่ระบุ"))
            loc = m.get("location", "-")
            t_str = m.get("time", "-")
            p_cnt = m.get("participants", 0)
            max_l = m.get("max_limit")
            p_txt = f"{p_cnt}/{max_l} คน" if max_l else f"{p_cnt} คน"
            meetup_lines.append(f"`{idx}.` **{topic}** — 📍 {loc} • ⏰ `{t_str}` (👥 {p_txt})")
        meetup_summary = "\n".join(meetup_lines)
    else:
        meetup_summary = "📭 *วันนี้ยังไม่มีรายการนัดหมาย พิมพ์ `/นัด` เพื่อชวนเพื่อนได้เลย!*"

    embed.add_field(
        name=f"📅 นัดหมายที่มีในวันนี้ ({len(today_meetups)} รายการ)",
        value=meetup_summary[:1024],
        inline=False
    )

    embed.set_footer(text=f"📍 พิกัด: {lat}, {lon} • ส่งอัตโนมัติทุกวันเวลา 07:30 น.")

    map_buf = await generate_weather_map(lat, lon)
    view = WeatherMapView(lat, lon)
    return embed, map_buf, view


bot = MyBot()

# ==================== คำสั่ง Slash Commands ====================

class CreateMeetupModal(discord.ui.Modal, title='📅 สร้างการนัดหมาย'):
    topic = discord.ui.TextInput(
        label='หัวข้อการนัดหมาย',
        style=discord.TextStyle.short,
        placeholder='เช่น ตีป้อม, กินหมูกระทะ, ดูหนัง',
        required=True,
        max_length=100,
    )
    location = discord.ui.TextInput(
        label='สถานที่',
        style=discord.TextStyle.short,
        placeholder='เช่น หอ A, ร้านประจำ, Major ลพบุรี',
        required=True,
        max_length=100,
    )
    time_str = discord.ui.TextInput(
        label='เวลา (ระบุ HH:MM เพื่อเตือนล่วงหน้า 30 นาที)',
        style=discord.TextStyle.short,
        placeholder='เช่น วันนี้ 20:00, พรุ่งนี้ 18:30',
        required=True,
        max_length=50,
    )
    max_participants = discord.ui.TextInput(
        label='จำนวนคนสูงสุด (เว้นว่าง = ไม่จำกัด)',
        style=discord.TextStyle.short,
        placeholder='เช่น 4 (นั่งรถ), 5 (ตีป้อม) - เต็มแล้วมีคิวสำรองให้',
        required=False,
        max_length=5,
    )
    note = discord.ui.TextInput(
        label='หมายเหตุ (ไม่ใส่ก็ได้)',
        style=discord.TextStyle.long,
        placeholder='เช่น ใครสายจ่ายค่าข้าว, เตรียมเสื้อสีดำมา...',
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
                await interaction.response.send_message("❌ จำนวนคนสูงสุดต้องเป็นตัวเลขจำนวนเต็มบวก เช่น `4` หรือ `5` ครับ", ephemeral=True)
                return
            max_limit = int(max_raw)

        base_embed = discord.Embed(
            title=f"📅  {topic_val}",
            description="━━━━━━━━━━━━━━━━━━━━━━",
            color=0x5865F2
        )
        base_embed.set_footer(text=f"🎯 สร้างโดย {interaction.user.display_name}  •  กดยืนยันเพื่อส่งลงห้องแชท")
        base_embed.set_author(name="🔔 ร่างการนัดหมาย", icon_url=interaction.user.display_avatar.url)

        info = {
            "location": location_val,
            "time_val": time_val,
            "pin_note": note_val,
            "going": [],
            "waitlist": [],
            "tentative": [],
            "max_limit": max_limit,
            "friend_notes": "*ยังไม่มีหมายเหตุ*",
        }
        embed = rebuild_meetup_embed(base_embed, info)
        view = DraftMeetupView()

        await interaction.response.send_message(
            content="นี่คือ **ร่างการนัดหมาย** (มีแค่คุณที่เห็น) \nเมื่อแก้ไขจนพอใจแล้ว ให้กดปุ่ม **📢 ยืนยันข้อมูลนัด** เพื่อส่งเข้าห้องแชทรวมครับ",
            embed=embed,
            view=view,
            ephemeral=True
        )


@bot.tree.command(name="นัด", description="สร้างการนัดหมาย (นัดปกติ หรือ นัดดูหนัง พร้อมระบบจำกัดคนและคิวสำรอง)")
@app_commands.describe(ประเภท="เลือกประเภทการนัดหมาย")
@app_commands.choices(ประเภท=[
    app_commands.Choice(name="📝 นัดปกติ (พิมพ์สถานที่ เวลา และจำกัดจำนวนคนได้)", value="normal"),
    app_commands.Choice(name="🎬 นัดดูหนัง (เลือกหนังจากเมเจอร์)", value="movie"),
])
async def meetup(interaction: discord.Interaction, ประเภท: app_commands.Choice[str]):
    if ประเภท.value == "normal":
        await interaction.response.send_modal(CreateMeetupModal())
    elif ประเภท.value == "movie":
        await interaction.response.defer(ephemeral=True)
        movies = await fetch_major_movies()

        if not movies:
            await interaction.followup.send("❌ ไม่พบข้อมูลรอบหนังในขณะนี้", ephemeral=True)
            return

        embed = discord.Embed(
            title="🎬 เลือกหนังที่อยากดู",
            description="━━━━━━━━━━━━━━━━━━━━━━\nเลือกหนังจาก Dropdown ด้านล่าง\nจากนั้นเลือกรอบฉาย แล้วบอทจะสร้างนัดหมายให้อัตโนมัติ!",
            color=0xE50914
        )
        embed.set_footer(text="📍 เมเจอร์ บิ๊กซี ลพบุรี")

        view = MovieSelectView(movies)
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)


# ==================== ฟีเจอร์: ประวัตินัดหมาย ====================

@bot.tree.command(name="ประวัตินัด", description="ดูประวัตินัดหมายย้อนหลังของเซิร์ฟเวอร์นี้")
async def meetup_history(interaction: discord.Interaction):
    all_history = load_json(HISTORY_FILE, [])

    # กรองเฉพาะประวัตินัดหมายของเซิร์ฟเวอร์ปัจจุบัน (แก้บั๊กข้อมูลข้ามเซิร์ฟเวอร์)
    guild_str = str(interaction.guild_id) if interaction.guild_id else ""
    if guild_str:
        history = [h for h in all_history if str(h.get("guild_id", "")) == guild_str]
    else:
        history = all_history

    if not history:
        await interaction.response.send_message("📜 ยังไม่มีประวัตินัดหมายในเซิร์ฟเวอร์นี้เลยครับ", ephemeral=True)
        return

    recent = list(reversed(history[-10:]))

    embed = discord.Embed(
        title="📜 ประวัตินัดหมาย",
        description="━━━━━━━━━━━━━━━━━━━━━━\nรายการนัดหมายล่าสุด 10 ครั้งในเซิร์ฟเวอร์นี้",
        color=0x5865F2
    )

    for i, entry in enumerate(recent, 1):
        try:
            dt = datetime.fromisoformat(entry.get("created_at", ""))
            date_str = dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            date_str = "ไม่ทราบวัน"

        topic = clean_topic_title(entry.get("topic", "ไม่ระบุ"))
        location = entry.get("location", "ไม่ระบุ")
        time_val = entry.get("time", "ไม่ระบุ")
        participants = entry.get("participants", 0)
        max_limit = entry.get("max_limit")
        tentative_cnt = entry.get("tentative_count", 0)
        created_by = entry.get("created_by", "ไม่ทราบ")

        p_str = f"{participants}/{max_limit} คน" if max_limit else f"{participants} คน"
        if tentative_cnt:
            p_str += f" (ไม่แน่ใจ {tentative_cnt})"

        embed.add_field(
            name=f"`{i}.` {topic}",
            value=(
                f"📍 {location}  •  ⏰ {time_val}\n"
                f"👥 {p_str}  •  🗓️ {date_str}\n"
                f"สร้างโดย: {created_by}"
            ),
            inline=False
        )

    embed.set_footer(text=f"ทั้งหมด {len(history)} นัดหมายในเซิร์ฟเวอร์นี้")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ==================== ฟีเจอร์: สรุปข่าวสารประจำวัน (Morning Briefing) ====================

@bot.tree.command(name="สรุปประจำวัน", description="ดูการ์ดสรุปสภาพอากาศ ค่าฝุ่น PM 2.5 โอกาสฝนตก และนัดหมายที่มีในวันนี้")
async def daily_briefing_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    target_lat = bot.weather_lat if bot.weather_lat is not None else 14.7995
    target_lon = bot.weather_lon if bot.weather_lon is not None else 100.6534
    guild_str = str(interaction.guild_id) if interaction.guild_id else None

    embed, map_buf, view = await build_morning_briefing_card(target_lat, target_lon, guild_str)
    if not embed:
        await interaction.followup.send("❌ ไม่สามารถดึงข้อมูลสรุปประจำวันได้ในขณะนี้")
        return

    if map_buf:
        file = discord.File(fp=map_buf, filename="morning_weather_map.png")
        embed.set_image(url="attachment://morning_weather_map.png")
        await interaction.followup.send(embed=embed, file=file, view=view)
    else:
        await interaction.followup.send(embed=embed, view=view)


# ==================== ฟีเจอร์: ตั้งค่าห้องแจ้งเตือนหนังใหม่ ====================

@bot.tree.command(name="ตั้งค่าแจ้งเตือนหนัง", description="ตั้งค่าห้องแชทที่ต้องการให้บอทแจ้งเตือนหนังใหม่")
@app_commands.describe(channel="เลือกห้องแชท (ถ้าไม่เลือกจะใช้ห้องปัจจุบัน)")
async def set_movie_channel(interaction: discord.Interaction, channel: discord.TextChannel = None):
    await interaction.response.defer(ephemeral=True)
    try:
        target_channel = channel if channel else interaction.channel
        bot.movie_channel_id = target_channel.id

        config = load_config()
        config["movie_channel_id"] = str(target_channel.id)
        save_config(config)

        embed = discord.Embed(
            title="✅ ตั้งค่าสำเร็จ!",
            description=f"บอทจะส่งแจ้งเตือนหนังเข้าใหม่ไปที่ {target_channel.mention}\n\nระบบจะเช็คหนังใหม่ทุกๆ 1 ชั่วโมงอัตโนมัติ",
            color=0x57F287
        )
        await interaction.followup.send(embed=embed, ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)

# ==================== ดึงข้อมูลหนัง ====================

async def fetch_major_movies():
    url = "https://www.majorcineplex.com/cinema/bigc-lopburi/"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
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

# ==================== เช็ครอบหนัง ====================

class MovieTrailerView(discord.ui.View):
    def __init__(self, movies):
        super().__init__(timeout=None)
        for movie in movies[:25]:
            trailer_url = get_trailer_url(movie['name'])
            self.add_item(discord.ui.Button(
                label=f"▶️ ตัวอย่าง: {movie['name'][:35]}",
                style=discord.ButtonStyle.link,
                url=trailer_url
            ))

@bot.tree.command(name="เช็คหนัง", description="เช็ครอบหนังและดูตัวอย่างหนังที่เมเจอร์ บิ๊กซี ลพบุรี")
async def check_movies_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    movies = await fetch_major_movies()

    if not movies:
        await interaction.followup.send("ไม่พบข้อมูลรอบหนังในขณะนี้")
        return

    embeds = []
    intro_embed = discord.Embed(
        title="🍿 รอบหนังเมเจอร์ บิ๊กซี ลพบุรี วันนี้",
        description="กดปุ่มด้านล่างหรือคลิกลิงก์ในการ์ดเพื่อดูตัวอย่างหนัง (Trailer) บน YouTube ได้เลย!",
        color=discord.Color.red()
    )
    embeds.append(intro_embed)

    display_movies = movies[:9]
    for movie in display_movies:
        trailer_url = get_trailer_url(movie['name'])
        embed = discord.Embed(title=f"🎬 {movie['name']}", color=discord.Color.red())

        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ รอบฉาย", value=showtimes_fmt[:1024], inline=False)
        embed.add_field(name="🎞️ ตัวอย่างหนัง (Trailer)", value=f"[▶️ คลิกดูตัวอย่างบน YouTube]({trailer_url})", inline=False)

        if movie['image']:
            embed.set_thumbnail(url=movie['image'])

        embeds.append(embed)

    view = MovieTrailerView(display_movies)
    await interaction.followup.send(embeds=embeds[:10], view=view)

# ==================== แจ้งเตือนฝนตก & เช็คสภาพอากาศ ====================

def translate_weather_code(code: int) -> str:
    """แปลงรหัสสภาพอากาศ WMO เป็นข้อความภาษาไทยพร้อมอีโมจิ"""
    weather_map = {
        0: "☀️ ท้องฟ้าแจ่มใส",
        1: "🌤️ แจ่มใสเป็นส่วนใหญ่",
        2: "⛅ มีเมฆบางส่วน",
        3: "☁️ มีเมฆมาก (มืดครึ้ม)",
        45: "🌫️ มีหมอก",
        48: "🌫️ มีหมอกลงจัด",
        51: "🌦️ ฝนตกปรอยๆ เบาบาง",
        53: "🌦️ ฝนตกปรอยๆ ปานกลาง",
        55: "🌧️ ฝนตกปรอยๆ ค่อนข้างหนัก",
        61: "🌧️ ฝนตกเล็กน้อย",
        63: "🌧️ ฝนตกปานกลาง",
        65: "🌧️ ฝนตกหนัก",
        80: "🌦️ ฝนฟ้าคะนองเป็นแห่งๆ (เบา)",
        81: "🌧️ ฝนฟ้าคะนองปานกลาง",
        82: "⛈️ ฝนฟ้าคะนองรุนแรง",
        95: "⛈️ พายุฝนฟ้าคะนอง",
        96: "⛈️ พายุฝนฟ้าคะนองและลูกเห็บเล็กน้อย",
        99: "⛈️ พายุฝนฟ้าคะนองรุนแรง",
    }
    return weather_map.get(code, f"🌤️ สภาพอากาศทั่วไป (Code {code})")

class WeatherMapView(discord.ui.View):
    def __init__(self, lat: float, lon: float):
        super().__init__(timeout=None)
        windy_url = f"https://www.windy.com/{lat}/{lon}?radar,{lat},{lon},9"
        gmaps_url = f"https://www.google.com/maps?q={lat},{lon}"
        self.add_item(discord.ui.Button(
            label="🗺️ ดูเรดาร์ฝนสดแบบโต้ตอบ (Windy)",
            style=discord.ButtonStyle.link,
            url=windy_url
        ))
        self.add_item(discord.ui.Button(
            label="📍 เปิดพิกัดใน Google Maps",
            style=discord.ButtonStyle.link,
            url=gmaps_url
        ))

def _compose_weather_map_sync(coords, tile_xy, base_tiles, radar_parent_map, offset_x, offset_y):
    """ฟังก์ชันประมวลผลภาพเรดาร์ฝนด้วย Pillow (รันใน Thread แยกเพื่อไม่ให้บล็อก Event Loop)"""
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
    """สร้างภาพแผนที่พร้อมเลเยอร์เรดาร์ฝนสด (RainViewer + OpenStreetMap) และปักหมุดพิกัด"""
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
        async with aiohttp.ClientSession(headers=headers) as session:
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

@bot.tree.command(name="ตั้งค่าแจ้งเตือนฝนตก", description="ตั้งค่าห้องแจ้งเตือนฝนตกและสรุปตอนเช้า (07:30 น.) พร้อมระบุละติจูดและลองจิจูด")
@app_commands.describe(
    lat="ละติจูด (Latitude) เช่น 14.7995",
    lon="ลองจิจูด (Longitude) เช่น 100.6534",
    channel="เลือกห้องแชท (ถ้าไม่เลือกจะใช้ห้องปัจจุบัน)"
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
            title="✅ ตั้งค่าพยากรณ์อากาศ & สรุปประจำวันสำเร็จ!",
            description=(
                f"บอทจะส่งแจ้งเตือนฝนตกและ **🌅 สรุปข่าวสารตอนเช้า (07:30 น.)** ไปที่ {target_channel.mention}\n\n"
                f"📍 **พิกัด:** `{lat}, {lon}`\n"
                f"⏱️ เมื่อพบแนวโน้มฝนตก บอทจะแจ้งเตือน **1 ครั้ง** และเว้นระยะรออีก **30 นาที**"
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
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาด: {e}", ephemeral=True)

def evaluate_air_quality(pm25: float, aqi: int):
    """ประเมินระดับคุณภาพอากาศและค่าฝุ่น PM 2.5 ตามเกณฑ์มาตรฐานไทย"""
    if pm25 is None:
        return "ไม่ทราบค่า", None, False
    if pm25 <= 15.0:
        return "🟦 ดีมาก (อากาศสะอาด)", None, False
    elif pm25 <= 25.0:
        return "🟩 ดี (ปกติ)", None, False
    elif pm25 <= 37.5:
        return "🟨 ปานกลาง", None, False
    elif pm25 <= 75.0:
        return (
            "🟧 เริ่มมีผลกระทบต่อสุขภาพ!",
            "⚠️ **คำเตือนฝุ่น PM 2.5 เกินมาตรฐาน!** (`> 37.5 µg/m³`) ควรสวมหน้ากากป้องกันฝุ่นเมื่อออกนอกอาคาร",
            True
        )
    else:
        return (
            "🟥 อันตรายต่อสุขภาพรุนแรง!",
            "🚨 **เตือนภัยฝุ่น PM 2.5 หนาจัด!** (`> 75.0 µg/m³`) งดกิจกรรมกลางแจ้งและสวมหน้ากาก N95 ทันที",
            True
        )

@bot.tree.command(name="เช็คสภาพอากาศ", description="เช็คสภาพอากาศ ค่าฝุ่น PM 2.5 และเรดาร์ฝนสด ณ ปัจจุบัน")
@app_commands.describe(
    lat="ละติจูด (ถ้าไม่ใส่จะใช้พิกัดที่ตั้งค่าไว้ หรือลพบุรีเป็นค่าเริ่มต้น)",
    lon="ลองจิจูด (ถ้าไม่ใส่จะใช้พิกัดที่ตั้งค่าไว้ หรือลพบุรีเป็นค่าเริ่มต้น)"
)
async def check_weather_cmd(interaction: discord.Interaction, lat: float = None, lon: float = None):
    await interaction.response.defer()

    target_lat = lat if lat is not None else (bot.weather_lat if bot.weather_lat is not None else 14.7995)
    target_lon = lon if lon is not None else (bot.weather_lon if bot.weather_lon is not None else 100.6534)

    try:
        weather_url = (
            f"https://api.open-meteo.com/v1/forecast?"
            f"latitude={target_lat}&longitude={target_lon}"
            f"&current=temperature_2m,relative_humidity_2m,apparent_temperature,precipitation,weather_code,wind_speed_10m"
            f"&hourly=temperature_2m,precipitation_probability,weather_code"
            f"&timezone=Asia%2FBangkok&forecast_days=2"
        )
        aq_url = (
            f"https://air-quality-api.open-meteo.com/v1/air-quality?"
            f"latitude={target_lat}&longitude={target_lon}"
            f"&current=pm2_5,pm10,us_aqi&timezone=Asia%2FBangkok"
        )

        async with aiohttp.ClientSession() as session:
            w_resp, aq_resp = await asyncio.gather(
                session.get(weather_url),
                session.get(aq_url),
                return_exceptions=True
            )
            if isinstance(w_resp, Exception) or w_resp.status != 200:
                await interaction.followup.send("❌ ไม่สามารถดึงข้อมูลสภาพอากาศได้ในขณะนี้")
                return
            data = await w_resp.json()

            aq_data = {}
            if not isinstance(aq_resp, Exception) and aq_resp.status == 200:
                aq_data = await aq_resp.json()

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
        pm25_str = f"{pm25_val} µg/m³" if pm25_val is not None else "ไม่มีข้อมูล"

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
            forecast_lines.append(f"` {t_label} ` {icon} **{t_val}°C** • ☔ โอกาสฝนตก `{p_val}%`")

        forecast_text = "\n".join(forecast_lines) if forecast_lines else "ไม่มีข้อมูลพยากรณ์"

        if is_dust_high:
            card_color = 0xE67E22 if (pm25_val and pm25_val <= 75.0) else 0xE74C3C
        else:
            card_color = 0x3498DB if max_rain_prob >= 60 else (0xF1C40F if max_rain_prob >= 30 else 0x2ECC71)

        desc_text = f"━━━━━━━━━━━━━━━━━━━━━━\n# {weather_desc}\n━━━━━━━━━━━━━━━━━━━━━━"
        if aq_warning:
            desc_text += f"\n{aq_warning}\n━━━━━━━━━━━━━━━━━━━━━━"

        embed = discord.Embed(
            title="🌤️ รายงานสภาพอากาศ • ค่าฝุ่น PM 2.5 & เรดาร์ฝนสด",
            description=desc_text,
            color=card_color
        )
        embed.add_field(name="🌡️ อุณหภูมิ", value=f"```{temp} °C```", inline=True)
        embed.add_field(name="🥵 รู้สึกเหมือน", value=f"```{feels_like} °C```", inline=True)
        embed.add_field(name="💧 ความชื้น", value=f"```{humidity} %```", inline=True)
        embed.add_field(name="💨 ความเร็วลม", value=f"```{wind} km/h```", inline=True)
        embed.add_field(name="☔ โอกาสฝนตกสูงสุด", value=f"```{max_rain_prob} %```", inline=True)
        embed.add_field(name="📍 พิกัด", value=f"```{target_lat}, {target_lon}```", inline=True)
        embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="😷 ค่าฝุ่น PM 2.5", value=f"```{pm25_str}```", inline=True)
        embed.add_field(name="📊 ดัชนีคุณภาพอากาศ (AQI)", value=f"```AQI {aqi_val} • {aq_status}```", inline=True)
        embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="🕒 พยากรณ์ล่วงหน้า (รายชั่วโมง)", value=forecast_text, inline=False)
        embed.set_footer(text=f"🗺️ แผนที่เรดาร์ฝนสด (จุดสีแดงคือพิกัดของคุณ) • อัพเดท {now.strftime('%d/%m/%Y %H:%M')} น.")

        map_buf = await generate_weather_map(target_lat, target_lon)
        view = WeatherMapView(target_lat, target_lon)
        if map_buf:
            file = discord.File(fp=map_buf, filename="weather_map.png")
            embed.set_image(url="attachment://weather_map.png")
            await interaction.followup.send(embed=embed, file=file, view=view)
        else:
            await interaction.followup.send(embed=embed, view=view)
    except Exception as e:
        await interaction.followup.send(f"❌ เกิดข้อผิดพลาดในการเช็คสภาพอากาศ: {e}")

# ==================== รัน ====================

if __name__ == '__main__':
    keep_alive()
    if TOKEN == 'YOUR_BOT_TOKEN_HERE' or not TOKEN:
        print("กรุณาใส่ Token ในไฟล์ .env หรือตั้งค่า DISCORD_TOKEN ใน Environment Variables")
    else:
        bot.run(TOKEN)
