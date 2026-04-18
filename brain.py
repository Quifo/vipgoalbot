class BettingBrain:
    def __init__(self):
        # Eşik Değerleri
        self.MIN_DA = 10     # Min Tehlikeli Atak
        self.MIN_SOT = 2     # Min İsabetli Şut
        self.MIN_PROB = 0.75

    def calculate_pressure(self, stats, minute):
        """Maçtaki baskı kalitesini 0-100 arası puanlar."""
        # Stats: {sot, da, poss}
        da_score = (stats['da'] / minute) * 100 if minute > 0 else 0
        sot_score = stats['sot'] * 15
        
        pressure_score = (da_score * 0.6) + (sot_score * 0.4)
        return min(100, int(pressure_score))

    def analyze_advanced(self, match_data, stats, minute):
        """
        Detaylı istatistiklere göre sinyal üretir.
        stats: {'home_sot': x, 'away_sot': y, 'home_da': z, 'away_da': w}
        """
        # Hangi takımın baskılı olduğunu bul
        h_score = match_data.get('homeScore', {}).get('current', 0)
        a_score = match_data.get('awayScore', {}).get('current', 0)
        
        # Ev sahibi analizi
        h_pressure = self.calculate_pressure({'sot': stats['home_sot'], 'da': stats['home_da']}, minute)
        # Deplasman analizi
        a_pressure = self.calculate_pressure({'sot': stats['away_sot'], 'da': stats['away_da']}, minute)

        # Karar Mekanizması
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
                "pick": "SIRADAKİ GOL" if final_pressure > 80 else "0.5 ÜST",
                "confidence": "ELITE" if final_pressure > 85 else "YÜKSEK",
                "stats_summary": f"🎯 Şut: {stats['home_sot'] + stats['away_sot']} | 🔥 T.Atak: {stats['home_da'] + stats['away_da']}"
            }
        
        return {"is_signal": False}
