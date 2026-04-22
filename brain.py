import time

class BettingBrain:
    def __init__(self):
        self.MIN_MINUTE        = 18      # Daha erken sinyal (25'ten 18'e)
        self.MAX_MINUTE        = 82
        self.MAX_TOTAL_GOALS   = 4
        self.MIN_TOTAL_SHOTS   = 10
        self.MIN_TOTAL_SOT     = 3
        self.MIN_SOT_RATIO     = 0.18
        self.MIN_PRESSURE      = 48      # Hafif düşürüldü (50'den)
        self.MIN_PRESSURE_DIFF = 12
        self.MIN_ODDS          = 1.40    # Hafif düşürüldü (1.45'ten)
        self.MIN_VALUE_SCORE   = 55      # Hafif düşürüldü (58'den)
        self.MIN_CONFIRMATIONS = 3
        self.MIN_XG_DOMINANT   = 0.75    # Hafif düşürüldü (0.8'den)
        self.MIN_XG_TOTAL      = 1.2
        self.MIN_MOMENTUM      = 55

    # ─────────────────────────────────────────
    # GÜVENLİ VERİ OKUMA
    # ─────────────────────────────────────────
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

    def _safe_get_team(self, m, team, field, default=0):
        try:
            val = m.get(f'{team}Score', {}).get(field, default)
            return self._safe_int(val, default)
        except:
            return default

    def _safe_team_name(self, m, side):
        try:
            return (m.get(f'{side}Team', {}).get('name', 'Bilinmiyor')
                    or 'Bilinmiyor')
        except:
            return 'Bilinmiyor'

    # ─────────────────────────────────────────
    # xG HESAPLAMA
    # ─────────────────────────────────────────
    def _calculate_xg(self, sot, shots, corners, poss, minute,
                       real_xg=None):
        try:
            if real_xg is not None and float(real_xg) > 0:
                return round(float(real_xg), 2)

            sot     = max(0, self._safe_int(sot))
            shots   = max(0, self._safe_int(shots))
            corners = max(0, self._safe_int(corners))
            poss    = max(0, self._safe_int(poss, 50))
            minute  = max(1, self._safe_int(minute, 1))

            xg = (sot * 0.30 + shots * 0.07 + corners * 0.045)
            poss_mult     = 1.0 + max(0, (poss - 55) * 0.008)
            minute_factor = min(1.0, minute / 75)

            return round(max(0.0, xg * poss_mult * minute_factor), 2)
        except:
            return 0.0

    # ─────────────────────────────────────────
    # MOMENTUM
    # ─────────────────────────────────────────
    def _calculate_momentum(self, stats, minute, dominant):
        try:
            if dominant == 'home':
                sot       = self._safe_get(stats, 'home_sot')
                shots     = self._safe_get(stats, 'home_shots')
                corners   = self._safe_get(stats, 'home_corners')
                poss      = self._safe_get(stats, 'home_poss', 50)
                dangerous = self._safe_get(stats, 'home_dangerous')
            else:
                sot       = self._safe_get(stats, 'away_sot')
                shots     = self._safe_get(stats, 'away_shots')
                corners   = self._safe_get(stats, 'away_corners')
                poss      = self._safe_get(stats, 'away_poss', 50)
                dangerous = self._safe_get(stats, 'away_dangerous')

            minute = max(1, self._safe_int(minute, 1))

            momentum = (
                min(35, (shots    / minute) * 90 * 2.2) +
                min(30, (sot      / minute) * 90 * 6.5) +
                min(15, (corners  / minute) * 90 * 2.0) +
                min(10, max(0, poss - 50) * 0.5)        +
                min(10, (dangerous/ minute) * 90 * 0.8)
            )

            if 65 <= minute <= 80:
                momentum *= 1.15
            elif minute > 80:
                momentum *= 0.85

            return min(100, int(momentum))
        except:
            return 0

    # ─────────────────────────────────────────
    # TUTARSIZLIK DEDEKTÖRÜ
    # ─────────────────────────────────────────
    def _check_inconsistency(self, stats, dominant):
        try:
            if dominant == 'home':
                poss  = self._safe_get(stats, 'home_poss', 50)
                shots = self._safe_get(stats, 'home_shots')
                sot   = self._safe_get(stats, 'home_sot')
            else:
                poss  = self._safe_get(stats, 'away_poss', 50)
                shots = self._safe_get(stats, 'away_shots')
                sot   = self._safe_get(stats, 'away_sot')

            issues = []

            if poss >= 60 and shots <= 3:
                issues.append("Yüksek hakimiyet ama çok az şut")
            if shots >= 8 and sot == 0:
                issues.append("Çok şut ama isabetli şut yok")
            if sot > shots:
                issues.append("İsabetli şut > Toplam şut")

            total_poss = (self._safe_get(stats, 'home_poss', 50) +
                          self._safe_get(stats, 'away_poss', 50))
            if not (85 <= total_poss <= 115):
                issues.append(f"Hakimiyet tutarsız ({total_poss}%)")

            return issues
        except:
            return []

    # ─────────────────────────────────────────
    # SKOR BAĞLAMI
    # ─────────────────────────────────────────
    def _analyze_score_context(self, h_s, a_s, dominant, minute, pick):
        try:
            issues    = []
            dom_score = h_s if dominant == 'home' else a_s
            rec_score = a_s if dominant == 'home' else h_s
            diff      = dom_score - rec_score

            if diff >= 3:
                issues.append("Fark çok açık")
            if diff <= -3:
                issues.append("3+ gol geride")
            if minute > 75 and (h_s + a_s) == 0 and 'ÜST' in str(pick):
                issues.append("Geç dakika 0-0 ÜST riskli")
            if minute > 70 and (h_s + a_s) == 0 and 'KG' in str(pick):
                issues.append("70+ 0-0 KG riskli")

            return issues
        except:
            return []

    # ─────────────────────────────────────────
    # BASKI HESAPLAMA
    # ─────────────────────────────────────────
    def _calculate_pressure(self, data, minute):
        try:
            minute    = max(1, self._safe_int(minute, 1))
            sot       = max(0, self._safe_int(data.get('sot',       0)))
            shots     = max(0, self._safe_int(data.get('shots',     0)))
            corners   = max(0, self._safe_int(data.get('corners',   0)))
            poss      = max(0, self._safe_int(data.get('poss',     50)))
            dangerous = max(0, self._safe_int(data.get('dangerous', 0)))
            attacks   = max(0, self._safe_int(data.get('attacks',   0)))
            saves     = max(0, self._safe_int(data.get('saves',     0)))
            shots_box = max(0, self._safe_int(data.get('shots_box', 0)))

            base = (
                sot       * 16  +
                shots     *  3  +
                corners   *  6  +
                dangerous *  4  +
                shots_box *  5  +
                saves     *  8  +
                attacks   *  0.5
            )
            poss_bonus = max(0, poss - 55) * 0.8

            if 40 <= minute <= 75:
                mf = 1.2
            elif minute > 80:
                mf = 0.75
            else:
                mf = min(1.4, minute / 30)

            score = (base + poss_bonus) * mf
            if minute > 75:
                score *= 0.88

            return min(100, int(score))
        except:
            return 0

    # ─────────────────────────────────────────
    # DEĞER SKORU (Genişletilmiş)
    # ─────────────────────────────────────────
    def _calc_value_score(self, pressure, sot, shots, corners,
                           minute, bet_type, xg=0.0, curr_score=0):
        try:
            pressure  = max(0, self._safe_int(pressure))
            sot       = max(0, self._safe_int(sot))
            shots     = max(0, self._safe_int(shots))
            corners   = max(0, self._safe_int(corners))
            minute    = max(1, self._safe_int(minute, 1))
            xg        = max(0.0, self._safe_float(xg))
            curr_score= max(0, self._safe_int(curr_score))

            base     = pressure * 0.40
            xg_bonus = min(xg * 12, 18)

            # Dakika bonusları
            early_bonus = 15 if 25 <= minute <= 35 else 5 if 20 <= minute <= 40 else 0
            late_bonus = 10 if 55 <= minute <= 70 else 5 if 45 <= minute <= 75 else 0

            scores = {
                'iy_ust': base + min(sot * 8, 30) + min(shots * 2, 10) + early_bonus + xg_bonus,
                'iy_ust_15': base + min(sot * 9, 35) + min(shots * 2.5, 15) + (10 if 30 <= minute <= 40 else 0) + xg_bonus * 1.2,
                'iy_kg': base + min(sot * 6, 20) + min(corners * 0.5, 5) + 10 + xg_bonus * 0.8,
                'ms_25': base + min(sot * 7, 25) + min(shots * 1.5, 8) + late_bonus + xg_bonus,
                'ms_35': base + min(sot * 8, 30) + min(shots * 2, 12) + (15 if curr_score >= 2 else 0) + xg_bonus * 1.3,
                'ms_05': base + min(sot * 6, 20) + 5 + xg_bonus * 0.7,
                'team_15': base + min(sot * 9, 35) + xg_bonus * 1.4,
                'kg_var': base + min(sot * 6, 22) + 12 + xg_bonus,
                'kg_yok': (100 - base) * 0.6 + min((10-sot) * 3, 20),
                'korner_iy': base * 0.5 + min(corners * 4, 30) + 10,
                'korner_ms': base * 0.4 + min(corners * 3.5, 25) + 8,
                'handicap': base + min(sot * 8, 28) + xg_bonus * 1.2,
                'taraf': base + min(sot * 7, 25) + xg_bonus,
            }
            
            return min(100, int(scores.get(bet_type, base)))
        except:
            return 0

    # ─────────────────────────────────────────
    # AŞAMA 1: ÖN FİLTRE
    # ─────────────────────────────────────────
    def _phase1_prefilter(self, m, stats, minute):
        try:
            reasons = []
            minute  = self._safe_int(minute, 0)

            if minute < self.MIN_MINUTE:
                reasons.append(f"Erken dakika ({minute}')")
            if minute > self.MAX_MINUTE:
                reasons.append(f"Geç dakika ({minute}')")

            h_s = self._safe_get_team(m, 'home', 'current')
            a_s = self._safe_get_team(m, 'away', 'current')
            if h_s + a_s > self.MAX_TOTAL_GOALS:
                reasons.append(f"Çok gollü ({h_s + a_s} gol)")

            if not stats or not stats.get('has', False):
                reasons.append("İstatistik yok")

            return len(reasons) == 0, reasons
        except Exception as e:
            return False, [f"Ön filtre hatası: {e}"]

    # ─────────────────────────────────────────
    # AŞAMA 2: İSTATİSTİK KALİTESİ
    # ─────────────────────────────────────────
    def _phase2_stats_quality(self, stats, minute):
        try:
            reasons = []
            score   = 0
            minute  = max(1, self._safe_int(minute, 1))

            total_shots = (self._safe_get(stats, 'home_shots') +
                           self._safe_get(stats, 'away_shots'))
            total_sot   = (self._safe_get(stats, 'home_sot') +
                           self._safe_get(stats, 'away_sot'))

            if total_shots < self.MIN_TOTAL_SHOTS:
                reasons.append(f"Şut az ({total_shots})")
            else:
                score += 15

            if total_sot < self.MIN_TOTAL_SOT:
                reasons.append(f"İsabetli şut az ({total_sot})")
            else:
                score += 20

            if total_shots > 0:
                ratio = total_sot / total_shots
                if ratio < self.MIN_SOT_RATIO:
                    reasons.append(
                        f"Şut kalitesi düşük (%{int(ratio * 100)})")
                else:
                    score += 15

            total_poss = (self._safe_get(stats, 'home_poss', 50) +
                          self._safe_get(stats, 'away_poss', 50))
            if 85 <= total_poss <= 115:
                score += 10
            else:
                reasons.append("Hakimiyet verisi hatalı")

            expected = max(6, minute * 0.16)
            if total_shots >= expected:
                score += 15
            else:
                reasons.append(
                    f"Şut az "
                    f"(beklenen:{int(expected)}, olan:{total_shots})")

            total_dangerous = (self._safe_get(stats, 'home_dangerous') +
                                self._safe_get(stats, 'away_dangerous'))
            if total_dangerous >= 10:
                score += 15
            elif total_dangerous > 0:
                score += 5

            total_big = (self._safe_get(stats, 'home_big_chances') +
                         self._safe_get(stats, 'away_big_chances'))
            if total_big >= 2:
                score += 10

            return len(reasons) == 0, score, reasons
        except Exception as e:
            return False, 0, [f"İstatistik hatası: {e}"]

    # ─────────────────────────────────────────
    # AŞAMA 3: BASKI VE TREND
    # ─────────────────────────────────────────
    def _phase3_pressure_trend(self, stats, minute):
        try:
            reasons = []

            h_data = {
                'sot':       self._safe_get(stats, 'home_sot'),
                'shots':     self._safe_get(stats, 'home_shots'),
                'corners':   self._safe_get(stats, 'home_corners'),
                'poss':      self._safe_get(stats, 'home_poss', 50),
                'dangerous': self._safe_get(stats, 'home_dangerous'),
                'attacks':   self._safe_get(stats, 'home_attacks'),
                'saves':     self._safe_get(stats, 'away_saves'),
                'shots_box': self._safe_get(stats, 'home_shots_box'),
            }
            a_data = {
                'sot':       self._safe_get(stats, 'away_sot'),
                'shots':     self._safe_get(stats, 'away_shots'),
                'corners':   self._safe_get(stats, 'away_corners'),
                'poss':      self._safe_get(stats, 'away_poss', 50),
                'dangerous': self._safe_get(stats, 'away_dangerous'),
                'attacks':   self._safe_get(stats, 'away_attacks'),
                'saves':     self._safe_get(stats, 'home_saves'),
                'shots_box': self._safe_get(stats, 'away_shots_box'),
            }

            h_p = self._calculate_pressure(h_data, minute)
            a_p = self._calculate_pressure(a_data, minute)

            dominant       = None
            final_p        = 0
            corner_support = False

            pressure_diff = abs(h_p - a_p)
            if pressure_diff < self.MIN_PRESSURE_DIFF:
                reasons.append(f"Baskı farkı yetersiz ({pressure_diff})")

            if h_p > a_p and h_p >= self.MIN_PRESSURE:
                dominant = 'home'
                final_p  = h_p
                if h_data['sot'] <= a_data['sot']:
                    reasons.append("Baskı isabetli şuta yansımıyor")
                corner_support = h_data['corners'] > a_data['corners']

            elif a_p > h_p and a_p >= self.MIN_PRESSURE:
                dominant = 'away'
                final_p  = a_p
                if a_data['sot'] <= h_data['sot']:
                    reasons.append("Baskı isabetli şuta yansımıyor")
                corner_support = a_data['corners'] > h_data['corners']
            else:
                reasons.append(f"Yeterli baskı yok (Ev:{h_p} Dep:{a_p})")

            return (len(reasons) == 0, dominant, final_p,
                    h_p, a_p, corner_support, reasons)
        except Exception as e:
            return False, None, 0, 0, 0, False, [f"Baskı hatası: {e}"]

    # ─────────────────────────────────────────
    # AŞAMA 4: DEĞER ANALİZİ (GENİŞLETİLMİŞ)
    # ─────────────────────────────────────────
    def _phase4_value_analysis(self, m, stats, minute,
                                dominant, final_p, corner_support):
        default_return = [], "2. YARI", 0, 0.0, 0.0, 0.0
        try:
            if dominant == 'home':
                dom = {
                    'sot':     self._safe_get(stats, 'home_sot'),
                    'shots':   self._safe_get(stats, 'home_shots'),
                    'corners': self._safe_get(stats, 'home_corners'),
                    'poss':    self._safe_get(stats, 'home_poss', 50),
                    'dangerous': self._safe_get(stats, 'home_dangerous'),
                    'big_chances': self._safe_get(stats, 'home_big_chances'),
                }
                rec = {
                    'sot':     self._safe_get(stats, 'away_sot'),
                    'shots':   self._safe_get(stats, 'away_shots'),
                    'corners': self._safe_get(stats, 'away_corners'),
                    'poss':    self._safe_get(stats, 'away_poss', 50),
                }
                real_xg = self._safe_float(stats.get('home_xg', None), None)
            else:
                dom = {
                    'sot':     self._safe_get(stats, 'away_sot'),
                    'shots':   self._safe_get(stats, 'away_shots'),
                    'corners': self._safe_get(stats, 'away_corners'),
                    'poss':    self._safe_get(stats, 'away_poss', 50),
                    'dangerous': self._safe_get(stats, 'away_dangerous'),
                    'big_chances': self._safe_get(stats, 'away_big_chances'),
                }
                rec = {
                    'sot':     self._safe_get(stats, 'home_sot'),
                    'shots':   self._safe_get(stats, 'home_shots'),
                    'corners': self._safe_get(stats, 'home_corners'),
                    'poss':    self._safe_get(stats, 'home_poss', 50),
                }
                real_xg = self._safe_float(stats.get('away_xg', None), None)

            h_s        = self._safe_get_team(m, 'home', 'current')
            a_s        = self._safe_get_team(m, 'away', 'current')
            curr_score = h_s + a_s
            minute     = max(1, self._safe_int(minute, 1))
            period     = "1. YARI" if minute <= 45 else "2. YARI"
            
            dom_xg   = self._calculate_xg(dom['sot'], dom['shots'], dom['corners'], dom['poss'], minute, real_xg)
            rec_xg   = self._calculate_xg(rec['sot'], rec['shots'], rec['corners'], rec['poss'], minute)
            total_xg = round(dom_xg + rec_xg, 2)
            
            picks = []
            total_c = (self._safe_get(stats, 'home_corners') + self._safe_get(stats, 'away_corners'))

            # ═══════════════════════════════════════
            # 1. İLK YARI BAHİSLERİ (Dakika <= 40)
            # ═══════════════════════════════════════
            if minute <= 40 and curr_score <= 1:
                # İY 0.5 ÜST
                if dom['sot'] >= 2 and dom_xg >= 0.4:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'iy_ust', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE:
                        picks.append(("İY 0.5 ÜST", 1.70, "Düşük", v, "iy"))
                
                # İY 1.5 ÜST (Agresif)
                if dom['sot'] >= 3 and dom['shots'] >= 7 and dom_xg >= 0.7 and curr_score == 0:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'iy_ust_15', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE + 5:
                        picks.append(("İY 1.5 ÜST", 2.20, "Orta", v, "iy"))
                
                # İY KG VAR
                if total_xg >= 1.0 and rec['sot'] >= 1 and dom['sot'] >= 2:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'iy_kg', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE + 3:
                        picks.append(("İY KG VAR", 2.40, "Orta", v, "iy"))

            # ═══════════════════════════════════════
            # 2. MAÇ SONU BAHİSLERİ (Dakika >= 50)
            # ═══════════════════════════════════════
            if minute >= 50:
                # MS 2.5 ÜST
                if total_xg >= 1.8 and curr_score >= 1 and dom['sot'] >= 2:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'ms_25', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE:
                        picks.append(("MS 2.5 ÜST", 1.90, "Orta", v, "ms"))
                
                # MS 3.5 ÜST
                if total_xg >= 2.4 and curr_score >= 2 and (dom['sot'] + rec['sot']) >= 5:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'ms_35', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE + 8:
                        picks.append(("MS 3.5 ÜST", 2.60, "Yüksek", v, "ms"))

                # MS 0.5 ÜST (Güvenli)
                if curr_score == 0 and dom['sot'] >= 3 and minute < 75:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'ms_05', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE:
                        picks.append(("MS 0.5 ÜST", 1.35, "Çok Düşük", v, "ms"))

            # ═══════════════════════════════════════
            # 3. TAKIM BAZLI GOL BAHİSLERİ
            # ═══════════════════════════════════════
            if minute >= 60 and dom['sot'] >= 4 and dom_xg >= 1.2:
                team_code = "1" if dominant == 'home' else "2"
                v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'team_15', dom_xg, curr_score)
                if v >= self.MIN_VALUE_SCORE + 5:
                    picks.append((f"{team_code}T 1.5 ÜST", 2.10, "Orta", v, "team"))

            # ═══════════════════════════════════════
            # 4. KG VAR / YOK
            # ═══════════════════════════════════════
            if total_xg >= 1.6 and rec['sot'] >= 2 and dom['sot'] >= 2:
                if not (minute > 75 and curr_score == 0):
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'kg_var', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE + 5:
                        picks.append(("KG VAR", 1.85, "Orta", v, "kg"))

            # KG YOK (Defansif)
            if curr_score == 0 and minute > 60 and total_xg < 0.8 and dom['sot'] <= 2 and rec['sot'] <= 1:
                v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'kg_yok', dom_xg, curr_score)
                if v >= self.MIN_VALUE_SCORE + 10:
                    picks.append(("KG YOK", 2.80, "Yüksek", v, "kg"))

            # ═══════════════════════════════════════
            # 5. KORNER BAHİSLERİ
            # ═══════════════════════════════════════
            if minute > 50:
                corner_ratio = total_c / minute if minute > 0 else 0
                
                if minute <= 55 and total_c >= 6:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], total_c, minute, 'korner_iy', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE:
                        picks.append((f"İY Korner {total_c + 2.5} ÜST", 1.90, "Düşük", v, "corner"))
                
                if corner_ratio >= 0.22:
                    target = 10.5 if total_c >= 8 else 9.5
                    if total_c >= 10:
                        target = 11.5
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], total_c, minute, 'korner_ms', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE:
                        picks.append((f"Korner {target} ÜST", 1.85, "Orta", v, "corner"))

            # ═══════════════════════════════════════
            # 6. HANDIKAP / TARAF
            # ═══════════════════════════════════════
            if final_p >= 68 and minute > 60:
                dom_score = h_s if dominant == 'home' else a_s
                rec_score = a_s if dominant == 'home' else h_s
                
                if dom_score > rec_score and (dom_score - rec_score) == 1:
                    if dom['sot'] >= 4 and dom['poss'] >= 55:
                        v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'handicap', dom_xg, curr_score)
                        if v >= self.MIN_VALUE_SCORE + 8:
                            picks.append((f"Handikap -1 ({self._safe_team_name(m, dominant)})", 2.40, "Yüksek", v, "handicap"))
                
                if dom_score == rec_score and dom['sot'] >= 5:
                    team_code = "1" if dominant == 'home' else "2"
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'taraf', dom_xg, curr_score)
                    if v >= self.MIN_VALUE_SCORE + 5:
                        picks.append((f"MS {team_code}", 2.00, "Orta", v, "taraf"))

            return picks, period, curr_score, dom_xg, rec_xg, total_xg

        except Exception as e:
            return default_return

    # ─────────────────────────────────────────
    # ALTERNATİF SEÇİM SİSTEMİ
    # ─────────────────────────────────────────
    def _select_alternatives(self, picks, main_pick, stats, minute):
        """Ana bahise göre hedge ve çeşitlendirme önerileri"""
        if not picks or len(picks) <= 1:
            return []
        
        main_type = main_pick[4] if len(main_pick) > 4 else "unknown"
        alternatives = []
        
        for pick in picks:
            if pick[0] == main_pick[0]:
                continue
            
            pick_type = pick[4] if len(pick) > 4 else "unknown"
            
            # Hedge mantığı
            if main_type == "iy" and pick_type == "ms":
                alternatives.append(pick)
            elif main_type == "kg" and pick_type in ["ms", "iy"]:
                alternatives.append(pick)
            elif main_type in ["ms", "iy"] and pick_type in ["kg", "corner"]:
                alternatives.append(pick)
            elif main_type == "team" and pick_type in ["kg", "ms"]:
                alternatives.append(pick)
            elif pick[2] == "Düşük" and main_pick[2] != "Düşük":
                alternatives.append(pick)
        
        # En fazla 2 alternatif, farklı risk seviyelerinde
        final_alts = []
        seen_types = set()
        
        for alt in sorted(alternatives, key=lambda x: x[3], reverse=True):
            alt_type = alt[4] if len(alt) > 4 else "unknown"
            if alt_type not in seen_types and len(final_alts) < 2:
                final_alts.append(alt)
                seen_types.add(alt_type)
        
        return final_alts

    # ─────────────────────────────────────────
    # AŞAMA 5: ÇAPRAZ DOĞRULAMA
    # ─────────────────────────────────────────
    def _phase5_cross_validation(self, stats, dominant, picks, minute,
                                  corner_support, momentum, dom_xg,
                                  inconsistencies, score_issues):
        try:
            if not picks:
                return False, [], ["Geçerli bahis yok"]
            if inconsistencies:
                return False, [], inconsistencies
            if score_issues:
                return False, [], score_issues
            if momentum < self.MIN_MOMENTUM:
                return False, [], [f"Momentum düşük ({momentum})"]

            confirmations = []
            denials       = []
            minute        = max(1, self._safe_int(minute, 1))

            if dominant == 'home':
                dom_sot       = self._safe_get(stats, 'home_sot')
                rec_sot       = self._safe_get(stats, 'away_sot')
                dom_shots     = self._safe_get(stats, 'home_shots')
                dom_poss      = self._safe_get(stats, 'home_poss', 50)
                dom_dangerous = self._safe_get(stats, 'home_dangerous')
                rec_dangerous = self._safe_get(stats, 'away_dangerous')
                dom_big       = self._safe_get(stats, 'home_big_chances')
                dom_saves     = self._safe_get(stats, 'away_saves')
            else:
                dom_sot       = self._safe_get(stats, 'away_sot')
                rec_sot       = self._safe_get(stats, 'home_sot')
                dom_shots     = self._safe_get(stats, 'away_shots')
                dom_poss      = self._safe_get(stats, 'away_poss', 50)
                dom_dangerous = self._safe_get(stats, 'away_dangerous')
                rec_dangerous = self._safe_get(stats, 'home_dangerous')
                dom_big       = self._safe_get(stats, 'away_big_chances')
                dom_saves     = self._safe_get(stats, 'home_saves')

            # 1. İsabetli şut
            if dom_sot > rec_sot:
                confirmations.append("isabetli şut üstünlüğü")
            else:
                denials.append("isabetli şut dezavantajı")

            # 2. Top hakimiyeti
            if dom_poss >= 52:
                confirmations.append("top hakimiyeti")
            else:
                denials.append("top hakimiyeti yok")

            # 3. Korner
            if corner_support:
                confirmations.append("korner üstünlüğü")
            else:
                denials.append("korner dezavantajı")

            # 4. Şut yoğunluğu
            if (dom_shots / minute) * 90 >= 13:
                confirmations.append("yüksek şut yoğunluğu")
            else:
                denials.append("düşük şut yoğunluğu")

            # 5. Hücum kalitesi
            if dom_sot >= 3 and dom_shots >= 6:
                confirmations.append("yüksek hücum kalitesi")
            else:
                denials.append("hücum kalitesi yetersiz")

            # 6. xG
            if dom_xg >= self.MIN_XG_DOMINANT:
                confirmations.append(f"xG destekliyor ({dom_xg})")
            else:
                denials.append(f"xG düşük ({dom_xg})")

            # 7. Momentum
            if momentum >= self.MIN_MOMENTUM:
                confirmations.append(f"momentum pozitif ({momentum})")
            else:
                denials.append(f"momentum zayıf ({momentum})")

            # 8. Tehlikeli atak
            if dom_dangerous > rec_dangerous and dom_dangerous >= 5:
                confirmations.append(f"tehlikeli atak üstünlüğü ({dom_dangerous})")
            elif dom_dangerous > 0:
                denials.append("tehlikeli atak yetersiz")

            # 9. Büyük fırsat
            if dom_big >= 2:
                confirmations.append(f"büyük fırsat ({dom_big})")
            elif dom_big == 1:
                denials.append("büyük fırsat az (1)")

            # 10. Kaleci baskısı
            if dom_saves >= 3:
                confirmations.append(f"rakip kaleci zorlanıyor ({dom_saves})")

            return (len(confirmations) >= self.MIN_CONFIRMATIONS,
                    confirmations, denials)

        except Exception as e:
            return False, [], [f"Doğrulama hatası: {e}"]

    # ─────────────────────────────────────────
    # GÜVEN SKORU
    # ─────────────────────────────────────────
    def _calc_confidence(self, final_p, stats, dominant,
                          confirmations, xg):
        try:
            if dominant == 'home':
                sot_diff  = (self._safe_get(stats, 'home_sot') -
                             self._safe_get(stats, 'away_sot'))
                shot_diff = (self._safe_get(stats, 'home_shots') -
                             self._safe_get(stats, 'away_shots'))
            else:
                sot_diff  = (self._safe_get(stats, 'away_sot') -
                             self._safe_get(stats, 'home_sot'))
                shot_diff = (self._safe_get(stats, 'away_shots') -
                             self._safe_get(stats, 'home_shots'))

            xg   = max(0.0, self._safe_float(xg))
            prob = int(
                final_p * 0.50
                + min(abs(sot_diff)  * 4,   15)
                + min(abs(shot_diff) * 1.5,  8)
                + len(confirmations) * 3
                + min(xg * 8, 12)
            )
            prob = max(55, min(83, prob))

            if prob >= 78:
                conf = "🔥 ELITE"
            elif prob >= 70:
                conf = "⭐ ÇOK YÜKSEK"
            elif prob >= 62:
                conf = "✅ YÜKSEK"
            else:
                conf = "📊 ORTA"

            return prob, conf
        except:
            return 60, "📊 ORTA"

    # ─────────────────────────────────────────
    # ANA FONKSİYON (Genişletilmiş)
    # ─────────────────────────────────────────
    def analyze_advanced(self, m, stats, minute, odds_drop=0):
        try:
            minute = max(0, self._safe_int(minute, 0))

            # AŞAMA 1
            ok, reasons = self._phase1_prefilter(m, stats, minute)
            if not ok:
                return {"is_signal": False,
                        "reason": f"[A1] {', '.join(reasons)}"}

            # AŞAMA 2
            ok, _, reasons = self._phase2_stats_quality(stats, minute)
            if not ok:
                return {"is_signal": False,
                        "reason": f"[A2] {', '.join(reasons)}"}

            # AŞAMA 3
            (ok, dominant, final_p,
             h_p, a_p, corner_support, reasons) = \
                self._phase3_pressure_trend(stats, minute)
            if not ok or dominant is None:
                return {"is_signal": False,
                        "reason": f"[A3] {', '.join(reasons)}"}

            inconsistencies = self._check_inconsistency(stats, dominant)
            momentum        = self._calculate_momentum(
                stats, minute, dominant)

            # AŞAMA 4
            (picks, period, curr_score,
             dom_xg, rec_xg, total_xg) = \
                self._phase4_value_analysis(
                    m, stats, minute, dominant, final_p, corner_support)

            if not picks:
                return {"is_signal": False,
                        "reason": "[A4] Değer taşıyan bahis yok"}

            best = max(picks, key=lambda x: x[3])
            if best[1] < self.MIN_ODDS:
                return {"is_signal": False,
                        "reason": "[A4] Oran çok düşük"}

            h_s = self._safe_get_team(m, 'home', 'current')
            a_s = self._safe_get_team(m, 'away', 'current')
            score_issues = self._analyze_score_context(
                h_s, a_s, dominant, minute, best[0])

            # AŞAMA 5
            ok, confirmations, denials = self._phase5_cross_validation(
                stats, dominant, picks, minute, corner_support,
                momentum, dom_xg, inconsistencies, score_issues)
            if not ok:
                return {"is_signal": False,
                        "reason": f"[A5] {', '.join(denials[:2])}"}

            # Alternatif seçim
            alt_picks = self._select_alternatives(picks, best, stats, minute)

            total_c = (self._safe_get(stats, 'home_corners') +
                       self._safe_get(stats, 'away_corners'))
            target  = self._safe_team_name(m, dominant)
            prob, conf = self._calc_confidence(
                final_p, stats, dominant, confirmations, dom_xg)

            return {
                "is_signal":     True,
                "team":          target,
                "pressure":      final_p,
                "period":        period,
                "pick":          best[0],
                "pick_type":     best[4] if len(best) > 4 else "unknown",
                "confidence":    conf,
                "risk":          best[2],
                "prob":          prob,
                "alt":           [(p[0], p[1], p[2], p[4]) for p in alt_picks],
                "score":         f"{h_s}-{a_s}",
                "total_score":   curr_score,
                "confirmations": confirmations,
                "momentum":      momentum,
                "xg":            dom_xg,
                "total_c":       total_c,
                "value_score":   best[3],
            }

        except Exception as e:
            return {"is_signal": False,
                    "reason": f"[HATA] {str(e)}"}
