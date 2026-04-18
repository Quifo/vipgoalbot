import os, asyncio, httpx, json, time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# Ayarlar
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

brain = BettingBrain()
HEADERS = {"User-Agent": "Mozilla/5.0"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

# --- DAKİKA HESAPLAMA (KESİN ÇÖZÜM) ---

def get_real_minute(m):
    status = m.get('status', {})
    desc = status.get('description', '')
    if desc == 'HT': return "İY"
    if desc == 'FT': return "MS"

    # 1. SofaScore'un sunduğu elapsed verisi
    elapsed = status.get('elapsed')
    
    # 2. Eğer elapsed 0 veya 1 ise alternatif hesaplama (Saniye üzerinden)
    if not elapsed or elapsed <= 1:
        current_s = m.get('time', {}).get('current')
        if current_s:
            elapsed = (current_s // 60) + 1
            
    # 3. Hala 0' ise startTimestamp üzerinden hesapla (En garantisi)
    if not elapsed or elapsed <= 1:
        start_ts = m.get('startTimestamp')
        if start_ts:
            now_ts = int(time.time())
            diff = (now_ts - start_ts) // 60
            if 0 < diff < 120: # Mantıklı bir aralıktaysa
                elapsed = diff

    return f"{elapsed or 1}'"

# --- API VE BULUT SİSTEMİ ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            print(f"⚠️ API Hatası: {e}")
            return {}

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

async def load_history():
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GIST_HEADERS)
            if r.status_code == 200:
                files = r.json().get('files', {})
                content = files.get('sent_signals.json', {}).get('content', '[]')
                return set(json.loads(content))
            return set()
        except: return set()

async def save_history(sent_set):
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {"files": {"sent_signals.json": {"content": json.dumps(list(sent_set))}}}
    async with httpx.AsyncClient() as client:
        try: await client.patch(url, headers=GIST_HEADERS, json=data)
        except: print("⚠️ Gist yazma hatası")

# --- KOMUTLAR ---

async def live_command(update, context):
    print("📥 /canli komutu tetiklendi.")
    data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
    events = data.get('events', [])
    if not events: await update.message.reply_text("Canlı maç yok."); return
    
    msg = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        minute = get_real_minute(m)
        h = m['homeTeam'].get('shortName') or m['homeTeam']['name']
        a = m['awayTeam'].get('shortName') or m['awayTeam']['name']
        sh = m.get('homeScore', {}).get('current', 0)
        sa = m.get('awayScore', {}).get('current', 0)
        msg += f"⏱ `{minute}` | {h} *{sh}-{sa}* {a}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# --- SİNYAL MONİTÖRÜ ---

async def signal_monitor(app):
    print("🚀 Sinyal tarama döngüsü başlatıldı...")
    sent_signals = await load_history()
    
    while True:
        try:
            data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
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
                        sent_signals.add(mid)
                        await save_history(sent_signals)
                        print(f"✅ Sinyal Gönderildi: {m['homeTeam']['name']}")
            
            await asyncio.sleep(180) # 3 dakikada bir tarama
        except Exception as e:
            print(f"⚠️ Döngüde Hata: {e}")
            await asyncio.sleep(10)

# --- BAŞLATICI ---

async def post_init(application):
    # Railway'de donmayı önlemek için döngüyü ayrı bir task olarak başlatıyoruz
    asyncio.create_task(signal_monitor(application))

if __name__ == "__main__":
    print("🤖 Bot ana süreci başlıyor...")
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("canli", live_command))
    application.add_handler(CommandHandler("live", live_command))
    
    print("✅ Polling başlatılıyor. Bot artık komut dinliyor.")
    application.run_polling()
