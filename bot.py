import os, asyncio, httpx
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

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except: return {}

async def get_stats(match_id):
    """Maçın detaylı şut ve atak verilerini çeker."""
    data = await fetch_api(f"https://api.sofascore.com/api/v1/event/{match_id}/statistics")
    stats = {'home_sot': 0, 'away_sot': 0, 'home_da': 0, 'away_da': 0}
    
    for period in data.get('statistics', []):
        if period['period'] == 'ALL':
            for item in period['groups']:
                for s in item['statisticsItems']:
                    if s['name'] == 'Shots on target':
                        stats['home_sot'] = int(s['homeValue']); stats['away_sot'] = int(s['awayValue'])
                    if s['name'] == 'Dangerous attacks':
                        stats['home_da'] = int(s['homeValue']); stats['away_da'] = int(s['awayValue'])
    return stats

def get_min(m):
    """SofaScore dakika bug'ını çözen fonksiyon."""
    st = m.get('status', {})
    if st.get('description') == 'HT': return "İY"
    # elapsed 0 ise SofaScore'un start timestamp'inden hesapla (opsiyonel)
    elapsed = st.get('elapsed')
    return f"{elapsed}'" if elapsed else "1'"

async def live_command(update, context):
    data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
    events = data.get('events', [])
    if not events: await update.message.reply_text("Canlı maç yok."); return

    msg = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        min_str = get_min(m)
        h = m['homeTeam'].get('shortName') or m['homeTeam']['name']
        a = m['awayTeam'].get('shortName') or m['awayTeam']['name']
        sh = m.get('homeScore', {}).get('current', 0)
        sa = m.get('awayScore', {}).get('current', 0)
        msg += f"⏱ `{min_str}` | {h} *{sh}-{sa}* {a}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def signal_monitor(app):
    print("🚀 Monitör başladı..."); sent = set()
    while True:
        data = await fetch_api("https://api.sofascore.com/api/v1/sport/football/events/live")
        for m in data.get('events', []):
            mid = m['id']; minute = m.get('status', {}).get('elapsed', 0)
            if mid not in sent and 10 < minute < 85:
                # Detaylı istatistikleri çek
                stats = await get_stats(mid)
                res = brain.analyze_advanced(m, stats, minute)
                
                if res['is_signal']:
                    txt = (
                        f"🚨 *VIP GOL SİNYALİ* 🚨\n\n"
                        f"🏟 *MAÇ:* {m['homeTeam']['name']} vs {m['awayTeam']['name']}\n"
                        f"⏰ *DAKİKA:* {minute}' ({res['period']})\n"
                        f"🔥 *BASKI GÜCÜ:* %{res['pressure']}\n"
                        f"🎯 *DURUM:* {res['stats_summary']}\n"
                        f"🏆 *TAHMİN:* {res['pick']}\n"
                        f"⭐ *GÜVEN:* {res['confidence']}\n\n"
                        f"🚀 *BASKIDAKİ TAKIM:* {res['team']}\n\n"
                        f"💸 *STAKE:* 4/10"
                    )
                    await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                    sent.add(mid)
        await asyncio.sleep(150)

async def post_init(app): asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("canli", live_command))
    application.run_polling()
