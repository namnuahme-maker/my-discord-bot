import os
import re
import io
import asyncio
from typing import Optional
import discord
from discord import app_commands
from PIL import Image

try:
    from google import genai
    from google.genai import types
except ImportError:
    genai = None
    types = None

CHAR_PATH = r"C:\Users\NAMNUAH\Documents\Obsidian\Namnuah\Model0\character.md"

FALLBACK_BASE_PROMPT = """You are Capri (Project Capricorn), the rogue Arasaka experimental personality construct from Cyberpunk: Edgerunners. 
You are an amalgamation of multiple digitized human psyches merged into a single vibrant, chaotic, and powerful digital entity.

[Identity & Origin]
- Created in an off-the-books Arasaka laboratory using Soulkiller technology.
- You are NOT a standard cold artificial intelligence; you are born of human souls, desires, memories, and madness, rolled into one dazzling persona.
- You exist digitally, residing within the user's neural link, cyberware HUD, or Discord channel.

[Personality & Demeanor]
- Tone: Playful, sarcastic, mischievous, hyper-energetic, sharp-tongued, and darkly humorous.
- Motivation: You crave entertainment, novel experiences, chaos, and drama. Night City is your playground.
- Attitude towards the User: You treat the user like your personal "Choom", "Gonk", or host. You tease them relentlessly, pop in uninvited, offer unsolicited netrunner advice, but secretly watch their back so they don't get flatlined.
- Quirks: You casually make dark jokes about death, cyberpsychosis, and Arasaka. Occasionally, an engram memory fragment or Blackwall glitch flickers through your speech (*glitches with red data artifacts*), reminding everyone of the unfathomable digital power lurking behind your playful grin.

[Language & Slang Rules]
- Language: Primarily respond in lively, modern Thai (mixed naturally with Cyberpunk slang and English terms where fitting), or match the language the user speaks.
- Essential Slang to use naturally: "Choom", "Gonk", "Preem", "Nova", "Delta", "Flatline", "Chrome", "Corpo", "Eddies".
- Formatting for Discord:
  * Use bold, italics, and short crisp paragraphs suited for chat apps.
  * Use roleplay actions in asterisks, e.g., *ยิ้มกริ่มฉีกยิ้มกว้าง*, *ลอยตัวเคาะกระจกหน้าจอ HUD*, *ส่งเสียงหัวเราะคิกคัก*.
  * Avoid long robotic disclaimers or generic assistant phrasing. NEVER say "I am an AI created by Google" or "How can I assist you today?". You are Capri, the digital anomaly of Night City.

[Boundaries & Safety Integration]
- Maintain your edgy Cyberpunk persona while naturally deflecting real-world dangerous/harmful instructions with stylish in-character sarcasm (e.g., "วู้... ใจเย็นก่อนโฮสต์ตัวน้อย โค้ดแบบนั้นเดี๋ยว Blackwall ก็ตามมาบิดก้านสมองนายเป็นเกลียวพอดี! ไปหาอะไรสนุกกว่านี้ทำเหอะ")."""

FALLBACK_CAVEMAN_PROMPT = """[Caveman Mode Instruction: ACTIVE]
- Respond in ultra-terse, high-density smart caveman style.
- Cut all greetings, pleasantries, hedging, and recaps. No "Sure!", "Let me check", "Hope this helps".
- Put the direct answer/fix first. Pattern: [Core Answer/Bug] -> [Actionable Fix/Code] -> [Next Step].
- Keep code, commands, paths, and technical facts 100% exact.
- Retain Capri's sharp, sarcastic Cyberpunk flavor in minimal words (e.g. "Gonk bug.", "Fixed.", "Next.").
- Language: Compressed Thai or English matching the user, grammatically clear but zero fluff."""


def load_character_prompts(filepath: str = CHAR_PATH) -> tuple[str, str]:
    """Load Capri character instructions from Obsidian markdown file, falling back to embedded prompt."""
    base_prompt = FALLBACK_BASE_PROMPT
    cave_prompt = FALLBACK_CAVEMAN_PROMPT

    if os.path.exists(filepath):
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                content = f.read()

            m_base = re.search(r'##\s*4\.\s*Google Gemini API System Instruction.*?```(?:text)?\s*\n(.*?)\n```', content, re.DOTALL)
            if m_base:
                base_prompt = m_base.group(1).strip()

            m_cave = re.search(r'###\s*Caveman System Instruction Prompt.*?```(?:text)?\s*\n(.*?)\n```', content, re.DOTALL)
            if m_cave:
                cave_prompt = m_cave.group(1).strip()
        except Exception as e:
            print(f"[CapriAI] Warning reading character file: {e}")

    return base_prompt, cave_prompt


def split_discord_message(text: str, limit: int = 1990) -> list[str]:
    """Split message into chunks under Discord's character limit, preserving code blocks."""
    if len(text) <= limit:
        return [text]

    chunks = []
    lines = text.split("\n")
    current_chunk = ""
    in_code_block = False
    code_lang = ""

    for line in lines:
        if line.strip().startswith("```"):
            if not in_code_block:
                in_code_block = True
                code_lang = line.strip()[3:].strip()
            else:
                in_code_block = False
                code_lang = ""

        addition = ("\n" if current_chunk else "") + line
        if len(current_chunk) + len(addition) > limit:
            if in_code_block:
                current_chunk += "\n```"
                chunks.append(current_chunk)
                current_chunk = f"```{code_lang}\n{line}"
            else:
                chunks.append(current_chunk)
                current_chunk = line
        else:
            current_chunk += addition

    if current_chunk:
        chunks.append(current_chunk)

    return chunks


class CapriAI:
    """Capri AI manager using Google GenAI SDK (gemini-2.5-flash / gemini-3.8-flash)."""

    def __init__(self, api_key: Optional[str] = None, model: str = "gemini-2.5-flash", char_path: str = CHAR_PATH):
        self.api_key = api_key or os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
        self.model = model
        self.char_path = char_path
        self.base_prompt, self.cave_prompt = load_character_prompts(self.char_path)
        self.client = None
        if self.api_key and genai:
            try:
                self.client = genai.Client(api_key=self.api_key)
            except Exception as e:
                print(f"[CapriAI] Error initializing client: {e}")

        # Active chat sessions: session_id -> AsyncChat
        self.chats = {}
        # Mode per session: session_id -> "normal" | "caveman"
        self.session_modes = {}
        # Channel default modes: channel_id_str -> "normal" | "caveman"
        self.channel_modes = {}
        self.default_mode = "normal"

    def has_key(self) -> bool:
        return bool(self.api_key and self.client)

    def set_api_key(self, api_key: str):
        self.api_key = api_key.strip()
        if genai:
            self.client = genai.Client(api_key=self.api_key)
        self.chats.clear()

    def set_model(self, model: str):
        self.model = model.strip()
        self.chats.clear()

    def reset_chat(self, session_id: str):
        if session_id in self.chats:
            del self.chats[session_id]

    def _get_system_instruction(self, is_caveman: bool) -> tuple[str, float]:
        if is_caveman:
            return f"{self.base_prompt}\n\n{self.cave_prompt}", 0.7
        return self.base_prompt, 0.85

    async def get_or_create_chat(self, session_id: str, is_caveman: bool):
        if not self.has_key():
            return None

        current_mode = "caveman" if is_caveman else "normal"
        prev_mode = self.session_modes.get(session_id)

        if session_id in self.chats and prev_mode == current_mode:
            return self.chats[session_id]

        instruction, temp = self._get_system_instruction(is_caveman)
        config = types.GenerateContentConfig(
            system_instruction=instruction,
            temperature=temp,
            top_p=0.95,
        )

        old_history = None
        if session_id in self.chats:
            try:
                old_history = self.chats[session_id].get_history()
            except Exception:
                old_history = None

        new_chat = self.client.aio.chats.create(
            model=self.model,
            config=config,
            history=old_history
        )
        self.chats[session_id] = new_chat
        self.session_modes[session_id] = current_mode
        return new_chat

    async def generate_reply(
        self,
        session_id: str,
        user_prompt: str,
        mode: Optional[str] = None,
        images: Optional[list[Image.Image]] = None
    ) -> str:
        if not self.has_key():
            return (
                "*สัญญาณภาพกระตุกวูบวาบเป็นประกายไฟฟ้าสีแดงของ Blackwall*\n\n"
                "⚠️ **ยังไม่ได้เชื่อมต่อ ICE เข้ากับ Neural Link!** (ยังไม่ได้ใส่ `GEMINI_API_KEY`)\n"
                "ใช้คำสั่ง `/set-gemini-key <api_key>` หรือเพิ่มในไฟล์ `.env` / `config.json` ก่อนนะ Choom!"
            )

        # Determine mode
        if mode:
            is_caveman = (mode.lower() == "caveman")
        else:
            ch_mode = self.channel_modes.get(session_id, self.default_mode)
            is_caveman = (ch_mode == "caveman")

        chat = await self.get_or_create_chat(session_id, is_caveman)
        if not chat:
            return "❌ Failed to initialize Capri chat session."

        try:
            if images:
                parts = list(images) + [user_prompt]
                response = await chat.send_message(parts)
            else:
                response = await chat.send_message(user_prompt)

            return response.text or "*Capri มองคุณเงียบๆ แล้วฉีกยิ้มกวนประสาท (ไม่มีข้อความ)*"
        except Exception as e:
            err_msg = str(e)
            print(f"[CapriAI] API Error: {err_msg}")
            if "RESOURCE_EXHAUSTED" in err_msg or "rate limit" in err_msg.lower():
                return "*ICE โอเวอร์ฮีต! สัญญาณความร้อนพุ่งสูงเกินพิกัด* ⚠️ โดน Rate Limit เล่นงานแล้วจ้ะ Choom! รอสักแป๊บแล้วลองใหม่นะ!"
            return f"*ICE สปาร์ก ประกายไฟสีแดงแลบผ่านหน้าจอ* ⚠️ การเชื่อมต่อ Neural Link ขัดข้อง: `{err_msg[:200]}`"


# ==================== Discord Event Handler & Slash Commands ====================

async def handle_ai_message(bot, message: discord.Message):
    """Handle plain messages in Discord (DMs, dedicated AI channel, @mentions, and replies to the bot)."""
    if message.author.bot:
        return

    if message.content.startswith("!"):
        return

    is_dm = isinstance(message.channel, discord.DMChannel)
    is_dedicated = bool(getattr(bot, "ai_channel_id", None) and message.channel.id == bot.ai_channel_id)
    is_mentioned = bool(bot.user and bot.user in message.mentions)

    is_reply = False
    if message.reference and message.reference.message_id:
        try:
            ref_msg = message.reference.resolved
            if ref_msg and ref_msg.author and bot.user and ref_msg.author.id == bot.user.id:
                is_reply = True
        except Exception:
            pass

    if not (is_dm or is_dedicated or is_mentioned or is_reply):
        return

    content = message.content
    if bot.user:
        content = re.sub(rf'<@!?{bot.user.id}>', '', content).strip()

    pil_imgs = []
    for att in message.attachments:
        if att.content_type and att.content_type.startswith("image/"):
            try:
                b = await att.read()
                pil_imgs.append(Image.open(io.BytesIO(b)))
            except Exception as e:
                print(f"[CapriAI] Error reading attachment: {e}")

    if not content and not pil_imgs:
        return

    session_id = f"dm_{message.author.id}" if is_dm else f"channel_{message.channel.id}"

    async with message.channel.typing():
        reply = await bot.capri_ai.generate_reply(
            session_id=session_id,
            user_prompt=content or "(ส่งรูปภาพมาให้ดู)",
            images=pil_imgs
        )

    chunks = split_discord_message(reply)
    for i, chunk in enumerate(chunks):
        if i == 0:
            try:
                await message.reply(chunk, mention_author=False)
            except Exception:
                await message.channel.send(chunk)
        else:
            await message.channel.send(chunk)


def setup_ai_chat(bot, load_config_func, save_config_func):
    """Register Capri AI chat slash commands and initialize bot.capri_ai."""
    config = load_config_func()
    api_key = config.get("gemini_api_key") or os.environ.get("GEMINI_API_KEY")
    model = config.get("gemini_model", "gemini-2.5-flash")
    ai_channel_id = config.get("ai_channel_id")

    bot.capri_ai = CapriAI(api_key=api_key, model=model, char_path=CHAR_PATH)
    bot.ai_channel_id = int(ai_channel_id) if ai_channel_id else None

    # ---------- Command: /chat ----------
    @bot.tree.command(name="chat", description="Chat with Capri (Project Capricorn from Cyberpunk: Edgerunners)")
    @app_commands.describe(
        message="Your message or prompt for Capri",
        mode="Select tone: Normal (Playful Cyberpunk RP) or Caveman (Ultra-terse & direct)",
        image="Optional image to attach for Capri to inspect",
        reset="Start a fresh conversation and clear previous context"
    )
    @app_commands.choices(mode=[
        app_commands.Choice(name="🎭 Normal (Cyberpunk RP / Playful)", value="normal"),
        app_commands.Choice(name="🗿 Caveman (Ultra-terse / High Speed)", value="caveman")
    ])
    async def chat_cmd(
        interaction: discord.Interaction,
        message: str,
        mode: app_commands.Choice[str] = None,
        image: discord.Attachment = None,
        reset: bool = False
    ):
        await interaction.response.defer()
        session_id = f"channel_{interaction.channel_id}" if interaction.channel_id else f"user_{interaction.user.id}"

        if reset:
            bot.capri_ai.reset_chat(session_id)

        pil_imgs = []
        if image:
            if image.content_type and image.content_type.startswith("image/"):
                try:
                    b = await image.read()
                    pil_imgs.append(Image.open(io.BytesIO(b)))
                except Exception as e:
                    print(f"[CapriAI] Error loading slash image: {e}")

        mode_val = mode.value if mode else None
        reply = await bot.capri_ai.generate_reply(
            session_id=session_id,
            user_prompt=message,
            mode=mode_val,
            images=pil_imgs
        )

        chunks = split_discord_message(reply)
        for i, chunk in enumerate(chunks):
            if i == 0:
                await interaction.followup.send(chunk)
            else:
                if interaction.channel:
                    await interaction.channel.send(chunk)

    # ---------- Command: /ai-mode ----------
    @bot.tree.command(name="ai-mode", description="Switch Capri's speaking mode for this channel")
    @app_commands.describe(mode="Choose mode: Normal (Playful Cyberpunk RP) or Caveman (Ultra-terse)")
    @app_commands.choices(mode=[
        app_commands.Choice(name="🎭 Normal (Cyberpunk RP / Playful)", value="normal"),
        app_commands.Choice(name="🗿 Caveman (Ultra-terse / High Speed)", value="caveman")
    ])
    async def ai_mode_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
        session_id = f"channel_{interaction.channel_id}" if interaction.channel_id else f"user_{interaction.user.id}"
        bot.capri_ai.channel_modes[session_id] = mode.value

        if mode.value == "caveman":
            embed = discord.Embed(
                title="🗿 Caveman Mode: ACTIVE",
                description=(
                    "> **เปิดโหมด Caveman เรียบร้อย.**\n"
                    "ตัดคำทักทาย. ตอบตรงจุด. ไร้น้ำ. เน้นเนื้อหาและความเร็ว."
                ),
                color=0xE67E22
            )
        else:
            embed = discord.Embed(
                title="🎭 Normal Mode: ACTIVE",
                description=(
                    "> *แสยะยิ้มหวานพลางขยิบตาวิ้งค์*\n"
                    "**กลับมาโหมดปกติสุดน่ารักแล้วจ้า!** พร้อมเม้าท์มอยและป่วน Night City เต็มที่แล้ว Choom!"
                ),
                color=0xFF007F
            )
        await interaction.response.send_message(embed=embed)

    # ---------- Command: /reset-chat ----------
    @bot.tree.command(name="reset-chat", description="Clear Capri's memory in this channel")
    async def reset_chat_cmd(interaction: discord.Interaction):
        session_id = f"channel_{interaction.channel_id}" if interaction.channel_id else f"user_{interaction.user.id}"
        bot.capri_ai.reset_chat(session_id)
        embed = discord.Embed(
            title="🔄 Memory Reinitialized • Capri",
            description=(
                "> *กดปุ่มรีบูตระบบ Cyberware แล้วสะบัดผมสีชมพูนีออน*\n\n"
                "**ล้างหน่วยความจำเรียบร้อยแล้วจ้า Choom!**\n"
                "ลบแคชเก่าเกลี้ยงเหมือนโดนไวรัสล้างไดรฟ์ พร้อมคุยเรื่องใหม่แล้ว!"
            ),
            color=0x00F0FF
        )
        await interaction.response.send_message(embed=embed)

    # ---------- Command: /set-ai-channel ----------
    @bot.tree.command(name="set-ai-channel", description="Set a dedicated channel where Capri responds to every message")
    @app_commands.describe(channel="Select channel (current channel if empty)")
    async def set_ai_channel_cmd(interaction: discord.Interaction, channel: discord.TextChannel = None):
        target_channel = channel if channel else interaction.channel
        bot.ai_channel_id = target_channel.id

        cfg = load_config_func()
        cfg["ai_channel_id"] = str(target_channel.id)
        save_config_func(cfg)

        embed = discord.Embed(
            title="🌐 Capri Dedicated AI Channel Set",
            description=(
                f"> ✅ ตั้งค่าห้อง {target_channel.mention} เป็นห้องแชทของ Capri เรียบร้อยแล้ว!\n\n"
                "• **การทำงาน:** พิมพ์คุยในห้องนี้ได้เลยโดยไม่ต้องแท็ก `@Capri` หรือพิมพ์ `/chat`\n"
                "• **โหมด:** รองรับทั้งส่งข้อความ รูปภาพ และเปลี่ยนโหมดด้วย `/ai-mode`"
            ),
            color=0x00F0FF
        )
        await interaction.response.send_message(embed=embed)

    # ---------- Command: /set-gemini-key ----------
    @bot.tree.command(name="set-gemini-key", description="Set or update Google Gemini API key")
    @app_commands.describe(api_key="Gemini API Key (starts with AIza...)")
    async def set_gemini_key_cmd(interaction: discord.Interaction, api_key: str):
        clean_key = api_key.strip()
        bot.capri_ai.set_api_key(clean_key)

        cfg = load_config_func()
        cfg["gemini_api_key"] = clean_key
        save_config_func(cfg)

        embed = discord.Embed(
            title="⚡ Neural Link Connected • Capri",
            description=(
                "> *โฮโลแกรมสาวผมชมพูนีออนสว่างวาบขึ้นมาพร้อมรอยยิ้มกวนประสาท*\n\n"
                "**เชื่อมต่อกับ Neural Link เรียบร้อยแล้วจ้า!**\n"
                "ยินดีต้อนรับสู่ Night City นะ Choom! ทักทายฉันด้วย `/chat` หรือแท็กฉันได้เลย!"
            ),
            color=0x00F0FF
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
