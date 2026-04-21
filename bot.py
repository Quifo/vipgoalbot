import os, asyncio, httpx, json, time, logging, re, html
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode, ChatAction
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
ODDS_URL  = "https://www.sofascore.com/api/v1/event/{}/odds/1/all"

last_ai_requests  = []
MAX_AI_PER_MINUTE = 20

# odds + trend cache
odds_event_cache = {}     # mid -> (ts, data or None)
odds_parse_fail_once = set()  # mid set
match_snapshots  = {}     # mid -> snapshot

# Lig kalite filtresi
LEAGUE_BLACKLIST = [
    r"\bu\d{2}\b", r"\bu\d{1}\b",
    r"youth", r"junior", r"academy",
    r"reserves?", r"reserve",
    r"friendly", r"club friendly", r"amistoso",
]
LEAGUE_BLACKLIST_RX = [re.compile(p, re.IGNORECASE) for p in LEAGUE_BLACKLIST]


def is_league_allowed(m):
    try:
        t = m.get("tournament", {}) or {}
        cat = t.get("category", {}) or {}
        name = f"{cat.get('name','')} {t.get('name','')}".strip()
        if not name:
            return False
        return not any(rx.search(name) for rx in LEAGUE_BLACKLIST_RX)
    except:
        return True


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
        # ms gelirse saniyeye indir
        if ts > 10_000_000_000:
            ts //= 1000
        return ts
    except:
        return None


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
# DAKİKA HESAPLAMA (stabil +1)
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
        async with httpx.AsyncClient(timeout=20.0, headers=GIST_HEADERS) as client:
            try:
                if mode == "read":
                    r = await client.get(url)
                    if r.status_code == 200:
                        files = r.json().get('files', {})
                        if 'sent_signals.json' in files:
                            return json.loads(files['sent_signals.json']['content'])
                    return []
                else:
                    payload = {"files": {"sent_signals.json": {"content": json.dumps(data)}}}
                    r = await client.patch(url, json=payload)
                    if r.status_code != 200:
                        logger.error(f"Gist write: {r.status_code}")
            except Exception as e:
                logger.error(f"Gist hatası: {e}")
                return [] if mode == "read" else None


# ─────────────────────────────────────────
# İSTATİSTİK ÇEKİMİ (event'ten xG + dakika + kırmızı kart)
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
        'home_red': 0,       'away_red': 0,
        'minute_str': None,  'minute_int': 0,
        'has': False
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=HEADERS) as client:
        try:
            stats_resp, match_resp = await asyncio.gather(
                client.get(stats_url),
                client.get(match_url),
                return_exceptions=True
            )
        except Exception as e:
            logger.error(f"Paralel istek hatası ({match_id}): {e}")
            return None

    # Stats endpoint
    try:
        if (not isinstance(stats_resp, Exception) and stats_resp.status_code == 200):
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

    # Match endpoint (xG + minute + red cards)
    try:
        if (not isinstance(match_resp, Exception) and match_resp.status_code == 200):
            ev = match_resp.json().get('event', {}) or {}

            home_xg = ev.get('homeXg')
            away_xg = ev.get('awayXg')
            if home_xg is not None:
                s['home_xg'] = round(safe_float(home_xg), 2)
            if away_xg is not None:
                s['away_xg'] = round(safe_float(away_xg), 2)

            # Dakika (event detayından daha doğru)
            minute_str = get_real_minute(ev)
            s["minute_str"] = minute_str
            s["minute_int"] = safe_int(str(minute_str).replace("'", ""), 0)

            # Kırmızı kart (varsa)
            s["home_red"] = safe_int(ev.get("homeRedCards", 0))
            s["away_red"] = safe_int(ev.get("awayRedCards", 0))
    except Exception as e:
        logger.error(f"Match parse ({match_id}): {e}")

    return s if s['has'] else None


# ─────────────────────────────────────────
# TREND (son snapshot'a göre)
# ─────────────────────────────────────────
def compute_trend(mid: str, stats: dict, minute_int: int):
    try:
        now = time.time()
        key = str(mid)

        shots = safe_int(stats.get("home_shots", 0)) + safe_int(stats.get("away_shots", 0))
        sot   = safe_int(stats.get("home_sot", 0)) + safe_int(stats.get("away_sot", 0))
        dang  = safe_int(stats.get("home_dangerous", 0)) + safe_int(stats.get("away_dangerous", 0))

        prev = match_snapshots.get(key)
        match_snapshots[key] = {"t": now, "shots": shots, "sot": sot, "dangerous": dang, "minute": minute_int}

        if not prev:
            return {"trend_ok": False, "shots_per_min": 0.0, "sot_per_min": 0.0, "danger_per_min": 0.0}

        dm = max(1, minute_int - safe_int(prev.get("minute", minute_int)))
        dshots = max(0, shots - safe_int(prev.get("shots", shots)))
        dsot   = max(0, sot   - safe_int(prev.get("sot", sot)))
        ddang  = max(0, dang  - safe_int(prev.get("dangerous", dang)))

        return {
            "trend_ok": True,
            "shots_per_min": round(dshots / dm, 2),
            "sot_per_min": round(dsot / dm, 2),
            "danger_per_min": round(ddang / dm, 2),
        }
    except:
        return {"trend_ok": False, "shots_per_min": 0.0, "sot_per_min": 0.0, "danger_per_min": 0.0}


# ─────────────────────────────────────────
# ODDS FETCH + PARSE
# ─────────────────────────────────────────
async def fetch_odds_event(mid: str):
    mid = str(mid)
    now = time.time()
    cached = odds_event_cache.get(mid)
    if cached and now - cached[0] < 180:
        return cached[1]

    data = await fetch_api(ODDS_URL.format(mid))
    if not isinstance(data, dict) or not data:
        odds_event_cache[mid] = (now, None)
        return None

    odds_event_cache[mid] = (now, data)
    return data


def _find_decimal_odd(obj):
    if not isinstance(obj, dict):
        return None
    for k in ("decimalValue", "decimalOdds", "value", "odds"):
        v = obj.get(k)
        if isinstance(v, (int, float)) and v >= 1.01:
            return float(v)
    return None


def extract_odds_from_event(odds_data: dict, pick: str):
    if not isinstance(odds_data, dict):
        return None

    pick_u = str(pick).upper().strip()
    markets = odds_data.get("markets") or odds_data.get("odds") or []
    if not isinstance(markets, list):
        return None

    # Pick parse
    m_over = re.search(r"^(MS|İY|IY)\s+(\d+(?:\.\d+)?)\s+ÜST$", pick_u)
    is_btts = (pick_u == "KG VAR")
    m_corner = re.search(r"^KORNER\s+(\d+(?:\.\d+)?)\s+ÜST$", pick_u)
    is_1x2 = pick_u in ("MS 1Ç", "MS 2Ç")

    for mk in markets:
        mname = str(mk.get("marketName") or mk.get("name") or "").lower()
        choices = mk.get("choices") or mk.get("outcomes") or mk.get("selections") or []
        if not isinstance(choices, list):
            continue

        # TOTAL GOALS OVER
        if m_over:
            if not any(x in mname for x in ["total goals", "over/under", "goals over/under"]):
                continue
            line = m_over.group(2)
            for ch in choices:
                cname = str(ch.get("name") or ch.get("choiceName") or "").lower()
                odd = _find_decimal_odd(ch)
                if odd and ("over" in cname) and (line in cname):
                    return odd

        # BTTS YES
        if is_btts:
            if not any(x in mname for x in ["both teams to score", "btts"]):
                continue
            for ch in choices:
                cname = str(ch.get("name") or "").lower()
                odd = _find_decimal_odd(ch)
                if odd and (("yes" in cname) or ("var" in cname)):
                    return odd

        # CORNERS OVER
        if m_corner:
            if "corner" not in mname:
                continue
            line = m_corner.group(1)
            for ch in choices:
                cname = str(ch.get("name") or "").lower()
                odd = _find_decimal_odd(ch)
                if odd and ("over" in cname) and (line in cname):
                    return odd

        # 1X2 HOME/AWAY
        if is_1x2:
            if not any(x in mname for x in ["match winner", "1x2"]):
                continue
            want_home = pick_u.endswith("1Ç")
            for ch in choices:
                cname = str(ch.get("name") or "").lower()
                odd = _find_decimal_odd(ch)
                if not odd:
                    continue
                if want_home and cname in ("1", "home", "ev sahibi"):
                    return odd
                if (not want_home) and cname in ("2", "away", "deplasman"):
                    return odd

    return None


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

        mn_int = safe_int(minute_str.replace("'", ""), 0)
        if not (10 < mn_int < 85):
            return False, f"Dakika dışı: {mn_int}"
        if not m.get('tournament'):
            return False, "Turnuva bilgisi yok"

        if not is_league_allowed(m):
            return False, "Lig filtresi"

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
async def get_ai_insight(home, away, stats, pick, pressure, minute, score, xg=0.0):
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

    hxg = safe_float(stats.get("home_xg", 0.0), 0.0) if isinstance(stats, dict) else 0.0
    axg = safe_float(stats.get("away_xg", 0.0), 0.0) if isinstance(stats, dict) else 0.0
    if hxg > 0 or axg > 0:
        xg_line = f"{hxg}-{axg}"
    else:
        xg_line = str(round(safe_float(xg, 0.0), 2))

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
                    raw = r.json()['choices'][0]['message']['content']
                    clean = (raw.replace('*', '').replace('_', '')
                               .replace('`', '').replace('[', '')
                               .replace(']', '').strip())

                    # 2 cümleye zorla
                    import re as _re
                    clean = _re.sub(r"\s+", " ", clean).strip()
                    parts = _re.split(r"(?<=[.!?])\s+", clean)
                    parts = [p.strip() for p in parts if p.strip()]
                    if len(parts) >= 2:
                        clean = parts[0] + " " + parts[1]
                    else:
                        return _fallback_comment(home, stats, pick, pressure)

                    if len(clean.split()) > 40:
                        return _fallback_comment(home, stats, pick, pressure)

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
    hsot = stats.get("home_sot", 0); asot = stats.get("away_sot", 0)
    hsh  = stats.get("home_shots", 0); ash = stats.get("away_shots", 0)
    hdan = stats.get("home_dangerous", 0); adan = stats.get("away_dangerous", 0)
    hcor = stats.get("home_corners", 0); acor = stats.get("away_corners", 0)

    dom = "Ev sahibi" if (hsot + hdan + hsh) >= (asot + adan + ash) else "Deplasman"
    line1 = f"{dom} SOT {hsot}-{asot}, şut {hsh}-{ash}, tehlikeli {hdan}-{adan}, korner {hcor}-{acor} ile baskın."
    line2 = f"Bu tempo ve %{pressure} baskı, {pick} seçimini değerli kılar."
    return f"{line1} {line2}"


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
        events = data.get("events", [])
        if not events:
            await update.message.reply_text("📭 Şu an canlı maç yok.")
            return

        # Dakika doğruluğu için event/{id} üzerinden ilk 20 maçı netleştir
        sem = asyncio.Semaphore(10)

        async def fetch_minute_for(mid: str):
            async with sem:
                r = await fetch_api(MATCH_URL.format(mid))
                ev = r.get("event", {}) if isinstance(r, dict) else {}
                return get_real_minute(ev) if ev else None

        chosen = events[:20]
        tasks  = [fetch_minute_for(str(m.get("id", ""))) for m in chosen]
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
    except Exception as e:
        logger.exception("live_command error")
        await update.message.reply_text(f"/canli hata: {e}")


async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text(
        "🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN
    )

    api_data  = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")

    ai_test = await get_ai_insight(
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

    # ODDS TEST
    odds_status = "⚠️ N/A"
    try:
        evs = api_data.get("events", []) if isinstance(api_data, dict) else []
        if evs:
            test_id = str(evs[0].get("id", ""))
            od = await fetch_odds_event(test_id)
            markets = (od.get("markets") or od.get("odds")) if isinstance(od, dict) else None
            odds_status = "✅ OK" if markets else "❌ FAIL"
    except:
        odds_status = "❌ FAIL"

    report = (
        f"🛡 *BOT DENETİM RAPORU*\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {'✅ OK' if len(ai_test) > 10 else '❌ HATA'}\n"
        f"🎲 Odds: {odds_status}\n"
        f"📩 İletim: {delivery}\n"
        f"⚽ Canlı Maç: {len(api_data.get('events', [])) if isinstance(api_data, dict) else 0}\n"
        f"📊 Kayıtlı Sinyal: {len(gist_data) if isinstance(gist_data, list) else 0}\n\n"
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
                    r  = await fetch_api(MATCH_URL.format(sig['id']))
                    ev = r.get('event', {}) if isinstance(r, dict) else {}
                    if ev.get('status', {}).get('type') == 'finished':
                        hs  = safe_int(ev.get('homeScore', {}).get('current', 0))
                        as_ = safe_int(ev.get('awayScore', {}).get('current', 0))
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
            events  = data.get('events', []) if isinstance(data, dict) else []
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []

            sent_ids   = {str(x['id']) for x in history if 'id' in x}
            candidates = []

            for m in events:
                ok, result = should_check_match(m, sent_ids)
                if ok:
                    candidates.append((m, result))

            logger.info(
                f"📊 {len(events)} maç → {len(candidates)} aday → {len(history)} sinyal kayıtlı"
            )

            for m, mn_int in candidates:
                try:
                    mid   = str(m.get('id', ''))
                    stats = await get_stats(mid)

                    if not stats or not stats.get('has'):
                        continue

                    # event detay dakikası daha doğruysa override et
                    if stats.get("minute_int", 0) > 0:
                        mn_int = stats["minute_int"]

                    trend = compute_trend(mid, stats, mn_int)

                    res = brain.analyze_advanced(m, stats, mn_int, trend=trend)

                    if not res.get('is_signal'):
                        logger.info(f"⏭ {res.get('reason', '?')}")
                        continue

                    # Oran yoksa maç es geç
                    odds_data = await fetch_odds_event(mid)
                    if not odds_data:
                        logger.info("⏭ Oran yok, maç es geçildi.")
                        continue

                    # Brain pick + alt picklerden (ilk 4) value seç
                    candidate_picks = [res["pick"]]
                    for p in res.get("alt", []):
                        if not p:
                            continue
                        p_label = p[0]
                        if p_label and p_label != res["pick"]:
                            candidate_picks.append(p_label)

                    # uniq + limit
                    uniq = []
                    for p in candidate_picks:
                        if p not in uniq:
                            uniq.append(p)
                    candidate_picks = uniq[:4]

                    best_choice = None
                    prob_map = res.get("pick_probs", {}) or {}

                    for pck in candidate_picks:
                        odd = extract_odds_from_event(odds_data, pck)
                        if not odd:
                            continue
                        if odd < brain.MIN_ODDS:
                            continue

                        pick_prob = prob_map.get(pck, res.get("prob", 60))
                        model_p = float(pick_prob) / 100.0
                        implied = 1.0 / float(odd)
                        value = model_p - implied

                        if value < 0.03:
                            continue

                        if (best_choice is None) or (value > best_choice["value"]):
                            best_choice = {
                                "pick": pck,
                                "odds": float(odd),
                                "value": float(value),
                                "pick_prob": int(pick_prob)
                            }
                            
                        if not best_choice:
                            # Odds geldi ama hiçbir pick için oran bulamadık mı? (Parser kaçırıyor olabilir)
                            key = str(mid)
                            if key not in odds_parse_fail_once:
                                odds_parse_fail_once.add(key)
                                mkts = odds_data.get("markets") or odds_data.get("odds") or []
                                names = []
                                if isinstance(mkts, list):
                                    for mk in mkts[:15]:
                                        names.append(str(mk.get("marketName") or mk.get("name") or "?"))
                            logger.info(f"ODDS PARSE FAIL mid={mid} picks={candidate_picks} markets={names}")
                        continue

                    # seçimi güncelle
                    res["pick"]  = best_choice["pick"]
                    res["odds"]  = round(best_choice["odds"], 2)
                    res["value"] = round(best_choice["value"], 3)
                    res["prob"]  = int(best_choice["pick_prob"])

                    home_name = m.get('homeTeam', {}).get('name', '?')
                    away_name = m.get('awayTeam', {}).get('name', '?')
                    league    = m.get('tournament', {}).get('name', 'Bilinmiyor')
                    xg_val    = res.get('xg', 0.0)
                    momentum  = res.get('momentum', 0)

                    logger.info(
                        f"🔍 Sinyal: {home_name} vs {away_name} | {mn_int}' | {res['pick']} @ {res['odds']} (v:{res['value']})"
                    )

                    ai_msg = await get_ai_insight(
                        home_name, away_name, stats,
                        res['pick'], res['pressure'],
                        mn_int, res['score'], xg_val
                    )

                    # Alternatifler
                    alt_picks = [p for p in res.get('alt', []) if p and p[0] != res['pick']]
                    alt_lines = []
                    for p in alt_picks[:2]:
                        p_label = p[0]
                        p_risk  = p[2]
                        p_prob  = p[3]
                        if p_prob is not None:
                            alt_lines.append(f"  • {p_label} (Risk: {p_risk}, Model: {p_prob}%)")
                        else:
                            alt_lines.append(f"  • {p_label} (Risk: {p_risk})")

                    alt_section = f"\n💡 *ALTERNATİF*\n" + "\n".join(alt_lines) + "\n" if alt_lines else ""

                    bar_val = max(0, min(100, res['pressure']))
                    bar     = ("🟩" * (bar_val // 10) + "⬜" * (10 - bar_val // 10))

                    confirms = res.get('confirmations', [])
                    conf_txt = " · ".join(confirms[:3])

                    period_emoji = "1️⃣" if res['period'] == "1. YARI" else "2️⃣"

                    h_xg = stats.get('home_xg', 0.0)
                    a_xg = stats.get('away_xg', 0.0)
                    if h_xg > 0 or a_xg > 0:
                        xg_line = f"`{h_xg} - {a_xg}` (Sofascore)"
                    else:
                        xg_line = f"`{xg_val}` (tahmini)"

                    odds_value_line = f"🎲 *Oran:* `{res['odds']}` | 📈 *Value:* `{res['value']}`\n"

                    txt = (
                        f"📡 *SİNYAL* | {time.strftime('%H:%M')}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚽ *{home_name}* `{res['score']}` *{away_name}*\n"
                        f"🏆 _{league}_\n"
                        f"⏱ `{mn_int}'` {period_emoji} {res['period']}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 *TAHMİN:* `{res['pick']}`\n"
                        f"{odds_value_line}"
                        f"📊 *Güven:* {res['confidence']} `{res['prob']}%`\n"
                        f"⚠️ *Risk:* `{res['risk']}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 *İSTATİSTİKLER*\n"
                        f"┌ 🥅 İsabetli Şut: `{stats['home_sot']} - {stats['away_sot']}`\n"
                        f"├ ⚡ Toplam Şut:   `{stats['home_shots']} - {stats['away_shots']}`\n"
                        f"├ 🚩 Korner:      `{stats['home_corners']} - {stats['away_corners']}`\n"
                        f"├ 🎮 Hakimiyet:   `%{stats['home_poss']} - %{stats['away_poss']}`\n"
                        f"├ 🔥 Teh. Atak:   `{stats['home_dangerous']} - {stats['away_dangerous']}`\n"
                        f"├ 💥 Büyük Fırsat:`{stats['home_big_chances']} - {stats['away_big_chances']}`\n"
                        f"└ 🧤 Kurtarış:    `{stats['home_saves']} - {stats['away_saves']}`\n"
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
                        chat_id=CHAT_ID,
                        text=txt,
                        parse_mode=ParseMode.MARKDOWN
                    )

                    history.append({
                        "id":          mid,
                        "timestamp":   time.time(),
                        "status":      "pending",
                        "start_total": res['total_score'],
                        "match":       f"{home_name} vs {away_name}",
                        "pick":        res['pick'],
                        "odds":        res.get("odds"),
                        "value":       res.get("value"),
                        "prob":        res.get("prob")
                    })
                    await manage_history("write", history)
                    sent_ids.add(mid)

                    logger.info(f"✅ Sinyal: {res['pick']} @ {res['odds']} (v:{res['value']})")
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Maç hatası ({m.get('id','')}): {e}")
                    continue

        except Exception as e:
            logger.error(f"Monitör hatası: {e}")

        await asyncio.sleep(180)


# ─────────────────────────────────────────
# ERROR HANDLER
# ─────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error("Update handling error", exc_info=context.error)


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

    app.add_error_handler(error_handler)

    app.add_handler(CommandHandler("start",   start_command))
    app.add_handler(CommandHandler("canli",   live_command))
    app.add_handler(CommandHandler("kontrol", control_command))

    logger.info("✅ Bot hazır!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES
    )
