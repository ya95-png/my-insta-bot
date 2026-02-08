import os
import re
import time
import random
import threading
from queue import Queue
from contextlib import suppress
from collections import deque
from threading import Lock

from flask import Flask, request, abort
import telebot
from telebot import types
from instagrapi import Client

# ================== Environment ==================
TOKEN = os.getenv("BOT_TOKEN")
IG_USERNAME = os.getenv("IG_USERNAME")
IG_PASSWORD = os.getenv("IG_PASSWORD")

PUBLIC_URL = os.getenv("PUBLIC_URL")  # مثال: https://xxxx.koyeb.app (بدون سلاش بالنهاية)
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")  # أي كلمة سر تختارها
WEBHOOK_BASE_PATH = os.getenv("WEBHOOK_BASE_PATH", "/telegram-webhook")

DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", "downloads")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

# Limits (تقدر تغيّرها بالـ env إذا تحب)
MAX_PENDING_PER_USER = int(os.getenv("MAX_PENDING_PER_USER", "2"))     # كم طلب معلّق لنفس الشخص
LINKS_PER_MINUTE = int(os.getenv("LINKS_PER_MINUTE", "4"))             # كم رابط بالدقيقة
ACTIONS_PER_MINUTE = int(os.getenv("ACTIONS_PER_MINUTE", "8"))         # كم ضغط زر بالدقيقة

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is missing (your Koyeb public https URL)")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing (set any random secret)")

# المسار الفعلي يصير مثل: /telegram-webhook/<secret>
WEBHOOK_PATH = f"{WEBHOOK_BASE_PATH}/{WEBHOOK_SECRET}"
WEBHOOK_URL = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH

# ================== Telegram ==================
bot = telebot.TeleBot(TOKEN, threaded=True)

# ================== Instagram Client ==================
cl = Client()

def ig_login():
    """Best-effort login with session.json fallback."""
    if os.path.exists("session.json"):
        try:
            cl.load_settings("session.json")
            if IG_USERNAME and IG_PASSWORD:
                cl.login(IG_USERNAME, IG_PASSWORD, relogin=True)
            else:
                cl.login_by_sessionid(cl.settings.get("sessionid"))
            cl.dump_settings("session.json")
            return
        except Exception as e:
            print("Session login failed:", e)

    if not IG_USERNAME or not IG_PASSWORD:
        raise RuntimeError("Instagram credentials missing for first login")
    cl.login(IG_USERNAME, IG_PASSWORD)
    cl.dump_settings("session.json")

ig_login()

# ================== Rate Limit + Pending ==================
pending_lock = Lock()
pending_by_user = {}

def can_queue_user(user_id: int) -> bool:
    with pending_lock:
        cur = pending_by_user.get(user_id, 0)
        if cur >= MAX_PENDING_PER_USER:
            return False
        pending_by_user[user_id] = cur + 1
        return True

def done_queue_user(user_id: int):
    with pending_lock:
        pending_by_user[user_id] = max(0, pending_by_user.get(user_id, 0) - 1)

class SimpleRateLimiter:
    def __init__(self, max_hits: int, window_sec: int):
        self.max_hits = max_hits
        self.window_sec = window_sec
        self.lock = Lock()
        self.hits = {}  # user_id -> deque[timestamps]

    def allow(self, user_id: int):
        now = time.time()
        with self.lock:
            dq = self.hits.setdefault(user_id, deque())
            while dq and (now - dq[0]) > self.window_sec:
                dq.popleft()
            if len(dq) >= self.max_hits:
                retry_after = int(self.window_sec - (now - dq[0])) + 1
                return False, retry_after
            dq.append(now)
            return True, 0

link_limiter = SimpleRateLimiter(LINKS_PER_MINUTE, 60)
action_limiter = SimpleRateLimiter(ACTIONS_PER_MINUTE, 60)

# ================== Helpers ==================
def extract_instagram_url(text: str):
    pattern = r"(https?://(?:www\.)?instagram\.com/[^\s]+)"
    m = re.search(pattern, text)
    if not m:
        return None
    # شيل علامات شائعة بالآخر
    return m.group(1).rstrip(").,!?")

def safe_caption(text, max_len=900):
    if not text:
        return ""
    t = text.strip()
    return t[:max_len] + ("…" if len(t) > max_len else "")

def ig_delay():
    time.sleep(random.randint(8, 15))

def safe_remove(path: str):
    if not path:
        return
    with suppress(Exception):
        os.remove(path)

# ================== IG Queue Worker ==================
# نخزن (func, args, user_id) حتى نقدر ننقص pending مهما صار
ig_queue = Queue()

def ig_worker():
    while True:
        task = ig_queue.get()
        if task is None:
            ig_queue.task_done()
            break

        func, args, user_id = task
        try:
            func(*args)
        except Exception as e:
            print("IG ERROR:", e)
            # حاول بلغ المستخدم لو نكدر
            with suppress(Exception):
                chat_id = args[0]
                bot.send_message(chat_id, f"⚠️ صار خطأ أثناء المعالجة:\n{e}")
        finally:
            done_queue_user(user_id)
            ig_delay()
            ig_queue.task_done()

threading.Thread(target=ig_worker, daemon=True).start()

# ================== Telegram Update Queue (حتى الـ webhook يرجع بسرعة) ==================
tg_queue = Queue()

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

threading.Thread(target=tg_worker, daemon=True).start()

# ================== Flask App ==================
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is Alive!"

@app.post(WEBHOOK_PATH)
def telegram_webhook():
    # قبول application/json + charset
    ctype = request.content_type or ""
    if "application/json" not in ctype:
        abort(403)

    data = request.get_json(silent=True)
    if not data:
        abort(400)

    upd = types.Update.de_json(data)
    tg_queue.put(upd)
    return "OK", 200

# ================== Telegram Handlers ==================
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(message, "👋 أهلاً\nأرسل رابط إنستغرام (بوست/ريلز/ألبوم) وسيتم تحميله.")

@bot.message_handler(func=lambda m: m.text and "instagram.com" in m.text)
def handle_instagram(message):
    user_id = message.from_user.id
    allowed, retry = link_limiter.allow(user_id)
    if not allowed:
        bot.reply_to(message, f"⏳ هواي طلبات.. حاول بعد {retry} ثانية.")
        return

    if not can_queue_user(user_id):
        bot.reply_to(message, "⏳ عندك طلبات معلّقة.. انتظر تخلص وبعدين جرّب.")
        return

    url = extract_instagram_url(message.text or "")
    if not url:
        done_queue_user(user_id)
        bot.reply_to(message, "❌ رابط غير صالح")
        return

    # رسالة حالة سريعة
    status = bot.reply_to(message, "⏳ جاري قراءة الرابط...")

    # خلي قراءة media_pk بالـ IG queue (حتى ما يثقل webhook)
    ig_queue.put((process_prepare_options, (message.chat.id, url, status.message_id), user_id))

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    user_id = call.from_user.id
    allowed, retry = action_limiter.allow(user_id)
    if not allowed:
        bot.answer_callback_query(call.id, f"⏳ انتظر {retry} ثانية", show_alert=False)
        return

    try:
        action, media_pk = call.data.split("_", 1)
        media_pk = int(media_pk)
    except Exception:
        return

    chat_id = call.message.chat.id

    if not can_queue_user(user_id):
        bot.answer_callback_query(call.id, "⏳ عندك طلبات معلّقة.. انتظر.", show_alert=False)
        return

    bot.answer_callback_query(call.id, "✅ تم استلام الطلب")

    if action == "dl":
        status = bot.send_message(chat_id, "⏳ جاري التحميل...")
        ig_queue.put((process_download, (chat_id, media_pk, status.message_id), user_id))
    elif action == "info":
        status = bot.send_message(chat_id, "⏳ جاري جلب المعلومات...")
        ig_queue.put((process_info, (chat_id, media_pk, status.message_id), user_id))

# ================== IG Tasks ==================
def process_prepare_options(chat_id, url, status_msg_id):
    try:
        media_pk = cl.media_pk_from_url(url)
    except Exception:
        with suppress(Exception):
            bot.edit_message_text("⚠️ لا يمكن قراءة الرابط (قد يكون خاص/محذوف/أو غير مباشر).", chat_id, status_msg_id)
        return

    markup = types.InlineKeyboardMarkup()
    markup.add(
        types.InlineKeyboardButton("تحميل 📥", callback_data=f"dl_{media_pk}"),
        types.InlineKeyboardButton("معلومات ℹ️", callback_data=f"info_{media_pk}")
    )

    with suppress(Exception):
        bot.edit_message_text("اختر العملية:", chat_id, status_msg_id, reply_markup=markup)

def process_download(chat_id, media_pk, status_msg_id):
    try:
        media = cl.media_info(media_pk)
        caption = safe_caption(getattr(media, "caption_text", ""))

        # 1 = Photo, 2 = Video/Reel, 8 = Album
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
                bot.send_message(chat_id, "⚠️ ألبوم لكن لم أستطع قراءة الصور/الفيديوهات.")
                return

            group = []
            opened_files = []
            downloaded_paths = []

            try:
                for i, r in enumerate(resources[:10]):  # تيليجرام حد 10
                    is_video = (getattr(r, "media_type", None) == 2)
                    video_url = getattr(r, "video_url", None)
                    photo_url = getattr(r, "thumbnail_url", None) or getattr(r, "url", None)

                    if is_video and video_url:
                        p = cl.video_download_by_url(video_url, folder=DOWNLOAD_DIR)
                        downloaded_paths.append(p)
                        f = open(p, "rb"); opened_files.append(f)
                        group.append(types.InputMediaVideo(f, caption=caption if i == 0 else ""))

                    elif photo_url:
                        p = cl.photo_download_by_url(photo_url, folder=DOWNLOAD_DIR)
                        downloaded_paths.append(p)
                        f = open(p, "rb"); opened_files.append(f)
                        group.append(types.InputMediaPhoto(f, caption=caption if i == 0 else ""))

                if group:
                    bot.send_media_group(chat_id, group)
                else:
                    bot.send_message(chat_id, "⚠️ ما قدرت أستخرج ملفات من الألبوم.")
            finally:
                for f in opened_files:
                    with suppress(Exception):
                        f.close()
                for p in downloaded_paths:
                    safe_remove(p)

        else:
            bot.send_message(chat_id, "⚠️ نوع وسائط غير مدعوم.")

        with suppress(Exception):
            bot.edit_message_text("✅ تم الإرسال", chat_id, status_msg_id)

    except Exception as e:
        with suppress(Exception):
            bot.edit_message_text(f"⚠️ فشل التحميل:\n{e}", chat_id, status_msg_id)

def process_info(chat_id, media_pk, status_msg_id):
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
    with suppress(Exception):
        bot.remove_webhook()
    time.sleep(1)
    bot.set_webhook(url=WEBHOOK_URL)
    print("✅ Webhook set to:", WEBHOOK_URL)
try:
    setup_webhook()
except Exception as e:
    print("⚠️ Webhook setup failed:", e)


# ================== Run ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

