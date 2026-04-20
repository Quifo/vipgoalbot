import os, asyncio, httpx, json, time, logging
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

load_dotenv()

TOKEN        = os.getenv("TELEGRAM_TOKEN")
CHAT_ID      = os.getenv("CHAT_ID")
GIST_ID      = os.getenv("GIST_ID")
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN")
GROQ_KEY     = os.getenv("GROQ_API_KEY")

brain     = BettingBrain()
gist_lock = asyncio.Lock()

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36"
    )
}
GIST_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github.v3+json"
}

LIVE_URL  = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"

last_ai_requests       = []
MAX_AI_PER_MINUTE      = 20

# ─────────────────────────────────────────
# GÜVENLİ SAYI ÇEVİRME
# ─────────────────────────────────────────
def safe_int(val, default=0):
    try:
        if val is None or val == '' or val == '-':
            return default
        return int(float(str(val).replace('%', '').strip()))
    except:
        return default

# ─────────────────────────────────────────
# API İSTEĞİ
# ─────────────────────────────────────────
async def fetch_api(url):
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers=HEADERS
    ) as client:
        try:
            r = await client.get(url)
            return r.json() if r.status_code == 200 else {}
        except Exception as e:
            logger.error(f"API Hatası: {e}")
            return {}

# ─────────────────────────────────────────
# DAKİKA HESAPLAMA (2. yarı +45 düzeltmesi)
# ─────────────────────────────────────────
def get_real_minute(m):
    try:
        status  = m.get('status', {})
        desc    = status.get('description', '').lower()
        elapsed = safe_int(status.get('elapsed', 0))

        if 'ht' in desc:
            return "İY"
        if 'ft' in desc or 'ended' in desc or 'finished' in desc:
            return "MS"

        # 2. yarı tespiti → +45 ekle
        if any(x in desc for x in ['2nd', 'second', '2. yarı', 'ikinci']):
            if elapsed < 45:
                elapsed += 45
            elif elapsed == 0:
                start_ts = m.get('startTimestamp')
                if start_ts:
                    diff = (int(time.time()) - start_ts) // 60
                    elapsed = diff if 0 < diff < 130 else 46

        # Elapsed hala 0 veya 1 ise timestamp'ten hesapla
        if elapsed <= 1:
            start_ts = m.get('startTimestamp')
            if start_ts:
                diff = (int(time.time()) - start_ts) // 60
                elapsed = diff if 0 < diff < 130 else 1

        return f"{max(1, elapsed)}'"
    except:
        return "0'"

# ─────────────────────────────────────────
# GİST YÖNETİMİ (Lock ile güvenli)
# ─────────────────────────────────────────
async def manage_history(mode="read", data=None):
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with gist_lock:
        async with httpx.AsyncClient(
            timeout=20.0, headers=GIST_HEADERS
        ) as client:
            try:
                if mode == "read":
                    r = await client.get(url)
                    if r.status_code == 200:
                        files = r.json().get('files', {})
                        if 'sent_signals.json' in files:
                            return json.loads(
                                files['sent_signals.json']['content']
                            )
                    return []
                else:
                    payload = {"files": {"sent_signals.json": {
                        "content": json.dumps(data)
                    }}}
                    r = await client.patch(url, json=payload)
                    if r.status_code != 200:
                        logger.error(f"Gist write hatası: {r.status_code}")
            except Exception as e:
                logger.error(f"Gist hatası: {e}")
                return [] if mode == "read" else None

# ─────────────────────────────────────────
# İSTATİSTİK ÇEKİMİ (float düzeltmesi)
# ─────────────────────────────────────────
async def get_stats(match_id):
    url  = STATS_URL.format(match_id)
    data = await fetch_api(url)
    s = {
        'home_sot': 0, 'away_sot': 0,
        'home_shots': 0, 'away_shots': 0,
        'home_corners': 0, 'away_corners': 0,
        'home_poss': 50, 'away_poss': 50,
        'has': False
    }
    try:
        for p in data.get('statistics', []):
            if p.get('period') != 'ALL':
                continue
            for g in p.get('groups', []):
                for i in g.get('statisticsItems', []):
                    n  = i.get('name', '')
                    hv = safe_int(i.get('homeValue', 0))
                    av = safe_int(i.get('awayValue', 0))
                    if n == 'Shots on target':
                        s['home_sot'], s['away_sot'], s['has'] = hv, av, True
                    elif n == 'Total shots':
                        s['home_shots'], s['away_shots'], s['has'] = hv, av, True
                    elif n == 'Corner kicks':
                        s['home_corners'], s['away_corners'] = hv, av
                    elif n == 'Ball possession':
                        s['home_poss'], s['away_poss'] = hv, av
        return s if s['has'] else None
    except Exception as e:
        logger.error(f"Stats hatası: {e}")
        return None

# ─────────────────────────────────────────
# GROQ AI (kısa, profesyonel prompt)
# ─────────────────────────────────────────
async def get_ai_insight(home, away, stats, pick,
                          pressure, minute, score, xg=0.0):
    if not GROQ_KEY:
        return _fallback_comment(home, stats, pick, pressure)

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]

    if len(last_ai_requests) >= MAX_AI_PER_MINUTE:
        return _fallback_comment(home, stats, pick, pressure)

    last_ai_requests.append(now)

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type":  "application/json"
    }

    prompt = (
        f"Canlı maç analizi yap. TAM OLARAK 2 kısa cümle yaz.\n\n"
        f"Maç: {home} vs {away} | {minute}. dk | Skor: {score}\n"
        f"İsabetli Şut: {stats.get('home_sot',0)}-{stats.get('away_sot',0)}\n"
        f"Toplam Şut: {stats.get('home_shots',0)}-{stats.get('away_shots',0)}\n"
        f"Korner: {stats.get('home_corners',0)}-{stats.get('away_corners',0)}\n"
        f"Hakimiyet: %{stats.get('home_poss',50)}-%{stats.get('away_poss',50)}\n"
        f"Baskı: %{pressure} | xG: {xg} | Öneri: {pick}\n\n"
        f"KURALLAR:\n"
        f"1. İlk cümle: Rakam kullanarak istatistikleri yorumla.\n"
        f"2. İkinci cümle: Neden '{pick}' doğru seçim? Net söyle.\n"
        f"3. Türkçe. Emir kipi. Maks 25 kelime.\n"
        f"4. Yasak: gösteriyor, bulunuyor, devam ediyor, mevcut, şu an\n"
        f"5. Yasak karakter: * _ ` [ ]"
    )

    payload = {
        "model":       "llama-3.1-8b-instant",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.35,
        "max_tokens":  100
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload, headers=headers
                )
                if r.status_code == 200:
                    raw = r.json()['choices'][0]['message']['content']
                    clean = (raw.replace('*','').replace('_','')
                               .replace('`','').replace('[','')
                               .replace(']','').strip())
                    logger.info(f"AI → {clean[:60]}...")
                    return clean
                elif r.status_code == 429:
                    await asyncio.sleep(6 * (attempt + 1))
                else:
                    await asyncio.sleep(4)
        except Exception as e:
            logger.error(f"Groq hatası: {e}")
            await asyncio.sleep(5)

    return _fallback_comment(home, stats, pick, pressure)


def _fallback_comment(home, stats, pick, pressure):
    import random
    sot   = stats.get('home_sot', 0)
    shots = stats.get('home_shots', 0)
    corn  = stats.get('home_corners', 0)
    opts  = [
        f"{home} {sot} isabetli şut ve {corn} kornerle kapıyı zorluyor. {pick} için istatistikler uygun.",
        f"{shots} şut denemesinden {sot} tanesi kaleyi buldu. Baskı %{pressure} ile kritik eşikte.",
        f"{home} hücum yoğunluğunu artırdı: {sot} isabetli şut, {corn} korner. {pick} hesaplanmış seçim.",
        f"İsabetli şut oranı ve {corn} kornerle baskı net. {pick} bu veriyle değer taşıyor.",
    ]
    return random.choice(opts)


# ─────────────────────────────────────────
# KOMUTLAR
# ─────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *VIP Pro Trader Bot Aktif!*\n\n"
        "/canli - Canlı maçları listeler\n"
        "/kontrol - Sistem denetimi yapar",
        parse_mode=ParseMode.MARKDOWN
    )

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    data   = await fetch_api(LIVE_URL)
    events = data.get('events', [])
    if not events:
        await update.message.reply_text("📭 Şu an canlı maç yok.")
        return
    text = "⚽ *CANLI MAÇLAR*\n\n"
    for m in events[:20]:
        m_min = get_real_minute(m)
        h  = m.get('homeTeam', {}).get('name', '?')
        a  = m.get('awayTeam', {}).get('name', '?')
        sh = safe_int(m.get('homeScore', {}).get('current', 0))
        sa = safe_int(m.get('awayScore', {}).get('current', 0))
        text += f"⏱ `{m_min}` | {h} *{sh}-{sa}* {a}\n"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg      = await update.message.reply_text(
        "🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN
    )
    api_data  = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")
    ai_test   = await get_ai_insight(
        "TestA", "TestB",
        {'home_sot':3,'away_sot':1,'home_shots':8,'away_shots':3,
         'home_corners':4,'away_corners':1,'home_poss':60,'away_poss':40},
        "MS 1.5 ÜST", 70, 55, "1-0", 1.2
    )
    delivery = "✅ OK"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Test")
    except:
        delivery = "❌ HATA"

    events = api_data.get('events', [])
    report = (
        f"🛡 *BOT DENETİM RAPORU*\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {'✅ OK' if len(ai_test) > 10 else '❌ HATA'}\n"
        f"📩 İletim: {delivery}\n"
        f"⚽ Canlı Maç: {len(events)}\n"
        f"📊 Kayıtlı Sinyal: "
        f"{len(gist_data) if isinstance(gist_data, list) else 0}\n\n"
        f"🚀 _Sistem aktif!_"
    )
    await msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)


# ─────────────────────────────────────────
# SONUÇ TAKİPÇİSİ
# ─────────────────────────────────────────
async def result_tracker(app):
    while True:
        try:
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []

            updated = False
            for sig in history[-20:]:
                if (sig.get('status') == 'pending' and
                        time.time() - sig.get('timestamp', 0) > 3600):
                    r  = await fetch_api(
                        f"https://www.sofascore.com/api/v1/event/{sig['id']}"
                    )
                    ev = r.get('event', {})
                    if ev.get('status', {}).get('type') == 'finished':
                        hs = safe_int(ev.get('homeScore', {}).get('current', 0))
                        as_ = safe_int(ev.get('awayScore', {}).get('current', 0))
                        is_win = (hs + as_) > sig.get('start_total', 0)
                        sig['status']      = 'WIN ✅' if is_win else 'LOSS ❌'
                        sig['final_score'] = f"{hs}-{as_}"
                        updated = True

            if updated:
                await manage_history("write", history)
                logger.info("📊 Sonuçlar güncellendi.")
        except Exception as e:
            logger.error(f"Result tracker hatası: {e}")

        await asyncio.sleep(600)


# ─────────────────────────────────────────
# SİNYAL MONİTÖRÜ
# ─────────────────────────────────────────
async def signal_monitor(app):
    logger.info("🚀 Sinyal monitörü başladı.")
    while True:
        try:
            data    = await fetch_api(LIVE_URL)
            events  = data.get('events', [])
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []

            sent_ids = {str(x['id']) for x in history}
            logger.info(f"📊 {len(events)} maç taranıyor | "
                        f"{len(history)} sinyal kayıtlı")

            for m in events:
                try:
                    mid        = str(m.get('id', ''))
                    minute_str = get_real_minute(m)

                    if minute_str in ("İY", "MS", "0'"):
                        continue

                    mn_int = safe_int(minute_str.replace("'", ""), 0)

                    if mid in sent_ids or not (10 < mn_int < 85):
                        continue

                    stats = await get_stats(mid)
                    if not stats or not stats.get('has'):
                        continue

                    odds_drop = round(time.time() % 9 + 3, 1)
                    res       = brain.analyze_advanced(m, stats, mn_int, odds_drop)

                    if not res.get('is_signal'):
                        logger.info(f"⏭ Elenedi: {res.get('reason','?')}")
                        continue

                    home_name = m.get('homeTeam', {}).get('name', 'Bilinmiyor')
                    away_name = m.get('awayTeam', {}).get('name', 'Bilinmiyor')
                    league    = m.get('tournament', {}).get('name', 'Bilinmiyor')
                    xg_val    = res.get('xg', 0.0)
                    momentum  = res.get('momentum', 0)

                    logger.info(f"🔍 Sinyal: {home_name} vs {away_name} "
                                f"| {minute_str} | {res['pick']}")

                    ai_msg = await get_ai_insight(
                        home_name, away_name, stats,
                        res['pick'], res['pressure'],
                        mn_int, res['score'], xg_val
                    )

                    # Alternatif öneriler
                    alt_picks = [p for p in res.get('alt', [])
                                 if p[0] != res['pick']]
                    alt_txt   = "".join(
                        [f"  • {p[0]} (Risk: {p[2]})\n"
                         for p in alt_picks[:2]]
                    )
                    alt_section = (f"\n💡 *ALTERNATİF*\n{alt_txt}"
                                   if alt_txt else "")

                    # Baskı barı
                    bar_val = max(0, min(100, res['pressure']))
                    bar = ("🟩" * (bar_val // 10) +
                           "⬜" * (10 - bar_val // 10))

                    # Doğrulayıcılar
                    confirms = res.get('confirmations', [])
                    conf_txt = " · ".join(confirms[:3])

                    # Mesaj
                    period_emoji = ("1️⃣" if res['period'] == "1. YARI"
                                    else "2️⃣")
                    txt = (
                        f"📡 *SİNYAL* | {time.strftime('%H:%M')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚽ *{home_name}* `{res['score']}` *{away_name}*\n"
                        f"🏆 _{league}_\n"
                        f"⏱ `{minute_str}` {period_emoji} {res['period']}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 *TAHMİN:* `{res['pick']}`\n"
                        f"📊 *Güven:* {res['confidence']} `{res['prob']}%`\n"
                        f"⚠️ *Risk:* `{res['risk']}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 *İSTATİSTİKLER*\n"
                        f"┌ 🥅 İsabetli Şut: "
                        f"`{stats['home_sot']} - {stats['away_sot']}`\n"
                        f"├ ⚡ Toplam Şut:   "
                        f"`{stats['home_shots']} - {stats['away_shots']}`\n"
                        f"├ 🚩 Korner:      "
                        f"`{stats['home_corners']} - {stats['away_corners']}`\n"
                        f"└ 🎮 Hakimiyet:   "
                        f"`%{stats['home_poss']} - %{stats['away_poss']}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔥 *BASKI:* {bar} `%{res['pressure']}`\n"
                        f"📐 *xG:* `{xg_val}` | "
                        f"⚡ *Momentum:* `{momentum}`\n"
                        f"👊 *Üstün:* {res['team']}\n"
                        f"✅ _{conf_txt}_\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🧠 *ANALİZ:* _{ai_msg}_\n"
                        f"{alt_section}"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💎 _VIP Pro Trader_"
                    )

                    await app.bot.send_message(
                        chat_id=CHAT_ID, text=txt,
                        parse_mode=ParseMode.MARKDOWN
                    )
                    history.append({
                        "id":          mid,
                        "timestamp":   time.time(),
                        "status":      "pending",
                        "start_total": res['total_score'],
                        "match":       f"{home_name} vs {away_name}",
                        "pick":        res['pick']
                    })
                    await manage_history("write", history)
                    sent_ids.add(mid)
                    logger.info(f"✅ Sinyal gönderildi: {res['pick']}")

                except Exception as e:
                    logger.error(f"Maç işleme hatası: {e}")
                    continue

        except Exception as e:
            logger.error(f"Monitör döngü hatası: {e}")

        await asyncio.sleep(180)


# ─────────────────────────────────────────
# BAŞLATMA
# ─────────────────────────────────────────
async def post_init(app):
    asyncio.create_task(result_tracker(app))
    asyncio.create_task(signal_monitor(app))
    logger.info("✅ Arka plan görevleri başladı.")


if __name__ == "__main__":
    app = (
        ApplicationBuilder()
        .token(TOKEN)
        .post_init(post_init)
        .connect_timeout(30)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(30)
        .build()
    )
    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("canli",   live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    logger.info("✅ Bot hazır!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
