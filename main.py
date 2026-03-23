#!/usr/bin/env python3
"""
🎬 SRT Subtitle Translator — Ultra Version
Powered by Groq AI + Whisper + Cobalt
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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
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

PIXABAY_API_KEY = os.environ.get('PIXABAY_API_KEY', '')

try:
    from fpdf import FPDF
    FPDF_AVAILABLE = True
except ImportError:
    FPDF_AVAILABLE = False

# ══════════════════════════════════════════════
# ⚙️  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN        = os.environ.get('BOT_TOKEN', '')
GROQ_API_KEY     = os.environ.get('GROQ_API_KEY', '')
CHANNEL_USERNAME = os.environ.get('CHANNEL_USERNAME', '@your_channel')
RENDER_URL       = os.environ.get('RENDER_URL', '')
SUBDL_API_KEY    = os.environ.get('SUBDL_API_KEY', '')
OMDB_API_KEY     = os.environ.get('OMDB_API_KEY', '')
ADMIN_IDS_STR    = os.environ.get('ADMIN_IDS', '')

ADMIN_IDS = set()
for _a in ADMIN_IDS_STR.split(','):
    try: ADMIN_IDS.add(int(_a.strip()))
    except: pass

WELCOME_TOKENS = 50
DAILY_TOKENS   = 10
REF_REFERRER   = 25
REF_REFEREE    = 15
PREMIUM_THRESH = 100

COST = {
    'srt_per_500lines': 2,
    'youtube':          8,
    'audio_transcribe': 5,
    'audio_translate':  8,
    'chat_per_msg':     1,
}

PROMO_CODES = {
    'PREMIUMBOT': {'tokens': 50, 'desc': '🎁 Premium Welcome Pack'},
    'SUBTITLE50': {'tokens': 50, 'desc': '🎬 Subtitle Special'},
    'FREEBONUS':  {'tokens': 30, 'desc': '🆓 Free Bonus'},
}

SRC_LANGS = {
    'auto':'🔍 Auto','en':'🇺🇸 English','hi':'🇮🇳 Hindi',
    'ko':'🇰🇷 Korean','ja':'🇯🇵 Japanese','ar':'🇸🇦 Arabic',
    'fr':'🇫🇷 French','de':'🇩🇪 German','zh':'🇨🇳 Chinese',
    'es':'🇪🇸 Spanish','ru':'🇷🇺 Russian',
}
DST_LANGS   = {'bn':'🇧🇩 বাংলা','en':'🇺🇸 English','hi':'🇮🇳 Hindi'}
AUDIO_EXTS  = ('.mp3','.mp4','.wav','.m4a','.ogg','.webm','.oga','.flac')
MAX_AUDIO   = 25*1024*1024
DB_PATH     = '/tmp/subtitle_bot.db'

MAX_VIDEO_SIZE  = 50 * 1024 * 1024   # 50MB Telegram limit

logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

groq_client   = Groq(api_key=GROQ_API_KEY)
executor      = ThreadPoolExecutor(max_workers=8)
active_tasks  = {}
cancel_events = {}
chat_mode     = {}
chat_history  = {}
pending_audio = {}
user_state    = {}
image_tasks   = {}   # {uid: cancel_flag}

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
        uid INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
        tokens INTEGER DEFAULT 0,
        total_translations INTEGER DEFAULT 0, total_lines INTEGER DEFAULT 0,
        join_date TEXT, referral_code TEXT UNIQUE, referred_by INTEGER,
        last_daily TEXT, is_banned INTEGER DEFAULT 0,
        from_lang TEXT DEFAULT 'auto', to_lang TEXT DEFAULT 'bn',
        used_promos TEXT DEFAULT ''
    );
    CREATE TABLE IF NOT EXISTS history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        uid INTEGER, file_name TEXT, line_count INTEGER,
        from_lang TEXT, to_lang TEXT, cost INTEGER, ts TEXT
    );
    CREATE TABLE IF NOT EXISTS referrals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        referrer INTEGER, referee INTEGER, ts TEXT
    );
    ''')
    con.commit(); con.close()
    logger.info("✅ DB ready")

def _now(): return datetime.now().isoformat()
def _rcode(uid): return "R"+hashlib.md5(f"sub{uid}bd".encode()).hexdigest()[:7].upper()

def get_user(uid, username=None, first_name=None, ref_code=None):
    con = db(); c = con.cursor()
    c.execute("SELECT * FROM users WHERE uid=?", (uid,))
    u = c.fetchone()
    if not u:
        tokens = WELCOME_TOKENS; referred_by = None
        if ref_code:
            c.execute("SELECT uid FROM users WHERE referral_code=?", (ref_code,))
            r = c.fetchone()
            if r and r['uid'] != uid:
                referred_by = r['uid']
                tokens += REF_REFEREE
                con.execute("UPDATE users SET tokens=tokens+? WHERE uid=?", (REF_REFERRER, referred_by))
                con.execute("INSERT INTO referrals VALUES(NULL,?,?,?)", (referred_by, uid, _now()))
        con.execute(
            'INSERT INTO users (uid,username,first_name,tokens,join_date,referral_code,referred_by) VALUES(?,?,?,?,?,?,?)',
            (uid,username,first_name,tokens,_now(),_rcode(uid),referred_by))
        con.commit()
        c.execute("SELECT * FROM users WHERE uid=?", (uid,))
        u = c.fetchone()
    else:
        con.execute("UPDATE users SET username=?,first_name=? WHERE uid=?", (username,first_name,uid))
        con.commit()
    result = dict(u); con.close(); return result

def add_tokens(uid, n):
    con = db(); con.execute("UPDATE users SET tokens=tokens+? WHERE uid=?", (n,uid))
    con.commit(); con.close()

def deduct_tokens(uid, n) -> bool:
    con = db(); c = con.cursor()
    c.execute("SELECT tokens FROM users WHERE uid=?", (uid,))
    row = c.fetchone()
    if not row or row['tokens'] < n: con.close(); return False
    con.execute("UPDATE users SET tokens=tokens-? WHERE uid=?", (n,uid))
    con.commit(); con.close(); return True

def get_tokens(uid):
    con = db(); c = con.cursor()
    c.execute("SELECT tokens FROM users WHERE uid=?", (uid,))
    r = c.fetchone(); con.close()
    return r['tokens'] if r else 0

def claim_daily(uid):
    con = db(); c = con.cursor()
    c.execute("SELECT last_daily FROM users WHERE uid=?", (uid,))
    row = c.fetchone()
    if row and row['last_daily']:
        last = datetime.fromisoformat(row['last_daily'])
        diff = datetime.now()-last
        if diff < timedelta(hours=24):
            left = int((timedelta(hours=24)-diff).total_seconds()/3600)
            con.close(); return False, left
    con.execute("UPDATE users SET tokens=tokens+?,last_daily=? WHERE uid=?", (DAILY_TOKENS,_now(),uid))
    con.commit(); con.close(); return True, 0

def use_promo(uid, code):
    code = code.upper().strip()
    if code not in PROMO_CODES: return False, "❌ কোড সঠিক নয়!"
    con = db(); c = con.cursor()
    c.execute("SELECT used_promos FROM users WHERE uid=?", (uid,))
    row = c.fetchone()
    used = set((row['used_promos'] or '').split(',')) if row else set()
    if code in used: con.close(); return False, "❌ এই code আগেই ব্যবহার করেছ!"
    promo = PROMO_CODES[code]; used.add(code)
    con.execute("UPDATE users SET tokens=tokens+?,used_promos=? WHERE uid=?",
                (promo['tokens'],','.join(filter(None,used)),uid))
    con.commit(); con.close()
    return True, f"✅ {promo['desc']}\n\n🎁 *+{promo['tokens']} tokens* পেয়েছ!"

def log_history(uid, fname, lines, fl, tl, cost):
    con = db()
    con.execute("INSERT INTO history VALUES(NULL,?,?,?,?,?,?,?)", (uid,fname,lines,fl,tl,cost,_now()))
    con.execute("UPDATE users SET total_translations=total_translations+1,total_lines=total_lines+? WHERE uid=?", (lines,uid))
    con.commit(); con.close()

def get_history(uid, limit=5):
    con = db(); c = con.cursor()
    c.execute("SELECT * FROM history WHERE uid=? ORDER BY ts DESC LIMIT ?", (uid,limit))
    rows = [dict(r) for r in c.fetchall()]; con.close(); return rows

def get_stats():
    con = db(); c = con.cursor()
    c.execute("SELECT COUNT(*) t FROM users"); tu = c.fetchone()['t']
    c.execute("SELECT COUNT(*) t FROM users WHERE is_banned=0"); au = c.fetchone()['t']
    c.execute("SELECT COALESCE(SUM(total_translations),0) t FROM users"); tt = c.fetchone()['t']
    c.execute("SELECT COALESCE(SUM(total_lines),0) t FROM users"); tl = c.fetchone()['t']
    con.close(); return {'users':tu,'active':au,'translations':tt,'lines':tl}

def all_uids():
    con = db(); c = con.cursor()
    c.execute("SELECT uid FROM users WHERE is_banned=0")
    ids = [r['uid'] for r in c.fetchall()]; con.close(); return ids

def ban_user(uid, banned):
    con = db(); con.execute("UPDATE users SET is_banned=? WHERE uid=?", (1 if banned else 0,uid))
    con.commit(); con.close()

def is_banned(uid):
    con = db(); c = con.cursor()
    c.execute("SELECT is_banned FROM users WHERE uid=?", (uid,))
    r = c.fetchone(); con.close(); return bool(r and r['is_banned'])

def set_lang(uid, from_lang=None, to_lang=None):
    con = db()
    if from_lang: con.execute("UPDATE users SET from_lang=? WHERE uid=?", (from_lang,uid))
    if to_lang:   con.execute("UPDATE users SET to_lang=? WHERE uid=?",   (to_lang,uid))
    con.commit(); con.close()

def ref_count(uid):
    con = db(); c = con.cursor()
    c.execute("SELECT COUNT(*) t FROM referrals WHERE referrer=?", (uid,))
    n = c.fetchone()['t']; con.close(); return n

def calc_srt_cost(lines: int) -> int:
    if lines <= 500: return 0
    return max(1, (lines // 500) * COST['srt_per_500lines'])

# ══════════════════════════════════════════════
# 🌐  FLASK
# ══════════════════════════════════════════════
flask_app = Flask(__name__)

@flask_app.route('/')
def web_home():
    return """<!DOCTYPE html><html><head><title>SRT Ultra Bot</title>
<meta charset="UTF-8"><style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:'Segoe UI',sans-serif;background:linear-gradient(135deg,#0f0e17,#1a1a2e);
     color:#fff;display:flex;justify-content:center;align-items:center;
     min-height:100vh;flex-direction:column;gap:16px}
.card{background:rgba(255,255,255,.05);border:1px solid rgba(255,137,6,.35);
      border-radius:20px;padding:36px 56px;text-align:center}
h1{color:#ff8906;font-size:2.3em;margin-bottom:8px}
.dot{width:13px;height:13px;background:#00d4aa;border-radius:50%;
     display:inline-block;animation:p 1.5s infinite;box-shadow:0 0 8px #00d4aa}
@keyframes p{0%,100%{opacity:1}50%{opacity:.35}}
p{color:#a7a9be;font-size:1em;line-height:1.9}
.b{display:inline-block;background:rgba(255,137,6,.13);border:1px solid #ff8906;
   color:#ff8906;padding:4px 12px;border-radius:16px;font-size:.85em;margin:3px}
</style></head><body>
<div class="card">
  <h1>🎬 SRT Ultra Bot</h1>
  <p><span class="dot"></span>&nbsp;<span style="color:#00d4aa;font-weight:700">Bot is Live!</span></p>
  <p>Subtitle · YouTube DL · Tokens · Admin</p><br>
  <div>
    <span class="b">🤖 Groq AI</span><span class="b">🎙 Whisper</span>
    <span class="b">📥 Cobalt DL</span><span class="b">🪙 Tokens</span>
  </div>
</div></body></html>""", 200

@flask_app.route('/ping')
def ping(): return 'pong', 200

def run_flask():
    flask_app.run(host='0.0.0.0', port=int(os.environ.get('PORT',10000)), use_reloader=False)

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
def parse_srt(content):
    content = content.replace('\r\n','\n').replace('\r','\n')
    blocks, pat = [], re.compile(
        r'(\d+)\n(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})\n'
        r'((?:.+\n?)+?)(?=\n\d+\n|\Z)', re.MULTILINE)
    for m in pat.finditer(content.strip()+'\n\n'):
        txt = m.group(4).strip()
        if txt:
            blocks.append({'index':m.group(1),'start':m.group(2),'end':m.group(3),'text':txt})
    return blocks

def parse_vtt(content):
    content = content.replace('\r\n','\n').replace('\r','\n')
    blocks, idx = [], 1
    lines = content.split('\n'); i = 0
    while i < len(lines) and '-->' not in lines[i]: i += 1
    pat = re.compile(r'(\d{2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[.,]\d{3})')
    while i < len(lines):
        m = pat.search(lines[i].strip())
        if m:
            start = m.group(1).replace('.',','); end = m.group(2).replace('.',',')
            i += 1; tlines = []
            while i < len(lines) and lines[i].strip():
                tlines.append(lines[i].strip()); i += 1
            txt = re.sub(r'<[^>]+>','','\n'.join(tlines)).strip()
            if txt:
                blocks.append({'index':str(idx),'start':start,'end':end,'text':txt}); idx += 1
        else: i += 1
    return blocks

def parse_ass(content):
    blocks, idx = [], 1
    pat = re.compile(
        r'^Dialogue:.*?(\d:\d{2}:\d{2}\.\d{2}),(\d:\d{2}:\d{2}\.\d{2}),[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,[^,]*,(.*)',
        re.MULTILINE)
    def at(t):
        p = t.split(':'); h=p[0].zfill(2); m=p[1].zfill(2)
        s,ms = p[2].replace('.',',').split(',')
        return f"{h}:{m}:{s.zfill(2)},{ms.ljust(3,'0')}"
    for match in pat.finditer(content):
        txt = re.sub(r'\{[^}]*\}','',match.group(3)).replace('\\N','\n').strip()
        if txt:
            blocks.append({'index':str(idx),'start':at(match.group(1)),'end':at(match.group(2)),'text':txt}); idx += 1
    return blocks

def parse_auto(content, filename):
    fn = filename.lower()
    if fn.endswith('.vtt'): return parse_vtt(content)
    if fn.endswith(('.ass','.ssa')): return parse_ass(content)
    return parse_srt(content)

def build_srt(blocks):
    return '\n\n'.join(
        f"{b['index']}\n{b['start']} --> {b['end']}\n{b['text']}"
        for b in blocks) + '\n'

def fix_timing(blocks, offset_sec):
    def shift(ts):
        h,m,s_ms = ts.split(':'); s,ms = s_ms.split(',')
        total = (int(h)*3600+int(m)*60+int(s))*1000+int(ms)+int(offset_sec*1000)
        total = max(0,total); h2,r=divmod(total,3600000); m2,r=divmod(r,60000); s2,ms2=divmod(r,1000)
        return f"{h2:02d}:{m2:02d}:{s2:02d},{ms2:03d}"
    return [dict(b,start=shift(b['start']),end=shift(b['end'])) for b in blocks]

def merge_subtitles(b1, b2):
    def t2ms(ts):
        h,m,s_ms=ts.split(':'); s,ms=s_ms.split(',')
        return (int(h)*3600+int(m)*60+int(s))*1000+int(ms)
    all_b = sorted(b1+b2, key=lambda x: t2ms(x['start']))
    for i,b in enumerate(all_b,1): b['index']=str(i)
    return all_b

# ══════════════════════════════════════════════
# 📊  PIE CHART
# ══════════════════════════════════════════════
def pie_chart(done, total):
    pct = (done/total*100) if total>0 else 0
    rem = max(total-done,0)
    fig, ax = plt.subplots(figsize=(7,5.5))
    fig.patch.set_facecolor('#0f0e17'); ax.set_facecolor('#0f0e17')
    if done==0: sizes,colors,labels=[100],['#2d2d44'],['Waiting...']
    elif done>=total: sizes,colors,labels=[100],['#00d4aa'],['Completed ✓']
    else:
        sizes=[done,rem]; colors=['#00d4aa','#2d2d44']
        labels=[f'Done ({done})',f'Left ({rem})']
    explode = ([0.05,0] if len(sizes)==2 else [0])
    _,_,ats = ax.pie(sizes,explode=explode,colors=colors,autopct='%1.1f%%',
                     startangle=90,pctdistance=0.65,
                     wedgeprops={'linewidth':2.5,'edgecolor':'#0f0e17'},shadow=True)
    for at in ats: at.set_color('white'); at.set_fontsize(13); at.set_fontweight('bold')
    ax.text(0,0,f'{pct:.1f}%',ha='center',va='center',fontsize=26,fontweight='bold',color='white')
    patches=[mpatches.Patch(color=colors[i],label=labels[i]) for i in range(len(labels))]
    ax.legend(handles=patches,loc='lower center',bbox_to_anchor=(.5,-.13),ncol=2,
              facecolor='#1e1e2e',edgecolor='#444466',labelcolor='white',fontsize=10)
    ax.set_title('Translation Progress',color='#ff8906',fontsize=15,fontweight='bold',pad=18)
    fig.text(.5,.01,f'Total:{total}  Done:{done}  Left:{rem}',ha='center',color='#a7a9be',fontsize=9)
    plt.tight_layout()
    buf=io.BytesIO(); plt.savefig(buf,format='png',dpi=110,bbox_inches='tight',facecolor='#0f0e17')
    buf.seek(0); plt.close(fig); return buf

# ══════════════════════════════════════════════
# 🤖  TRANSLATION
# ══════════════════════════════════════════════
def _iq(e): return any(k in str(e).lower() for k in ['quota','limit exceeded','402','billing'])
def _ir(e): return 'rate_limit' in str(e).lower() or '429' in str(e)

def _tsys(fl, tl):
    src = SRC_LANGS.get(fl,fl) if fl!='auto' else 'any language'
    dst = DST_LANGS.get(tl,tl)
    return (f"You are a professional subtitle translator.\n"
            f"Translate {src} subtitles to {dst}.\n"
            f"Rules:\n- Translate meaning, NOT word-by-word\n"
            f"- Use natural conversational style\n- Keep emotion and tone\n"
            f"- Return ONLY the translation, nothing else")

def t1(text, fl='auto', tl='bn', ce=None):
    for _ in range(3):
        if ce and ce.is_set(): return text
        try:
            r = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role":"system","content":_tsys(fl,tl)},
                          {"role":"user","content":f"Translate:\n{text}"}],
                temperature=0.15, max_tokens=256)
            return r.choices[0].message.content.strip()
        except Exception as e:
            if _iq(e): raise Exception("QUOTA_EXCEEDED")
            elif _ir(e):
                for _ in range(60):
                    if ce and ce.is_set(): return text
                    time.sleep(1)
            else: time.sleep(3)
    return text

def tbatch(texts, fl='auto', tl='bn', ce=None):
    if ce and ce.is_set(): return texts
    numbered = '\n'.join(f"[{i+1}] {t}" for i,t in enumerate(texts))
    msg = (f"Translate the following {len(texts)} subtitle lines.\n"
           f"Keep number prefix [1],[2]...\nReturn ONLY translations.\n\n{numbered}\n\nTranslation:")
    translated = [None]*len(texts)
    for att in range(3):
        if ce and ce.is_set(): return texts
        try:
            resp = groq_client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[{"role":"system","content":_tsys(fl,tl)},
                          {"role":"user","content":msg}],
                temperature=0.15, max_tokens=3000)
            raw = resp.choices[0].message.content.strip()
            for m in re.finditer(r'\[(\d+)\]\s*(.*?)(?=\[\d+\]|\Z)',raw,re.DOTALL):
                idx=int(m.group(1))-1; val=m.group(2).strip()
                if 0<=idx<len(texts) and val: translated[idx]=val
            break
        except Exception as e:
            if _iq(e): raise Exception("QUOTA_EXCEEDED")
            elif _ir(e):
                for _ in range(60):
                    if ce and ce.is_set(): return texts
                    time.sleep(1)
            else: time.sleep(5)
    for i,v in enumerate(translated):
        if v is None:
            if ce and ce.is_set(): return texts
            translated[i] = t1(texts[i],fl,tl,ce)
    return translated

# ══════════════════════════════════════════════
# 🎙  AUDIO TRANSCRIPTION
# ══════════════════════════════════════════════
def transcribe(audio_bytes, filename, language='en'):
    suffix = os.path.splitext(filename)[-1] or '.mp3'
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(audio_bytes); tmp_path = tmp.name
    try:
        with open(tmp_path,'rb') as f:
            params = dict(file=(filename,f,'audio/mpeg'),
                          model="whisper-large-v3-turbo",
                          response_format="text", temperature=0.0)
            if language != 'auto': params['language'] = language
            r = groq_client.audio.transcriptions.create(**params)
        return r.strip() if isinstance(r,str) else r.text.strip()
    except Exception as e:
        if _iq(e): raise Exception("QUOTA_EXCEEDED")
        raise e
    finally: os.unlink(tmp_path)

def trans_plain(text, tl='bn'):
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role":"system","content":(
                    f"You are a professional translator. "
                    f"Translate to {DST_LANGS.get(tl,'Bengali')} naturally. "
                    f"Return ONLY the translation.")},
                {"role":"user","content":f"Translate:\n{text}"}],
            temperature=0.15, max_tokens=4096)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        if _iq(e): raise Exception("QUOTA_EXCEEDED")
        raise e

# ══════════════════════════════════════════════
# 📥  yt-dlp দিয়ে YouTube Video/Audio Download
# ══════════════════════════════════════════════
def yt_download_video(yt_url: str, quality: str = "720") -> dict:
    """
    yt-dlp দিয়ে video download করো।
    Returns: {'data': bytes, 'filename': str, 'size': int, 'title': str}
    """
    if not YT_AVAILABLE:
        return {'error': 'yt-dlp install নেই।'}
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_tmpl = os.path.join(tmpdir, '%(title).40s.%(ext)s')
            ydl_opts = {
                'format': (
                    f'bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]'
                    f'/best[height<={quality}][ext=mp4]'
                    f'/best[height<={quality}]'
                    f'/best'
                ),
                'outtmpl':        out_tmpl,
                'quiet':          True,
                'no_warnings':    True,
                'noplaylist':     True,
                'merge_output_format': 'mp4',
                'max_filesize':   MAX_VIDEO_SIZE,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(yt_url, download=True)
                title = info.get('title', 'video')[:50]

            # ডাউনলোড হওয়া ফাইল খোঁজো
            for fname in os.listdir(tmpdir):
                fpath = os.path.join(tmpdir, fname)
                fsize = os.path.getsize(fpath)
                if fsize > MAX_VIDEO_SIZE:
                    return {'error': f'ফাইল সাইজ {fsize//(1024*1024)}MB — 50MB-এর বেশি!'}
                with open(fpath, 'rb') as f:
                    data = f.read()
                return {
                    'data':     data,
                    'filename': fname,
                    'size':     fsize,
                    'title':    title,
                }
    except Exception as e:
        err = str(e)
        if 'filesize' in err.lower() or 'too large' in err.lower():
            return {'error': 'ফাইল সাইজ 50MB-এর বেশি! ছোট ভিডিও দাও।'}
        logger.error(f"yt-dlp video error: {e}")
        return {'error': f'Download failed: {str(e)[:100]}'}
    return {'error': 'কোনো ফাইল পাওয়া যায়নি।'}


def yt_download_audio(yt_url: str) -> dict:
    """
    yt-dlp দিয়ে audio (MP3) download করো।
    Returns: {'data': bytes, 'filename': str, 'size': int, 'title': str}
    """
    if not YT_AVAILABLE:
        return {'error': 'yt-dlp install নেই।'}
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            out_tmpl = os.path.join(tmpdir, '%(title).40s.%(ext)s')
            ydl_opts = {
                'format':         'bestaudio/best',
                'outtmpl':        out_tmpl,
                'quiet':          True,
                'no_warnings':    True,
                'noplaylist':     True,
                'postprocessors': [{
                    'key':            'FFmpegExtractAudio',
                    'preferredcodec': 'mp3',
                    'preferredquality': '192',
                }],
                'max_filesize':   MAX_VIDEO_SIZE,
            }
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(yt_url, download=True)
                    title = info.get('title', 'audio')[:50]
            except Exception:
                # FFmpeg নেই হলে raw audio নাও
                ydl_opts2 = {
                    'format':      'bestaudio/best',
                    'outtmpl':     out_tmpl,
                    'quiet':       True,
                    'no_warnings': True,
                    'noplaylist':  True,
                    'max_filesize': MAX_VIDEO_SIZE,
                }
                with yt_dlp.YoutubeDL(ydl_opts2) as ydl:
                    info = ydl.extract_info(yt_url, download=True)
                    title = info.get('title', 'audio')[:50]

            for fname in os.listdir(tmpdir):
                fpath = os.path.join(tmpdir, fname)
                fsize = os.path.getsize(fpath)
                if fsize > MAX_VIDEO_SIZE:
                    return {'error': f'ফাইল সাইজ {fsize//(1024*1024)}MB — 50MB-এর বেশি!'}
                with open(fpath, 'rb') as f:
                    data = f.read()
                return {
                    'data':     data,
                    'filename': fname,
                    'size':     fsize,
                    'title':    title,
                }
    except Exception as e:
        logger.error(f"yt-dlp audio error: {e}")
        return {'error': f'Download failed: {str(e)[:100]}'}
    return {'error': 'কোনো ফাইল পাওয়া যায়নি।'}

# ══════════════════════════════════════════════
# ▶️  YOUTUBE SUBTITLE
# ══════════════════════════════════════════════
def yt_subtitle(url, lang='en'):
    if not YT_AVAILABLE: return None, None
    with tempfile.TemporaryDirectory() as tmpdir:
        opts = {'writesubtitles':True,'writeautomaticsub':True,
                'subtitleslangs':[lang,'en'],'skip_download':True,
                'outtmpl':os.path.join(tmpdir,'%(title)s'),
                'subtitlesformat':'vtt/srt','quiet':True,'no_warnings':True}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title = info.get('title','video')[:50]
            for f in os.listdir(tmpdir):
                if f.endswith(('.srt','.vtt')):
                    with open(os.path.join(tmpdir,f),'r',encoding='utf-8',errors='ignore') as sf:
                        return sf.read(), title
        except Exception as e: logger.error(f"YT subtitle: {e}")
    return None, None

# ══════════════════════════════════════════════
# 🎬  MOVIE INFO (OMDB + Spell Fix)
# ══════════════════════════════════════════════
def fix_movie_name(query: str) -> str:
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role":"system","content":(
                    "You are a movie title spell checker. "
                    "Given a possibly misspelled movie title, return the correct movie title. "
                    "Return ONLY the corrected title, nothing else.")},
                {"role":"user","content":f"Correct this movie title: {query}"}],
            temperature=0.1, max_tokens=50)
        return resp.choices[0].message.content.strip()
    except: return query

def get_movie_info(title: str) -> dict:
    if not OMDB_API_KEY: return {}
    try:
        r = requests.get("http://www.omdbapi.com/",
                         params={'apikey':OMDB_API_KEY,'t':title,'type':'movie'}, timeout=10)
        if r.status_code==200:
            data = r.json()
            if data.get('Response')=='True': return data
        r2 = requests.get("http://www.omdbapi.com/",
                          params={'apikey':OMDB_API_KEY,'t':title,'type':'series'}, timeout=10)
        if r2.status_code==200:
            data2 = r2.json()
            if data2.get('Response')=='True': return data2
    except: pass
    return {}

def download_poster(url: str):
    if not url or url=='N/A': return None
    try:
        r = requests.get(url, timeout=10)
        if r.status_code==200: return r.content
    except: pass
    return None


# ══════════════════════════════════════════════
# 🖼️  IMAGE SEARCH + PDF CREATOR
# ══════════════════════════════════════════════
def search_images(query: str, count: int) -> list:
    """Pixabay API দিয়ে ছবির URL খোঁজো"""
    if not PIXABAY_API_KEY:
        logger.warning("PIXABAY_API_KEY not set")
        return []
    urls = []
    try:
        r = requests.get(
            "https://pixabay.com/api/",
            params={
                "key":        PIXABAY_API_KEY,
                "q":          query,
                "image_type": "photo",
                "per_page":   min(count * 2, 40),
                "safesearch": "true",
                "order":      "popular",
            },
            timeout=15
        )
        if r.status_code == 200:
            hits = r.json().get("hits", [])
            for h in hits:
                url = h.get("largeImageURL") or h.get("webformatURL", "")
                if url:
                    urls.append(url)
                if len(urls) >= count * 2:
                    break
        else:
            logger.error(f"Pixabay API error: {r.status_code} {r.text[:100]}")
    except Exception as e:
        logger.error(f"Pixabay search error: {e}")
    return urls


def download_image(url: str, timeout: int = 10):
    """একটি ছবি download করো, bytes ফেরত দাও"""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        r = requests.get(url, timeout=timeout, headers=headers, stream=True)
        if r.status_code == 200:
            ct = r.headers.get('Content-Type','')
            if 'image' in ct or ct == '':
                data = r.content
                if len(data) > 1000:   # tiny placeholder বাদ
                    return data
    except Exception:
        pass
    return None


def create_pdf_from_images(
    image_bytes_list: list,
    topic: str,
) -> bytes:
    """
    ছবির list থেকে PDF তৈরি করো।
    প্রতিটি ছবি একটি page-এ।
    """
    from fpdf import FPDF
    import struct, zlib

    pdf = FPDF(orientation='P', unit='mm', format='A4')
    pdf.set_auto_page_break(False)

    A4_W, A4_H = 210, 297
    MARGIN = 10

    for img_bytes in image_bytes_list:
        try:
            # ছবি format detect করো
            buf = io.BytesIO(img_bytes)
            if img_bytes[:3] == b'\xff\xd8\xff':
                ext = 'jpg'
            elif img_bytes[:8] == b'\x89PNG\r\n\x1a\n':
                ext = 'png'
            elif img_bytes[:6] in (b'GIF87a', b'GIF89a'):
                ext = 'gif'
            elif img_bytes[:4] == b'RIFF' and img_bytes[8:12] == b'WEBP':
                ext = 'webp'
            else:
                ext = 'jpg'

            # WEBP → JPEG convert (fpdf doesn't support webp)
            if ext == 'webp':
                try:
                    from PIL import Image as PilImage
                    pil_img = PilImage.open(io.BytesIO(img_bytes)).convert('RGB')
                    new_buf = io.BytesIO()
                    pil_img.save(new_buf, format='JPEG', quality=85)
                    buf = new_buf; ext = 'jpg'
                except Exception:
                    continue

            # Image dimensions
            try:
                from PIL import Image as PilImage
                pil_img = PilImage.open(buf)
                iw, ih = pil_img.size
                buf.seek(0)
            except Exception:
                iw, ih = A4_W, A4_H

            # Scale to fit A4
            avail_w = A4_W - 2*MARGIN
            avail_h = A4_H - 2*MARGIN
            scale   = min(avail_w/iw, avail_h/ih)
            fw, fh  = iw*scale, ih*scale
            x = MARGIN + (avail_w - fw)/2
            y = MARGIN + (avail_h - fh)/2

            pdf.add_page()
            pdf.image(buf, x=x, y=y, w=fw, h=fh)
        except Exception:
            continue

    result = pdf.output()
    # fpdf2 version অনুযায়ী output type আলাদা:
    # purano: str → encode করো
    # নতুন: bytes বা bytearray → সরাসরি bytes() করো
    if isinstance(result, (bytes, bytearray)):
        return bytes(result)
    return result.encode('latin-1')

# ══════════════════════════════════════════════
# 🔍  SUBDL
# ══════════════════════════════════════════════
def subdl_search(query):
    try:
        r = requests.get("https://api.subdl.com/api/v1/subtitles",
                         params={"api_key":SUBDL_API_KEY,"film_name":query,
                                 "languages":"EN","subs_per_page":8}, timeout=15)
        if r.status_code!=200: return []
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
CHAT_SYS = "তুমি একটি বন্ধুত্বপূর্ণ AI assistant। বাংলায় কথা বলো। স্বাভাবিক ভাষায় উত্তর দাও।"

def ai_chat(uid, text):
    if uid not in chat_history: chat_history[uid]=[]
    chat_history[uid].append({"role":"user","content":text})
    if len(chat_history[uid])>20: chat_history[uid]=chat_history[uid][-20:]
    try:
        resp = groq_client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[{"role":"system","content":CHAT_SYS}]+chat_history[uid],
            temperature=0.7, max_tokens=1024)
        reply = resp.choices[0].message.content.strip()
        chat_history[uid].append({"role":"assistant","content":reply})
        return reply
    except Exception as e:
        if _iq(e): return "⚠️ API limit শেষ।"
        return "❌ সমস্যা হয়েছে।"

# ══════════════════════════════════════════════
# 🔒  ACCESS CHECK
# ══════════════════════════════════════════════
async def is_member(uid, bot):
    try:
        m = await bot.get_chat_member(CHANNEL_USERNAME, uid)
        return m.status in ['member','administrator','creator']
    except: return False

NOT_JOINED = ("🔒 *চ্যানেল Membership নেই!*\n\n"
              "বট ব্যবহার করতে চ্যানেলে যোগ দাও।\n"
              "Leave নিলে সাথে সাথে access বন্ধ।")
BANNED    = "🚫 *তোমাকে ban করা হয়েছে।*"
QUOTA_MSG = ("⚠️ *Groq API Limit শেষ!*\n\n"
             "২৪ ঘণ্টা পরে চেষ্টা করো।\n"
             "[console.groq.com](https://console.groq.com)")

async def check_access(uid, bot, reply_fn) -> bool:
    if is_banned(uid):
        await reply_fn(BANNED, parse_mode='Markdown'); return False
    if not await is_member(uid, bot):
        ch = CHANNEL_USERNAME.lstrip('@')
        await reply_fn(NOT_JOINED, parse_mode='Markdown',
                       reply_markup=InlineKeyboardMarkup([
                           [InlineKeyboardButton("📢 চ্যানেলে যোগ দাও", url=f"https://t.me/{ch}")],
                           [InlineKeyboardButton("✅ চেক করো", callback_data="chk")]]))
        return False
    return True

def token_warn(cost, balance) -> str:
    return (f"🪙 *Token কম!*\n\n"
            f"এই কাজে লাগবে: *{cost} tokens*\n"
            f"তোমার আছে: *{balance} tokens*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"💡 *Token পেতে:*\n"
            f"• `/daily` — প্রতিদিন {DAILY_TOKENS} tokens\n"
            f"• `/referral` — বন্ধু আনো, {REF_REFERRER} tokens পাও\n"
            f"• `/promo PREMIUMBOT` — 50 tokens ফ্রি!")

def token_info_text():
    return (f"🪙 *Token খরচের তালিকা*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📁 *SRT/VTT/ASS অনুবাদ:*\n"
            f"   ≤500 লাইন → বিনামূল্যে 🆓\n"
            f"   প্রতি ৫০০ লাইনে → {COST['srt_per_500lines']} tokens\n\n"
            f"▶️ *YouTube Subtitle:* {COST['youtube']} tokens\n"
            f"📥 *YouTube Download:* ফ্রি 🆓\n"
            f"🎙 *Audio Transcription:* {COST['audio_transcribe']} tokens\n"
            f"🔄 *Audio + অনুবাদ:* {COST['audio_translate']} tokens\n"
            f"💬 *AI Chat:* {COST['chat_per_msg']} token/message\n"
            f"🛠 *Tools (Timing/Merge):* ফ্রি 🆓\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎁 *Token পাওয়ার উপায়:*\n"
            f"• Welcome: *{WELCOME_TOKENS}* | Daily: *{DAILY_TOKENS}*\n"
            f"• Referral: *{REF_REFERRER}* | Promo: `/promo PREMIUMBOT`")

# ══════════════════════════════════════════════
# 🎹  KEYBOARDS
# ══════════════════════════════════════════════
def kb_not_joined():
    ch = CHANNEL_USERNAME.lstrip('@')
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 চ্যানেলে যোগ দাও", url=f"https://t.me/{ch}")],
        [InlineKeyboardButton("✅ যোগ দিয়েছি — চেক করো", callback_data="chk")]])

def kb_home():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👤  প্রোফাইল",       callback_data="profile"),
         InlineKeyboardButton("📖  সাহায্য",         callback_data="help")],
        [InlineKeyboardButton("🪙  Token দেখো",     callback_data="token_info"),
         InlineKeyboardButton("🤝  Refer & Earn",   callback_data="refer")],
        [InlineKeyboardButton("🎁  Daily Token",    callback_data="daily"),
         InlineKeyboardButton("🌐  ভাষা সেটিং",     callback_data="lang_menu")],
        [InlineKeyboardButton("▬▬▬▬▬  Features  ▬▬▬▬▬", callback_data="noop")],
        [InlineKeyboardButton("🔍  Subtitle খোঁজো", callback_data="search")],
        [InlineKeyboardButton("📥  YouTube ডাউনলোড  🆓", callback_data="yt_download")],
        [InlineKeyboardButton("🖼️  ছবি থেকে PDF বানাও", callback_data="img_pdf")],
        [InlineKeyboardButton("🛠  Subtitle Tools", callback_data="tools_menu"),
         InlineKeyboardButton("🎙  Audio → Text",   callback_data="audio_info")],
        [InlineKeyboardButton("💬  AI-এর সাথে কথা বলো  🤖", callback_data="chat_start")],
    ])

def kb_back():
    return InlineKeyboardMarkup([[InlineKeyboardButton("🔙  হোম", callback_data="home")]])

def kb_cancel(uid):
    return InlineKeyboardMarkup([[InlineKeyboardButton("⛔  অনুবাদ বাতিল করো", callback_data=f"cancel_{uid}")]])

def kb_chat():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑  কথোপকথন মুছো",  callback_data="chat_clear"),
         InlineKeyboardButton("🔙  চ্যাট বন্ধ করো", callback_data="chat_stop")]])

def kb_quota():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔑  Groq Console খোলো", url="https://console.groq.com")],
        [InlineKeyboardButton("🔙  হোম",                callback_data="home")]])

def kb_audio(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🇧🇩  বাংলায় Transcription  ({COST['audio_transcribe']}🪙)", callback_data=f"tr_bn_{uid}")],
        [InlineKeyboardButton(f"🇺🇸  English Transcription  ({COST['audio_transcribe']}🪙)", callback_data=f"tr_en_{uid}")],
        [InlineKeyboardButton(f"🔄  Transcription + অনুবাদ  ({COST['audio_translate']}🪙)", callback_data=f"tr_translate_{uid}")],
        [InlineKeyboardButton("🔙  হোম", callback_data="home")]])

def kb_src_lang():
    rows = []
    items = list(SRC_LANGS.items())
    for i in range(0, len(items), 2):
        row = [InlineKeyboardButton(name, callback_data=f"set_src_{code}")
               for code,name in items[i:i+2]]
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
        [InlineKeyboardButton("⏱  Timing Fix — সময় ঠিক করো", callback_data="tool_timing")],
        [InlineKeyboardButton("🔀  Merge — দুটো ফাইল এক করো", callback_data="tool_merge")],
        [InlineKeyboardButton("🔙  হোম",                        callback_data="home")]])

def kb_admin():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊  Statistics",   callback_data="adm_stats"),
         InlineKeyboardButton("📢  Broadcast",    callback_data="adm_broadcast")],
        [InlineKeyboardButton("👤  User Lookup",  callback_data="adm_lookup"),
         InlineKeyboardButton("🪙  Tokens দাও",  callback_data="adm_tokens")],
        [InlineKeyboardButton("🚫  Ban",          callback_data="adm_ban"),
         InlineKeyboardButton("✅  Unban",         callback_data="adm_unban")],
        [InlineKeyboardButton("🔙  হোম",           callback_data="home")]])

def kb_yt_choice(url: str):
    """YouTube link দিলে কী করবে জিজ্ঞেস করার keyboard"""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📥  Video ডাউনলোড  🆓",  callback_data="yt_download")],
        [InlineKeyboardButton("🎵  MP3 Audio  🆓",       callback_data="yt_dl_audio")],
        [InlineKeyboardButton("🔙  হোম",                callback_data="home")]])

# ══════════════════════════════════════════════
# /start
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user; args = ctx.args or []
    ref = args[0] if args else None
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    user = get_user(u.id, u.username, u.first_name, ref)
    chat_mode[u.id] = False
    is_new = (datetime.fromisoformat(user['join_date']).date()==datetime.now().date()
              and user['total_translations']==0)
    badge  = "👑" if user['tokens']>=PREMIUM_THRESH else "🆓"
    extra  = ""
    if is_new:
        extra = f"\n\n🎁 *Welcome!* তোমার account-এ *{WELCOME_TOKENS} tokens* যোগ হয়েছে!"
        if user['referred_by']:
            extra += f"\n🤝 Referral bonus: আরো *{REF_REFEREE} tokens*!"
    await update.message.reply_text(
        f"🎬 *Subtitle BD Bot-এ স্বাগতম!* {badge}\n\n"
        f"হ্যালো *{u.first_name}* ভাই! 👋{extra}\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🪙 Token: *{user['tokens']}* | 🎖 {badge}\n\n"
        f"📌 ফাইল বা YouTube link পাঠাও 👇",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# COMMANDS
# ══════════════════════════════════════════════
async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    user = get_user(u.id); hist = get_history(u.id,5); refs = ref_count(u.id)
    badge = "👑 Premium" if user['tokens']>=PREMIUM_THRESH else "🆓 Free"
    ht = ""
    if hist:
        ht = "\n\n📋 *শেষ ৫টি অনুবাদ:*\n"
        for h in hist:
            ht += f"• `{h['file_name'][:22]}` {h['line_count']}লাইন (-{h['cost']}🪙) `{h['ts'][:10]}`\n"
    await update.message.reply_text(
        f"👤 *প্রোফাইল*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🏷 {user['first_name']} | 🆔 `{u.id}`\n"
        f"🎖 {badge} | 🪙 *{user['tokens']} tokens*\n\n"
        f"📊 অনুবাদ: *{user['total_translations']}* | লাইন: *{user['total_lines']}*\n"
        f"🤝 Referral: *{refs} জন*\n"
        f"🌐 {SRC_LANGS.get(user['from_lang'],'Auto')} → {DST_LANGS.get(user['to_lang'],'বাংলা')}"
        f"{ht}",
        parse_mode='Markdown', reply_markup=kb_back())

async def cmd_referral(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    user = get_user(u.id); refs = ref_count(u.id)
    bi = await ctx.bot.get_me()
    link = f"https://t.me/{bi.username}?start={user['referral_code']}"
    await update.message.reply_text(
        f"🤝 *Referral System*\n\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"তোমার Referral Link:\n`{link}`\n\n"
        f"📌 *কীভাবে কাজ করে:*\n"
        f"1️⃣ তুমি link শেয়ার করো\n"
        f"2️⃣ বন্ধু link-এ click করে বট open করে\n"
        f"3️⃣ Telegram bot-কে auto `{user['referral_code']}` পাঠায়\n"
        f"4️⃣ তুমি পাও: *{REF_REFERRER} tokens* 🎁\n"
        f"   বন্ধু পায়: *{WELCOME_TOKENS+REF_REFEREE} tokens* 🎁\n\n"
        f"📊 এখন পর্যন্ত: *{refs} জন* | উপার্জন: *{refs*REF_REFERRER} tokens*",
        parse_mode='Markdown', reply_markup=kb_back())

async def cmd_daily(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    ok, h = claim_daily(u.id); bal = get_tokens(u.id)
    if ok:
        await update.message.reply_text(
            f"🎁 *+{DAILY_TOKENS} tokens পেয়েছ!*\n\n💰 Balance: *{bal} tokens*",
            parse_mode='Markdown', reply_markup=kb_home())
    else:
        await update.message.reply_text(
            f"⏰ *{h} ঘণ্টা পরে আবার নাও।*\n💰 Balance: *{bal} tokens*",
            parse_mode='Markdown', reply_markup=kb_home())

async def cmd_promo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    args = ctx.args or []
    if not args:
        await update.message.reply_text(
            "🎁 *Promo Code*\n\nব্যবহার: `/promo CODE`\nউদাহরণ: `/promo PREMIUMBOT`",
            parse_mode='Markdown'); return
    ok, msg = use_promo(u.id, args[0])
    bal = get_tokens(u.id)
    await update.message.reply_text(
        f"{msg}\n\n💰 Balance: *{bal} tokens*",
        parse_mode='Markdown', reply_markup=kb_home())

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ তুমি admin নও।"); return
    s = get_stats()
    await update.message.reply_text(
        f"👑 *Admin Panel*\n\n"
        f"👥 Users: `{s['users']}` | ✅ Active: `{s['active']}`\n"
        f"🔄 Translations: `{s['translations']}` | 📝 Lines: `{s['lines']}`",
        parse_mode='Markdown', reply_markup=kb_admin())

# ══════════════════════════════════════════════
# 🎙  TRANSCRIPTION TASK
# ══════════════════════════════════════════════
async def do_transcription(uid, mode, bot, chat_id):
    info = pending_audio.get(uid)
    if not info:
        await bot.send_message(chat_id, "❌ অডিও নেই। আবার পাঠাও।"); return
    pending_audio.pop(uid, None)
    cost = COST['audio_translate'] if mode=='translate' else COST['audio_transcribe']
    if not deduct_tokens(uid, cost):
        bal = get_tokens(uid)
        await bot.send_message(chat_id, token_warn(cost,bal), parse_mode='Markdown',
                               reply_markup=kb_back()); return
    status = await bot.send_message(chat_id, "⏳ *Transcription চলছে...*", parse_mode='Markdown')
    try:
        f   = await bot.get_file(info['file_id'])
        raw = await f.download_as_bytearray()
        loop = asyncio.get_event_loop()
        lang = 'bn' if mode=='bn' else 'en'
        tr = await loop.run_in_executor(executor,
             functools.partial(transcribe, bytes(raw), info['file_name'], lang))
        if not tr or not tr.strip():
            await bot.edit_message_text("❌ কথা পাওয়া যায়নি!", chat_id=chat_id,
                message_id=status.message_id, reply_markup=kb_back())
            add_tokens(uid, cost); return
        bal = get_tokens(uid)
        if mode in ('bn','en'):
            flag = "🇧🇩" if mode=='bn' else "🇺🇸"
            text = f"✅ *{flag} Transcription সম্পন্ন!*\n🪙 -{cost} | Balance: *{bal}*\n\n━━━━━━━━\n{tr}\n━━━━━━━━"
            if len(text)>4096:
                await bot.edit_message_text("✅ সম্পন্ন! ফাইল পাঠাচ্ছি...",
                    chat_id=chat_id, message_id=status.message_id)
                await bot.send_document(chat_id, document=io.BytesIO(tr.encode()),
                    filename="transcript.txt", reply_markup=kb_home())
            else:
                await bot.edit_message_text(text, chat_id=chat_id,
                    message_id=status.message_id, parse_mode='Markdown', reply_markup=kb_home())
        else:
            await bot.edit_message_text("🔄 অনুবাদ করছি...",
                chat_id=chat_id, message_id=status.message_id)
            user = get_user(uid)
            bn = await loop.run_in_executor(executor,
                 functools.partial(trans_plain, tr, user['to_lang']))
            combined = f"🇺🇸 *English:*\n{tr}\n\n🇧🇩 *Translation:*\n{bn}"
            if len(combined)>4096:
                full = f"=== English ===\n{tr}\n\n=== Translation ===\n{bn}"
                await bot.edit_message_text("✅ সম্পন্ন!",
                    chat_id=chat_id, message_id=status.message_id)
                await bot.send_document(chat_id, document=io.BytesIO(full.encode()),
                    filename="transcript_translated.txt", reply_markup=kb_home())
            else:
                await bot.edit_message_text(
                    f"✅ *সম্পন্ন!* 🪙 -{cost} | Balance: *{bal}*\n\n{combined}",
                    chat_id=chat_id, message_id=status.message_id,
                    parse_mode='Markdown', reply_markup=kb_home())
    except Exception as e:
        err = str(e); add_tokens(uid, cost)
        notice = QUOTA_MSG if "QUOTA_EXCEEDED" in err else f"❌ সমস্যা: `{err[:150]}`"
        try:
            await bot.edit_message_text(notice, chat_id=chat_id,
                message_id=status.message_id, parse_mode='Markdown',
                reply_markup=kb_quota() if "QUOTA" in err else kb_back())
        except: pass

# ══════════════════════════════════════════════
# CALLBACK HANDLER
# ══════════════════════════════════════════════
async def cb_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    d   = q.data

    # ── No-auth ──
    if d == "chk":
        await q.answer()
        if await is_member(uid, ctx.bot):
            get_user(uid, q.from_user.username, q.from_user.first_name)
            await q.edit_message_text("✅ *যোগ দিয়েছ!* শুরু করো 🚀",
                                      parse_mode='Markdown', reply_markup=kb_home())
        else:
            await q.edit_message_text(NOT_JOINED, parse_mode='Markdown',
                                      reply_markup=kb_not_joined())
        return

    if d.startswith("cancel_"):
        await q.answer()
        target = int(d.split("_")[1])
        if uid==target and uid in active_tasks:
            active_tasks[uid] = True
            if uid in cancel_events: cancel_events[uid].set()
            try:
                await q.edit_message_caption(
                    caption="❌ *বাতিল!* নতুন ফাইল পাঠাও।",
                    parse_mode='Markdown', reply_markup=kb_home())
            except: pass
        else: await q.answer("কোনো সক্রিয় কাজ নেই!", show_alert=True)
        return

    # ── Audio ──
    if d.startswith("tr_"):
        if is_banned(uid): await q.answer(BANNED, show_alert=True); return
        parts  = d.split("_"); mode = parts[1]; target = int(parts[-1])
        if uid != target: await q.answer("এটা তোমার জন্য নয়!", show_alert=True); return
        cost = COST['audio_translate'] if mode=='translate' else COST['audio_transcribe']
        bal  = get_tokens(uid)
        if bal < cost:
            await q.answer(f"Token কম! লাগবে {cost}, আছে {bal}", show_alert=True)
            try: await q.edit_message_text(token_warn(cost,bal), parse_mode='Markdown', reply_markup=kb_back())
            except: pass
            return
        await q.answer()
        mode_txt = {"bn":"🇧🇩 বাংলায় Transcription","en":"🇺🇸 English Transcription",
                    "translate":"🔄 Transcription + অনুবাদ"}.get(mode,"")
        try: await q.edit_message_text(f"✅ *{mode_txt} শুরু...*\n⏳ অপেক্ষা করো...", parse_mode='Markdown')
        except: pass
        asyncio.create_task(do_transcription(uid, mode, ctx.bot, q.message.chat_id))
        return

    # ── Full access check ──
    if is_banned(uid): await q.answer("🚫 ban!", show_alert=True); return
    if not await is_member(uid, ctx.bot):
        await q.answer("🔒 চ্যানেলে যোগ দাও!", show_alert=True)
        try: await q.edit_message_text(NOT_JOINED, parse_mode='Markdown', reply_markup=kb_not_joined())
        except: pass
        return

    # ── noop (divider button) ──
    if d == "noop":
        await q.answer("", show_alert=False)
        return

    await q.answer()
    user = get_user(uid)

    # ── Refer ──
    if d == "refer":
        refs  = ref_count(uid)
        bi    = await ctx.bot.get_me()
        user2 = get_user(uid)
        link  = f"https://t.me/{bi.username}?start={user2['referral_code']}"
        msg_text = (
            f"🤝 *Refer & Earn*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"তোমার Referral Link:\n`{link}`\n\n"
            f"📌 *কীভাবে কাজ করে:*\n"
            f"বন্ধু link-এ click করে বট open করলে:\n"
            f"• তুমি পাবে: *{REF_REFERRER} tokens* 🎁\n"
            f"• বন্ধু পাবে: *{WELCOME_TOKENS+REF_REFEREE} tokens* 🎁\n\n"
            f"📊 এখন পর্যন্ত: *{refs} জন* referred\n"
            f"💰 মোট উপার্জন: *{refs*REF_REFERRER} tokens*"
        )
        await q.edit_message_text(
            msg_text, parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🔗  Link Copy করো", callback_data="refer_copy")],
                [InlineKeyboardButton("🔙  হোম",            callback_data="home")]]))
        return


    if d == "refer_copy":
        user3= get_user(uid)
        bi2  = await ctx.bot.get_me()
        link2= f"https://t.me/{bi2.username}?start={user3['referral_code']}"
        await q.answer(f"Link: {link2}", show_alert=True)
        return

    # ── SubDL ──
    if d.startswith("subdl_"):
        idx  = d.split("_")[1]
        url  = ctx.user_data.get(f"suburl_{idx}", '')
        name = ctx.user_data.get(f"subname_{idx}", 'subtitle.srt')
        if not url: await q.message.reply_text("❌ তথ্য নেই।"); return
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
            except: pass
        if not name.lower().endswith('.srt'): name += '.srt'
        await q.message.reply_document(
            document=io.BytesIO(srt_b), filename=name,
            caption=f"✅ *ডাউনলোড সম্পন্ন!*\n📁 `{name}`\n\n_পাঠালে অনুবাদ করব!_ 🔄",
            parse_mode='Markdown')
        return

    # ── Lang ──
    if d.startswith("set_src_"):
        lang = d.replace("set_src_",""); set_lang(uid, from_lang=lang)
        await q.edit_message_text(
            f"✅ Source: {SRC_LANGS.get(lang,lang)}\n\nTarget language বেছে নাও:",
            parse_mode='Markdown', reply_markup=kb_dst_lang()); return
    if d.startswith("set_dst_"):
        lang = d.replace("set_dst_",""); set_lang(uid, to_lang=lang)
        await q.edit_message_text(
            f"✅ *ভাষা সেটিং সেভ হয়েছে!*\n{SRC_LANGS.get(user['from_lang'],'Auto')} → {DST_LANGS.get(lang,lang)}",
            parse_mode='Markdown', reply_markup=kb_home()); return

    # ── Chat ──
    if d == "chat_start":
        chat_mode[uid] = True
        if uid not in chat_history: chat_history[uid] = []
        await q.edit_message_text(
            f"💬 *AI চ্যাট মোড চালু!*\n\n"
            f"প্রতি message-এ *{COST['chat_per_msg']} token* লাগবে।\n"
            f"Balance: *{user['tokens']} tokens*\n\nযা মনে চায় লেখো! 🤖",
            parse_mode='Markdown', reply_markup=kb_chat()); return
    if d == "chat_clear":
        chat_history[uid] = []
        await q.edit_message_text("🗑 *মুছা হয়েছে!*", parse_mode='Markdown', reply_markup=kb_chat()); return
    if d == "chat_stop":
        chat_mode[uid] = False
        await q.edit_message_text("✅ *চ্যাট বন্ধ।*", parse_mode='Markdown', reply_markup=kb_home()); return

    # ── Daily ──
    if d == "daily":
        ok, h = claim_daily(uid); bal = get_tokens(uid)
        if ok:
            await q.edit_message_text(f"🎁 *+{DAILY_TOKENS} tokens!*\n💰 Balance: *{bal}*",
                                      parse_mode='Markdown', reply_markup=kb_home())
        else:
            await q.edit_message_text(f"⏰ *{h} ঘণ্টা পরে।*\n💰 Balance: *{bal}*",
                                      parse_mode='Markdown', reply_markup=kb_home())
        return

    # ── Token info ──
    if d == "token_info":
        bal = get_tokens(uid)
        await q.edit_message_text(
            f"🪙 *তোমার Balance: {bal} tokens*\n\n{token_info_text()}",
            parse_mode='Markdown', reply_markup=kb_back()); return

    # ── Profile ──
    if d == "profile":
        hist  = get_history(uid,5); refs = ref_count(uid)
        badge = "👑 Premium" if user['tokens']>=PREMIUM_THRESH else "🆓 Free"
        ht = ""
        if hist:
            ht = "\n\n📋 *শেষ ৫টি:*\n"
            for h in hist: ht += f"• `{h['file_name'][:20]}` {h['line_count']}লাইন -{h['cost']}🪙\n"
        await q.edit_message_text(
            f"👤 *প্রোফাইল*\n\n🎖 {badge} | 🪙 *{user['tokens']}*\n"
            f"🔄 {user['total_translations']} অনুবাদ | 📝 {user['total_lines']} লাইন\n"
            f"🤝 {refs} referral{ht}",
            parse_mode='Markdown', reply_markup=kb_back()); return

    # ── Tools ──
    if d == "tools_menu":
        await q.edit_message_text(
            "🛠 *Subtitle Tools* 🆓\n\n⏱ Timing Fix\n🔀 Merge",
            parse_mode='Markdown', reply_markup=kb_tools()); return
    if d == "tool_timing":
        user_state[uid] = {'action':'timing_wait_file'}
        await q.edit_message_text("⏱ *Timing Fix*\n\nSubtitle ফাইল পাঠাও।",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল", callback_data="home")]])); return
    if d == "tool_merge":
        user_state[uid] = {'action':'merge_wait_first'}
        await q.edit_message_text("🔀 *Merge*\n\n*প্রথম* ফাইলটা পাঠাও।",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল", callback_data="home")]])); return


        user_state[uid] = {'action':'yt_wait_url'}
        await q.edit_message_text(
            f"▶️ *YouTube Subtitle অনুবাদ*\n\n"
            f"💰 খরচ: *{COST['youtube']} tokens*\n"
            f"তোমার balance: *{user['tokens']} tokens*\n\n"
            f"YouTube video-র link পাঠাও।\n"
            f"বট subtitle download করে অনুবাদ করবে!",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল", callback_data="home")]])); return

    # ── YouTube Download ──
    if d == "yt_download":
        pending = ctx.user_data.get('yt_url_pending','')
        if pending:
            ctx.user_data.pop('yt_url_pending', None)
            msg2 = await q.edit_message_text(
                "⏳ *Video ডাউনলোড হচ্ছে...*\n\n"
                "📥 720p পর্যন্ত ডাউনলোড হবে\n"
                "⚠️ সর্বোচ্চ 50MB | একটু সময় লাগবে...",
                parse_mode='Markdown')
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(executor, yt_download_video, pending, "720")
            if result.get('error'):
                await q.edit_message_text(
                    f"❌ *ডাউনলোড হয়নি!*\n\n`{result['error']}`",
                    parse_mode='Markdown', reply_markup=kb_home()); return
            size_mb = result['size'] / (1024*1024)
            await q.edit_message_text(
                f"✅ *ডাউনলোড সম্পন্ন!* পাঠাচ্ছি...\n\n"
                f"📁 `{result['filename']}`\n"
                f"📊 সাইজ: `{size_mb:.1f}MB`",
                parse_mode='Markdown')
            await q.message.reply_video(
                video=io.BytesIO(result['data']),
                filename=result['filename'],
                caption=(f"🎬 *{result['title']}*\n\n"
                         f"📥 yt-dlp দ্বারা ডাউনলোড | 🆓"),
                parse_mode='Markdown',
                supports_streaming=True,
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("📥 আরেকটি", callback_data="yt_download")],
                    [InlineKeyboardButton("🔙 হোম",     callback_data="home")]]))
            return

        user_state[uid] = {'action':'yt_download_wait_url'}
        await q.edit_message_text(
            "📥 *YouTube ডাউনলোড* 🆓\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "YouTube video-র link পাঠাও।\n"
            "বট Telegram-এ সরাসরি পাঠাবে! ✅\n\n"
            "📌 *সীমা:*\n"
            "• Quality: 720p | Max: 50MB\n"
            "• ছোট ভিডিও (৫-১৫ মিনিট) সেরা",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🎵  MP3 Audio",  callback_data="yt_dl_audio")],
                [InlineKeyboardButton("🔙  হোম",        callback_data="home")]])); return

    # ── YouTube MP3 ──
    if d == "yt_dl_audio":
        pending = ctx.user_data.get('yt_url_pending','')
        if pending:
            ctx.user_data.pop('yt_url_pending', None)
            await q.edit_message_text(
                "⏳ *Audio ডাউনলোড হচ্ছে...*\n\n"
                "🎵 MP3 format-এ ডাউনলোড হবে\n"
                "একটু সময় লাগবে...",
                parse_mode='Markdown')
            loop   = asyncio.get_event_loop()
            result = await loop.run_in_executor(executor, yt_download_audio, pending)
            if result.get('error'):
                await q.edit_message_text(
                    f"❌ *ডাউনলোড হয়নি!*\n\n`{result['error']}`",
                    parse_mode='Markdown', reply_markup=kb_home()); return
            size_mb = result['size'] / (1024*1024)
            await q.edit_message_text(
                f"✅ *Audio ডাউনলোড সম্পন্ন!* পাঠাচ্ছি...\n\n"
                f"📁 `{result['filename']}`\n📊 `{size_mb:.1f}MB`",
                parse_mode='Markdown')
            await q.message.reply_audio(
                audio=io.BytesIO(result['data']),
                filename=result['filename'],
                title=result['title'],
                caption=f"🎵 *{result['title']}*\n\n📥 yt-dlp | 🆓",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🎵 আরেকটি", callback_data="yt_dl_audio")],
                    [InlineKeyboardButton("🔙 হোম",     callback_data="home")]]))
            return

        user_state[uid] = {'action':'yt_download_audio_url'}
        await q.edit_message_text(
            "🎵 *MP3 Audio ডাউনলোড* 🆓\n\n"
            "YouTube link পাঠাও।\n"
            "বট MP3 হিসেবে Telegram-এ পাঠাবে! ✅",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("🔙  হোম", callback_data="home")]])); return

    # ── Search ──
    if d == "search":
        if not SUBDL_API_KEY:
            await q.edit_message_text("❌ SUBDL_API_KEY নেই।", reply_markup=kb_back()); return
        ctx.user_data['awaiting_search'] = True
        await q.edit_message_text(
            "🔍 *Subtitle খোঁজো*\n\n"
            "মুভির নাম লেখো — বানান ভুল হলেও চলবে!\n\n"
            "📌 `Pirats of Caribian` → ঠিক করে খুঁজবে ✨",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল", callback_data="home")]])); return

    # ── Audio info ──
    if d == "audio_info":
        await q.edit_message_text(
            f"🎙 *Audio Transcription*\n\n"
            f"Voice message বা অডিও ফাইল পাঠাও!\n\n"
            f"💰 Token:\n"
            f"• Transcription: *{COST['audio_transcribe']} tokens*\n"
            f"• + অনুবাদ: *{COST['audio_translate']} tokens*\n\n"
            f"সাপোর্টেড: `mp3 mp4 wav m4a ogg webm flac`\nMax: 25MB",
            parse_mode='Markdown', reply_markup=kb_back()); return

    # ── Lang menu ──
    if d == "lang_menu":
        await q.edit_message_text(
            f"🌐 *ভাষা সেটিং*\n\nবর্তমান: {SRC_LANGS.get(user['from_lang'],'Auto')} → {DST_LANGS.get(user['to_lang'],'বাংলা')}\n\nSource বেছে নাও:",
            parse_mode='Markdown', reply_markup=kb_src_lang()); return

    # ── Help ──
    if d == "help":
        await q.edit_message_text(
            "📖 *ব্যবহার বিধি*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "*📁 SRT অনুবাদ:*\n"
            f"≤500লাইন ফ্রি | তারপর {COST['srt_per_500lines']}🪙/500লাইন\n\n"
            f"*▶️ YouTube Subtitle:* {COST['youtube']}🪙\n"
            f"*📥 YouTube Download:* ফ্রি 🆓\n"
            f"*🎙 Audio:* {COST['audio_transcribe']}🪙\n"
            f"*🛠 Tools:* ফ্রি 🆓\n"
            f"*💬 Chat:* {COST['chat_per_msg']}🪙/msg\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "🎁 `/promo PREMIUMBOT` → 50 tokens!\n"
            "📌 `/daily` `/referral` `/profile`",
            parse_mode='Markdown', reply_markup=kb_back()); return

    # ── Status ──
    if d == "status":
        await q.edit_message_text(
            f"📊 *স্ট্যাটাস*\n\n🟢 Online | 🪙 Token: *{user['tokens']}*",
            parse_mode='Markdown', reply_markup=kb_back()); return

    # ── Admin ──
    if d.startswith("adm_") and uid in ADMIN_IDS:
        action = d.replace("adm_","")
        if action == "stats":
            s = get_stats()
            await q.edit_message_text(
                f"📊\n👥{s['users']} | ✅{s['active']} | 🔄{s['translations']} | 📝{s['lines']}",
                reply_markup=kb_admin()); return
        for act, prompt in [("broadcast","📢 message লেখো:"),("ban","🚫 User ID:"),
                             ("unban","✅ User ID:"),("lookup","👤 User ID:"),("tokens","🪙 User ID:")]:
            if action == act:
                user_state[uid] = {'action':f'admin_{act}'}
                await q.edit_message_text(prompt,
                    reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌", callback_data="home")]])); return

    if d.startswith("img_count_"):
        # format: img_count_N_topic
        parts = d.split("_", 3)
        # parts: ['img','count','N','topic']
        try:
            count = int(parts[2])
            topic = parts[3] if len(parts) > 3 else "images"
        except (ValueError, IndexError):
            await q.answer("সমস্যা হয়েছে!", show_alert=True); return
        user_state.pop(uid, None)
        await q.answer()
        try:
            await q.edit_message_text(
                f"🖼️ *`{topic}`* — {count}টি ছবি খোঁজা হচ্ছে...\n\n⏳ শুরু হচ্ছে...",
                parse_mode='Markdown')
        except Exception: pass
        asyncio.create_task(
            do_image_pdf(uid, topic, count, ctx.bot, q.message.chat_id))
        return

    # ── Image PDF ──
    if d == "img_pdf":
        if not PIXABAY_API_KEY:
            await q.edit_message_text(
                "❌ *Image Search চালু নেই!*\n\nRender-এ `PIXABAY_API_KEY` set করো।",
                parse_mode='Markdown', reply_markup=kb_back())
            return
        if not FPDF_AVAILABLE:
            await q.edit_message_text(
                "❌ *PDF তৈরি করা যাচ্ছে না!*\n\n`fpdf2` install নেই।",
                parse_mode='Markdown', reply_markup=kb_back())
            return
        user_state[uid] = {'action': 'img_wait_topic'}
        await q.edit_message_text(
            "🖼️ *ছবি থেকে PDF বানাও*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "📌 *কোন বিষয়ের ছবি চাও?*\n\n"
            "উদাহরণ:\n"
            "`Bangladesh nature`\n"
            "`Space galaxy nebula`\n"
            "`Tiger in jungle`\n"
            "`Eiffel Tower Paris`\n\n"
            "_English-এ লিখলে সেরা ফলাফল পাবে_",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌  বাতিল", callback_data="home")]]))
        return

    if d == "img_cancel":
        image_tasks[uid] = True
        await q.answer("❌ বাতিল করা হচ্ছে...", show_alert=False)
        try:
            await q.edit_message_text(
                "❌ *PDF তৈরি বাতিল করা হয়েছে।*",
                parse_mode='Markdown', reply_markup=kb_home())
        except Exception: pass
        return

    # ── Home ──
    if d == "home":
        chat_mode[uid] = False
        ctx.user_data['awaiting_search'] = False
        ctx.user_data.pop('yt_url_pending', None)
        user_state.pop(uid, None)
        await q.edit_message_text(
            "🎬 *Subtitle BD Bot*\n\nফাইল বা YouTube link পাঠাও! 🚀",
            parse_mode='Markdown', reply_markup=kb_home())


# ══════════════════════════════════════════════
# 🖼️  IMAGE PDF TASK
# ══════════════════════════════════════════════
async def do_image_pdf(uid: int, topic: str, count: int,
                       bot, chat_id: int):
    """ছবি খুঁজে PDF তৈরি করে পাঠাও"""
    image_tasks[uid] = False   # running

    # ── Status message ──
    status = await bot.send_message(
        chat_id,
        f"🔍 *`{topic}`* বিষয়ে ছবি খোঁজা হচ্ছে...\n\n⏳ একটু অপেক্ষা করো...",
        parse_mode='Markdown')

    try:
        loop = asyncio.get_event_loop()

        # ── Step 1: Search URLs ──
        urls = await loop.run_in_executor(
            executor, search_images, topic, count)

        if not urls:
            await bot.edit_message_text(
                "❌ *কোনো ছবি পাওয়া যায়নি!*\n\n"
                "অন্য keyword দিয়ে আবার চেষ্টা করো।",
                chat_id=chat_id, message_id=status.message_id,
                parse_mode='Markdown', reply_markup=kb_home())
            return

        total_urls  = len(urls)
        downloaded  = []
        failed      = 0

        # ── Step 2: Download images with progress ──
        for i, url in enumerate(urls):
            if image_tasks.get(uid, False):   # cancel check
                await bot.edit_message_text(
                    "❌ *বাতিল করা হয়েছে।*",
                    chat_id=chat_id, message_id=status.message_id,
                    reply_markup=kb_home())
                return

            if len(downloaded) >= count:
                break

            img_data = await loop.run_in_executor(executor, download_image, url)
            if img_data:
                downloaded.append(img_data)
            else:
                failed += 1

            # Progress update every 3 downloads
            done    = len(downloaded)
            pct     = int(done / count * 100)
            bar     = '█' * (pct // 5) + '░' * (20 - pct // 5)

            if i % 3 == 0 or done >= count:
                try:
                    await bot.edit_message_text(
                        f"📥 *ছবি ডাউনলোড হচ্ছে...*\n\n"
                        f"🔍 বিষয়: `{topic}`\n"
                        f"`[{bar}]` *{pct}%*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━\n"
                        f"✅ পাওয়া গেছে: *{done}/{count}*\n"
                        f"❌ পাওয়া যায়নি: *{failed}*",
                        chat_id=chat_id, message_id=status.message_id,
                        parse_mode='Markdown',
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("❌  বাতিল", callback_data="img_cancel")]]))
                except Exception:
                    pass

            await asyncio.sleep(0.1)

        if not downloaded:
            await bot.edit_message_text(
                "❌ *কোনো ছবি ডাউনলোড হয়নি!*\n\n"
                "Internet সমস্যা বা ছবিগুলো protected।\nআবার চেষ্টা করো।",
                chat_id=chat_id, message_id=status.message_id,
                parse_mode='Markdown', reply_markup=kb_home())
            return

        # ── Step 3: Create PDF ──
        if image_tasks.get(uid, False): return

        await bot.edit_message_text(
            f"📄 *PDF তৈরি হচ্ছে...*\n\n"
            f"🖼️ {len(downloaded)}টি ছবি দিয়ে PDF বানানো হচ্ছে...\n"
            f"⏳ একটু অপেক্ষা করো...",
            chat_id=chat_id, message_id=status.message_id,
            parse_mode='Markdown')

        pdf_bytes = await loop.run_in_executor(
            executor, create_pdf_from_images, downloaded, topic)

        if not pdf_bytes:
            await bot.edit_message_text(
                "❌ *PDF তৈরি হয়নি!*\n\nআবার চেষ্টা করো।",
                chat_id=chat_id, message_id=status.message_id,
                reply_markup=kb_home())
            return

        size_kb = len(pdf_bytes) / 1024
        fname   = re.sub(r'[^\w\s-]', '', topic)[:30].strip() + '_images.pdf'

        # ── Step 4: Send PDF ──
        await bot.edit_message_text(
            f"✅ *PDF তৈরি সম্পন্ন!*\n\n"
            f"🖼️ {len(downloaded)}টি ছবি | 📄 {size_kb:.0f}KB\n"
            f"পাঠাচ্ছি...",
            chat_id=chat_id, message_id=status.message_id,
            parse_mode='Markdown')

        await bot.send_document(
            chat_id=chat_id,
            document=io.BytesIO(pdf_bytes),
            filename=fname,
            caption=(
                f"📄 *{topic}* — ছবির PDF\n\n"
                f"🖼️ {len(downloaded)}টি ছবি | 📊 {size_kb:.0f}KB\n"
                f"━━━━━━━━━━━━━━━━━━━━━\n"
                f"_🖼️ ছবি → PDF বট দ্বারা তৈরি_"
            ),
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🖼️  আরেকটি PDF", callback_data="img_pdf")],
                [InlineKeyboardButton("🔙  হোম",         callback_data="home")]]))  

        await bot.delete_message(chat_id=chat_id, message_id=status.message_id)

    except Exception as e:
        logger.error(f"Image PDF error for {uid}: {e}")
        try:
            await bot.edit_message_text(
                f"❌ *সমস্যা হয়েছে!*\n\n`{str(e)[:150]}`",
                chat_id=chat_id, message_id=status.message_id,
                parse_mode='Markdown', reply_markup=kb_home())
        except Exception: pass
    finally:
        image_tasks.pop(uid, None)

# ══════════════════════════════════════════════
# 📁  FILE HANDLER
# ══════════════════════════════════════════════
async def handle_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    doc = update.message.document
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return

    fname = (doc.file_name or '').lower()
    user  = get_user(u.id, u.username, u.first_name)
    state = user_state.get(u.id, {})

    # Timing wait
    if state.get('action')=='timing_wait_file' and fname.endswith(('.srt','.vtt','.ass','.ssa')):
        user_state[u.id] = {'action':'timing_wait_offset','file_id':doc.file_id,'file_name':doc.file_name}
        await update.message.reply_text("✅ ফাইল পেয়েছি!\n\nকত সেকেন্ড shift? (যেমন: +5 বা -3.5)",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল", callback_data="home")]])); return

    # Merge first
    if state.get('action')=='merge_wait_first' and fname.endswith(('.srt','.vtt','.ass','.ssa')):
        user_state[u.id] = {'action':'merge_wait_second','file1_id':doc.file_id,'file1_name':doc.file_name}
        await update.message.reply_text("✅ প্রথম ফাইল পেয়েছি!\n\n*দ্বিতীয়* ফাইলটা পাঠাও।",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ বাতিল", callback_data="home")]])); return

    # Merge second
    if state.get('action')=='merge_wait_second' and fname.endswith(('.srt','.vtt','.ass','.ssa')):
        msg = await update.message.reply_text("⏳ Merge হচ্ছে...")
        try:
            f1 = await ctx.bot.get_file(state['file1_id']); r1 = await f1.download_as_bytearray()
            f2 = await ctx.bot.get_file(doc.file_id);       r2 = await f2.download_as_bytearray()
            b1 = parse_auto(r1.decode('utf-8-sig','ignore'), state['file1_name'])
            b2 = parse_auto(r2.decode('utf-8-sig','ignore'), doc.file_name)
            merged = merge_subtitles(b1, b2); out = build_srt(merged).encode('utf-8-sig')
            await msg.delete()
            await update.message.reply_document(
                document=io.BytesIO(out), filename="merged_subtitle.srt",
                caption=f"✅ *Merge সম্পন্ন!* 🆓\n{len(b1)}+{len(b2)}=*{len(merged)} লাইন*",
                parse_mode='Markdown', reply_markup=kb_home())
        except Exception as e: await msg.edit_text(f"❌ `{str(e)[:100]}`", parse_mode='Markdown')
        user_state.pop(u.id, None); return

    # Audio file
    if any(fname.endswith(ext) for ext in AUDIO_EXTS):
        if doc.file_size and doc.file_size > MAX_AUDIO:
            await update.message.reply_text("❌ ২৫MB-এর বেশি!"); return
        pending_audio[u.id] = {'file_id':doc.file_id,'file_name':doc.file_name or 'audio.mp3'}
        await update.message.reply_text(
            f"🎙 *অডিও পেয়েছি!*\n📁 `{doc.file_name}`\n\nকী করতে চাও?",
            parse_mode='Markdown', reply_markup=kb_audio(u.id)); return

    # SRT/VTT/ASS
    if not fname.endswith(('.srt','.vtt','.ass','.ssa')):
        await update.message.reply_text(
            "❌ সাপোর্টেড ফাইল: `.srt` `.vtt` `.ass` বা অডিও `mp3 wav m4a`",
            parse_mode='Markdown'); return

    if u.id in active_tasks and not active_tasks[u.id]:
        await update.message.reply_text("⚠️ একটি অনুবাদ চলছে! আগেরটা শেষ করো।"); return

    max_mb = 10*1024*1024 if user['tokens']>=PREMIUM_THRESH else 5*1024*1024
    if doc.file_size and doc.file_size > max_mb:
        await update.message.reply_text(
            f"❌ ফাইল সাইজ সীমা অতিক্রম!\n_(Free: 5MB, 100+ tokens: 10MB)_",
            parse_mode='Markdown'); return

    f   = await ctx.bot.get_file(doc.file_id); raw = await f.download_as_bytearray()
    content = None
    for enc in ['utf-8-sig','utf-8','latin-1','cp1252']:
        try: content = raw.decode(enc); break
        except: continue
    if not content: await update.message.reply_text("❌ ফাইল পড়া যাচ্ছে না!"); return
    blocks = parse_auto(content, doc.file_name)
    if not blocks: await update.message.reply_text("❌ subtitle নেই!"); return

    total = len(blocks); cost = calc_srt_cost(total); bal = get_tokens(u.id)
    if bal < cost:
        await update.message.reply_text(token_warn(cost,bal), parse_mode='Markdown', reply_markup=kb_back()); return

    deduct_tokens(u.id, cost)
    user_state.pop(u.id, None); chat_mode[u.id] = False
    active_tasks[u.id] = False; cancel_events[u.id] = threading.Event()
    ce = cancel_events[u.id]
    fl = user.get('from_lang','auto'); tl = user.get('to_lang','bn')
    lang_disp = f"{SRC_LANGS.get(fl,'Auto')} → {DST_LANGS.get(tl,'বাংলা')}"
    new_bal   = get_tokens(u.id)

    status = await update.message.reply_photo(
        photo=pie_chart(0,1),
        caption=(f"📥 *ফাইল পেয়েছি!*\n\n📁 `{doc.file_name}`\n"
                 f"🌐 {lang_disp}\n📊 {total} লাইন | 🪙 -{cost} token\n"
                 f"💰 Balance: *{new_bal}*\n⏳ শুরু হচ্ছে..."),
        parse_mode='Markdown', reply_markup=kb_cancel(u.id))

    try:
        await status.edit_media(InputMediaPhoto(
            media=pie_chart(0,total),
            caption=(f"🎬 *অনুবাদ শুরু হচ্ছে...*\n\n📁 `{doc.file_name}`\n"
                     f"🌐 {lang_disp} | 📊 মোট: *{total}টি*\n"
                     f"🪙 -{cost} | Balance: *{new_bal}*\n"
                     f"━━━━━━━━━━━━━━━━━━━━━\n⏳ 0/{total}"),
            parse_mode='Markdown'), reply_markup=kb_cancel(u.id))

        BATCH = 7; translated = list(blocks); completed = 0; loop = asyncio.get_event_loop()
        for i in range(0, total, BATCH):
            if ce.is_set() or active_tasks.get(u.id,False): add_tokens(u.id,cost); return
            chunk  = blocks[i:i+BATCH]; texts = [b['text'] for b in chunk]
            result = await loop.run_in_executor(executor, functools.partial(tbatch,texts,fl,tl,ce))
            if ce.is_set() or active_tasks.get(u.id,False): add_tokens(u.id,cost); return
            for j,tr in enumerate(result):
                if i+j<total: translated[i+j]['text']=tr
            completed = min(i+BATCH,total); pct = completed/total*100
            bar = '█'*int(pct/5)+'░'*(20-int(pct/5))
            try:
                await status.edit_media(InputMediaPhoto(
                    media=pie_chart(completed,total),
                    caption=(f"🔄 *অনুবাদ চলছে...*\n\n📁 `{doc.file_name}`\n"
                             f"`[{bar}]` *{pct:.1f}%*\n🌐 {lang_disp}\n"
                             f"━━━━━━━━━━━━━━━━━━━━━\n✅ {completed}/{total}"),
                    parse_mode='Markdown'), reply_markup=kb_cancel(u.id))
            except Exception as e: logger.warning(f"Edit ignored: {e}")
            await asyncio.sleep(0.2)

        if ce.is_set() or active_tasks.get(u.id,False): add_tokens(u.id,cost); return

        out   = build_srt(translated).encode('utf-8-sig')
        oname = re.sub(r'\.(srt|vtt|ass|ssa)$','_Bengali.srt',doc.file_name,flags=re.IGNORECASE)
        final = get_tokens(u.id); add_tokens(u.id,2)

        await status.edit_media(InputMediaPhoto(
            media=pie_chart(total,total),
            caption=(f"✅ *অনুবাদ সম্পন্ন!*\n\n📁 `{doc.file_name}`\n"
                     f"🎉 *{total}টি* লাইন | 🌐 {lang_disp}\n"
                     f"🪙 -{cost}+2bonus | Balance: *{final+2}*"),
            parse_mode='Markdown'))

        await update.message.reply_document(
            document=io.BytesIO(out), filename=oname,
            caption=(f"🎬 *অনুবাদিত ফাইল*\n\n📁 `{oname}`\n"
                     f"✅ *{total}* লাইন | ⏱ Timing অক্ষুণ্ণ\n\n"
                     f"_VLC / MX Player-এ ব্যবহার করো_ 🎥"),
            parse_mode='Markdown', reply_markup=kb_home())

        log_history(u.id, doc.file_name, total, fl, tl, cost)

    except Exception as e:
        err = str(e); add_tokens(u.id, cost)
        notice = QUOTA_MSG if "QUOTA_EXCEEDED" in err else f"❌ সমস্যা!\n\n`{err[:200]}`\n\n_Token ফেরত দেওয়া হয়েছে।_"
        kb = kb_quota() if "QUOTA_EXCEEDED" in err else kb_home()
        try: await status.edit_caption(notice, parse_mode='Markdown', reply_markup=kb)
        except: await update.message.reply_text(notice, parse_mode='Markdown', reply_markup=kb)
    finally:
        active_tasks.pop(u.id, None); cancel_events.pop(u.id, None)

# ══════════════════════════════════════════════
# 🎤  VOICE / AUDIO HANDLER
# ══════════════════════════════════════════════
async def handle_audio_or_voice(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u   = update.effective_user
    msg = update.message
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    if msg.voice:   fo,fn,fs = msg.voice, f"voice_{u.id}.ogg", msg.voice.file_size
    elif msg.audio: fo,fn,fs = msg.audio, (msg.audio.file_name or "audio.mp3"), msg.audio.file_size
    else: return
    if fs and fs > MAX_AUDIO: await msg.reply_text("❌ ২৫MB-এর বেশি!"); return
    pending_audio[u.id] = {'file_id':fo.file_id,'file_name':fn}
    await msg.reply_text(f"🎙 *অডিও পেয়েছি!*\n📁 `{fn}`\n\nকী করতে চাও?",
        parse_mode='Markdown', reply_markup=kb_audio(u.id))

# ══════════════════════════════════════════════
# 💬  TEXT HANDLER
# ══════════════════════════════════════════════
async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u     = update.effective_user
    text  = update.message.text.strip()
    if not await check_access(u.id, ctx.bot, update.message.reply_text): return
    user  = get_user(u.id, u.username, u.first_name)
    state = user_state.get(u.id, {})

    # ── Image PDF: topic wait ──
    if state.get('action') == 'img_wait_topic':
        topic = text.strip()
        if not topic:
            await update.message.reply_text("❌ বিষয়টা লেখো!"); return
        user_state[u.id] = {'action': 'img_wait_count', 'topic': topic}
        await update.message.reply_text(
            f"✅ বিষয়: *`{topic}`*\n\n"
            f"━━━━━━━━━━━━━━━━━━━━━\n"
            f"📌 কতটি ছবি চাও?\n\n"
            f"সংখ্যা লেখো (সর্বোচ্চ ২০):\n"
            f"_বেশি ছবি = বেশি সময়_",
            parse_mode='Markdown',
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("5️⃣  ৫টি",   callback_data=f"img_count_5_{topic[:30]}"),
                 InlineKeyboardButton("1️⃣0️⃣  ১০টি", callback_data=f"img_count_10_{topic[:30]}")],
                [InlineKeyboardButton("1️⃣5️⃣  ১৫টি", callback_data=f"img_count_15_{topic[:30]}"),
                 InlineKeyboardButton("2️⃣0️⃣  ২০টি", callback_data=f"img_count_20_{topic[:30]}")],
                [InlineKeyboardButton("❌  বাতিল",   callback_data="home")]]))
        return

    # ── Image PDF: count wait (manual input) ──
    if state.get('action') == 'img_wait_count':
        try:
            count = int(text.strip())
            if count < 1:   count = 1
            if count > 20:  count = 20
        except ValueError:
            await update.message.reply_text("❌ শুধু সংখ্যা লেখো! (যেমন: 10)"); return
        topic = state.get('topic','images')
        user_state.pop(u.id, None)
        asyncio.create_task(do_image_pdf(u.id, topic, count, ctx.bot, update.effective_chat.id))
        return

    # ── Timing offset ──
    if state.get('action') == 'timing_wait_offset':
        try: offset = float(text.replace(',','.'))
        except: await update.message.reply_text("❌ সংখ্যা লেখো! (+5 বা -3.5)"); return
        msg = await update.message.reply_text("⏳ Timing ঠিক করছি...")
        try:
            f   = await ctx.bot.get_file(state['file_id']); raw = await f.download_as_bytearray()
            content = raw.decode('utf-8-sig','ignore'); blocks = parse_auto(content, state['file_name'])
            fixed   = fix_timing(blocks, offset); out = build_srt(fixed).encode('utf-8-sig')
            oname   = re.sub(r'\.srt$', f'_shifted{offset:+g}s.srt', state['file_name'])
            await msg.delete()
            await update.message.reply_document(
                document=io.BytesIO(out), filename=oname,
                caption=f"✅ *Timing Fix সম্পন্ন!* 🆓\n\n⏱ Shift: `{offset:+g}` সেকেন্ড\n{len(fixed)} লাইন",
                parse_mode='Markdown', reply_markup=kb_home())
        except Exception as e: await msg.edit_text(f"❌ `{str(e)[:100]}`", parse_mode='Markdown')
        user_state.pop(u.id, None); return

    # ── Admin ──
    if state.get('action') == 'admin_broadcast' and u.id in ADMIN_IDS:
        uids = all_uids(); msg = await update.message.reply_text(f"📢 পাঠাচ্ছি {len(uids)} জনকে...")
        ok = fail = 0
        for uid2 in uids:
            try: await ctx.bot.send_message(uid2, text, parse_mode='Markdown'); ok+=1
            except: fail+=1
            await asyncio.sleep(0.05)
        await msg.edit_text(f"✅ Broadcast সম্পন্ন!\n✅{ok} ❌{fail}")
        user_state.pop(u.id, None); return

    for act in ['admin_ban','admin_unban']:
        if state.get('action')==act and u.id in ADMIN_IDS:
            try:
                ban_user(int(text), act=='admin_ban')
                await update.message.reply_text(
                    f"✅ User `{text}` {'ban' if act=='admin_ban' else 'unban'} হয়েছে।",
                    parse_mode='Markdown', reply_markup=kb_admin())
            except: await update.message.reply_text("❌ ভুল ID")
            user_state.pop(u.id, None); return

    if state.get('action') == 'admin_lookup' and u.id in ADMIN_IDS:
        try:
            con = db(); c = con.cursor()
            c.execute("SELECT * FROM users WHERE uid=?", (int(text),))
            row = c.fetchone(); con.close()
            if row:
                row = dict(row)
                await update.message.reply_text(
                    f"👤 ID:`{row['uid']}` | {row['first_name']}\n"
                    f"🪙{row['tokens']} | 🔄{row['total_translations']} | 🚫{bool(row['is_banned'])}",
                    reply_markup=kb_admin())
            else: await update.message.reply_text("❌ পাওয়া যায়নি।")
        except: await update.message.reply_text("❌ ভুল ID")
        user_state.pop(u.id, None); return

    if state.get('action') == 'admin_tokens_uid' and u.id in ADMIN_IDS:
        try: user_state[u.id] = {'action':'admin_tokens_amount','target':int(text)}; await update.message.reply_text("কত token?")
        except: await update.message.reply_text("❌ ভুল ID")
        return

    if state.get('action') == 'admin_tokens_amount' and u.id in ADMIN_IDS:
        try:
            t = state['target']; n = int(text); add_tokens(t,n)
            await update.message.reply_text(f"✅ `{t}` কে *{n} tokens* দেওয়া হয়েছে।",
                parse_mode='Markdown', reply_markup=kb_admin())
        except: await update.message.reply_text("❌ ভুল")
        user_state.pop(u.id, None); return

    # ── YouTube Subtitle ──
    if state.get('action') == 'yt_wait_url':
        if not ('youtube.com' in text or 'youtu.be' in text):
            await update.message.reply_text("❌ সঠিক YouTube link দাও!"); return
        cost = COST['youtube']; bal = get_tokens(u.id)
        if bal < cost:
            user_state.pop(u.id, None)
            await update.message.reply_text(token_warn(cost,bal), parse_mode='Markdown', reply_markup=kb_back()); return
        deduct_tokens(u.id, cost); user_state.pop(u.id, None)
        msg = await update.message.reply_text("▶️ *YouTube subtitle download হচ্ছে...*\n⏳", parse_mode='Markdown')
        loop = asyncio.get_event_loop()
        content, title = await loop.run_in_executor(executor, functools.partial(yt_subtitle, text, 'en'))
        if not content:
            add_tokens(u.id, cost)
            await msg.edit_text("❌ *Subtitle পাওয়া যায়নি!*\n\n_Token ফেরত দেওয়া হয়েছে।_",
                parse_mode='Markdown', reply_markup=kb_back()); return
        blocks = parse_vtt(content) if content.startswith('WEBVTT') else parse_srt(content)
        if not blocks:
            add_tokens(u.id, cost); await msg.edit_text("❌ Parse করা যায়নি!", reply_markup=kb_back()); return
        total = len(blocks); fl = user.get('from_lang','auto'); tl = user.get('to_lang','bn')
        translated = list(blocks)
        for i in range(0, total, 7):
            chunk = blocks[i:i+7]; texts = [b['text'] for b in chunk]
            result = await loop.run_in_executor(executor, functools.partial(tbatch,texts,fl,tl))
            for j,tr in enumerate(result):
                if i+j<total: translated[i+j]['text']=tr
            try: await msg.edit_text(f"🔄 *অনুবাদ...* {min(i+7,total)/total*100:.0f}%", parse_mode='Markdown')
            except: pass
            await asyncio.sleep(0.5)
        oname = f"{title[:30]}_Bengali.srt"; out = build_srt(translated).encode('utf-8-sig')
        new_bal = get_tokens(u.id); add_tokens(u.id, 3)
        await msg.delete()
        await update.message.reply_document(
            document=io.BytesIO(out), filename=oname,
            caption=(f"🎬 *YouTube Subtitle অনুবাদ সম্পন্ন!*\n\n📺 `{title}`\n"
                     f"✅ *{total}টি* লাইন | 🪙 -{cost}+3bonus\n💰 Balance: *{new_bal+3}*"),
            parse_mode='Markdown', reply_markup=kb_home())
        log_history(u.id, oname, total, fl, tl, cost); return

    # ── YouTube Video/Audio Download (yt-dlp) ──
    if state.get('action') in ('yt_download_wait_url', 'yt_download_audio_url'):
        is_audio = (state.get('action') == 'yt_download_audio_url')
        if not ('youtube.com' in text or 'youtu.be' in text):
            await update.message.reply_text("❌ সঠিক YouTube link দাও!"); return
        user_state.pop(u.id, None)
        emoji    = "🎵" if is_audio else "🎬"
        mode_txt = "Audio (MP3)" if is_audio else "Video (720p)"
        msg = await update.message.reply_text(
            f"⏳ *{emoji} {mode_txt} ডাউনলোড হচ্ছে...*\n\n"
            f"📥 সরাসরি ডাউনলোড চলছে\n"
            f"⚠️ সর্বোচ্চ 50MB | একটু সময় লাগবে...",
            parse_mode='Markdown')
        loop = asyncio.get_event_loop()
        if is_audio:
            result = await loop.run_in_executor(executor, yt_download_audio, text)
        else:
            result = await loop.run_in_executor(executor, yt_download_video, text, "720")
        if result.get('error'):
            await msg.edit_text(
                f"❌ *ডাউনলোড হয়নি!*\n\n`{result['error']}`\n\n"
                f"💡 *সম্ভাব্য কারণ:*\n"
                f"• ভিডিও 50MB-এর বেশি বড়\n"
                f"• Private বা age-restricted\n"
                f"• ছোট ভিডিও দিয়ে চেষ্টা করো",
                parse_mode='Markdown', reply_markup=kb_home()); return
        size_mb = result['size'] / (1024*1024)
        await msg.edit_text(
            f"✅ *{emoji} ডাউনলোড সম্পন্ন!* Telegram-এ পাঠাচ্ছি...\n\n"
            f"📁 `{result['filename']}`\n"
            f"📊 সাইজ: `{size_mb:.1f}MB`",
            parse_mode='Markdown')
        try:
            if is_audio:
                await update.message.reply_audio(
                    audio=io.BytesIO(result['data']),
                    filename=result['filename'],
                    title=result['title'],
                    caption=f"🎵 *{result['title']}*\n\n📥 yt-dlp | 🆓",
                    parse_mode='Markdown',
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("🎵 আরেকটি", callback_data="yt_dl_audio")],
                        [InlineKeyboardButton("🔙 হোম",     callback_data="home")]]))
            else:
                await update.message.reply_video(
                    video=io.BytesIO(result['data']),
                    filename=result['filename'],
                    caption=f"🎬 *{result['title']}*\n\n📥 yt-dlp | 🆓",
                    parse_mode='Markdown',
                    supports_streaming=True,
                    reply_markup=InlineKeyboardMarkup([
                        [InlineKeyboardButton("📥 আরেকটি", callback_data="yt_download")],
                        [InlineKeyboardButton("🔙 হোম",     callback_data="home")]]))
            await msg.delete()
        except Exception as e:
            await msg.edit_text(
                f"❌ *Telegram-এ পাঠানো যায়নি!*\n\n"
                f"সাইজ `{size_mb:.1f}MB` হয়তো বেশি বড়।\n"
                f"ছোট ভিডিও দিয়ে চেষ্টা করো।",
                parse_mode='Markdown', reply_markup=kb_home())
        return

    # ── Search ──
    if ctx.user_data.get('awaiting_search'):
        ctx.user_data['awaiting_search'] = False
        msg  = await update.message.reply_text(
            f"🔍 *খোঁজা হচ্ছে:* `{text}`\n\n✨ বানান চেক করছি...", parse_mode='Markdown')
        loop = asyncio.get_event_loop()
        fixed = await loop.run_in_executor(executor, fix_movie_name, text)
        if fixed.lower() != text.lower():
            try: await msg.edit_text(f"🔍 `{text}` → ✅ `{fixed}`\n⏳ খুঁজছি...", parse_mode='Markdown')
            except: pass
        movie_info = {}; poster_data = None
        if OMDB_API_KEY:
            movie_info  = await loop.run_in_executor(executor, get_movie_info, fixed)
            if movie_info.get('Poster') and movie_info['Poster']!='N/A':
                poster_data = await loop.run_in_executor(executor, download_poster, movie_info['Poster'])
        results = await loop.run_in_executor(executor, subdl_search, fixed)
        if not results and fixed!=text:
            results = await loop.run_in_executor(executor, subdl_search, text)
        if not results:
            await msg.edit_text(
                f"😔 *`{fixed}`* এর জন্য কিছু পাওয়া যায়নি!\n\nঅন্যভাবে লিখে চেষ্টা করো।",
                parse_mode='Markdown',
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")],
                    [InlineKeyboardButton("🔙 হোম",         callback_data="home")]])); return
        info_text = ""
        if movie_info:
            t  = movie_info.get('Title',''); y = movie_info.get('Year','')
            r  = movie_info.get('imdbRating','N/A'); g = movie_info.get('Genre','')
            p  = movie_info.get('Plot','')
            if p and p!='N/A': p = p[:120]+"..."
            if t: info_text = f"🎬 *{t}* ({y})\n⭐ IMDb: *{r}* | 🎭 {g}\n📝 _{p}_\n\n"
        header = f"🔍 `{text}` → ✅ `{fixed}`\n\n" if fixed.lower()!=text.lower() else f"🔍 *`{fixed}`* এর Subtitle:\n\n"
        body = header + info_text + "━━━━━━━━━━━━━━━━━━━━━\n\n"
        buttons = []
        for i, item in enumerate(results, 1):
            name = item.get('release_name','Unknown')[:45]; lang = item.get('language','EN')
            yr   = f" ({item.get('year','')})" if item.get('year') else ""
            ctx.user_data[f"suburl_{i}"] = item.get('url','')
            ctx.user_data[f"subname_{i}"] = (item.get('release_name',f'subtitle_{i}')+'.srt')[:60]
            body += f"*{i}.* {name}{yr} 🌐{lang}\n\n"
            buttons.append([InlineKeyboardButton(f"⬇️ {i}. {name[:33]}{yr}", callback_data=f"subdl_{i}")])
        buttons.append([InlineKeyboardButton("🔍 আবার খোঁজো", callback_data="search")])
        buttons.append([InlineKeyboardButton("🔙 হোম",         callback_data="home")])
        kb = InlineKeyboardMarkup(buttons)
        try:
            if poster_data:
                await msg.delete()
                await update.message.reply_photo(photo=io.BytesIO(poster_data),
                    caption=body, parse_mode='Markdown', reply_markup=kb)
            else:
                await msg.edit_text(body, parse_mode='Markdown', reply_markup=kb)
        except Exception as e:
            logger.warning(f"Search display: {e}")
            try: await msg.edit_text(body, parse_mode='Markdown', reply_markup=kb)
            except: pass
        return

    # ── Auto-detect YouTube link ──
    if 'youtube.com' in text or 'youtu.be' in text:
        ctx.user_data['yt_url_pending'] = text
        await update.message.reply_text(
            "📥 *YouTube Link পেয়েছি!*\n\n"
            "━━━━━━━━━━━━━━━━━━━━━\n"
            "কী ডাউনলোড করতে চাও?",
            parse_mode='Markdown', reply_markup=kb_yt_choice(text)); return

    # ── AI Chat ──
    if chat_mode.get(u.id, False):
        cost = COST['chat_per_msg']; bal = get_tokens(u.id)
        if bal < cost:
            await update.message.reply_text(token_warn(cost,bal),
                parse_mode='Markdown', reply_markup=kb_back()); return
        deduct_tokens(u.id, cost)
        await ctx.bot.send_chat_action(update.effective_chat.id, "typing")
        loop  = asyncio.get_event_loop()
        reply = await loop.run_in_executor(executor, functools.partial(ai_chat, u.id, text))
        hlen  = len(chat_history.get(u.id,[])) // 2; new_bal = get_tokens(u.id)
        await update.message.reply_text(
            f"{reply}\n\n━━━━━━━━━━━━━━━━━━━━━\n"
            f"_💬 {hlen} বার্তা | 🪙 Balance: {new_bal}_",
            parse_mode='Markdown', reply_markup=kb_chat()); return

    # ── Default ──
    await update.message.reply_text(
        "📌 *কী করতে চাও?*\n\n"
        "• SRT/VTT/ASS ফাইল পাঠাও\n"
        "• YouTube link পাঠাও\n"
        "• Voice/Audio পাঠাও\n"
        "• বাটন চাপো 👇",
        parse_mode='Markdown', reply_markup=kb_home())

# ══════════════════════════════════════════════
# 🚀  MAIN
# ══════════════════════════════════════════════
def main():
    if not BOT_TOKEN:    logger.error("❌ BOT_TOKEN missing!"); return
    if not GROQ_API_KEY: logger.error("❌ GROQ_API_KEY missing!"); return

    init_db()
    threading.Thread(target=run_flask, daemon=True).start()
    threading.Thread(target=self_ping, daemon=True).start()
    logger.info("✅ Flask + self-ping started")

    app = (Application.builder()
           .token(BOT_TOKEN)
           .connection_pool_size(16)
           .get_updates_connection_pool_size(8)
           .build())
    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("profile",  cmd_profile))
    app.add_handler(CommandHandler("referral", cmd_referral))
    app.add_handler(CommandHandler("daily",    cmd_daily))
    app.add_handler(CommandHandler("promo",    cmd_promo))
    app.add_handler(CommandHandler("admin",    cmd_admin))
    app.add_handler(CallbackQueryHandler(cb_handler))
    app.add_handler(MessageHandler(filters.VOICE,         handle_audio_or_voice))
    app.add_handler(MessageHandler(filters.AUDIO,         handle_audio_or_voice))
    app.add_handler(MessageHandler(filters.Document.ALL,  handle_file))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("🤖 Bot polling started!")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
        read_timeout=30, write_timeout=30,
        connect_timeout=30, pool_timeout=30)

if __name__ == '__main__':
    main()
