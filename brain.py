import math

class BettingBrain:
    def __init__(self):
        self.MIN_TOTAL_SHOTS = 5
        self.MIN_PRESSURE = 35

    def calculate_pressure(self, stats, minute):
        if minute <= 0: minute = 1
        sot = stats.get('sot', 0)
        total_shots = stats.get('shots', 0)
        corners = stats.get('corners', 0)
        poss = stats.get('poss', 50)
        # Formül: İsabetli şut ve korner odaklı
        score = (sot * 12) + (total_shots * 4) + (corners * 6) + (max(0, poss - 50) * 0.8)
        return min(100, int(score))

    def analyze_advanced(self, m, stats, minute, odds_drop, home_form, away_form):
        # Takım formlarını ve bitiriciliğini hesaba kat
        h_eff = (stats['home_sot'] / stats['home_shots']) if stats['home_shots'] > 0 else 0
        a_eff = (stats['away_sot'] / stats['away_shots']) if stats['away_shots'] > 0 else 0

        h_p = self.calculate_pressure(stats, minute) # Basitleştirilmiş çağrı
        # ... (Önceki baskı hesaplama mantığı aynı kalacak şekilde) ...
        
        # Basınç hesaplama (Önceki bot.py'deki mantıkla aynı)
        h_data = {'sot': stats['home_sot'], 'shots': stats['home_shots'], 'corners': stats['home_corners'], 'poss': stats['home_poss']}
        a_data = {'sot': stats['away_sot'], 'shots': stats['away_shots'], 'corners': stats['away_corners'], 'poss': stats['away_poss']}
        h_p = self.calculate_pressure(h_data, minute)
        a_p = self.calculate_pressure(a_data, minute)

        if h_p > a_p and h_p >= self.MIN_PRESSURE:
            target, final_p = m['homeTeam']['name'], h_p
        elif a_p > h_p and a_p >= self.MIN_PRESSURE:
            target, final_p = m['awayTeam']['name'], a_p
        else: return {"is_signal": False}

        h_s = m.get('homeScore', {}).get('current', 0)
        a_s = m.get('awayScore', {}).get('current', 0)
        curr_score = h_s + a_s

        # Tahmin Belirleme
        if minute <= 40:
            pick = "İY 0.5 ÜST" if curr_score == 0 else f"İY {curr_score + 0.5} ÜST"
        else:
            pick = f"MS {curr_score + 0.5} ÜST"

        # Oran Düşüşü (Sharp Money) Bonusu
        if odds_drop > 8: final_p += 10 # %8+ düşüş varsa güveni artır

        conf = "🔥 ELITE" if final_p >= 75 else ("⭐ YÜKSEK" if final_p >= 55 else "📊 ORTA")
        
        return {
            "is_signal": True, "team": target, "pressure": min(100, final_p),
            "pick": pick, "confidence": conf, "score": f"{h_s}-{a_s}",
            "odds_drop": odds_drop, "home_form": home_form, "away_form": away_form
        }
