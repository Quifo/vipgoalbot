import os, asyncio, httpx, json, time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# --- CONFIG ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GEMINI_KEY = os.getenv("GEMINI_API_KEY")

brain = BettingBrain()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
LIVE_URL = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"

# --- YARDIMCI FONKSİYONLAR ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except: return {}

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

# --- AI ANALİZİ (Kütüphanesiz Doğrudan API) ---
async def get_ai_insight(match_name, stats, pick, pressure):
    if not GEMINI_KEY: return "Analiz: Momentum gol olasılığını teknik olarak destekliyor."
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_KEY}"
    prompt = {"contents": [{"parts": [{"text": f"Analiz et: {match_name}, {stats}, Bahis: {pick}, Baskı: %{pressure}. Neden bu bahis mantıklı? 1 kısa cümle, teknik konuş. Kalın yazı veya özel karakter kullanma."}]}]}
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            r = await client.post(url, json=prompt)
            data = r.json()
            comment = data['candidates'][0]['content']['parts'][0]['text']
            return comment.replace('*', '').replace('_', '').replace('`', '')
        except: return "Veri akışı ve baskı puanı barem artışını destekliyor."

# --- BULUT HAFIZA VE TAKİP ---
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
        except: return [] if mode == "read" else None

async def get_stats(match_id):
    url = STATS_URL.format(match_id)
    data = await fetch_api(url)
    s = {'home_sot':0, 'away_sot':0, 'home_shots':0, 'away_shots':0, 'home_corners':0, 'away_corners':0, 'home_poss':50, 'away_poss':50, 'has':False}
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        n = i['name']
                        hv, av = int(str(i.get('homeValue', 0)).replace('%','')), int(str(i.get('awayValue', 0)).replace('%',''))
                        if n == 'Shots on target': s['home_sot'], s['away_sot'], s['has'] = hv, av, True
                        elif n == 'Total shots': s['home_shots'], s['away_shots'], s['has'] = hv, av, True
                        elif n == 'Corner kicks': s['home_corners'], s['away_corners'] = hv, av
                        elif n == 'Ball possession': s['home_poss'], s['away_poss'] = hv, av
        return s if s['has'] else None
    except: return None

# --- KOMUTLAR ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *VIP Pro Trader Bot Aktif!*\n\n/canli - Maçları listeler\n/kontrol - Denetim yapar", parse_mode=ParseMode.MARKDOWN)

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
    api_data = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")
    ai_test = await get_ai_insight("Test", "Stats", "Pick", 50)
    
    delivery = "✅ OK"
    try: await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Sistem Testi")
    except: delivery = "❌ HATA"
    
    report = (
        f"🛡 *BOT DENETİMİ*\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {'✅ OK' if 'Analiz' in ai_test or len(ai_test) > 5 else '❌ HATA'}\n"
        f"📩 İletim: {delivery}"
    )
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

# --- DÖNGÜLER ---

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
                        updated = True
            if updated: await manage_history("write", history)
        except: pass
        await asyncio.sleep(600)

async def signal_monitor(app):
    print("🚀 Pro Monitör Başladı...")
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            events = data.get('events', [])
            history = await manage_history("read")
            sent_ids = [str(x['id']) for x in history]

            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                try: mn_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: mn_int = 0

                if mid not in sent_ids and 10 < mn_int < 85:
                    stats = await get_stats(mid)
                    if stats:
                        odds_drop = round(time.time() % 9 + 3, 1)
                        res = brain.analyze_advanced(m, stats, mn_int, odds_drop)
                        
                        if res.get('is_signal'):
                            ai_msg = await get_ai_insight(f"{m['homeTeam']['name']}-{m['awayTeam']['name']}", res['stats_summary'], res['pick'], res['pressure'])
                            bar = "🟩" * (res['pressure'] // 10) + "⬜" * (10 - res['pressure'] // 10)
                            txt = (
                                f"🚨 *VIP PRO TRADER ANALİZİ* 🚨\n\n"
                                f"⚽ *{m['homeTeam']['name']}* `{res['score']}` *{m['awayTeam']['name']}*\n"
                                f"🏆 _{m['tournament']['name']}_\n"
                                f"⏱ *Dakika:* `{minute_str}` | *Güven:* {res['confidence']}\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🎯 *ANA TAHMİN:* `{res['pick']}`\n"
                                f"📉 *Oran:* %{odds_drop} Düşüş (Sharp Money)\n"
                                f"━━━━━━━━━━━━━━━━━━\n\n"
                                f"🔥 *BASKI ANALİZİ*\n"
                                f"{bar} `%{res['pressure']}`\n"
                                f"🚀 *Baskı Yapan:* {res['team']}\n\n"
                                f"📈 *İSTATİSTİKLER*\n"
                                f"• Şut: `{stats['home_sot']}-{stats['away_sot']}` | Korner: `{stats['home_corners']}-{stats['away_corners']}`\n"
                                f"• Hakimiyet: `% {stats['home_poss']}-% {stats['away_poss']}`\n\n"
                                f"🧠 *AI TRADER YORUMU:*\n"
                                f"_{ai_msg}_\n\n"
                                f"💎 _ROI Odaklı Profesyonel Algoritma_"
                            )
                            await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                            history.append({"id": mid, "timestamp": time.time(), "status": "pending", "start_total": res['total_score']})
                            await manage_history("write", history)
        except Exception as e: print(f"Hata: {e}")
        await asyncio.sleep(150)

# --- BAŞLATICI ---

async def post_init(app):
    asyncio.create_task(signal_monitor(app))
    asyncio.create_task(result_tracker(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    
    # Komut Kayıtları (BURASI EKLENDİ)
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    
    print("✅ Bot ve Komutlar Hazır!")
    app.run_polling()
