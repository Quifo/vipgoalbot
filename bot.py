import os
import asyncio
import httpx
import logging
from telegram import Bot
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

# Ayarlar
TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
bot = Bot(token=TOKEN)
brain = BettingBrain()

# SofaScore Ücretsiz Canlı Veri URL'si
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

async def fetch_live_matches():
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
    }
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(URL, headers=headers)
            return response.json().get('events', [])
        except Exception as e:
            print(f"Veri çekme hatası: {e}")
            return []

async def run_bot():
    print("🚀 Algoritma Maçları Taramaya Başladı...")
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
                        f"• {decision['analysis'][1]}\n"
                        f"• {decision['analysis'][2]}\n\n"
                        f"💸 *STAKE:* {decision['stake']}/10\n\n"
                        f"⚠️ _Bu bir veri analizidir, uzun vadeli ROI odaklıdır._"
                    )
                    await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
                    sent_signals.add(m_id)
        
        await asyncio.sleep(300) # 5 dakikada bir kontrol et (Sunucu güvenliği için)

if __name__ == "__main__":
    asyncio.run(run_bot())