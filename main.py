#!/usr/bin/env python3
"""
🎬 SRT Subtitle Translator Bot
বাংলা সাবটাইটেল অনুবাদক | Powered by Groq AI
Compatible: Python 3.11 | PTB 20.7
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
# ⚙️ Configuration
# ══════════════════════════════════════════════
BOT_TOKEN        = os.environ.get('BOT_TOKEN', '')
GROQ_API_KEY     = os.environ.get('GROQ_API_KEY', '')
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', '@your_channel')
RENDER_URL       = os.environ.get('RENDER_URL', '')

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

groq_client = Groq(api_key=GROQ_API_KEY)
executor    = ThreadPoolExecutor(max_workers=3)

# ══════════════════════════════════════════════
# 🌐 Flask Server (Keep Alive)
# ══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
def home():
    return """
    <!DOCTYPE html>
    <html>
    <head>
        <title>🎬 SRT Translator Bot</title>
        <meta charset="UTF-8">
        <style>
            * { margin: 0; padding: 0; box-sizing: border-box; }
            body {
                font-family: 'Segoe UI', sans-serif;
                background: linear-gradient(135deg, #0f0e17 0%, #1a1a2e 100%);
                color: #fffffe;
                display: flex;
                justify-content: center;
                align-items: center;
                min-height: 100vh;
                flex-direction: column;
                gap: 20px;
            }
            .card {
                background: rgba(255,255,255,0.05);
                border: 1px solid rgba(255,137,6,0.3);
                border-radius: 20px;
                padding: 40px 60px;
                text-align: center;
                backdrop-filter: blur(10px);
            }
            h1 { color: #ff8906; font-size: 2.8em; margin-bottom: 10px; }
            .status {
                display: flex;
                align-items: center;
                justify-content: center;
                gap: 10px;
                margin: 20px 0;
            }
            .dot {
                width: 14px; height: 14px;
                background: #00d4aa;
                border-radius: 50%;
                animation: pulse 1.5s infinite;
                box-shadow: 0 0 10px #00d4aa;
            }
            @keyframes pulse { 0%,100%{opacity:1;transform:scale(1)} 50%{opacity:0.5;transform:scale(0.8)} }
            p { color: #a7a9be; font-size: 1.1em; line-height: 1.8; }
            .badge {
                display: inline-block;
                background: rgba(255,137,6,0.15);
                border: 1px solid #ff8906;
                color: #ff8906;
                padding: 6px 16px;
                border-radius: 20px;
                font-size: 0.9em;
                margin: 5px;
            }
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🎬 SRT Translator Bot</h1>
            <div class="status">
                <div class="dot"></div>
                <span style="color:#00d4aa; font-size:1.2em; font-weight:bold;">Bot is Live & Running!</span>
            </div>
            <p>English সাবটাইটেল → সুন্দর বাংলা অনুবাদ</p>
            <br>
            <div>
                <span class="badge">🤖 Groq AI</span>
                <span class="badge">⚡ LLaMA 3.3 70B</span>
                <span class="badge">🔄 Self-Ping Active</span>
            </div>
        </div>
    </body>
    </html>
    """, 200

@flask_app.route('/ping')
def ping():
    return 'pong', 200

@flask_app.route('/health')
def health():
    return {'status': 'ok', 'bot': 'running'}, 200

def run_flask():
    port = int(os.environ.get('PORT', 10000))
    flask_app.run(host='0.0.0.0', port=port, use_reloader=False)

# ══════════════════════════════════════════════
# 🔄 Self-Ping (Render ঘুমানো ঠেকাতে)
# ══════════════════════════════════════════════
def self_ping():
    time.sleep(30)  # বট চালু হওয়ার ৩০ সেকেন্ড পর শুরু
    while True:
        time.sleep(840)  # প্রতি ১৪ মিনিটে পিং
        if RENDER_URL:
            try:
                r = requests.get(f"{RENDER_URL}/ping", timeout=15)
                logger.info(f"✅ Self-ping OK: {r.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ Self-ping failed: {e}")

# ══════════════════════════════════════════════
# 📄 SRT Parser & Builder
# ══════════════════════════════════════════════
def parse_srt(content: str) -> list:
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    blocks = []
    pattern = re.compile(
        r'(\d+)\n'
        r'(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n'
        r'((?:.+\n?)+?)(?=\n\d+\n|\Z)',
        re.MULTILINE
    )
    for m in pattern.finditer(content.strip() + '\n\n'):
        text = m.group(4).strip()
        if text:
            blocks.append({
                'index': m.group(1),
                'start': m.group(2),
                'end':   m.group(3),
                'text':  text
            })
    return blocks

def build_srt(blocks: list) -> str:
    parts = []
    for b in blocks:
        parts.append(f"{b['index']}\n{b['start']} --> {b['end']}\n{b['text']}")
    return '\n\n'.join(parts) + '\n'

# ══════════════════════════════════════════════
# 📊 Pie Chart Generator
# ══════════════════════════════════════════════
def generate_pie_chart(completed: int, total: int) -> io.BytesIO:
    pct       = (completed / total * 100) if total > 0 else 0
    remaining = max(total - completed, 0)

    fig, ax = plt.subplots(figsize=(7, 5.5))
    fig.patch.set_facecolor('#0f0e17')
    ax.set_facecolor('#0f0e17')

    if completed == 0:
        sizes, colors, labels = [100], ['#2d2d44'], ['অপেক্ষায়...']
    elif completed >= total:
        sizes, colors, labels = [100], ['#00d4aa'], ['সম্পন্ন ✓']
    else:
        sizes  = [completed, remaining]
        colors = ['#00d4aa', '#2d2d44']
        labels = [f'সম্পন্ন ({completed})', f'বাকি ({remaining})']

    explode = ([0.05, 0] if len(sizes) == 2 else [0])

    wedges, texts, autotexts = ax.pie(
        sizes,
        explode=explode,
        colors=colors,
        autopct='%1.1f%%',
        startangle=90,
        pctdistance=0.65,
        wedgeprops={'linewidth': 2.5, 'edgecolor': '#0f0e17'},
        shadow=True
    )
    for at in autotexts:
        at.set_color('white')
        at.set_fontsize(13)
        at.set_fontweight('bold')

    # মাঝখানে শতাংশ
    ax.text(0, 0, f'{pct:.1f}%',
            ha='center', va='center',
            fontsize=26, fontweight='bold', color='white')

    # Legend
    patches = [mpatches.Patch(color=colors[i], label=labels[i]) for i in range(len(labels))]
    ax.legend(
        handles=patches,
        loc='lower center',
        bbox_to_anchor=(0.5, -0.13),
        ncol=2,
        facecolor='#1e1e2e',
        edgecolor='#444466',
        labelcolor='white',
        fontsize=10
    )

    ax.set_title('অনুবাদের অগ্রগতি', color='#ff8906',
                 fontsize=15, fontweight='bold', pad=18)

    fig.text(0.5, 0.01,
             f'মোট: {total}  |  সম্পন্ন: {completed}  |  বাকি: {remaining}',
             ha='center', color='#a7a9be', fontsize=9)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=110,
                bbox_inches='tight', facecolor='#0f0e17')
    buf.seek(0)
    plt.close(fig)
    return buf

# ══════════════════════════════════════════════
# 🤖 Translation (Groq AI)
# ══════════════════════════════════════════════
SYSTEM_PROMPT = """তুমি একজন পেশাদার চলচ্চিত্র ও নাটকের সাবটাইটেল অনুবাদক।
তোমার কাজ হলো সাবটাইটেল স্বাভাবিক, কথ্য বাংলায় অনুবাদ করা।

নিয়ম:
- ভাব বুঝে অনুবাদ করো, আক্ষরিক অনুবাদ করো না
- স্বাভাবিক কথ্য বাংলা ব্যবহার করো
- আবেগ, টোন ও ভাবভঙ্গি বজায় রাখো
- শুধুমাত্র অনুবাদ দেবে, কোনো ব্যাখ্যা বা অতিরিক্ত কথা লিখবে না"""

def translate_batch_sync(texts: list) -> list:
    combined = ' ||| '.join(texts)
    user_msg = f"""নিচের সাবটাইটেলগুলো বাংলায় অনুবাদ করো।
প্রতিটি অনুবাদ ঠিক ||| দিয়ে আলাদা করো।
শুধু অনুবাদ দাও, আর কিছু লিখবে না।

Subtitles:
{combined}

Bengali Translation:"""

    for attempt in range(3):
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user",   "content": user_msg}
                ],
                temperature=0.15,
                max_tokens=2048
            )
            result     = resp.choices[0].message.content.strip()
            translated = [t.strip() for t in result.split('|||')]

            while len(translated) < len(texts):
                translated.append(texts[len(translated)])
            return translated[:len(texts)]

        except Exception as e:
            err = str(e).lower()
            if 'rate_limit' in err:
                logger.warning(f"Rate limit, waiting 60s... (attempt {attempt+1})")
                time.sleep(60)
            else:
                logger.error(f"Translation error (attempt {attempt+1}): {e}")
                if attempt == 2:
                    return texts
                time.sleep(5)
    return texts

# ══════════════════════════════════════════════
# 🔒 Channel Membership Check
# ══════════════════════════════════════════════
async def is_member(user_id: int, bot) -> bool:
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, user_id)
        return m.status in ['member', 'administrator', 'creator']
    except Exception as e:
        logger.warning(f"Membership check error: {e}")
        return False

def not_joined_keyboard():
    ch = CHANNEL_USERNAME.lstrip('@')
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 চ্যানেলে যোগ দাও", url=f"https://t.me/{ch}")],
        [InlineKeyboardButton("✅ যোগ দিয়েছি — চেক করো", callback_data="chk")]
    ])

def home_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📖 ব্যবহার বিধি", callback_data="help"),
            InlineKeyboardButton("ℹ️ বট সম্পর্কে",  callback_data="about")
        ],
        [InlineKeyboardButton("📊 বট স্ট্যাটাস", callback_data="status")]
    ])

def back_keyboard():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔙 হোম", callback_data="home")]
    ])

# ══════════════════════════════════════════════
# 🤖 Bot Handlers
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user

    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 *বট ব্যবহার করতে প্রথমে চ্যানেলে যোগ দাও!*\n\n"
            f"📢 চ্যানেল: `{CHANNEL_USERNAME}`\n\n"
            "নিচের বাটনে চাপো → যোগ দাও → ✅ চেক করো",
            parse_mode='Markdown',
            reply_markup=not_joined_keyboard()
        )
        return

    await update.message.reply_text(
        f"🎬 *SRT সাবটাইটেল অনুবাদক বটে স্বাগতম!*\n\n"
        f"হ্যালো *{u.first_name}* ভাই! 👋\n\n"
        f"আমি তোমার English সাবটাইটেল ফাইলকে\n"
        f"সুন্দর ও প্রাঞ্জল বাংলায় অনুবাদ করি। ✨\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 *কী করতে হবে?*\n"
        f"একটি `.srt` ফাইল পাঠাও — বাকি সব আমি করব!\n\n"
        f"⚡ _Powered by Groq AI (LLaMA 3.3 70B)_",
        parse_mode='Markdown',
        reply_markup=home_keyboard()
    )

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    if d == "chk":
        if await is_member(q.from_user.id, ctx.bot):
            await q.edit_message_text(
                "✅ *দারুণ! তুমি চ্যানেলে যোগ দিয়েছ!*\n\n"
                "এখন একটি `.srt` ফাইল পাঠাও\n"
                "অনুবাদ শুরু করতে 🚀",
                parse_mode='Markdown',
                reply_markup=home_keyboard()
            )
        else:
            await q.edit_message_text(
                "❌ *এখনো যোগ দাওনি!*\n\n"
                "চ্যানেলে যোগ দাও, তারপর আবার চেক করো।",
                parse_mode='Markdown',
                reply_markup=not_joined_keyboard()
            )

    elif d == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার বিধি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "1️⃣ যেকোনো `.srt` সাবটাইটেল ফাইল\n"
            "   এই চ্যাটে পাঠাও\n\n"
            "2️⃣ বট স্বয়ংক্রিয়ভাবে অনুবাদ শুরু করবে\n\n"
            "3️⃣ Live পাই চার্টে দেখবে কত % হলো\n\n"
            "4️⃣ অনুবাদ শেষে `.srt` ফাইল পাবে\n"
            "   ➜ Timing একদম অক্ষুণ্ণ থাকবে!\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⚠️ *সর্বোচ্চ ফাইল সাইজ:* 5MB\n"
            "✅ *সাপোর্টেড ফরম্যাট:* `.srt`",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )

    elif d == "about":
        await q.edit_message_text(
            "ℹ️ *বট পরিচিতি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🤖 *নাম:* SRT Translator Bot\n"
            "🧠 *AI:* LLaMA 3.3 70B (Groq)\n"
            "🌐 *Hosting:* Render Free Tier\n"
            "⚡ *Speed:* Ultra-fast inference\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "✨ *বিশেষত্ব:*\n"
            "• ভাবানুবাদ করে, আক্ষরিক নয়\n"
            "• কথ্য স্বাভাবিক বাংলা ব্যবহার\n"
            "• Timing একদম নির্ভুল\n"
            "• UTF-8 BOM সহ ফাইল (সব player চলে)\n"
            "• Live প্রগ্রেস চার্ট",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )

    elif d == "status":
        await q.edit_message_text(
            "📊 *বট স্ট্যাটাস*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🟢 *Bot:* Online ও সচল\n"
            "🟢 *Groq AI:* সংযুক্ত\n"
            "🟢 *Flask:* চালু\n"
            "🔄 *Self-ping:* সক্রিয় (প্রতি ১৪ মিনিট)\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "_বট সর্বদা জেগে আছে!_ ⚡",
            parse_mode='Markdown',
            reply_markup=back_keyboard()
        )

    elif d == "home":
        await q.edit_message_text(
            "🎬 *SRT সাবটাইটেল অনুবাদক বট*\n\n"
            "একটি `.srt` ফাইল পাঠাও\n"
            "অনুবাদ শুরু করতে! 🚀",
            parse_mode='Markdown',
            reply_markup=home_keyboard()
        )

# ══════════════════════════════════════════════
# 📁 File Handler (মূল কাজ)
# ══════════════════════════════════════════════
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    doc = update.message.document

    # ── Membership check ──
    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 বট ব্যবহার করতে প্রথমে চ্যানেলে যোগ দাও!",
            reply_markup=not_joined_keyboard()
        )
        return

    # ── File type check ──
    if not doc.file_name.lower().endswith('.srt'):
        await update.message.reply_text(
            "❌ *শুধুমাত্র `.srt` ফাইল পাঠাও!*\n\n"
            "অন্য ফরম্যাট সাপোর্ট করা হয় না।",
            parse_mode='Markdown'
        )
        return

    # ── File size check ──
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text(
            "❌ *ফাইল সাইজ ৫MB-এর বেশি!*\n\n"
            "ছোট ফাইল পাঠাও।",
            parse_mode='Markdown'
        )
        return

    # ── Initial status message ──
    status = await update.message.reply_photo(
        photo=generate_pie_chart(0, 1),
        caption=(
            f"📥 *ফাইল পেয়েছি!*\n\n"
            f"📁 `{doc.file_name}`\n"
            f"⏳ প্রস্তুত হচ্ছে..."
        ),
        parse_mode='Markdown'
    )

    try:
        # ── Download & decode ──
        f   = await ctx.bot.get_file(doc.file_id)
        raw = await f.download_as_bytearray()

        srt_text = None
        for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
            try:
                srt_text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue

        if not srt_text:
            await status.edit_caption(
                "❌ ফাইল পড়তে পারছি না!\nUTF-8 encoding দিয়ে সেভ করো।",
                parse_mode='Markdown'
            )
            return

        blocks = parse_srt(srt_text)
        if not blocks:
            await status.edit_caption(
                "❌ SRT ফাইলে কোনো সাবটাইটেল পাওয়া যায়নি!",
                parse_mode='Markdown'
            )
            return

        total = len(blocks)

        # ── Show total count ──
        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(0, total),
            caption=(
                f"🎬 *অনুবাদ শুরু হচ্ছে...*\n\n"
                f"📁 `{doc.file_name}`\n"
                f"📊 মোট সাবটাইটেল: *{total}টি*\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏳ ০/{total} (০%)"
            ),
            parse_mode='Markdown'
        ))

        # ── Translate in batches ──
        BATCH      = 10
        translated = list(blocks)
        completed  = 0
        loop       = asyncio.get_event_loop()

        for i in range(0, total, BATCH):
            batch_blocks = blocks[i:i + BATCH]
            texts        = [b['text'] for b in batch_blocks]

            result = await loop.run_in_executor(
                executor, translate_batch_sync, texts
            )

            for j, tr in enumerate(result):
                if i + j < total:
                    translated[i + j]['text'] = tr

            completed = min(i + BATCH, total)
            pct       = completed / total * 100
            done_bar  = int(pct / 5)
            bar       = '█' * done_bar + '░' * (20 - done_bar)

            try:
                await status.edit_media(InputMediaPhoto(
                    media=generate_pie_chart(completed, total),
                    caption=(
                        f"🔄 *অনুবাদ চলছে...*\n\n"
                        f"📁 `{doc.file_name}`\n"
                        f"`[{bar}]` *{pct:.1f}%*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ সম্পন্ন: *{completed}/{total}*\n"
                        f"⏳ বাকি: *{total - completed}টি*"
                    ),
                    parse_mode='Markdown'
                ))
            except Exception as e:
                logger.warning(f"Edit error (ignored): {e}")

            await asyncio.sleep(0.8)

        # ── Build translated file ──
        out_srt   = build_srt(translated)
        out_name  = doc.file_name.replace('.srt', '_Bengali.srt')
        out_bytes = out_srt.encode('utf-8-sig')  # BOM for compatibility

        # ── Final chart ──
        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(total, total),
            caption=(
                f"✅ *অনুবাদ সম্পন্ন!*\n\n"
                f"📁 `{doc.file_name}`\n"
                f"🎉 *{total}টি* সাবটাইটেল অনুবাদ হয়েছে\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"⬇️ নিচের ফাইলটি ডাউনলোড করো"
            ),
            parse_mode='Markdown'
        ))

        # ── Send translated file ──
        await update.message.reply_document(
            document=io.BytesIO(out_bytes),
            filename=out_name,
            caption=(
                f"🎬 *অনুবাদিত সাবটাইটেল ফাইল*\n\n"
                f"📁 `{out_name}`\n"
                f"✅ *{total}টি* লাইন বাংলায় অনুবাদিত\n"
                f"⏱ Timing সম্পূর্ণ অক্ষুণ্ণ\n\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"_VLC / MX Player-এ ব্যবহার করো_ 🎥"
            ),
            parse_mode='Markdown'
        )

        logger.info(f"✅ Translated {total} subtitles for user {u.id}")

    except Exception as e:
        logger.error(f"Processing error for user {u.id}: {e}")
        try:
            await status.edit_caption(
                f"❌ *সমস্যা হয়েছে!*\n\n"
                f"`{str(e)[:200]}`\n\n"
                f"আবার চেষ্টা করো অথবা অন্য ফাইল দিয়ে দেখো।",
                parse_mode='Markdown'
            )
        except Exception:
            pass

# ══════════════════════════════════════════════
# 🚀 Main Entry Point
# ══════════════════════════════════════════════
def main():
    if not BOT_TOKEN:
        logger.error("❌ BOT_TOKEN not set!")
        return
    if not GROQ_API_KEY:
        logger.error("❌ GROQ_API_KEY not set!")
        return

    # Flask thread (web server)
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    logger.info("✅ Flask server started")

    # Self-ping thread
    ping_thread = threading.Thread(target=self_ping, daemon=True)
    ping_thread.start()
    logger.info("✅ Self-ping thread started")

    # Build Telegram bot
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    logger.info("🤖 Bot is now running!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True
    )


if __name__ == '__main__':
    main()
