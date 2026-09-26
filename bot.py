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

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.movie_cache = set()
        self.movie_channel_id = None

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
            
            if new_movies and self.movie_channel_id:
                channel = self.get_channel(self.movie_channel_id)
                if channel:
                    for movie in new_movies:
                        embed = discord.Embed(title=f"🎬 หนังเข้าใหม่! {movie['name']}", color=discord.Color.red())
                        embed.set_thumbnail(url=movie['image'])
                        embed.add_field(name="รอบฉายเร็วๆ นี้", value="\n".join(movie['showtimes'][:5]), inline=False)
                        await channel.send(embed=embed)
        except Exception as e:
            print(f"Error checking movies: {e}")

bot = MyBot()

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
            color=0x5865F2  # สีม่วงดิสคอร์ด
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
        await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="นัดเพื่อน", description="เปิดหน้าต่าง UI สร้างการนัดหมาย")
async def meetup(interaction: discord.Interaction):
    await interaction.response.send_modal(CreateMeetupModal())

async def fetch_major_movies():
    url = "https://www.majorcineplex.com/cinema/bigc-lopburi/"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            html = await response.text()
            soup = BeautifulSoup(html, 'html.parser')
            
            # หา script ที่เป็น application/ld+json
            scripts = soup.find_all('script', type='application/ld+json')
            
            movie_dict = {}
            for script in scripts:
                if not script.string: continue
                try:
                    data = json.loads(script.string)
                    # โครงสร้าง json ld มี @graph
                    if '@context' in data and '@graph' in data:
                        for item in data['@graph']:
                            if item.get('@type') == 'ScreeningEvent':
                                # ชื่อหนังจาก workPresented (เน้นภาษาอังกฤษ)
                                work = item.get('workPresented', {})
                                movie_name = work.get('alternateName')
                                if not movie_name:
                                    movie_name = work.get('name', 'Unknown')
                                movie_image = work.get('image', '')
                                
                                # รอบฉายจาก startDate
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
            
            # เรียงรอบฉาย
            for k in movie_dict:
                movie_dict[k]['showtimes'].sort()
                
            return list(movie_dict.values())

@bot.tree.command(name="เช็คหนัง", description="เช็ครอบหนังที่เมเจอร์ บิ๊กซี ลพบุรี")
async def check_movies_cmd(interaction: discord.Interaction):
    await interaction.response.defer()
    movies = await fetch_major_movies()
    
    if not movies:
        await interaction.followup.send("ไม่พบข้อมูลรอบหนังในขณะนี้")
        return
        
    # หน้าแรกใส่หัวข้อนำหน้า
    embeds = []
    intro_embed = discord.Embed(title="🍿 รอบหนังเมเจอร์ บิ๊กซี ลพบุรี วันนี้", color=discord.Color.red())
    embeds.append(intro_embed)
    
    for movie in movies:
        embed = discord.Embed(title=f"🎬 {movie['name']}", color=discord.Color.red())
        
        # จัดข้อความรอบฉายให้เป็นป้ายกำกับสวยๆ
        showtimes_fmt = " ".join([f"` {t} `" for t in movie['showtimes']])
        embed.add_field(name="⏰ รอบฉาย", value=showtimes_fmt, inline=False)
        
        if movie['image']:
            embed.set_thumbnail(url=movie['image'])
            
        embeds.append(embed)
        
    # ส่งทั้งหมดในข้อความเดียว (discord รองรับสูงสุด 10 embeds ต่อข้อความ)
    await interaction.followup.send(embeds=embeds[:10])

if __name__ == '__main__':
    keep_alive()
    if TOKEN == 'YOUR_BOT_TOKEN_HERE' or not TOKEN:
        print("กรุณาใส่ Token หรือตั้งค่า DISCORD_TOKEN ใน Environment Variables")
    else:
        bot.run(TOKEN)
