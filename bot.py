import os, asyncio, httpx, json, time
from datetime import datetime
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# --- AYARLAR ---
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

brain = BettingBrain()

# --- KRİTİK DEĞİŞKENLER (BURASI EKSİKTİ) ---
LIVE_URL = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://www.sofascore.com/",
    "Origin": "https://www.sofascore.com"
}

GIST_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

# --- YARDIMCI FONKSİYONLAR ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            print(f"⚠️ API Hatası: {e}")
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
            now_ts = int(time.time())
            diff = (now_ts - start_ts) // 60
            if 0 < diff < 120: elapsed = diff
    return f"{elapsed or 1}'"

# --- BULUT HAFIZA (GIST) ---

async def load_history_cloud():
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(url, headers=GIST_HEADERS)
            if r.status_code == 200:
                files = r.json().get('files', {})
                content = files.get('sent_signals.json', {}).get('content', '[]')
                return set(json.loads(content))
        except: pass
        return set()

async def save_history_cloud(sent_set):
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {"files": {"sent_signals.json": {"content": json.dumps(list(sent_set))}}}
    async with httpx.AsyncClient(timeout=20.0) as client:
        try: await client.patch(url, headers=GIST_HEADERS, json=data)
        except: print("⚠️ Bulut kaydı başarısız.")

# --- İSTATİSTİK ÇEKME ---

async def get_stats(match_id):
    url = STATS_URL.format(match_id)
    data = await fetch_api(url)
    stats = {'home_sot': 0, 'away_sot': 0, 'home_da': 0, 'away_da': 0, 'has_data': False}
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        if i['name'] == 'Shots on target':
                            stats['home_sot'] = int(i['homeValue']); stats['away_sot'] = int(i['awayValue'])
                            stats['has_data'] = True
                        if i['name'] == 'Dangerous attacks':
                            stats['home_da'] = int(i['homeValue']); stats['away_da'] = int(i['awayValue'])
                            stats['has_data'] = True
    except: pass
    return stats if stats['has_data'] else None

# --- KOMUTLAR ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *VIP Bahis Algoritması Aktif\\!*", parse_mode=ParseMode.MARKDOWN)

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
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
    gist_data = await load_history_cloud()
    delivery = "✅ OK"
    try: await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Sistem Testi")
    except: delivery = "❌ HATA"
    report = f"🛡 *DENETİM*\n\n🌐 API: {'✅' if api_data else '❌'}\n💾 Gist: {'✅' if isinstance(gist_data, set) else '❌'}\n📩 Kanal: {delivery}"
    await update.message.reply_text(report, parse_mode=ParseMode.MARKDOWN)

# --- SİNYAL MONİTÖRÜ (HIZLANDIRILMIŞ) ---

async def signal_monitor(app):
    print("🚀 Sinyal Monitörü Başladı...")
    sent_signals = await load_history_cloud()
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            events = data.get('events', [])
            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                try:
                    minute_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: minute_int = 0

                if mid not in sent_signals and 10 < minute_int < 85:
                    stats = await get_stats(mid)
                    if stats:
                        res = brain.analyze_advanced(m, stats, minute_int)
                        if res.get('is_signal'):
                            txt = (
                                f"🚨 *VIP GOL SİNYALİ* 🚨\n\n"
                                f"🏟 *MAÇ:* {m['homeTeam']['name']} vs {m['awayTeam']['name']}\n"
                                f"⏰ *DAKİKA:* {minute_str} ({res['period']})\n"
                                f"🔥 *BASKI GÜCÜ:* %{res['pressure']}\n"
                                f"🎯 *DURUM:* {res['stats_summary']}\n"
                                f"🏆 *TAHMİN:* {res['pick']}\n\n"
                                f"🚀 *BASKIDAKİ TAKIM:* {res['team']}\n"
                                f"💸 *STAKE:* 4/10"
                            )
                            await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                            sent_signals.add(mid)
                            await save_history_cloud(sent_signals)
                            print(f"✅ Sinyal Atıldı: {m['homeTeam']['name']}")
        except Exception as e: print(f"Döngü hatası: {e}")
        
        await asyncio.sleep(90) # 90 saniyede bir hızlı kontrol

# --- BAŞLATICI ---
async def post_init(application):
    asyncio.create_task(signal_monitor(application))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    app.run_polling()
