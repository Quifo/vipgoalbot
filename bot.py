import os, asyncio, httpx, json, time, logging, re, html
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode, ChatAction
from brain import BettingBrain
from dotenv import load_dotenv

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
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
    "Accept": "application/vnd.github.v3+json",
}

LIVE_URL  = "https://www.sofascore.com/api/v1/sport/football/events/live"
STATS_URL = "https://www.sofascore.com/api/v1/event/{}/statistics"
MATCH_URL = "https://www.sofascore.com/api/v1/event/{}"

ODDS_URL = "https://www.sofascore.com/api/v1/event/{}/odds/1/all"

last_ai_requests  = []
MAX_AI_PER_MINUTE = 20

# Trend snapshot cache
match_snapshots = {}  # mid -> {"t": time, "shots":int, "sot":int, "dangerous":int, "minute":int}

# Odds cache
odds_event_cache = {}  # mid -> (ts, data_or_none)

# League filters (C)
LEAGUE_BLACKLIST = [
    r"\bu\d{2}\b", r"\bu\d{1}\b",
    r"youth", r"junior", r"academy",
    r"reserves?", r"reserve",
    r"friendly", r"club friendly", r"amistoso",
]
LEAGUE_BLACKLIST_RX = [re.compile(p, re.IGNORECASE) for p in LEAGUE_BLACKLIST]


# ─────────────────────────────────────────
# SAFE CASTS
# ─────────────────────────────────────────
def safe_int(val, default=0):
    try:
        if val is None or val == "" or val == "-":
            return default
        return int(float(str(val).replace("%", "").strip()))
    except:
        return default


def safe_float(val, default=0.0):
    try:
        if val is None or val == "" or val == "-":
            return default
        return float(str(val).replace("%", "").strip())
    except:
        return default


def normalize_ts(ts):
    try:
        if ts is None:
            return None
        ts = int(ts)
        # sometimes milliseconds
        if ts > 10_000_000_000:
            ts //= 1000
        return ts
    except:
        return None


# ─────────────────────────────────────────
# API
# ─────────────────────────────────────────
async def fetch_api(url):
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True, headers=HEADERS) as client:
        try:
            r = await client.get(url)
            if r.status_code == 200:
                return r.json()
            return {}
        except Exception as e:
            logger.error(f"API Hatası ({url}): {e}")
            return {}


# ─────────────────────────────────────────
# LEAGUE FILTER
# ─────────────────────────────────────────
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
# MINUTE (fixed, +1 display)
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

        # infer second half if LIVE payload lacks period
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
# GIST HISTORY
# ─────────────────────────────────────────
async def manage_history(mode="read", data=None):
    url = f"https://api.github.com/gists/{GIST_ID}"
    async with gist_lock:
        async with httpx.AsyncClient(timeout=20.0, headers=GIST_HEADERS) as client:
            try:
                if mode == "read":
                    r = await client.get(url)
                    if r.status_code == 200:
                        files = r.json().get("files", {})
                        if "sent_signals.json" in files:
                            return json.loads(files["sent_signals.json"]["content"])
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
# STATS (+ minute + red cards)
# ─────────────────────────────────────────
async def get_stats(match_id):
    stats_url = STATS_URL.format(match_id)
    match_url = MATCH_URL.format(match_id)

    s = {
        "home_sot": 0, "away_sot": 0,
        "home_shots": 0, "away_shots": 0,
        "home_corners": 0, "away_corners": 0,
        "home_poss": 50, "away_poss": 50,
        "home_xg": 0.0, "away_xg": 0.0,
        "home_attacks": 0, "away_attacks": 0,
        "home_dangerous": 0, "away_dangerous": 0,
        "home_saves": 0, "away_saves": 0,
        "home_big_chances": 0, "away_big_chances": 0,
        "home_shots_box": 0, "away_shots_box": 0,
        "home_red": 0, "away_red": 0,
        "minute_str": None, "minute_int": 0,
        "has": False,
    }

    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True, headers=HEADERS) as client:
        try:
            stats_resp, match_resp = await asyncio.gather(
                client.get(stats_url),
                client.get(match_url),
                return_exceptions=True,
            )
        except Exception as e:
            logger.error(f"Paralel istek hatası ({match_id}): {e}")
            return None

    # statistics endpoint
    try:
        if (not isinstance(stats_resp, Exception)) and stats_resp.status_code == 200:
            data = stats_resp.json()
            for p in data.get("statistics", []):
                if p.get("period") != "ALL":
                    continue
                for g in p.get("groups", []):
                    for i in g.get("statisticsItems", []):
                        n  = i.get("name", "")
                        hv = safe_int(i.get("homeValue", 0))
                        av = safe_int(i.get("awayValue", 0))
                        if n == "Shots on target":
                            s["home_sot"], s["away_sot"] = hv, av
                            s["has"] = True
                        elif n == "Total shots":
                            s["home_shots"], s["away_shots"] = hv, av
                            s["has"] = True
                        elif n == "Corner kicks":
                            s["home_corners"], s["away_corners"] = hv, av
                        elif n == "Ball possession":
                            s["home_poss"], s["away_poss"] = hv, av
                        elif n == "Goalkeeper saves":
                            s["home_saves"], s["away_saves"] = hv, av
                        elif n == "Attacks":
                            s["home_attacks"], s["away_attacks"] = hv, av
                        elif n == "Dangerous attacks":
                            s["home_dangerous"], s["away_dangerous"] = hv, av
                        elif n == "Big chances":
                            s["home_big_chances"], s["away_big_chances"] = hv, av
                        elif n in ("Shots inside box", "Shots on box"):
                            s["home_shots_box"], s["away_shots_box"] = hv, av
    except Exception as e:
        logger.error(f"Stats parse ({match_id}): {e}")

    # match endpoint (xG + minute + cards)
    try:
        if (not isinstance(match_resp, Exception)) and match_resp.status_code == 200:
            ev = match_resp.json().get("event", {}) or {}

            # xG
            home_xg = ev.get("homeXg")
            away_xg = ev.get("awayXg")
            if home_xg is not None:
                s["home_xg"] = round(safe_float(home_xg), 2)
            if away_xg is not None:
                s["away_xg"] = round(safe_float(away_xg), 2)

            # minute (more accurate)
            minute_str = get_real_minute(ev)
            s["minute_str"] = minute_str
            s["minute_int"] = safe_int(str(minute_str).replace("'", ""), 0)

            # red cards (if provided)
            s["home_red"] = safe_int(ev.get("homeRedCards", 0))
            s["away_red"] = safe_int(ev.get("awayRedCards", 0))
    except Exception as e:
        logger.error(f"Match parse ({match_id}): {e}")

    return s if s["has"] else None


# ─────────────────────────────────────────
# PREFILTER (plus league)
# ─────────────────────────────────────────
def should_check_match(m, sent_ids):
    try:
        mid        = str(m.get("id", ""))
        minute_str = get_real_minute(m)

        if mid in sent_ids:
            return False, "Zaten gönderildi"
        if minute_str in ("İY", "MS", "0'"):
            return False, f"Geçersiz dakika: {minute_str}"

        mn_int = safe_int(minute_str.replace("'", ""), 0)

        # keep this aligned-ish with brain min/max
        if not (10 < mn_int < 85):
            return False, f"Dakika dışı: {mn_int}"

        if not m.get("tournament"):
            return False, "Turnuva bilgisi yok"

        if not is_league_allowed(m):
            return False, "Lig filtresi"

        h_s = safe_int(m.get("homeScore", {}).get("current", 0))
        a_s = safe_int(m.get("awayScore", {}).get("current", 0))
        if h_s + a_s > 4:
            return False, f"Çok gollü: {h_s + a_s}"

        return True, mn_int
    except Exception as e:
        return False, f"Filtre hatası: {e}"


# ─────────────────────────────────────────
# TREND (D)
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
# ODDS (F)
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
    """
    Defensive parser. Returns decimal odd or None.
    Supports:
      - "MS X.X ÜST", "İY X.X ÜST"
      - "KG VAR"
      - "KORNER X.X ÜST"
      - "MS 1Ç" / "MS 2Ç" (1x2)
    """
    if not isinstance(odds_data, dict):
        return None

    pick_u = str(pick).upper().strip()
    markets = odds_data.get("markets") or odds_data.get("odds") or []
    if not isinstance(markets, list):
        return None

    m_over = re.search(r"^(MS|İY)\s+(\d+(?:\.\d+)?)\s+ÜST$", pick_u)
    is_btts = (pick_u == "KG VAR")
    m_corner = re.search(r"^KORNER\s+(\d+(?:\.\d+)?)\s+ÜST$", pick_u)
    is_1x2 = pick_u in ("MS 1Ç", "MS 2Ç")

    for mk in markets:
        mname = str(mk.get("marketName") or mk.get("name") or "").lower()
        choices = mk.get("choices") or mk.get("outcomes") or mk.get("selections") or []
        if not isinstance(choices, list):
            continue

        if m_over:
            if not any(x in mname for x in ["total goals", "over/under", "goals over/under"]):
                continue
            line = m_over.group(2)
            for ch in choices:
                cname = str(ch.get("name") or ch.get("choiceName") or "").lower()
                odd = _find_decimal_odd(ch)
                if odd and ("over" in cname) and (line in cname):
                    return odd

        if is_btts:
            if not any(x in mname for x in ["both teams to score", "btts"]):
                continue
            for ch in choices:
                cname = str(ch.get("name") or "").lower()
                odd = _find_decimal_odd(ch)
                if odd and (("yes" in cname) or ("var" in cname)):
                    return odd

        if m_corner:
            if "corner" not in mname:
                continue
            line = m_corner.group(1)
            for ch in choices:
                cname = str(ch.get("name") or "").lower()
                odd = _find_decimal_odd(ch)
                if odd and ("over" in cname) and (line in cname):
                    return odd

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
# GROQ AI (fixed xg_line + 2 sentence clamp)
# ─────────────────────────────────────────
async def get_ai_insight(home, away, stats, pick, pressure, minute, score, xg=0.0):
    # fallback if no key
    if not GROQ_KEY:
        return _fallback_comment(home, away, stats, pick, pressure)

    global last_ai_requests
    now = time.time()
    last_ai_requests = [t for t in last_ai_requests if now - t < 60]
    if len(last_ai_requests) >= MAX_AI_PER_MINUTE:
        return _fallback_comment(home, away, stats, pick, pressure)
    last_ai_requests.append(now)

    # xg_line always defined
    hxg = safe_float(stats.get("home_xg", 0.0), 0.0) if isinstance(stats, dict) else 0.0
    axg = safe_float(stats.get("away_xg", 0.0), 0.0) if isinstance(stats, dict) else 0.0
    if hxg > 0 or axg > 0:
        xg_line = f"{hxg}-{axg}"
    else:
        xg_line = str(round(safe_float(xg, 0.0), 2))

    headers = {
        "Authorization": f"Bearer {GROQ_KEY}",
        "Content-Type": "application/json",
    }

    prompt = (
        "Rol: Profesyonel canlı maç analisti.\n"
        "Çıktı: TAM 2 cümle, Türkçe, kısa ve net.\n"
        "Kural: Sadece sayı/istatistik üzerinden konuş; abartı ve genel laf yok.\n"
        "Kural: 1. cümle baskıyı rakamlarla özetlesin. 2. cümle pick gerekçelendirsin.\n"
        "Kural: Her cümle max 18 kelime. Emoji yok. Markdown yok.\n\n"
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
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.15,
        "max_tokens": 90,
    }

    for attempt in range(3):
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                r = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    json=payload,
                    headers=headers,
                )
                if r.status_code == 200:
                    raw = r.json()["choices"][0]["message"]["content"]
                    clean = (
                        raw.replace("*", "")
                        .replace("_", "")
                        .replace("`", "")
                        .replace("[", "")
                        .replace("]", "")
                        .replace("<", "")
                        .replace(">", "")
                        .strip()
                    )

                    # force exactly 2 sentences
                    import re as _re
                    clean = _re.sub(r"\s+", " ", clean).strip()
                    parts = _re.split(r"(?<=[.!?])\s+", clean)
                    parts = [p.strip() for p in parts if p.strip()]
                    if len(parts) >= 2:
                        clean = parts[0] + " " + parts[1]
                    else:
                        return _fallback_comment(home, away, stats, pick, pressure)

                    if len(clean.split()) > 40:
                        return _fallback_comment(home, away, stats, pick, pressure)

                    return clean

                if r.status_code == 429:
                    await asyncio.sleep(6 * (attempt + 1))
                else:
                    await asyncio.sleep(3)

        except Exception as e:
            logger.error(f"Groq hatası: {e}")
            await asyncio.sleep(4)

    return _fallback_comment(home, away, stats, pick, pressure)


def _fallback_comment(home, away, stats, pick, pressure):
    hsot = stats.get("home_sot", 0); asot = stats.get("away_sot", 0)
    hsh  = stats.get("home_shots", 0); ash = stats.get("away_shots", 0)
    hdan = stats.get("home_dangerous", 0); adan = stats.get("away_dangerous", 0)
    hcor = stats.get("home_corners", 0); acor = stats.get("away_corners", 0)

    dom = f"{home}" if (hsot + hdan + hsh) >= (asot + adan + ash) else f"{away}"
    line1 = f"{dom} SOT {hsot}-{asot}, şut {hsh}-{ash}, tehlikeli {hdan}-{adan}, korner {hcor}-{acor} ile baskın."
    line2 = f"%{pressure} baskı seviyesi {pick} için değer üretir."
    return f"{line1} {line2}"


# ─────────────────────────────────────────
# COMMANDS
# ─────────────────────────────────────────
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    txt = (
        "<b>VIP Pro Trader Bot Aktif</b>\n\n"
        "<code>/canli</code> - Canlı maçları listeler\n"
        "<code>/kontrol</code> - Sistem denetimi yapar"
    )
    await update.message.reply_text(txt, parse_mode=ParseMode.HTML)


async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await context.bot.send_chat_action(
            chat_id=update.effective_chat.id,
            action=ChatAction.TYPING,
        )

        data = await fetch_api(LIVE_URL)
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
        tasks = []
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
            disable_web_page_preview=True,
        )
    except Exception as e:
        logger.exception("live_command error")
        await update.message.reply_text(f"/canli hata: {e}")


async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 <b>Sistem Denetleniyor...</b>", parse_mode=ParseMode.HTML)

    api_data = await fetch_api(LIVE_URL)
    gist_data = await manage_history("read")

    ai_test = await get_ai_insight(
        "TestA",
        "TestB",
        {
            "home_sot": 3, "away_sot": 1,
            "home_shots": 8, "away_shots": 3,
            "home_corners": 4, "away_corners": 1,
            "home_poss": 60, "away_poss": 40,
            "home_dangerous": 8, "away_dangerous": 3,
            "home_big_chances": 2, "away_big_chances": 0,
        },
        "MS 1.5 ÜST",
        70,
        55,
        "1-0",
        1.2,
    )

    delivery = "✅ OK"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Test")
    except:
        delivery = "❌ HATA"

    report = (
        "<b>BOT DENETİM RAPORU</b>\n\n"
        f"🌐 API: {'✅ OK' if api_data else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {'✅ OK' if isinstance(ai_test, str) and len(ai_test) > 10 else '❌ HATA'}\n"
        f"📩 İletim: {delivery}\n"
        f"⚽ Canlı Maç: {len(api_data.get('events', []))}\n"
        f"📊 Kayıtlı Sinyal: {len(gist_data) if isinstance(gist_data, list) else 0}\n\n"
        "<i>Sistem aktif.</i>"
    )
    await msg.edit_text(report, parse_mode=ParseMode.HTML)


# ─────────────────────────────────────────
# RESULT TRACKER
# ─────────────────────────────────────────
async def result_tracker(app):
    while True:
        try:
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []
            updated = False

            for sig in history[-30:]:
                if (sig.get("status") == "pending" and time.time() - sig.get("timestamp", 0) > 3600):
                    r = await fetch_api(MATCH_URL.format(sig["id"]))
                    ev = r.get("event", {})
                    if ev.get("status", {}).get("type") == "finished":
                        hs = safe_int(ev.get("homeScore", {}).get("current", 0))
                        as_ = safe_int(ev.get("awayScore", {}).get("current", 0))
                        is_win = (hs + as_) > sig.get("start_total", 0)
                        sig["status"] = "WIN ✅" if is_win else "LOSS ❌"
                        sig["final_score"] = f"{hs}-{as_}"
                        updated = True

            if updated:
                await manage_history("write", history)
                logger.info("📊 Sonuçlar güncellendi.")
        except Exception as e:
            logger.error(f"Result tracker: {e}")

        await asyncio.sleep(600)


# ─────────────────────────────────────────
# SIGNAL MONITOR
# ─────────────────────────────────────────
async def signal_monitor(app):
    logger.info("🚀 Sinyal monitörü başladı.")
    while True:
        try:
            data = await fetch_api(LIVE_URL)
            events = data.get("events", [])
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []

            sent_ids = {str(x["id"]) for x in history if "id" in x}
            candidates = []

            for m in events:
                ok, result = should_check_match(m, sent_ids)
                if ok:
                    candidates.append((m, result))

            logger.info(f"📊 {len(events)} maç → {len(candidates)} aday → {len(history)} sinyal kayıtlı")

            for m, mn_int in candidates:
                try:
                    mid = str(m.get("id", ""))
                    stats = await get_stats(mid)
                    if not stats or not stats.get("has"):
                        continue

                    # override minute from event details if available
                    if stats.get("minute_int", 0) > 0:
                        mn_int = stats["minute_int"]

                    trend = compute_trend(mid, stats, mn_int)

                    res = brain.analyze_advanced(m, stats, mn_int, trend=trend)
                    if not res.get("is_signal"):
                        logger.info(f"⏭ {res.get('reason', '?')}")
                        continue

                    # odds: if no odds data -> skip the match (as you requested)
                    odds_data = await fetch_odds_event(mid)
                    if not odds_data:
                        logger.info("⏭ Oran yok, maç es geçildi.")
                        continue

                    # try multiple picks and select best VALUE
                    pick_probs = res.get("pick_probs", {}) if isinstance(res.get("pick_probs"), dict) else {}
                    candidate_picks = [res["pick"]] + [p[0] for p in res.get("alt", []) if p and p[0] != res["pick"]]
                    candidate_picks = candidate_picks[:5]

                    best_choice = None
                    for pck in candidate_picks:
                        odd = extract_odds_from_event(odds_data, pck)
                        if not odd:
                            continue
                        if odd < brain.MIN_ODDS:
                            continue

                        p_model = pick_probs.get(pck, res.get("prob", 60) / 100.0)
                        p_model = max(0.01, min(0.95, float(p_model)))
                        implied = 1.0 / float(odd)
                        value = p_model - implied

                        # value threshold
                        if value < 0.03:
                            continue

                        if (best_choice is None) or (value > best_choice["value"]):
                            best_choice = {"pick": pck, "odds": odd, "value": value, "model_prob": p_model}

                    if not best_choice:
                        logger.info("⏭ Uygun oran/value bulunamadı.")
                        continue

                    # update choice
                    res["pick"] = best_choice["pick"]
                    res["odds"] = round(best_choice["odds"], 2)
                    res["value"] = round(best_choice["value"], 3)
                    # update prob to chosen pick prob if available
                    res["prob"] = int(best_choice["model_prob"] * 100)

                    home_name = m.get("homeTeam", {}).get("name", "?")
                    away_name = m.get("awayTeam", {}).get("name", "?")
                    league = (m.get("tournament", {}) or {}).get("name", "Bilinmiyor")

                    ai_msg = await get_ai_insight(
                        home_name,
                        away_name,
                        stats,
                        res["pick"],
                        res["pressure"],
                        mn_int,
                        res["score"],
                        res.get("xg", 0.0),
                    )

                    # build message (HTML, safe)
                    h_esc = html.escape(home_name or "?")
                    a_esc = html.escape(away_name or "?")
                    l_esc = html.escape(league or "Bilinmiyor")
                    ai_esc = html.escape(ai_msg or "")

                    # pressure bar
                    bar_val = max(0, min(100, safe_int(res.get("pressure", 0))))
                    bar = ("🟩" * (bar_val // 10)) + ("⬜" * (10 - bar_val // 10))

                    period_emoji = "1️⃣" if res.get("period") == "1. YARI" else "2️⃣"
                    confirms = res.get("confirmations", [])
                    conf_txt = " · ".join(confirms[:3]) if confirms else "doğrulama yok"

                    # xG line (stats has real xg maybe)
                    h_xg = stats.get("home_xg", 0.0)
                    a_xg = stats.get("away_xg", 0.0)
                    xg_line = f"{h_xg} - {a_xg} (Sofascore)" if (h_xg or a_xg) else f"{res.get('total_xg', res.get('xg', 0.0))} (tahmini)"

                    odds_value_line = f"🎲 <b>Oran:</b> <code>{res.get('odds')}</code> | 📈 <b>Value:</b> <code>{res.get('value')}</code>\n"
                    pois_line = ""
                    if res.get("poisson_prob") is not None:
                        pois_line = f"🎯 <b>Poisson:</b> <code>{res.get('poisson_prob')}%</code>\n"

                    alt_picks = [p for p in res.get("alt", []) if p and p[0] != res["pick"]]
                    alt_txt = ""
                    for p in alt_picks[:2]:
                        alt_txt += f"• {html.escape(str(p[0]))} (Risk: {html.escape(str(p[2]))})\n"
                    alt_section = f"\n💡 <b>ALTERNATİF</b>\n<code>{alt_txt}</code>" if alt_txt else ""

                    txt = (
                        f"📡 <b>SİNYAL</b> | <code>{time.strftime('%H:%M')}</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚽ <b>{h_esc}</b> <code>{html.escape(res['score'])}</code> <b>{a_esc}</b>\n"
                        f"🏆 <i>{l_esc}</i>\n"
                        f"⏱ <code>{mn_int}'</code> {period_emoji} {html.escape(res.get('period',''))}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 <b>TAHMİN:</b> <code>{html.escape(res['pick'])}</code>\n"
                        f"📊 <b>Güven:</b> {html.escape(res.get('confidence',''))} <code>{res.get('prob')}%</code>\n"
                        f"⚠️ <b>Risk:</b> <code>{html.escape(res.get('risk',''))}</code>\n"
                        f"{odds_value_line}"
                        f"{pois_line}"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 <b>İSTATİSTİKLER</b>\n"
                        f"┌ 🥅 SOT: <code>{stats['home_sot']} - {stats['away_sot']}</code>\n"
                        f"├ ⚡ Şut: <code>{stats['home_shots']} - {stats['away_shots']}</code>\n"
                        f"├ 🚩 Korner: <code>{stats['home_corners']} - {stats['away_corners']}</code>\n"
                        f"├ 🎮 Poss: <code>%{stats['home_poss']} - %{stats['away_poss']}</code>\n"
                        f"├ 🔥 Tehlikeli: <code>{stats['home_dangerous']} - {stats['away_dangerous']}</code>\n"
                        f"├ 💥 Büyük Fırsat: <code>{stats['home_big_chances']} - {stats['away_big_chances']}</code>\n"
                        f"└ 🧤 Kurtarış: <code>{stats['home_saves']} - {stats['away_saves']}</code>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔥 <b>BASKI:</b> {bar} <code>%{res.get('pressure')}</code>\n"
                        f"📐 <b>xG:</b> <code>{html.escape(str(xg_line))}</code>\n"
                        f"⚡ <b>Momentum:</b> <code>{res.get('momentum')}</code>\n"
                        f"👊 <b>Üstün:</b> {html.escape(res.get('team',''))}\n"
                        f"✅ <i>{html.escape(conf_txt)}</i>\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🧠 <b>ANALİZ:</b> <i>{ai_esc}</i>\n"
                        f"{alt_section}"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💎 <i>VIP Pro Trader</i>"
                    )

                    await app.bot.send_message(
                        chat_id=CHAT_ID,
                        text=txt,
                        parse_mode=ParseMode.HTML,
                        disable_web_page_preview=True,
                    )

                    history.append({
                        "id": mid,
                        "timestamp": time.time(),
                        "status": "pending",
                        "start_total": res.get("total_score", 0),
                        "match": f"{home_name} vs {away_name}",
                        "pick": res["pick"],
                        "odds": res.get("odds"),
                        "value": res.get("value"),
                        "prob": res.get("prob"),
                    })
                    await manage_history("write", history)
                    sent_ids.add(mid)

                    logger.info(f"✅ Sinyal: {res['pick']} @ {res.get('odds')} value={res.get('value')}")
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Maç hatası ({m.get('id','')}): {e}")
                    continue

        except Exception as e:
            logger.error(f"Monitör hatası: {e}")

        await asyncio.sleep(180)


# ─────────────────────────────────────────
# POST INIT TASKS
# ─────────────────────────────────────────
async def post_init(app):
    asyncio.create_task(result_tracker(app))
    asyncio.create_task(signal_monitor(app))
    logger.info("✅ Görevler başladı.")


# ─────────────────────────────────────────
# GLOBAL ERROR HANDLER
# ─────────────────────────────────────────
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

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))

    logger.info("✅ Bot hazır!")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )
