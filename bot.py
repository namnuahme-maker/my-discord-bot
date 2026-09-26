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
        super().__init__(timeout=None) # ให้ปุ่มอยู่ได้ตลอดไป

    @discord.ui.button(label="ลงชื่อไป", style=discord.ButtonStyle.green, custom_id="join_meetup")
    async def join_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # ดึง embed ปัจจุบัน
        embed = interaction.message.embeds[0]
        
        # หา field "ใครไปบ้าง"
        participants_index = -1
        for i, field in enumerate(embed.fields):
            if field.name == "👥 ใครไปบ้าง":
                participants_index = i
                break
                
        if participants_index != -1:
            current_participants = embed.fields[participants_index].value
            user_mention = interaction.user.mention
            
            if current_participants == "-":
                new_participants = user_mention
            elif user_mention not in current_participants:
                new_participants = current_participants + f"\n{user_mention}"
            else:
                await interaction.response.send_message("คุณลงชื่อไปแล้ว!", ephemeral=True)
                return
                
            embed.set_field_at(participants_index, name="👥 ใครไปบ้าง", value=new_participants, inline=False)
            await interaction.response.edit_message(embed=embed)
            await interaction.followup.send("ลงชื่อสำเร็จ!", ephemeral=True)
        else:
            await interaction.response.send_message("เกิดข้อผิดพลาด ไม่พบช่องลงชื่อ", ephemeral=True)

    @discord.ui.button(label="เพิ่มหมายเหตุ", style=discord.ButtonStyle.secondary, custom_id="note_meetup")
    async def note_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        # เปิด Modal ให้พิมพ์หมายเหตุ
        await interaction.response.send_modal(NoteModal())

class NoteModal(discord.ui.Modal, title='เพิ่มหมายเหตุ'):
    note_text = discord.ui.TextInput(
        label='พิมพ์หมายเหตุของคุณ',
        style=discord.TextStyle.long,
        placeholder='เช่น ขอไปสาย 10 นาทีนะ...',
        required=True,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        embed = interaction.message.embeds[0]
        
        notes_index = -1
        for i, field in enumerate(embed.fields):
            if field.name == "📝 หมายเหตุเพิ่มเติม":
                notes_index = i
                break
                
        note_str = f"**{interaction.user.display_name}**: {self.note_text.value}"
                
        if notes_index != -1:
            current_notes = embed.fields[notes_index].value
            if current_notes == "-":
                new_notes = note_str
            else:
                new_notes = current_notes + f"\n{note_str}"
                
            embed.set_field_at(notes_index, name="📝 หมายเหตุเพิ่มเติม", value=new_notes, inline=False)
            await interaction.response.edit_message(embed=embed)
            await interaction.followup.send("เพิ่มหมายเหตุแล้ว!", ephemeral=True)
        else:
            await interaction.response.send_message("เกิดข้อผิดพลาด ไม่พบช่องหมายเหตุ", ephemeral=True)

class MyBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        super().__init__(command_prefix='!', intents=intents)
        self.movie_cache = set()
        self.movie_channel_id = None # ใส่ ID ห้องที่อยากให้แจ้งเตือนหนังใหม่ (ถ้าต้องการ)

    async def setup_hook(self):
        # Sync slash commands
        await self.tree.sync()
        self.add_view(MeetupView())
        self.check_movies.start()

    async def on_ready(self):
        print(f'Logged in as {self.user} (ID: {self.user.id})')
        print('------')

    @tasks.loop(hours=1) # เช็คทุกๆ 1 ชั่วโมง
    async def check_movies(self):
        try:
            movies = await fetch_major_movies()
            new_movies = []
            for movie in movies:
                if movie['name'] not in self.movie_cache:
                    new_movies.append(movie)
                    self.movie_cache.add(movie['name'])
            
            # ถ้ามีหนังใหม่ และตั้งค่าช่องไว้ ให้ส่งแจ้งเตือน
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

class CreateMeetupModal(discord.ui.Modal, title='สร้างการนัดหมาย'):
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
        placeholder='เช่น หอ A, ร้านประจำ, Discord',
        required=True,
        max_length=100,
    )
    time_str = discord.ui.TextInput(
        label='เวลา',
        style=discord.TextStyle.short,
        placeholder='เช่น 20:00, วันนี้ทุ่มตรง',
        required=True,
        max_length=50,
    )
    note = discord.ui.TextInput(
        label='หมายเหตุ (ใส่หรือไม่ใส่ก็ได้)',
        style=discord.TextStyle.long,
        placeholder='เช่น ใครสายจ่ายค่าข้าว...',
        required=False,
        max_length=300,
    )

    async def on_submit(self, interaction: discord.Interaction):
        topic_val = self.topic.value
        location_val = self.location.value
        time_val = self.time_str.value
        note_val = self.note.value if self.note.value else "-"

        embed = discord.Embed(title=f"📢 **{topic_val}**", color=discord.Color.blue())
        embed.add_field(name="📍 ที่ไหน", value=location_val, inline=False)
        embed.add_field(name="⏰ เวลาเท่าไหร่", value=time_val, inline=False)
        
        if note_val != "-":
            embed.add_field(name="📌 หมายเหตุตั้งต้น", value=note_val, inline=False)
            
        embed.add_field(name="👥 ใครไปบ้าง", value="-", inline=False)
        embed.add_field(name="📝 หมายเหตุเพิ่มเติม", value="-", inline=False)
        embed.set_footer(text=f"สร้างโดย {interaction.user.display_name}")
        
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
