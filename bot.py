import os, asyncio, httpx, json
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
brain = BettingBrain()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
HISTORY_FILE = "sent_signals.json"

# --- HAFIZA YÖNETİMİ ---

def load_history():
    """Gönderilen sinyalleri dosyadan yükler."""
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            try:
                return set(json.load(f))
            except:
                return set()
    return set()

def save_history(sent_set):
    """Gönderilen sinyalleri dosyaya kaydeder."""
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(sent_set), f)

# --- API VE DAKİKA FONKSİYONLARI ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=25.0) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except: return {}

async def get_stats(match_id):
    """İstatistikleri çeker."""
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

# --- SİNYAL MONİTÖRÜ ---

async def signal_monitor(app):
    print("🚀 Sinyal Monitörü Başladı...")
    # Bot başladığında geçmişi yükle
    sent_signals = load_history()

    while True:
        data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
        events = data.get('events', [])
        
        active_ids = []
        for m in events:
            mid = m['id']
            active_ids.append(mid)
            minute = m.get('status', {}).get('elapsed', 0)
            
            # Eğer bu maç ID'si daha önce gönderilmediyse ve dakika uygunsa
            if str(mid) not in sent_signals and 15 < minute < 85:
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
                        sent_signals.add(str(mid))
                        save_history(sent_signals) # Her sinyalden sonra kaydet
                        print(f"✅ Sinyal Gönderildi: {m['homeTeam']['name']}")
                    except: pass

        # Hafıza Temizliği: Biten maçları listeden çıkar (RAM şişmesin)
        # Sadece o an canlı olan maçları tut, bitenleri listeden silebilirsin (opsiyonel)
        # Ancak tekrar analiz etmesin istiyorsan bitenleri silmemek daha güvenli.
        
        await asyncio.sleep(120)

# --- BOT BAŞLATICI ---

async def post_init(app):
    asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    # /canli komutu vb. buraya eklenebilir
    application.run_polling()
