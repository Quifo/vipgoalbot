import math

class BettingBrain:
    def __init__(self):
        # ROI odaklı eşik değerleri
        self.MIN_TOTAL_SHOTS = 5
        self.MIN_PRESSURE = 35
        self.MIN_CORNERS = 4

    def calculate_pressure(self, stats, minute):
        if minute <= 0: minute = 1
        sot = stats.get('sot', 0)
        total_shots = stats.get('shots', 0)
        corners = stats.get('corners', 0)
        poss = stats.get('poss', 50)
        
        # Gelişmiş Baskı Formülü
        score = (sot * 12) + (total_shots * 4) + (corners * 6) + (max(0, poss - 50) * 0.8)
        return min(100, int(score))

    def analyze_advanced(self, match_data, stats, minute):
        h = {'sot': stats['home_sot'], 'shots': stats['home_shots'], 
             'corners': stats['home_corners'], 'poss': stats['home_poss']}
        a = {'sot': stats['away_sot'], 'shots': stats['away_shots'], 
             'corners': stats['away_corners'], 'poss': stats['away_poss']}

        h_p = self.calculate_pressure(h, minute)
        a_p = self.calculate_pressure(a, minute)
        
        # Toplam şut kontrolü (Maçta hareket yoksa sinyal yok)
        if (h['shots'] + a['shots']) < self.MIN_TOTAL_SHOTS:
            return {"is_signal": False}
        
        # Baskın tarafı belirle
        if h_p > a_p and h_p >= self.MIN_PRESSURE:
            target, final_p, target_stats = match_data['homeTeam']['name'], h_p, h
        elif a_p > h_p and a_p >= self.MIN_PRESSURE:
            target, final_p, target_stats = match_data['awayTeam']['name'], a_p, a
        else:
            return {"is_signal": False}
        
        # Mevcut Skor
        h_s = match_data.get('homeScore', {}).get('current', 0)
        a_s = match_data.get('awayScore', {}).get('current', 0)
        curr_score = h_s + a_s
        
        picks = []
        # --- BÜYÜK VERİ ANALİZİ VE BAHİS SEÇİMİ ---
        if minute <= 40: # İlk Yarı
            period = "1. YARI"
            if curr_score == 0:
                picks.append(("İlk Yarı 0.5 Üst", 0.75, "Düşük"))
                if h['sot'] >= 2 and a['sot'] >= 2: picks.append(("İlk Yarı KG Var", 0.65, "Orta"))
            else:
                picks.append((f"İlk Yarı {curr_score + 0.5} Üst", 0.80, "Düşük"))
        else: # İkinci Yarı
            period = "2. YARI"
            picks.append((f"Maç Sonu {curr_score + 0.5} Üst", 0.85, "Düşük"))
            if curr_score == 0 and h['sot'] >= 1 and a['sot'] >= 1:
                picks.append(("KG Var", 0.70, "Orta"))
            elif curr_score >= 1:
                if h['sot'] >= 2 and a['sot'] >= 2: picks.append(("KG Var", 0.75, "Orta"))

        # Korner Bahsi
        total_c = h['corners'] + a['corners']
        if total_c >= 8: picks.append((f"Korner {total_c + 1.5} Üst", 0.72, "Orta"))

        if not picks: return {"is_signal": False}

        # En güvenilir olanı ana bahis yap
        best = max(picks, key=lambda x: x[1])
        conf = "🔥 ELITE" if best[1] >= 0.80 else ("⭐ YÜKSEK" if best[1] >= 0.70 else "📊 ORTA")
        
        return {
            "is_signal": True, "team": target, "pressure": final_p,
            "period": period, "pick": best[0], "confidence": conf,
            "risk": best[2], "prob": int(best[1] * 100),
            "alt": picks, "score": f"{h_s} - {a_s}", "corners": total_c
        }
