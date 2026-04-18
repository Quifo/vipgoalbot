import os
import asyncio
import httpx
from telegram import Bot
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
bot = Bot(token=TOKEN)
brain = BettingBrain()

# URL'yi sadece canlı maçları (live) kapsayacak şekilde netleştirdik
URL = "https://api.sofascore.com/api/v1/sport/football/events/live"

async def fetch_live_matches():
    # Timeout'u 30 saniyeye çıkardık (ReadTimeout hatası için)
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
            print(f"⚠️ Bağlantı Hatası (Timeout veya Network): {e}")
            return []

async def run_bot():
    print("🤖 VIP Bahis Algoritması Taramaya Başladı...")
    # Sadece son 50 maçı hafızada tutarak şişmeyi önlüyoruz
    sent_signals = set()

    while True:
        matches = await fetch_live_matches()
        
        if not matches:
            print("📭 Şu an aktif canlı maç bulunamadı veya veri alınamadı.")
        
        for match in matches:
            m_id = match['id']
            
            # Eğer bu maç için zaten sinyal gönderildiyse geç
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
                        f"⚠️ _ROI Odaklı Analiz: Disiplini koruyun._"
                    )
                    try:
                        await bot.send_message(chat_id=CHAT_ID, text=text, parse_mode=ParseMode.MARKDOWN)
                        sent_signals.add(m_id)
                        print(f"✅ Sinyal Gönderildi: {match['homeTeam']['name']}")
                    except Exception as te:
                        print(f"❌ Telegram Hatası: {te}")
        
        # Hafıza yönetimi: sent_signals listesini periyodik temizle (örn. 500 ID'yi geçerse)
        if len(sent_signals) > 500:
            sent_signals.clear()

        # Kontrol aralığı: 2 dakika (Canlı maçları kaçırmamak için ideal)
        await asyncio.sleep(120)

if __name__ == "__main__":
    asyncio.run(run_bot())
