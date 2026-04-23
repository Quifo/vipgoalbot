import os, asyncio, httpx, json, time, logging, random
import html
from datetime import datetime
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

# FOTMOB Headers (Basit ama yeterli)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.fotmob.com/",
    "Origin": "https://www.fotmob.com",
    "X-Requested-With": "XMLHttpRequest"
}

GIST_HEADERS = {
    "Authorization": f"token {GITHUB_TOKEN}",
    "Accept":        "application/vnd.github.v3+json"
}

# FOTMOB URL'ler
MATCH_URL = "https://www.fotmob.com/api/matchDetails?matchId={}"

last_ai_requests  = []
MAX_AI_PER_MINUTE = 20

def safe_int(val, default=0):
    try:
        if val is None or val == '' or val == '-' or val == 'None':
            return default
        return int(float(str(val).replace('%', '').strip()))
    except:
        return default

def safe_float(val, default=0.0):
    try:
        if val is None or val == '' or val == '-' or val == 'None':
            return default
        return float(str(val).replace('%', '').strip())
    except:
        return default

async def fetch_api(url, retries=3):
    """Fotmob için retry mekanizmalı fetch"""
    for attempt in range(retries):
        try:
            await asyncio.sleep(random.uniform(0.5, 1.5))
            
            async with httpx.AsyncClient(
                timeout=30.0, 
                follow_redirects=True, 
                headers=HEADERS
            ) as client:
                r = await client.get(url)
                
                if r.status_code == 200:
                    return r.json()
                elif r.status_code == 429:
                    logger.warning(f"429 rate limit, bekleme: {2 ** attempt}s")
                    await asyncio.sleep(2 ** attempt)
                    continue
                else:
                    logger.error(f"HTTP {r.status_code}: {url}")
                    if attempt < retries - 1:
                        continue
                    return {}
                    
        except Exception as e:
            logger.error(f"Fetch hatası ({url}): {e}")
            if attempt == retries - 1:
                return {}
            await asyncio.sleep(1)
    
    return {}

def get_live_matches(data):
    """Fotmob'dan canlı maçları filtrele"""
    if not data:
        return []
    
    matches = []
    leagues = data.get("leagues", [])
    
    for league in leagues:
        league_matches = league.get("matches", [])
        for match in league_matches:
            status = match.get("status", {})
            # Sadece canlı ve başlamış maçlar
            if status.get("started") and not status.get("finished"):
                match["leagueName"] = league.get("name", "Bilinmiyor")
                matches.append(match)
    
    return matches

def get_fotmob_minute(match_data):
    """Fotmob'dan dakika çıkarımı"""
    try:
        status = match_data.get("status", {})
        minute = safe_int(status.get("minute"), 0)
        
        if status.get("finished"):
            return "MS"
        if status.get("halfTimeBreak"):
            return "İY"
        if not status.get("started"):
            return "0'"
            
        return f"{minute}'"
    except:
        return "0'"

def should_check_match(match, sent_ids):
    """Fotmob formatına göre filtreleme"""
    try:
        mid = str(match.get("id", ""))
        minute_str = get_fotmob_minute(match)
        
        if mid in sent_ids:
            return False, "Zaten gönderildi"
        if minute_str in ("İY", "MS", "0'"):
            return False, f"Geçersiz dakika: {minute_str}"
            
        minute_num = safe_int(minute_str.replace("'", ""), 0)
        if not (20 <= minute_num <= 85):
            return False, f"Dakika dışı: {minute_num}"
            
        home_score = safe_int(match.get("home", {}).get("score"), 0)
        away_score = safe_int(match.get("away", {}).get("score"), 0)
        
        if home_score + away_score > 4:
            return False, f"Çok gollü: {home_score + away_score}"
            
        if not match.get("leagueName"):
            return False, "Lig bilgisi yok"
            
        return True, minute_num
    except Exception as e:
        return False, f"Filtre hatası: {e}"

async def get_match_stats(match_id):
    """Fotmob'dan detaylı istatistik çekme"""
    url = MATCH_URL.format(match_id)
    data = await fetch_api(url)
    
    if not data or not data.get("content"):
        return None
        
    try:
        content = data["content"]
        stats = {
            'home_sot': 0,       'away_sot': 0,
            'home_shots': 0,     'away_shots': 0,
            'home_corners': 0,   'away_corners': 0,
            'home_poss': 50,     'away_poss': 50,
            'home_xg': 0.0,      'away_xg': 0.0,
            'home_attacks': 0,   'away_attacks': 0,
            'home_dangerous': 0, 'away_dangerous': 0,
            'home_saves': 0,     'away_saves': 0,
            'home_big_chances': 0, 'away_big_chances': 0,
            'has': False
        }
        
        # Maç durumu
        match_facts = content.get("matchFacts", {})
        match_status = match_facts.get("matchStatus", {})
        minute = safe_int(match_status.get("minute"), 0)
        stats['minute_int'] = minute
        stats['minute_str'] = f"{minute}'" if minute > 0 else "0'"
        
        # İstatistikler
        stats_data = content.get("stats", {})
        periods = stats_data.get("periods", [])
        
        if not periods:
            return None
            
        # Toplam istatistikleri al (son period veya "All")
        target_period = None
        for period in periods:
            if period.get("period") in ["ALL", "FullMatch", "MatchTotal"]:
                target_period = period
                break
        
        if not target_period and periods:
            target_period = periods[-1]
            
        if not target_period:
            return None
            
        # Stats parse et
        stats_list = target_period.get("stats", [])
        for stat_group in stats_list:
            if not isinstance(stat_group, dict):
                continue
                
            items = stat_group.get("stats", [])
            for item in items:
                if not isinstance(item, dict):
                    continue
                    
                key = item.get("key", "").lower()
                home_val = item.get("home") or item.get("homeValue") or 0
                away_val = item.get("away") or item.get("awayValue") or 0
                
                if key in ["shots_on_target", "sot", "shotson", "shots_on"]:
                    stats['home_sot'] = safe_int(home_val)
                    stats['away_sot'] = safe_int(away_val)
                    stats['has'] = True
                elif key in ["total_shots", "shots", "attempts", "totalshots"]:
                    stats['home_shots'] = safe_int(home_val)
                    stats['away_shots'] = safe_int(away_val)
                    stats['has'] = True
                elif key in ["corners", "corner_kicks", "cornerkicks"]:
                    stats['home_corners'] = safe_int(home_val)
                    stats['away_corners'] = safe_int(away_val)
                elif key in ["possession", "poss", "ball_possession"]:
                    stats['home_poss'] = safe_int(home_val, 50)
                    stats['away_poss'] = safe_int(away_val, 50)
                elif key in ["saves", "goalkeeper_saves"]:
                    stats['home_saves'] = safe_int(home_val)
                    stats['away_saves'] = safe_int(away_val)
                elif key in ["big_chances", "bigchances", "big_chance"]:
                    stats['home_big_chances'] = safe_int(home_val)
                    stats['away_big_chances'] = safe_int(away_val)
                elif key in ["dangerous_attacks", "dangerousattacks"]:
                    stats['home_dangerous'] = safe_int(home_val)
                    stats['away_dangerous'] = safe_int(away_val)
        
        # xG Hesapla
        shotmap = content.get("shotmap", [])
        if shotmap:
            home_team_id = match_facts.get("homeTeam", {}).get("id")
            away_team_id = match_facts.get("awayTeam", {}).get("id")
            
            home_xg = sum(safe_float(shot.get("expectedGoals"), 0) 
                         for shot in shotmap 
                         if shot.get("teamId") == home_team_id)
            away_xg = sum(safe_float(shot.get("expectedGoals"), 0) 
                         for shot in shotmap 
                         if shot.get("teamId") == away_team_id)
            
            stats['home_xg'] = round(home_xg, 2)
            stats['away_xg'] = round(away_xg, 2)
        else:
            home_xg_val = match_facts.get("homeTeam", {}).get("expectedGoals")
            away_xg_val = match_facts.get("awayTeam", {}).get("expectedGoals")
            if home_xg_val is not None:
                stats['home_xg'] = safe_float(home_xg_val, 0.0)
            if away_xg_val is not None:
                stats['away_xg'] = safe_float(away_xg_val, 0.0)
                    
        return stats if stats['has'] else None
        
    except Exception as e:
        logger.error(f"Stats parse hatası ({match_id}): {e}")
        return None

async def manage_history(mode="read", data=None):
    """Gist yönetimi"""
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

async def get_ai_insight(home, away, stats, pick, pressure, minute, score, xg=0.0, pick_type="ust"):
    """AI yorum"""
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
    h_danger = safe_int(stats.get("home_dangerous", 0))
    h_poss = safe_int(stats.get("home_poss", 50))
    
    prompt = f"""Profesyonel spor analisti. 2 kısa cümle, maksimum 150 karakter.
MAÇ: {home} vs {away} | {minute}' | {score}
BAHİS: {pick}
VERİ: {h_sot} isabetli şut, %{h_poss} baskı, {h_danger} tehlikeli atak.
KURAL: Tam 2 cümle. Kesin dil, emoji yok.
ANALİZ:"""

    payload = {
        "model": "llama-3.1-8b-instant",
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.2,
        "max_tokens": 300
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
                    clean = (raw.replace('*', '').replace('_', '').replace('`', '')
                               .replace('[', '').replace(']', '').replace('"', '')
                               .replace("'", "").strip())
                    
                    if len(clean) > 150:
                        clean = clean[:147] + "..."
                    if len(clean) < 20:
                        return _fallback_comment(home, stats, pick, pressure, pick_type)
                    
                    logger.info(f"AI → {clean[:60]}...")
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
    templates = {
        'iy': [f"{h_sot} isabetli şutla baskı kuruluyor. Gol yakın.", f"İlk yarı temposu yüksek."],
        'ms': [f"Maçın ikinci yarısında baskı sürüyor. Gol potansiyeli var.", f"İstatistikler üst bahisini destekliyor."],
        'kg': [f"İki taraf da açık oynuyor. Karşılıklı gol olabilir.", f"Defans zafiyetleri KG ihtimalini artırıyor."],
        'default': [f"İstatistiksel veriler {pick} lehine.", f"Baskı skoru ({pressure}%) destekliyor."]
    }
    category = pick_type if pick_type in templates else 'default'
    return random.choice(templates[category])

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *VIP Pro Trader Bot Aktif!*\n\n"
        "/canli - Canlı maçları listeler\n"
        "/kontrol - Sistem denetimi yapar",
        parse_mode=ParseMode.MARKDOWN
    )

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    
    current_date = datetime.now().strftime("%Y%m%d")
    live_url = f"https://www.fotmob.com/api/matches?date={current_date}"
    
    data = await fetch_api(live_url)
    matches = get_live_matches(data)
    
    if not matches:
        await update.message.reply_text("📭 Şu an canlı maç yok.")
        return

    lines = ["⚽ <b>CANLI MAÇLAR</b>", ""]
    shown = 0
    
    for match in matches[:20]:
        try:
            mn = get_fotmob_minute(match)
            if mn in ("İY", "MS", "0'"):
                continue
                
            h = html.escape(match.get("home", {}).get("name", "?"))
            a = html.escape(match.get("away", {}).get("name", "?"))
            sh = safe_int(match.get("home", {}).get("score"), 0)
            sa = safe_int(match.get("away", {}).get("score"), 0)
            
            lines.append(f"⏱ <code>{mn}</code> | {h} <b>{sh}-{sa}</b> {a}")
            shown += 1
        except:
            continue

    if shown == 0:
        await update.message.reply_text("📭 Şu an listelenecek canlı maç yok.")
        return

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

async def control_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("🔎 *Sistem Denetleniyor...*", parse_mode=ParseMode.MARKDOWN)
    
    current_date = datetime.now().strftime("%Y%m%d")
    live_url = f"https://www.fotmob.com/api/matches?date={current_date}"
    
    api_data = await fetch_api(live_url)
    matches = get_live_matches(api_data) if api_data else []
    gist_data = await manage_history("read")
    
    ai_test = await get_ai_insight("TestA", "TestB", 
        {'home_sot': 3, 'away_sot': 1, 'home_shots': 8, 'away_shots': 3}, 
        "MS 1.5 ÜST", 70, 55, "1-0", 1.2, "ms")
    
    delivery = "✅ OK"
    try:
        await context.bot.send_message(chat_id=CHAT_ID, text="🧪 Test")
    except:
        delivery = "❌ HATA"

    report = (
        f"🛡 *BOT DENETİM RAPORU*\n\n"
        f"🌐 Fotmob API: {'✅ OK' if matches else '❌ HATA'}\n"
        f"💾 Gist: {'✅ OK' if isinstance(gist_data, list) else '❌ HATA'}\n"
        f"🧠 AI: {'✅ OK' if len(ai_test) > 10 else '❌ HATA'}\n"
        f"📩 İletim: {delivery}\n"
        f"⚽ Canlı Maç: {len(matches)}\n"
        f"📊 Kayıtlı Sinyal: {len(gist_data) if isinstance(gist_data, list) else 0}\n\n"
        f"🚀 _Sistem aktif!_"
    )
    await msg.edit_text(report, parse_mode=ParseMode.MARKDOWN)

async def result_tracker(app):
    while True:
        try:
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []
            updated = False

            for sig in history[-20:]:
                if (sig.get('status') == 'pending' and time.time() - sig.get('timestamp', 0) > 3600):
                    r = await fetch_api(MATCH_URL.format(sig['id']))
                    content = r.get("content", {}) if r else {}
                    status = content.get("matchFacts", {}).get("matchStatus", {})
                    
                    if status.get("finished"):
                        home_score = safe_int(content.get("matchFacts", {}).get("homeTeam", {}).get("score"), 0)
                        away_score = safe_int(content.get("matchFacts", {}).get("awayTeam", {}).get("score"), 0)
                        is_win = (home_score + away_score) > sig.get('start_total', 0)
                        sig['status'] = 'WIN ✅' if is_win else 'LOSS ❌'
                        sig['final_score'] = f"{home_score}-{away_score}"
                        updated = True

            if updated:
                await manage_history("write", history)
                logger.info("📊 Sonuçlar güncellendi.")
        except Exception as e:
            logger.error(f"Result tracker: {e}")
        await asyncio.sleep(600)

async def signal_monitor(app):
    logger.info("🚀 Sinyal monitörü başladı (Fotmob).")
    while True:
        try:
            current_date = datetime.now().strftime("%Y%m%d")
            live_url = f"https://www.fotmob.com/api/matches?date={current_date}"
            
            data = await fetch_api(live_url)
            matches = get_live_matches(data)
            
            if not matches:
                logger.info("Canlı maç bulunamadı")
                await asyncio.sleep(180)
                continue
                
            history = await manage_history("read")
            if not isinstance(history, list):
                history = []

            sent_ids = {str(x['id']) for x in history}
            candidates = []

            for m in matches:
                ok, result = should_check_match(m, sent_ids)
                if ok:
                    candidates.append((m, result))

            logger.info(f"📊 {len(matches)} maç → {len(candidates)} aday")

            for m, mn_int in candidates:
                try:
                    mid = str(m.get("id", ""))
                    stats = await get_match_stats(mid)
                    
                    if not stats or not stats.get('has'):
                        continue

                    home_data = m.get("home", {})
                    away_data = m.get("away", {})
                    
                    match_obj = {
                        'id': mid,
                        'homeTeam': {'name': home_data.get('name', '?')},
                        'awayTeam': {'name': away_data.get('name', '?')},
                        'homeScore': {'current': safe_int(home_data.get('score'), 0)},
                        'awayScore': {'current': safe_int(away_data.get('score'), 0)},
                        'tournament': {'name': m.get('leagueName', 'Bilinmiyor')}
                    }
                    
                    if stats.get('minute_int', 0) > 0:
                        mn_int = stats['minute_int']

                    res = brain.analyze_advanced(match_obj, stats, mn_int)

                    if not res.get('is_signal'):
                        logger.info(f"⏭ {res.get('reason', '?')}")
                        continue

                    home_name = match_obj['homeTeam']['name']
                    away_name = match_obj['awayTeam']['name']
                    league = match_obj['tournament']['name']
                    xg_val = res.get('xg', 0.0)
                    pick_type = res.get('pick_type', 'ust')

                    logger.info(f"🔍 Sinyal: {home_name} vs {away_name} | {mn_int}' | {res['pick']}")

                    ai_msg = await get_ai_insight(
                        home_name, away_name, stats, res['pick'], 
                        res['pressure'], mn_int, res['score'], xg_val, pick_type
                    )

                    alt_picks = res.get('alt', [])
                    alt_txt = ""
                    if alt_picks:
                        alt_lines = []
                        for p in alt_picks[:2]:
                            bet_name = p[0]
                            alt_lines.append(f"  - {bet_name}")
                        if alt_lines:
                            alt_txt = "\n💡 *Alternatif Tercihler:*\n" + "\n".join(alt_lines)

                    period_emoji = "2️⃣" if res['period'] == "2. YARI" else "1️⃣"
                    
                    h_sot = stats.get('home_sot', 0)
                    a_sot = stats.get('away_sot', 0)
                    h_shots = stats.get('home_shots', 0)
                    a_shots = stats.get('away_shots', 0)
                    h_corners = stats.get('home_corners', 0)
                    a_corners = stats.get('away_corners', 0)
                    h_poss = stats.get('home_poss', 50)
                    a_poss = stats.get('away_poss', 50)
                    h_big = stats.get('home_big_chances', 0)
                    a_big = stats.get('away_big_chances', 0)
                    h_saves = stats.get('home_saves', 0)
                    a_saves = stats.get('away_saves', 0)

                    txt = (
                        f"📡 *SİNYAL*\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"⚽️ *{home_name}* {res['score']} *{away_name}*\n"
                        f"🏆 _{league}_\n"
                        f"⏱️ `{mn_int}'` {period_emoji} {res['period']}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🎯 *Ana Bahis:* `{res['pick']}`\n"
                        f"📊 *Güven:* {res['confidence']} `{res['prob']}%`\n"
                        f"⚠️ *Risk:* `{res['risk']}`"
                        f"{alt_txt}\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📈 *İSTATİSTİKLER*\n"
                        f"┌ 🥅 İsabetli Şut: `{h_sot} - {a_sot}`\n"
                        f"├ ⚡️ Toplam Şut:   `{h_shots} - {a_shots}`\n"
                        f"├ 🚩 Korner:      `{h_corners} - {a_corners}`\n"
                        f"├ 🎮 Hakimiyet:   `%{h_poss} - %{a_poss}`\n"
                        f"├ 💥 Büyük Fırsat: `{h_big} - {a_big}`\n"
                        f"└ 🧤 Kurtarış:    `{h_saves} - {a_saves}`\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🧠 *ANALİZ:* _{ai_msg}_\n"
                        f"━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"💎 *VIP Pro Trader*"
                    )

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
                    sent_ids.add(mid)
                    logger.info(f"✅ Sinyal: {res['pick']}")
                    await asyncio.sleep(2)

                except Exception as e:
                    logger.error(f"Maç hatası: {e}")
                    continue

        except Exception as e:
            logger.error(f"Monitör hatası: {e}")

        await asyncio.sleep(180)

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
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("kontrol", control_command))

    logger.info("✅ Bot hazır!")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)
