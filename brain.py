import math

class BettingBrain:
    def __init__(self):
        self.MIN_TOTAL_SHOTS = 6
        self.MIN_PRESSURE = 40

    def calculate_pressure(self, stats, minute):
        """SofaScore'un gerçekten tuttuğu verilerle baskı hesaplar"""
        if minute <= 0: minute = 1
        
        # Gerçek SofaScore verileri
        sot = stats.get('sot', 0)           # İsabetli şut
        total_shots = stats.get('shots', 0)  # Toplam şut
        corners = stats.get('corners', 0)    # Korner
        poss = stats.get('poss', 50)         # Top hakimiyeti
        
        # Baskı puanı hesabı (Veri uyumlu)
        score = 0
        score += sot * 12                          # Her isabetli şut +12
        score += total_shots * 4                   # Her şut +4
        score += corners * 6                       # Her korner +6
        score += max(0, poss - 50) * 0.8           # %50 üstü hakimiyet bonus
        
        return min(100, int(score))

    def analyze_advanced(self, match_data, stats, minute):
        h = {'sot': stats['home_sot'], 'shots': stats['home_shots'], 
             'corners': stats['home_corners'], 'poss': stats['home_poss']}
        a = {'sot': stats['away_sot'], 'shots': stats['away_shots'], 
             'corners': stats['away_corners'], 'poss': stats['away_poss']}

        h_pressure = self.calculate_pressure(h, minute)
        a_pressure = self.calculate_pressure(a, minute)
        
        total_shots = stats['home_shots'] + stats['away_shots']
        if total_shots < self.MIN_TOTAL_SHOTS:
            return {"is_signal": False}
        
        if h_pressure > a_pressure and h_pressure >= self.MIN_PRESSURE:
            target = match_data['homeTeam']['name']
            final_pressure = h_pressure
            target_stats = h
        elif a_pressure > h_pressure and a_pressure >= self.MIN_PRESSURE:
            target = match_data['awayTeam']['name']
            final_pressure = a_pressure
            target_stats = a
        else:
            return {"is_signal": False}
        
        h_score = match_data.get('homeScore', {}).get('current', 0)
        a_score = match_data.get('awayScore', {}).get('current', 0)
        total_score = h_score + a_score
        
        # Akıllı bahis önerisi
        if minute <= 35:
            pick = "İLK YARI 0.5 ÜST"
            risk = "Düşük"
        elif total_score == 0:
            pick = "0.5 ÜST"
            risk = "Düşük"
        elif total_score == 1:
            pick = "1.5 ÜST / KG VAR"
            risk = "Orta"
        else:
            pick = f"{total_score + 0.5} ÜST"
            risk = "Orta"
        
        confidence = "🔥 ELITE" if final_pressure >= 70 else ("⭐ YÜKSEK" if final_pressure >= 55 else "📊 ORTA")
        
        return {
            "is_signal": True,
            "team": target,
            "pressure": final_pressure,
            "period": "1. YARI" if minute < 45 else "2. YARI",
            "pick": pick,
            "confidence": confidence,
            "risk": risk,
            "target_stats": target_stats,
            "all_stats": stats
        }
