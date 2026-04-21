# bot.py

import os, asyncio, httpx, json, time, logging
import html
from telegram.constants import ParseMode, ChatAction
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
        
def minute_str_to_int(minute_str: str) -> int:
    """
    "45'" -> 45
    "45+2'" -> 47
    "90+4'" -> 94
    "İY/MS/0'" -> 0
    """
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
    """
    En stabil yöntem:
    - period (1/2/3/4) -> yarı bazını belirler
    - currentPeriodStartTimestamp -> o devrenin başlangıcından dakika hesaplar
    - elapsed varsa karşılaştırma amaçlı kullanır
    """
    try:
        status = (m.get("status") or {})
        stype  = (status.get("type") or "").lower()
        desc   = (status.get("description") or "").lower()

        # kesin durumlar
        if stype in ("finished", "ended"):
            return "MS"
        if stype in ("notstarted", "scheduled"):
            return "0'"

        # devre arası varyantları
        if any(x in desc for x in ("ht", "half-time", "halftime", "interval", "break")):
            return "İY"

        t      = (m.get("time") or {})
        period = safe_int(t.get("period", 0), 0)

        # period bazları (uzatma gelirse 3/4)
        base_map = {1: 0, 2: 45, 3: 90, 4: 105}
        base     = base_map.get(period, 0)

        now = int(time.time())

        cps = t.get("currentPeriodStartTimestamp") or m.get("currentPeriodStartTimestamp")
        minute_from_cps = None
        if cps:
            diff = max(0, (now - int(cps)) // 60)
            minute_from_cps = base + diff

        elapsed = safe_int(status.get("elapsed", 0), 0)
        minute_from_elapsed = None
        if elapsed > 0:
            # bazı maçlarda elapsed ikinci yarıda 0-45 arası olur, bazı maçlarda 46+ mutlak gelir
            if period == 2 and elapsed <= 45:
                minute_from_elapsed = 45 + elapsed
            elif period == 1 and elapsed <= 45:
                minute_from_elapsed = elapsed
            else:
                minute_from_elapsed = elapsed

        # seçim: cps varsa genelde en doğru
        minute = None
        if minute_from_cps is not None:
            minute = minute_from_cps
            # elapsed da varsa ve çok farklıysa (API sapması), mantıklı olana yaklaş
            if minute_from_elapsed is not None and abs(minute_from_elapsed - minute_from_cps) >= 7:
                # period aralığına uyanı seç
                cand = [minute_from_cps, minute_from_elapsed]
                if period == 1:
                    cand = [x for x in cand if 1 <= x <= 55] or cand
                elif period == 2:
                    cand = [x for x in cand if 46 <= x <= 105] or cand
                minute = cand[0]
        elif minute_from_elapsed is not None:
            minute = minute_from_elapsed
        else:
            # son çare: startTimestamp (en kötü fallback)
            start_ts = m.get("startTimestamp")
            if start_ts:
                minute = max(1, (now - int(start_ts)) // 60)
            else:
                return "0'"

        # makul aralığa sıkıştır
        minute = max(1, min(130, int(minute)))

        # uzatma gösterimi (istersen): 45+ / 90+
        if period == 1 and minute > 45:
            return f"45+{minute-45}'"
        if period == 2 and minute > 90:
            return f"90+{minute-90}'"

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

    # İstatistik endpoint
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

    # Maç endpoint (xG)
    try:
        if (not isinstance(match_resp, Exception)
                and match_resp.status_code == 200):
            ev      = match_resp.json().get('event', {})
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
# GROQ AI
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
        "Rol: Profesyonel canlı maç analisti.\n"
        "Çıktı: TAM 2 cümle, Türkçe, kısa ve net.\n"
        "Kural: Sadece sayı/istatistik üzerinden konuş; abartı ve genel laf yok.\n"
        "Kural: 1. cümle baskıyı rakamlarla özetlesin. 2. cümle seçimi (pick) gerekçelendirsin.\n"
        "Kural: Her cümle max 18 kelime. Emoji yok. Markdown karakteri yok.\n\n"
        f"Maç: {home} - {away}\n"
        f"Dakika: {minute} | Skor: {score}\n"
        f"SOT: {stats.get('home_sot',0)}-{stats.get('away_sot',0)} | "
        f"Şut: {stats.get('home_shots',0)}-{stats.get('away_shots',0)} | "
        f"Tehlikeli: {stats.get('home_dangerous',0)}-{stats.get('away_dangerous',0)} | "
        f"Korner: {stats.get('home_corners',0)}-{stats.get('away_corners',0)} | "
        f"Pozisyon: {stats.get('home_poss',50)}-{stats.get('away_poss',50)} | "
        f"xG: {xg_line} | Baskı: {pressure}\n"
        f"Pick: {pick}\n\n"
        "Sadece iki cümleyi yaz:"
    )
    payload = {
        "model":       "llama-3.1-8b-instant",
        "messages":    [{"role": "user", "content": prompt}],
        "temperature": 0.15,
        "max_tokens":  90
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload, headers=headers
                )
                if r.status_code == 200:
                    raw   = r.json()['choices'][0]['message']['content']
                    clean = (raw.replace('*', '').replace('_', '')
                               .replace('`', '').replace('[', '')
                               .replace(']', '').strip())
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
    sot  = stats.get('home_sot', 0)
    shots= stats.get('home_shots', 0)
    corn = stats.get('home_corners', 0)
    dan  = stats.get('home_dangerous', 0)
    opts = [
        f"{home} {sot} isabetli şut ve {dan} tehlikeli atakla baskı kuruyor. {pick} için istatistikler uygun.",
        f"{shots} şut denemesinden {sot} tanesi kaleyi buldu. Baskı %{pressure} ile kritik eşikte.",
        f"{home} {sot} isabetli şut ve {corn} kornerle kapıyı zorluyor. {pick} hesaplanmış seçim.",
        f"Tehlikeli atak sayısı ve isabetli şut oranı baskıyı doğruluyor. {pick} bu veriyle değer taşıyor.",
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
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING
        )

        data   = await fetch_api(LIVE_URL)
        events = data.get('events', [])
        if not events:
            await update.message.reply_text("📭 Şu an canlı maç yok.")
            return

        lines = ["⚽ <b>CANLI MAÇLAR</b>", ""]
        shown = 0

        for m in events:
            stype = (m.get("status", {}).get("type") or "").lower()
            # Bitmiş / başlamamış maçları canlı listeye sokma
            if stype in ("finished", "ended", "notstarted", "scheduled"):
                continue

            m_min = get_real_minute(m)
            if m_min in ("İY", "MS", "0'"):
                continue

            h  = html.escape(m.get('homeTeam', {}).get('name', '?') or "?")
            a  = html.escape(m.get('awayTeam', {}).get('name', '?') or "?")
            sh = safe_int(m.get('homeScore', {}).get('current', 0))
            sa = safe_int(m.get('awayScore', {}).get('current', 0))

            lines.append(f"⏱ <code>{m_min}</code> | {h} <b>{sh}-{sa}</b> {a}")
            shown += 1
            if shown >= 20:
                break

        if shown == 0:
            await update.message.reply_text("📭 Şu an listelenecek canlı maç yok.")
            return

        await update.message.reply_text(
            "\n".join(lines),
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True
        )

    except Exception as e:
        logger.exception("live_command error")
        await update.message.reply_text(f"/canli hata: {e}")

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
        "MS 1.5 ÜST", 70, 55, "1-0", 1.2
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

                    logger.info(
                        f"🔍 Sinyal: {home_name} vs {away_name} "
                        f"| {mn_int}' | {res['pick']}"
                    )

                    ai_msg = await get_ai_insight(
                        home_name, away_name, stats,
                        res['pick'], res['pressure'],
                        mn_int, res['score'], xg_val
                    )

                    alt_picks = [p for p in res.get('alt', [])
                                 if p[0] != res['pick']]
                    alt_txt   = "".join(
                        [f"  • {p[0]} (Risk: {p[2]})\n"
                         for p in alt_picks[:2]]
                    )
                    alt_section = (
                        f"\n💡 *ALTERNATİF*\n{alt_txt}" if alt_txt else ""
                    )

                    bar_val = max(0, min(100, res['pressure']))
                    bar     = ("🟩" * (bar_val // 10) +
                               "⬜" * (10 - bar_val // 10))

                    confirms = res.get('confirmations', [])
                    conf_txt = " · ".join(confirms[:3])

                    period_emoji = (
                        "1️⃣" if res['period'] == "1. YARI" else "2️⃣"
                    )

                    # xG kaynağı
                    h_xg = stats.get('home_xg', 0.0)
                    a_xg = stats.get('away_xg', 0.0)
                    if h_xg > 0 or a_xg > 0:
                        xg_line = f"`{h_xg} - {a_xg}` (Sofascore)"
                    else:
                        xg_line = f"`{xg_val}` (tahmini)"

                    txt = (
                        f"📡 *SİNYAL* | {time.strftime('%H:%M')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚽ *{home_name}* `{res['score']}` *{away_name}*\n"
                        f"🏆 _{league}_\n"
                        f"⏱ `{mn_int}'` {period_emoji} {res['period']}\n"
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
    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception("Update handling error", exc_info=context.error)

app.add_error_handler(error_handler)
    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("canli",   live_command))
    app.add_handler(CommandHandler("kontrol", control_command))
    logger.info("✅ Bot hazır!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )
