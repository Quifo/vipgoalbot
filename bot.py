import os, asyncio, httpx, json, time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GIST_ID = os.getenv("GIST_ID")

brain = BettingBrain()
HEADERS = {"User-Agent": "Mozilla/5.0"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

# --- ELITE LİG FİLTRESİ (Sadece Kaliteli Veri Sunan Ligler) ---
# SofaScore ID'lerine göre popüler ligler (Süper Lig: 52, PL: 17, CL: 7, vb.)
ELITE_LEAGUES = [
    52, 17, 7, 8, 23, 34, 35, 33, 37, 203, 11, 13, 20, 21, 22, 676, 1468, 10, 14, 15, 16, 18, 19, 24, 25
]

# --- DAKİKA HESAPLAMA ---
def get_real_minute(m):
    status = m.get('status', {})
    desc = status.get('description', '').lower()
    elapsed = status.get('elapsed', 0)
    if 'ht' in desc: return "İY"
    if 'ft' in desc: return "MS"
    if "2nd half" in desc and elapsed < 45: elapsed = 45 + elapsed
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
            return set(json.loads(r.json()['files']['sent_signals.json']['content']))
        except: return set()

async def save_history(sent_set):
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {"files": {"sent_signals.json": {"content": json.dumps(list(sent_set))}}}
    async with httpx.AsyncClient() as client:
        try: await client.patch(url, headers=GIST_HEADERS, json=data)
        except: pass

# --- VERİ DERİNLİĞİ KONTROLÜ (KRİTİK FONKSİYON) ---
async def get_valid_stats(match_id):
    """Sadece Tehlikeli Atak verisi olan maçları döndürür, yoksa None döner."""
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
    data = await fetch_api(url)
    stats = {'home_sot': 0, 'away_sot': 0, 'home_da': 0, 'away_da': 0, 'has_data': False}
    
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        if i['name'] == 'Shots on target':
                            stats['home_sot'] = int(i['homeValue'])
                            stats['away_sot'] = int(i['awayValue'])
                            stats['has_data'] = True # İsabetli şut varsa veri vardır
                        if i['name'] == 'Dangerous attacks':
                            stats['home_da'] = int(i['homeValue'])
                            stats['away_da'] = int(i['awayValue'])
                            stats['has_data'] = True # Tehlikeli atak varsa veri vardır
    except: pass
    
    # Eğer tehlikeli atak ve şut verisi hiç gelmemişse (0 ise), bu ligi reddet
    if stats['home_da'] == 0 and stats['away_da'] == 0:
        return None
    return stats

# --- SİNYAL MONİTÖRÜ ---
async def signal_monitor(app):
    print("🚀 Sinyal Monitörü Elite Modda Başladı...")
    sent_signals = await load_history()
    while True:
        try:
            data = await fetch_api(URL)
            events = data.get('events', [])
            for m in events:
                mid = str(m['id'])
                league_id = m.get('uniqueTournament', {}).get('id')
                minute_str = get_real_minute(m)
                
                # 1. Lig Filtresi: Sadece elit ligler (Opsiyonel: Listeyi boş bırakırsan tüm liglere bakar)
                # if league_id not in ELITE_LEAGUES: continue 

                try: minute_int = int(minute_str.replace("'", ""))
                except: minute_int = 0

                if mid not in sent_signals and 15 < minute_int < 85:
                    # 2. Veri Derinliği Kontrolü: Atak/Şut verisi var mı?
                    stats = await get_valid_stats(mid)
                    
                    if stats: # Sadece verisi olan maçlar için Brain çalışır
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
                            sent_signals.add(mid); await save_history(sent_signals)
            await asyncio.sleep(150)
        except: await asyncio.sleep(10)

async def post_init(app): asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("canli", live_command)) # live_command kodda var sayıldı
    application.run_polling()
