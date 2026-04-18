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
    """SofaScore'dan canlı maçları çeker."""
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
            print(f"⚠️ Veri Hatası: {e}")
            return []

# --- KOMUTLAR ---

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start komutu karşılaması."""
    await update.message.reply_text(
        "🤖 *VIP Bahis Algoritması Aktif!*\n\n"
        "Sistem 7/24 canlı maçları tarar ve elit fırsatları sana gönderir.\n\n"
        "📜 *Komutlar:*\n"
        "/canli - Şu an oynanan tüm canlı maçları listeler.\n"
        "/yardim - Botun çalışma mantığını anlatır.",
        parse_mode=ParseMode.MARKDOWN
    )

async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/canli komutu: O anki tüm maçları listeler."""
    await update.message.reply_chat_action("typing")
    matches = await fetch_live_matches()
    
    if not matches:
        await update.message.reply_text("📭 Şu an aktif canlı maç bulunamadı.")
        return

    text = "⚽ *GÜNCEL CANLI MAÇLAR*\n\n"
    # Sadece ilk 20 maçı gösteriyoruz (Mesaj boyutu sınırı için)
    for m in matches[:20]:
        home = m['homeTeam']['shortName']
        away = m['awayTeam']['shortName']
        score_h = m.get('homeScore', {}).get('current', 0)
        score_a = m.get('awayScore', {}).get('current', 0)
        minute = m.get('status', {}).get('elapsed', 0)
        
        text += f"⏱ `{minute}'` | {home} *{score_h}-{score_a}* {away}\n"
    
    text += "\n🔍 _VIP Sinyaller kriterlere uyunca otomatik gönderilir._"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

# --- ARKA PLAN SİNYAL DÖNGÜSÜ ---

async def signal_monitor(application):
    """Arka planda maçları tarayıp VIP sinyal atan döngü."""
    print("🚀 Sinyal Monitörü Başlatıldı...")
    sent_signals = set()

    while True:
        matches = await fetch_live_matches()
        for match in matches:
            m_id = match['id']
            if m_id not in sent_signals:
                decision = brain.analyze_match(match)
                if decision["is_signal"]:
                    text = (
                        f"🚨 *VIP SİNYAL* 🚨\n\n"
                        f"⚽ *MAÇ:* {match['homeTeam']['name']} vs {match['awayTeam']['name']}\n"
                        f"🎯 *TAHMİN:* {decision['pick']}\n"
                        f"📊 *OLASILIK:* %{decision['prob']:.0f}\n"
                        f"💰 *ORAN:* {decision['odds']:.2f}\n"
                        f"📈 *VALUE:* %{decision['value']:.1f}\n"
                        f"🔥 *GÜVEN:* {decision['confidence']}\n\n"
                        f"🧠 *ANALİZ:*\n"
                        f"• {decision['analysis'][0]}\n"
                        f"• {decision['analysis'][1]}\n\n"
                        f"💸 *STAKE:* {decision['stake']}/10"
                    )
                    await application.bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
                    sent_signals.add(m_id)
        
        if len(sent_signals) > 500: sent_signals.clear()
        await asyncio.sleep(120)

# --- ANA ÇALIŞTIRICI ---

if __name__ == "__main__":
    # Telegram Uygulamasını Oluştur
    app = ApplicationBuilder().token(TOKEN).build()

    # Komutları Tanımla
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    app.add_handler(CommandHandler("live", live_command))

    # Arka plan döngüsünü (sinyal monitörü) botla birlikte başlat
    loop = asyncio.get_event_loop()
    loop.create_task(signal_monitor(app))

    print("✅ Bot ve Komutlar Hazır. Railway üzerinde çalışıyor...")
    app.run_polling()
