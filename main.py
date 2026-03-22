#!/usr/bin/env python3
"""
🎬 SRT Subtitle Translator — Ultra Version
বাংলা সাবটাইটেল অনুবাদক | Powered by Groq AI + Whisper
Features: Multi-lang · Multi-format · YouTube · Tokens · Referral · Admin · Tools
Python 3.11 | PTB 20.7
"""

import os, re, io, time, asyncio, logging, threading, functools
import requests, tempfile, sqlite3, hashlib
from datetime import datetime, timedelta
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

try:
    import yt_dlp
    YT_AVAILABLE = True
except ImportError:
    YT_AVAILABLE = False
    logging.warning("yt-dlp not installed")

# ══════════════════════════════════════════════
# ⚙️  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN        = os.environ.get('BOT_TOKEN', '')
GROQ_API_KEY     = os.environ.get('GROQ_API_KEY', '')
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', '@your_channel')
RENDER_URL       = os.environ.get('RENDER_URL', '')
SUBDL_API_KEY    = os.environ.get('SUBDL_API_KEY', '')
ADMIN_IDS_STR    = os.environ.get('ADMIN_IDS', '')   # comma-separated user IDs

ADMIN_IDS = set()
for _a in ADMIN_IDS_STR.split(','):
    try: ADMIN_IDS.add(int(_a.strip()))
    except: pass

# ── Token config ──
WELCOME_TOKENS            = 50
DAILY_TOKENS              = 10
REFERRAL_TOKENS_REFERRER  = 25
REFERRAL_TOKENS_REFEREE   = 15
PREMIUM_THRESHOLD         = 100   # tokens needed for premium badge

# ── Supported source languages ──
SRC_LANGS = {
    'auto': '🔍 Auto-detect',
    'en':   '🇺🇸 English',
    'hi':   '🇮🇳 Hindi',
    'ko':   '🇰🇷 Korean',
    'ja':   '🇯🇵 Japanese',
    'ar':   '🇸🇦 Arabic',
    'fr':   '🇫🇷 French',
    'de':   '🇩🇪 German',
    'zh':   '🇨🇳 Chinese',
    'es':   '🇪🇸 Spanish',
    'ru':   '🇷🇺 Russian',
}

# ── Target languages ──
DST_LANGS = {
    'bn': '🇧🇩 বাংলা',
    'en': '🇺🇸 English',
    'hi': '🇮🇳 Hindi',
}

AUDIO_EXTS   = ('.mp3', '.mp4', '.wav', '.m4a', '.ogg', '.webm', '.oga', '.flac')
MAX_AUDIO_MB  = 25 * 1024 * 1024
DB_PATH       = '/tmp/subtitle_bot.db'

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s',
                    level=logging.INFO)
logger = logging.getLogger(__name__)

groq_client   = Groq(api_key=GROQ_API_KEY)
executor      = ThreadPoolExecutor(max_workers=4)
active_tasks  = {}
cancel_events = {}
chat_mode     = {}
chat_history  = {}
pending_audio = {}
user_state    = {}   # state machine for multi-step actions

# ══════════════════════════════════════════════
# 🗄️  DATABASE
# ══════════════════════════════════════════════
def db():
    c = sqlite3.connect(DB_PATH, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def init_db():
    con = db()
    con.executescript('''
    CREATE TABLE IF NOT EXISTS users (
        uid INTEGER PRIMARY KEY,
        username TEXT, first_name TEXT,
        tokens INTEGER DEFAULT 0,
        total_translations INTEGER DEFAULT 0,
        total_lines INTEGER DEFAULT 0,
        join_date TEXT,
        referral_code TEXT UNIQUE,
        referred_by INTEGER,
        last_daily TEXT,
        is_banned INTEGER DEFAULT 0,
        from_lang TEXT DEFAULT 'auto',
        to_lang TEXT DEFAULT 'bn'
    );
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER, file_name TEXT,
        line_count INTEGER, from_lang TEXT,
        to_lang TEXT, ts TEXT
    );
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer INTEGER, referee INTEGER, ts TEXT
    );
    ''')
    con.commit(); con.close()
    logger.info("✅ DB ready")

def _ref_code(uid: int) -> str:
    return "R" + hashlib.md5(f"sub{uid}bot".encode()).hexdigest()[:7].upper()

def get_user(uid: int, username=None, first_name=None, ref_code_used=None) -> dict:
    con = db(); c = con.cursor()
    c.execute("SELECT * FROM users WHERE uid=?", (uid,))
    u = c.fetchone()
    if not u:
        tokens = WELCOME_TOKENS
        referred_by = None
        if ref_code_used:
            c.execute("SELECT uid FROM users WHERE referral_code=?", (ref_code_used,))
            r = c.fetchone()
            if r and r['uid'] != uid:
                referred_by = r['uid']
                tokens += REFERRAL_TOKENS_REFEREE
                con.execute("UPDATE users SET tokens=tokens+? WHERE uid=?",
                            (REFERRAL_TOKENS_REFERRER, referred_by))
                con.execute("INSERT INTO referrals VALUES(NULL,?,?,?)",
                            (referred_by, uid, datetime.now().isoformat()))
        con.execute('''INSERT INTO users
            (uid,username,first_name,tokens,join_date,referral_code,referred_by)
            VALUES(?,?,?,?,?,?,?)''',
            (uid, username, first_name, tokens,
             datetime.now().isoformat(), _ref_code(uid), referred_by))
        con.commit()
        c.execute("SELECT * FROM users WHERE uid=?", (uid,))
        u = c.fetchone()
    else:
        con.execute("UPDATE users SET username=?,first_name=? WHERE uid=?",
                    (username, first_name, uid)); con.commit()
    result = dict(u); con.close()
    return result

def add_tokens(uid, n):
    con = db(); con.execute("UPDATE users SET tokens=tokens+? WHERE uid=?", (n, uid))
    con.commit(); con.close()

def claim_daily(uid) -> tuple:
    """(success, hours_left)"""
    con = db(); c = con.cursor()
    c.execute("SELECT last_daily FROM users WHERE uid=?", (uid,))
    row = c.fetchone()
    if row and row['last_daily']:
        last = datetime.fromisoformat(row['last_daily'])
        diff = datetime.now() - last
        if diff < timedelta(hours=24):
            left = int((timedelta(hours=24) - diff).total_seconds() / 3600)
            con.close(); return False, left
    con.execute("UPDATE users SET tokens=tokens+?,last_daily=? WHERE uid=?",
                (DAILY_TOKENS, datetime.now().isoformat(), uid))
    con.commit(); con.close()
    return True, 0

def log_history(uid, fname, lines, fl, tl):
    con = db()
    con.execute("INSERT INTO history VALUES(NULL,?,?,?,?,?,?)",
                (uid, fname, lines, fl, tl, datetime.now().isoformat()))
    con.execute("UPDATE users SET total_translations=total_translations+1,"
                "total_lines=total_lines+? WHERE uid=?", (lines, uid))
    con.commit(); con.close()

def get_history(uid, limit=10):
    con = db(); c = con.cursor()
    c.execute("SELECT * FROM history WHERE uid=? ORDER BY ts DESC LIMIT ?", (uid, limit))
    rows = [dict(r) for r in c.fetchall()]; con.close()
    return rows

def get_stats():
    con = db(); c = con.cursor()
    c.execute("SELECT COUNT(*) t FROM users"); tu = c.fetchone()['t']
    c.execute("SELECT COUNT(*) t FROM users WHERE is_banned=0"); au = c.fetchone()['t']
    c.execute("SELECT COALESCE(SUM(total_translations),0) t FROM users"); tt = c.fetchone()['t']
    c.execute("SELECT COALESCE(SUM(total_lines),0) t FROM users"); tl = c.fetchone()['t']
    con.close()
    return {'users': tu, 'active': au, 'translations': tt, 'lines': tl}

def all_uids():
    con = db(); c = con.cursor()
    c.execute("SELECT uid FROM users WHERE is_banned=0")
    ids = [r['uid'] for r in c.fetchall()]; con.close()
    return ids

def ban_user(uid, banned):
    con = db()
    con.execute("UPDATE users SET is_banned=? WHERE uid=?", (1 if banned else 0, uid))
    con.commit(); con.close()

def is_banned_user(uid):
    con = db(); c = con.cursor()
    c.execute("SELECT is_banned FROM users WHERE uid=?", (uid,))
    r = c.fetchone(); con.close()
    return bool(r and r['is_banned'])

def set_lang_pref(uid, from_lang=None, to_lang=None):
    con = db()
    if from_lang: con.execute("UPDATE users SET from_lang=? WHERE uid=?", (from_lang, uid))
    if to_lang:   con.execute("UPDATE users SET to_lang=? WHERE uid=?",   (to_lang,   uid))
    con.commit(); con.close()

def ref_count(uid):
    con = db(); c = con.cursor()
    c.execute("SELECT COUNT(*) t FROM referrals WHERE referrer=?", (uid,))
    n = c.fetchone()['t']; con.close(); return n

# ══════════════════════════════════════════════
# 🌐  FLASK
# ══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
def web_home():
    return """<!DOCTYPE html><html><head><title>SRT Ultra Bot</title>
<meta charset="UTF-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;
     background:linear-gradient(135deg,#0f0e17,#1a1a2e);
     color:#fff;display:flex;justify-content:center;
     align-items:center;min-height:100vh;flex-direction:column;gap:16px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,137,6,.35);
      border-radius:20px;padding:36px 56px;text-align:center}
h1{color:#ff8906;font-size:2.3em;margin-bottom:8px}
.dot{width:13px;height:13px;background:#00d4aa;border-radius:50%;
     display:inline-block;animation:p 1.5s infinite;box-shadow:0 0 8px #00d4aa}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
p{color:#a7a9be;font-size:1em;line-height:1.9}
.b{display:inline-block;background:rgba(255,137,6,.13);
   border:1px solid #ff8906;color:#ff8906;
   padding:4px 12px;border-radius:16px;font-size:.85em;margin:3px}
</style></head><body>
<div class="card">
  <h1>🎬 SRT Ultra Bot</h1>
  <p><span class="dot"></span>&nbsp;
  <span style="color:#00d4aa;font-weight:700">Bot is Live!</span></p>
  <p>Multi-lang · Multi-format · YouTube · Tokens · Admin</p><br>
  <div>
    <span class="b">🤖 Groq AI</span><span class="b">🎙 Whisper</span>
    <span class="b">🌐 Multi-lang</span><span class="b">▶️ YouTube</span>
    <span class="b">🪙 Tokens</span><span class="b">👑 Admin</span>
  </div>
</div></body></html>""", 200

@flask_app.route('/ping')
def ping(): return 'pong', 200

def run_flask():
    flask_app.run(host='0.0.0.0',
                  port=int(os.environ.get('PORT', 10000)),
                  use_reloader=False)

def self_ping():
    time.sleep(30)
    while True:
        time.sleep(840)
        if RENDER_URL:
            try: requests.get(f"{RENDER_URL}/ping", timeout=15)
            except: pass

# ══════════════════════════════════════════════
# 📄  SUBTITLE PARSERS
# ══════════════════════════════════════════════
def parse_srt(content: str) -> list:
    content = content.replace('\r\n','\n').replace('\r','\n')
    blocks, pat = [], re.compile(
        r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n'
        r'((?:.+\n?)+?)(?=\n\d+\n|\Z)', re.MULTILINE)
    for m in pat.finditer(content.strip() + '\n\n'):
        txt = m.group(4).strip()
        if txt:
            blocks.append({'index': m.group(1), 'start': m.group(2),
                           'end': m.group(3), 'text': txt})
    return blocks

def parse_vtt(content: str) -> list:
    """WebVTT → SRT blocks"""
    content = content.replace('\r\n','\n').replace('\r','\n')
    blocks, idx = [], 1
    # Remove WEBVTT header
    lines = content.split('\n')
    i = 0
    while i < len(lines) and not '-->' in lines[i]:
        i += 1
    pat = re.compile(
        r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})')
    while i < len(lines):
        line = lines[i].strip()
        m = pat.search(line)
        if m:
            start = m.group(1).replace('.', ',')
            end   = m.group(2).replace('.', ',')
            i += 1
            text_lines = []
            while i < len(lines) and lines[i].strip():
                text_lines.append(lines[i].strip()); i += 1
            txt = '\n'.join(text_lines)
            # Remove VTT tags
            txt = re.sub(r'<[^>]+>', '', txt).strip()
            if txt:
                blocks.append({'index': str(idx), 'start': start,
                               'end': end, 'text': txt})
                idx += 1
        else:
            i += 1
    return blocks

def parse_ass(content: str) -> list:
    """ASS/SSA → SRT blocks"""
    blocks, idx = [], 1
    # Format: Dialogue: Layer,Start,End,Style,Name,ML,MR,MV,Effect,Text
    pat = re.compile(
        r'^Dialogue:.*?'
        r'(\d:\d{2}:\d{2}\.\d{2}),(\d:\d{2}:\d{2}\.\d{2}),'
        r'[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)',
        re.MULTILINE)
    def ass_time(t):  # 0:00:01.23 → 00:00:01,230
        parts = t.split(':')
        h = parts[0].zfill(2)
        m = parts[1].zfill(2)
        s_ms = parts[2].replace('.', ',')
        s, ms = s_ms.split(',')
        return f"{h}:{m}:{s.zfill(2)},{ms.ljust(3,'0')}"
    for match in pat.finditer(content):
        start = ass_time(match.group(1))
        end   = ass_time(match.group(2))
        text  = match.group(3).strip()
        # Remove ASS override tags
        text = re.sub(r'\{[^}]*\}', '', text).replace('\\N','\n').strip()
        if text:
            blocks.append({'index': str(idx), 'start': start,
                           'end': end, 'text': text})
            idx += 1
    return blocks

def parse_auto(content: str, filename: str) -> list:
    fn = filename.lower()
    if fn.endswith('.vtt'):  return parse_vtt(content)
    if fn.endswith(('.ass','.ssa')): return parse_ass(content)
    return parse_srt(content)

def build_srt(blocks: list) -> str:
    return '\n\n'.join(
        f"{b['index']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in blocks) + '\n'

def fix_timing(blocks: list, offset_sec: float) -> list:
    """Shift all timestamps by offset_sec (positive or negative)"""
    def shift(ts):
        # Parse hh:mm:ss,mmm
        h,m,s_ms = ts.split(':')
        s,ms = s_ms.split(',')
        total_ms = (int(h)*3600 + int(m)*60 + int(s))*1000 + int(ms)
        total_ms += int(offset_sec * 1000)
        total_ms = max(0, total_ms)
        h2,rem = divmod(total_ms, 3600000)
        m2,rem = divmod(rem, 60000)
        s2,ms2 = divmod(rem, 1000)
        return f"{h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}"
    result = []
    for b in blocks:
        nb = dict(b)
        nb['start'] = shift(b['start'])
        nb['end']   = shift(b['end'])
        result.append(nb)
    return result

def merge_subtitles(blocks1: list, blocks2: list) -> list:
    """Merge two subtitle lists sorted by start time"""
    all_blocks = blocks1 + blocks2
    def time_to_ms(ts):
        h,m,s_ms = ts.split(':')
        s,ms = s_ms.split(',')
        return (int(h)*3600+int(m)*60+int(s))*1000+int(ms)
    all_blocks.sort(key=lambda b: time_to_ms(b['start']))
    for i, b in enumerate(all_blocks, 1):
        b['index'] = str(i)
    return all_blocks

# ══════════════════════════════════════════════
# 📊  PIE CHART
# ══════════════════════════════════════════════
def pie_chart(completed: int, total: int) -> io.BytesIO:
    pct = (completed/total*100) if total > 0 else 0
    rem = max(total-completed, 0)
    fig, ax = plt.subplots(figsize=(7,5.5))
    fig.patch.set_facecolor('#0f0e17'); ax.set_facecolor('#0f0e17')

    if completed == 0:
        sizes,colors,labels = [100],['#2d2d44'],['Waiting...']
    elif completed >= total:
        sizes,colors,labels = [100],['#00d4aa'],['Completed ✓']
    else:
        sizes=[completed,rem]; colors=['#00d4aa','#2d2d44']
        labels=[f'Done ({completed})',f'Left ({rem})']

    explode = ([0.05,0] if len(sizes)==2 else [0])
    _,_,ats = ax.pie(sizes, explode=explode, colors=colors, autopct='%1.1f%%',
                     startangle=90, pctdistance=0.65,
                     wedgeprops={'linewidth':2.5,'edgecolor':'#0f0e17'}, shadow=True)
    for at in ats:
        at.set_color('white'); at.set_fontsize(13); at.set_fontweight('bold')
    ax.text(0,0,f'{pct:.1f}%',ha='center',va='center',
            fontsize=26,fontweight='bold',color='white')
    patches=[mpatches.Patch(color=colors[i],label=labels[i]) for i in range(len(labels))]
    ax.legend(handles=patches,loc='lower center',bbox_to_anchor=(.5,-.13),
              ncol=2,facecolor='#1e1e2e',edgecolor='#444466',labelcolor='white',fontsize=10)
    ax.set_title('Translation Progress',color='#ff8906',fontsize=15,fontweight='bold',pad=18)
    fig.text(.5,.01,f'Total:{total}  Done:{completed}  Left:{rem}',
             ha='center',color='#a7a9be',fontsize=9)
    plt.tight_layout()
    buf = io.BytesIO()
    plt.savefig(buf,format='png',dpi=110,bbox_inches='tight',facecolor='#0f0e17')
    buf.seek(0); plt.close(fig)
    return buf

# ══════════════════════════════════════════════
# 🤖  TRANSLATION
# ══════════════════════════════════════════════
def _is_quota(e): 
    return any(k in str(e).lower() for k in ['quota','limit exceeded','402','billing'])
def _is_rate(e):  
    return 'rate_limit' in str(e).lower() or '429' in str(e)

def _translate_prompt(from_lang: str, to_lang: str) -> str:
    src = SRC_LANGS.get(from_lang, from_lang) if from_lang != 'auto' else 'any language'
    dst = DST_LANGS.get(to_lang, to_lang)
    return (
        f"You are a professional subtitle translator.\n"
        f"Translate {src} subtitles to {dst}.\n"
        f"Rules:\n"
        f"- Translate meaning, NOT word-by-word\n"
        f"- Use natural conversational style\n"
        f"- Keep emotion and tone\n"
        f"- Return ONLY the translation, nothing else"
    )

def translate_one(text, from_lang='auto', to_lang='bn', cancel_event=None):
    for _ in range(3):
        if cancel_event and cancel_event.is_set(): return text
        try:
            r = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role":"system","content":_translate_prompt(from_lang,to_lang)},
                    {"role":"user","content":f"Translate:\n{text}"}
                ],
                temperature=0.15, max_tokens=256)
            return r.choices[0].message.content.strip()
        except Exception as e:
            if _is_quota(e): raise Exception("QUOTA_EXCEEDED")
            elif _is_rate(e):
                for _ in range(60):
                    if cancel_event and cancel_event.is_set(): return text
                    time.sleep(1)
            else: time.sleep(3)
    return text

def translate_batch(texts, from_lang='auto', to_lang='bn', cancel_event=None):
    if cancel_event and cancel_event.is_set(): return texts
    numbered = '\n'.join(f"[{i+1}] {t}" for i,t in enumerate(texts))
    msg = (f"Translate the following {len(texts)} subtitle lines.\n"
           f"Keep the same number prefix [1], [2]...\n"
           f"Return ONLY translations.\n\n{numbered}\n\nTranslation:")
    translated = [None]*len(texts)
    for attempt in range(3):
        if cancel_event and cancel_event.is_set(): return texts
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role":"system","content":_translate_prompt(from_lang,to_lang)},
                    {"role":"user","content":msg}
                ],
                temperature=0.15, max_tokens=3000)
            raw = resp.choices[0].message.content.strip()
            for m in re.finditer(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|\Z)', raw, re.DOTALL):
                idx = int(m.group(1))-1
                val = m.group(2).strip()
                if 0 <= idx < len(texts) and val:
                    translated[idx] = val
            break
        except Exception as e:
            if _is_quota(e): raise Exception("QUOTA_EXCEEDED")
            elif _is_rate(e):
                for _ in range(60):
                    if cancel_event and cancel_event.is_set(): return texts
                    time.sleep(1)
            else: time.sleep(5)
    for i,v in enumerate(translated):
        if v is None:
            if cancel_event and cancel_event.is_set(): return texts
            translated[i] = translate_one(texts[i], from_lang, to_lang, cancel_event)
    return translated

# ══════════════════════════════════════════════
# 🎙  AUDIO TRANSCRIPTION
# ══════════════════════════════════════════════
def transcribe_audio(audio_bytes, filename, language='en'):
    suffix = os.path.splitext(filename)[-1] or '.mp3'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes); tmp_path = tmp.name
    try:
        with open(tmp_path,'rb') as f:
            params = dict(file=(filename,f,'audio/mpeg'),
                          model="whisper-large-v3-turbo",
                          response_format="text", temperature=0.0)
            if language != 'auto': params['language'] = language
            result = groq_client.audio.transcriptions.create(**params)
        return result.strip() if isinstance(result,str) else result.text.strip()
    except Exception as e:
        if _is_quota(e): raise Exception("QUOTA_EXCEEDED")
        raise e
    finally:
        os.unlink(tmp_path)

def translate_plain_text(text, to_lang='bn'):
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role":"system",
                 "content":(f"You are a professional translator. "
                            f"Translate the given text to {DST_LANGS.get(to_lang,'Bengali')}. "
                            f"Translate meaning naturally. Return ONLY the translation.")},
                {"role":"user","content":f"Translate:\n{text}"}
            ],
            temperature=0.15, max_tokens=4096)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        if _is_quota(e): raise Exception("QUOTA_EXCEEDED")
        raise e

# ══════════════════════════════════════════════
# ▶️  YOUTUBE SUBTITLE
# ══════════════════════════════════════════════
def download_yt_subtitle(url: str, lang='en') -> tuple:
    """Returns (srt_content, title) or (None, None)"""
    if not YT_AVAILABLE:
        return None, None
    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = {
            'writesubtitles': True,
            'writeautomaticsub': True,
            'subtitleslangs': [lang, 'en'],
            'skip_download': True,
            'outtmpl': os.path.join(tmpdir, '%(title)s'),
            'subtitlesformat': 'vtt/srt',
            'quiet': True,
            'no_warnings': True,
        }
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title', 'video')[:50]
            for f in os.listdir(tmpdir):
                if f.endswith(('.srt','.vtt')):
                    with open(os.path.join(tmpdir,f),'r',encoding='utf-8',errors='ignore') as sf:
                        return sf.read(), title
        except Exception as e:
            logger.error(f"YT error: {e}")
    return None, None

# ══════════════════════════════════════════════
# 🔍  SUBDL
# ══════════════════════════════════════════════
def subdl_search(query):
    try:
        r = requests.get("https://api.subdl.com/api/v1/subtitles",
                         params={"api_key":SUBDL_API_KEY,"film_name":query,
                                 "languages":"EN","subs_per_page":8}, timeout=15)
        if r.status_code != 200: return []
        return r.json().get("subtitles",[])[:8]
    except: return []

def subdl_dl(url_path):
    try:
        r = requests.get(f"https://dl.subdl.com{url_path}", timeout=30)
        return r.content if r.status_code==200 else None
    except: return None

# ══════════════════════════════════════════════
# 💬  AI CHAT
# ══════════════════════════════════════════════
CHAT_SYS = ("তুমি একটি বন্ধুত্বপূর্ণ AI assistant। বাংলায় কথা বলো। "
            "স্বাভাবিক ভাষায় উত্তর দাও।")

def ai_chat(uid, text):
    if uid not in chat_history: chat_history[uid] = []
    chat_history[uid].append({"role":"user","content":text})
    if len(chat_history[uid]) > 20:
        chat_history[uid] = chat_history[uid][-20:]
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":CHAT_SYS}]+chat_history[uid],
            temperature=0.7, max_tokens=1024)
        reply = resp.choices[0].message.content.strip()
        chat_history[uid].append({"role":"assistant","content":reply})
        return reply
    except Exception as e:
        if _is_quota(e): return "⚠️ API limit শেষ। ২৪ঘণ্টা পরে চেষ্টা করো।"
        return "❌ সমস্যা হয়েছে।"

# ══════════════════════════════════════════════
# 🔒  CHANNEL + BAN CHECK
# ══════════════════════════════════════════════
async def is_member(uid, bot):
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return m.status in ['member','administrator','creator']
    except: return False

NOT_JOINED = ("🔒 *চ্যানেল Membership নেই!*\n\n"
              "বট ব্যবহার করতে চ্যানেলে যোগ দাও।\n"
              "Leave নিলে সাথে সাথে access বন্ধ।")
BANNED_MSG  = "🚫 *তোমাকে এই বট থেকে ban করা হয়েছে।*"

async def access_ok(uid, bot, reply_fn) -> bool:
    if is_banned_user(uid):
        await reply_fn(BANNED_MSG, parse_mode='Markdown'); return False
    if not await is_member(uid, bot):
        ch = CHANNEL_USERNAME.lstrip('@')
        await reply_fn(NOT_JOINED, parse_mode='Markdown',
                       reply_markup=InlineKeyboardMarkup([
                           [InlineKeyboardButton("📢 চ্যানেলে যোগ দাও",
                                                 url=f"https://t.me/{ch}")],
                           [InlineKeyboardButton("✅ চেক করো", callback_data="chk")]
                       ]))
        return False
    return True

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
         InlineKeyboardButton("👤 আমার প্রোফাইল", callback_data="profile")],
        [InlineKeyboardButton("🌐 ভাষা সেটিং",    callback_data="lang_menu"),
         InlineKeyboardButton("📊 স্ট্যাটাস",       callback_data="status")],
        [InlineKeyboardButton("🔍 Subtitle খোঁজো", callback_data="search"),
         InlineKeyboardButton("▶️ YouTube",         callback_data="yt_info")],
        [InlineKeyboardButton("🛠 Subtitle Tools",  callback_data="tools_menu")],
        [InlineKeyboardButton("🎙 Audio Transcription", callback_data="audio_info")],
        [InlineKeyboardButton("💬 AI চ্যাট",        callback_data="chat_start"),
         InlineKeyboardButton("🪙 Daily Token",     callback_data="daily")]
    ])

def kb_back(): return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 হোম",callback_data="home")]])
def kb_cancel(uid): return InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল করো",callback_data=f"cancel_{uid}")]])
def kb_chat():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 মুছো",callback_data="chat_clear")],
        [InlineKeyboardButton("🔙 বন্ধ",callback_data="chat_stop")]
    ])
def kb_quota():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑 Groq Console",url="https://console.groq.com")],
        [InlineKeyboardButton("🔙 হোম",callback_data="home")]
    ])
def kb_audio_options(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🇧🇩 বাংলায় Transcription",    callback_data=f"tr_bn_{uid}")],
        [InlineKeyboardButton("🇺🇸 English Transcription",    callback_data=f"tr_en_{uid}")],
        [InlineKeyboardButton("🔄 Transcription + অনুবাদ",    callback_data=f"tr_translate_{uid}")],
        [InlineKeyboardButton("❌ বাতিল",                      callback_data="home")]
    ])
def kb_src_lang():
    rows = []
    items = list(SRC_LANGS.items())
    for i in range(0, len(items), 2):
        row = []
        for code, name in items[i:i+2]:
            row.append(InlineKeyboardButton(name, callback_data=f"set_src_{code}"))
        rows.append(row)
    rows.append([InlineKeyboardButton("🔙 হোম", callback_data="home")])
    return InlineKeyboardMarkup(rows)
def kb_dst_lang():
    rows = [[InlineKeyboardButton(name, callback_data=f"set_dst_{code}")]
            for code,name in DST_LANGS.items()]
    rows.append([InlineKeyboardButton("🔙 হোম", callback_data="home")])
    return InlineKeyboardMarkup(rows)
def kb_tools():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("⏱ Timing Fix (সময় ঠিক করো)", callback_data="tool_timing")],
        [InlineKeyboardButton("🔀 Merge (দুটো ফাইল একসাথে)", callback_data="tool_merge")],
        [InlineKeyboardButton("🔙 হোম", callback_data="home")]
    ])
def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 Statistics",   callback_data="adm_stats"),
         InlineKeyboardButton("📢 Broadcast",    callback_data="adm_broadcast")],
        [InlineKeyboardButton("👤 User Lookup",  callback_data="adm_lookup"),
         InlineKeyboardButton("🚫 Ban User",     callback_data="adm_ban")],
        [InlineKeyboardButton("✅ Unban User",   callback_data="adm_unban"),
         InlineKeyboardButton("🪙 Give Tokens",  callback_data="adm_tokens")],
        [InlineKeyboardButton("🔙 হোম",          callback_data="home")]
    ])

# ══════════════════════════════════════════════
# HELPER — quota error message
# ══════════════════════════════════════════════
QUOTA_MSG = (
    "⚠️ *Groq API Limit শেষ!*\n\n"
    "তোমার API key-এর দৈনিক limit শেষ।\n\n"
    "🔧 *সমাধান:*\n"
    "1️⃣ [console.groq.com](https://console.groq.com) যাও\n"
    "2️⃣ নতুন API key তৈরি করো\n"
    "3️⃣ Render-এ `GROQ_API_KEY` update করো\n\n"
    "⏰ অথবা ২৪ ঘণ্টা পরে চেষ্টা করো।"
)

# ══════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    args = ctx.args or []
    ref_code_used = args[0] if args else None

    if not await access_ok(u.id, ctx.bot, update.message.reply_text):
        return

    user = get_user(u.id, u.username, u.first_name, ref_code_used)
    chat_mode[u.id] = False

    is_new       = (datetime.fromisoformat(user['join_date']).date() == datetime.now().date()
                    and user['total_translations'] == 0)
    premium_badge = " 👑" if user['tokens'] >= PREMIUM_THRESHOLD else ""

    welcome_extra = ""
    if is_new:
        welcome_extra = (f"\n\n🎁 *Welcome Bonus:* তোমার account-এ "
                         f"*{WELCOME_TOKENS} tokens* যোগ হয়েছে!")
        if user['referred_by']:
            welcome_extra += f"\n🤝 *Referral Bonus:* আরো *{REFERRAL_TOKENS_REFEREE} tokens* পেয়েছ!"

    await update.message.reply_text(
        f"🎬 *Subtitle BD Bot-এ স্বাগতম!{premium_badge}*\n\n"
        f"হ্যালো *{u.first_name}* ভাই! 👋"
        f"{welcome_extra}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"✨ *আমি যা করতে পারি:*\n\n"
        f"🔄 SRT/VTT/ASS → যেকোনো ভাষায় অনুবাদ\n"
        f"▶️ YouTube link → Subtitle → বাংলা অনুবাদ\n"
        f"🎙 Audio/Voice → Transcription\n"
        f"🛠 Subtitle timing fix, merge\n"
        f"🔍 Movie subtitle খোঁজা ও ডাউনলোড\n"
        f"💬 AI-এর সাথে বাংলায় চ্যাট\n"
        f"🪙 Token: *{user['tokens']}* | "
        f"Referral: `/referral`\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"📌 ফাইল পাঠাও বা বাটন ব্যবহার করো 👇",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# /profile
# ══════════════════════════════════════════════
async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await access_ok(u.id, ctx.bot, update.message.reply_text): return
    user = get_user(u.id)
    hist = get_history(u.id, 5)
    refs = ref_count(u.id)
    badge = "👑 Premium" if user['tokens'] >= PREMIUM_THRESHOLD else "🆓 Free"
    hist_text = ""
    if hist:
        hist_text = "\n\n📋 *শেষ ৫টি অনুবাদ:*\n"
        for h in hist:
            ts = h['ts'][:10]
            hist_text += f"• `{h['file_name'][:25]}` — {h['line_count']} লাইন ({ts})\n"
    await update.message.reply_text(
        f"👤 *তোমার প্রোফাইল*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 *নাম:* {user['first_name']}\n"
        f"🆔 *ID:* `{u.id}`\n"
        f"🎖 *Status:* {badge}\n"
        f"🪙 *Tokens:* `{user['tokens']}`\n\n"
        f"📊 *পরিসংখ্যান:*\n"
        f"• মোট অনুবাদ: `{user['total_translations']}`\n"
        f"• মোট লাইন: `{user['total_lines']}`\n"
        f"• Referral: `{refs}` জন\n\n"
        f"🌐 *ভাষা সেটিং:*\n"
        f"• Source: `{SRC_LANGS.get(user['from_lang'],'Auto')}`\n"
        f"• Target: `{DST_LANGS.get(user['to_lang'],'Bengali')}`\n"
        f"• Join: `{user['join_date'][:10]}`"
        f"{hist_text}",
        parse_mode='Markdown', reply_markup=kb_back())

# ══════════════════════════════════════════════
# /referral
# ══════════════════════════════════════════════
async def cmd_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await access_ok(u.id, ctx.bot, update.message.reply_text): return
    user = get_user(u.id)
    refs = ref_count(u.id)
    bot_info = await ctx.bot.get_me()
    ref_link = f"https://t.me/{bot_info.username}?start={user['referral_code']}"
    await update.message.reply_text(
        f"🤝 *Referral System*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"তোমার Referral link:\n`{ref_link}`\n\n"
        f"📌 *কীভাবে কাজ করে:*\n"
        f"• কেউ তোমার link দিয়ে join করলে\n"
        f"• তুমি পাবে: *{REFERRAL_TOKENS_REFERRER} tokens* 🎁\n"
        f"• সে পাবে: *{WELCOME_TOKENS + REFERRAL_TOKENS_REFEREE} tokens* 🎁\n\n"
        f"📊 এখন পর্যন্ত: *{refs} জন* refer করেছ\n"
        f"💰 মোট উপার্জন: *{refs * REFERRAL_TOKENS_REFERRER} tokens*",
        parse_mode='Markdown', reply_markup=kb_back())

# ══════════════════════════════════════════════
# /daily
# ══════════════════════════════════════════════
async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await access_ok(u.id, ctx.bot, update.message.reply_text): return
    success, hours_left = claim_daily(u.id)
    if success:
        user = get_user(u.id)
        await update.message.reply_text(
            f"🎁 *Daily Token পেয়েছ!*\n\n"
            f"✅ *+{DAILY_TOKENS} tokens* তোমার account-এ যোগ হয়েছে\n"
            f"💰 *মোট balance:* `{user['tokens']} tokens`\n\n"
            f"_আবার আসো ২৪ ঘণ্টা পরে!_",
            parse_mode='Markdown', reply_markup=kb_home())
    else:
        await update.message.reply_text(
            f"⏰ *Daily Token ইতিমধ্যে নিয়েছ!*\n\n"
            f"আবার আসো *{hours_left} ঘণ্টা* পরে।",
            parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# /admin
# ══════════════════════════════════════════════
async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if u.id not in ADMIN_IDS:
        await update.message.reply_text("❌ তুমি admin নও।"); return
    s = get_stats()
    await update.message.reply_text(
        f"👑 *Admin Panel*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"👥 মোট Users: `{s['users']}`\n"
        f"✅ Active: `{s['active']}`\n"
        f"🔄 মোট অনুবাদ: `{s['translations']}`\n"
        f"📝 মোট Lines: `{s['lines']}`\n\n"
        f"নিচের বাটন ব্যবহার করো:",
        parse_mode='Markdown', reply_markup=kb_admin())

# ══════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    d   = q.data

    # ── No-auth callbacks ──
    if d == "chk":
        await q.answer()
        if await is_member(uid, ctx.bot):
            get_user(uid, q.from_user.username, q.from_user.first_name)
            await q.edit_message_text(
                "✅ *দারুণ! যোগ দিয়েছ!*\n\nএখন শুরু করো! 🚀",
                parse_mode='Markdown', reply_markup=kb_home())
        else:
            await q.edit_message_text(NOT_JOINED, parse_mode='Markdown',
                                      reply_markup=kb_not_joined())
        return

    if d.startswith("cancel_"):
        await q.answer()
        target = int(d.split("_")[1])
        if uid == target and uid in active_tasks:
            active_tasks[uid] = True
            if uid in cancel_events: cancel_events[uid].set()
            try:
                await q.edit_message_caption(
                    caption="❌ *বাতিল করা হয়েছে!*\nনতুন ফাইল পাঠাও।",
                    parse_mode='Markdown', reply_markup=kb_home())
            except: pass
        else:
            await q.answer("কোনো সক্রিয় কাজ নেই!", show_alert=True)
        return

    # ── Audio callbacks (no full auth needed, just ban check) ──
    if d.startswith("tr_"):
        if is_banned_user(uid):
            await q.answer(BANNED_MSG, show_alert=True); return
        parts    = d.split("_")
        mode     = parts[1]
        target   = int(parts[-1])
        if uid != target:
            await q.answer("এটা তোমার জন্য নয়!", show_alert=True); return
        await q.answer()
        mode_txt = {"bn":"🇧🇩 বাংলায় Transcription",
                    "en":"🇺🇸 English Transcription",
                    "translate":"🔄 Transcription + অনুবাদ"}.get(mode,"")
        try:
            await q.edit_message_text(
                f"✅ *{mode_txt} শুরু...*\n\n⏳ অপেক্ষা করো...",
                parse_mode='Markdown')
        except: pass
        asyncio.create_task(
            do_transcription(uid, mode, ctx.bot, q.message.chat_id, ctx))
        return

    # ── Full access check ──
    if is_banned_user(uid):
        await q.answer("🚫 তুমি ban!", show_alert=True); return
    if not await is_member(uid, ctx.bot):
        await q.answer("🔒 চ্যানেলে যোগ দাও!", show_alert=True)
        try:
            await q.edit_message_text(NOT_JOINED, parse_mode='Markdown',
                                      reply_markup=kb_not_joined())
        except: pass
        return

    await q.answer()
    user = get_user(uid)

    # ── SubDL download ──
    if d.startswith("subdl_"):
        idx  = d.split("_")[1]
        url  = ctx.user_data.get(f"suburl_{idx}",'')
        name = ctx.user_data.get(f"subname_{idx}",'subtitle.srt')
        if not url:
            await q.message.reply_text("❌ তথ্য পাওয়া যায়নি।"); return
        await q.answer("⏳ ডাউনলোড...")
        loop    = asyncio.get_event_loop()
        content = await loop.run_in_executor(executor, subdl_dl, url)
        if not content:
            await q.message.reply_text("❌ ডাউনলোড হয়নি।"); return
        srt_b = content
        if content[:2] == b'PK':
            import zipfile
            try:
                with zipfile.ZipFile(io.BytesIO(content)) as z:
                    for n in z.namelist():
                        if n.lower().endswith('.srt'):
                            srt_b = z.read(n); name = os.path.basename(n); break
            except Exception as e: logger.error(f"Zip: {e}")
        if not name.lower().endswith('.srt'): name += '.srt'
        await q.message.reply_document(
            document=io.BytesIO(srt_b), filename=name,
            caption=f"✅ *ডাউনলোড সম্পন্ন!*\n\n📁 `{name}`\n\n"
                    f"_এই ফাইলটা পাঠালে অনুবাদ করে দেব!_ 🔄",
            parse_mode='Markdown')
        return

    # ── Language settings ──
    if d.startswith("set_src_"):
        lang = d.replace("set_src_","")
        set_lang_pref(uid, from_lang=lang)
        await q.edit_message_text(
            f"✅ *Source language set:* {SRC_LANGS.get(lang,lang)}\n\n"
            f"এখন target language বেছে নাও:",
            parse_mode='Markdown', reply_markup=kb_dst_lang())
        return

    if d.startswith("set_dst_"):
        lang = d.replace("set_dst_","")
        set_lang_pref(uid, to_lang=lang)
        await q.edit_message_text(
            f"✅ *Target language set:* {DST_LANGS.get(lang,lang)}\n\n"
            f"সেটিং সেভ হয়েছে! পরবর্তী অনুবাদে এই ভাষা ব্যবহার হবে।",
            parse_mode='Markdown', reply_markup=kb_home())
        return

    # ── Chat ──
    if d == "chat_start":
        chat_mode[uid] = True
        if uid not in chat_history: chat_history[uid] = []
        await q.edit_message_text(
            "💬 *AI চ্যাট মোড চালু!*\n\nযা মনে চায় লেখো 🤖",
            parse_mode='Markdown', reply_markup=kb_chat())
        return
    if d == "chat_clear":
        chat_history[uid] = []
        await q.edit_message_text(
            "🗑 *কথোপকথন মুছা হয়েছে!*", parse_mode='Markdown', reply_markup=kb_chat())
        return
    if d == "chat_stop":
        chat_mode[uid] = False
        await q.edit_message_text(
            "✅ *চ্যাট বন্ধ।*", parse_mode='Markdown', reply_markup=kb_home())
        return

    # ── Daily ──
    if d == "daily":
        success, hours = claim_daily(uid)
        u2 = get_user(uid)
        if success:
            await q.edit_message_text(
                f"🎁 *+{DAILY_TOKENS} tokens পেয়েছ!*\n\n"
                f"💰 Balance: `{u2['tokens']}`",
                parse_mode='Markdown', reply_markup=kb_home())
        else:
            await q.edit_message_text(
                f"⏰ *{hours} ঘণ্টা পরে আবার নাও।*\n💰 Balance: `{u2['tokens']}`",
                parse_mode='Markdown', reply_markup=kb_home())
        return

    # ── Profile ──
    if d == "profile":
        hist  = get_history(uid, 5)
        refs  = ref_count(uid)
        badge = "👑 Premium" if user['tokens'] >= PREMIUM_THRESHOLD else "🆓 Free"
        hist_text = ""
        if hist:
            hist_text = "\n\n📋 *শেষ ৫টি:*\n"
            for h in hist:
                hist_text += f"• `{h['file_name'][:20]}` {h['line_count']}লাইন\n"
        await q.edit_message_text(
            f"👤 *প্রোফাইল*\n\n"
            f"🎖 {badge} | 🪙 `{user['tokens']}` tokens\n"
            f"🔄 অনুবাদ: `{user['total_translations']}` | "
            f"📝 Lines: `{user['total_lines']}`\n"
            f"🤝 Referral: `{refs}` জন\n"
            f"🌐 {SRC_LANGS.get(user['from_lang'],'Auto')} → "
            f"{DST_LANGS.get(user['to_lang'],'বাংলা')}"
            f"{hist_text}",
            parse_mode='Markdown', reply_markup=kb_back())
        return

    # ── Tools menu ──
    if d == "tools_menu":
        await q.edit_message_text(
            "🛠 *Subtitle Tools*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "⏱ *Timing Fix:* subtitle-এর সময় ঠিক করো\n"
            "🔀 *Merge:* দুটো ফাইল একসাথে জোড়া লাগাও",
            parse_mode='Markdown', reply_markup=kb_tools())
        return

    if d == "tool_timing":
        user_state[uid] = {'action': 'timing_wait_file'}
        await q.edit_message_text(
            "⏱ *Timing Fix*\n\n"
            "Subtitle file পাঠাও।\n"
            "তারপর কত সেকেন্ড shift করতে চাও বলবে।\n\n"
            "_(সামনে যেতে: +5, পেছনে যেতে: -5)_",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ বাতিল", callback_data="home")
            ]]))
        return

    if d == "tool_merge":
        user_state[uid] = {'action': 'merge_wait_first'}
        await q.edit_message_text(
            "🔀 *Subtitle Merge*\n\n"
            "*প্রথম* SRT ফাইলটা পাঠাও।",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ বাতিল", callback_data="home")
            ]]))
        return

    # ── YouTube info ──
    if d == "yt_info":
        if not YT_AVAILABLE:
            await q.edit_message_text(
                "❌ *YouTube support চালু নেই।*\n\n"
                "`yt-dlp` install করা নেই।",
                parse_mode='Markdown', reply_markup=kb_back())
            return
        user_state[uid] = {'action': 'yt_wait_url'}
        await q.edit_message_text(
            "▶️ *YouTube Subtitle Downloader*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "YouTube video-র link পাঠাও।\n"
            "বট subtitle download করে বাংলায় অনুবাদ করবে!\n\n"
            "📌 Example:\n`https://www.youtube.com/watch?v=...`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ বাতিল", callback_data="home")
            ]]))
        return

    # ── Language menu ──
    if d == "lang_menu":
        await q.edit_message_text(
            f"🌐 *ভাষা সেটিং*\n\n"
            f"বর্তমান: {SRC_LANGS.get(user['from_lang'],'Auto')} → "
            f"{DST_LANGS.get(user['to_lang'],'বাংলা')}\n\n"
            f"Source language বেছে নাও:",
            parse_mode='Markdown', reply_markup=kb_src_lang())
        return

    # ── Search ──
    if d == "search":
        if not SUBDL_API_KEY:
            await q.edit_message_text("❌ SUBDL_API_KEY নেই।",
                                      parse_mode='Markdown', reply_markup=kb_back())
            return
        ctx.user_data['awaiting_search'] = True
        await q.edit_message_text(
            "🔍 *Subtitle খোঁজো*\n\nমুভির নাম লেখো (English):\n\n"
            "📌 `Me Before You 2016`\n📌 `Breaking Bad S01E01`",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ বাতিল", callback_data="home")
            ]]))
        return

    # ── Audio info ──
    if d == "audio_info":
        await q.edit_message_text(
            "🎙 *Audio Transcription*\n\n"
            "Voice message বা অডিও ফাইল পাঠাও!\n\n"
            "সাপোর্টেড: `mp3 mp4 wav m4a ogg webm flac`\n"
            "⚠️ Max: 25MB | Powered by Groq Whisper",
            parse_mode='Markdown', reply_markup=kb_back())
        return

    # ── Status ──
    if d == "status":
        running  = sum(1 for v in active_tasks.values() if not v)
        chatting = sum(1 for v in chat_mode.values() if v)
        await q.edit_message_text(
            f"📊 *বট স্ট্যাটাস*\n\n🟢 Online\n"
            f"⚙️ চলমান: {running} | 💬 চ্যাট: {chatting}",
            parse_mode='Markdown', reply_markup=kb_back())
        return

    # ── Help ──
    if d == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার বিধি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "*📁 ফাইল অনুবাদ:*\n"
            "SRT/VTT/ASS ফাইল পাঠাও → অনুবাদ পাবে\n\n"
            "*▶️ YouTube:*\n"
            "YouTube link পাঠাও → subtitle → অনুবাদ\n\n"
            "*🎙 Audio:*\n"
            "Voice/Audio পাঠাও → Transcription\n\n"
            "*🛠 Tools:*\n"
            "Timing fix, Merge — Tools বাটনে\n\n"
            "*🪙 Tokens:*\n"
            "• Welcome: 50 | Daily: 10 | Referral: 25\n"
            "• /daily — প্রতিদিন token নাও\n"
            "• /referral — বন্ধু আনো, token পাও\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 Commands:\n"
            "`/start` `/profile` `/referral` `/daily`",
            parse_mode='Markdown', reply_markup=kb_back())
        return

    # ── Admin callbacks ──
    if d.startswith("adm_") and uid in ADMIN_IDS:
        action = d.replace("adm_","")
        if action == "stats":
            s = get_stats()
            await q.edit_message_text(
                f"📊 *Statistics*\n\n"
                f"👥 Users: `{s['users']}`\n✅ Active: `{s['active']}`\n"
                f"🔄 Translations: `{s['translations']}`\n"
                f"📝 Lines: `{s['lines']}`",
                parse_mode='Markdown', reply_markup=kb_admin())
        elif action == "broadcast":
            user_state[uid] = {'action': 'admin_broadcast'}
            await q.edit_message_text(
                "📢 *Broadcast*\n\nসবাইকে পাঠানোর message লেখো:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ বাতিল", callback_data="home")
                ]]))
        elif action == "ban":
            user_state[uid] = {'action': 'admin_ban'}
            await q.edit_message_text(
                "🚫 *Ban User*\n\nUser ID লেখো:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ বাতিল", callback_data="home")
                ]]))
        elif action == "unban":
            user_state[uid] = {'action': 'admin_unban'}
            await q.edit_message_text(
                "✅ *Unban User*\n\nUser ID লেখো:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ বাতিল", callback_data="home")
                ]]))
        elif action == "lookup":
            user_state[uid] = {'action': 'admin_lookup'}
            await q.edit_message_text(
                "👤 *User Lookup*\n\nUser ID বা @username লেখো:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ বাতিল", callback_data="home")
                ]]))
        elif action == "tokens":
            user_state[uid] = {'action': 'admin_tokens_uid'}
            await q.edit_message_text(
                "🪙 *Token দাও*\n\nUser ID লেখো:",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ বাতিল", callback_data="home")
                ]]))
        return

    # ── Home ──
    if d == "home":
        chat_mode[uid] = False
        ctx.user_data['awaiting_search'] = False
        user_state.pop(uid, None)
        await q.edit_message_text(
            "🎬 *Subtitle BD Bot*\n\nফাইল পাঠাও বা বাটন ব্যবহার করো! 🚀",
            parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 🎙  AUDIO TRANSCRIPTION TASK
# ══════════════════════════════════════════════
async def do_transcription(uid, mode, bot, chat_id, ctx):
    audio_info = pending_audio.get(uid)
    if not audio_info:
        await bot.send_message(chat_id, "❌ অডিও পাওয়া যায়নি। আবার পাঠাও।"); return
    pending_audio.pop(uid, None)
    mode_text = {"bn":"🇧🇩 বাংলায় Transcription",
                 "en":"🇺🇸 English Transcription",
                 "translate":"🔄 Transcription + অনুবাদ"}.get(mode,"")
    status = await bot.send_message(
        chat_id, f"⏳ *{mode_text} চলছে...*\nঅপেক্ষা করো...",
        parse_mode='Markdown')
    try:
        f   = await bot.get_file(audio_info['file_id'])
        raw = await f.download_as_bytearray()
        loop = asyncio.get_event_loop()
        lang = 'bn' if mode == 'bn' else 'en'
        transcript = await loop.run_in_executor(
            executor, functools.partial(
                transcribe_audio, bytes(raw), audio_info['file_name'], lang))
        if not transcript or not transcript.strip():
            await bot.edit_message_text(
                "❌ কোনো কথা পাওয়া যায়নি!", chat_id=chat_id,
                message_id=status.message_id, reply_markup=kb_back()); return
        if mode in ('bn','en'):
            flag = "🇧🇩" if mode=='bn' else "🇺🇸"
            text = f"✅ *{flag} Transcription সম্পন্ন!*\n\n━━━━━━━━\n{transcript}\n━━━━━━━━"
            if len(text) > 4096:
                await bot.edit_message_text("✅ সম্পন্ন! ফাইল পাঠাচ্ছি...",
                    chat_id=chat_id, message_id=status.message_id)
                await bot.send_document(
                    chat_id, document=io.BytesIO(transcript.encode()),
                    filename="transcript.txt", reply_markup=kb_home())
            else:
                await bot.edit_message_text(text, chat_id=chat_id,
                    message_id=status.message_id, parse_mode='Markdown',
                    reply_markup=kb_home())
        elif mode == 'translate':
            await bot.edit_message_text("🔄 অনুবাদ করছি...", chat_id=chat_id,
                                        message_id=status.message_id)
            user = get_user(uid)
            bengali = await loop.run_in_executor(
                executor, functools.partial(
                    translate_plain_text, transcript, user['to_lang']))
            combined = f"🇺🇸 *English:*\n{transcript}\n\n🇧🇩 *Translation:*\n{bengali}"
            if len(combined) > 4096:
                full = f"=== English ===\n{transcript}\n\n=== Translation ===\n{bengali}"
                await bot.edit_message_text("✅ সম্পন্ন! ফাইল পাঠাচ্ছি...",
                    chat_id=chat_id, message_id=status.message_id)
                await bot.send_document(
                    chat_id, document=io.BytesIO(full.encode()),
                    filename="transcript_translated.txt", reply_markup=kb_home())
            else:
                await bot.edit_message_text(
                    f"✅ *সম্পন্ন!*\n\n{combined}", chat_id=chat_id,
                    message_id=status.message_id, parse_mode='Markdown',
                    reply_markup=kb_home())
    except Exception as e:
        err = str(e)
        notice = QUOTA_MSG if "QUOTA_EXCEEDED" in err else f"❌ সমস্যা: `{err[:150]}`"
        try:
            await bot.edit_message_text(notice, chat_id=chat_id,
                message_id=status.message_id, parse_mode='Markdown',
                reply_markup=kb_quota() if "QUOTA" in err else kb_back())
        except: pass

# ══════════════════════════════════════════════
# 📁  FILE HANDLER
# ══════════════════════════════════════════════
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    doc = update.message.document
    if not await access_ok(u.id, ctx.bot, update.message.reply_text): return

    fname = doc.file_name.lower() if doc.file_name else ''
    user  = get_user(u.id, u.username, u.first_name)
    state = user_state.get(u.id, {})

    # ── Tool: Timing fix — waiting for file ──
    if state.get('action') == 'timing_wait_file' and fname.endswith(('.srt','.vtt','.ass','.ssa')):
        user_state[u.id] = {'action': 'timing_wait_offset', 'file_id': doc.file_id,
                             'file_name': doc.file_name}
        await update.message.reply_text(
            "✅ ফাইল পেয়েছি!\n\nএখন কত সেকেন্ড shift করতে চাও?\n"
            "_(সামনে: +5, পেছনে: -3.5)_",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ বাতিল", callback_data="home")]]))
        return

    # ── Tool: Merge — waiting for first file ──
    if state.get('action') == 'merge_wait_first' and fname.endswith(('.srt','.vtt','.ass','.ssa')):
        user_state[u.id] = {'action': 'merge_wait_second',
                             'file1_id': doc.file_id, 'file1_name': doc.file_name}
        await update.message.reply_text(
            "✅ প্রথম ফাইল পেয়েছি!\n\nএখন *দ্বিতীয়* SRT ফাইলটা পাঠাও।",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ বাতিল", callback_data="home")]]))
        return

    # ── Tool: Merge — waiting for second file ──
    if state.get('action') == 'merge_wait_second' and fname.endswith(('.srt','.vtt','.ass','.ssa')):
        msg = await update.message.reply_text("⏳ Merge হচ্ছে...")
        try:
            f1  = await ctx.bot.get_file(state['file1_id'])
            r1  = await f1.download_as_bytearray()
            f2  = await ctx.bot.get_file(doc.file_id)
            r2  = await f2.download_as_bytearray()
            c1  = r1.decode('utf-8-sig','ignore')
            c2  = r2.decode('utf-8-sig','ignore')
            b1  = parse_auto(c1, state['file1_name'])
            b2  = parse_auto(c2, doc.file_name)
            merged = merge_subtitles(b1, b2)
            out = build_srt(merged).encode('utf-8-sig')
            await msg.delete()
            await update.message.reply_document(
                document=io.BytesIO(out),
                filename="merged_subtitle.srt",
                caption=f"✅ *Merge সম্পন্ন!*\n\n"
                        f"📊 {len(b1)} + {len(b2)} = *{len(merged)} লাইন*",
                parse_mode='Markdown', reply_markup=kb_home())
        except Exception as e:
            await msg.edit_text(f"❌ সমস্যা: `{str(e)[:100]}`",
                                parse_mode='Markdown')
        user_state.pop(u.id, None)
        return

    # ── Audio file ──
    if any(fname.endswith(ext) for ext in AUDIO_EXTS):
        if doc.file_size and doc.file_size > MAX_AUDIO_MB:
            await update.message.reply_text("❌ অডিও ফাইল ২৫MB-এর বেশি!")
            return
        pending_audio[u.id] = {'file_id': doc.file_id,
                                'file_name': doc.file_name or 'audio.mp3'}
        await update.message.reply_text(
            f"🎙 *অডিও পেয়েছি!*\n\n📁 `{doc.file_name}`\n\nকী করতে চাও?",
            parse_mode='Markdown', reply_markup=kb_audio_options(u.id))
        return

    # ── SRT/VTT/ASS file ──
    if not fname.endswith(('.srt','.vtt','.ass','.ssa')):
        await update.message.reply_text(
            "❌ *সাপোর্টেড ফাইল:* `.srt` `.vtt` `.ass`\n"
            "বা অডিও: `mp3 wav m4a ogg`", parse_mode='Markdown')
        return

    max_mb = 10 * 1024 * 1024 if user['tokens'] >= PREMIUM_THRESHOLD else 5 * 1024 * 1024
    if doc.file_size and doc.file_size > max_mb:
        mb = max_mb // (1024*1024)
        await update.message.reply_text(
            f"❌ ফাইল সাইজ {mb}MB-এর বেশি!\n"
            f"_(👑 Premium users: 10MB, Free: 5MB)_", parse_mode='Markdown')
        return

    if u.id in active_tasks and not active_tasks[u.id]:
        await update.message.reply_text("⚠️ একটি অনুবাদ চলছে! আগেরটা শেষ করো।"); return

    user_state.pop(u.id, None)
    chat_mode[u.id]     = False
    active_tasks[u.id]  = False
    cancel_events[u.id] = threading.Event()
    c_event             = cancel_events[u.id]
    from_lang           = user.get('from_lang','auto')
    to_lang             = user.get('to_lang','bn')
    lang_display        = f"{SRC_LANGS.get(from_lang,'Auto')} → {DST_LANGS.get(to_lang,'বাংলা')}"

    status = await update.message.reply_photo(
        photo=pie_chart(0,1),
        caption=(f"📥 *ফাইল পেয়েছি!*\n\n"
                 f"📁 `{doc.file_name}`\n"
                 f"🌐 {lang_display}\n⏳ প্রস্তুত হচ্ছে..."),
        parse_mode='Markdown', reply_markup=kb_cancel(u.id))

    try:
        f   = await ctx.bot.get_file(doc.file_id)
        raw = await f.download_as_bytearray()
        content = None
        for enc in ['utf-8-sig','utf-8','latin-1','cp1252']:
            try: content = raw.decode(enc); break
            except: continue
        if not content:
            await status.edit_caption("❌ ফাইল পড়তে পারছি না!",parse_mode='Markdown'); return

        blocks = parse_auto(content, doc.file_name)
        if not blocks:
            await status.edit_caption("❌ কোনো subtitle নেই!",parse_mode='Markdown'); return

        total = len(blocks)
        await status.edit_media(InputMediaPhoto(
            media=pie_chart(0,total),
            caption=(f"🎬 *অনুবাদ শুরু হচ্ছে...*\n\n"
                     f"📁 `{doc.file_name}`\n"
                     f"🌐 {lang_display}\n"
                     f"📊 মোট: *{total}টি*\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n⏳ 0/{total}"),
            parse_mode='Markdown'), reply_markup=kb_cancel(u.id))

        BATCH      = 7
        translated = list(blocks)
        completed  = 0
        loop       = asyncio.get_event_loop()

        for i in range(0, total, BATCH):
            if c_event.is_set() or active_tasks.get(u.id, False): return
            chunk  = blocks[i:i+BATCH]
            texts  = [b['text'] for b in chunk]
            result = await loop.run_in_executor(
                executor,
                functools.partial(translate_batch, texts, from_lang, to_lang, c_event))
            if c_event.is_set() or active_tasks.get(u.id, False): return
            for j,tr in enumerate(result):
                if i+j < total: translated[i+j]['text'] = tr
            completed = min(i+BATCH, total)
            pct  = completed/total*100
            bar  = '█'*int(pct/5)+'░'*(20-int(pct/5))
            try:
                await status.edit_media(InputMediaPhoto(
                    media=pie_chart(completed,total),
                    caption=(f"🔄 *অনুবাদ চলছে...*\n\n"
                             f"📁 `{doc.file_name}`\n"
                             f"`[{bar}]` *{pct:.1f}%*\n"
                             f"━━━━━━━━━━━━━━━━━━━━━\n"
                             f"✅ {completed}/{total} | ⏳ বাকি {total-completed}"),
                    parse_mode='Markdown'), reply_markup=kb_cancel(u.id))
            except Exception as e:
                logger.warning(f"Edit ignored: {e}")
            await asyncio.sleep(0.4)

        if c_event.is_set() or active_tasks.get(u.id, False): return

        out_bytes = build_srt(translated).encode('utf-8-sig')
        out_name  = re.sub(r'\.(srt|vtt|ass|ssa)$', '_Bengali.srt',
                           doc.file_name, flags=re.IGNORECASE)

        await status.edit_media(InputMediaPhoto(
            media=pie_chart(total,total),
            caption=(f"✅ *অনুবাদ সম্পন্ন!*\n\n"
                     f"📁 `{doc.file_name}`\n"
                     f"🎉 *{total}টি* সাবটাইটেল\n"
                     f"🌐 {lang_display}"),
            parse_mode='Markdown'))

        await update.message.reply_document(
            document=io.BytesIO(out_bytes), filename=out_name,
            caption=(f"🎬 *অনুবাদিত ফাইল*\n\n"
                     f"📁 `{out_name}`\n✅ *{total}* লাইন\n"
                     f"🌐 {lang_display}\n⏱ Timing অক্ষুণ্ণ\n\n"
                     f"_VLC / MX Player-এ ব্যবহার করো_ 🎥"),
            parse_mode='Markdown', reply_markup=kb_home())

        log_history(u.id, doc.file_name, total, from_lang, to_lang)
        add_tokens(u.id, 2)   # প্রতিটি সফল অনুবাদে ২ token bonus
        logger.info(f"✅ Translated {total} lines for {u.id}")

    except Exception as e:
        err = str(e)
        logger.error(f"Error {u.id}: {err}")
        notice = QUOTA_MSG if "QUOTA_EXCEEDED" in err else f"❌ সমস্যা!\n\n`{err[:200]}`"
        kb = kb_quota() if "QUOTA_EXCEEDED" in err else kb_home()
        try:
            await status.edit_caption(notice, parse_mode='Markdown', reply_markup=kb)
        except:
            await update.message.reply_text(notice, parse_mode='Markdown', reply_markup=kb)
    finally:
        active_tasks.pop(u.id, None)
        cancel_events.pop(u.id, None)

# ══════════════════════════════════════════════
# 🎤  VOICE / AUDIO HANDLER
# ══════════════════════════════════════════════
async def handle_audio_or_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    msg = update.message
    if not await access_ok(u.id, ctx.bot, update.message.reply_text): return
    if msg.voice:
        file_obj, fname, fsize = msg.voice, f"voice_{u.id}.ogg", msg.voice.file_size
    elif msg.audio:
        file_obj, fname, fsize = msg.audio, (msg.audio.file_name or f"audio.mp3"), msg.audio.file_size
    else: return
    if fsize and fsize > MAX_AUDIO_MB:
        await msg.reply_text("❌ অডিও ফাইল ২৫MB-এর বেশি!"); return
    pending_audio[u.id] = {'file_id': file_obj.file_id, 'file_name': fname}
    await msg.reply_text(
        f"🎙 *অডিও পেয়েছি!*\n\n📁 `{fname}`\n\nকী করতে চাও?",
        parse_mode='Markdown', reply_markup=kb_audio_options(u.id))

# ══════════════════════════════════════════════
# 💬  TEXT HANDLER
# ══════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u    = update.effective_user
    text = update.message.text.strip()
    if not await access_ok(u.id, ctx.bot, update.message.reply_text): return

    user  = get_user(u.id, u.username, u.first_name)
    state = user_state.get(u.id, {})

    # ── Timing fix — offset input ──
    if state.get('action') == 'timing_wait_offset':
        try:
            offset = float(text.replace(',','.'))
        except:
            await update.message.reply_text("❌ সংখ্যা লেখো! (যেমন: +5 বা -3.5)"); return
        msg = await update.message.reply_text("⏳ Timing ঠিক করছি...")
        try:
            f   = await ctx.bot.get_file(state['file_id'])
            raw = await f.download_as_bytearray()
            content = raw.decode('utf-8-sig','ignore')
            blocks  = parse_auto(content, state['file_name'])
            fixed   = fix_timing(blocks, offset)
            out     = build_srt(fixed).encode('utf-8-sig')
            out_name = re.sub(r'\.srt$', f'_shifted{offset:+g}s.srt', state['file_name'])
            await msg.delete()
            await update.message.reply_document(
                document=io.BytesIO(out), filename=out_name,
                caption=(f"✅ *Timing Fix সম্পন্ন!*\n\n"
                         f"⏱ Shift: `{offset:+g}` সেকেন্ড\n"
                         f"📊 {len(fixed)} লাইন"),
                parse_mode='Markdown', reply_markup=kb_home())
        except Exception as e:
            await msg.edit_text(f"❌ সমস্যা: `{str(e)[:100]}`", parse_mode='Markdown')
        user_state.pop(u.id, None)
        return

    # ── Admin: broadcast ──
    if state.get('action') == 'admin_broadcast' and u.id in ADMIN_IDS:
        uids = all_uids()
        msg  = await update.message.reply_text(f"📢 পাঠানো হচ্ছে {len(uids)} জনকে...")
        success, fail = 0, 0
        for uid2 in uids:
            try:
                await ctx.bot.send_message(uid2, text, parse_mode='Markdown')
                success += 1
            except: fail += 1
            await asyncio.sleep(0.05)
        await msg.edit_text(f"✅ Broadcast সম্পন্ন!\n✅ Success: {success}\n❌ Failed: {fail}")
        user_state.pop(u.id, None); return

    # ── Admin: ban ──
    if state.get('action') == 'admin_ban' and u.id in ADMIN_IDS:
        try:
            ban_user(int(text), True)
            await update.message.reply_text(f"🚫 User `{text}` ban হয়েছে।",
                                            parse_mode='Markdown', reply_markup=kb_admin())
        except: await update.message.reply_text("❌ ভুল ID")
        user_state.pop(u.id, None); return

    # ── Admin: unban ──
    if state.get('action') == 'admin_unban' and u.id in ADMIN_IDS:
        try:
            ban_user(int(text), False)
            await update.message.reply_text(f"✅ User `{text}` unban হয়েছে।",
                                            parse_mode='Markdown', reply_markup=kb_admin())
        except: await update.message.reply_text("❌ ভুল ID")
        user_state.pop(u.id, None); return

    # ── Admin: lookup ──
    if state.get('action') == 'admin_lookup' and u.id in ADMIN_IDS:
        try:
            uid2 = int(text)
            con  = db(); c = con.cursor()
            c.execute("SELECT * FROM users WHERE uid=?", (uid2,))
            row = c.fetchone(); con.close()
            if row:
                row = dict(row)
                await update.message.reply_text(
                    f"👤 *User Info*\n\nID: `{row['uid']}`\n"
                    f"Name: {row['first_name']}\n🪙 Tokens: {row['tokens']}\n"
                    f"🔄 Trans: {row['total_translations']}\n"
                    f"🚫 Banned: {bool(row['is_banned'])}",
                    parse_mode='Markdown', reply_markup=kb_admin())
            else:
                await update.message.reply_text("❌ User পাওয়া যায়নি।")
        except: await update.message.reply_text("❌ ভুল ID")
        user_state.pop(u.id, None); return

    # ── Admin: give tokens ──
    if state.get('action') == 'admin_tokens_uid' and u.id in ADMIN_IDS:
        try:
            user_state[u.id] = {'action': 'admin_tokens_amount', 'target_uid': int(text)}
            await update.message.reply_text(f"কত token দেবে?")
        except: await update.message.reply_text("❌ ভুল ID")
        return

    if state.get('action') == 'admin_tokens_amount' and u.id in ADMIN_IDS:
        try:
            target = state['target_uid']
            amount = int(text)
            add_tokens(target, amount)
            await update.message.reply_text(
                f"✅ `{target}` কে *{amount} tokens* দেওয়া হয়েছে।",
                parse_mode='Markdown', reply_markup=kb_admin())
        except: await update.message.reply_text("❌ ভুল পরিমাণ")
        user_state.pop(u.id, None); return

    # ── YouTube URL ──
    if state.get('action') == 'yt_wait_url':
        if not ('youtube.com' in text or 'youtu.be' in text):
            await update.message.reply_text("❌ সঠিক YouTube link দাও!"); return
        user_state.pop(u.id, None)
        msg = await update.message.reply_text(
            "▶️ *YouTube subtitle download হচ্ছে...*\n\nঅপেক্ষা করো ⏳",
            parse_mode='Markdown')
        loop = asyncio.get_event_loop()
        content, title = await loop.run_in_executor(
            executor, functools.partial(download_yt_subtitle, text, 'en'))
        if not content:
            await msg.edit_text(
                "❌ *Subtitle পাওয়া যায়নি!*\n\n"
                "এই video-তে subtitle নাও থাকতে পারে।",
                parse_mode='Markdown', reply_markup=kb_back())
            return
        await msg.edit_text(
            f"✅ *Subtitle পাওয়া গেছে!*\n\n🎬 `{title}`\n\n"
            f"⏳ এখন বাংলায় অনুবাদ করছি...",
            parse_mode='Markdown')
        # Save as temp file and process
        fname   = f"{title[:30]}.vtt"
        blocks  = parse_vtt(content) if content.startswith('WEBVTT') else parse_srt(content)
        if not blocks:
            await msg.edit_text("❌ Subtitle parse করা যায়নি!", reply_markup=kb_back()); return
        total      = len(blocks)
        translated = list(blocks)
        from_lang  = user.get('from_lang','auto')
        to_lang    = user.get('to_lang','bn')
        BATCH = 7
        for i in range(0, total, BATCH):
            chunk  = blocks[i:i+BATCH]
            texts  = [b['text'] for b in chunk]
            result = await loop.run_in_executor(
                executor,
                functools.partial(translate_batch, texts, from_lang, to_lang))
            for j, tr in enumerate(result):
                if i+j < total: translated[i+j]['text'] = tr
            pct = min(i+BATCH, total)/total*100
            try:
                await msg.edit_text(
                    f"🔄 *অনুবাদ চলছে...*\n\n"
                    f"🎬 `{title}`\n"
                    f"⏳ `{pct:.0f}%` সম্পন্ন",
                    parse_mode='Markdown')
            except: pass
            await asyncio.sleep(0.5)

        out_name  = f"{title[:30]}_Bengali.srt"
        out_bytes = build_srt(translated).encode('utf-8-sig')
        await msg.delete()
        await update.message.reply_document(
            document=io.BytesIO(out_bytes), filename=out_name,
            caption=(f"🎬 *YouTube Subtitle অনুবাদ সম্পন্ন!*\n\n"
                     f"📺 `{title}`\n✅ *{total}টি* লাইন"),
            parse_mode='Markdown', reply_markup=kb_home())
        log_history(u.id, out_name, total, from_lang, to_lang)
        add_tokens(u.id, 3)
        return

    # ── Search mode ──
    if ctx.user_data.get('awaiting_search'):
        ctx.user_data['awaiting_search'] = False
        msg     = await update.message.reply_text(
            f"🔍 *খোঁজা হচ্ছে:* `{text}`\n\n⏳...", parse_mode='Markdown')
        loop    = asyncio.get_event_loop()
        results = await loop.run_in_executor(executor, subdl_search, text)
        if not results:
            await msg.edit_text(
                f"😔 `{text}` এর জন্য কিছু পাওয়া যায়নি!",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")],
                    [InlineKeyboardButton("🔙 হোম",         callback_data="home")]
                ]))
            return
        body = f"🎬 *`{text}`* এর Subtitle:\n\n━━━━━━━━━━━━━━━━━━━━━\n\n"
        buttons = []
        for i, item in enumerate(results, 1):
            name = item.get('release_name','Unknown')[:45]
            lang = item.get('language','EN')
            yr   = f" ({item.get('year','')})" if item.get('year') else ""
            ctx.user_data[f"suburl_{i}"] = item.get('url','')
            ctx.user_data[f"subname_{i}"] = (item.get('release_name',f'subtitle_{i}')+'.srt')[:60]
            body += f"*{i}.* {name}{yr} 🌐{lang}\n\n"
            buttons.append([InlineKeyboardButton(f"⬇️ {i}. {name[:33]}{yr}",
                                                  callback_data=f"subdl_{i}")])
        buttons.append([InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")])
        buttons.append([InlineKeyboardButton("🔙 হোম",         callback_data="home")])
        await msg.edit_text(body, parse_mode='Markdown',
                            reply_markup=InlineKeyboardMarkup(buttons))
        return

    # ── AI Chat ──
    if chat_mode.get(u.id, False):
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        loop  = asyncio.get_event_loop()
        reply = await loop.run_in_executor(executor, functools.partial(ai_chat, u.id, text))
        hlen  = len(chat_history.get(u.id,[])) // 2
        await update.message.reply_text(
            f"{reply}\n\n━━━━━━━━━━━━━━━━━━━━━\n_💬 {hlen} বার্তা_",
            parse_mode='Markdown', reply_markup=kb_chat())
        return

    # ── Default ──
    await update.message.reply_text(
        "📌 *কী করতে চাও?*\n\n"
        "• SRT/VTT/ASS ফাইল পাঠাও\n"
        "• YouTube link পাঠাও\n"
        "• Voice/Audio পাঠাও\n"
        "• বাটন ব্যবহার করো 👇",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════
def main():
    if not BOT_TOKEN:   logger.error("❌ BOT_TOKEN missing!"); return
    if not GROQ_API_KEY: logger.error("❌ GROQ_API_KEY missing!"); return

    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    logger.info("✅ Flask + self-ping started")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("profile",  cmd_profile))
    app.add_handler(CommandHandler("referral", cmd_referral))
    app.add_handler(CommandHandler("daily",    cmd_daily))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.VOICE, handle_audio_or_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio_or_voice))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🤖 Bot polling started!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=30, write_timeout=30,
        connect_timeout=30, pool_timeout=30)

if __name__ == '__main__':
    main()
