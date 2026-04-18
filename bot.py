import os, asyncio, httpx, json
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
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

# --- YARDIMCI FONKSİYONLAR ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except: return {}

def get_min(m):
    """
    SofaScore Dakika Bug'ını Çözen Kesin Yöntem.
    Saniyeyi alıp 60'a böler ve futbol dakikasına çevirir.
    """
    status = m.get('status', {})
    match_time = m.get('time', {}) # SofaScore asıl dakikayı burada tutar
    
    # 1. Devre arası kontrolü
    if status.get('description') == 'HT':
        return "İY"
    
    # 2. SofaScore 'current' saniye verisi (En garantisi budur)
    # Bu değer maç başladığından beri akan saniyedir.
    current_seconds = match_time.get('current')
    
    if current_seconds is not None and current_seconds > 0:
        # Saniyeyi dakikaya çevir (Örn: 130 saniye // 60 = 2. dakika)
        # Futbolda 0-59 saniye arası 1. dakika kabul edildiği için +1 ekliyoruz.
        minute = (current_seconds // 60) + 1
        
        # İkinci yarı başladıysa SofaScore saniyeyi sıfırlamaz, kümülatif sayar.
        # Eğer saniye 2700'den (45. dk) fazlaysa ve hala 1. yarı görünüyorsa 45+ yazdırabiliriz.
        return f"{minute}'"

    # 3. Eğer saniye verisi yoksa 'elapsed' verisine güven (Fallback)
    elapsed = status.get('elapsed')
    if elapsed is not None and elapsed > 0:
        return f"{elapsed}'"
    
    # 4. Maç henüz başlamış veya veri henüz düşmemişse
    return "1'"

# --- BULUT HAFIZA (GIST) ---

async def load_history_cloud():
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

async def save_history_cloud(sent_set):
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {"files": {"sent_signals.json": {"content": json.dumps(list(sent_set))}}}
    async with httpx.AsyncClient() as client:
        try: await client.patch(url, headers=GIST_HEADERS, json=data)
        except: pass

# --- KOMUTLAR ---

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
    events = data.get('events', [])
    
    if not events:
        await update.message.reply_text("📭 Şu an canlı maç yok.")
        return

    text = "⚽ *GÜNCEL CANLI MAÇLAR*\n\n"
    for m in events[:25]:
        m_min = get_min(m)
        h = m.get('homeTeam', {}).get('shortName') or m.get('homeTeam', {}).get('name', 'Ev')
        a = m.get('awayTeam', {}).get('shortName') or m.get('awayTeam', {}).get('name', 'Dep')
        sh = m.get('homeScore', {}).get('current', 0)
        sa = m.get('awayScore', {}).get('current', 0)
        text += f"⏱ `{m_min}` | {h} *{sh}-{sa}* {a}\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

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
    sent_signals = await load_history_cloud()
    while True:
        data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
        events = data.get('events', [])
        for m in events:
            mid = str(m['id'])
            # Dakika bilgisini burada sayısal alıyoruz
            m_min_str = get_min(m)
            try:
                # '42'' -> 42 (integer çevrimi)
                minute = int(m_min_str.replace("'", "")) if m_min_str not in ["İY", "MS", "Canlı"] else 45
            except: minute = 0

            if mid not in sent_signals and 10 < minute < 85:
                stats = await get_stats(mid)
                res = brain.analyze_advanced(m, stats, minute)
                if res['is_signal']:
                    txt = (
                        f"🚨 *VIP GOL SİNYALİ* 🚨\n\n"
                        f"🏟 *MAÇ:* {m['homeTeam']['name']} vs {m['awayTeam']['name']}\n"
                        f"⏰ *DAKİKA:* {m_min_str} ({res['period']})\n"
                        f"🔥 *BASKI GÜCÜ:* %{res['pressure']}\n"
                        f"🎯 *DURUM:* {res['stats_summary']}\n"
                        f"🏆 *TAHMİN:* {res['pick']}\n"
                        f"🚀 *BASKIDAKİ TAKIM:* {res['team']}\n"
                        f"💸 *STAKE:* 4/10"
                    )
                    try:
                        await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                        sent_signals.add(mid); await save_history_cloud(sent_signals)
                    except: pass
        await asyncio.sleep(150)

async def post_init(app): asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("canli", live_command))
    app.run_polling()
