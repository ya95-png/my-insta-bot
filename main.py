import os
import re
import time
import random
import threading
from queue import Queue
from contextlib import suppress

from flask import Flask, request, abort
import telebot
from telebot import types
from instagrapi import Client

# ================== Config / Env ==================
TOKEN = os.getenv("BOT_TOKEN")

IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

PUBLIC_URL = os.getenv("PUBLIC_URL")  # مثال: https://xxxxx.koyeb.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # سترنغ عشوائي قوي
WEBHOOK_BASE_PATH = os.getenv("WEBHOOK_BASE_PATH", "/telegram-webhook")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing")

WEBHOOK_PATH = f"{WEBHOOK_BASE_PATH}/{WEBHOOK_SECRET}"
WEBHOOK_URL = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH

# ================== Telegram ==================
bot = telebot.TeleBot(TOKEN, threaded=True)

# ================== Instagram ==================
IG_SESSIONID = os.getenv("IG_SESSIONID")

def ig_login() -> bool:
    """
    Login using IG_SESSIONID only.
    Safe: never crashes the service.
    """
    try:
        if not IG_SESSIONID:
            print("⚠️ IG_SESSIONID is missing")
            return False

        # Load settings if exists (optional)
        if os.path.exists("session.json"):
            with suppress(Exception):
                cl.load_settings("session.json")

        cl.login_by_sessionid(IG_SESSIONID)

        # Save settings to reuse (optional)
        with suppress(Exception):
            cl.dump_settings("session.json")

        return True

    except Exception as e:
        print("⚠️ Instagram session login failed:", e)
        return False


# ================== Queues ==================
tg_queue: Queue = Queue()
ig_queue: Queue = Queue()

def ig_delay():
    time.sleep(random.randint(8, 15))

def tg_worker():
    while True:
        upd = tg_queue.get()
        if upd is None:
            tg_queue.task_done()
            break
        try:
            bot.process_new_updates([upd])
        except Exception as e:
            print("TG ERROR:", e)
        finally:
            tg_queue.task_done()

def ig_worker():
    while True:
        job = ig_queue.get()
        if job is None:
            ig_queue.task_done()
            break

        func, args = job
        try:
            func(*args)
        except Exception as e:
            print("IG ERROR:", e)
            # حاول تبلغ المستخدم إذا أول arg هو chat_id
            with suppress(Exception):
                chat_id = args[0]
                bot.send_message(chat_id, f"⚠️ صار خطأ أثناء المعالجة:\n{e}")
        finally:
            ig_delay()
            ig_queue.task_done()

threading.Thread(target=tg_worker, daemon=True).start()
threading.Thread(target=ig_worker, daemon=True).start()

# ================== Flask ==================
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is Alive!"

@app.post(WEBHOOK_PATH)
def telegram_webhook():
    ctype = request.content_type or ""
    if "application/json" not in ctype:
        abort(403)

    data = request.get_json(silent=True)
    if not data:
        abort(400)

    upd = types.Update.de_json(data)
    tg_queue.put(upd)
    return "OK", 200

# ================== Helpers ==================
def extract_instagram_url(text: str):
    pattern = r"(https?://(?:www\.)?instagram\.com/[^\s]+)"
    m = re.search(pattern, text or "")
    return m.group(1).rstrip(").,!?") if m else None

def safe_caption(text, max_len=900):
    if not text:
        return ""
    t = text.strip()
    return t[:max_len] + ("…" if len(t) > max_len else "")

# ================== Telegram Handlers ==================
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "👋 أهلاً\nأرسل رابط إنستغرام (بوست/ريلز/ألبوم)")

@bot.message_handler(func=lambda m: m.text and "instagram.com" in m.text)
def handle_instagram(message):
    url = extract_instagram_url(message.text)
    if not url:
        bot.reply_to(message, "❌ رابط غير صالح")
        return

    # رسالة حالة
    status = bot.reply_to(message, "⏳ جاري قراءة الرابط...")

    # لا تثقل على webhook: خليه يشتغل داخل ig_queue
    ig_queue.put((prepare_options, (message.chat.id, url, status.message_id)))

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    try:
        action, media_pk = call.data.split("_", 1)
        media_pk = int(media_pk)
    except Exception:
        return

    chat_id = call.message.chat.id
    bot.answer_callback_query(call.id, "✅ تم استلام الطلب")

    if action == "dl":
        status = bot.send_message(chat_id, "⏳ جاري التحميل...")
        ig_queue.put((process_download, (chat_id, media_pk, status.message_id)))
    elif action == "info":
        status = bot.send_message(chat_id, "⏳ جاري جلب المعلومات...")
        ig_queue.put((process_info, (chat_id, media_pk, status.message_id)))

# ================== IG Jobs ==================
def prepare_options(chat_id: int, url: str, status_msg_id: int):
    if not ig_login():
        with suppress(Exception):
            bot.edit_message_text(
                "⚠️ إنستغرام رافض تسجيل الدخول حالياً (حظر IP/Challenge). جرّب لاحقاً.",
                chat_id, status_msg_id
            )
        return

    try:
        media_pk = cl.media_pk_from_url(url)
    except Exception:
        with suppress(Exception):
            bot.edit_message_text("⚠️ لا يمكن قراءة الرابط", chat_id, status_msg_id)
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("تحميل 📥", callback_data=f"dl_{media_pk}"),
        types.InlineKeyboardButton("معلومات ℹ️", callback_data=f"info_{media_pk}")
    )
    with suppress(Exception):
        bot.edit_message_text("اختر العملية:", chat_id, status_msg_id, reply_markup=markup)

def process_download(chat_id: int, media_pk: int, status_msg_id: int):
    if not ig_login():
        with suppress(Exception):
            bot.edit_message_text(
                "⚠️ إنستغرام رافض تسجيل الدخول حالياً (حظر IP/Challenge).",
                chat_id, status_msg_id
            )
        return

    try:
        media = cl.media_info(media_pk)
        caption = safe_caption(getattr(media, "caption_text", ""))

        # 1=Photo, 2=Video, 8=Album
        if media.media_type == 2:
            path = None
            try:
                path = cl.video_download(media_pk, folder=DOWNLOAD_DIR)
                with open(path, "rb") as f:
                    bot.send_video(chat_id, f, caption=caption or "✅ تم التحميل")
            finally:
                safe_remove(path)

        elif media.media_type == 1:
            path = None
            try:
                path = cl.photo_download(media_pk, folder=DOWNLOAD_DIR)
                with open(path, "rb") as f:
                    bot.send_photo(chat_id, f, caption=caption or "✅ تم التحميل")
            finally:
                safe_remove(path)

        elif media.media_type == 8:
            resources = getattr(media, "resources", []) or []
            if not resources:
                bot.send_message(chat_id, "⚠️ ألبوم لكن لم أستطع قراءة الملفات.")
                return

            group = []
            opened = []
            paths = []

            try:
                for i, r in enumerate(resources[:10]):  # حد تيليجرام 10
                    is_video = (getattr(r, "media_type", None) == 2)
                    video_url = getattr(r, "video_url", None)
                    photo_url = getattr(r, "thumbnail_url", None) or getattr(r, "url", None)

                    if is_video and video_url:
                        p = cl.video_download_by_url(video_url, folder=DOWNLOAD_DIR)
                        paths.append(p)
                        f = open(p, "rb"); opened.append(f)
                        group.append(types.InputMediaVideo(f, caption=caption if i == 0 else ""))
                    elif photo_url:
                        p = cl.photo_download_by_url(photo_url, folder=DOWNLOAD_DIR)
                        paths.append(p)
                        f = open(p, "rb"); opened.append(f)
                        group.append(types.InputMediaPhoto(f, caption=caption if i == 0 else ""))

                if group:
                    bot.send_media_group(chat_id, group)
                else:
                    bot.send_message(chat_id, "⚠️ ما قدرت أستخرج ملفات من الألبوم.")
            finally:
                for f in opened:
                    with suppress(Exception):
                        f.close()
                for p in paths:
                    safe_remove(p)

        else:
            bot.send_message(chat_id, "⚠️ نوع غير مدعوم.")

        with suppress(Exception):
            bot.edit_message_text("✅ تم الإرسال", chat_id, status_msg_id)

    except Exception as e:
        with suppress(Exception):
            bot.edit_message_text(f"⚠️ فشل التحميل:\n{e}", chat_id, status_msg_id)

def process_info(chat_id: int, media_pk: int, status_msg_id: int):
    if not ig_login():
        with suppress(Exception):
            bot.edit_message_text(
                "⚠️ إنستغرام رافض تسجيل الدخول حالياً (حظر IP/Challenge).",
                chat_id, status_msg_id
            )
        return

    try:
        media = cl.media_info(media_pk)
        username = getattr(media.user, "username", "unknown")
        like_count = getattr(media, "like_count", 0)
        comment_count = getattr(media, "comment_count", 0)

        text = f"👤 {username}\n❤️ {like_count}\n💬 {comment_count}"
        with suppress(Exception):
            bot.edit_message_text(text, chat_id, status_msg_id)

    except Exception as e:
        with suppress(Exception):
            bot.edit_message_text(f"⚠️ فشل جلب المعلومات:\n{e}", chat_id, status_msg_id)

# ================== Webhook Setup ==================
def setup_webhook():
    # لا نخلي فشل webhook يطيّح الخدمة
    try:
        bot.remove_webhook()
        time.sleep(1)
        bot.set_webhook(url=WEBHOOK_URL)
        print("✅ Webhook set to:", WEBHOOK_URL)
    except Exception as e:
        print("⚠️ Webhook setup failed:", e)

setup_webhook()

# ================== Run ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

