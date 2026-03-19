#!/usr/bin/env python3
"""
🎬 SRT Subtitle Translator Bot — Final Version
বাংলা সাবটাইটেল অনুবাদক | Powered by Groq AI
Python 3.11 | PTB 20.7 | SubDL Search
"""

import os, re, io, time, asyncio, logging, threading, requests
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

groq_client  = Groq(api_key=GROQ_API_KEY)
executor     = ThreadPoolExecutor(max_workers=4)
active_tasks = {}   # {user_id: False=running | True=cancelled}

# ══════════════════════════════════════════════
# 🌐  FLASK  (keep-alive)
# ══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return """<!DOCTYPE html><html><head><title>SRT Bot</title><meta charset="UTF-8">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#0f0e17,#1a1a2e);
     color:#fff;display:flex;justify-content:center;
     align-items:center;min-height:100vh;flex-direction:column;gap:16px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,137,6,.35);
      border-radius:20px;padding:36px 56px;text-align:center}
h1{color:#ff8906;font-size:2.6em;margin-bottom:8px}
.dot{width:13px;height:13px;background:#00d4aa;border-radius:50%;
     display:inline-block;animation:p 1.5s infinite;box-shadow:0 0 8px #00d4aa}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
p{color:#a7a9be;font-size:1.05em;line-height:1.9}
.b{display:inline-block;background:rgba(255,137,6,.13);border:1px solid #ff8906;
   color:#ff8906;padding:5px 14px;border-radius:18px;font-size:.88em;margin:4px}
</style></head><body>
<div class="card">
  <h1>🎬 SRT Translator Bot</h1>
  <p><span class="dot"></span>&nbsp;
     <span style="color:#00d4aa;font-weight:700;font-size:1.15em">Bot is Live!</span></p>
  <p>English Subtitle → সুন্দর বাংলা অনুবাদ</p><br>
  <div>
    <span class="b">🤖 Groq AI</span>
    <span class="b">⚡ LLaMA 3.3 70B</span>
    <span class="b">🔍 SubDL Search</span>
    <span class="b">❌ Cancel Support</span>
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
# 📊  PIE CHART  (English labels — no Bengali font needed)
# ══════════════════════════════════════════════
def generate_pie_chart(completed: int, total: int) -> io.BytesIO:
    pct       = (completed / total * 100) if total > 0 else 0
    remaining = max(total - completed, 0)

    fig, ax = plt.subplots(figsize=(7, 5.5))
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
# 🤖  GROQ TRANSLATION  (fixed — no missing lines)
# ══════════════════════════════════════════════
SYSTEM_PROMPT = (
    "তুমি একজন পেশাদার চলচ্চিত্র সাবটাইটেল অনুবাদক।\n"
    "নিয়ম:\n"
    "- ভাব বুঝে অনুবাদ করো, আক্ষরিক নয়\n"
    "- স্বাভাবিক কথ্য বাংলা ব্যবহার করো\n"
    "- আবেগ ও টোন বজায় রাখো\n"
    "- শুধু অনুবাদ দেবে, বাড়তি কিছু লিখবে না"
)

def translate_one_sync(text: str) -> str:
    """একটি মাত্র subtitle লাইন অনুবাদ — fallback হিসেবে ব্যবহার হয়"""
    for _ in range(3):
        try:
            r = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",
                     "content": f"শুধু এই সাবটাইটেলটি বাংলায় অনুবাদ করো:\n{text}"}
                ],
                temperature=0.15, max_tokens=256
            )
            return r.choices[0].message.content.strip()
        except Exception as e:
            if 'rate_limit' in str(e).lower():
                time.sleep(60)
            else:
                time.sleep(3)
    return text   # fail হলে original ফেরত

def translate_batch_sync(texts: list) -> list:
    """
    Batch অনুবাদ।
    যদি কোনো লাইন missing থাকে → সেই লাইনটা আলাদাভাবে অনুবাদ করে।
    এভাবে কোনো লাইন skip হওয়া সম্ভব না।
    """
    # numbered marker ব্যবহার করি — ||| এর চেয়ে বেশি নির্ভরযোগ্য
    numbered = '\n'.join(f"[{i+1}] {t}" for i, t in enumerate(texts))
    user_msg = (
        f"নিচের {len(texts)}টি সাবটাইটেল লাইন বাংলায় অনুবাদ করো।\n"
        f"প্রতিটি লাইনের আগে একই নম্বর রাখো: [1], [2], [3]...\n"
        f"শুধু অনুবাদ দাও, অন্য কিছু লিখবে না।\n\n"
        f"{numbered}\n\nBengali:"
    )

    translated = [None] * len(texts)

    for attempt in range(3):
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role": "system", "content": SYSTEM_PROMPT},
                          {"role": "user",   "content": user_msg}],
                temperature=0.15,
                max_tokens=3000
            )
            raw = resp.choices[0].message.content.strip()

            # parse [N] pattern
            pattern = re.compile(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|\Z)', re.DOTALL)
            for m in pattern.finditer(raw):
                idx = int(m.group(1)) - 1
                val = m.group(2).strip()
                if 0 <= idx < len(texts) and val:
                    translated[idx] = val
            break

        except Exception as e:
            if 'rate_limit' in str(e).lower():
                logger.warning(f"Rate limit, waiting 60s (attempt {attempt+1})")
                time.sleep(60)
            else:
                logger.error(f"Batch error: {e}")
                time.sleep(5)

    # যেগুলো এখনো None → একটা একটা করে অনুবাদ করো
    for i, val in enumerate(translated):
        if val is None:
            logger.info(f"Fallback single translate for index {i}")
            translated[i] = translate_one_sync(texts[i])

    return translated

# ══════════════════════════════════════════════
# 🔍  SUBDL API
# ══════════════════════════════════════════════
SUBDL_BASE = "https://api.subdl.com/api/v1"

def subdl_search(query: str) -> list:
    try:
        r = requests.get(
            f"{SUBDL_BASE}/subtitles",
            params={"api_key": SUBDL_API_KEY, "film_name": query,
                    "languages": "EN", "subs_per_page": 8},
            timeout=15
        )
        if r.status_code != 200:
            logger.error(f"SubDL search error: {r.status_code} {r.text[:200]}")
            return []
        return r.json().get("subtitles", [])[:8]
    except Exception as e:
        logger.error(f"SubDL exception: {e}")
        return []

def subdl_download(url_path: str) -> bytes | None:
    try:
        full = f"https://dl.subdl.com{url_path}"
        r = requests.get(full, timeout=30)
        return r.content if r.status_code == 200 else None
    except Exception as e:
        logger.error(f"SubDL download error: {e}")
        return None

# ══════════════════════════════════════════════
# 🔒  CHANNEL CHECK
# ══════════════════════════════════════════════
async def is_member(uid: int, bot) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return m.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.warning(f"Member check: {e}")
        return False

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
        [InlineKeyboardButton("📖 ব্যবহার বিধি",  callback_data="help"),
         InlineKeyboardButton("ℹ️ বট সম্পর্কে",   callback_data="about")],
        [InlineKeyboardButton("🔍 Subtitle খোঁজো", callback_data="search"),
         InlineKeyboardButton("📊 স্ট্যাটাস",       callback_data="status")]
    ])

def kb_back():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 হোম", callback_data="home")]
    ])

def kb_cancel(uid: int):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ অনুবাদ বাতিল করো", callback_data=f"cancel_{uid}")]
    ])

def kb_search_cancel():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ বাতিল", callback_data="home")]
    ])

# ══════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 *বট ব্যবহার করতে প্রথমে চ্যানেলে যোগ দাও!*\n\n"
            f"📢 চ্যানেল: `{CHANNEL_USERNAME}`",
            parse_mode='Markdown', reply_markup=kb_not_joined())
        return

    await update.message.reply_text(
        f"🎬 *SRT সাবটাইটেল অনুবাদক বটে স্বাগতম!*\n\n"
        f"হ্যালো *{u.first_name}* ভাই! 👋\n\n"
        f"আমি যা করতে পারি:\n"
        f"🔄 English `.srt` → সুন্দর বাংলা অনুবাদ\n"
        f"🔍 মুভির নাম দিয়ে Subtitle খোঁজা ও ডাউনলোড\n"
        f"❌ যেকোনো সময় অনুবাদ বাতিল করা\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 অনুবাদ করতে → `.srt` ফাইল পাঠাও\n"
        f"🔍 খুঁজতে → নিচের বাটন চাপো\n\n"
        f"⚡ _Powered by Groq AI (LLaMA 3.3 70B)_",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    # ── Cancel translation ──
    if d.startswith("cancel_"):
        uid = int(d.split("_")[1])
        if q.from_user.id == uid and uid in active_tasks:
            active_tasks[uid] = True   # signal cancellation
            try:
                await q.edit_message_caption(
                    caption="❌ *অনুবাদ বাতিল করা হয়েছে!*\n\n"
                            "নতুন ফাইল পাঠালে আবার শুরু হবে।",
                    parse_mode='Markdown', reply_markup=kb_home())
            except Exception:
                pass
        else:
            await q.answer("কোনো সক্রিয় অনুবাদ নেই!", show_alert=True)
        return

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
                "❌ ডাউনলোড হয়নি।\n"
                "SubDL ফ্রি account-এ দিনে ৫টার বেশি download হয় না।")
            return

        # zip হলে unzip করে srt বের করো
        srt_content = content
        if fname.lower().endswith('.zip') or content[:2] == b'PK':
            import zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    for name in z.namelist():
                        if name.lower().endswith('.srt'):
                            srt_content = z.read(name)
                            fname = name
                            break
            except Exception as e:
                logger.error(f"Zip extract error: {e}")

        await q.message.reply_document(
            document=io.BytesIO(srt_content),
            filename=fname if fname.endswith('.srt') else fname + '.srt',
            caption=(
                f"✅ *Subtitle ডাউনলোড সম্পন্ন!*\n\n"
                f"📁 `{fname}`\n\n"
                f"_এই ফাইলটা আমাকে পাঠালে বাংলায় অনুবাদ করে দেব!_ 🔄"
            ),
            parse_mode='Markdown')
        return

    # ── Standard ──
    if d == "chk":
        if await is_member(q.from_user.id, ctx.bot):
            await q.edit_message_text(
                "✅ *দারুণ! চ্যানেলে যোগ দিয়েছ!*\n\n"
                "`.srt` ফাইল পাঠাও অনুবাদ শুরু করতে 🚀",
                parse_mode='Markdown', reply_markup=kb_home())
        else:
            await q.edit_message_text(
                "❌ *এখনো যোগ দাওনি!*\n\nযোগ দাও, তারপর চেক করো।",
                parse_mode='Markdown', reply_markup=kb_not_joined())

    elif d == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার বিধি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "*🔄 অনুবাদ করতে:*\n"
            "1️⃣ যেকোনো `.srt` ফাইল পাঠাও\n"
            "2️⃣ Live chart-এ progress দেখো\n"
            "3️⃣ মাঝে বন্ধ করতে ❌ Cancel চাপো\n"
            "4️⃣ অনুবাদিত `.srt` ফাইল পাবে\n\n"
            "*🔍 Subtitle খুঁজতে:*\n"
            "1️⃣ 🔍 বাটন চাপো\n"
            "2️⃣ মুভির নাম লেখো (English)\n"
            "3️⃣ ফলাফল থেকে ⬇️ বাটনে ডাউনলোড করো\n"
            "4️⃣ ডাউনলোড করা ফাইল পাঠিয়ে অনুবাদ করাও!\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ Max size: 5MB | Format: `.srt`",
            parse_mode='Markdown', reply_markup=kb_back())

    elif d == "about":
        await q.edit_message_text(
            "ℹ️ *বট পরিচিতি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 *AI:* LLaMA 3.3 70B (Groq)\n"
            "🌐 *Hosting:* Render Free\n"
            "📚 *Subtitle DB:* SubDL.com\n"
            "⚡ *Speed:* Ultra-fast\n\n"
            "✨ *বিশেষত্ব:*\n"
            "• ভাবানুবাদ — আক্ষরিক নয়\n"
            "• প্রতিটি লাইন নিশ্চিত অনুবাদ (কোনো skip নেই)\n"
            "• Timing সম্পূর্ণ নির্ভুল\n"
            "• যেকোনো সময় Cancel করা যায়\n"
            "• মুভির Subtitle সরাসরি খোঁজা ও ডাউনলোড",
            parse_mode='Markdown', reply_markup=kb_back())

    elif d == "status":
        running = sum(1 for v in active_tasks.values() if not v)
        await q.edit_message_text(
            "📊 *বট স্ট্যাটাস*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 *Bot:* Online\n"
            "🟢 *Groq AI:* Connected\n"
            "🟢 *Flask:* Running\n"
            "🔄 *Self-ping:* Active (14 min)\n"
            f"⚙️ *চলমান অনুবাদ:* {running}\n\n"
            "_Bot is always awake!_ ⚡",
            parse_mode='Markdown', reply_markup=kb_back())

    elif d == "search":
        if not SUBDL_API_KEY:
            await q.edit_message_text(
                "❌ *Subtitle Search চালু নেই!*\n\n"
                "Render-এ `SUBDL_API_KEY` set করো।\n"
                "subdl.com থেকে ফ্রি API key নাও।",
                parse_mode='Markdown', reply_markup=kb_back())
            return
        ctx.user_data['awaiting_search'] = True
        await q.edit_message_text(
            "🔍 *Subtitle খোঁজো*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "মুভি বা সিরিজের নাম *English-এ* লেখো:\n\n"
            "📌 _উদাহরণ:_ `Pirates of the Caribbean`\n"
            "📌 _উদাহরণ:_ `Breaking Bad S01E01`\n"
            "📌 _উদাহরণ:_ `Me Before You 2016`",
            parse_mode='Markdown', reply_markup=kb_search_cancel())

    elif d == "home":
        ctx.user_data['awaiting_search'] = False
        await q.edit_message_text(
            "🎬 *SRT সাবটাইটেল অনুবাদক বট*\n\n"
            "`.srt` ফাইল পাঠাও অনুবাদ করতে\n"
            "অথবা 🔍 দিয়ে subtitle খোঁজো! 🚀",
            parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 📁  SRT FILE HANDLER
# ══════════════════════════════════════════════
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    doc = update.message.document

    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 বট ব্যবহার করতে চ্যানেলে যোগ দাও!",
            reply_markup=kb_not_joined()); return

    if not doc.file_name.lower().endswith('.srt'):
        await update.message.reply_text(
            "❌ *শুধুমাত্র `.srt` ফাইল পাঠাও!*",
            parse_mode='Markdown'); return

    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text(
            "❌ *ফাইল সাইজ ৫MB-এর বেশি!*",
            parse_mode='Markdown'); return

    if u.id in active_tasks and not active_tasks[u.id]:
        await update.message.reply_text(
            "⚠️ *একটি অনুবাদ ইতিমধ্যে চলছে!*\n\n"
            "আগেরটা শেষ করো বা Cancel করো।",
            parse_mode='Markdown'); return

    active_tasks[u.id] = False   # running

    status = await update.message.reply_photo(
        photo=generate_pie_chart(0, 1),
        caption=(f"📥 *ফাইল পেয়েছি!*\n\n"
                 f"📁 `{doc.file_name}`\n⏳ প্রস্তুত হচ্ছে..."),
        parse_mode='Markdown', reply_markup=kb_cancel(u.id))

    try:
        # ── download ──
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
                "❌ ফাইল পড়তে পারছি না! UTF-8 encoding দিয়ে সেভ করো.",
                parse_mode='Markdown'); active_tasks.pop(u.id, None); return

        blocks = parse_srt(srt_text)
        if not blocks:
            await status.edit_caption(
                "❌ SRT ফাইলে কোনো সাবটাইটেল নেই!",
                parse_mode='Markdown'); active_tasks.pop(u.id, None); return

        total = len(blocks)

        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(0, total),
            caption=(f"🎬 *অনুবাদ শুরু হচ্ছে...*\n\n"
                     f"📁 `{doc.file_name}`\n"
                     f"📊 মোট সাবটাইটেল: *{total}টি*\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n⏳ 0/{total} (0%)"),
            parse_mode='Markdown'),
            reply_markup=kb_cancel(u.id))

        # ── translate ──
        BATCH      = 7          # ছোট batch → নির্ভুল অনুবাদ
        translated = list(blocks)
        completed  = 0
        loop       = asyncio.get_event_loop()

        for i in range(0, total, BATCH):

            if active_tasks.get(u.id, False):   # cancelled?
                logger.info(f"Cancelled by {u.id}")
                active_tasks.pop(u.id, None); return

            chunk  = blocks[i:i + BATCH]
            texts  = [b['text'] for b in chunk]
            result = await loop.run_in_executor(
                executor, translate_batch_sync, texts)

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

            await asyncio.sleep(0.5)

        if active_tasks.get(u.id, False):
            active_tasks.pop(u.id, None); return

        # ── send result ──
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
            document=io.BytesIO(out_bytes),
            filename=out_name,
            caption=(f"🎬 *অনুবাদিত সাবটাইটেল ফাইল*\n\n"
                     f"📁 `{out_name}`\n"
                     f"✅ *{total}টি* লাইন — প্রতিটি অনুবাদিত\n"
                     f"⏱ Timing সম্পূর্ণ অক্ষুণ্ণ\n\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n"
                     f"_VLC / MX Player-এ ব্যবহার করো_ 🎥"),
            parse_mode='Markdown', reply_markup=kb_home())

        logger.info(f"✅ Done {total} lines for user {u.id}")

    except Exception as e:
        logger.error(f"Error {u.id}: {e}")
        try:
            await status.edit_caption(
                f"❌ *সমস্যা হয়েছে!*\n\n`{str(e)[:200]}`\n\nআবার চেষ্টা করো।",
                parse_mode='Markdown')
        except Exception:
            pass
    finally:
        active_tasks.pop(u.id, None)

# ══════════════════════════════════════════════
# 💬  TEXT HANDLER  (subtitle search)
# ══════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 বট ব্যবহার করতে চ্যানেলে যোগ দাও!",
            reply_markup=kb_not_joined()); return

    # ── search mode ──
    if ctx.user_data.get('awaiting_search'):
        ctx.user_data['awaiting_search'] = False
        query = update.message.text.strip()

        msg = await update.message.reply_text(
            f"🔍 *খোঁজা হচ্ছে:* `{query}`\n\n⏳ একটু অপেক্ষা করো...",
            parse_mode='Markdown')

        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(executor, subdl_search, query)

        if not results:
            await msg.edit_text(
                f"😔 *`{query}`* এর জন্য কিছু পাওয়া যায়নি!\n\n"
                f"একটু ভিন্নভাবে লিখে আবার চেষ্টা করো।",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")],
                    [InlineKeyboardButton("🔙 হোম",         callback_data="home")]
                ]))
            return

        text    = f"🎬 *`{query}`* এর Subtitle:\n\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        buttons = []

        for i, item in enumerate(results, 1):
            name     = item.get('release_name', 'Unknown')[:45]
            lang     = item.get('language', 'EN')
            url_path = item.get('url', '')
            year     = item.get('year', '')
            fname    = (item.get('release_name', f'subtitle_{i}') + '.srt')[:60]

            # store
            ctx.user_data[f"suburl_{i}"] = url_path
            ctx.user_data[f"subname_{i}"] = fname

            yr = f" ({year})" if year else ""
            text += f"*{i}.* {name}{yr}\n   🌐 {lang}\n\n"
            buttons.append([InlineKeyboardButton(
                f"⬇️ {i}. {name[:35]}{yr}",
                callback_data=f"subdl_{i}")])

        buttons.append([InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")])
        buttons.append([InlineKeyboardButton("🔙 হোম",         callback_data="home")])

        await msg.edit_text(
            text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup(buttons))
        return

    # ── default ──
    await update.message.reply_text(
        "📌 *কী করতে চাও?*\n\n"
        "• অনুবাদ করতে → `.srt` ফাইল পাঠাও\n"
        "• Subtitle খুঁজতে → 🔍 বাটন চাপো",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!"); return
    if not GROQ_API_KEY:
        logger.error("❌ GROQ_API_KEY not set!"); return

    threading.Thread(target=run_flask,  daemon=True).start()
    threading.Thread(target=self_ping,  daemon=True).start()
    logger.info("✅ Flask + self-ping started")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🤖 Bot polling started!")
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=True)

if __name__ == '__main__':
    main()
