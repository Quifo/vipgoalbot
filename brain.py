class BettingBrain:
    def __init__(self):
        self.MIN_TOTAL_SHOTS = 6
        self.MIN_PRESSURE = 35

    def calculate_pressure(self, stats, minute):
        if minute <= 0: minute = 1
        sot = stats.get('sot', 0)
        total_shots = stats.get('shots', 0)
        corners = stats.get('corners', 0)
        poss = stats.get('poss', 50)
        score = (sot * 12) + (total_shots * 4) + (corners * 6) + (max(0, poss - 50) * 0.8)
        return min(100, int(score))

    def analyze_advanced(self, m, stats, minute, odds_drop=0):
        # Takım verilerini ayır
        h_data = {'sot': stats['home_sot'], 'shots': stats['home_shots'], 
                  'corners': stats['home_corners'], 'poss': stats['home_poss']}
        a_data = {'sot': stats['away_sot'], 'shots': stats['away_shots'], 
                  'corners': stats['away_corners'], 'poss': stats['away_poss']}
        
        # Baskı hesapla
        h_p = self.calculate_pressure(h_data, minute)
        a_p = self.calculate_pressure(a_data, minute)

        # Toplam şut kontrolü
        if (stats['home_shots'] + stats['away_shots']) < self.MIN_TOTAL_SHOTS:
            return {"is_signal": False}

        # Baskın takımı belirle
        if h_p > a_p and h_p >= self.MIN_PRESSURE:
            target = m['homeTeam']['name']
            final_p = h_p
            target_stats = h_data
        elif a_p > h_p and a_p >= self.MIN_PRESSURE:
            target = m['awayTeam']['name']
            final_p = a_p
            target_stats = a_data
        else:
            return {"is_signal": False}

        # Skor bilgisi
        h_s = m.get('homeScore', {}).get('current', 0)
        a_s = m.get('awayScore', {}).get('current', 0)
        curr_score = h_s + a_s

        # Çoklu Bahis Önerileri
        picks = []
        
        if minute <= 40:
            period = "1. YARI"
            if curr_score == 0:
                picks.append(("İY 0.5 ÜST", 0.75, "Düşük"))
                if h_data['sot'] >= 2 and a_data['sot'] >= 2:
                    picks.append(("İY KG VAR", 0.65, "Orta"))
            else:
                picks.append((f"İY {curr_score + 0.5} ÜST", 0.80, "Düşük"))
        else:
            period = "2. YARI"
            picks.append((f"MS {curr_score + 0.5} ÜST", 0.85, "Düşük"))
            if curr_score == 0 and h_data['sot'] >= 1 and a_data['sot'] >= 1:
                picks.append(("KG VAR", 0.70, "Orta"))
            elif curr_score >= 1 and h_data['sot'] >= 2 and a_data['sot'] >= 2:
                picks.append(("KG VAR", 0.75, "Orta"))

        # Korner bahsi
        total_c = stats['home_corners'] + stats['away_corners']
        if total_c >= 8:
            picks.append((f"Korner {total_c + 1.5} ÜST", 0.72, "Orta"))

        if not picks:
            return {"is_signal": False}

        # Sharp Money etkisi
        if odds_drop > 8:
            final_p = min(100, final_p + 10)

        # En iyi bahsi seç
        best = max(picks, key=lambda x: x[1])
        conf = "🔥 ELITE" if final_p >= 75 else ("⭐ YÜKSEK" if final_p >= 55 else "📊 ORTA")

        return {
            "is_signal": True,
            "team": target,
            "pressure": final_p,
            "period": period,
            "pick": best[0],
            "confidence": conf,
            "risk": best[2],
            "prob": int(best[1] * 100),
            "alt": picks,
            "score": f"{h_s}-{a_s}",
            "total_score": curr_score,
            "stats_summary": f"Şut: {stats['home_sot']+stats['away_sot']} | Korner: {total_c}"
        }
