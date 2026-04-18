import os
import asyncio
import httpx
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# Ayarlar
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
brain = BettingBrain()

# SofaScore Canlı Veri URL'si
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

async def fetch_live_matches():
    """SofaScore'dan canlı maçları güvenli bir şekilde çeker."""
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
    """Dakika bilgisini doğru yerden çeker (HT ve 0 sorununu çözer)."""
    status = match.get('status', {})
    description = status.get('description', '')
    
    if description == 'HT':
        return "İY" # İlk Yarı Sonu
    
    # SofaScore'da bazen 'elapsed' bazen 'lastPeriod' kullanılır
    minute = status.get('elapsed', 0)
    return f"{minute}'"

# --- KOMUTLAR ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *VIP Bahis Algoritması Aktif!*\n\n"
        "/canli - Canlı maçları listeler\n"
        "/live - Canlı maçları listeler",
        parse_mode=ParseMode.MARKDOWN
    )

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_chat_action("typing")
    matches = await fetch_live_matches()
    
    if not matches:
        await update.message.reply_text("📭 Şu an canlı maç bulunamadı.")
        return

    text = "⚽ *GÜNCEL CANLI MAÇLAR*\n\n"
    # Sadece ilk 25 maç
    for m in matches[:25]:
        home = m['homeTeam']['shortName']
        away = m['awayTeam']['shortName']
        score_h = m.get('homeScore', {}).get('current', 0)
        score_a = m.get('awayScore', {}).get('current', 0)
        
        # Dakika düzeltmesi burada uygulanıyor
        minute_display = get_match_minute(m)
        
        text += f"⏱ `{minute_display}` | {home} *{score_h}-{score_a}* {away}\n"
    
    text += "\n🔍 _VIP Sinyaller kriterlere uyunca otomatik gönderilir._"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# --- ARKA PLAN SİNYAL DÖNGÜSÜ ---

async def signal_monitor(application):
    """7/24 maçları tarayan ve sinyal atan döngü."""
    print("🚀 Sinyal Monitörü Çalışıyor...")
    sent_signals = set()

    while True:
        matches = await fetch_live_matches()
        for match in matches:
            m_id = match['id']
            if m_id not in sent_signals:
                decision = brain.analyze_match(match)
                if decision["is_signal"]:
                    minute_display = get_match_minute(match)
                    text = (
                        f"🚨 *VIP SİNYAL* 🚨\n\n"
                        f"⚽ *MAÇ:* {match['homeTeam']['name']} vs {match['awayTeam']['name']}\n"
                        f"🎯 *TAHMİN:* {decision['pick']}\n"
                        f"📊 *OLASILIK:* %{decision['prob']:.0f}\n"
                        f"💰 *ORAN:* {decision['odds']:.2f}\n"
                        f"📈 *VALUE:* %{decision['value']:.1f}\n"
                        f"🔥 *GÜVEN:* {decision['confidence']}\n\n"
                        f"⏱ *Dakika:* {minute_display}\n\n"
                        f"💸 *STAKE:* {decision['stake']}/10"
                    )
                    await application.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
                    sent_signals.add(m_id)
        
        if len(sent_signals) > 1000: sent_signals.clear()
        await asyncio.sleep(120)

# --- BOT BAŞLATMA AYARI ---

async def post_init(application):
    """Bot başladığında arka plan döngüsünü de başlatır."""
    asyncio.create_task(signal_monitor(application))

if __name__ == "__main__":
    # Railway ve loop hatasını çözen modern başlatma yöntemi
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("live", live_command))

    print("✅ Bot ve Arka Plan Tarayıcı Hazır.")
    app.run_polling()
