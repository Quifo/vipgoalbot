import os, asyncio, httpx, json
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
GIST_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept": "application/vnd.github.v3+json"
}

# --- GITHUB GIST HAFIZA SİSTEMİ ---

async def load_history_cloud():
    """Gist üzerinden gönderilmiş sinyal listesini çeker."""
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with httpx.AsyncClient() as client:
        try:
            r = await client.get(url, headers=GIST_HEADERS)
            content = r.json()['files']['sent_signals.json']['content']
            return set(json.loads(content))
        except Exception as e:
            print(f"Hafıza yükleme hatası: {e}")
            return set()

async def save_history_cloud(sent_set):
    """Gist üzerindeki dosyayı günceller."""
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {
        "files": {
            "sent_signals.json": {
                "content": json.dumps(list(sent_set))
            }
        }
    }
    async with httpx.AsyncClient() as client:
        try:
            await client.patch(url, headers=GIST_HEADERS, json=data)
        except Exception as e:
            print(f"Hafıza kaydetme hatası: {e}")

# --- ANALİZ VE MONİTÖR ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except: return {}

async def get_stats(match_id):
    """Maçın canlı istatistiklerini çeker."""
    data = await fetch_api(f"https://api.sofascore.com/api/v1/event/{match_id}/statistics")
    stats = {'home_sot': 0, 'away_sot': 0, 'home_da': 0, 'away_da': 0}
    for period in data.get('statistics', []):
        if period.get('period') == 'ALL':
            for group in period.get('groups', []):
                for item in group.get('statisticsItems', []):
                    if item['name'] == 'Shots on target':
                        stats['home_sot'] = int(item['homeValue']); stats['away_sot'] = int(item['awayValue'])
                    if item['name'] == 'Dangerous attacks':
                        stats['home_da'] = int(item['homeValue']); stats['away_da'] = int(item['awayValue'])
    return stats

async def signal_monitor(app):
    print("🚀 Sinyal Monitörü Başlatıldı (Bulut Hafıza Aktif)...")
    # Başlangıçta buluttan verileri çek
    sent_signals = await load_history_cloud()

    while True:
        data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
        events = data.get('events', [])
        
        for m in events:
            mid = str(m['id'])
            minute = m.get('status', {}).get('elapsed', 0)
            
            # Daha önce gönderilmemişse ve maç canlıysa (10-85 dk)
            if mid not in sent_signals and 10 < minute < 85:
                stats = await get_stats(mid)
                res = brain.analyze_advanced(m, stats, minute)
                
                if res['is_signal']:
                    txt = (
                        f"🚨 *VIP GOL SİNYALİ* 🚨\n\n"
                        f"🏟 *MAÇ:* {m['homeTeam']['name']} vs {m['awayTeam']['name']}\n"
                        f"⏰ *DAKİKA:* {minute}' ({res['period']})\n"
                        f"🔥 *BASKI GÜCÜ:* %{res['pressure']}\n"
                        f"🎯 *DURUM:* {res['stats_summary']}\n"
                        f"🏆 *TAHMİN:* {res['pick']}\n\n"
                        f"🚀 *BASKIDAKİ TAKIM:* {res['team']}\n"
                        f"💸 *STAKE:* 4/10"
                    )
                    try:
                        await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                        sent_signals.add(mid)
                        # Buluta kaydet
                        await save_history_cloud(sent_signals)
                        print(f"✅ Sinyal Buluta Kaydedildi: {m['homeTeam']['name']}")
                    except: pass
        
        # 300 saniyede bir kontrol (Gist API limitlerine takılmamak ve güvenli analiz için)
        await asyncio.sleep(150)

async def post_init(app):
    asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.run_polling()
