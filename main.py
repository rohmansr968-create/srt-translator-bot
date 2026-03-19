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
        <title>SRT Translator Bot</title>
        <style>
            body { font-family: 'Segoe UI', sans-serif; background: #0f0e17;
                   color: #fffffe; display: flex; justify-content: center;
                   align-items: center; height: 100vh; margin: 0; flex-direction: column; }
            h1 { color: #ff8906; font-size: 2.5em; }
            p  { color: #a7a9be; font-size: 1.2em; }
            .dot { width: 12px; height: 12px; background: #00d4aa;
                   border-radius: 50%; display: inline-block;
                   animation: pulse 1.5s infinite; }
            @keyframes pulse { 0%,100%{opacity:1} 50%{opacity:0.3} }
        </style>
    </head>
    <body>
        <h1>🎬 SRT Translator Bot</h1>
        <p><span class="dot"></span>&nbsp; Bot is running and alive!</p>
        <p style="color:#f25f4c">Powered by Groq AI — Translating to Bengali</p>
    </body>
    </html>
    """

@flask_app.route('/ping')
def ping():
    return 'pong', 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 8080)))

# ══════════════════════════════════════════════
# 🔄 Self-Ping (Render ঘুমানো ঠেকাতে)
# ══════════════════════════════════════════════
def self_ping():
    while True:
        time.sleep(840)  # প্রতি ১৪ মিনিটে পিং
        if RENDER_URL:
            try:
                r = requests.get(f"{RENDER_URL}/ping", timeout=10)
                logger.info(f"✅ Self-ping: {r.status_code}")
            except Exception as e:
                logger.warning(f"⚠️ Self-ping failed: {e}")

# ══════════════════════════════════════════════
# 📄 SRT Parser
# ══════════════════════════════════════════════
def parse_srt(content: str) -> list:
    content = content.replace('\r\n', '\n').replace('\r', '\n')
    blocks, pattern = [], re.compile(
        r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n((?:.+\n?)+?)(?=\n\d+\n|\Z)',
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
    pct = (completed / total * 100) if total > 0 else 0
    remaining = total - completed

    fig, ax = plt.subplots(figsize=(7, 5.5))
    fig.patch.set_facecolor('#0f0e17')
    ax.set_facecolor('#0f0e17')

    if completed == 0:
        sizes, colors, labels = [100], ['#2d2d44'], ['Waiting...']
    elif completed >= total:
        sizes, colors, labels = [100], ['#00d4aa'], ['Completed ✓']
    else:
        sizes  = [completed, remaining]
        colors = ['#00d4aa', '#2d2d44']
        labels = [f'Done ({completed})', f'Left ({remaining})']

    explode = [0.05, 0] if len(sizes) == 2 else [0]

    wedges, texts, autotexts = ax.pie(
        sizes, explode=explode, colors=colors,
        autopct='%1.1f%%', startangle=90,
        pctdistance=0.65,
        wedgeprops={'linewidth': 2.5, 'edgecolor': '#0f0e17'},
        shadow=True
    )
    for at in autotexts:
        at.set_color('white'); at.set_fontsize(13); at.set_fontweight('bold')

    # Center percentage
    ax.text(0, 0, f'{pct:.1f}%', ha='center', va='center',
            fontsize=24, fontweight='bold', color='white')

    # Legend
    patches = [mpatches.Patch(color=colors[i], label=labels[i]) for i in range(len(labels))]
    ax.legend(handles=patches, loc='lower center', bbox_to_anchor=(0.5, -0.12),
              ncol=2, facecolor='#1e1e2e', edgecolor='#444466',
              labelcolor='white', fontsize=10)

    # Title & footer
    ax.set_title('Translation Progress', color='#ff8906', fontsize=15,
                 fontweight='bold', pad=18)
    fig.text(0.5, 0.01, f'Total: {total}  |  Done: {completed}  |  Left: {remaining}',
             ha='center', color='#a7a9be', fontsize=9)

    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf, format='png', dpi=110, bbox_inches='tight', facecolor='#0f0e17')
    buf.seek(0)
    plt.close()
    return buf

# ══════════════════════════════════════════════
# 🤖 Translation (Groq AI)
# ══════════════════════════════════════════════
SYSTEM_PROMPT = """তুমি একজন পেশাদার চলচ্চিত্র ও নাটকের সাবটাইটেল অনুবাদক।
তোমার কাজ হলো সাবটাইটেল স্বাভাবিক, কথ্য বাংলায় অনুবাদ করা।
শুধুমাত্র অনুবাদ দেবে। কোনো ব্যাখ্যা বা অতিরিক্ত কথা লিখবে না।"""

def translate_batch_sync(texts: list) -> list:
    combined = ' ||| '.join(texts)
    user_msg = f"""নিচের সাবটাইটেলগুলো বাংলায় অনুবাদ করো। প্রতিটি অনুবাদ ||| দিয়ে আলাদা করো।

নিয়ম:
- কথার ভাব বুঝে অনুবাদ করো, আক্ষরিক করো না
- স্বাভাবিক কথ্য বাংলা ব্যবহার করো
- আবেগ ও টোন বজায় রাখো
- শুধু অনুবাদ দাও, আর কিছু লিখবে না
- প্রতিটির মাঝে শুধু ||| রাখো

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
            result = resp.choices[0].message.content.strip()
            translated = [t.strip() for t in result.split('|||')]

            # Count fix
            while len(translated) < len(texts):
                translated.append(texts[len(translated)])
            return translated[:len(texts)]

        except Exception as e:
            err = str(e).lower()
            if 'rate_limit' in err:
                logger.warning(f"Rate limit hit, waiting 60s... (attempt {attempt+1})")
                time.sleep(60)
            else:
                logger.error(f"Translation error: {e}")
                return texts
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
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 চ্যানেলে যোগ দাও", url=f"https://t.me/{CHANNEL_USERNAME.lstrip('@')}")],
        [InlineKeyboardButton("✅ যোগ দিয়েছি — চেক করো", callback_data="chk")]
    ])

# ══════════════════════════════════════════════
# 🤖 Bot Handlers
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 *বট ব্যবহার করতে প্রথমে আমাদের চ্যানেলে যোগ দাও!*\n\n"
            f"চ্যানেল: `{CHANNEL_USERNAME}`\n\n"
            "যোগ দিয়ে নিচের বাটনে ক্লিক করো।",
            parse_mode='Markdown', reply_markup=not_joined_keyboard()
        )
        return

    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 কিভাবে ব্যবহার করব", callback_data="help"),
         InlineKeyboardButton("ℹ️ বট সম্পর্কে",        callback_data="about")],
        [InlineKeyboardButton("📊 স্ট্যাটাস", callback_data="status")]
    ])
    await update.message.reply_text(
        f"🎬 *SRT সাবটাইটেল অনুবাদক বটে স্বাগতম!*\n\n"
        f"হ্যালো *{u.first_name}* ভাই! 👋\n\n"
        f"আমি তোমার English সাবটাইটেল ফাইলকে সুন্দর, প্রাঞ্জল বাংলায় অনুবাদ করে দিই।\n\n"
        f"🎯 *কী করতে হবে?*\n"
        f"শুধু একটি `.srt` ফাইল পাঠাও — বাকি সব আমি করব!\n\n"
        f"⚡ _Powered by Groq AI (LLaMA 3.3 70B)_",
        parse_mode='Markdown', reply_markup=kb
    )

async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    d = q.data

    home_kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📖 কিভাবে ব্যবহার করব", callback_data="help"),
         InlineKeyboardButton("ℹ️ বট সম্পর্কে",        callback_data="about")],
        [InlineKeyboardButton("📊 স্ট্যাটাস", callback_data="status")]
    ])
    back_kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম", callback_data="home")]])

    if d == "chk":
        if await is_member(q.from_user.id, ctx.bot):
            await q.edit_message_text(
                "✅ *দারুণ! তুমি চ্যানেলে যোগ দিয়েছ!*\n\n"
                "এখন একটি `.srt` ফাইল পাঠাও অনুবাদ শুরু করতে 🚀",
                parse_mode='Markdown', reply_markup=home_kb
            )
        else:
            await q.edit_message_text(
                "❌ *এখনো যোগ দাওনি!*\n\nচ্যানেলে যোগ দাও, তারপর আবার চেক করো।",
                parse_mode='Markdown', reply_markup=not_joined_keyboard()
            )

    elif d == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার বিধি:*\n\n"
            "1️⃣ যেকোনো `.srt` সাবটাইটেল ফাইল এই চ্যাটে পাঠাও\n\n"
            "2️⃣ বট স্বয়ংক্রিয়ভাবে অনুবাদ শুরু করবে\n\n"
            "3️⃣ Live পাই চার্টে দেখতে পাবে কত % হলো\n\n"
            "4️⃣ অনুবাদ শেষে `.srt` ফাইল পাবে\n"
            "   ➡ Timing একদম অক্ষুণ্ণ থাকবে!\n\n"
            "⚠️ *সর্বোচ্চ ফাইল সাইজ:* 5MB\n"
            "✅ *সাপোর্টেড:* `.srt` format",
            parse_mode='Markdown', reply_markup=back_kb
        )

    elif d == "about":
        await q.edit_message_text(
            "ℹ️ *বট পরিচিতি:*\n\n"
            "🤖 *নাম:* SRT Translator Bot\n"
            "🧠 *AI Model:* LLaMA 3.3 70B (Groq)\n"
            "🌐 *Hosting:* Render (Free Tier)\n"
            "⚡ *Speed:* Ultra-fast (Groq inference)\n\n"
            "📌 *বিশেষত্ব:*\n"
            "• আক্ষরিক নয়, ভাবানুবাদ করে\n"
            "• কথ্য স্বাভাবিক বাংলা ব্যবহার করে\n"
            "• Timing একদম নির্ভুল রাখে\n"
            "• UTF-8 BOM সহ ফাইল দেয় (সব player-এ চলে)",
            parse_mode='Markdown', reply_markup=back_kb
        )

    elif d == "status":
        await q.edit_message_text(
            "📊 *বট স্ট্যাটাস:*\n\n"
            "🟢 Online ও সচল\n"
            "🤖 AI: সংযুক্ত\n"
            "🔄 Self-ping: সক্রিয় (প্রতি ১৪ মিনিট)\n\n"
            "_বট সর্বদা জেগে আছে!_",
            parse_mode='Markdown', reply_markup=back_kb
        )

    elif d == "home":
        await q.edit_message_text(
            "🎬 *SRT সাবটাইটেল অনুবাদক বট*\n\n"
            "একটি `.srt` ফাইল পাঠাও অনুবাদ শুরু করতে! 🚀",
            parse_mode='Markdown', reply_markup=home_kb
        )

async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    doc = update.message.document

    # Membership check
    if not await is_member(u.id, ctx.bot):
        await update.message.reply_text(
            "🔒 বট ব্যবহার করতে প্রথমে চ্যানেলে যোগ দাও!",
            reply_markup=not_joined_keyboard()
        )
        return

    # File type check
    if not doc.file_name.lower().endswith('.srt'):
        await update.message.reply_text(
            "❌ *শুধুমাত্র `.srt` ফাইল পাঠাও!*\n\n"
            "অন্য format সাপোর্ট করা হয় না।",
            parse_mode='Markdown'
        )
        return

    # File size check (5MB)
    if doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ ফাইল সাইজ ৫MB-এর বেশি হওয়া যাবে না!")
        return

    # Send initial status with pie chart
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
        # Download & decode
        f = await ctx.bot.get_file(doc.file_id)
        raw = await f.download_as_bytearray()

        srt_text = None
        for enc in ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252']:
            try:
                srt_text = raw.decode(enc); break
            except UnicodeDecodeError:
                continue

        if not srt_text:
            await status.edit_caption("❌ ফাইল পড়তে পারছি না। UTF-8 encoding দিয়ে সেভ করো।")
            return

        blocks = parse_srt(srt_text)
        if not blocks:
            await status.edit_caption("❌ SRT ফাইলে কোনো সাবটাইটেল পাওয়া যায়নি!")
            return

        total = len(blocks)

        # Show total count
        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(0, total),
            caption=(
                f"🎬 *অনুবাদ শুরু হচ্ছে...*\n\n"
                f"📁 `{doc.file_name}`\n"
                f"📊 মোট সাবটাইটেল: `{total}টি`\n"
                f"⏳ ০/{total} (০%)"
            ),
            parse_mode='Markdown'
        ))

        # Translate in batches
        BATCH = 10
        translated = list(blocks)
        completed  = 0
        loop = asyncio.get_event_loop()

        for i in range(0, total, BATCH):
            batch_blocks = blocks[i:i+BATCH]
            texts = [b['text'] for b in batch_blocks]

            result = await loop.run_in_executor(executor, translate_batch_sync, texts)

            for j, tr in enumerate(result):
                if i + j < total:
                    translated[i + j]['text'] = tr

            completed = min(i + BATCH, total)
            pct = completed / total * 100
            bar = '█' * int(pct / 5) + '░' * (20 - int(pct / 5))

            try:
                await status.edit_media(InputMediaPhoto(
                    media=generate_pie_chart(completed, total),
                    caption=(
                        f"🔄 *অনুবাদ চলছে...*\n\n"
                        f"📁 `{doc.file_name}`\n"
                        f"[{bar}] `{pct:.1f}%`\n"
                        f"✅ সম্পন্ন: `{completed}/{total}`\n"
                        f"⏳ বাকি: `{total - completed}টি`"
                    ),
                    parse_mode='Markdown'
                ))
            except Exception as e:
                logger.warning(f"Edit error: {e}")

            await asyncio.sleep(0.8)

        # Build & send translated file
        out_srt  = build_srt(translated)
        out_name = doc.file_name.replace('.srt', '_Bengali.srt')
        out_bytes = out_srt.encode('utf-8-sig')  # BOM for compatibility

        # Final chart
        await status.edit_media(InputMediaPhoto(
            media=generate_pie_chart(total, total),
            caption=(
                f"✅ *অনুবাদ সম্পন্ন!*\n\n"
                f"📁 `{doc.file_name}`\n"
                f"🎉 `{total}টি` সাবটাইটেল অনুবাদ হয়েছে\n"
                f"⬇️ নিচের ফাইলটি ডাউনলোড করো"
            ),
            parse_mode='Markdown'
        ))

        await update.message.reply_document(
            document=io.BytesIO(out_bytes),
            filename=out_name,
            caption=(
                f"🎬 *অনুবাদিত সাবটাইটেল ফাইল*\n\n"
                f"📁 `{out_name}`\n"
                f"✅ `{total}টি` লাইন বাংলায় অনুবাদ\n"
                f"⏱ Timing অক্ষুণ্ণ রাখা হয়েছে\n\n"
                f"_VLC / MX Player-এ ব্যবহার করো_"
            ),
            parse_mode='Markdown'
        )

    except Exception as e:
        logger.error(f"Processing error: {e}")
        try:
            await status.edit_caption(
                f"❌ *সমস্যা হয়েছে!*\n\n`{str(e)[:150]}`\n\nআবার চেষ্টা করো।",
                parse_mode='Markdown'
            )
        except:
            pass

# ══════════════════════════════════════════════
# 🚀 Main
# ══════════════════════════════════════════════
def main():
    # Flask thread
    threading.Thread(target=run_flask, daemon=True).start()
    # Self-ping thread
    threading.Thread(target=self_ping, daemon=True).start()

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))

    logger.info("🤖 Bot started successfully!")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == '__main__':
    main()
