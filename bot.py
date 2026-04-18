import os, asyncio, httpx, json, time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# Railway Değişkenleri
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

brain = BettingBrain()
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

# --- DAKİKA HESAPLAMA (KESİN ÇÖZÜM) ---

def get_real_minute(m):
    status = m.get('status', {})
    desc = status.get('description', '')
    
    if desc == 'HT': return "İY"
    if desc == 'FT': return "MS"
    
    # 1. Öncelik: SofaScore'un kendi elapsed (geçen süre) verisi
    elapsed = status.get('elapsed')
    
    # 2. Eğer elapsed verisi 0, 1 veya None ise 'time' objesinden hesapla
    if not elapsed or elapsed <= 1:
        current_seconds = m.get('time', {}).get('current')
        if current_seconds:
            elapsed = (current_seconds // 60) + 1
            
    # 3. Eğer hala sonuç alınamadıysa bilgisayar saatiyle startTimestamp farkına bak
    if not elapsed or elapsed <= 1:
        start_ts = m.get('startTimestamp')
        if start_ts:
            now_ts = int(time.time())
            diff = (now_ts - start_ts) // 60
            if 0 < diff < 130: # Mantıklı aralıktaysa
                elapsed = diff

    return f"{elapsed or 1}'"

# --- API VE BULUT HAFIZA ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except: return {}

async def load_history():
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GIST_HEADERS)
            content = r.json()['files']['sent_signals.json']['content']
            return set(json.loads(content))
        except: return set()

async def save_history(sent_set):
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {"files": {"sent_signals.json": {"content": json.dumps(list(sent_set))}}}
    async with httpx.AsyncClient() as client:
        try: await client.patch(url, headers=GIST_HEADERS, json=data)
        except: pass

# --- YENİ KOMUTLAR ---

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kontrol: Botun tüm fonksiyonlarını test eder."""
    status_msg = await update.message.reply_text("⚙️ *Sistem Kontrolü Başlatılıyor...*", parse_mode=ParseMode.MARKDOWN)
    
    # 1. API Kontrolü
    api_data = await fetch_api(URL)
    api_res = "✅ OK" if api_data else "❌ HATA"
    
    # 2. Gist Kontrolü
    gist_data = await load_history()
    gist_res = "✅ OK" if isinstance(gist_data, set) else "❌ HATA"
    
    # 3. Brain Kontrolü
    try:
        test_brain = brain.calculate_pressure({'sot': 5, 'da': 30}, 20)
        brain_res = "✅ OK" if test_brain > 0 else "❌ HATA"
    except: brain_res = "❌ HATA"

    report = (
        f"🖥 *BOT DURUM RAPORU*\n\n"
        f"📡 *SofaScore API:* {api_res}\n"
        f"☁️ *GitHub Gist:* {gist_res}\n"
        f"🧠 *Analiz Motoru:* {brain_res}\n"
        f"⏰ *Server Saati:* {time.strftime('%H:%M:%S')}\n\n"
        f"🚀 _Sistem sorunsuz çalışıyor!_" if "❌" not in (api_res, gist_res, brain_res) else "⚠️ _Sorun tespit edildi!_"
    )
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fetch_api(URL)
    events = data.get('events', [])
    if not events: await update.message.reply_text("Canlı maç yok."); return
    
    msg = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        minute = get_real_minute(m)
        h = m.get('homeTeam', {}).get('shortName') or m.get('homeTeam', {}).get('name')
        a = m.get('awayTeam', {}).get('shortName') or m.get('awayTeam', {}).get('name')
        score_h = m.get('homeScore', {}).get('current', 0)
        score_a = m.get('awayScore', {}).get('current', 0)
        msg += f"⏱ `{minute}` | {h} *{score_h}-{score_a}* {a}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# --- ANALİZ DÖNGÜSÜ ---

async def get_stats(match_id):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
    data = await fetch_api(url)
    stats = {'home_sot': 0, 'away_sot': 0, 'home_da': 0, 'away_da': 0}
    for p in data.get('statistics', []):
        if p.get('period') == 'ALL':
            for g in p.get('groups', []):
                for i in g.get('statisticsItems', []):
                    if i['name'] == 'Shots on target':
                        stats['home_sot'] = int(i['homeValue']); stats['away_sot'] = int(i['awayValue'])
                    if i['name'] == 'Dangerous attacks':
                        stats['home_da'] = int(i['homeValue']); stats['away_da'] = int(i['awayValue'])
    return stats

async def signal_monitor(app):
    sent_signals = await load_history()
    while True:
        try:
            data = await fetch_api(URL)
            events = data.get('events', [])
            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                try:
                    minute_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: minute_int = 0

                if mid not in sent_signals and 10 < minute_int < 85:
                    stats = await get_stats(mid)
                    res = brain.analyze_advanced(m, stats, minute_int)
                    if res['is_signal']:
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
                        sent_signals.add(mid); await save_history(sent_signals)
            await asyncio.sleep(150)
        except: await asyncio.sleep(10)

async def post_init(app): asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    app.run_polling()
