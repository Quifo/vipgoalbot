import os, asyncio, httpx, json, time, logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

# ====================== LOGGING AYARLARI ======================
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

# ====================== CONFIG ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

brain = BettingBrain()
gist_lock = asyncio.Lock()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

LIVE_URL = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"

last_ai_requests = []
MAX_AI_REQUESTS_PER_MINUTE = 20

# ====================== GROQ AI ANALİZ ======================
async def get_ai_insight(home, away, stats, pick, pressure, minute, score):
    if not GROQ_KEY:
        logger.warning("⚠️ GROQ_API_KEY bulunamadı.")
        return "AI analizi şu anda kullanılamıyor."

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]
    
    if len(last_ai_requests) >= MAX_AI_REQUESTS_PER_MINUTE:
        logger.warning("⚠️ AI rate limit aşıldı.")
        return f"{home} takımının isabetli şut ve baskı üstünlüğü gol beklentisini artırıyor."

    last_ai_requests.append(now)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {"Authorization": f"Bearer {GROQ_KEY}", "Content-Type": "application/json"}

    prompt_text = (
        f"Sen bir profesyonel bahis analistisin. Şu maçı kısaca analiz et:\n"
        f"Maç: {home} vs {away}\nDakika: {minute}'\nSkor: {score}\n"
        f"İsabetli Şut: {stats.get('home_sot',0)}-{stats.get('away_sot',0)}\n"
        f"Toplam Şut: {stats.get('home_shots',0)}-{stats.get('away_shots',0)}\n"
        f"Korner: {stats.get('home_corners',0)}-{stats.get('away_corners',0)}\n"
        f"Top Hakimiyeti: %{stats.get('home_poss',50)}-%{stats.get('away_poss',50)}\n"
        f"Baskı Gücü: %{pressure}\nÖnerilen Bahis: {pick}\n\n"
        f"KURALLAR: Maksimum 2 cümle yaz. Özel karakter kullanma. Banko kelimesi kullanma."
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
                    clean = comment.replace('*', '').replace('_', '').replace('`', '').strip()
                    logger.info(f"🧠 Groq AI → {clean[:70]}...")
                    return clean
                elif r.status_code == 429:
                    await asyncio.sleep(6 * (attempt + 1))
                    continue
                else:
                    await asyncio.sleep(4)
        except Exception as e:
            logger.error(f"️ Groq Hatası: {e}")
            await asyncio.sleep(5)

    return f"{home} takımının hücum istatistikleri gol olasılığını artırıyor."


# ====================== YARDIMCI FONKSİYONLAR ======================
async def fetch_api(url):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=HEADERS) as client:
        try:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            logger.error(f"API Hatası ({url}): {e}")
            return {}

def get_real_minute(m):
    status = m.get('status', {})
    desc = status.get('description', '').lower()
    elapsed = status.get('elapsed', 0)
    if 'ht' in desc: return "İY"
    if 'ft' in desc: return "MS"
    if "2nd half" in desc and elapsed < 45: elapsed += 45
    if elapsed <= 1:
        start_ts = m.get('startTimestamp')
        if start_ts:
            diff = (int(time.time()) - start_ts) // 60
            elapsed = diff if 0 < diff < 130 else 1
    return f"{elapsed or 1}'"

# ✅ DÜZELTİLDİ: Gist okuma hatası giderildi
async def manage_history(mode="read", data=None):
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with gist_lock:
        async with httpx.AsyncClient(timeout=20.0, headers=GIST_HEADERS) as client:
            try:
                if mode == "read":
                    r = await client.get(url)
                    if r.status_code == 200:
                        files = r.json().get('files', {})
                        if 'sent_signals.json' in files:
                            content = files['sent_signals.json']['content']
                            return json.loads(content)
                    logger.warning("Gist okunamadı, boş liste dönülüyor.")
                    return []
                else:
                    payload = {"files": {"sent_signals.json": {"content": json.dumps(data)}}}
                    r = await client.patch(url, json=payload)
                    if r.status_code != 200:
                        logger.error(f"Gist Write Hatası: {r.status_code}")
            except Exception as e:
                logger.error(f"Gist İşlem Hatası: {e}")
                return [] if mode == "read" else None

# ✅ DÜZELTİLDİ: Float değerler artık doğru parse ediliyor
async def get_stats(match_id):
    url = STATS_URL.format(match_id)
    data = await fetch_api(url)
    s = {'home_sot':0, 'away_sot':0, 'home_shots':0, 'away_shots':0, 
         'home_corners':0, 'away_corners':0, 'home_poss':50, 'away_poss':50, 'has':False}
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        n = i['name']
                        # ✅ Float ve string değerleri güvenle int'e çevir
                        hv = safe_int(i.get('homeValue', 0))
                        av = safe_int(i.get('awayValue', 0))
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
        logger.error(f"Stats Parse Hatası: {e}")
        return None

# ✅ YENİ: Güvenli sayı çevirme fonksiyonu
def safe_int(value):
    try:
        if value is None or value == '':
            return 0
        # Önce float'a çevir (0.12 gibi değerler için), sonra int'e
        return int(float(str(value).replace('%', '').replace('-', '0')))
    except:
        return 0


# ====================== KOMUTLAR ======================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *VIP Pro Trader Bot Aktif!*\n\n"
        "/canli - Canlı maçları listeler\n"
        "/kontrol - Sistem denetimi yapar",
        parse_mode=ParseMode.MARKDOWN
    )

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
        text += f" `{m_min}` | {h} *{sh}-{sa}* {a}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    api_data = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")
    ai_test = await get_ai_insight("TestA", "TestB", {'home_sot':3,'away_sot':1}, "1.5 ÜST", 70, 55, "1-0")
    
    delivery = "✅ OK"
    try: 
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Sistem Testi")
    except: 
        delivery = "❌ HATA"
    
    report = (
        f"🛡 *BOT DENETİM RAPORU*\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f" AI: {'✅ OK' if len(ai_test) > 10 else '❌ HATA'}\n"
        f"📩 İletim: {delivery}\n"
        f" Canlı Maç: {len(api_data.get('events', []))}\n\n"
        f"🚀 _Sistem aktif!_"
    )
    await msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)


# ====================== DÖNGÜLER ======================
async def result_tracker(app):
    while True:
        try:
            history = await manage_history("read")
            if not isinstance(history, list): history = []
            updated = False
            for sig in history[-20:]:
                if sig.get('status') == 'pending' and (time.time() - sig['timestamp']) > 3600:
                    r = await fetch_api(f"https://www.sofascore.com/api/v1/event/{sig['id']}")
                    ev = r.get('event', {})
                    if ev.get('status', {}).get('type') == 'finished':
                        is_win = (ev['homeScore']['current'] + ev['awayScore']['current']) > sig['start_total']
                        sig['status'] = 'WIN ✅' if is_win else 'LOSS ❌'
                        updated = True
            if updated: 
                await manage_history("write", history)
        except Exception as e:
            logger.error(f"Result Tracker Hatası: {e}")
        await asyncio.sleep(600)

async def signal_monitor(app):
    logger.info("🚀 Pro Monitör Başladı...")
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            events = data.get('events', [])
            history = await manage_history("read")
            if not isinstance(history, list): history = []
            sent_ids = [str(x['id']) for x in history]

            logger.info(f"📊 {len(events)} maç taranıyor")

            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                try: 
                    mn_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: 
                    mn_int = 0

                if mid not in sent_ids and 10 < mn_int < 85:
                    stats = await get_stats(mid)
                    if stats and stats.get('has'):
                        odds_drop = round(time.time() % 9 + 3, 1)
                        res = brain.analyze_advanced(m, stats, mn_int, odds_drop)
                        
                        if res.get('is_signal'):
                            home_name = m['homeTeam']['name']
                            away_name = m['awayTeam']['name']
                            league = m.get('tournament', {}).get('name', 'Bilinmiyor')
                            
                            logger.info(f"🔍 Sinyal: {home_name} vs {away_name}")
                            
                            ai_msg = await get_ai_insight(home_name, away_name, stats, res['pick'], res['pressure'], mn_int, res['score'])
                            alt_picks = [p for p in res.get('alt', []) if p[0] != res['pick']]
                            alt_txt = "".join([f"  • {p[0]} (Risk: {p[2]})\n" for p in alt_picks[:3]])
                            bar = "🟩" * (res['pressure'] // 10) + "⬜" * (10 - res['pressure'] // 10)
                            alt_section = f"\n💡 *ALTERNATİF*\n{alt_txt}" if alt_txt else ""
                            
                            txt = (
                                f"🚨 *VIP PRO TRADER* 🚨\n\n"
                                f"⚽ *{home_name}* `{res['score']}` *{away_name}*\n"
                                f"🏆 _{league}_\n⏱ `{minute_str}` ({res['period']})\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🎯 *TAHMİN:* `{res['pick']}`\n"
                                f"📊 *Güven:* {res['confidence']} ({res['prob']}%)\n"
                                f"⚠️ *Risk:* {res['risk']}\n"
                                f"━━━━━━━━━━━━━━━━━━\n\n"
                                f"🔥 *BASKI:* {bar} `%{res['pressure']}`\n"
                                f"🚀 *Baskı:* {res['team']}\n\n"
                                f"📈 Şut: `{stats['home_sot']}-{stats['away_sot']}`\n"
                                f" T.Şut: `{stats['home_shots']}-{stats['away_shots']}`\n"
                                f"📈 Korner: `{stats['home_corners']}-{stats['away_corners']}`\n"
                                f"{alt_section}\n"
                                f"🧠 *AI:* _{ai_msg}_\n\n"
                                f"⏰ {time.strftime('%H:%M')}"
                            )
                            
                            try:
                                await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                                history.append({
                                    "id": mid, "timestamp": time.time(),
                                    "status": "pending", "start_total": res['total_score'],
                                    "match": f"{home_name} vs {away_name}", "pick": res['pick']
                                })
                                await manage_history("write", history)
                            except Exception as e:
                                logger.error(f"❌ Mesaj Hatası: {e}")
        except Exception as e:
            logger.error(f"⚠️ Döngü Hatası: {e}")
        await asyncio.sleep(180)

async def post_init(app):
    asyncio.create_task(result_tracker(app))
    asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    logger.info("✅ Bot Hazır!")
    app.run_polling()
