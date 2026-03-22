#!/usr/bin/env python3
"""
🎬 SRT Subtitle Translator + 🎙 Audio Transcription Bot
বাংলা সাবটাইটেল অনুবাদক | Powered by Groq AI + Whisper
Features: SRT Translate · Cancel · SubDL · AI Chat · Audio Transcription
Python 3.11 | PTB 20.7
"""

import os, re, io, time, asyncio, logging, threading, functools, requests, tempfile
from concurrent.futures import ThreadPoolExecutor

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from flask import Flask
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, filters, ContextTypes
)
from groq import Groq

# ══════════════════════════════════════════════
# ⚙️  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN        = os.environ.get('BOT_TOKEN', '')
GROQ_API_KEY     = os.environ.get('GROQ_API_KEY', '')
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', '@your_channel')
RENDER_URL       = os.environ.get('RENDER_URL', '')
SUBDL_API_KEY    = os.environ.get('SUBDL_API_KEY', '')

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

groq_client   = Groq(api_key=GROQ_API_KEY)
executor      = ThreadPoolExecutor(max_workers=4)

active_tasks  = {}
cancel_events = {}
chat_mode     = {}
chat_history  = {}

# অডিও ফাইল pending রাখার জন্য
pending_audio = {}  # {uid: {'file_id': ..., 'file_name': ..., 'duration': ...}}

AUDIO_EXTENSIONS = ('.mp3', '.mp4', '.wav', '.m4a', '.ogg', '.webm', '.oga', '.flac')
MAX_AUDIO_SIZE   = 25 * 1024 * 1024  # 25MB

# ══════════════════════════════════════════════
# 🌐  FLASK
# ══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
def web_home():
    return """<!DOCTYPE html><html><head><title>SRT + Transcription Bot</title>
<meta charset="UTF-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#0f0e17,#1a1a2e);
     color:#fff;display:flex;justify-content:center;
     align-items:center;min-height:100vh;flex-direction:column;gap:16px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,137,6,.35);
      border-radius:20px;padding:36px 56px;text-align:center}
h1{color:#ff8906;font-size:2.4em;margin-bottom:8px}
.dot{width:13px;height:13px;background:#00d4aa;border-radius:50%;
     display:inline-block;animation:p 1.5s infinite;box-shadow:0 0 8px #00d4aa}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
p{color:#a7a9be;font-size:1.05em;line-height:1.9}
.b{display:inline-block;background:rgba(255,137,6,.13);
   border:1px solid #ff8906;color:#ff8906;
   padding:5px 14px;border-radius:18px;font-size:.88em;margin:4px}
</style></head><body>
<div class="card">
  <h1>🎬 SRT + 🎙 Transcription Bot</h1>
  <p><span class="dot"></span>&nbsp;
  <span style="color:#00d4aa;font-weight:700;font-size:1.15em">Bot is Live!</span></p>
  <p>Subtitle Translate · Audio Transcribe · AI Chat</p><br>
  <div>
    <span class="b">🤖 Groq AI</span>
    <span class="b">🎙 Whisper</span>
    <span class="b">⚡ LLaMA 3.3 70B</span>
    <span class="b">🔍 SubDL</span>
    <span class="b">❌ Cancel</span>
  </div>
</div></body></html>""", 200

@flask_app.route('/ping')
def ping():
    return 'pong', 200

def run_flask():
    flask_app.run(host='0.0.0.0',
                  port=int(os.environ.get('PORT', 10000)),
                  use_reloader=False)

# ══════════════════════════════════════════════
# 🔄  SELF-PING
# ══════════════════════════════════════════════
def self_ping():
    time.sleep(30)
    while True:
        time.sleep(840)
        if RENDER_URL:
            try:
                r = requests.get(f"{RENDER_URL}/ping", timeout=15)
                logger.info(f"✅ ping {r.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ ping fail: {e}")

# ══════════════════════════════════════════════
# 📄  SRT PARSER / BUILDER
# ══════════════════════════════════════════════
def parse_srt(content: str) -> list:
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    blocks, pat = [], re.compile(
        r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n'
        r'((?:.+\n?)+?)(?=\n\d+\n|\Z)', re.MULTILINE)
    for m in pat.finditer(content.strip() + '\n\n'):
        txt = m.group(4).strip()
        if txt:
            blocks.append({'index': m.group(1), 'start': m.group(2),
                           'end': m.group(3), 'text': txt})
    return blocks

def build_srt(blocks: list) -> str:
    return '\n\n'.join(
        f"{b['index']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in blocks) + '\n'

# ══════════════════════════════════════════════
# 📊  PIE CHART
# ══════════════════════════════════════════════
def generate_pie_chart(completed: int, total: int) -> io.BytesIO:
    pct       = (completed / total * 100) if total > 0 else 0
    remaining = max(total - completed, 0)
    fig, ax   = plt.subplots(figsize=(7, 5.5))
    fig.patch.set_facecolor('#0f0e17')
    ax.set_facecolor('#0f0e17')

    if completed == 0:
        sizes, colors, labels = [100], ['#2d2d44'], ['Waiting...']
    elif completed >= total:
        sizes, colors, labels = [100], ['#00d4aa'], ['Completed 100% ✓']
    else:
        sizes  = [completed, remaining]
        colors = ['#00d4aa', '#2d2d44']
        labels = [f'Done ({completed})', f'Left ({remaining})']

    explode = ([0.05, 0] if len(sizes) == 2 else [0])
    _, _, ats = ax.pie(sizes, explode=explode, colors=colors,
                       autopct='%1.1f%%', startangle=90, pctdistance=0.65,
                       wedgeprops={'linewidth': 2.5, 'edgecolor': '#0f0e17'},
                       shadow=True)
    for at in ats:
        at.set_color('white'); at.set_fontsize(13); at.set_fontweight('bold')

    ax.text(0, 0, f'{pct:.1f}%', ha='center', va='center',
            fontsize=26, fontweight='bold', color='white')
    patches = [mpatches.Patch(color=colors[i], label=labels[i])
               for i in range(len(labels))]
    ax.legend(handles=patches, loc='lower center', bbox_to_anchor=(.5, -.13),
              ncol=2, facecolor='#1e1e2e', edgecolor='#444466',
              labelcolor='white', fontsize=10)
    ax.set_title('Translation Progress', color='#ff8906',
                 fontsize=15, fontweight='bold', pad=18)
    fig.text(.5, .01,
             f'Total: {total}  |  Done: {completed}  |  Left: {remaining}',
             ha='center', color='#a7a9be', fontsize=9)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=110, bbox_inches='tight',
                facecolor='#0f0e17')
    buf.seek(0); plt.close(fig)
    return buf

# ══════════════════════════════════════════════
# 🤖  SUBTITLE TRANSLATION
# ══════════════════════════════════════════════
TRANSLATE_SYSTEM = (
    "তুমি একজন পেশাদার চলচ্চিত্র সাবটাইটেল অনুবাদক।\n"
    "নিয়ম:\n"
    "- ভাব বুঝে অনুবাদ করো, আক্ষরিক নয়\n"
    "- স্বাভাবিক কথ্য বাংলা ব্যবহার করো\n"
    "- আবেগ ও টোন বজায় রাখো\n"
    "- শুধু অনুবাদ দেবে, বাড়তি কিছু লিখবে না"
)

def _is_quota_error(e: Exception) -> bool:
    err = str(e).lower()
    return any(k in err for k in [
        'quota', 'limit exceeded', '402', 'billing',
        'insufficient_quota', 'exceeded your current quota'
    ])

def _is_rate_limit(e: Exception) -> bool:
    err = str(e).lower()
    return 'rate_limit' in err or '429' in err

def translate_one_sync(text: str, cancel_event=None) -> str:
    for _ in range(3):
        if cancel_event and cancel_event.is_set():
            return text
        try:
            r = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": TRANSLATE_SYSTEM},
                    {"role": "user",
                     "content": f"শুধু এই সাবটাইটেলটি বাংলায় অনুবাদ করো:\n{text}"}
                ],
                temperature=0.15, max_tokens=256)
            return r.choices[0].message.content.strip()
        except Exception as e:
            if _is_quota_error(e): raise Exception("QUOTA_EXCEEDED")
            elif _is_rate_limit(e):
                for _ in range(60):
                    if cancel_event and cancel_event.is_set(): return text
                    time.sleep(1)
            else:
                time.sleep(3)
    return text

def translate_batch_sync(texts: list, cancel_event=None) -> list:
    if cancel_event and cancel_event.is_set():
        return texts
    numbered = '\n'.join(f"[{i+1}] {t}" for i, t in enumerate(texts))
    user_msg = (
        f"নিচের {len(texts)}টি সাবটাইটেল লাইন বাংলায় অনুবাদ করো।\n"
        f"প্রতিটি লাইনের আগে একই নম্বর রাখো: [1], [2], [3]...\n"
        f"শুধু অনুবাদ দাও, অন্য কিছু লিখবে না।\n\n"
        f"{numbered}\n\nBengali:"
    )
    translated = [None] * len(texts)
    for attempt in range(3):
        if cancel_event and cancel_event.is_set(): return texts
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": TRANSLATE_SYSTEM},
                          {"role": "user",   "content": user_msg}],
                temperature=0.15, max_tokens=3000)
            raw = resp.choices[0].message.content.strip()
            pat = re.compile(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|\Z)', re.DOTALL)
            for m in pat.finditer(raw):
                idx = int(m.group(1)) - 1
                val = m.group(2).strip()
                if 0 <= idx < len(texts) and val:
                    translated[idx] = val
            break
        except Exception as e:
            if _is_quota_error(e): raise Exception("QUOTA_EXCEEDED")
            elif _is_rate_limit(e):
                logger.warning(f"Rate limit (attempt {attempt+1}), waiting 60s...")
                for _ in range(60):
                    if cancel_event and cancel_event.is_set(): return texts
                    time.sleep(1)
            else:
                logger.error(f"Batch error: {e}"); time.sleep(5)

    for i, val in enumerate(translated):
        if val is None:
            if cancel_event and cancel_event.is_set(): return texts
            translated[i] = translate_one_sync(texts[i], cancel_event)
    return translated

# ══════════════════════════════════════════════
# 🎙  AUDIO TRANSCRIPTION (Groq Whisper)
# ══════════════════════════════════════════════
def transcribe_audio_sync(audio_bytes: bytes, filename: str,
                          language: str = "en") -> str:
    """
    language: 'en' = English, 'bn' = Bengali, 'auto' = auto-detect
    """
    try:
        # temp file তৈরি করো
        suffix = os.path.splitext(filename)[-1] or '.mp3'
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            tmp.write(audio_bytes)
            tmp_path = tmp.name

        try:
            with open(tmp_path, 'rb') as f:
                params = dict(
                    file=(filename, f, 'audio/mpeg'),
                    model="whisper-large-v3-turbo",
                    response_format="text",
                    temperature=0.0,
                )
                if language != 'auto':
                    params['language'] = language

                result = groq_client.audio.transcriptions.create(**params)

            return result.strip() if isinstance(result, str) else result.text.strip()

        finally:
            os.unlink(tmp_path)

    except Exception as e:
        if _is_quota_error(e):
            raise Exception("QUOTA_EXCEEDED")
        logger.error(f"Transcription error: {e}")
        raise e

def translate_text_to_bengali_sync(text: str) -> str:
    """Transcribed text বাংলায় অনুবাদ করো"""
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system",
                 "content": (
                     "তুমি একজন পেশাদার অনুবাদক। "
                     "দেওয়া text স্বাভাবিক কথ্য বাংলায় অনুবাদ করো। "
                     "ভাব বুঝে অনুবাদ করো, আক্ষরিক নয়। "
                     "শুধু অনুবাদ দাও, অন্য কিছু লিখবে না।"
                 )},
                {"role": "user", "content": f"অনুবাদ করো:\n{text}"}
            ],
            temperature=0.15, max_tokens=4096)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        if _is_quota_error(e):
            raise Exception("QUOTA_EXCEEDED")
        raise e

# ══════════════════════════════════════════════
# 💬  AI CHAT
# ══════════════════════════════════════════════
CHAT_SYSTEM = (
    "তুমি একটি বন্ধুত্বপূর্ণ ও সহায়ক AI assistant। "
    "তুমি বাংলায় কথা বলো। "
    "সহজ, স্বাভাবিক ভাষায় উত্তর দাও। "
    "প্রয়োজনে English শব্দ ব্যবহার করতে পারো।"
)
MAX_CHAT_HISTORY = 20

def ai_chat_sync(uid: int, user_text: str) -> str:
    if uid not in chat_history:
        chat_history[uid] = []
    chat_history[uid].append({"role": "user", "content": user_text})
    if len(chat_history[uid]) > MAX_CHAT_HISTORY:
        chat_history[uid] = chat_history[uid][-MAX_CHAT_HISTORY:]
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role": "system", "content": CHAT_SYSTEM}]
                     + chat_history[uid],
            temperature=0.7, max_tokens=1024)
        reply = resp.choices[0].message.content.strip()
        chat_history[uid].append({"role": "assistant", "content": reply})
        return reply
    except Exception as e:
        if _is_quota_error(e):
            return ("⚠️ *Groq API Limit শেষ!*\n\n"
                    "তোমার API key-এর দৈনিক limit শেষ হয়ে গেছে।\n"
                    "২৪ ঘণ্টা পরে আবার চেষ্টা করো।")
        logger.error(f"Chat error: {e}")
        return "❌ সমস্যা হয়েছে, একটু পরে আবার চেষ্টা করো।"

# ══════════════════════════════════════════════
# 🔍  SUBDL
# ══════════════════════════════════════════════
def subdl_search(query: str) -> list:
    try:
        r = requests.get(
            "https://api.subdl.com/api/v1/subtitles",
            params={"api_key": SUBDL_API_KEY, "film_name": query,
                    "languages": "EN", "subs_per_page": 8},
            timeout=15)
        if r.status_code != 200:
            logger.error(f"SubDL {r.status_code}"); return []
        return r.json().get("subtitles", [])[:8]
    except Exception as e:
        logger.error(f"SubDL search: {e}"); return []

def subdl_download(url_path: str):
    try:
        r = requests.get(f"https://dl.subdl.com{url_path}", timeout=30)
        return r.content if r.status_code == 200 else None
    except Exception as e:
        logger.error(f"SubDL download: {e}"); return None

# ══════════════════════════════════════════════
# 🔒  CHANNEL CHECK
# ══════════════════════════════════════════════
async def is_member(uid: int, bot) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return m.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.warning(f"Member check: {e}"); return False

NOT_JOINED_MSG = (
    "🔒 *চ্যানেল Membership নেই!*\n\n"
    "বট ব্যবহার করতে চ্যানেলে যোগ দিতে হবে।\n"
    "চ্যানেল থেকে leave নিলে বট access বন্ধ হয়ে যাবে।"
)

# ══════════════════════════════════════════════
# 🎹  KEYBOARDS
# ══════════════════════════════════════════════
def kb_not_joined():
    ch = CHANNEL_USERNAME.lstrip('@')
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 চ্যানেলে যোগ দাও", url=f"https://t.me/{ch}")],
        [InlineKeyboardButton("✅ যোগ দিয়েছি — চেক করো", callback_data="chk")]
    ])

def kb_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 ব্যবহার বিধি",        callback_data="help"),
         InlineKeyboardButton("ℹ️ বট সম্পর্কে",         callback_data="about")],
        [InlineKeyboardButton("🔍 Subtitle খোঁজো",       callback_data="search"),
         InlineKeyboardButton("📊 স্ট্যাটাস",             callback_data="status")],
        [InlineKeyboardButton("🎙 Audio Transcription",  callback_data="audio_info")],
        [InlineKeyboardButton("💬 AI-এর সাথে চ্যাট করো", callback_data="chat_start")]
    ])

def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 হোম", callback_data="home")]
    ])

def kb_cancel(uid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ বাতিল করো", callback_data=f"cancel_{uid}")]
    ])

def kb_chat():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 কথোপকথন মুছো",  callback_data="chat_clear")],
        [InlineKeyboardButton("🔙 চ্যাট বন্ধ করো", callback_data="chat_stop")]
    ])

def kb_search_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ বাতিল", callback_data="home")]
    ])

def kb_quota_error():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Groq Console", url="https://console.groq.com")],
        [InlineKeyboardButton("🔙 হোম", callback_data="home")]
    ])

def kb_audio_options(uid: int):
    """অডিও পাঠানোর পরে কী করবে জিজ্ঞেস করার keyboard"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇧🇩 বাংলায় Transcription",
                              callback_data=f"tr_bn_{uid}")],
        [InlineKeyboardButton("🇺🇸 English Transcription",
                              callback_data=f"tr_en_{uid}")],
        [InlineKeyboardButton("🔄 Transcription + বাংলা অনুবাদ",
                              callback_data=f"tr_translate_{uid}")],
        [InlineKeyboardButton("❌ বাতিল", callback_data="home")]
    ])

# ══════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            NOT_JOINED_MSG, parse_mode='Markdown',
            reply_markup=kb_not_joined())
        return
    chat_mode[u.id] = False
    await update.message.reply_text(
        f"🎬 *Subtitle BD Bot-এ স্বাগতম!*\n\n"
        f"হ্যালো *{u.first_name}* ভাই! 👋\n\n"
        f"আর কঠিন লাগবে না ভিনদেশি সিনেমা —\n"
        f"আমি তোমার সাবটাইটেল বাংলায় অনুবাদ করে দেব!\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✨ *আমি যা করতে পারি:*\n\n"
        f"🔄 `.srt` ফাইল → সুন্দর বাংলা অনুবাদ\n"
        f"🎙 অডিও/Voice → বাংলা বা English Transcription\n"
        f"🔍 মুভির Subtitle খোঁজা ও ডাউনলোড\n"
        f"❌ যেকোনো সময় বাতিল করা\n"
        f"💬 AI-এর সাথে বাংলায় চ্যাট করা\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 সরাসরি `.srt` বা অডিও ফাইল পাঠাও 👇\n\n"
        f"⚡ _Powered by Groq AI + Whisper_",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 🎙  AUDIO / VOICE HANDLER
# ══════════════════════════════════════════════
async def handle_audio_or_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Voice message এবং Audio file দুটোই handle করে"""
    u = update.effective_user

    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            NOT_JOINED_MSG, parse_mode='Markdown',
            reply_markup=kb_not_joined())
        return

    msg = update.message

    # Voice message নাকি Audio file তা নির্ধারণ করো
    if msg.voice:
        file_obj  = msg.voice
        file_name = f"voice_{u.id}.ogg"
        duration  = msg.voice.duration
        file_size = msg.voice.file_size
    elif msg.audio:
        file_obj  = msg.audio
        file_name = msg.audio.file_name or f"audio_{u.id}.mp3"
        duration  = msg.audio.duration
        file_size = msg.audio.file_size
    else:
        return

    # Size check
    if file_size and file_size > MAX_AUDIO_SIZE:
        await msg.reply_text(
            "❌ *অডিও ফাইল ২৫MB-এর বেশি!*\n\n"
            "ছোট ফাইল পাঠাও।",
            parse_mode='Markdown')
        return

    # Duration format
    dur_str = ""
    if duration:
        m, s    = divmod(duration, 60)
        dur_str = f"{m}:{s:02d}"

    # pending-এ সেভ করো
    pending_audio[u.id] = {
        'file_id':  file_obj.file_id,
        'file_name': file_name,
        'duration': dur_str
    }

    await msg.reply_text(
        f"🎙 *অডিও পেয়েছি!*\n\n"
        f"📁 `{file_name}`\n"
        f"{'⏱ সময়: ' + dur_str if dur_str else ''}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"এখন কী করতে চাও?",
        parse_mode='Markdown',
        reply_markup=kb_audio_options(u.id))


async def do_transcription(uid: int, mode: str, bot, chat_id: int,
                           ctx: ContextTypes.DEFAULT_TYPE):
    """
    mode: 'bn' | 'en' | 'translate'
    """
    audio_info = pending_audio.get(uid)
    if not audio_info:
        await bot.send_message(
            chat_id,
            "❌ অডিও ফাইল পাওয়া যায়নি। আবার পাঠাও।")
        return

    pending_audio.pop(uid, None)

    # Status message
    mode_text = {
        'bn':        "🇧🇩 বাংলায় Transcription",
        'en':        "🇺🇸 English Transcription",
        'translate': "🔄 Transcription + বাংলা অনুবাদ"
    }.get(mode, "Transcription")

    status = await bot.send_message(
        chat_id,
        f"⏳ *{mode_text} চলছে...*\n\n"
        f"📁 `{audio_info['file_name']}`\n"
        f"একটু অপেক্ষা করো...",
        parse_mode='Markdown')

    try:
        # অডিও ডাউনলোড
        f        = await bot.get_file(audio_info['file_id'])
        raw      = await f.download_as_bytearray()
        fname    = audio_info['file_name']
        loop     = asyncio.get_event_loop()

        # Transcription language
        lang = 'bn' if mode == 'bn' else 'en'

        await bot.edit_message_text(
            f"🎙 *Transcription শুরু হয়েছে...*\n\n"
            f"📁 `{fname}`\n"
            f"🌐 Language: `{lang.upper()}`\n"
            f"⏳ Whisper AI process করছে...",
            chat_id=chat_id,
            message_id=status.message_id,
            parse_mode='Markdown')

        # Transcribe
        transcript = await loop.run_in_executor(
            executor,
            functools.partial(transcribe_audio_sync, bytes(raw), fname, lang))

        if not transcript or not transcript.strip():
            await bot.edit_message_text(
                "❌ *Transcription ব্যর্থ!*\n\n"
                "অডিওতে কোনো কথা শোনা যায়নি বা\n"
                "ফাইল format সমস্যা আছে।",
                chat_id=chat_id,
                message_id=status.message_id,
                parse_mode='Markdown',
                reply_markup=kb_back())
            return

        # শুধু transcription দরকার হলে পাঠাও
        if mode in ('bn', 'en'):
            flag = "🇧🇩" if mode == 'bn' else "🇺🇸"
            lang_name = "বাংলা" if mode == 'bn' else "English"

            # বড় হলে ফাইল হিসেবে পাঠাও
            if len(transcript) > 3500:
                txt_bytes = transcript.encode('utf-8')
                ext_name  = fname.replace('.ogg', '').replace('.mp3', '')
                out_fname = f"{ext_name}_{lang_name}_transcript.txt"
                await bot.edit_message_text(
                    f"✅ *{flag} {lang_name} Transcription সম্পন্ন!*\n\n"
                    f"📝 টেক্সট অনেক বড় — ফাইল হিসেবে পাঠাচ্ছি।",
                    chat_id=chat_id,
                    message_id=status.message_id,
                    parse_mode='Markdown')
                await bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(txt_bytes),
                    filename=out_fname,
                    caption=f"{flag} *{lang_name} Transcription*\n\n"
                            f"📁 `{out_fname}`",
                    parse_mode='Markdown',
                    reply_markup=kb_home())
            else:
                await bot.edit_message_text(
                    f"✅ *{flag} {lang_name} Transcription সম্পন্ন!*\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"{transcript}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━",
                    chat_id=chat_id,
                    message_id=status.message_id,
                    parse_mode='Markdown',
                    reply_markup=kb_home())

        # Transcription + বাংলা অনুবাদ
        elif mode == 'translate':
            await bot.edit_message_text(
                f"✅ *English Transcription সম্পন্ন!*\n\n"
                f"🔄 এখন বাংলায় অনুবাদ করছি...",
                chat_id=chat_id,
                message_id=status.message_id,
                parse_mode='Markdown')

            bengali = await loop.run_in_executor(
                executor,
                functools.partial(translate_text_to_bengali_sync, transcript))

            # বড় হলে ফাইল হিসেবে
            if len(transcript) + len(bengali) > 3000:
                ext_name  = fname.replace('.ogg', '').replace('.mp3', '')
                combined  = (
                    f"=== English Transcription ===\n\n{transcript}\n\n"
                    f"=== বাংলা অনুবাদ ===\n\n{bengali}"
                )
                await bot.edit_message_text(
                    f"✅ *Transcription + বাংলা অনুবাদ সম্পন্ন!*\n\n"
                    f"📝 টেক্সট অনেক বড় — ফাইল হিসেবে পাঠাচ্ছি।",
                    chat_id=chat_id,
                    message_id=status.message_id,
                    parse_mode='Markdown')
                await bot.send_document(
                    chat_id=chat_id,
                    document=io.BytesIO(combined.encode('utf-8')),
                    filename=f"{ext_name}_transcript_bengali.txt",
                    caption="✅ *Transcription + বাংলা অনুবাদ*",
                    parse_mode='Markdown',
                    reply_markup=kb_home())
            else:
                await bot.edit_message_text(
                    f"✅ *Transcription + বাংলা অনুবাদ সম্পন্ন!*\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🇺🇸 *English:*\n{transcript}\n\n"
                    f"━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🇧🇩 *বাংলা:*\n{bengali}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━",
                    chat_id=chat_id,
                    message_id=status.message_id,
                    parse_mode='Markdown',
                    reply_markup=kb_home())

    except Exception as e:
        err = str(e)
        logger.error(f"Transcription error for {uid}: {err}")

        if "QUOTA_EXCEEDED" in err:
            notice = (
                "⚠️ *Groq API Limit শেষ!*\n\n"
                "তোমার API key-এর দৈনিক limit শেষ।\n"
                "২৪ ঘণ্টা পরে আবার চেষ্টা করো।"
            )
        else:
            notice = f"❌ *সমস্যা হয়েছে!*\n\n`{err[:200]}`"

        try:
            await bot.edit_message_text(
                notice, chat_id=chat_id,
                message_id=status.message_id,
                parse_mode='Markdown',
                reply_markup=kb_quota_error() if "QUOTA" in err else kb_back())
        except Exception:
            pass

# ══════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    d   = q.data

    # ── Channel check বাটন ──
    if d == "chk":
        await q.answer()
        if await is_member(uid, ctx.bot):
            chat_mode[uid] = False
            await q.edit_message_text(
                "✅ *দারুণ! চ্যানেলে যোগ দিয়েছ!*\n\n"
                "`.srt` ফাইল বা অডিও পাঠাও শুরু করতে 🚀",
                parse_mode='Markdown', reply_markup=kb_home())
        else:
            await q.edit_message_text(
                "❌ *এখনো যোগ দাওনি!*\n\nযোগ দাও, তারপর চেক করো।",
                parse_mode='Markdown', reply_markup=kb_not_joined())
        return

    # ── Cancel ──
    if d.startswith("cancel_"):
        await q.answer()
        target = int(d.split("_")[1])
        if uid == target and uid in active_tasks:
            active_tasks[uid] = True
            if uid in cancel_events:
                cancel_events[uid].set()
            try:
                await q.edit_message_caption(
                    caption="❌ *বাতিল করা হয়েছে!*\n\n"
                            "নতুন ফাইল পাঠালে আবার শুরু হবে।",
                    parse_mode='Markdown', reply_markup=kb_home())
            except Exception:
                pass
        else:
            await q.answer("কোনো সক্রিয় কাজ নেই!", show_alert=True)
        return

    # ── Audio transcription callbacks ──
    if d.startswith("tr_"):
        parts = d.split("_")
        # tr_bn_UID or tr_en_UID or tr_translate_UID
        if len(parts) == 3:
            mode       = parts[1]        # bn / en / translate
            target_uid = int(parts[2])
        else:
            # tr_translate_UID — "translate" has underscore
            mode       = "translate"
            target_uid = int(parts[-1])

        if uid != target_uid:
            await q.answer("এটা তোমার জন্য নয়!", show_alert=True)
            return

        if not await is_member(uid, ctx.bot):
            await q.answer("🔒 চ্যানেলে যোগ দাও!", show_alert=True)
            return

        await q.answer()

        # message edit করে "Processing..." দেখাও
        mode_text = {
            'bn':        "🇧🇩 বাংলায় Transcription",
            'en':        "🇺🇸 English Transcription",
            'translate': "🔄 Transcription + বাংলা অনুবাদ"
        }.get(mode, "Transcription")

        try:
            await q.edit_message_text(
                f"✅ *{mode_text} শুরু হচ্ছে...*\n\n⏳ একটু অপেক্ষা করো...",
                parse_mode='Markdown')
        except Exception:
            pass

        # Background-এ চালাও
        asyncio.create_task(
            do_transcription(uid, mode, ctx.bot,
                             q.message.chat_id, ctx))
        return

    # ══════════════════════════════════════════
    # Membership check — বাকি সব callback-এ
    # ══════════════════════════════════════════
    if not await is_member(uid, ctx.bot):
        await q.answer("🔒 চ্যানেলে যোগ দাও!", show_alert=True)
        try:
            await q.edit_message_text(
                NOT_JOINED_MSG, parse_mode='Markdown',
                reply_markup=kb_not_joined())
        except Exception:
            pass
        return

    await q.answer()

    # ── SubDL download ──
    if d.startswith("subdl_"):
        idx      = d.split("_")[1]
        url_path = ctx.user_data.get(f"suburl_{idx}", '')
        fname    = ctx.user_data.get(f"subname_{idx}", 'subtitle.srt')
        if not url_path:
            await q.message.reply_text("❌ ফাইলের তথ্য পাওয়া যায়নি।"); return

        await q.answer("⏳ ডাউনলোড হচ্ছে...")
        loop    = asyncio.get_event_loop()
        content = await loop.run_in_executor(executor, subdl_download, url_path)
        if not content:
            await q.message.reply_text(
                "❌ ডাউনলোড হয়নি।\nSubDL ফ্রি-তে দিনে ৫টার বেশি হয় না।")
            return

        srt_bytes = content
        if content[:2] == b'PK':
            import zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    for name in z.namelist():
                        if name.lower().endswith('.srt'):
                            srt_bytes = z.read(name)
                            fname = os.path.basename(name); break
            except Exception as e:
                logger.error(f"Zip: {e}")

        if not fname.lower().endswith('.srt'):
            fname += '.srt'

        await q.message.reply_document(
            document=io.BytesIO(srt_bytes), filename=fname,
            caption=(f"✅ *Subtitle ডাউনলোড সম্পন্ন!*\n\n"
                     f"📁 `{fname}`\n\n"
                     f"_এই ফাইলটা আমাকে পাঠালে বাংলায় অনুবাদ করে দেব!_ 🔄"),
            parse_mode='Markdown')
        return

    # ── Chat callbacks ──
    if d == "chat_start":
        chat_mode[uid] = True
        if uid not in chat_history: chat_history[uid] = []
        await q.edit_message_text(
            "💬 *AI চ্যাট মোড চালু!*\n\n"
            "যা মনে চায় লেখো — আমি বাংলায় উত্তর দেব। 🤖\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 SRT/অডিও ফাইল পাঠালে তা কাজ করবে\n"
            "🗑 কথোপকথন মুছতে নিচের বাটন",
            parse_mode='Markdown', reply_markup=kb_chat())
        return

    if d == "chat_clear":
        chat_history[uid] = []
        await q.edit_message_text(
            "🗑 *কথোপকথন মুছে ফেলা হয়েছে!*\n\nনতুনভাবে শুরু করো 😊",
            parse_mode='Markdown', reply_markup=kb_chat())
        return

    if d == "chat_stop":
        chat_mode[uid] = False
        await q.edit_message_text(
            "✅ *চ্যাট মোড বন্ধ।*\n\nআবার শুরু করতে 💬 চাপো।",
            parse_mode='Markdown', reply_markup=kb_home())
        return

    if d == "audio_info":
        await q.edit_message_text(
            "🎙 *Audio Transcription*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "যেকোনো অডিও বা Voice message পাঠাও,\n"
            "তারপর বেছে নাও কী করতে চাও:\n\n"
            "🇧🇩 বাংলায় Transcription\n"
            "🇺🇸 English Transcription\n"
            "🔄 Transcription + বাংলা অনুবাদ\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📁 *সাপোর্টেড ফরম্যাট:*\n"
            "`mp3, mp4, wav, m4a, ogg, webm, flac`\n\n"
            "⚠️ *সর্বোচ্চ সাইজ:* 25MB\n"
            "⚡ *Powered by:* Groq Whisper AI",
            parse_mode='Markdown', reply_markup=kb_back())
        return

    # ── Standard buttons ──
    if d == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার বিধি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "*🔄 SRT অনুবাদ:*\n"
            "1️⃣ `.srt` ফাইল পাঠাও\n"
            "2️⃣ Live chart-এ progress দেখো\n"
            "3️⃣ ❌ Cancel বাটনে বাতিল করো\n"
            "4️⃣ অনুবাদিত ফাইল পাবে\n\n"
            "*🎙 Audio Transcription:*\n"
            "1️⃣ Voice message বা অডিও ফাইল পাঠাও\n"
            "2️⃣ বাংলা/English/অনুবাদ — যেটা চাও বেছে নাও\n"
            "3️⃣ ফলাফল পাবে\n\n"
            "*🔍 Subtitle খুঁজতে:*\n"
            "1️⃣ 🔍 বাটন চাপো\n"
            "2️⃣ মুভির নাম লেখো (English)\n"
            "3️⃣ ⬇️ বাটনে ডাউনলোড করো\n\n"
            "*💬 AI চ্যাট:*\n"
            "1️⃣ 💬 বাটন চাপো → লেখো → উত্তর পাও\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ SRT max: 5MB | Audio max: 25MB",
            parse_mode='Markdown', reply_markup=kb_back())

    elif d == "about":
        await q.edit_message_text(
            "ℹ️ *বট পরিচিতি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 *AI:* LLaMA 3.3 70B (Groq)\n"
            "🎙 *Transcription:* Whisper Large v3\n"
            "🌐 *Hosting:* Render Free\n"
            "📚 *Subtitle DB:* SubDL.com\n\n"
            "✨ *বিশেষত্ব:*\n"
            "• ভাবানুবাদ — আক্ষরিক নয়\n"
            "• Audio → বাংলা/English Transcription\n"
            "• Telegram Voice message সাপোর্ট\n"
            "• প্রতিটি SRT লাইন নিশ্চিত অনুবাদ\n"
            "• তাৎক্ষণিক Cancel সাপোর্ট\n"
            "• চ্যানেল leave করলে সাথে সাথে block",
            parse_mode='Markdown', reply_markup=kb_back())

    elif d == "status":
        running  = sum(1 for v in active_tasks.values() if not v)
        chatting = sum(1 for v in chat_mode.values() if v)
        await q.edit_message_text(
            "📊 *বট স্ট্যাটাস*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 *Bot:* Online\n"
            "🟢 *Groq AI:* Connected\n"
            "🟢 *Whisper:* Ready\n"
            "🟢 *Flask:* Running\n"
            "🔄 *Self-ping:* Active (14 min)\n"
            f"⚙️ *চলমান অনুবাদ:* {running}\n"
            f"💬 *সক্রিয় চ্যাট:* {chatting}\n\n"
            "_Bot is always awake!_ ⚡",
            parse_mode='Markdown', reply_markup=kb_back())

    elif d == "search":
        if not SUBDL_API_KEY:
            await q.edit_message_text(
                "❌ *Subtitle Search চালু নেই!*\n\n"
                "Render-এ `SUBDL_API_KEY` set করো।",
                parse_mode='Markdown', reply_markup=kb_back())
            return
        ctx.user_data['awaiting_search'] = True
        await q.edit_message_text(
            "🔍 *Subtitle খোঁজো*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "মুভির নাম *English-এ* লেখো:\n\n"
            "📌 `Pirates of the Caribbean`\n"
            "📌 `Breaking Bad S01E01`\n"
            "📌 `Me Before You 2016`",
            parse_mode='Markdown', reply_markup=kb_search_cancel())

    elif d == "home":
        chat_mode[uid] = False
        ctx.user_data['awaiting_search'] = False
        await q.edit_message_text(
            "🎬 *SRT Subtitle BD Bot*\n\n"
            "`.srt` ফাইল বা অডিও পাঠাও\n"
            "অথবা নিচের বাটন ব্যবহার করো! 🚀",
            parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 📁  SRT FILE HANDLER
# ══════════════════════════════════════════════
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    doc = update.message.document

    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            NOT_JOINED_MSG, parse_mode='Markdown',
            reply_markup=kb_not_joined())
        return

    fname = doc.file_name.lower()

    # অডিও ফাইল হলে audio handler-এ পাঠাও
    if any(fname.endswith(ext) for ext in AUDIO_EXTENSIONS):
        # document হিসেবে আসা audio ফাইল handle করো
        file_size = doc.file_size
        if file_size and file_size > MAX_AUDIO_SIZE:
            await update.message.reply_text(
                "❌ *অডিও ফাইল ২৫MB-এর বেশি!*",
                parse_mode='Markdown')
            return

        pending_audio[u.id] = {
            'file_id':   doc.file_id,
            'file_name': doc.file_name,
            'duration':  ''
        }
        await update.message.reply_text(
            f"🎙 *অডিও ফাইল পেয়েছি!*\n\n"
            f"📁 `{doc.file_name}`\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"এখন কী করতে চাও?",
            parse_mode='Markdown',
            reply_markup=kb_audio_options(u.id))
        return

    # SRT ফাইল
    if not fname.endswith('.srt'):
        await update.message.reply_text(
            "❌ *শুধুমাত্র `.srt` বা অডিও ফাইল পাঠাও!*\n\n"
            "সাপোর্টেড অডিও: `mp3, mp4, wav, m4a, ogg, webm`",
            parse_mode='Markdown')
        return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text(
            "❌ *SRT ফাইল সাইজ ৫MB-এর বেশি!*",
            parse_mode='Markdown')
        return

    if u.id in active_tasks and not active_tasks[u.id]:
        await update.message.reply_text(
            "⚠️ *একটি অনুবাদ ইতিমধ্যে চলছে!*\n\n"
            "আগেরটা শেষ করো বা Cancel করো।",
            parse_mode='Markdown')
        return

    chat_mode[u.id]     = False
    active_tasks[u.id]  = False
    cancel_events[u.id] = threading.Event()
    c_event             = cancel_events[u.id]

    status = await update.message.reply_photo(
        photo=generate_pie_chart(0, 1),
        caption=(f"📥 *ফাইল পেয়েছি!*\n\n"
                 f"📁 `{doc.file_name}`\n⏳ প্রস্তুত হচ্ছে..."),
        parse_mode='Markdown', reply_markup=kb_cancel(u.id))

    try:
        f   = await ctx.bot.get_file(doc.file_id)
        raw = await f.download_as_bytearray()

        srt_text = None
        for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
            try:
                srt_text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue

        if not srt_text:
            await status.edit_caption(
                "❌ ফাইল পড়তে পারছি না! UTF-8 দিয়ে সেভ করো.",
                parse_mode='Markdown')
            return

        blocks = parse_srt(srt_text)
        if not blocks:
            await status.edit_caption(
                "❌ SRT ফাইলে কোনো সাবটাইটেল নেই!",
                parse_mode='Markdown')
            return

        total = len(blocks)
        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(0, total),
            caption=(f"🎬 *অনুবাদ শুরু হচ্ছে...*\n\n"
                     f"📁 `{doc.file_name}`\n"
                     f"📊 মোট: *{total}টি* সাবটাইটেল\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n⏳ 0/{total} (0%)"),
            parse_mode='Markdown'),
            reply_markup=kb_cancel(u.id))

        BATCH      = 7
        translated = list(blocks)
        completed  = 0
        loop       = asyncio.get_event_loop()

        for i in range(0, total, BATCH):
            if c_event.is_set() or active_tasks.get(u.id, False):
                return
            chunk  = blocks[i:i + BATCH]
            texts  = [b['text'] for b in chunk]
            result = await loop.run_in_executor(
                executor,
                functools.partial(translate_batch_sync, texts, c_event))
            if c_event.is_set() or active_tasks.get(u.id, False):
                return
            for j, tr in enumerate(result):
                if i + j < total:
                    translated[i + j]['text'] = tr
            completed = min(i + BATCH, total)
            pct  = completed / total * 100
            bar  = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))
            try:
                await status.edit_media(InputMediaPhoto(
                    media=generate_pie_chart(completed, total),
                    caption=(f"🔄 *অনুবাদ চলছে...*\n\n"
                             f"📁 `{doc.file_name}`\n"
                             f"`[{bar}]` *{pct:.1f}%*\n"
                             f"━━━━━━━━━━━━━━━━━━━━━\n"
                             f"✅ সম্পন্ন: *{completed}/{total}*\n"
                             f"⏳ বাকি: *{total - completed}টি*"),
                    parse_mode='Markdown'),
                    reply_markup=kb_cancel(u.id))
            except Exception as e:
                logger.warning(f"Edit ignored: {e}")
            await asyncio.sleep(0.4)

        if c_event.is_set() or active_tasks.get(u.id, False):
            return

        out_bytes = build_srt(translated).encode('utf-8-sig')
        out_name  = doc.file_name.replace('.srt', '_Bengali.srt')

        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(total, total),
            caption=(f"✅ *অনুবাদ সম্পন্ন!*\n\n"
                     f"📁 `{doc.file_name}`\n"
                     f"🎉 *{total}টি* সাবটাইটেল অনুবাদ হয়েছে\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n"
                     f"⬇️ নিচের ফাইলটি ডাউনলোড করো"),
            parse_mode='Markdown'))

        await update.message.reply_document(
            document=io.BytesIO(out_bytes), filename=out_name,
            caption=(f"🎬 *অনুবাদিত সাবটাইটেল ফাইল*\n\n"
                     f"📁 `{out_name}`\n"
                     f"✅ *{total}টি* লাইন — প্রতিটি অনুবাদিত\n"
                     f"⏱ Timing সম্পূর্ণ অক্ষুণ্ণ\n\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n"
                     f"_VLC / MX Player-এ ব্যবহার করো_ 🎥"),
            parse_mode='Markdown', reply_markup=kb_home())

        logger.info(f"✅ Done {total} for {u.id}")

    except Exception as e:
        err_msg = str(e)
        logger.error(f"Error {u.id}: {err_msg}")
        if "QUOTA_EXCEEDED" in err_msg:
            notice = (
                "⚠️ *Groq API Limit শেষ!*\n\n"
                "তোমার API key-এর দৈনিক limit শেষ।\n\n"
                "🔧 *সমাধান:*\n"
                "1️⃣ [console.groq.com](https://console.groq.com) যাও\n"
                "2️⃣ নতুন API key তৈরি করো\n"
                "3️⃣ Render-এ `GROQ_API_KEY` update করো\n\n"
                "⏰ অথবা ২৪ ঘণ্টা পরে আবার চেষ্টা করো।"
            )
            try:
                await status.edit_caption(
                    notice, parse_mode='Markdown',
                    reply_markup=kb_quota_error())
            except Exception:
                await update.message.reply_text(
                    notice, parse_mode='Markdown',
                    reply_markup=kb_quota_error())
        else:
            notice = f"❌ *সমস্যা হয়েছে!*\n\n`{err_msg[:200]}`\n\nআবার চেষ্টা করো।"
            try:
                await status.edit_caption(
                    notice, parse_mode='Markdown', reply_markup=kb_home())
            except Exception:
                await update.message.reply_text(
                    notice, parse_mode='Markdown', reply_markup=kb_home())
    finally:
        active_tasks.pop(u.id, None)
        cancel_events.pop(u.id, None)

# ══════════════════════════════════════════════
# 💬  TEXT HANDLER
# ══════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    text = update.message.text.strip()

    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            NOT_JOINED_MSG, parse_mode='Markdown',
            reply_markup=kb_not_joined())
        return

    # Search mode
    if ctx.user_data.get('awaiting_search'):
        ctx.user_data['awaiting_search'] = False
        msg     = await update.message.reply_text(
            f"🔍 *খোঁজা হচ্ছে:* `{text}`\n\n⏳ একটু অপেক্ষা করো...",
            parse_mode='Markdown')
        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(executor, subdl_search, text)

        if not results:
            await msg.edit_text(
                f"😔 *`{text}`* এর জন্য কিছু পাওয়া যায়নি!\n\n"
                f"একটু ভিন্নভাবে লিখে আবার চেষ্টা করো।",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")],
                    [InlineKeyboardButton("🔙 হোম",         callback_data="home")]
                ]))
            return

        body    = f"🎬 *`{text}`* এর Subtitle:\n\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        buttons = []
        for i, item in enumerate(results, 1):
            name     = item.get('release_name', 'Unknown')[:45]
            lang     = item.get('language', 'EN')
            url_path = item.get('url', '')
            year     = item.get('year', '')
            fname    = (item.get('release_name', f'subtitle_{i}') + '.srt')[:60]
            ctx.user_data[f"suburl_{i}"]  = url_path
            ctx.user_data[f"subname_{i}"] = fname
            yr    = f" ({year})" if year else ""
            body += f"*{i}.* {name}{yr}\n   🌐 {lang}\n\n"
            buttons.append([InlineKeyboardButton(
                f"⬇️ {i}. {name[:33]}{yr}", callback_data=f"subdl_{i}")])
        buttons.append([InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")])
        buttons.append([InlineKeyboardButton("🔙 হোম",         callback_data="home")])
        await msg.edit_text(body, parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(buttons))
        return

    # AI Chat mode
    if chat_mode.get(u.id, False):
        await ctx.bot.send_chat_action(
            chat_id=update.effective_chat.id, action="typing")
        loop  = asyncio.get_event_loop()
        reply = await loop.run_in_executor(
            executor, functools.partial(ai_chat_sync, u.id, text))
        history_len = len(chat_history.get(u.id, []))
        await update.message.reply_text(
            f"{reply}\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"_💬 {history_len // 2} বার্তা_ | _/start → হোম_",
            parse_mode='Markdown', reply_markup=kb_chat())
        return

    # Default
    await update.message.reply_text(
        "📌 *কী করতে চাও?*\n\n"
        "• SRT অনুবাদ → `.srt` ফাইল পাঠাও\n"
        "• Transcription → 🎙 অডিও/Voice পাঠাও\n"
        "• Subtitle খুঁজতে → 🔍 বাটন চাপো\n"
        "• AI চ্যাট → 💬 বাটন চাপো",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!"); return
    if not GROQ_API_KEY:
        logger.error("❌ GROQ_API_KEY not set!"); return

    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    logger.info("✅ Flask + self-ping started")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))

    # Voice message
    app.add_handler(MessageHandler(filters.VOICE, handle_audio_or_voice))
    # Audio file
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio_or_voice))
    # Document (SRT + audio as document)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    # Text
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🤖 Bot polling started!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=30,
        write_timeout=30,
        connect_timeout=30,
        pool_timeout=30,
    )

if __name__ == '__main__':
    main()
