import os
from flask import Flask
from threading import Thread

app = Flask('')

BOT_STATUS = {
    "connected": False,
    "bot_name": None,
    "message": "⏳ กำลังเริ่มต้นเชื่อมต่อกับ Discord...",
}

def set_bot_status(connected: bool, message: str, bot_name: str = None):
    BOT_STATUS["connected"] = connected
    BOT_STATUS["message"] = message
    if bot_name is not None:
        BOT_STATUS["bot_name"] = bot_name

@app.route('/')
def home():
    status_icon = "🟢 ออนไลน์" if BOT_STATUS["connected"] else "🔴 ยังไม่เชื่อมต่อ"
    bot_info = f"<p><b>บอท:</b> {BOT_STATUS['bot_name']}</p>" if BOT_STATUS["bot_name"] else ""
    return (
        f"<html><head><meta charset='utf-8'><title>Discord Bot Status</title></head>"
        f"<body style='font-family:sans-serif;padding:24px;'>"
        f"<h2>สถานะบอท: {status_icon}</h2>"
        f"{bot_info}"
        f"<p>{BOT_STATUS['message']}</p>"
        f"</body></html>"
    )

def run():
    # Render จะทำการจ่ายพอร์ตให้เราผ่านตัวแปร PORT ถ้าไม่มีให้ใช้ 8080
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run, daemon=True)
    t.start()
