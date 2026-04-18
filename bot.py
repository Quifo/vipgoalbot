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
    return f"{elapsed}'"

# --- HAFIZA SİSTEMİ ---
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
        except: print("⚠️ Hafıza kaydı hatası")

# --- İSTATİSTİK ---
async def get_stats(match_id):
    data = await fetch_api(STATS_URL.format(match_id))
    s = {'home_sot':0, 'away_sot':0, 'home_shots':0, 'away_shots':0, 'home_corners':0, 'away_corners':0, 'home_poss':50, 'away_poss':50, 'has':False}
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        n = i.get('name')
                        h_v = int(str(i.get('homeValue', 0)).replace('%',''))
                        a_v = int(str(i.get('awayValue', 0)).replace('%',''))
                        if n == 'Shots on target': s['home_sot'], s['away_sot'], s['has'] = h_v, a_v, True
                        elif n == 'Total shots': s['home_shots'], s['away_shots'], s['has'] = h_v, a_v, True
                        elif n == 'Corner kicks': s['home_corners'], s['away_corners'] = h_v, a_v
                        elif n == 'Ball possession': s['home_poss'], s['away_poss'] = h_v, a_v
    except: pass
    return s if s['has'] else None

# --- KOMUTLAR ---
async def live_command(update, context):
    data = await fetch_api(LIVE_URL); events = data.get('events', [])
    if not events: await update.message.reply_text("Canlı maç yok."); return
    msg = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        mn = get_real_minute(m); h = m['homeTeam'].get('shortName') or m['homeTeam']['name']
        a = m['awayTeam'].get('shortName') or m['awayTeam']['name']
        sh, sa = m.get('homeScore',{}).get('current',0), m.get('awayScore',{}).get('current',0)
        msg += f"⏱ `{mn}` | {h} {sh}-{sa} {a}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def control_command(update, context):
    api = await fetch_api(LIVE_URL); gist = await load_history(); deliv = "✅"
    try: await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Sistem Testi")
    except: deliv = "❌"
    rep = f"🛡 *DENETİM*\n\n🌐 API: {'✅' if api else '❌'}\n💾 Gist: {'✅' if isinstance(gist, set) else '❌'}\n📩 İletim: {deliv}"
    await update.message.reply_text(rep, parse_mode=ParseMode.MARKDOWN)

# --- ANA DÖNGÜ ---
async def signal_monitor(app):
    print("🚀 Sinyal Monitörü Başladı...")
    sent = await load_history()
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            for m in data.get('events', []):
                mid = str(m['id'])
                mn_str = get_real_minute(m)
                try: mn_int = int(mn_str.replace("'", "")) if "'" in mn_str else 45
                except: mn_int = 0
                
                if mid not in sent and 10 < mn_int < 85:
                    stats = await get_stats(mid)
                    if stats:
                        res = brain.analyze_advanced(m, stats, mn_int)
                        if res.get('is_signal'):
                            league = m.get('tournament', {}).get('name', 'Bilinmiyor')
                            bar = "🟩" * (res['pressure'] // 10) + "⬜" * (10 - res['pressure'] // 10)
                            
                            # Ana tahmini alternatiflerden çıkar
                            alt_picks = [p for p in res['alt'] if p[0] != res['pick']]
                            alt_txt = ""
                            if alt_picks:
                                for p in alt_picks[:3]:
                                    alt_txt += f"  • {p[0]} ({p[2]})\n"
                            else:
                                alt_txt = "  • Ek öneri yok\n"
                            
                            txt = (
                                f"🚨 *VIP ÇOKLU ANALİZ* 🚨\n\n"
                                f"⚽ *{m['homeTeam']['name']}* `{res['score']}` *{m['awayTeam']['name']}*\n"
                                f"🏆 _{league}_\n"
                                f"⏱ *Dakika:* `{mn_str}` ({res['period']})\n"
                                f"━━━━━━━━━━━━━━━━━━\n"
                                f"🎯 *ANA TAHMİN:* `{res['pick']}`\n"
                                f"📊 *Güven:* {res['confidence']} ({res['prob']}%)\n"
                                f"⚠️ *Risk:* {res['risk']}\n"
                                f"━━━━━━━━━━━━━━━━━━\n\n"
                                f"🔥 *BASKI ANALİZİ*\n"
                                f"{bar} `%{res['pressure']}`\n"
                                f"🚀 *Baskı Yapan:* {res['team']}\n\n"
                                f"📈 *İSTATİSTİKLER*\n"
                                f"  🥅 Şut: `{stats['home_sot']}-{stats['away_sot']}`\n"
                                f"  ⚡ T.Şut: `{stats['home_shots']}-{stats['away_shots']}`\n"
                                f"  🚩 Korner: `{stats['home_corners']}-{stats['away_corners']}`\n"
                                f"  🎮 Hakimiyet: `%{stats['home_poss']}-%{stats['away_poss']}`\n\n"
                                f"💡 *ALTERNATİF ÖNERİLER*\n{alt_txt}\n"
                                f"💎 _ROI Odakli Profesyonel Analiz_\n"
                                f"⏰ {time.strftime('%H:%M')}"
                            )
                            await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                            sent.add(mid); await save_history(sent)
                            print(f"✅ Sinyal: {m['homeTeam']['name']} vs {m['awayTeam']['name']}")
        except Exception as e: print(f"Döngü hatası: {e}")
        await asyncio.sleep(120)

async def post_init(app): asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    app.run_polling()
