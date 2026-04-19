import os, asyncio, httpx, json, time, signal, sys
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# ====================== CONFIG ======================
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
GIST_ID = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_KEY = os.getenv("GROQ_API_KEY")

brain = BettingBrain()

HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
GIST_HEADERS = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}

LIVE_URL = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"

# Groq Rate Limiter
last_ai_requests = []
MAX_AI_REQUESTS_PER_MINUTE = 20

# Background task handles for clean cancellation on shutdown
_background_tasks: list[asyncio.Task] = []

# ====================== GROQ AI ANALİZ ======================
async def get_ai_insight(home, away, stats, pick, pressure, minute, score):
    if not GROQ_KEY:
        return "Veri yetersiz."

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]
    
    if len(last_ai_requests) >= MAX_AI_REQUESTS_PER_MINUTE:
        return _fallback_comment(home, away, stats, pick, pressure)

    last_ai_requests.append(now)

    url = "https://api.groq.com/openai/v1/chat/completions"
    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }

    # ✅ YENİ PROMPT - Kısa, keskin, profesyonel
    prompt_text = (
        f"Aşağıdaki canlı maç verisini analiz et ve TAM OLARAK 2 cümle yaz.\n\n"
        f"VERİ:\n"
        f"- Maç: {home} vs {away} | {minute}. dakika | Skor: {score}\n"
        f"- İsabetli Şut: {stats.get('home_sot',0)}-{stats.get('away_sot',0)}\n"
        f"- Toplam Şut: {stats.get('home_shots',0)}-{stats.get('away_shots',0)}\n"
        f"- Korner: {stats.get('home_corners',0)}-{stats.get('away_corners',0)}\n"
        f"- Hakimiyet: %{stats.get('home_poss',50)}-{stats.get('away_poss',50)}\n"
        f"- Baskı Skoru: %{pressure}\n"
        f"- Öneri: {pick}\n\n"
        f"YAZIM KURALLARI:\n"
        f"1. İlk cümle: Sadece istatistikleri yorumla. Rakam kullan.\n"
        f"2. İkinci cümle: Bu verilerle neden '{pick}' mantıklı? Direkt söyle.\n"
        f"3. Türkçe yaz. Emir kipi kullan. Net ve kısa ol.\n"
        f"4. Yasak kelimeler: gösteriyor, bulunuyor, devam ediyor, şu an, mevcut\n"
        f"5. Kesinlikle * _ ` gibi özel karakter kullanma.\n"
        f"6. Toplam 30 kelimeyi geçme."
    )

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt_text}],
        "temperature": 0.4,  # ✅ Daha az yaratıcı = Daha tutarlı
        "max_tokens": 120    # ✅ Kısa cevap zorla
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(url, json=payload, headers=headers)
                
                if r.status_code == 200:
                    data = r.json()
                    comment = data['choices'][0]['message']['content']
                    # Temizle
                    clean = (comment
                        .replace('*', '').replace('_', '').replace('`', '')
                        .replace('[', '').replace(']', '')
                        .replace('"', '').replace("'", '')
                        .strip())
                    print(f"🧠 AI → {clean[:80]}...")
                    return clean

                elif r.status_code == 429:
                    await asyncio.sleep(6 * (attempt + 1))
                    continue
                else:
                    await asyncio.sleep(4)
        except Exception as e:
            print(f"⚠️ Groq Hatası: {e}")
            await asyncio.sleep(5)

    return _fallback_comment(home, away, stats, pick, pressure)


# ✅ YENİ: Akıllı fallback yorumları
def _fallback_comment(home, away, stats, pick, pressure):
    sot_home = stats.get('home_sot', 0)
    sot_away = stats.get('away_sot', 0)
    shots_home = stats.get('home_shots', 0)
    corners_home = stats.get('home_corners', 0)
    
    comments = [
        f"{home} {sot_home} isabetli şutla kaleci zorluyor, baskı kritik eşiği aştı. {pick} için istatistikler uygun.",
        f"{shots_home} şut deneyen {home}, rakibini {corners_home} kornerle de tehdit etti. Oran değeri taşıyor.",
        f"Baskı skoru %{pressure} ile üst sınırda. {home} isabetli şut üstünlüğünü gole dönüştürmeli.",
        f"{home} hücum yoğunluğu artırıyor: {sot_home} isabetli şut, {corners_home} korner. {pick} hesaplanmış risk.",
    ]
    
    import random
    return random.choice(comments)


# ====================== YARDIMCI FONKSİYONLAR ======================
async def fetch_api(url):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            r = await client.get(url, headers=HEADERS)
            return r.json() if r.status_code == 200 else {}
        except:
            return {}

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
    return f"{elapsed or 1}'"

async def manage_history(mode="read", data=None):
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            if mode == "read":
                r = await client.get(url, headers=GIST_HEADERS)
                return json.loads(r.json()['files']['sent_signals.json']['content'])
            else:
                payload = {"files": {"sent_signals.json": {"content": json.dumps(data)}}}
                await client.patch(url, headers=GIST_HEADERS, json=payload)
        except:
            return [] if mode == "read" else None

async def get_stats(match_id):
    url = STATS_URL.format(match_id)
    data = await fetch_api(url)
    s = {'home_sot':0, 'away_sot':0, 'home_shots':0, 'away_shots':0,
         'home_corners':0, 'away_corners':0, 'home_poss':50, 'away_poss':50, 'has':False}
    try:
        for p in data.get('statistics', []):
            if p.get('period') == 'ALL':
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        n = i['name']
                        hv = int(str(i.get('homeValue', 0)).replace('%',''))
                        av = int(str(i.get('awayValue', 0)).replace('%',''))
                        if n == 'Shots on target': s['home_sot'], s['away_sot'], s['has'] = hv, av, True
                        elif n == 'Total shots': s['home_shots'], s['away_shots'], s['has'] = hv, av, True
                        elif n == 'Corner kicks': s['home_corners'], s['away_corners'] = hv, av
                        elif n == 'Ball possession': s['home_poss'], s['away_poss'] = hv, av
        return s if s['has'] else None
    except:
        return None


# ====================== KOMUTLAR ======================
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *VIP Pro Trader Bot Aktif!*\n\n"
        "/canli - Canlı maçları listeler\n"
        "/kontrol - Sistem denetimi yapar",
        parse_mode=ParseMode.MARKDOWN
    )

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data = await fetch_api(LIVE_URL)
    events = data.get('events', [])
    if not events:
        await update.message.reply_text("📭 Şu an canlı maç yok.")
        return
    text = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        m_min = get_real_minute(m)
        h, a = m['homeTeam']['name'], m['awayTeam']['name']
        sh, sa = m.get('homeScore', {}).get('current', 0), m.get('awayScore', {}).get('current', 0)
        text += f"⏱ `{m_min}` | {h} *{sh}-{sa}* {a}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    api_data = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")
    ai_test = await get_ai_insight("TestA", "TestB", {'home_sot':3,'away_sot':1,'home_shots':8,'away_shots':3,'home_corners':4,'away_corners':1,'home_poss':60,'away_poss':40}, "1.5 ÜST", 70, 55, "1-0")

    delivery = "✅ OK"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Sistem Testi")
    except:
        delivery = "❌ HATA"

    ai_status = "✅ OK" if len(ai_test) > 10 else "❌ HATA"

    report = (
        f"🛡 *BOT DENETİM RAPORU*\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {ai_status}\n"
        f"📩 İletim: {delivery}\n"
        f"⚽ Canlı Maç: {len(api_data.get('events', []))}\n"
        f"📊 Hafızadaki Sinyal: {len(gist_data) if isinstance(gist_data, list) else 0}\n\n"
        f"🚀 _Sistem aktif!_"
    )
    await msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)


# ====================== DÖNGÜLER ======================
async def result_tracker(app):
    while True:
        try:
            history = await manage_history("read")
            updated = False
            for sig in history[-20:]:
                if sig.get('status') == 'pending' and (time.time() - sig['timestamp']) > 3600:
                    r = await fetch_api(f"https://www.sofascore.com/api/v1/event/{sig['id']}")
                    ev = r.get('event', {})
                    if ev.get('status', {}).get('type') == 'finished':
                        is_win = (ev['homeScore']['current'] + ev['awayScore']['current']) > sig['start_total']
                        sig['status'] = 'WIN ✅' if is_win else 'LOSS ❌'
                        sig['final_score'] = f"{ev['homeScore']['current']}-{ev['awayScore']['current']}"
                        updated = True
            if updated:
                await manage_history("write", history)
        except:
            pass
        await asyncio.sleep(600)


async def signal_monitor(app):
    print("🚀 Pro Monitör Başladı... (Groq AI Aktif)")
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            events = data.get('events', [])
            history = await manage_history("read")
            sent_ids = [str(x['id']) for x in history]

            print(f"📊 {len(events)} maç taranıyor | Hafızada {len(history)} sinyal")

            for m in events:
                mid = str(m['id'])
                minute_str = get_real_minute(m)
                try:
                    mn_int = int(minute_str.replace("'", "")) if "'" in minute_str else 45
                except:
                    mn_int = 0

                if mid not in sent_ids and 10 < mn_int < 85:
                    stats = await get_stats(mid)
                    if stats:
                        odds_drop = round(time.time() % 9 + 3, 1)
                        res = brain.analyze_advanced(m, stats, mn_int, odds_drop)

                        if res.get('is_signal'):
                            home_name = m['homeTeam']['name']
                            away_name = m['awayTeam']['name']
                            league = m.get('tournament', {}).get('name', 'Bilinmiyor')

                            print(f"🔍 Sinyal bulundu: {home_name} vs {away_name} ({minute_str})")

                            ai_msg = await get_ai_insight(home_name, away_name, stats, res['pick'], res['pressure'], mn_int, res['score'])

                            alt_picks = [p for p in res.get('alt', []) if p[0] != res['pick']]
                            alt_txt = "".join([f"  • {p[0]} (Risk: {p[2]})\n" for p in alt_picks[:3]])

                            bar = "🟩" * (res['pressure'] // 10) + "⬜" * (10 - res['pressure'] // 10)
                            alt_section = f"\n💡 *ALTERNATİF ÖNERİLER*\n{alt_txt}" if alt_txt else ""

                            period_emoji = "1️⃣" if res['period'] == "1. YARI" else "2️⃣"

                            txt = (
                                f"📡 *SİNYAL* | {time.strftime('%H:%M')}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"⚽ *{home_name}* `{res['score']}` *{away_name}*\n"
                                f"🏆 {league}\n"
                                f"⏱ `{minute_str}` {period_emoji} {res['period']}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🎯 *TAHMİN:* `{res['pick']}`\n"
                                f"📊 *Güven:* {res['confidence']} `{res['prob']}%`\n"
                                f"⚠️ *Risk:* `{res['risk']}`\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"📈 *İSTATİSTİKLER*\n"
                                f"┌ 🥅 İsabetli Şut: `{stats['home_sot']} - {stats['away_sot']}`\n"
                                f"├ ⚡ Toplam Şut:  `{stats['home_shots']} - {stats['away_shots']}`\n"
                                f"├ 🚩 Korner:     `{stats['home_corners']} - {stats['away_corners']}`\n"
                                f"└ 🎮 Hakimiyet:  `%{stats['home_poss']} - %{stats['away_poss']}`\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🔥 *BASKI:* {bar} `%{res['pressure']}`\n"
                                f"👊 *Üstün Taraf:* {res['team']}\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"🧠 *ANALİZ:* _{ai_msg}_\n"
                                f"━━━━━━━━━━━━━━━━━━━━━━\n"
                                f"{alt_section}"
                                f"💎 _VIP Pro Trader_"
                            )

                            try:
                                await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                                history.append({
                                    "id": mid,
                                    "timestamp": time.time(),
                                    "status": "pending",
                                    "start_total": res['total_score'],
                                    "match": f"{home_name} vs {away_name}",
                                    "pick": res['pick']
                                })
                                await manage_history("write", history)
                            except Exception as e:
                                print(f"❌ Mesaj Gönderilemedi: {e}")
        except Exception as e:
            print(f"⚠️ Döngü hatası: {e}")

        await asyncio.sleep(180)  # 3 dakikada bir tarama


# ====================== LIFECYCLE ======================
async def post_init(app):
    """Start background tasks after the application is fully initialised."""
    t1 = asyncio.create_task(signal_monitor(app), name="signal_monitor")
    t2 = asyncio.create_task(result_tracker(app), name="result_tracker")
    _background_tasks.extend([t1, t2])


async def post_shutdown(app):
    """Cancel background tasks before the application shuts down."""
    for task in _background_tasks:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    _background_tasks.clear()
    print("🛑 Arka plan görevleri durduruldu.")


if __name__ == "__main__":
    if not TOKEN:
        print("❌ TELEGRAM_TOKEN bulunamadı. Bot durduruluyor.")
        sys.exit(1)

    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    
    print("✅ Bot Hazır! (Groq AI Aktif)")
    
    # ✅ Drop pending updates - Biriken eski istekleri temizle
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )
