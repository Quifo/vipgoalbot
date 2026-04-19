class BettingBrain:
    def __init__(self):
        self.MIN_TOTAL_SHOTS = 6
        self.MIN_PRESSURE = 38      # Biraz yükselttim

    def calculate_pressure(self, stats, minute):
        if minute <= 0: 
            minute = 1
            
        sot = stats.get('sot', 0)
        total_shots = stats.get('shots', 0)
        corners = stats.get('corners', 0)
        poss = stats.get('poss', 50)

        # Daha dengeli ve dakikaya duyarlı formül
        base_score = (sot * 14) + (total_shots * 3.5) + (corners * 5.5)
        possession_bonus = max(0, poss - 52) * 0.9
        minute_factor = min(1.8, minute / 35)   # Maç ilerledikçe baskı daha değerli

        score = (base_score + possession_bonus) * minute_factor
        return min(100, int(score))

    def analyze_advanced(self, m, stats, minute, odds_drop=0):
        h_data = {'sot': stats['home_sot'], 'shots': stats['home_shots'], 
                  'corners': stats['home_corners'], 'poss': stats['home_poss']}
        a_data = {'sot': stats['away_sot'], 'shots': stats['away_shots'], 
                  'corners': stats['away_corners'], 'poss': stats['away_poss']}
        
        h_p = self.calculate_pressure(h_data, minute)
        a_p = self.calculate_pressure(a_data, minute)

        if (stats['home_shots'] + stats['away_shots']) < self.MIN_TOTAL_SHOTS:
            return {"is_signal": False}

        if h_p > a_p and h_p >= self.MIN_PRESSURE:
            target = m['homeTeam']['name']
            final_p = h_p
        elif a_p > h_p and a_p >= self.MIN_PRESSURE:
            target = m['awayTeam']['name']
            final_p = a_p
        else:
            return {"is_signal": False}

        h_s = m.get('homeScore', {}).get('current', 0)
        a_s = m.get('awayScore', {}).get('current', 0)
        curr_score = h_s + a_s

        picks = []
        period = "1. YARI" if minute <= 40 else "2. YARI"

        if minute <= 40:
            if curr_score == 0:
                picks.append(("İY 0.5 ÜST", 1.45, "Düşük"))
                if h_data['sot'] >= 2 and a_data['sot'] >= 2:
                    picks.append(("İY KG VAR", 1.70, "Orta"))
            else:
                picks.append((f"İY {curr_score + 0.5} ÜST", 1.55, "Düşük"))
        else:
            picks.append((f"MS {curr_score + 0.5} ÜST", 1.50, "Düşük"))
            if curr_score == 0 and h_data['sot'] >= 1 and a_data['sot'] >= 1:
                picks.append(("KG VAR", 1.65, "Orta"))
            elif curr_score >= 1 and h_data['sot'] >= 2 and a_data['sot'] >= 2:
                picks.append(("KG VAR", 1.75, "Orta"))

        total_c = stats['home_corners'] + stats['away_corners']
        if total_c >= 8:
            picks.append((f"Korner {total_c + 1.5} ÜST", 1.60, "Orta"))

        if not picks:
            return {"is_signal": False}

        # En iyi pick
        best = max(picks, key=lambda x: x[1])
        
        # === YENİ: ORAN FİLTRESİ ===
        if best[1] < 1.38:
            return {"is_signal": False}

        conf = "🔥 ELITE" if final_p >= 78 else ("⭐ YÜKSEK" if final_p >= 60 else "📊 ORTA")

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
