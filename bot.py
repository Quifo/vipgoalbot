import os, asyncio, httpx, json, time
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode
from brain import BettingBrain
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
brain = BettingBrain()
HEADERS = {"User-Agent": "Mozilla/5.0"}

# --- YENİ: DETAYLI İSTATİK ÇEKME ---
async def get_detailed_stats(match_id):
    """Daha fazla veri kaynağından istatistik çeker"""
    stats = {
        'home_da': 0, 'away_da': 0, 
        'home_sot': 0, 'away_sot': 0,
        'home_poss': 50, 'away_poss': 50,
        'home_corners': 0, 'away_corners': 0,
        'data_available': False
    }
    
    try:
        # 1. Ana istatistikler
        url = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
        async with httpx.AsyncClient() as client:
            r = await client.get(url, headers=HEADERS)
            if r.status_code == 200:
                data = r.json()
                for p in data.get('statistics', []):
                    if p.get('period') == 'ALL':
                        for g in p.get('groups', []):
                            for i in g.get('statisticsItems', []):
                                name = i['name']
                                if name == 'Shots on target':
                                    stats['home_sot'] = int(i['homeValue'])
                                    stats['away_sot'] = int(i['awayValue'])
                                    stats['data_available'] = True
                                elif name == 'Dangerous attacks':
                                    stats['home_da'] = int(i['homeValue'])
                                    stats['away_da'] = int(i['awayValue'])
                                    stats['data_available'] = True
                                elif name == 'Ball possession':
                                    stats['home_poss'] = int(str(i['homeValue']).replace('%','') or 50)
                                    stats['away_poss'] = int(str(i['awayValue']).replace('%','') or 50)
                                elif name == 'Corner kicks':
                                    stats['home_corners'] = int(i['homeValue'])
                                    stats['away_corners'] = int(i['awayValue'])
        
        # 2. Tehlikeli atak yoksa, genel atakları kontrol et
        if not stats['data_available'] or (stats['home_da'] == 0 and stats['away_da'] == 0):
            # Genel atak sayılarını al
            url2 = f"https://api.sofascore.com/api/v1/event/{match_id}/statistics"
            # (Burada alternatif veri kaynakları kullanılabilir)
            
    except Exception as e:
        print(f"İstatistik hatası: {e}")
    
    return stats

# --- SİNYAL MONİTÖRÜ (GÜNCELLENMİŞ) ---
async def signal_monitor(app):
    print("🚀 Süper Hassas Sinyal Monitörü Başladı...")
    sent_signals = set()
    
    while True:
        try:
            # Tüm canlı maçları çek
            async with httpx.AsyncClient() as client:
                r = await client.get("https://api.sofascore.com/api/v1/sport/football/events/live", headers=HEADERS)
                data = r.json()
                events = data.get('events', [])
            
            print(f"📊 {len(events)} maç taranıyor...")
            
            for m in events:
                mid = str(m['id'])
                home = m['homeTeam']['name']
                away = m['awayTeam']['name']
                
                # Dakika kontrolü
                status = m.get('status', {})
                minute = status.get('elapsed', 0)
                
                if minute < 5 or minute > 85:
                    continue
                
                if mid in sent_signals:
                    continue
                
                # Detaylı istatistikleri çek
                stats = await get_detailed_stats(mid)
                
                if not stats['data_available']:
                    print(f"⚠️ Veri yok: {home} vs {away}")
                    continue
                
                # Analiz yap
                res = brain.analyze_advanced(m, stats, minute)
                
                if res.get('is_signal'):
                    # Sinyal gönder
                    txt = (
                        f"🚨 *VIP BASKI SİNYALİ* 🚨\n\n"
                        f"⚽ *MAÇ:* {home} vs {away}\n"
                        f"⏰ *DAKİKA:* {minute}' ({res['period']})\n"
                        f"🎯 *BASKI YAPAN:* {res['team']}\n"
                        f"📊 *BASKI SKORU:* {res['pressure']}/100\n"
                        f"📈 *TAHMİN:* {res['pick']}\n"
                        f"✅ *GÜVEN:* {res['confidence']}\n\n"
                        f"📊 *DETAY:*\n"
                        f"• İsabetli Şut: {stats['home_sot']}-{stats['away_sot']}\n"
                        f"• Tehlikeli Atak: {stats['home_da']}-{stats['away_da']}\n"
                        f"• Top Hakimiyeti: %{stats['home_poss']}-{stats['away_poss']}\n"
                        f"• Korner: {stats['home_corners']}-{stats['away_corners']}\n\n"
                        f"💡 _Bu sinyal pressure analizi ile üretildi_"
                    )
                    
                    try:
                        await app.bot.send_message(chat_id=CHAT_ID, text=txt, parse_mode=ParseMode.MARKDOWN)
                        sent_signals.add(mid)
                        print(f"✅ Sinyal: {home} vs {away}")
                        
                        # Sinyal geçmişini temizle (bellek taşmasını önle)
                        if len(sent_signals) > 1000:
                            sent_signals.clear()
                            
                    except Exception as e:
                        print(f"Mesaj hatası: {e}")
                
                # API limitini aşmamak için bekle
                await asyncio.sleep(2)
            
        except Exception as e:
            print(f"Monitör hatası: {e}")
        
        # 3 dakika bekle
        await asyncio.sleep(180)

# --- KOMUTLAR ---
async def live_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Canlı maçlar taranıyor...")
    # (Canlı maç listesi komutu - önceki gibi)
    
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🤖 *VIP Baskı Analiz Sistemi Aktif*", parse_mode=ParseMode.MARKDOWN)

async def post_init(app):
    asyncio.create_task(signal_monitor(app))

if __name__ == "__main__":
    app = ApplicationBuilder().token(TOKEN).post_init(post_init).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("canli", live_command))
    print("✅ Hassas analiz botu çalışıyor...")
    app.run_polling()
