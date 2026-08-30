import time

class BettingBrain:
    def __init__(self):
        self.MIN_MINUTE        = 18
        self.MAX_MINUTE        = 85
        self.MAX_TOTAL_GOALS   = 4
        self.MIN_TOTAL_SHOTS   = 6
        self.MIN_TOTAL_SOT     = 2
        self.MIN_SOT_RATIO     = 0.16
        self.MIN_PRESSURE      = 50
        self.MIN_PRESSURE_DIFF = 10
        self.MIN_ODDS          = 1.40
        self.MIN_VALUE_SCORE   = 55
        self.MIN_CONFIRMATIONS = 3
        self.MIN_XG_DOMINANT   = 0.60
        self.MIN_MOMENTUM      = 55

    def _safe_int(self, val, default=0):
        try:
            if val is None or val == '' or val == '-':
                return default
            return int(float(str(val).replace('%', '').strip()))
        except:
            return default

    def _safe_float(self, val, default=0.0):
        try:
            if val is None or val == '' or val == '-':
                return default
            return float(str(val).replace('%', '').strip())
        except:
            return default

    def _safe_get(self, stats, key, default=0):
        try:
            return self._safe_int(stats.get(key, default), default)
        except:
            return default

    def _calculate_xg(self, sot, shots, corners, poss, minute, real_xg=None):
        try:
            if real_xg is not None and float(real_xg) > 0:
                return round(float(real_xg), 2)
            
            sot     = max(0, self._safe_int(sot))
            shots   = max(0, self._safe_int(shots))
            corners = max(0, self._safe_int(corners))
            poss    = max(0, self._safe_int(poss, 50))
            minute  = max(1, self._safe_int(minute, 1))
            
            xg = (sot * 0.30 + shots * 0.07 + corners * 0.045)
            poss_mult = 1.0 + max(0, (poss - 55) * 0.008)
            minute_factor = min(1.0, minute / 75)
            
            return round(max(0.0, xg * poss_mult * minute_factor), 2)
        except:
            return 0.0

    def _calculate_pressure(self, data, minute):
        try:
            minute  = max(1, self._safe_int(minute, 1))
            sot     = max(0, self._safe_int(data.get('sot', 0)))
            shots   = max(0, self._safe_int(data.get('shots', 0)))
            corners = max(0, self._safe_int(data.get('corners', 0)))
            poss    = max(0, self._safe_int(data.get('poss', 50)))
            
            base = (sot * 16 + shots * 3 + corners * 6)
            poss_bonus = max(0, poss - 55) * 0.8
            
            if 40 <= minute <= 75:
                mf = 1.2
            else:
                mf = min(1.4, minute / 30)
                
            return min(100, int((base + poss_bonus) * mf))
        except:
            return 0

    def analyze_advanced(self, m, stats, minute):
        try:
            minute = max(0, self._safe_int(minute, 0))
            
            # Phase 1: Pre-filter
            if minute < self.MIN_MINUTE:
                return {"is_signal": False, "reason": f"[A1] Erken dakika ({minute}')"}
            if minute > self.MAX_MINUTE:
                return {"is_signal": False, "reason": f"[A1] Geç dakika ({minute}')"}
            
            h_s = self._safe_int(m.get('homeScore', {}).get('current', 0))
            a_s = self._safe_int(m.get('awayScore', {}).get('current', 0))
            if h_s + a_s > self.MAX_TOTAL_GOALS:
                return {"is_signal": False, "reason": f"[A1] Çok gollü ({h_s+a_s})"}
            
            if not stats or not stats.get('has', False):
                return {"is_signal": False, "reason": "[A2] İstatistik yok"}
            
            # Phase 2: Stats Quality
            total_shots = self._safe_get(stats, 'home_shots') + self._safe_get(stats, 'away_shots')
            total_sot   = self._safe_get(stats, 'home_sot') + self._safe_get(stats, 'away_sot')
            
            if total_shots < self.MIN_TOTAL_SHOTS:
                return {"is_signal": False, "reason": f"[A2] Şut az ({total_shots})"}
            if total_sot < self.MIN_TOTAL_SOT:
                return {"is_signal": False, "reason": f"[A2] İsabetli şut az ({total_sot})"}
            
            # Phase 3: Pressure
            h_data = {
                'sot': self._safe_get(stats, 'home_sot'),
                'shots': self._safe_get(stats, 'home_shots'),
                'corners': self._safe_get(stats, 'home_corners'),
                'poss': self._safe_get(stats, 'home_poss', 50)
            }
            a_data = {
                'sot': self._safe_get(stats, 'away_sot'),
                'shots': self._safe_get(stats, 'away_shots'),
                'corners': self._safe_get(stats, 'away_corners'),
                'poss': self._safe_get(stats, 'away_poss', 50)
            }
            
            h_p = self._calculate_pressure(h_data, minute)
            a_p = self._calculate_pressure(a_data, minute)
            
            pressure_diff = abs(h_p - a_p)
            if pressure_diff < self.MIN_PRESSURE_DIFF:
                return {"is_signal": False, "reason": f"[A3] Baskı farkı yetersiz ({pressure_diff})"}
            
            dominant = 'home' if h_p > a_p else 'away'
            final_p = max(h_p, a_p)
            
            if final_p < self.MIN_PRESSURE:
                return {"is_signal": False, "reason": f"[A3] Yeterli baskı yok ({final_p})"}
            
            # Phase 4: Value Analysis
            is_first_half = minute <= 45
            period = "1. YARI" if is_first_half else "2. YARI"
            curr_score = h_s + a_s
            
            if dominant == 'home':
                dom_sot = self._safe_get(stats, 'home_sot')
                dom_xg = self._safe_float(stats.get('home_xg'), 0.0)
            else:
                dom_sot = self._safe_get(stats, 'away_sot')
                dom_xg = self._safe_float(stats.get('away_xg'), 0.0)
            
            picks = []
            
            if is_first_half and minute <= 35 and curr_score == 0:
                if dom_sot >= 2 and dom_xg >= 0.5:
                    picks.append(("İY 0.5 ÜST", 1.70, "Düşük", 60, "iy"))
            
            if not is_first_half and curr_score == 0 and minute < 75:
                if dom_sot >= 3:
                    picks.append(("MS 0.5 ÜST", 1.35, "Çok Düşük", 65, "ms"))
            
            if not is_first_half and curr_score == 1 and minute < 80:
                if dom_sot >= 2 and dom_xg >= 0.6:
                    picks.append(("MS 1.5 ÜST", 1.55, "Düşük", 60, "ms"))
            
            if not picks:
                return {"is_signal": False, "reason": "[A4] Değer taşıyan bahis yok"}
            
            best = max(picks, key=lambda x: x[3])
            
            return {
                "is_signal": True,
                "pick": best[0],
                "pick_type": best[4],
                "confidence": "⭐ YÜKSEK",
                "risk": best[2],
                "prob": best[3],
                "pressure": final_p,
                "period": period,
                "score": f"{h_s}-{a_s}",
                "total_score": curr_score,
                "xg": dom_xg,
                "alt": []
            }
            
        except Exception as e:
            return {"is_signal": False, "reason": f"[HATA] {str(e)}"}
