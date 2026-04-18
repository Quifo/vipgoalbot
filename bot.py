import os
import asyncio
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
brain = BettingBrain()

URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

async def fetch_live_matches():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
        try:
            response = await client.get(URL, headers=headers)
            if response.status_code == 200:
                return response.json().get('events', [])
            return []
        except Exception as e:
            print(f"⚠️ Veri çekme hatası: {e}")
            return []

def get_match_minute(match):
    """Dakika bilgisini güvenli bir şekilde çeker."""
    status = match.get('status', {})
    description = status.get('description', '')
    
    if description == 'HT': return "İY"
    if description == 'FT': return "MS"
    
    # SofaScore'da canlı dakika 'elapsed' içindedir
    minute = status.get('elapsed')
    if minute is not None:
        return f"{minute}'"
    
    return "Canlı"

# --- KOMUTLAR ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *VIP Bahis Algoritması Aktif!*\n\n"
        "/canli - Canlı maçları listeler",
        parse_mode=ParseMode.MARKDOWN
    )

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    matches = await fetch_live_matches()
    
    if not matches:
        await update.message.reply_text("📭 Şu an canlı maç bulunamadı.")
        return

    text = "⚽ *GÜNCEL CANLI MAÇLAR*\n\n"
    
    for m in matches[:25]:
        # 'shortName' yoksa normal 'name' kullan, o da yoksa 'Bilinmiyor' yaz (Çökmeyi önler)
        home = m.get('homeTeam', {}).get('shortName') or m.get('homeTeam', {}).get('name') or "Ev Sahibi"
        away = m.get('awayTeam', {}).get('shortName') or m.get('awayTeam', {}).get('name') or "Deplasman"
        
        score_h = m.get('homeScore', {}).get('current', 0)
        score_a = m.get('awayScore', {}).get('current', 0)
        
        minute_display = get_match_minute(m)
        
        text += f"⏱ `{minute_display}` | {home} *{score_h}-{score_a}* {away}\n"
    
    text += "\n🔍 _VIP Sinyaller kriterlere uyunca otomatik gönderilir._"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# --- ARKA PLAN SİNYAL DÖNGÜSÜ ---

async def signal_monitor(application):
    print("🚀 Sinyal Monitörü Çalışıyor...")
    sent_signals = set()

    while True:
        matches = await fetch_live_matches()
        for match in matches:
            m_id = match.get('id')
            if m_id and m_id not in sent_signals:
                decision = brain.analyze_match(match)
                if decision.get("is_signal"):
                    minute_display = get_match_minute(match)
                    # Takım isimlerini sinyal için de güvenli alalım
                    h_full = match.get('homeTeam', {}).get('name', 'Ev Sahibi')
                    a_full = match.get('awayTeam', {}).get('name', 'Deplasman')
                    
                    text = (
                        f"🚨 *VIP SİNYAL* 🚨\n\n"
                        f"⚽ *MAÇ:* {h_full} vs {a_full}\n"
                        f"🎯 *TAHMİN:* {decision['pick']}\n"
                        f"📊 *OLASILIK:* %{decision['prob']:.0f}\n"
                        f"💰 *ORAN:* {decision['odds']:.2f}\n"
                        f"📈 *VALUE:* %{decision['value']:.1f}\n"
                        f"🔥 *GÜVEN:* {decision['confidence']}\n\n"
                        f"⏱ *Dakika:* {minute_display}\n\n"
                        f"💸 *STAKE:* {decision['stake']}/10"
                    )
                    try:
                        await application.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
                        sent_signals.add(m_id)
                    except Exception as e:
                        print(f"Sinyal gönderim hatası: {e}")
        
        if len(sent_signals) > 1000: sent_signals.clear()
        await asyncio.sleep(120)

async def post_init(application):
    asyncio.create_task(signal_monitor(application))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("live", live_command))

    app.run_polling()
