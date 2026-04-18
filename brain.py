import math

class BettingBrain:
    def __init__(self):
        # KRİTERLERİ GEVŞETTİK (Daha fazla sinyal için)
        self.MIN_TOTAL_DA = 10    # Toplam Tehlikeli Atak (Daha önce 15'ti)
        self.MIN_SOT = 1          # En az 1 isabetli şut (Daha önce 2'ydi)
        self.MIN_PRESSURE = 35    # Baskı eşiği (Daha önce 45'ti)

    def calculate_pressure(self, stats, minute):
        da = stats.get('da', 0)
        sot = stats.get('sot', 0)
        if minute <= 0: minute = 1
        
        da_rate = da / minute
        # Baskı formülü
        pressure = (da_rate * 50) + (sot * 8)
        return min(100, int(pressure))

    def analyze_advanced(self, match_data, stats, minute):
        h_da = stats.get('home_da', 0)
        a_da = stats.get('away_da', 0)
        h_sot = stats.get('home_sot', 0)
        a_sot = stats.get('away_sot', 0)
        
        total_da = h_da + a_da
        total_sot = h_sot + a_sot
        
        # Filtreleme
        if total_da < self.MIN_TOTAL_DA and total_sot < self.MIN_SOT:
            return {"is_signal": False}
        
        h_pressure = self.calculate_pressure({'da': h_da, 'sot': h_sot}, minute)
        a_pressure = self.calculate_pressure({'da': a_da, 'sot': a_sot}, minute)
        
        if h_pressure > a_pressure and h_pressure >= self.MIN_PRESSURE:
            target_team = match_data['homeTeam']['name']
            final_pressure = h_pressure
        elif a_pressure > h_pressure and a_pressure >= self.MIN_PRESSURE:
            target_team = match_data['awayTeam']['name']
            final_pressure = a_pressure
        else:
            return {"is_signal": False}
        
        h_score = match_data.get('homeScore', {}).get('current', 0)
        a_score = match_data.get('awayScore', {}).get('current', 0)
        total_score = h_score + a_score
        
        # Bahis türü
        if minute <= 35: pick = "İLK YARI 0.5 ÜST"
        else: pick = f"{total_score + 0.5} ÜST (GOL BEKLENTİSİ)"
        
        return {
            "is_signal": True,
            "team": target_team,
            "pressure": final_pressure,
            "period": "1. YARI" if minute < 45 else "2. YARI",
            "pick": pick,
            "confidence": "ELITE" if final_pressure > 65 else "YÜKSEK",
            "stats_summary": f"İS: {total_sot} | TA: {total_da} | Baskı: {final_pressure}/100"
        }
