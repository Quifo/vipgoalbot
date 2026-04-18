import os, asyncio, httpx, json, time
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
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

# --- YARDIMCI FONKSİYONLAR ---

async def fetch_api(url):
    """SofaScore API'den veri çeker."""
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            if r.status_code == 200:
                return r.json()
            print(f"⚠️ API Hatası: {r.status_code}")
            return {}
        except Exception as e:
            print(f"⚠️ Bağlantı Hatası: {e}")
            return {}

def get_real_minute(m):
    """Maçın gerçek dakikasını hesaplar (İY, MS ve 2. Yarı düzeltmeli)."""
    status = m.get('status', {})
    desc = status.get('description', '').lower()
    elapsed = status.get('elapsed', 0)
    
    if 'ht' in desc: return "İY"
    if 'ft' in desc: return "MS"
    
    # 2. Yarıda dakikayı 45+ olarak hesapla
    if "2nd half" in desc and elapsed < 45:
        elapsed += 45
    
    # Eğer elapsed hala 0 veya 1 ise Timestamp'ten farkı bul
    if elapsed <= 1:
        start_ts = m.get('startTimestamp')
        if start_ts:
            now_ts = int(time.time())
            diff = (now_ts - start_ts) // 60
            elapsed = diff if 0 < diff < 120 else elapsed

    return f"{elapsed or 1}'"

# --- BULUT HAFIZA (GIST) ---

async def load_history_cloud():
    """Gist üzerinden gönderilmiş sinyalleri yükler."""
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
    """Gist üzerindeki sinyal listesini günceller."""
    url = f"https://api.github.com/gists/{GIST_ID}"
    data = {"files": {"sent_signals.json": {"content": json.dumps(list(sent_set))}}}
    async with httpx.AsyncClient() as client:
        try: await client.patch(url, headers=GIST_HEADERS, json=data)
        except: print("⚠️ Bulut hafıza kaydı yapılamadı.")

# --- KOMUTLAR ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *VIP Baskı Analizörü Aktif!*\n\n/canli - Maçları listeler\n/kontrol - Sistemi denetler", parse_mode=ParseMode.MARKDOWN)

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/canli: Canlı maçları skor ve dakikalarıyla listeler."""
    print("📥 Canlı maç listesi istendi.")
    data = await fetch_api(URL)
    events = data.get('events', [])
    
    if not events:
        await update.message.reply_text("📭 Şu an canlı maç bulunamadı.")
        return

    text = "⚽ *GÜNCEL CANLI MAÇLAR*\n\n"
    for m in events[:25]:
        m_min = get_real_minute(m)
        h = m.get('homeTeam', {}).get('name', 'Ev')
        a = m.get('awayTeam', {}).get('name', 'Dep')
        sh = m.get('homeScore', {}).get('current', 0)
        sa = m.get('awayScore', {}).get('current', 0)
        text += f"⏱ `{m_min}` | {h} *{sh}-{sa}* {a}\n"
    
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/kontrol: Botun tüm organlarını test eder."""
    status_msg = await update.message.reply_text("🔎 *VIP Analizör Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    
    api_data = await fetch_api(URL)
    api_res = "✅ OK" if api_data else "❌ HATA"
    
    gist_data = await load_history_cloud()
    gist_res = "✅ OK" if isinstance(gist_data, set) else "❌ HATA"
    
    # Kanal Mesaj Testi
    delivery = "✅ OK"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 *Sistem Testi:* Bot şu an ana kanala mesaj atabiliyor.")
    except:
        delivery = "❌ YETKİ YOK"

    report = (
        f"🛡 *OTOMATİK DENETİM RAPORU*\n\n"
        f"🌐 *SofaScore API:* {api_res}\n"
        f"💾 *Bulut Hafıza (Gist):* {gist_res}\n"
        f"📩 *Kanal İzni:* {delivery}\n\n"
        f"🚀 _Bot sinyal üretmeye hazır!_"
    )
    await status_msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

# --- ANALİZ DÖNGÜSÜ ---

async def get_stats(match_id):
    """Maçın detaylı şut ve atak istatistiklerini çeker."""
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
    """Arka planda 7/24 maçları tarayan ve sinyal atan döngü."""
    print("🚀 Sinyal Monitörü Başladı...")
    sent_signals = await load_history_cloud()

    while True:
        try:
            data = await fetch_api(URL)
            events = data.get('events', [])
            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                
                # Dakikayı filtreleme için sayıya çevir
                try:
                    minute_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except: minute_int = 0

                # Sinyal kriterleri (Dakika 10-85 arası ve daha önce atılmamış)
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
                        sent_signals.add(mid)
                        await save_history_cloud(sent_signals)
                        print(f"✅ Sinyal Atıldı: {m['homeTeam']['name']}")
            
            # Bellek yönetimi: Çok eski maçları Gist'ten temizle (Opsiyonel)
            if len(sent_signals) > 1000: sent_signals.clear()
            
            await asyncio.sleep(150) # 2.5 dakikada bir kontrol
        except Exception as e:
            print(f"⚠️ Monitör hatası: {e}")
            await asyncio.sleep(15)

# --- ANA ÇALIŞTIRICI ---

async def post_init(application):
    """Bot başladığında arka plan döngüsünü de başlatır."""
    asyncio.create_task(signal_monitor(application))

if __name__ == "__main__":
    # Botu modern yöntemle kuruyoruz (Komutlar burada tanımlı)
    application = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("canli", live_command))
    application.add_handler(CommandHandler("live", live_command))
    application.add_handler(CommandHandler("kontrol", control_command))

    print("✅ Bot ve Arka Plan Monitörü Hazır. Komutlar dinleniyor...")
    application.run_polling()
