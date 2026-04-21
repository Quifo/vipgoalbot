import math, re

class BettingBrain:
    def __init__(self):
        self.MIN_MINUTE        = 25
        self.MAX_MINUTE        = 82
        self.MAX_TOTAL_GOALS   = 4
        self.MIN_TOTAL_SHOTS   = 10
        self.MIN_TOTAL_SOT     = 3
        self.MIN_SOT_RATIO     = 0.18
        self.MIN_PRESSURE      = 50
        self.MIN_PRESSURE_DIFF = 12
        self.MIN_ODDS          = 1.45  # bot tarafında gerçek odds ile uygulanıyor
        self.MIN_VALUE_SCORE   = 58
        self.MIN_CONFIRMATIONS = 3
        self.MIN_XG_DOMINANT   = 0.8
        self.MIN_XG_TOTAL      = 1.2
        self.MIN_MOMENTUM      = 55

        # (A) A2 score threshold
        self.MIN_A2_SCORE      = 55

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
    # Poisson helpers (E)
    # ─────────────────────────────────────────
    def _poisson_prob_ge_k(self, lam: float, k: int) -> float:
        lam = max(0.0, float(lam))
        k = int(k)
        if k <= 0:
            return 1.0
        s = 0.0
        for i in range(k):
            s += math.exp(-lam) * (lam ** i) / math.factorial(i)
        return max(0.0, min(1.0, 1.0 - s))

    def _estimate_over_prob(self, total_xg: float, minute: int, scope: str,
                            curr_score: int, line: float) -> int:
        minute = max(1, int(minute))
        total_xg = max(0.0, float(total_xg))

        target_goals = int(line + 0.5)  # 0.5->1, 1.5->2, 2.5->3
        needed = max(0, target_goals - curr_score)

        if scope == "İY":
            remaining = max(0, 45 - minute)
        else:
            remaining = max(0, 90 - minute)

        if remaining <= 0:
            return 0

        rate = total_xg / minute
        lam = rate * remaining
        return int(self._poisson_prob_ge_k(lam, needed) * 100)

    def _model_prob_for_pick(self, pick: str, minute: int, curr_score: int,
                             total_xg: float, dom_xg: float, rec_xg: float) -> int | None:
        try:
            pick_u = str(pick).upper().strip()
            minute = max(1, int(minute))

            # MS/İY ÜST
            m = re.search(r"^(MS|İY|IY)\s+(\d+(?:\.\d+)?)\s+ÜST$", pick_u)
            if m:
                scope = "İY" if m.group(1) in ("İY", "IY") else "MS"
                line = float(m.group(2))
                return self._estimate_over_prob(total_xg, minute, scope, curr_score, line)

            # KG VAR (basit bağımsız Poisson yaklaşımı)
            if pick_u == "KG VAR":
                remaining = max(0, 90 - minute)
                if remaining <= 0:
                    return 0

                rate_dom = max(0.0, float(dom_xg)) / minute
                rate_rec = max(0.0, float(rec_xg)) / minute
                lam_dom = rate_dom * remaining
                lam_rec = rate_rec * remaining

                p_dom = 1.0 - math.exp(-lam_dom)
                p_rec = 1.0 - math.exp(-lam_rec)

                return int(max(0.0, min(1.0, p_dom * p_rec)) * 100)

            return None
        except:
            return None

    # ─────────────────────────────────────────
    # xG HESAPLAMA
    # ─────────────────────────────────────────
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
                min(35, (shots     / minute) * 90 * 2.2) +
                min(30, (sot       / minute) * 90 * 6.5) +
                min(15, (corners   / minute) * 90 * 2.0) +
                min(10, max(0, poss - 50) * 0.5)         +
                min(10, (dangerous / minute) * 90 * 0.8)
            )

            if 65 <= minute <= 80:
                momentum *= 1.15
            elif minute > 80:
                momentum *= 0.85

            return min(100, int(momentum))
        except:
            return 0

    # ─────────────────────────────────────────
    # TUTARSIZLIK DEDEKTÖRü
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
            if minute > 78 and (h_s + a_s) == 0 and 'ÜST' in str(pick):
                issues.append("Geç dakika 0-0 ÜST riskli")

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
    # DEĞER SKORU
    # ─────────────────────────────────────────
    def _calc_value_score(self, pressure, sot, shots, corners, minute, bet_type, xg=0.0):
        try:
            pressure  = max(0, self._safe_int(pressure))
            sot       = max(0, self._safe_int(sot))
            shots     = max(0, self._safe_int(shots))
            corners   = max(0, self._safe_int(corners))
            minute    = max(1, self._safe_int(minute, 1))
            xg        = max(0.0, self._safe_float(xg))

            base     = pressure * 0.45
            xg_bonus = min(xg * 15, 20)

            if bet_type in ['iy_ust', 'ms_ust_0', 'ms_ust_n']:
                return min(100, int(
                    base
                    + min(sot     * 7,   28)
                    + min(shots   * 1.8, 12)
                    + min(corners * 1.2,  8)
                    + (10 if 55 <= minute <= 75 else 5 if 45 <= minute <= 80 else 0)
                    + xg_bonus
                ))
            elif bet_type == 'kg_var':
                return min(100, int(base + min(sot * 6, 22) + xg_bonus))
            elif bet_type == 'korner':
                return min(100, int(base * 0.65 + min(corners * 4.5, 30) + xg_bonus * 0.5))
            elif bet_type == 'taraf':
                return min(100, int(pressure * 0.85 + xg_bonus))
            return 0
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

            # (B) kırmızı kart filtresi
            home_red = self._safe_int(stats.get("home_red", 0))
            away_red = self._safe_int(stats.get("away_red", 0))
            if home_red + away_red > 0:
                reasons.append(f"Kırmızı kart (H:{home_red} A:{away_red})")

            return len(reasons) == 0, reasons
        except Exception as e:
            return False, [f"Ön filtre hatası: {e}"]

    # ─────────────────────────────────────────
    # AŞAMA 2: İSTATİSTİK KALİTESİ (A)
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
                    reasons.append(f"Şut kalitesi düşük (%{int(ratio * 100)})")
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
                reasons.append(f"Şut az (beklenen:{int(expected)}, olan:{total_shots})")

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

            ok = score >= self.MIN_A2_SCORE
            return ok, score, reasons
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

            return (len(reasons) == 0, dominant, final_p, h_p, a_p, corner_support, reasons)
        except Exception as e:
            return False, None, 0, 0, 0, False, [f"Baskı hatası: {e}"]

    # ─────────────────────────────────────────
    # AŞAMA 4: DEĞER ANALİZİ / PICK ÜRETİMİ
    # ─────────────────────────────────────────
    def _phase4_value_analysis(self, m, stats, minute, dominant, final_p, corner_support):
        default_return = [], "2. YARI", 0, 0.0, 0.0, 0.0
        try:
            if dominant == 'home':
                dom = {
                    'sot':     self._safe_get(stats, 'home_sot'),
                    'shots':   self._safe_get(stats, 'home_shots'),
                    'corners': self._safe_get(stats, 'home_corners'),
                    'poss':    self._safe_get(stats, 'home_poss', 50),
                }
                rec = {
                    'sot':     self._safe_get(stats, 'away_sot'),
                    'shots':   self._safe_get(stats, 'away_shots'),
                    'corners': self._safe_get(stats, 'away_corners'),
                    'poss':    self._safe_get(stats, 'away_poss', 50),
                }
                real_xg = self._safe_float(stats.get('home_xg', None), None)
            elif dominant == 'away':
                dom = {
                    'sot':     self._safe_get(stats, 'away_sot'),
                    'shots':   self._safe_get(stats, 'away_shots'),
                    'corners': self._safe_get(stats, 'away_corners'),
                    'poss':    self._safe_get(stats, 'away_poss', 50),
                }
                rec = {
                    'sot':     self._safe_get(stats, 'home_sot'),
                    'shots':   self._safe_get(stats, 'home_shots'),
                    'corners': self._safe_get(stats, 'home_corners'),
                    'poss':    self._safe_get(stats, 'home_poss', 50),
                }
                real_xg = self._safe_float(stats.get('away_xg', None), None)
            else:
                return default_return

            h_s        = self._safe_get_team(m, 'home', 'current')
            a_s        = self._safe_get_team(m, 'away', 'current')
            curr_score = h_s + a_s
            minute     = max(1, self._safe_int(minute, 1))
            period     = "1. YARI" if minute <= 45 else "2. YARI"

            dom_xg   = self._calculate_xg(dom['sot'], dom['shots'], dom['corners'], dom['poss'], minute, real_xg)
            rec_xg   = self._calculate_xg(rec['sot'], rec['shots'], rec['corners'], rec['poss'], minute)
            total_xg = round(dom_xg + rec_xg, 2)

            picks = []

            # ÜST
            if dom_xg >= self.MIN_XG_DOMINANT:
                if minute <= 42:
                    if curr_score == 0 and dom['sot'] >= 2:
                        v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'iy_ust', dom_xg)
                        if v >= self.MIN_VALUE_SCORE:
                            picks.append(("İY 0.5 ÜST", 0, "Düşük", v))
                else:
                    if curr_score == 0 and dom['sot'] >= 3:
                        v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'ms_ust_0', dom_xg)
                        if v >= self.MIN_VALUE_SCORE:
                            picks.append(("MS 0.5 ÜST", 0, "Düşük", v))
                    elif curr_score >= 1 and dom['sot'] >= 2:
                        v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'ms_ust_n', dom_xg)
                        if v >= self.MIN_VALUE_SCORE:
                            picks.append((f"MS {curr_score + 0.5} ÜST", 0, "Düşük", v))

            # Daha agresif ÜST (çeşitlendirme)
            if minute <= 78 and total_xg >= (self.MIN_XG_TOTAL + 0.8) and final_p >= 70:
                high_line = curr_score + 1.5
                if dom['sot'] >= 4 and dom['shots'] >= 9:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'ms_ust_n', dom_xg)
                    if v >= self.MIN_VALUE_SCORE + 6:
                        picks.append((f"MS {high_line} ÜST", 0, "Orta", v))

            # KG VAR
            if total_xg >= self.MIN_XG_TOTAL:
                kg_min = 2 if minute > 60 else 3
                if dom['sot'] >= kg_min and rec['sot'] >= 2:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'kg_var', dom_xg)
                    if v >= self.MIN_VALUE_SCORE + 8:
                        picks.append(("KG VAR", 0, "Orta", v))

            # KORNER
            total_c = (self._safe_get(stats, 'home_corners') + self._safe_get(stats, 'away_corners'))
            if minute > 52 and total_c >= 8 and corner_support:
                if (total_c / minute) >= 0.13:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'korner', dom_xg)
                    if v >= self.MIN_VALUE_SCORE:
                        picks.append((f"KORNER {total_c + 1.5} ÜST", 0, "Orta", v))

            # TARAF (1X2)
            if final_p >= 72 and minute > 55:
                dom_score = h_s if dominant == 'home' else a_s
                rec_score = a_s if dominant == 'home' else h_s
                if dom_score <= rec_score:
                    v = self._calc_value_score(final_p, dom['sot'], dom['shots'], dom['corners'], minute, 'taraf', dom_xg)
                    if v >= self.MIN_VALUE_SCORE + 5:
                        label = ("MS 1Ç" if dominant == 'home' else "MS 2Ç")
                        picks.append((label, 0, "Orta", v))

            return picks, period, curr_score, dom_xg, rec_xg, total_xg

        except:
            return default_return

    # ─────────────────────────────────────────
    # AŞAMA 5: ÇAPRAZ DOĞRULAMA
    # ─────────────────────────────────────────
    def _phase5_cross_validation(self, stats, dominant, picks, minute, corner_support, momentum,
                                  dom_xg, inconsistencies, score_issues):
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

            if dom_sot > rec_sot:
                confirmations.append("isabetli şut üstünlüğü")
            else:
                denials.append("isabetli şut dezavantajı")

            if dom_poss >= 52:
                confirmations.append("top hakimiyeti")
            else:
                denials.append("top hakimiyeti yok")

            if corner_support:
                confirmations.append("korner üstünlüğü")
            else:
                denials.append("korner dezavantajı")

            if (dom_shots / minute) * 90 >= 13:
                confirmations.append("yüksek şut yoğunluğu")
            else:
                denials.append("düşük şut yoğunluğu")

            if dom_sot >= 3 and dom_shots >= 6:
                confirmations.append("yüksek hücum kalitesi")
            else:
                denials.append("hücum kalitesi yetersiz")

            if dom_xg >= self.MIN_XG_DOMINANT:
                confirmations.append(f"xG destekliyor ({dom_xg})")
            else:
                denials.append(f"xG düşük ({dom_xg})")

            if momentum >= self.MIN_MOMENTUM:
                confirmations.append(f"momentum pozitif ({momentum})")
            else:
                denials.append(f"momentum zayıf ({momentum})")

            if dom_dangerous > rec_dangerous and dom_dangerous >= 5:
                confirmations.append(f"tehlikeli atak üstünlüğü ({dom_dangerous})")
            elif dom_dangerous > 0:
                denials.append("tehlikeli atak yetersiz")

            if dom_big >= 2:
                confirmations.append(f"büyük fırsat ({dom_big})")
            elif dom_big == 1:
                denials.append("büyük fırsat az (1)")

            if dom_saves >= 3:
                confirmations.append(f"rakip kaleci zorlanıyor ({dom_saves})")

            return (len(confirmations) >= self.MIN_CONFIRMATIONS, confirmations, denials)

        except Exception as e:
            return False, [], [f"Doğrulama hatası: {e}"]

    # ─────────────────────────────────────────
    # GÜVEN SKORU
    # ─────────────────────────────────────────
    def _calc_confidence(self, final_p, stats, dominant, confirmations, xg):
        try:
            if dominant == 'home':
                sot_diff  = (self._safe_get(stats, 'home_sot') - self._safe_get(stats, 'away_sot'))
                shot_diff = (self._safe_get(stats, 'home_shots') - self._safe_get(stats, 'away_shots'))
            else:
                sot_diff  = (self._safe_get(stats, 'away_sot') - self._safe_get(stats, 'home_sot'))
                shot_diff = (self._safe_get(stats, 'away_shots') - self._safe_get(stats, 'home_shots'))

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
    # ANA FONKSİYON
    # ─────────────────────────────────────────
    def analyze_advanced(self, m, stats, minute, odds_drop=0, trend=None):
        try:
            minute = max(0, self._safe_int(minute, 0))

            # A1
            ok, reasons = self._phase1_prefilter(m, stats, minute)
            if not ok:
                return {"is_signal": False, "reason": f"[A1] {', '.join(reasons)}"}

            # A2 (A)
            ok, a2_score, reasons = self._phase2_stats_quality(stats, minute)
            if not ok:
                return {"is_signal": False, "reason": f"[A2] score:{a2_score} | " + ", ".join(reasons[:3])}

            # A3
            (ok, dominant, final_p, h_p, a_p, corner_support, reasons) = self._phase3_pressure_trend(stats, minute)
            if not ok or dominant is None:
                return {"is_signal": False, "reason": f"[A3] {', '.join(reasons)}"}

            inconsistencies = self._check_inconsistency(stats, dominant)
            momentum        = self._calculate_momentum(stats, minute, dominant)

            # A4
            (picks, period, curr_score, dom_xg, rec_xg, total_xg) = self._phase4_value_analysis(
                m, stats, minute, dominant, final_p, corner_support
            )
            if not picks:
                return {"is_signal": False, "reason": "[A4] Değer taşıyan bahis yok"}

            best = max(picks, key=lambda x: x[3])

            h_s = self._safe_get_team(m, 'home', 'current')
            a_s = self._safe_get_team(m, 'away', 'current')
            score_issues = self._analyze_score_context(h_s, a_s, dominant, minute, best[0])

            # A5
            ok, confirmations, denials = self._phase5_cross_validation(
                stats, dominant, picks, minute, corner_support,
                momentum, dom_xg, inconsistencies, score_issues
            )
            if not ok:
                return {"is_signal": False, "reason": f"[A5] {', '.join(denials[:2])}"}

            # (D) trend confirmation
            if isinstance(trend, dict) and trend.get("trend_ok"):
                spm = float(trend.get("shots_per_min", 0.0))
                sotpm = float(trend.get("sot_per_min", 0.0))
                if spm >= 0.6 or sotpm >= 0.12:
                    confirmations.append(f"trend pozitif (Δşut/dk:{spm}, ΔSOT/dk:{sotpm})")

            # pick bazlı model prob map (E)
            pick_probs = {}
            for p in picks:
                label = p[0]
                mp = self._model_prob_for_pick(label, minute, curr_score, total_xg, dom_xg, rec_xg)
                if mp is not None:
                    pick_probs[label] = mp

            target = self._safe_team_name(m, dominant)

            prob, conf = self._calc_confidence(final_p, stats, dominant, confirmations, dom_xg)

            # seçilen pick için model prob varsa harmanla
            best_mp = pick_probs.get(best[0])
            if best_mp is not None:
                prob = int(prob * 0.65 + best_mp * 0.35)

            total_c = (self._safe_get(stats, 'home_corners') + self._safe_get(stats, 'away_corners'))

            return {
                "is_signal":     True,
                "team":          target,
                "pressure":      final_p,
                "period":        period,
                "pick":          best[0],
                "confidence":    conf,
                "risk":          best[2],
                "prob":          prob,
                "alt":           [(p[0], p[1], p[2], pick_probs.get(p[0])) for p in picks],
                "pick_probs":    pick_probs,
                "score":         f"{h_s}-{a_s}",
                "total_score":   curr_score,
                "confirmations": confirmations,
                "momentum":      momentum,
                "xg":            dom_xg,
                "total_c":       total_c,
                "value_score":   best[3],
                "a2_score":      a2_score,
                "total_xg":      total_xg,
            }

        except Exception as e:
            return {"is_signal": False, "reason": f"[HATA] {str(e)}"}
