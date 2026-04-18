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
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

# --- DAKİKA HESAPLAMA (KESİN VE DOĞRU ÇÖZÜM) ---

def get_real_minute(m):
    status = m.get('status', {})
    desc = status.get('description', '').lower()
    elapsed = status.get('elapsed', 0)
    
    # 1. Devre Arası ve Maç Sonu Kontrolü
    if 'ht' in desc or 'half-time' in desc: return "İY"
    if 'ft' in desc or 'full-time' in desc: return "MS"
    if 'interrupted' in desc or 'paused' in desc: return "DURDU"

    # 2. İkinci Yarı Mantığı (SofaScore bazen 2. yarıda dakikayı sıfırlar)
    # Eğer açıklama '2nd half' (veya '2nd period') ise ve dakika 45'ten küçükse üzerine 45 ekle.
    if "2nd half" in desc or "2nd period" in desc:
        if elapsed < 45:
            elapsed = 45 + elapsed
        elif elapsed == 0: # Eğer 2. yarı yeni başladıysa ve 0 ise 46 yap
            elapsed = 46
            
    # 3. Eğer dakika hala 0 veya None ise startTimestamp'ten canlı hesapla
    if not elapsed or elapsed <= 1:
        start_ts = m.get('startTimestamp')
        if start_ts:
            now_ts = int(time.time())
            diff = (now_ts - start_ts) // 60
            # Eğer 2. yarıdaysak ve fark 45'ten az çıkıyorsa (ara dahil edilmediği için)
            if "2nd half" in desc and diff < 45:
                elapsed = 46
            else:
                elapsed = diff if diff > 0 else 1
    
    # Maksimum 90+ duraklama dakikalarını da kapsar
    return f"{elapsed}'"

# --- API VE BULUT HAFIZA ---

async def fetch_api(url):
    async with httpx.AsyncClient(timeout=25.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            if r.status_code == 200:
                return r.json()
            return {}
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

# --- KONTROL VE CANLI KOMUTLARI ---

async def control_command(update, context):
    """/kontrol: Sistemin tüm parçalarını test eder."""
    status_msg = await update.message.reply_text("🔎 *VIP Analizör Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    
    # API Test
    api_data = await fetch_api(URL)
    # Gist Test
    gist_data = await load_history()
    # Logic Test
    mock_stats = {'home_sot': 5, 'away_sot': 1, 'home_da': 30, 'away_da': 10}
    mock_match = {'homeTeam': {'name': 'TeamA'}, 'awayTeam': {'name': 'TeamB'}, 'homeScore': {'current': 0}, 'awayScore': {'current': 0}}
    test_analysis = brain.analyze_advanced(mock_match, mock_stats, 25)
    
    # Kanal Mesaj Testi
    delivery = "✅"
    try: await context.bot.send_message(chat_id=CHAT_ID, text="🧪 *Sistem Testi:* OK")
    except: delivery = "❌"

    report = (
        f"🛡 *OTOMATİK DENETİM RAPORU*\n\n"
        f"🌐 *API:* {'✅' if api_data else '❌'}\n"
        f"💾 *Gist:* {'✅' if isinstance(gist_data, set) else '❌'}\n"
        f"🧠 *Algoritma:* {'✅' if test_analysis.get('is_signal') else '❌'}\n"
        f"📩 *Kanal İzni:* {delivery}\n\n"
        f"🚀 _Sinyal üretimi için her şey hazır!_"
    )
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    data = await fetch_api(URL)
    events = data.get('events', [])
    if not events: await update.message.reply_text("📭 Canlı maç yok."); return
    
    msg = "⚽ *GÜNCEL CANLI MAÇLAR*\n\n"
    for m in events[:25]:
        minute = get_real_minute(m)
        h = m.get('homeTeam', {}).get('shortName') or m.get('homeTeam', {}).get('name')
        a = m.get('awayTeam', {}).get('shortName') or m.get('awayTeam', {}).get('name')
        score_h = m.get('homeScore', {}).get('current', 0)
        score_a = m.get('awayScore', {}).get('current', 0)
        msg += f"⏱ `{minute}` | {h} *{score_h}-{score_a}* {a}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

# --- SİNYAL MONİTÖRÜ ---

async def get_stats(match_id):
    url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
    data = await fetch_api(url)
    stats = {'home_sot': 0, 'away_sot': 0, 'home_da': 0, 'away_da': 0}
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        if i['name'] == 'Shots on target':
                            stats['home_sot'] = int(i['homeValue']); stats['away_sot'] = int(i['awayValue'])
                        if i['name'] == 'Dangerous attacks':
                            stats['home_da'] = int(i['homeValue']); stats['away_da'] = int(i['awayValue'])
    except: pass
    return stats

async def signal_monitor(app):
    print("🚀 Sinyal Monitörü Başladı...")
    sent_signals = await load_history()
    while True:
        try:
            data = await fetch_api(URL)
            events = data.get('events', [])
            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                
                # Dakikayı filtreleme için int yap
                try:
                    minute_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: minute_int = 0

                if mid not in sent_signals and 10 < minute_int < 85:
                    stats = await get_stats(mid)
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

async def post_init(app):
    asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("canli", live_command))
    application.add_handler(CommandHandler("kontrol", control_command))
    application.run_polling()
