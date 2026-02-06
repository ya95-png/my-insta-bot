
import telebot
from telebot import types
import instaloader
import re

import os
from flask import Flask
import threading
import instaloader
import random

# قائمة بروكسيات (يمكنك تحديثها من المواقع التي ذكرناها)
proxy_list = [
    'http://161.35.70.242:3128',
    'http://167.172.175.255:80',
    'http://159.203.87.130:80',
    'http://177.71.224.87',
    'http://209.135.168.41',
    'http://117.54.114.33',
    'http://165.227.5.10',
    'http://190.242.157.215'
]

def get_loader():
    L = instaloader.Instaloader()
    
    # اختيار بروكسي عشوائي من القائمة
    proxy = random.choice(proxy_list)
    L.context.proxies = {'https': proxy}
    
    # تسجيل الدخول لزيادة الأمان (اختياري لكنه يقلل الحظر)
    L.login("ya95ppp", "ya$$er12345") 
    
    return L

# عند محاولة التحميل، استخدم هذه الوظيفة
loader = get_loader()
 post = instaloader.Post.from_shortcode(loader.context, shortcode)

import time
time.sleep(5)

# تشغيل سيرفر ويب بسيط لإرضاء Koyeb
app = Flask('')
@app.route('/')
def home(): return "Bot is Alive!"

def run():
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)

threading.Thread(target=run).start()
                   
# ضع التوكن الخاص بك هنا
TOKEN = '8580178191:AAFo3Dyf9ilw7Sz4Y9KgRKcuCEoXjvgQJUs'
bot = telebot.TeleBot(TOKEN)
L = instaloader.Instaloader()

# دالة ذكية لاستخراج كود المنشور من أي رابط إنستغرام
def get_shortcode(url):
    pattern = r"/(?:p|reels|reel|tv)/([A-Za-z0-9_-]+)"
    match = re.search(pattern, url)
    if match:
        return match.group(1)
    return None

@bot.message_handler(commands=['start'])
def start(message):
    bot.reply_to(message, "مرحباً! أرسل لي رابط إنستغرام (فيديو، ريلز، أو صورة) وسأقوم بتحميله.")

@bot.message_handler(func=lambda message: "instagram.com" in message.text)
def handle_instagram(message):
    url = message.text
    shortcode = get_shortcode(url)
    
    if not shortcode:
        bot.reply_to(message, "عذراً، لم أستطع فهم هذا الرابط. تأكد أنه رابط منشور أو ريلز.")
        return

    # إنشاء الأزرار
    markup = types.InlineKeyboardMarkup()
    btn_download = types.InlineKeyboardButton("تحميل المحتوى 📥", callback_data=f"dl_{shortcode}")
    btn_info = types.InlineKeyboardButton("معلومات المنشور ℹ️", callback_data=f"info_{shortcode}")
    markup.add(btn_download, btn_info)
    
    bot.reply_to(message, "اختر ما تريد فعله:", reply_markup=markup)

@bot.callback_query_handler(func=lambda call: True)
def callback_query(call):
    # تقسيم البيانات المستلمة من الزر
    action, shortcode = call.data.split("_")
    chat_id = call.message.chat.id
    
    bot.answer_callback_query(call.id, "جاري المعالجة...")
    
    try:
        post = instaloader.Post.from_shortcode(L.context, shortcode)
        
        if action == "dl":
            if post.is_video:
                bot.send_video(chat_id, post.video_url, caption="تم التحميل  ✅")
            else:
                bot.send_photo(chat_id, post.display_url, caption="تم التحميل  ✅")
        
        elif action == "info":
            info = f"👤 الناشر: {post.owner_username}\n❤️ الإعجابات: {post.likes}\n💬 التعليقات: {post.comments}"
            bot.send_message(chat_id, info)
            
    except Exception as e:
        bot.send_message(chat_id, f"حدث خطأ أثناء جلب البيانات. قد يكون الحساب خاصاً أو الرابط غير متاح.")
        print(f"Error: {e}")

print("البوت يعمل الآن بنجاح...")

bot.polling(none_stop=True)






