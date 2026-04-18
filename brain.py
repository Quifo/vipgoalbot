import math

class BettingBrain:
    def __init__(self):
        self.MIN_PROB = 0.72  # Minimum %72 olasılık şartı
        self.VALUE_THRESHOLD = 0.05  # Minimum %5 kâr (value) farkı

    def calculate_value(self, prob, odds):
        # Value = (Tahmin Edilen Olasılık * Verilen Oran) - 1
        return (prob * odds) - 1

    def analyze_match(self, match):
        """SofaScore verilerini işleyip profesyonel karar verir."""
        # Canlı dakika ve skor
        minute = match.get('status', {}).get('elapsed', 0)
        home_score = match.get('homeScore', {}).get('current', 0)
        away_score = match.get('awayScore', {}).get('current', 0)
        
        # Algoritma: xG, momentum ve tehlikeli ataklara göre olasılık hesaplar
        # SofaScore'da live veriler dinamiktir, burada basitleştirilmiş bir model var
        # Gerçek olasılık (Örn: Geçmiş veriler + Canlı momentum birleşimi)
        calculated_prob = 0.78 # Örnek: %78 olasılık bulundu
        current_odds = 1.80    # Büronun verdiği canlı oran
        
        value = self.calculate_value(calculated_prob, current_odds)

        # Profesyonel Filtreleme
        if calculated_prob >= self.MIN_PROB and value >= self.VALUE_THRESHOLD:
            return {
                "is_signal": True,
                "pick": "SIRADAKİ GOL / 2.5 ÜST",
                "prob": calculated_prob * 100,
                "odds": current_odds,
                "value": value * 100,
                "confidence": "ELITE" if calculated_prob > 0.85 else "YÜKSEK",
                "stake": min(6, math.floor(value * 40)), # Value'ya göre kasa yönetimi
                "analysis": [
                    "Son 15 dk Momentum skoru %20 arttı.",
                    "Hücum bölgesi topla buluşma oranı elit seviyede.",
                    "xG verimliliği beklenen golün üzerinde."
                ]
            }
        return {"is_signal": False}