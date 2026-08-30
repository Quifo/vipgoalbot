import os
import asyncio
import json
import time
import logging
import html
import httpx
import aiosqlite
import traceback
from asyncio_limiter import Limiter
from telegram.constants import ChatAction
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

# Çevre değişkenlerini yükle
load_dotenv()

# Konfigürasyon
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GROQ_KEY = os.getenv("GROQ_KEY")
SPORTMONKS_API_KEY = os.getenv("SPORTMONKS_API_KEY")
DB_FILE = "signals.db"
BASE_URL = "https://api.sportmonks.com/v3/football"

# Rate limiter (Pro: 14 istek/saniye, biz 10 ile güvenli oynuyoruz)
rate_limiter = Limiter(10)

# Logging ayarları
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# Global değişkenler
brain = BettingBrain()
SIGNAL_COOLDOWN = 180  # 3 dakika
last_ai_requests = []
MAX_AI_PER_MINUTE = 20

# Yardımcı fonksiyonlar
def safe_int(val, default=0):
    try:
        if val is None or val == '' or val == '-':
            return default
        return int(float(str(val).replace('%', '').strip()))
    except:
        return default

def safe_float(val, default=0.0):
    try:
        if val is None or val == '' or val == '-':
            return default
        return float(str(val).replace('%', '').strip())
    except:
        return default

# API çağrısı (Rate limitli)
async def fetch_api(url):
    """Sportmonks API'den veri çeker (rate limitli)"""
    async with rate_limiter:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                r = await client.get(url)
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 429:
                    logger.warning("Rate limit aşıldı, 5 saniye bekleniyor...")
                    await asyncio.sleep(5)
                    return await fetch_api(url)
                else:
                    logger.error(f"API HTTP {r.status_code}: {url}")
                    return {}
        except Exception as e:
            logger.error(f"API Hatası: {e}")
            return {}

# Veritabanı fonksiyonları
async def init_db():
    """Veritabanını başlatır"""
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL,
                timestamp REAL NOT NULL,
                status TEXT DEFAULT 'pending',
                start_total INTEGER,
                match_name TEXT,
                pick TEXT,
                league TEXT,
                final_score TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        await db.execute("CREATE INDEX IF NOT EXISTS idx_status ON signals(status)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON signals(timestamp)")
        await db.execute("CREATE INDEX IF NOT EXISTS idx_match_id ON signals(match_id)")
        await db.commit()
    logger.info("✅ Veritabanı hazır")

async def add_signal(match_id, match_name, pick, start_total, league):
    """Sinyali veritabanına kaydeder"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                """INSERT OR IGNORE INTO signals 
                   (id, match_id, timestamp, status, start_total, match_name, pick, league) 
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"{match_id}_{int(time.time())}", str(match_id), time.time(), 
                 'pending', start_total, match_name, pick, league)
            )
            await db.commit()
            return True
    except Exception as e:
        logger.error(f"DB kayıt hatası: {e}")
        return False

async def get_last_signal_time(match_id, pick):
    """Son sinyal zamanını döndürür"""
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                "SELECT MAX(timestamp) FROM signals WHERE match_id = ? AND pick = ?",
                (str(match_id), pick)
            ) as cursor:
                row = await cursor.fetchone()
                return row[0] if row and row[0] else 0
    except Exception as e:
        logger.error(f"Zaman sorgu hatası: {e}")
        return 0

async def update_signal_result(match_id, final_score, is_win):
    """Maç sonucunu günceller"""
    try:
        status = 'WIN ✅' if is_win else 'LOSS ❌'
        async with aiosqlite.connect(DB_FILE) as db:
            async with db.execute(
                """SELECT id FROM signals 
                   WHERE match_id = ? AND status = 'pending' 
                   ORDER BY timestamp DESC LIMIT 1""",
                (str(match_id),)
            ) as cursor:
                row = await cursor.fetchone()
                if not row:
                    return False
                
                await db.execute(
                    "UPDATE signals SET status = ?, final_score = ? WHERE id = ?",
                    (status, final_score, row[0])
                )
                await db.commit()
                logger.info(f"📊 Sonuç: {match_id} -> {status}")
                return True
    except Exception as e:
        logger.error(f"DB güncelleme hatası: {e}")
        return False

# İstatistik parse fonksiyonu
async def get_stats_from_fixture(fixture_data):
    """Sportmonks fixture verisinden istatistikleri çıkarır"""
    s = {
        'home_sot': 0, 'away_sot': 0,
        'home_shots': 0, 'away_shots': 0,
        'home_corners': 0, 'away_corners': 0,
        'home_poss': 50, 'away_poss': 50,
        'home_xg': 0.0, 'away_xg': 0.0,
        'has': False
    }
    
    try:
        # Sportmonks type_id'ler (Kendi panelinden doğrula!)
        TYPE_MAP = {
            1: ('home_shots', 'away_shots'),      # Toplam Şut
            2: ('home_sot', 'away_sot'),          # İsabetli Şut
            3: ('home_corners', 'away_corners'),  # Korner
            9: ('home_poss', 'away_poss'),        # Possession %
            26: ('home_xg', 'away_xg'),           # xG (varsa)
        }
        
        stats_list = fixture_data.get('statistics', [])
        
        for stat in stats_list:
            type_id = stat.get('type_id')
            data = stat.get('data', {})
            
            if type_id in TYPE_MAP:
                home_key, away_key = TYPE_MAP[type_id]
                
                if 'xg' in home_key or 'poss' in home_key:
                    s[home_key] = safe_float(data.get('home'), 0.0)
                    s[away_key] = safe_float(data.get('away'), 0.0)
                else:
                    s[home_key] = safe_int(data.get('home'))
                    s[away_key] = safe_int(data.get('away'))
                
                s['has'] = True
                
    except Exception as e:
        logger.error(f"İstatistik parse hatası: {e}")
    
    return s

# AI Yorum fonksiyonu
async def get_ai_insight(home, away, stats, pick, pressure, minute, score, xg=0.0, pick_type="ust", league="Bilinmiyor"):
    """Groq AI'dan yorum alır"""
    if not GROQ_KEY:
        return _fallback_comment(home, stats, pick, pressure, pick_type)

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]

    if len(last_ai_requests) >= MAX_AI_PER_MINUTE:
        return _fallback_comment(home, stats, pick, pressure, pick_type)

    last_ai_requests.append(now)

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    
    h_sot = safe_int(stats.get("home_sot", 0))
    a_sot = safe_int(stats.get("away_sot", 0))
    h_shots = safe_int(stats.get("home_shots", 0))
    h_poss = safe_int(stats.get("home_poss", 50))
    
    prompt = f"""Profesyonel bahis analisti. Maç: {home} vs {away} ({league}) | Skor: {score} | Dakika: {minute}' | Bahis: {pick}.

Canlı Veriler: {h_shots} şut, {h_sot} isabetli, {h_poss}% baskı, xG {xg}.

Görev: Tam 1 cümle yaz (150-200 karakter). İstatistiklerin bahis için anlamı."""

    payload = {
        "model": "llama-3.3-70b-versatile",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 300,
        "top_p": 0.95
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=12.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload, headers=headers
                )
                if r.status_code == 200:
                    raw = r.json()['choices'][0]['message']['content']
                    clean = raw.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '').replace('"', "'").strip()
                    
                    if len(clean) > 260:
                        clean = clean[:257] + "..."
                    if len(clean) < 60:
                        return _fallback_comment(home, stats, pick, pressure, pick_type)
                    
                    logger.info(f"AI yorum: {len(clean)} karakter")
                    return clean
                    
                elif r.status_code == 429:
                    await asyncio.sleep(5)
                else:
                    await asyncio.sleep(3)
                    
        except Exception as e:
            logger.error(f"Groq hatası: {e}")
            await asyncio.sleep(4)

    return _fallback_comment(home, stats, pick, pressure, pick_type)

def _fallback_comment(home, stats, pick, pressure, pick_type="ust"):
    """Yedek yorum"""
    import random
    h_sot = safe_int(stats.get('home_sot', 0))
    templates = {
        'iy': [f"{h_sot} isabetli şutla baskı kuruluyor. Gol yakın.", f"İlk yarı temposu yüksek."],
        'ms': [f"Maçın ikinci yarısında baskı sürüyor. Gol potansiyeli var.", f"İstatistikler üst bahisini destekliyor."],
        'kg': [f"İki taraf da açık oynuyor. Karşılıklı gol olabilir.", f"Defans zafiyetleri KG ihtimalini artırıyor."],
        'default': [f"İstatistiksel veriler {pick} lehine.", f"Baskı skoru ({pressure}%) destekliyor."]
    }
    category = pick_type if pick_type in templates else 'default'
    return random.choice(templates[category])

# Telegram Komutları
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 VIP Pro Trader (Sportmonks Pro) Aktif!\n\n"
        "/kontrol - Sistem kontrolü\n"
        "/rapor - Performans raporu"
    )

async def kontrol_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("✅ Sistem çalışıyor (Sportmonks Pro API)")

async def rapor_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT COUNT(*) as total, SUM(CASE WHEN status LIKE '%WIN%' THEN 1 ELSE 0 END) as wins, SUM(CASE WHEN status LIKE '%LOSS%' THEN 1 ELSE 0 END) as losses FROM signals WHERE timestamp > ?", 
                (time.time() - 7*24*3600,)
            ) as cursor:
                row = await cursor.fetchone()
                total, wins, losses = row['total'], row['wins'], row['losses']
                rate = round((wins/(wins+losses)*100), 1) if (wins+losses) > 0 else 0
                
                text = (
                    f"📈 SON 7 GÜN\n\n"
                    f"🎯 Toplam: {total}\n"
                    f"✅ Kazanan: {wins}\n"
                    f"❌ Kaybeden: {losses}\n"
                    f"📊 Başarı: %{rate}"
                )
                await update.message.reply_text(text)
    except Exception as e:
        await update.message.reply_text(f"Hata: {e}")

# Ana Monitör
async def signal_monitor(app):
    logger.info("🚀 Monitör başladı (Sportmonks Pro)")
    
    LIVES_URL = f"{BASE_URL}/livescores?api_token={SPORTMONKS_API_KEY}&include=statistics;participants;league"
    
    while True:
        try:
            data = await fetch_api(LIVES_URL)
            fixtures = data.get('data', [])
            
            if not fixtures:
                logger.warning("⚠️ Canlı maç bulunamadı")
                await asyncio.sleep(60)
                continue
            
            logger.info(f"📊 {len(fixtures)} canlı maç")
            
            for fixture in fixtures:
                try:
                    match_id = str(fixture.get('id'))
                    minute = safe_int(fixture.get('minute'))
                    
                    if not (10 < minute < 85):
                        logger.info(f"⏭ {match_id}: Dakika dışı ({minute})")
                        continue
                    
                    # Skor bilgisi
                    scores = fixture.get('scores', {})
                    home_score = safe_int(scores.get('home'))
                    away_score = safe_int(scores.get('away'))
                    
                    if home_score + away_score > 4:
                        logger.info(f"⏭ {match_id}: Çok gollü")
                        continue
                    
                    # İstatistikleri al
                    stats = await get_stats_from_fixture(fixture)
                    
                    if not stats['has']:
                        logger.info(f"⏭ {match_id}: İstatistik yok")
                        continue
                    
                    # Takım isimleri
                    participants = fixture.get('participants', [])
                    home_name, away_name = "Ev", "Dep"
                    for p in participants:
                        if p.get('meta', {}).get('location') == 'home':
                            home_name = p.get('name', 'Ev')
                        else:
                            away_name = p.get('name', 'Dep')
                    
                    league_name = fixture.get('league', {}).get('name', 'Lig')
                    
                    # Brain analizi
                    m = {
                        'id': match_id,
                        'homeTeam': {'name': home_name},
                        'awayTeam': {'name': away_name},
                        'homeScore': {'current': home_score},
                        'awayScore': {'current': away_score},
                        'tournament': {'name': league_name}
                    }
                    
                    res = brain.analyze_advanced(m, stats, minute)
                    
                    if not res.get('is_signal'):
                        reason = res.get('reason', '')
                        if any(x in reason for x in ['[A1]', '[A2]', '[A3]', '[A4]']):
                            logger.info(f"⏭ {match_id}: {reason}")
                        continue
                    
                    # Cooldown kontrolü
                    pick = res['pick']
                    now = time.time()
                    last_sent = await get_last_signal_time(match_id, pick)
                    
                    if now - last_sent < SIGNAL_COOLDOWN:
                        continue
                    
                    is_repeat = last_sent > 0
                    
                    # AI yorumu
                    ai_msg = await get_ai_insight(
                        home_name, away_name, stats, res['pick'], 
                        res['pressure'], minute, res['score'], 
                        res.get('xg', 0.0), res.get('pick_type', 'ust'), league_name
                    )
                    
                    # Mesaj
                    repeat_text = " (GÜNCELLEME)" if is_repeat else ""
                    
                    txt = (
                        f"📡 SİNYAL{repeat_text} | 🏆 {league_name}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"⚽ {home_name} {res['score']} {away_name}\n"
                        f"⏱️ {minute}' | {res['period']}\n"
                        f"🎯 {res['pick']}\n"
                        f"📊 {res['confidence']} {res['prob']}%\n"
                        f"⚠️ {res['risk']}\n"
                        f"━━━━━━━━━━━━━━\n"
                        f"🧠 {ai_msg}\n"
                        f"💎 VIP Pro Trader"
                    )
                    
                    await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=None)
                    await add_signal(match_id, f"{home_name} vs {away_name}", pick, res['total_score'], league_name)
                    logger.info(f"✅ Sinyal gönderildi: {match_id}")
                    
                    await asyncio.sleep(2)
                    
                except Exception as e:
                    logger.error(f"Maç hatası: {e}")
                    continue
            
        except Exception as e:
            logger.error(f"Monitör hatası: {e}")
            logger.error(traceback.format_exc())
        
        await asyncio.sleep(60)

# Sonuç Takipçisi
async def result_tracker(app):
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM signals WHERE status = 'pending' AND timestamp < ?", 
                    (time.time() - 3600,)
                ) as cursor:
                    pending = await cursor.fetchall()
            
            for sig in pending:
                try:
                    url = f"{BASE_URL}/fixtures/{sig['match_id']}?api_token={SPORTMONKS_API_KEY}"
                    data = await fetch_api(url)
                    fixture = data.get('data', {})
                    
                    if fixture.get('status') == 'FT' or fixture.get('state_id') == 5:
                        scores = fixture.get('scores', {})
                        hs = safe_int(scores.get('home'))
                        as_ = safe_int(scores.get('away'))
                        
                        is_win = (hs + as_) > sig['start_total']
                        await update_signal_result(sig['match_id'], f"{hs}-{as_}", is_win)
                        
                except Exception as e:
                    continue
        except Exception as e:
            logger.error(f"Tracker hatası: {e}")
        await asyncio.sleep(600)

async def post_init(app):
    await init_db()
    asyncio.create_task(signal_monitor(app))
    asyncio.create_task(result_tracker(app))
    logger.info("✅ Bot başlatıldı")

async def error_handler(update, context):
    logger.error("Hata:", exc_info=context.error)

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("kontrol", kontrol_command))
    application.add_handler(CommandHandler("rapor", rapor_command))
    application.add_error_handler(error_handler)
    application.run_polling()
