class BettingBrain:
    def __init__(self):
        # Minimum şartları daha esnek yaptık
        self.MIN_TOTAL_DA = 15    # Toplam tehlikeli atak (Ev+Dep) en az 15 olmalı
        self.MIN_SOT = 2          # En az 2 isabetli şut (toplam)
        self.MIN_PRESSURE = 45    # Baskı skoru eşiği (düşürüldü)

    def calculate_pressure(self, stats, minute):
        """Basitleştirilmiş baskı skoru hesaplama"""
        da = stats.get('da', 0)
        sot = stats.get('sot', 0)
        
        if minute <= 0: minute = 1
        
        # Dakika başına tehlikeli atak
        da_rate = da / minute
        
        # Baskı formülü: (Tehlikeli Atak Hızı * 40) + (İsabetli Şut * 5)
        pressure = (da_rate * 40) + (sot * 5)
        return min(100, int(pressure))

    def analyze_advanced(self, match_data, stats, minute):
        h_da = stats.get('home_da', 0)
        a_da = stats.get('away_da', 0)
        h_sot = stats.get('home_sot', 0)
        a_sot = stats.get('away_sot', 0)
        
        # Toplam istatistikler
        total_da = h_da + a_da
        total_sot = h_sot + a_sot
        
        # Şart 1: Toplam istatistikler yeterli mi?
        if total_da < self.MIN_TOTAL_DA and total_sot < self.MIN_SOT:
            return {"is_signal": False, "reason": "Yetersiz istatistik"}
        
        # Şart 2: Baskı skorlarını hesapla
        h_pressure = self.calculate_pressure({'da': h_da, 'sot': h_sot}, minute)
        a_pressure = self.calculate_pressure({'da': a_da, 'sot': a_sot}, minute)
        
        # Hangi taraf daha baskılı?
        if h_pressure > a_pressure and h_pressure >= self.MIN_PRESSURE:
            target_team = match_data['homeTeam']['name']
            final_pressure = h_pressure
        elif a_pressure > h_pressure and a_pressure >= self.MIN_PRESSURE:
            target_team = match_data['awayTeam']['name']
            final_pressure = a_pressure
        else:
            return {"is_signal": False, "reason": f"Baskı eşiği: {max(h_pressure, a_pressure)} < {self.MIN_PRESSURE}"}
        
        # Maç durumu analizi
        h_score = match_data.get('homeScore', {}).get('current', 0)
        a_score = match_data.get('awayScore', {}).get('current', 0)
        total_score = h_score + a_score
        
        # Bahis türü belirleme
        if minute <= 30:
            pick = "İLK YARI 0.5 ÜST"
        elif minute <= 75:
            if total_score == 0:
                pick = "0.5 ÜST (GOL BEKLENTİSİ)"
            else:
                pick = f"{total_score + 0.5} ÜST"
        else:
            pick = "SON DAKİKALAR - GOL BEKLENTİSİ"
        
        period = "1. YARI" if minute < 45 else "2. YARI"
        
        return {
            "is_signal": True,
            "team": target_team,
            "pressure": final_pressure,
            "period": period,
            "pick": pick,
            "confidence": "ELITE" if final_pressure > 70 else "YÜKSEK",
            "stats_summary": f"İS: {total_sot} | TA: {total_da} | Baskı: {final_pressure}/100"
        }
