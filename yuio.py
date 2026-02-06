# ================== Imports ==================
import os
import re
import time
import random
import threading
from queue import Queue

from flask import Flask
import telebot
from telebot import types
from instagrapi import Client
threading.Thread(target=run_web, daemon=True).start()
# ================== Environment ==================
TOKEN = os.getenv("BOT_TOKEN")
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

# ================== Telegram ==================
bot = telebot.TeleBot(TOKEN)

# ================== Instagram Client ==================
cl = Client()

if os.path.exists("session.json"):
    cl.load_settings("session.json")
    cl.login_by_sessionid(cl.settings["sessionid"])
else:
    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError("Instagram credentials missing for first login")
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings("session.json")

# ================== Queue System ==================
ig_queue = Queue()

def ig_delay():
    time.sleep(random.randint(8, 15))

def ig_worker():
    while True:
        task = ig_queue.get()
        if task is None:
            break

        func, args = task
        try:
            func(*args)
        except Exception as e:
            print("IG ERROR:", e)

        ig_delay()
        ig_queue.task_done()

threading.Thread(target=ig_worker, daemon=True).start()

# ================== Flask (Keep Alive for Koyeb) ==================
app = Flask(__name__)

@app.route("/")
def home():
    return "Bot is Alive!"

def run_web():
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)

threading.Thread(target=run_web, daemon=True).start()

# ================== Helpers ==================
def extract_instagram_url(text: str):
    pattern = r"(https?://(?:www\.)?instagram\.com/[^\s]+)"
    match = re.search(pattern, text)
    return match.group(1) if match else None

# ================== Telegram Handlers ==================
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(
        message,
        "👋 أهلاً\nأرسل رابط إنستغرام (بوست أو ريلز) وسيتم تحميله."
    )

@bot.message_handler(func=lambda m: m.text and "instagram.com" in m.text)
def handle_instagram(message):
    url = extract_instagram_url(message.text)
    if not url:
        bot.reply_to(message, "❌ رابط غير صالح")
        return

    try:
        media_pk = cl.media_pk_from_url(url)
    except Exception:
        bot.reply_to(message, "⚠️ لا يمكن قراءة الرابط")
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("تحميل 📥", callback_data=f"dl_{media_pk}"),
        types.InlineKeyboardButton("معلومات ℹ️", callback_data=f"info_{media_pk}")
    )

    bot.reply_to(message, "اختر العملية:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    try:
        action, media_pk = call.data.split("_")
        media_pk = int(media_pk)
    except ValueError:
        return

    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id, "⏳ تم إضافة الطلب للطابور")

    if action == "dl":
        ig_queue.put((process_download, (chat_id, media_pk)))
    elif action == "info":
        ig_queue.put((process_info, (chat_id, media_pk)))

# ================== IG Tasks ==================
def process_download(chat_id, media_pk):
    media = cl.media_info(media_pk)

    if media.media_type == 2:  # Video / Reel
        bot.send_video(chat_id, media.video_url, caption="✅ تم التحميل")
    else:
        bot.send_photo(chat_id, media.thumbnail_url, caption="✅ تم التحميل")

def process_info(chat_id, media_pk):
    media = cl.media_info(media_pk)

    info = (
        f"👤 {media.user.username}\n"
        f"❤️ {media.like_count}\n"
        f"💬 {media.comment_count}"
    )
    bot.send_message(chat_id, info)

# ================== Start ==================
print("🤖 Bot is running...")
bot.polling(none_stop=True)




