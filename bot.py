import os, asyncio, httpx, json, time, logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# ====================== LOGGING ======================
logging.basicConfig(
    format='%(asctime)s | %(levelname)s | %(message)s',
    level=logging.INFO,
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()]
)
logger = logging.getLogger(__name__)

# ====================== CONFIG ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

brain = BettingBrain()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

LIVE_URL = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"

last_ai_requests = []
MAX_AI_REQUESTS_PER_MINUTE = 20

# ====================== GROQ AI ======================
async def get_ai_insight(home, away, stats, pick, pressure, minute, score):
    if not GROQ_KEY:
        return "AI analizi şu anda kullanılamıyor."

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]
    
    if len(last_ai_requests) >= MAX_AI_REQUESTS_PER_MINUTE:
        return f"{home} takımının isabetli şut ve baskı üstünlüğü gol beklentisini artırıyor."

    last_ai_requests.append(now)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    prompt_text = (
        f"Sen bir profesyonel bahis analistisin. Şu maçı kısaca analiz et:\n"
        f"Maç: {home} vs {away}\n"
        f"Dakika: {minute}'\n"
        f"Skor: {score}\n"
        f"İsabetli Şut: {stats.get('home_sot',0)}-{stats.get('away_sot',0)}\n"
        f"Toplam Şut: {stats.get('home_shots',0)}-{stats.get('away_shots',0)}\n"
        f"Korner: {stats.get('home_corners',0)}-{stats.get('away_corners',0)}\n"
        f"Top Hakimiyeti: %{stats.get('home_poss',50)}-%{stats.get('away_poss',50)}\n"
        f"Baskı Gücü: %{pressure}\n"
        f"Önerilen Bahis: {pick}\n\n"
        f"KURALLAR:\n"
        f"- Maksimum 2 cümle yaz\n"
        f"- Neden mantıklı olduğunu açıkla\n"
        f"- Özel karakter kullanma\n"
        f"- Banko, kesin gibi kelimeler kullanma"
    )

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.7,
        "max_tokens": 150
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, json=payload, headers=headers)
                if r.status_code == 200:
                    data = r.json()
                    comment = data['choices'][0]['message']['content']
                    clean = comment.replace('*', '').replace('_', '').replace('`', '').replace('[', '').replace(']', '').strip()
                    return clean
        except:
            pass
    return f"{home} takımının isabetli şut ve baskı puanı gol olasılığını artırıyor."


# ====================== GERÇEK ORAN (DÜZELTİLDİ) ======================
async def get_live_odds(match_id, pick):
    try:
        url = f"https://www.sofascore.com/api/v1/event/{match_id}/odds/2/all"
        data = await fetch_api(url)
        
        if not data or 'odds' not in data:
            logger.warning(f"Oran verisi alınamadı → Match ID: {match_id}")
            return 1.55

        pick_lower = pick.lower()
        for bookmaker in data.get('odds', []):
            for market in bookmaker.get('markets', []):
                for outcome in market.get('outcomes', []):
                    name = outcome.get('name', '').lower()
                    price = float(outcome.get('price', 1.55))
                    if ('üst' in pick_lower or 'over' in pick_lower) and 'over' in name:
                        logger.info(f"✅ Oran bulundu: {pick} = {price}")
                        return round(price, 2)
                    if ('kg' in pick_lower or 'both' in pick_lower) and any(x in name for x in ['both', 'her iki', 'kg var', 'yes']):
                        logger.info(f"✅ Oran bulundu: {pick} = {price}")
                        return round(price, 2)
        logger.warning(f"Oran bulunamadı → {pick}")
        return 1.55
    except Exception as e:
        logger.error(f"Oran çekme hatası: {e}")
        return 1.55


# ====================== YARDIMCI FONKSİYONLAR ======================
async def fetch_api(url):
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
    except:
        return {}

def get_real_minute(m):
    try:
        status = m.get('status', {})
        elapsed = status.get('elapsed', 0)
        if status.get('type') == 'HT' or 'half time' in str(status.get('description','')).lower():
            return "İY"
        if status.get('type') == 'FT' or 'full time' in str(status.get('description','')).lower():
            return "MS"
        if status.get('type') == '2H' and elapsed < 45:
            elapsed += 45
        if elapsed <= 1 and m.get('startTimestamp'):
            elapsed = max(1, (int(time.time()) - m.get('startTimestamp')) // 60)
        return f"{int(elapsed)}'"
    except:
        return "45'"

async def manage_history(mode="read", data=None):
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            if mode == "read":
                r = await client.get(url, headers=GIST_HEADERS)
                return json.loads(r.json()['files']['sent_signals.json']['content'])
            else:
                payload = {"files": {"sent_signals.json": {"content": json.dumps(data)}}}
                await client.patch(url, headers=GIST_HEADERS, json=payload)
        except:
            return [] if mode == "read" else None

async def get_stats(match_id):
    url = STATS_URL.format(match_id)
    data = await fetch_api(url)
    if not data or 'statistics' not in data:
        return None

    s = {'home_sot':0, 'away_sot':0, 'home_shots':0, 'away_shots':0, 
         'home_corners':0, 'away_corners':0, 'home_poss':50, 'away_poss':50, 'has':False}
    
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        n = i.get('name')
                        try:
                            hv = int(float(str(i.get('homeValue', 0)).replace('%','').strip()))
                            av = int(float(str(i.get('awayValue', 0)).replace('%','').strip()))
                        except:
                            hv = av = 0

                        if n == 'Shots on target':
                            s['home_sot'], s['away_sot'], s['has'] = hv, av, True
                        elif n == 'Total shots':
                            s['home_shots'], s['away_shots'], s['has'] = hv, av, True
                        elif n == 'Corner kicks':
                            s['home_corners'], s['away_corners'] = hv, av
                        elif n == 'Ball possession':
                            s['home_poss'], s['away_poss'] = hv, av
        return s if s['has'] else None
    except Exception as e:
        logger.error(f"İstatistik hatası: {e}")
        return None


# ====================== KOMUTLAR ve DÖNGÜLER ======================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *VIP Pro Trader Bot Aktif!*", parse_mode=ParseMode.MARKDOWN)

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = await fetch_api(LIVE_URL)
    events = data.get('events', [])
    if not events:
        await update.message.reply_text("📭 Şu an canlı maç yok.")
        return
    text = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        m_min = get_real_minute(m)
        h, a = m['homeTeam']['name'], m['awayTeam']['name']
        sh, sa = m.get('homeScore', {}).get('current', 0), m.get('awayScore', {}).get('current', 0)
        text += f"⏱ `{m_min}` | {h} *{sh}-{sa}* {a}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    api_data = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")
    ai_test = await get_ai_insight("TestA", "TestB", {'home_sot':3,'away_sot':1,'home_shots':8,'away_shots':3,'home_corners':4,'away_corners':1,'home_poss':60,'away_poss':40}, "1.5 ÜST", 70, 55, "1-0")
    
    delivery = "✅ OK"
    try: 
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Sistem Testi")
    except: 
        delivery = "❌ HATA"
    
    ai_status = "✅ OK" if len(ai_test) > 10 else "❌ HATA"
    
    report = f"🛡 *BOT DENETİM RAPORU*\n\n🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n🧠 AI: {ai_status}\n🚀 _Sistem aktif!_"
    await msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)


async def result_tracker(app):
    while True:
        try:
            history = await manage_history("read")
            updated = False
            for sig in history[-20:]:
                if sig.get('status') == 'pending' and (time.time() - sig['timestamp']) > 3600:
                    r = await fetch_api(f"https://www.sofascore.com/api/v1/event/{sig['id']}")
                    ev = r.get('event', {})
                    if ev.get('status', {}).get('type') == 'finished':
                        is_win = (ev['homeScore']['current'] + ev['awayScore']['current']) > sig['start_total']
                        sig['status'] = 'WIN ✅' if is_win else 'LOSS ❌'
                        sig['final_score'] = f"{ev['homeScore']['current']}-{ev['awayScore']['current']}"
                        updated = True
            if updated: 
                await manage_history("write", history)
        except:
            pass
        await asyncio.sleep(600)


async def signal_monitor(app):
    logger.info("🚀 Pro Monitör Başladı...")
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            events = data.get('events', [])
            history = await manage_history("read")
            sent_ids = [str(x['id']) for x in history]

            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                try: 
                    mn_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: 
                    mn_int = 0

                if mid not in sent_ids and 10 < mn_int < 85:
                    stats = await get_stats(mid)
                    if stats:
                        res = brain.analyze_advanced(m, stats, mn_int)
                        if res.get('is_signal'):
                            real_odds = await get_live_odds(mid, res['pick'])
                            if real_odds < 1.38:
                                continue
                            ai_msg = await get_ai_insight(m['homeTeam']['name'], m['awayTeam']['name'], stats, res['pick'], res['pressure'], mn_int, res['score'])
                            # ... (mesaj gönderme kısmı aynı kalıyor)
                            # Senin son kodundan kopyala
        except Exception as e:
            logger.error(f"Döngü hatası: {e}")
        await asyncio.sleep(180)


async def post_init(app):
    asyncio.create_task(signal_monitor(app))
    asyncio.create_task(result_tracker(app))


if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    logger.info("✅ Bot Hazır!")
    app.run_polling()
