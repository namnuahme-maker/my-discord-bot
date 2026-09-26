import discord
from discord.ext import commands, tasks
from discord import app_commands
import aiohttp
import json
import re
from bs4 import BeautifulSoup
from datetime import datetime
import os
from keep_alive import keep_alive

# จะโหลด Token จาก Environment Variable แทนเพื่อความปลอดภัยเวลาอัพโหลดขึ้นเว็บ
TOKEN = os.environ.get('DISCORD_TOKEN', 'YOUR_BOT_TOKEN_HERE')

# ไฟล์เก็บข้อมูล
DATA_DIR = os.path.dirname(os.path.abspath(__file__))
HISTORY_FILE = os.path.join(DATA_DIR, "meetup_history.json")
MOVIE_CACHE_FILE = os.path.join(DATA_DIR, "movie_cache.json")
CONFIG_FILE = os.path.join(DATA_DIR, "config.json")

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

def save_meetup_history(topic, location, time_val, participants_count, created_by, guild_id):
    history = load_json(HISTORY_FILE, [])
    history.append({
        "topic": topic,
        "location": location,
        "time": time_val,
        "participants": participants_count,
        "created_by": created_by,
        "created_at": datetime.now().isoformat(),
        "guild_id": guild_id
    })
    # เก็บแค่ 50 รายการล่าสุด
    history = history[-50:]
    save_json(HISTORY_FILE, history)

def load_config():
    return load_json(CONFIG_FILE, {})

def save_config(config):
    save_json(CONFIG_FILE, config)

def load_movie_cache():
    return set(load_json(MOVIE_CACHE_FILE, []))

def save_movie_cache(cache_set):
    save_json(MOVIE_CACHE_FILE, list(cache_set))

# ==================== Views & Modals ====================

class MeetupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ ไปด้วย!", style=discord.ButtonStyle.green, custom_id="join_meetup", row=0)
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        
        participants_index = -1
        count_index = -1
        for i, field in enumerate(embed.fields):
            if "ใครไปบ้าง" in field.name:
                participants_index = i
            if "จำนวนคน" in field.name:
                count_index = i
                
        if participants_index != -1:
            current_participants = embed.fields[participants_index].value
            user_mention = interaction.user.mention
            
            if current_participants == "*ยังไม่มีใครลงชื่อ*":
                new_participants = f">>> {user_mention}"
                count = 1
            elif user_mention not in current_participants:
                new_participants = current_participants + f"\n{user_mention}"
                count = new_participants.count("<@")
            else:
                await interaction.response.send_message("❌ คุณลงชื่อไปแล้วนะ!", ephemeral=True)
                return
                
            embed.set_field_at(participants_index, name="👥 ใครไปบ้าง", value=new_participants, inline=False)
            if count_index != -1:
                embed.set_field_at(count_index, name="🔢 จำนวนคน", value=f"` {count} คน `", inline=True)
            await interaction.response.edit_message(embed=embed)
            
            # บันทึกประวัติ (อัพเดทจำนวนคน)
            _save_meetup_from_embed(embed, interaction.guild_id)
        else:
            await interaction.response.send_message("เกิดข้อผิดพลาด", ephemeral=True)

    @discord.ui.button(label="❌ ไม่ไปแล้ว", style=discord.ButtonStyle.red, custom_id="leave_meetup", row=0)
    async def leave_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        
        participants_index = -1
        count_index = -1
        for i, field in enumerate(embed.fields):
            if "ใครไปบ้าง" in field.name:
                participants_index = i
            if "จำนวนคน" in field.name:
                count_index = i
                
        if participants_index != -1:
            current_participants = embed.fields[participants_index].value
            user_mention = interaction.user.mention
            
            if user_mention in current_participants:
                new_participants = current_participants.replace(f"\n{user_mention}", "").replace(f">>> {user_mention}", "").replace(user_mention, "")
                new_participants = new_participants.strip()
                
                if not new_participants or new_participants == ">>>":
                    new_participants = "*ยังไม่มีใครลงชื่อ*"
                    count = 0
                else:
                    if not new_participants.startswith(">>>"):
                        new_participants = ">>> " + new_participants.lstrip("\n")
                    count = new_participants.count("<@")
                    
                embed.set_field_at(participants_index, name="👥 ใครไปบ้าง", value=new_participants, inline=False)
                if count_index != -1:
                    embed.set_field_at(count_index, name="🔢 จำนวนคน", value=f"` {count} คน `", inline=True)
                await interaction.response.edit_message(embed=embed)
            else:
                await interaction.response.send_message("❌ คุณยังไม่ได้ลงชื่อเลยนะ!", ephemeral=True)
        else:
            await interaction.response.send_message("เกิดข้อผิดพลาด", ephemeral=True)

    @discord.ui.button(label="📝 เพิ่มหมายเหตุ", style=discord.ButtonStyle.blurple, custom_id="note_meetup", row=1)
    async def note_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(NoteModal())

    @discord.ui.button(label="🔔 แจ้งเตือนอีกครั้ง", style=discord.ButtonStyle.secondary, custom_id="notify_meetup", row=1)
    async def notify_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        embed = interaction.message.embeds[0]
        
        await interaction.response.send_message(
            content="@everyone 🔔 **มีการแจ้งเตือนนัดหมาย!**",
            embed=embed,
            view=MeetupView(),
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )

def _save_meetup_from_embed(embed, guild_id):
    """ดึงข้อมูลจาก embed แล้วบันทึกประวัติ"""
    topic = embed.title.replace("📅", "").replace("🎬", "").strip() if embed.title else "ไม่ระบุ"
    location = ""
    time_val = ""
    count = 0
    created_by = ""
    
    for field in embed.fields:
        if "สถานที่" in field.name:
            location = field.value.replace("```", "").strip()
        if "เวลานัด" in field.name:
            time_val = field.value.replace("```", "").strip()
        if "จำนวนคน" in field.name:
            try:
                count = int(field.value.replace("`", "").replace("คน", "").strip())
            except ValueError:
                count = 0
    
    if embed.footer and embed.footer.text:
        created_by = embed.footer.text.replace("🎯 สร้างโดย", "").split("•")[0].strip()
    
    # อัพเดทประวัติ (หาจาก topic+location+time ที่ตรงกัน แล้วอัพเดทจำนวนคน)
    history = load_json(HISTORY_FILE, [])
    found = False
    for entry in history:
        if entry.get("topic") == topic and entry.get("location") == location and entry.get("time") == time_val:
            entry["participants"] = count
            found = True
            break
    
    if not found:
        history.append({
            "topic": topic,
            "location": location,
            "time": time_val,
            "participants": count,
            "created_by": created_by,
            "created_at": datetime.now().isoformat(),
            "guild_id": str(guild_id) if guild_id else ""
        })
    
    history = history[-50:]
    save_json(HISTORY_FILE, history)

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
        
        notes_index = -1
        for i, field in enumerate(embed.fields):
            if "หมายเหตุจากเพื่อนๆ" in field.name:
                notes_index = i
                break
                
        note_str = f"💬 **{interaction.user.display_name}:** {self.note_text.value}"
                
        if notes_index != -1:
            current_notes = embed.fields[notes_index].value
            if current_notes == "*ยังไม่มีหมายเหตุ*":
                new_notes = note_str
            else:
                new_notes = current_notes + f"\n{note_str}"
                
            embed.set_field_at(notes_index, name="📋 หมายเหตุจากเพื่อนๆ", value=new_notes, inline=False)
            await interaction.response.edit_message(embed=embed)
        else:
            await interaction.response.send_message("เกิดข้อผิดพลาด", ephemeral=True)

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
        
        # สร้าง Dropdown เลือกรอบฉาย
        view = ShowtimeSelectView(movie)
        
        embed = discord.Embed(
            title=f"🎬 {movie['name']}",
            description="เลือกรอบฉายที่ต้องการ:",
            color=0xE50914
        )
        if movie['image']:
            embed.set_thumbnail(url=movie['image'])
        
        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ รอบฉายทั้งหมด", value=showtimes_fmt, inline=False)
        
        await interaction.response.edit_message(embed=embed, view=view)

class MovieSelectView(discord.ui.View):
    def __init__(self, movies):
        super().__init__(timeout=120)
        self.add_item(MovieSelect(movies))

class ShowtimeSelect(discord.ui.Select):
    def __init__(self, movie):
        self.movie = movie
        options = []
        for t in movie['showtimes']:
            options.append(discord.SelectOption(
                label=f"🕐 {t}",
                value=t,
                emoji="🎟️"
            ))
        super().__init__(placeholder="🕐 เลือกรอบฉาย...", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        selected_time = self.values[0]
        movie = self.movie
        
        # สร้างการ์ดนัดหมายดูหนังอัตโนมัติ
        embed = discord.Embed(
            title=f"📅  🎬 {movie['name']}",
            description="━━━━━━━━━━━━━━━━━━━━━━",
            color=0x5865F2
        )
        
        embed.add_field(name="📍 สถานที่", value="```เมเจอร์ บิ๊กซี ลพบุรี```", inline=True)
        embed.add_field(name="⏰ เวลานัด", value=f"```{selected_time}```", inline=True)
        embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="🔢 จำนวนคน", value="` 0 คน `", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="👥 ใครไปบ้าง", value="*ยังไม่มีใครลงชื่อ*", inline=False)
        embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="📋 หมายเหตุจากเพื่อนๆ", value="*ยังไม่มีหมายเหตุ*", inline=False)
        
        if movie['image']:
            embed.set_thumbnail(url=movie['image'])
        
        embed.set_footer(text=f"🎯 สร้างโดย {interaction.user.display_name}  •  กดปุ่มด้านล่างเพื่อลงชื่อ!")
        embed.set_author(name="🔔 นัดดูหนัง!", icon_url=interaction.user.display_avatar.url)
        
        view = MeetupView()
        
        # แก้ไขข้อความ ephemeral เดิมให้เป็นข้อความสำเร็จ
        await interaction.response.edit_message(content="✅ **สร้างการนัดหมายสำเร็จแล้ว!** ดูที่ช่องแชทได้เลยครับ", embed=None, view=None)
        
        # ส่งการนัดหมายลงช่องแชทแบบ public พร้อมแท็ก @everyone
        await interaction.channel.send(
            content="@everyone 🍿 **มีการนัดดูหนังใหม่!**", 
            embed=embed, 
            view=view, 
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )
        
        # บันทึกประวัติ
        _save_meetup_from_embed(embed, interaction.guild_id)

class ShowtimeSelectView(discord.ui.View):
    def __init__(self, movie):
        super().__init__(timeout=120)
        self.add_item(ShowtimeSelect(movie))

# ==================== Bot ====================

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.movie_cache = load_movie_cache()
        self.movie_channel_id = None
        
        # โหลด config
        config = load_config()
        channel_id = config.get("movie_channel_id")
        if channel_id:
            self.movie_channel_id = int(channel_id)

    async def setup_hook(self):
        await self.tree.sync()
        self.add_view(MeetupView())
        self.check_movies.start()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    @tasks.loop(hours=1)
    async def check_movies(self):
        try:
            movies = await fetch_major_movies()
            new_movies = []
            for movie in movies:
                if movie['name'] not in self.movie_cache:
                    new_movies.append(movie)
                    self.movie_cache.add(movie['name'])
            
            # บันทึก cache ลงไฟล์
            if new_movies:
                save_movie_cache(self.movie_cache)
            
            # ถ้ามีหนังใหม่ และตั้งค่าช่องไว้ ให้ส่งแจ้งเตือน
            if new_movies and self.movie_channel_id:
                channel = self.get_channel(self.movie_channel_id)
                if channel:
                    for movie in new_movies:
                        embed = discord.Embed(
                            title=f"🆕 หนังเข้าใหม่!",
                            description=f"# 🎬 {movie['name']}",
                            color=0xE50914
                        )
                        if movie['image']:
                            embed.set_thumbnail(url=movie['image'])
                        
                        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
                        embed.add_field(name="⏰ รอบฉาย", value=showtimes_fmt, inline=False)
                        embed.add_field(name="📍 โรง", value="เมเจอร์ บิ๊กซี ลพบุรี", inline=False)
                        embed.set_footer(text="พิมพ์ /นัดดูหนัง เพื่อนัดเพื่อนไปดูด้วยกัน!")
                        
                        await channel.send(
                            content="@everyone 🍿 **มีหนังใหม่เข้าโรงแล้ว!**",
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions(everyone=True)
                        )
        except Exception as e:
            print(f"Error checking movies: {e}")

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
        label='เวลา',
        style=discord.TextStyle.short,
        placeholder='เช่น วันนี้ 20:00, พรุ่งนี้บ่ายโมง',
        required=True,
        max_length=50,
    )
    note = discord.ui.TextInput(
        label='หมายเหตุ (ไม่ใส่ก็ได้)',
        style=discord.TextStyle.long,
        placeholder='เช่น ใครสายจ่ายค่าข้าว, เตรียมเสื้อสีดำมา...',
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        topic_val = self.topic.value
        location_val = self.location.value
        time_val = self.time_str.value
        note_val = self.note.value if self.note.value else None

        embed = discord.Embed(
            title=f"📅  {topic_val}",
            description="━━━━━━━━━━━━━━━━━━━━━━",
            color=0x5865F2
        )
        
        embed.add_field(name="📍 สถานที่", value=f"```{location_val}```", inline=True)
        embed.add_field(name="⏰ เวลานัด", value=f"```{time_val}```", inline=True)
        
        if note_val:
            embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
            embed.add_field(name="📌 หมายเหตุ", value=f"> {note_val}", inline=False)
            
        embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="🔢 จำนวนคน", value="` 0 คน `", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="\u200b", value="\u200b", inline=True)
        embed.add_field(name="👥 ใครไปบ้าง", value="*ยังไม่มีใครลงชื่อ*", inline=False)
        embed.add_field(name="\u200b", value="━━━━━━━━━━━━━━━━━━━━━━", inline=False)
        embed.add_field(name="📋 หมายเหตุจากเพื่อนๆ", value="*ยังไม่มีหมายเหตุ*", inline=False)
        
        embed.set_footer(text=f"🎯 สร้างโดย {interaction.user.display_name}  •  กดปุ่มด้านล่างเพื่อลงชื่อ!")
        embed.set_author(name="🔔 การนัดหมายใหม่!", icon_url=interaction.user.display_avatar.url)
        
        view = MeetupView()
        await interaction.response.send_message(
            content="@everyone 🔔 **มีการนัดหมายใหม่!**", 
            embed=embed, 
            view=view,
            allowed_mentions=discord.AllowedMentions(everyone=True)
        )
        
        # บันทึกประวัติ
        save_meetup_history(topic_val, location_val, time_val, 0, interaction.user.display_name, str(interaction.guild_id))

@bot.tree.command(name="นัดเพื่อน", description="เปิดหน้าต่าง UI สร้างการนัดหมาย")
async def meetup(interaction: discord.Interaction):
    await interaction.response.send_modal(CreateMeetupModal())

# ==================== ฟีเจอร์ 1: ประวัตินัดหมาย ====================

@bot.tree.command(name="ประวัตินัด", description="ดูประวัตินัดหมายย้อนหลัง")
async def meetup_history(interaction: discord.Interaction):
    history = load_json(HISTORY_FILE, [])
    
    if not history:
        await interaction.response.send_message("📜 ยังไม่มีประวัตินัดหมายเลยครับ", ephemeral=True)
        return
    
    # แสดง 10 รายการล่าสุด (เรียงจากใหม่ไปเก่า)
    recent = list(reversed(history[-10:]))
    
    embed = discord.Embed(
        title="📜 ประวัตินัดหมาย",
        description="━━━━━━━━━━━━━━━━━━━━━━\nรายการนัดหมายล่าสุด 10 ครั้ง",
        color=0x5865F2
    )
    
    for i, entry in enumerate(recent, 1):
        # จัดวันที่
        try:
            dt = datetime.fromisoformat(entry.get("created_at", ""))
            date_str = dt.strftime("%d/%m/%Y %H:%M")
        except Exception:
            date_str = "ไม่ทราบวัน"
        
        topic = entry.get("topic", "ไม่ระบุ")
        location = entry.get("location", "ไม่ระบุ")
        time_val = entry.get("time", "ไม่ระบุ")
        participants = entry.get("participants", 0)
        created_by = entry.get("created_by", "ไม่ทราบ")
        
        embed.add_field(
            name=f"`{i}.` {topic}",
            value=(
                f"📍 {location}  •  ⏰ {time_val}\n"
                f"👥 {participants} คน  •  🗓️ {date_str}\n"
                f"สร้างโดย: {created_by}"
            ),
            inline=False
        )
    
    embed.set_footer(text=f"ทั้งหมด {len(history)} นัดหมาย")
    await interaction.response.send_message(embed=embed, ephemeral=True)

# ==================== ฟีเจอร์ 2: นัดดูหนังอัตโนมัติ ====================

@bot.tree.command(name="นัดดูหนัง", description="เลือกหนังจากเมเจอร์แล้วสร้างนัดหมายอัตโนมัติ")
async def movie_meetup(interaction: discord.Interaction):
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

# ==================== ฟีเจอร์ 3: ตั้งค่าห้องแจ้งเตือนหนังใหม่ ====================

@bot.tree.command(name="ตั้งค่าแจ้งเตือนหนัง", description="ตั้งค่าห้องแชทที่ต้องการให้บอทแจ้งเตือนหนังใหม่")
@app_commands.describe(channel="เลือกห้องแชทที่ต้องการ")
async def set_movie_channel(interaction: discord.Interaction, channel: discord.TextChannel):
    bot.movie_channel_id = channel.id
    
    # บันทึกลง config
    config = load_config()
    config["movie_channel_id"] = str(channel.id)
    save_config(config)
    
    embed = discord.Embed(
        title="✅ ตั้งค่าสำเร็จ!",
        description=f"บอทจะส่งแจ้งเตือนหนังเข้าใหม่ไปที่ {channel.mention}\n\nระบบจะเช็คหนังใหม่ทุกๆ 1 ชั่วโมงอัตโนมัติ",
        color=0x57F287
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)

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
                if not script.string: continue
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
                                movie_dict[movie_name]["showtimes"].append(time_str)
                except json.JSONDecodeError:
                    continue
            
            for k in movie_dict:
                movie_dict[k]['showtimes'].sort()
                
            return list(movie_dict.values())

# ==================== เช็ครอบหนัง ====================

@bot.tree.command(name="เช็คหนัง", description="เช็ครอบหนังที่เมเจอร์ บิ๊กซี ลพบุรี")
async def check_movies_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    movies = await fetch_major_movies()
    
    if not movies:
        await interaction.followup.send("ไม่พบข้อมูลรอบหนังในขณะนี้")
        return
        
    embeds = []
    intro_embed = discord.Embed(title="🍿 รอบหนังเมเจอร์ บิ๊กซี ลพบุรี วันนี้", color=discord.Color.red())
    embeds.append(intro_embed)
    
    for movie in movies:
        embed = discord.Embed(title=f"🎬 {movie['name']}", color=discord.Color.red())
        
        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ รอบฉาย", value=showtimes_fmt, inline=False)
        
        if movie['image']:
            embed.set_thumbnail(url=movie['image'])
            
        embeds.append(embed)
        
    await interaction.followup.send(embeds=embeds[:10])

# ==================== รัน ====================

if __name__ == '__main__':
    keep_alive()
    if TOKEN == 'YOUR_BOT_TOKEN_HERE' or not TOKEN:
        print("กรุณาใส่ Token หรือตั้งค่า DISCORD_TOKEN ใน Environment Variables")
    else:
        bot.run(TOKEN)
