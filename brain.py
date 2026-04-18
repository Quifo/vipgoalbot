import math

class BettingBrain:
    def __init__(self):
        self.MIN_DA = 10
        self.MIN_SOT = 2
        self.MIN_PROB = 0.75

    def calculate_pressure(self, stats, minute):
        da_score = (stats['da'] / minute) * 100 if minute > 0 else 0
        sot_score = stats['sot'] * 15
        pressure_score = (da_score * 0.6) + (sot_score * 0.4)
        return min(100, int(pressure_score))

    def analyze_advanced(self, match_data, stats, minute):
        h_pressure = self.calculate_pressure({'sot': stats['home_sot'], 'da': stats['home_da']}, minute)
        a_pressure = self.calculate_pressure({'sot': stats['away_sot'], 'da': stats['away_da']}, minute)

        target_team = None
        final_pressure = 0
        
        if h_pressure > a_pressure and h_pressure > 60:
            target_team = match_data['homeTeam']['name']
            final_pressure = h_pressure
        elif a_pressure > h_pressure and a_pressure > 60:
            target_team = match_data['awayTeam']['name']
            final_pressure = a_pressure

        if target_team and (stats['home_sot'] + stats['away_sot']) >= self.MIN_SOT:
            period = "İLK YARI" if minute < 45 else "İKİNCİ YARI"
            return {
                "is_signal": True,
                "team": target_team,
                "pressure": final_pressure,
                "period": period,
                "pick": "SIRADAKİ GOL",
                "confidence": "YÜKSEK",
                "stats_summary": f"🎯 Şut: {stats['home_sot'] + stats['away_sot']} | 🔥 T.Atak: {stats['home_da'] + stats['away_da']}"
            }
        return {"is_signal": False}
