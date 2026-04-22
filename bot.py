import os, asyncio, httpx, json, time, logging
import html
from telegram.constants import ChatAction
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
MATCH_URL = "https://www.sofascore.com/api/v1/event/{}"

last_ai_requests  = []
MAX_AI_PER_MINUTE = 20

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

def safe_float(val, default=0.0):
    try:
        if val is None or val == '' or val == '-':
            return default
        return float(str(val).replace('%', '').strip())
    except:
        return default
        
def normalize_ts(ts):
    try:
        if ts is None:
            return None
        ts = int(ts)
        if ts > 10_000_000_000:
            ts //= 1000
        return ts
    except:
        return None        
        
def minute_str_to_int(minute_str: str) -> int:
    try:
        if not minute_str:
            return 0
        s = minute_str.replace("'", "").strip()
        if s in ("İY", "MS", "0"):
            return 0
        if "+" in s:
            a, b = s.split("+", 1)
            return safe_int(a, 0) + safe_int(b, 0)
        return safe_int(s, 0)
    except:
        return 0        

# ─────────────────────────────────────────
# API İSTEĞİ
# ─────────────────────────────────────────
async def fetch_api(url):
    async with httpx.AsyncClient(
        timeout=30.0, follow_redirects=True, headers=HEADERS
    ) as client:
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json()
            return {}
        except Exception as e:
            logger.error(f"API Hatası ({url}): {e}")
            return {}

# ─────────────────────────────────────────
# DAKİKA HESAPLAMA
# ─────────────────────────────────────────
def get_real_minute(m):
    try:
        now    = int(time.time())
        status = m.get("status") or {}
        stype  = (status.get("type") or "").lower()
        desc   = (status.get("description") or "").lower()

        if stype in ("finished", "ended"):
            return "MS"
        if stype in ("notstarted", "scheduled"):
            return "0'"
        if stype in ("halftime", "break", "pause"):
            return "İY"
        if any(x in desc for x in ["ht", "half-time", "halftime", "interval"]):
            return "İY"

        time_obj = m.get("time") or {}
        period = safe_int(time_obj.get("period", 0), 0)
        if period not in (1, 2):
            if any(x in desc for x in ["2nd", "second", "2. yar", "ikinci"]):
                period = 2
            elif any(x in desc for x in ["1st", "first", "1. yar", "birinci"]):
                period = 1
            else:
                period = 0

        start_ts = normalize_ts(m.get("startTimestamp"))
        diff_start = (((now - start_ts) // 60) + 1) if start_ts else None

        if diff_start is not None:
            if diff_start >= 55 and period in (0, 1):
                period = 2
            elif diff_start < 50 and period == 2:
                period = 1

        cps = normalize_ts(
            time_obj.get("currentPeriodStartTimestamp")
            or m.get("currentPeriodStartTimestamp")
        )

        if cps and period in (1, 2):
            elapsed_period = max(0, ((now - cps) // 60) + 1)
            minute = (period - 1) * 45 + elapsed_period
        else:
            elapsed = safe_int(status.get("elapsed", 0), 0)
            if elapsed <= 0 and diff_start is not None:
                elapsed = diff_start
            minute = elapsed

            if period == 2 and minute <= 45 and (diff_start is None or diff_start >= 55):
                minute += 45

        minute = max(1, min(130, int(minute)))
        return f"{minute}'"
    except:
        return "0'"

# ─────────────────────────────────────────
# GİST YÖNETİMİ
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
                                files['sent_signals.json']['content'])
                    return []
                else:
                    payload = {"files": {"sent_signals.json": {
                        "content": json.dumps(data)
                    }}}
                    r = await client.patch(url, json=payload)
                    if r.status_code != 200:
                        logger.error(f"Gist write: {r.status_code}")
            except Exception as e:
                logger.error(f"Gist hatası: {e}")
                return [] if mode == "read" else None

# ─────────────────────────────────────────
# İSTATİSTİK ÇEKİMİ
# ─────────────────────────────────────────
async def get_stats(match_id):
    stats_url = STATS_URL.format(match_id)
    match_url = MATCH_URL.format(match_id)

    s = {
        'home_sot': 0,       'away_sot': 0,
        'home_shots': 0,     'away_shots': 0,
        'home_corners': 0,   'away_corners': 0,
        'home_poss': 50,     'away_poss': 50,
        'home_xg': 0.0,      'away_xg': 0.0,
        'home_attacks': 0,   'away_attacks': 0,
        'home_dangerous': 0, 'away_dangerous': 0,
        'home_saves': 0,     'away_saves': 0,
        'home_big_chances': 0, 'away_big_chances': 0,
        'home_shots_box': 0, 'away_shots_box': 0,
        'has': False
    }

    async with httpx.AsyncClient(
        timeout=15.0, follow_redirects=True, headers=HEADERS
    ) as client:
        try:
            stats_resp, match_resp = await asyncio.gather(
                client.get(stats_url),
                client.get(match_url),
                return_exceptions=True
            )
        except Exception as e:
            logger.error(f"Paralel istek hatası ({match_id}): {e}")
            return None

    try:
        if (not isinstance(stats_resp, Exception)
                and stats_resp.status_code == 200):
            data = stats_resp.json()
            for p in data.get('statistics', []):
                if p.get('period') != 'ALL':
                    continue
                for g in p.get('groups', []):
                    for i in g.get('statisticsItems', []):
                        n  = i.get('name', '')
                        hv = safe_int(i.get('homeValue', 0))
                        av = safe_int(i.get('awayValue', 0))
                        if n == 'Shots on target':
                            s['home_sot'], s['away_sot'] = hv, av
                            s['has'] = True
                        elif n == 'Total shots':
                            s['home_shots'], s['away_shots'] = hv, av
                            s['has'] = True
                        elif n == 'Corner kicks':
                            s['home_corners'], s['away_corners'] = hv, av
                        elif n == 'Ball possession':
                            s['home_poss'], s['away_poss'] = hv, av
                        elif n == 'Goalkeeper saves':
                            s['home_saves'], s['away_saves'] = hv, av
                        elif n == 'Attacks':
                            s['home_attacks'], s['away_attacks'] = hv, av
                        elif n == 'Dangerous attacks':
                            s['home_dangerous'], s['away_dangerous'] = hv, av
                        elif n == 'Big chances':
                            s['home_big_chances'], s['away_big_chances'] = hv, av
                        elif n in ('Shots inside box', 'Shots on box'):
                            s['home_shots_box'], s['away_shots_box'] = hv, av
    except Exception as e:
        logger.error(f"Stats parse ({match_id}): {e}")

    try:
        if (not isinstance(match_resp, Exception)
                and match_resp.status_code == 200):
            ev      = match_resp.json().get('event', {})
            minute_str = get_real_minute(ev)
            s["minute_str"] = minute_str
            s["minute_int"] = safe_int(str(minute_str).replace("'", ""), 0)        
            home_xg = ev.get('homeXg')
            away_xg = ev.get('awayXg')
            if home_xg is not None:
                s['home_xg'] = round(safe_float(home_xg), 2)
            if away_xg is not None:
                s['away_xg'] = round(safe_float(away_xg), 2)
    except Exception as e:
        logger.error(f"Match parse ({match_id}): {e}")

    return s if s['has'] else None

# ─────────────────────────────────────────
# ÖN FİLTRE
# ─────────────────────────────────────────
def should_check_match(m, sent_ids):
    try:
        mid        = str(m.get('id', ''))
        minute_str = get_real_minute(m)

        if mid in sent_ids:
            return False, "Zaten gönderildi"
        if minute_str in ("İY", "MS", "0'"):
            return False, f"Geçersiz dakika: {minute_str}"

        mn_int = minute_str_to_int(minute_str)
        if not (10 < mn_int < 85):
            return False, f"Dakika dışı: {mn_int}"
        if not m.get('tournament'):
            return False, "Turnuva bilgisi yok"

        h_s = safe_int(m.get('homeScore', {}).get('current', 0))
        a_s = safe_int(m.get('awayScore', {}).get('current', 0))
        if h_s + a_s > 4:
            return False, f"Çok gollü: {h_s + a_s}"

        return True, mn_int
    except Exception as e:
        return False, f"Filtre hatası: {e}"

# ─────────────────────────────────────────
# GROQ AI (Profesyonel Prompt)
# ─────────────────────────────────────────
async def get_ai_insight(home, away, stats, pick, pressure, minute, score, xg=0.0, pick_type="ust"):
    if not GROQ_KEY:
        return _fallback_comment(home, stats, pick, pressure, pick_type)

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]

    if len(last_ai_requests) >= MAX_AI_PER_MINUTE:
        return _fallback_comment(home, stats, pick, pressure, pick_type)

    last_ai_requests.append(now)

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json"
    }
    
    h_sot = safe_int(stats.get("home_sot", 0))
    a_sot = safe_int(stats.get("away_sot", 0))
    h_shots = safe_int(stats.get("home_shots", 0))
    a_shots = safe_int(stats.get("away_shots", 0))
    h_danger = safe_int(stats.get("home_dangerous", 0))
    a_danger = safe_int(stats.get("away_dangerous", 0))
    h_poss = safe_int(stats.get("home_poss", 50))
    h_big = safe_int(stats.get("home_big_chances", 0))
    a_big = safe_int(stats.get("away_big_chances", 0))
    
    # Bahis tipine göre özel analiz
    context_analysis = ""
    if "İY" in pick:
        context_analysis = f"""İLK YARI ANALİZİ:
- Dakika {minute}'te pozisyon baskısı %{h_poss}.
- Erken gol ihtimali için isabetli şut ({h_sot}) ve tehlikeli atak ({h_danger}) kritik göstergeler.
- İlk yarı dinamiği: Yüksek tempo ve baskı devam ederse gol kaçınılmaz."""
    elif "KG" in pick and "YOK" not in pick:
        context_analysis = f"""KARŞILIKLI GOL ANALİZİ:
- İki taraf da açık oynuyor: Ev sahibi {h_sot}, Deplasman {a_sot} isabetli şut.
- Defans zafiyetleri ve hücum tempesi göz önüne alındığında her iki kalede de gol beklentisi yüksek."""
    elif "KG YOK" in pick:
        context_analysis = f"""DEFANSİF ANALİZ:
- Düşük tempolu maç: Toplam {h_sot + a_sot} isabetli şut, {h_danger + a_danger} tehlikeli atak.
- İki takım da temkinli oynuyor, gol beklenmiyor."""
    elif "Korner" in pick:
        context_analysis = f"""KORNER ANALİZİ:
- Kanat organizasyonları ve baskı korner üretiyor: Toplam {h_sot + a_sot} şut.
- Hücum baskısı sürdükçe korner sayısı artmaya devam edecek."""
    elif "Handikap" in pick or "MS 1" in pick or "MS 2" in pick:
        context_analysis = f"""TARAF ANALİZİ:
- Baskı ve pozisyon üstünlüğü (%{h_poss}) belirleyici faktör.
- Farkı açma potansiyeli: {h_sot} isabetli şut ve {h_danger} tehlikeli atak."""
    elif "T 1.5" in pick:
        context_analysis = f"""TAKIM GOL ANALİZİ:
- Belirgin hücum üstünlüğü: {h_sot} isabetli şut, {h_big} büyük fırsat.
- Rakip savunma zorlanıyor, 1.5 üstü için yüksek potansiyel."""
    else:
        context_analysis = f"""GOL POTANSİYELİ:
- xG {xg} ve toplam {h_sot + a_sot} isabetli şut ile üst bahis için güçlü veriler."""

    prompt = f"""Sen profesyonel bir futbol analisti ve bahis uzmanısın. Maçın canlı istatistiklerini yorumlayarak NEDEN bu bahsin mantıklı olduğunu açıklayacaksın.

MAÇ: {home} vs {away}
SKOR: {score} | DAKİKA: {minute}'
ÖNERİLEN BAHİS: {pick}

İSTATİSTİK ÖZETİ:
• Pozisyon: {h_poss}% - {100-h_poss}%
• İsabetli Şut: {h_sot} - {a_sot}
• Toplam Şut: {h_shots} - {a_shots}
• Tehlikeli Atak: {h_danger} - {a_danger}
• Büyük Fırsat: {h_big} - {a_big}
• xG: {xg}
• Baskı Skoru: {pressure}/100

{context_analysis}

GÖREVİN:
1. İlk cümle: Mevcut oyun hızını ve baskıyı TEKNİK verilerle özetle (max 22 kelime).
2. İkinci cümle: Önerilen bahisin mantığını istatistiksel olarak kesin dille açıkla (max 25 kelime).
3. KESİNLİKLE emoji kullanma, kesinlikle "belki", "sanırım", "olabilir" gibi belirsiz ifadeler kullanma.
4. Profesyonel, kendinden emin ve analitik bir dil kullan.
5. Skoru ve dakikayı tekrar etme.

ÖRNEK KALİTE:
"Ev sahibi son 15 dakikada 4 isabetli şutla kaleyi abluka altına aldı. Yüksek tempo ve pozisyon üstünlüğü 0.5 üstünün değerini garantiliyor."

Yorum:"""

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.25,
        "max_tokens": 120
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
                    clean = (raw.replace('*', '').replace('_', '')
                               .replace('`', '').replace('[', '')
                               .replace(']', '').replace('"', '')
                               .replace("'", "").strip())
                    
                    if len(clean) < 20 or "cümle" in clean.lower():
                        return _fallback_comment(home, stats, pick, pressure, pick_type)
                    
                    logger.info(f"AI → {clean[:80]}...")
                    return clean
                elif r.status_code == 429:
                    await asyncio.sleep(6 * (attempt + 1))
                else:
                    await asyncio.sleep(4)
        except Exception as e:
            logger.error(f"Groq hatası: {e}")
            await asyncio.sleep(5)

    return _fallback_comment(home, stats, pick, pressure, pick_type)


def _fallback_comment(home, stats, pick, pressure, pick_type="ust"):
    import random
    
    h_sot = safe_int(stats.get('home_sot', 0))
    a_sot = safe_int(stats.get('away_sot', 0))
    total_shots = safe_int(stats.get('home_shots', 0)) + safe_int(stats.get('away_shots', 0))
    h_danger = safe_int(stats.get('home_dangerous', 0))
    
    templates = {
        'iy': [
            f"{home} ilk yarı baskısını {h_sot} isabetli şutla sürdürüyor. Yüksek tempoda gol kaçınılmaz.",
            f"İlk 45 dakikada {h_sot + a_sot} isabetli şut üretildi. Kapalı savunma kırılmak üzere."
        ],
        'ms': [
            f"Maçın ikinci yarısında {home} {h_sot} isabetli şutla üstünlüğü elinde tutuyor.",
            f"Toplam {total_shots} şut ve %{pressure} baskı skoruyla gol potansiyeli yüksek."
        ],
        'kg': [
            f"İki takım da açık oynayarak {h_sot + a_sot} isabetli şut üretti. Karşılıklı gol için zemin hazır.",
            f"Defans zafiyetleri ve hücum tempesi göz önüne alındığında her iki kalede de gol beklentisi mevcut."
        ],
        'corner': [
            f"Kanat organizasyonları ve baskı {h_sot + a_sot} şuta dönüştü. Korner sayısı artmaya devam edecek.",
            f"Hücum baskısı ve geniş alan kullanımı korner üretimini destekliyor."
        ],
        'team': [
            f"{home} {h_sot} isabetli şutla etkili baskı kuruyor. Takım bazlı gol potansiyeli yüksek.",
            f"Ofansif üstünlük ve pozisyon baskısı 1.5 üstü için uygun şartları oluşturuyor."
        ],
        'default': [
            f"{home} {h_sot} isabetli şutla etkili baskı kuruyor. İstatistikler {pick} lehine.",
            f"Oyunun temposu ve pozisyon üstünlüğü ({pressure}%) değerli bir fırsat sunuyor."
        ]
    }
    
    category = pick_type if pick_type in templates else 'default'
    return random.choice(templates[category])

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
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING
    )

    data   = await fetch_api(LIVE_URL)
    events = data.get("events", [])
    if not events:
        await update.message.reply_text("📭 Şu an canlı maç yok.")
        return

    sem = asyncio.Semaphore(8)

    async def fetch_minute_for(mid: str):
        async with sem:
            r = await fetch_api(MATCH_URL.format(mid))
            ev = r.get("event", {}) if isinstance(r, dict) else {}
            return get_real_minute(ev) if ev else None

    chosen = events[:20]
    tasks  = []
    for m in chosen:
        mid = str(m.get("id", ""))
        tasks.append(fetch_minute_for(mid))

    minutes = await asyncio.gather(*tasks, return_exceptions=True)

    lines = ["⚽ <b>CANLI MAÇLAR</b>", ""]
    shown = 0

    for m, mn in zip(chosen, minutes):
        stype = (m.get("status", {}).get("type") or "").lower()
        if stype in ("finished", "ended", "notstarted", "scheduled"):
            continue

        if isinstance(mn, Exception) or not mn:
            mn = get_real_minute(m)

        if mn in ("İY", "MS", "0'"):
            continue

        h  = html.escape(m.get("homeTeam", {}).get("name", "?") or "?")
        a  = html.escape(m.get("awayTeam", {}).get("name", "?") or "?")
        sh = safe_int(m.get("homeScore", {}).get("current", 0))
        sa = safe_int(m.get("awayScore", {}).get("current", 0))

        lines.append(f"⏱ <code>{mn}</code> | {h} <b>{sh}-{sa}</b> {a}")
        shown += 1

    if shown == 0:
        await update.message.reply_text("📭 Şu an listelenecek canlı maç yok.")
        return

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True
    )

async def control_command(update: Update,
                           context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    api_data  = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")
    ai_test   = await get_ai_insight(
        "TestA", "TestB",
        {'home_sot': 3, 'away_sot': 1, 'home_shots': 8,
         'away_shots': 3, 'home_corners': 4, 'away_corners': 1,
         'home_poss': 60, 'away_poss': 40,
         'home_dangerous': 8, 'away_dangerous': 3,
         'home_big_chances': 2, 'away_big_chances': 0},
        "MS 1.5 ÜST", 70, 55, "1-0", 1.2, "ms"
    )
    delivery = "✅ OK"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Test")
    except:
        delivery = "❌ HATA"

    report = (
        f"🛡 *BOT DENETİM RAPORU*\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {'✅ OK' if len(ai_test) > 10 else '❌ HATA'}\n"
        f"📩 İletim: {delivery}\n"
        f"⚽ Canlı Maç: {len(api_data.get('events', []))}\n"
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
                        hs  = safe_int(
                            ev.get('homeScore', {}).get('current', 0))
                        as_ = safe_int(
                            ev.get('awayScore', {}).get('current', 0))
                        is_win = (hs + as_) > sig.get('start_total', 0)
                        sig['status']      = 'WIN ✅' if is_win else 'LOSS ❌'
                        sig['final_score'] = f"{hs}-{as_}"
                        updated = True

            if updated:
                await manage_history("write", history)
                logger.info("📊 Sonuçlar güncellendi.")
        except Exception as e:
            logger.error(f"Result tracker: {e}")

        await asyncio.sleep(600)

# ─────────────────────────────────────────
# SİNYAL MONİTÖRÜ (Gelişmiş)
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

            sent_ids   = {str(x['id']) for x in history}
            candidates = []

            for m in events:
                ok, result = should_check_match(m, sent_ids)
                if ok:
                    candidates.append((m, result))

            logger.info(
                f"📊 {len(events)} maç → "
                f"{len(candidates)} aday → "
                f"{len(history)} sinyal kayıtlı"
            )

            for m, mn_int in candidates:
                try:
                    mid   = str(m.get('id', ''))
                    stats = await get_stats(mid)
                    if stats and stats.get("minute_int", 0) > 0:
                        mn_int = stats["minute_int"]

                    if not stats or not stats.get('has'):
                        continue

                    res = brain.analyze_advanced(m, stats, mn_int)

                    if not res.get('is_signal'):
                        logger.info(f"⏭ {res.get('reason', '?')}")
                        continue

                    home_name = m.get('homeTeam', {}).get('name', '?')
                    away_name = m.get('awayTeam', {}).get('name', '?')
                    league    = m.get('tournament', {}).get(
                        'name', 'Bilinmiyor')
                    xg_val    = res.get('xg', 0.0)
                    momentum  = res.get('momentum', 0)
                    pick_type = res.get('pick_type', 'ust')

                    logger.info(
                        f"🔍 Sinyal: {home_name} vs {away_name} "
                        f"| {mn_int}' | {res['pick']}"
                    )

                    ai_msg = await get_ai_insight(
                        home_name, away_name, stats,
                        res['pick'], res['pressure'],
                        mn_int, res['score'], xg_val, pick_type
                    )

                    # Alternatif bahis formatlama (yeni)
                    alt_picks = res.get('alt', [])
                    alt_txt = ""
                    if alt_picks:
                        alt_lines = []
                        for p in alt_picks[:2]:
                            bet_name = p[0]
                            odds = p[1]
                            risk = p[2]
                            type_code = p[3] if len(p) > 3 else ""
                            risk_emoji = {"Düşük": "🟢", "Orta": "🟡", "Yüksek": "🔴", "Çok Düşük": "🟢"}.get(risk, "⚪")
                            alt_lines.append(f"  {risk_emoji} `{bet_name}` @ {odds} ({risk})")
                        
                        if alt_lines:
                            alt_txt = "\n💡 *ALTERNATİF STRATEJİLER*\n" + "\n".join(alt_lines) + "\n"

                    bar_val = max(0, min(100, res['pressure']))
                    bar     = ("🟩" * (bar_val // 10) +
                               "⬜" * (10 - bar_val // 10))

                    confirms = res.get('confirmations', [])
                    conf_txt = " · ".join(confirms[:3])

                    period_emoji = (
                        "1️⃣" if res['period'] == "1. YARI" else "2️⃣"
                    )

                    h_xg = stats.get('home_xg', 0.0)
                    a_xg = stats.get('away_xg', 0.0)
                    if h_xg > 0 or a_xg > 0:
                        xg_line = f"`{h_xg} - {a_xg}` (Sofascore)"
                    else:
                        xg_line = f"`{xg_val}` (tahmini)"

                    # Bahis tipi emoji
                    bet_emojis = {
                        "iy": "⏱", "ms": "🏁", "kg": "⚽", 
                        "corner": "🚩", "team": "🎯", 
                        "handicap": "📊", "taraf": "🥅"
                    }
                    bet_emoji = bet_emojis.get(pick_type, "🎯")

                    txt = (
                        f"📡 *SİNYAL* {bet_emoji} | {time.strftime('%H:%M')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚽ *{home_name}* `{res['score']}` *{away_name}*\n"
                        f"🏆 _{league}_\n"
                        f"⏱ `{mn_int}'` {period_emoji} {res['period']}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 *STRATEJİ:* `{res['pick']}`\n"
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
                        f"├ 🎮 Hakimiyet:   "
                        f"`%{stats['home_poss']} - %{stats['away_poss']}`\n"
                        f"├ 🔥 Teh. Atak:   "
                        f"`{stats['home_dangerous']} - {stats['away_dangerous']}`\n"
                        f"├ 💥 Büyük Fırsat:"
                        f"`{stats['home_big_chances']} - {stats['away_big_chances']}`\n"
                        f"└ 🧤 Kurtarış:    "
                        f"`{stats['home_saves']} - {stats['away_saves']}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔥 *BASKI:* {bar} `%{res['pressure']}`\n"
                        f"📐 *xG:* {xg_line}\n"
                        f"⚡ *Momentum:* `{momentum}`\n"
                        f"👊 *Üstün:* {res['team']}\n"
                        f"✅ _{conf_txt}_\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🧠 *ANALİZ:* _{ai_msg}_\n"
                        f"{alt_txt}"
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
                    logger.info(f"✅ Sinyal: {res['pick']}")
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Maç hatası ({m.get('id','')}): {e}")
                    continue

        except Exception as e:
            logger.error(f"Monitör hatası: {e}")

        await asyncio.sleep(180)

# ─────────────────────────────────────────
# BAŞLATMA
# ─────────────────────────────────────────
async def post_init(app):
    asyncio.create_task(result_tracker(app))
    asyncio.create_task(signal_monitor(app))
    logger.info("✅ Görevler başladı.")
    
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update handling error", exc_info=context.error)    

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

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("canli",   live_command))
    app.add_handler(CommandHandler("kontrol", control_command))

    logger.info("✅ Bot hazır!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )
