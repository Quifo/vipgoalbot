class BettingBrain:
    def __init__(self):
        self.MIN_TOTAL_SHOTS = 8          # 6'dan 8'e çıkarıldı (Daha az maç = Daha kaliteli sinyal)
        self.MIN_PRESSURE = 45            # 38'den 45'e çıkarıldı
        self.MIN_ODDS = 1.45              # 1.38'den 1.45'e çıkarıldı

    def calculate_pressure(self, stats, minute):
        if minute <= 0: 
            minute = 1
            
        sot = stats.get('sot', 0)
        total_shots = stats.get('shots', 0)
        corners = stats.get('corners', 0)
        poss = stats.get('poss', 50)

        # İsabetli şut en önemli veridir, katsayısı artırıldı
        base_score = (sot * 16) + (total_shots * 3) + (corners * 6)
        
        # Top hakimiyeti 55% altı bonus vermesin
        possession_bonus = max(0, poss - 55) * 0.8
        
        # Dakika çarpanı: 35-70. dakikalar en verimli zamanlardır
        if 35 <= minute <= 70:
            minute_factor = 1.2
        elif minute > 80:
            minute_factor = 0.8  # Maç sonu baskısı genelde sonuç vermez
        else:
            minute_factor = min(1.5, minute / 30)

        score = (base_score + possession_bonus) * minute_factor
        
        # 75. dakikadan sonra baskı puanını biraz kır (Yorgunluk)
        if minute > 75:
            score *= 0.9
            
        return min(100, int(score))

    def analyze_advanced(self, m, stats, minute, odds_drop=0):
        h_data = {'sot': stats['home_sot'], 'shots': stats['home_shots'], 
                  'corners': stats['home_corners'], 'poss': stats['home_poss']}
        a_data = {'sot': stats['away_sot'], 'shots': stats['away_shots'], 
                  'corners': stats['away_corners'], 'poss': stats['away_poss']}
        
        h_p = self.calculate_pressure(h_data, minute)
        a_p = self.calculate_pressure(a_data, minute)

        # Toplam şut kontrolü
        if (stats['home_shots'] + stats['away_shots']) < self.MIN_TOTAL_SHOTS:
            return {"is_signal": False}

        # Baskı yapan takımı belirle (Beraberlik durumunda sinyal yok)
        if h_p > a_p and h_p >= self.MIN_PRESSURE:
            target = m['homeTeam']['name']
            final_p = h_p
            dominant = 'home'
        elif a_p > h_p and a_p >= self.MIN_PRESSURE:
            target = m['awayTeam']['name']
            final_p = a_p
            dominant = 'away'
        else:
            return {"is_signal": False}

        h_s = m.get('homeScore', {}).get('current', 0)
        a_s = m.get('awayScore', {}).get('current', 0)
        curr_score = h_s + a_s

        picks = []
        period = "1. YARI" if minute <= 40 else "2. YARI"

        # --- STRATEJİ 1: GOL BAHİSLERİ ---
        if minute <= 40:
            if curr_score == 0:
                # İlk yarı golü için isabetli şut şartı
                if h_data['sot'] + a_data['sot'] >= 3:
                    picks.append(("İY 0.5 ÜST", 1.55, "Düşük"))
                if h_data['sot'] >= 2 and a_data['sot'] >= 2:
                    picks.append(("İY KG VAR", 1.75, "Orta"))
            else:
                picks.append((f"İY {curr_score + 0.5} ÜST", 1.60, "Düşük"))
        else:
            # İkinci yarı stratejisi
            if curr_score == 0 and (h_data['sot'] + a_data['sot']) >= 6:
                picks.append(("MS 0.5 ÜST", 1.50, "Düşük")) # Maçta en az 1 gol
            
            if curr_score >= 1:
                picks.append((f"MS {curr_score + 0.5} ÜST", 1.55, "Düşük"))
            
            # KG VAR için daha sıkı şartlar
            if h_data['sot'] >= 3 and a_data['sot'] >= 3:
                picks.append(("KG VAR", 1.70, "Orta"))

        # --- STRATEJİ 2: KORNER ---
        total_c = stats['home_corners'] + stats['away_corners']
        # Korner bahsi için maçın en az 50. dakikasında olmalıyız
        if minute > 50 and total_c >= 7:
            picks.append((f"Korner {total_c + 1.5} ÜST", 1.65, "Orta"))

        # --- STRATEJİ 3: TARAF BAHİSİ (YENİ) ---
        # Baskı çok yüksekse ve skor eşitse veya baskı yapan gerideyse
        if final_p >= 65 and curr_score <= 1:
            if dominant == 'home' and h_s <= a_s:
                picks.append(("MS 1 ÇŞ", 1.60, "Yüksek")) # Çifte Şans
            elif dominant == 'away' and a_s <= h_s:
                picks.append(("MS 2 ÇŞ", 1.60, "Yüksek"))

        if not picks:
            return {"is_signal": False}

        # En iyi pick (Sadece orana göre değil, risk oranına göre seç)
        # Risk: Düşük=1, Orta=2, Yüksek=3
        risk_map = {"Düşük": 1, "Orta": 2, "Yüksek": 3}
        best = max(picks, key=lambda x: x[1] / risk_map.get(x[2], 1))

        # === ORAN FİLTRESİ ===
        if best[1] < self.MIN_ODDS:
            return {"is_signal": False}

        # ====================== GÜVEN SKORU HESAPLAMA ======================
        sot_diff = abs(stats['home_sot'] - stats['away_sot'])
        shot_diff = abs(stats['home_shots'] - stats['away_shots'])
        
        # Gerçekçi olasılık hesabı (Tavan %85)
        base_prob = final_p * 0.65              # Baskı etkisi biraz azaltıldı
        shot_bonus = min(shot_diff * 2.5, 15)   # Maksimum 15 puan bonus
        sot_bonus = min(sot_diff * 6, 20)       # Maksimum 20 puan bonus
        
        # Dakika bonusu: 60-75. dakikalar en yüksek olasılıktır
        if 60 <= minute <= 75:
            base_prob += 5

        prob = int(base_prob + shot_bonus + sot_bonus)
        prob = max(55, min(85, prob))           # %55 - %85 arası sınır

        # Güven seviyesi metni
        if prob >= 80:
            conf = "🔥 ELITE"
        elif prob >= 70:
            conf = "⭐ ÇOK GÜVENİLİR"
        elif prob >= 60:
            conf = "✅ YÜKSEK"
        else:
            conf = "📊 ORTA"

        return {
            "is_signal": True,
            "team": target,
            "pressure": final_p,
            "period": period,
            "pick": best[0],
            "confidence": conf,
            "risk": best[2],
            "prob": prob,
            "alt": picks,
            "score": f"{h_s}-{a_s}",
            "total_score": curr_score,
            "stats_summary": f"Şut: {stats['home_sot']+stats['away_sot']} | Korner: {total_c}"
        }
