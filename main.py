import os
import re
import tempfile
import threading
from queue import Queue
from contextlib import suppress

from flask import Flask, request, abort
import telebot
from telebot import types
import yt_dlp

# ================== ENV ==================
TOKEN = os.getenv("BOT_TOKEN")
PUBLIC_URL = os.getenv("PUBLIC_URL")           # https://xxxx.koyeb.app
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET")   # أي سترنغ عشوائي

if not TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")
if not PUBLIC_URL:
    raise RuntimeError("PUBLIC_URL is missing")
if not WEBHOOK_SECRET:
    raise RuntimeError("WEBHOOK_SECRET is missing")

WEBHOOK_PATH = f"/telegram-webhook/{WEBHOOK_SECRET}"
WEBHOOK_URL = PUBLIC_URL.rstrip("/") + WEBHOOK_PATH

# ================== Telegram ==================
bot = telebot.TeleBot(TOKEN, threaded=True)

# ================== Flask ==================
app = Flask(__name__)

@app.get("/")
def home():
    return "Bot is Alive!"

# نخلي التحديثات تدخل على Queue حتى ما يثقل الويبهوك
tg_queue: Queue = Queue()
job_queue: Queue = Queue()

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

def job_worker():
    while True:
        job = job_queue.get()
        if job is None:
            job_queue.task_done()
            break
        func, args = job
        try:
            func(*args)
        except Exception as e:
            print("JOB ERROR:", e)
            with suppress(Exception):
                chat_id = args[0]
                bot.send_message(chat_id, f"⚠️ صار خطأ:\n{e}")
        finally:
            job_queue.task_done()

threading.Thread(target=tg_worker, daemon=True).start()
threading.Thread(target=job_worker, daemon=True).start()

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
    if not text:
        return None
    m = re.search(r"(https?://(?:www\.)?instagram\.com/[^\s]+)", text)
    return m.group(1).rstrip(").,!?") if m else None

def is_instagram_url(url: str) -> bool:
    return bool(url) and "instagram.com" in url

def ytdlp_extract(url: str):
    """
    Extract info without downloading first.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,   # مهم: يمنع الألبومات/القوائم
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=False)

def ytdlp_download(url: str, outdir: str):
    """
    Download best media to outdir and return downloaded filepath.
    """
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "outtmpl": os.path.join(outdir, "%(id)s.%(ext)s"),
        # نخليها بسيطة حتى تشتغل على Koyeb بدون ffmpeg
        "format": "best",
        "merge_output_format": "mp4",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        # اعثر على اسم الملف النهائي
        # yt-dlp يرجع _filename أو يقدر يطلع عبر prepare_filename
        filename = info.get("_filename")
        if filename and os.path.exists(filename):
            return filename
        return ydl.prepare_filename(info)

# ================== Telegram Handlers ==================
@bot.message_handler(commands=["start"])
def start(message):
    bot.reply_to(
        message,
        "👋 أهلاً\nأرسل رابط إنستغرام (Reel أو Post عام) وأنا أحمله لك.\n"
        "⚠️ الألبومات (Carousel) غير مدعومة حالياً."
    )

@bot.message_handler(func=lambda m: m.text and "instagram.com" in m.text)
def handle_instagram(message):
    url = extract_instagram_url(message.text)
    if not url or not is_instagram_url(url):
        bot.reply_to(message, "❌ رابط غير صالح")
        return

    status = bot.reply_to(message, "⏳ جاري فحص الرابط...")
    job_queue.put((process_instagram, (message.chat.id, url, status.message_id)))

# ================== Main Job ==================
def process_instagram(chat_id: int, url: str, status_msg_id: int):
    # 1) استخرج معلومات (حتى نعرف اذا Album)
    try:
        info = ytdlp_extract(url)
    except Exception:
        with suppress(Exception):
            bot.edit_message_text("⚠️ ما قدرت أقرأ الرابط. تأكد أنه عام (Public).", chat_id, status_msg_id)
        return

    # إذا كان Playlist/Carousel
    # بعض الأحيان يجي "entries" إذا مجموعة
    if isinstance(info, dict) and info.get("entries"):
        with suppress(Exception):
            bot.edit_message_text("❌ الألبومات (Carousel) غير مدعومة حالياً. أرسل Reel أو Post مفرد.", chat_id, status_msg_id)
        return

    with suppress(Exception):
        bot.edit_message_text("⏳ جاري التحميل...", chat_id, status_msg_id)

    # 2) حمّل الملف
    with tempfile.TemporaryDirectory() as tmp:
        try:
            path = ytdlp_download(url, tmp)
        except Exception:
            with suppress(Exception):
                bot.edit_message_text("⚠️ فشل التحميل. جرّب رابط ثاني أو تأكد أنه Public.", chat_id, status_msg_id)
            return

        if not path or not os.path.exists(path):
            with suppress(Exception):
                bot.edit_message_text("⚠️ ما حصلت ملف بعد التحميل.", chat_id, status_msg_id)
            return

        # 3) إرسال: إذا فيديو send_video، إذا صورة send_photo
        ext = os.path.splitext(path)[1].lower()

        try:
            with open(path, "rb") as f:
                if ext in [".mp4", ".mkv", ".webm", ".mov"]:
                    bot.send_video(chat_id, f, caption="✅ تم التحميل")
                else:
                    bot.send_photo(chat_id, f, caption="✅ تم التحميل")
        except Exception as e:
            with suppress(Exception):
                bot.send_message(chat_id, f"⚠️ فشل الإرسال:\n{e}")

    with suppress(Exception):
        bot.edit_message_text("✅ تم الإرسال", chat_id, status_msg_id)

# ================== Webhook Setup ==================
def setup_webhook():
    try:
        bot.remove_webhook()
        bot.set_webhook(url=WEBHOOK_URL)
        print("✅ Webhook:", WEBHOOK_URL)
    except Exception as e:
        print("⚠️ Webhook setup failed:", e)

setup_webhook()

# ================== Run ==================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
