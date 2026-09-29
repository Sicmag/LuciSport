# =============================================================
#  LUCI SPORT 4.5 — sin SofaScore
# =============================================================
import os, math, time, sqlite3, requests, json, hashlib
from datetime import datetime, timedelta, timezone

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill
    EXCEL_OK = True
except ImportError:
    EXCEL_OK = False

from config import (
    FOOTBALL_API_KEY, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    ODDS_API_KEYS, HEADERS,
    PROB_MINIMA, CUOTA_MINIMA, MAX_PICKS_PARTIDO,
    KELLY_FRACTION, KELLY_CAP, BANKROLL_INICIAL,
)

ODDS_BASE       = "https://api.the-odds-api.com/v4"
ODDS_REGIONS    = "eu"
ODDS_MARKETS    = "h2h,totals"
ODDS_CACHE_TTL_MIN = 60

DB_PATH            = "lucisport.db"
EXCEL_PATH         = "predicciones.xlsx"
CACHE_TTL_HORAS    = 12

SOSPECHA_PROB      = 88.0
SOSPECHA_CUOTA     = 1.30

MIN_MUESTRAS_CALIB = 8
FACTOR_CALIB_MIN   = 0.85
FACTOR_CALIB_MAX   = 1.15

LINEAS        = [0.5, 1.5, 2.5, 3.5, 4.5]
LINEAS_EQUIPO = [0.5, 1.5, 2.5]

G, Y, R, B, C, W = '\033[92m','\033[93m','\033[91m','\033[94m','\033[96m','\033[0m'
BOLD = '\033[1m'


# =============================================================
#  1. BASE DE DATOS
# =============================================================
def init_db():
    con = sqlite3.connect(DB_PATH); cur = con.cursor()
    cur.executescript("""
    CREATE TABLE IF NOT EXISTS team_stats_cache (
        team_id     INTEGER PRIMARY KEY,
        payload     TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );
    CREATE TABLE IF NOT EXISTS predictions (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id    TEXT NOT NULL,
        match_date  TEXT,
        home_id     INTEGER,
        away_id     INTEGER,
        home        TEXT NOT NULL,
        away        TEXT NOT NULL,
        market      TEXT NOT NULL,
        selection   TEXT NOT NULL,
        prob_model  REAL NOT NULL,
        fair_odd    REAL,
        cuota_real  REAL,
        edge_pct    REAL,
        stake_pct   REAL,
        result      TEXT DEFAULT 'PENDING',
        ft_home     INTEGER,
        ft_away     INTEGER,
        liga        TEXT,
        created_at  TEXT NOT NULL,
        resolved_at TEXT,
        UNIQUE(match_id, market, selection)
    );
    CREATE TABLE IF NOT EXISTS market_performance (
        market      TEXT PRIMARY KEY,
        wins        INTEGER DEFAULT 0,
        losses      INTEGER DEFAULT 0,
        voids       INTEGER DEFAULT 0,
        updated_at  TEXT
    );
    CREATE INDEX IF NOT EXISTS idx_pred_result ON predictions(result);
    CREATE INDEX IF NOT EXISTS idx_pred_match  ON predictions(match_id);
    """)
    con.commit(); con.close()


def migrate_db():
    con = db(); cur = con.cursor()
    cur.execute("PRAGMA table_info(predictions)")
    cols = [r[1] for r in cur.fetchall()]
    nuevos = [
        ("liga", "TEXT"), ("match_date", "TEXT"),
        ("home_id", "INTEGER"), ("away_id", "INTEGER"),
        ("ft_home", "INTEGER"), ("ft_away", "INTEGER"),
        ("resolved_at", "TEXT"), ("cuota_real", "REAL"),
        ("edge_pct", "REAL"),
    ]
    for col, tipo in nuevos:
        if col not in cols:
            cur.execute(f"ALTER TABLE predictions ADD COLUMN {col} {tipo}")
    con.commit(); con.close()


def db():
    return sqlite3.connect(DB_PATH)


def _utcnow_iso():
    return datetime.now(timezone.utc).isoformat()


def cache_get(team_id):
    con = db(); cur = con.cursor()
    cur.execute("SELECT payload, updated_at FROM team_stats_cache WHERE team_id=?", (team_id,))
    row = cur.fetchone(); con.close()
    if not row: return None
    try:
        ts = datetime.fromisoformat(row[1])
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - ts > timedelta(hours=CACHE_TTL_HORAS):
            return None
    except Exception:
        return None
    return json.loads(row[0])


def cache_set(team_id, payload):
    con = db(); cur = con.cursor()
    cur.execute("""INSERT INTO team_stats_cache(team_id, payload, updated_at)
                   VALUES(?,?,?)
                   ON CONFLICT(team_id) DO UPDATE SET
                     payload=excluded.payload, updated_at=excluded.updated_at""",
                (team_id, json.dumps(payload), _utcnow_iso()))
    con.commit(); con.close()


def save_prediction(match, market, selection, prob, fair_odd, stake_pct,
                    cuota_real=None, edge_pct=None):
    match_id = (f"{match['homeTeam']['id']}_{match['awayTeam']['id']}_"
                f"{match.get('utcDate','')}")
    liga = match.get('league_code', '')
    con = db(); cur = con.cursor()
    cur.execute("""INSERT INTO predictions
        (match_id, match_date, home_id, away_id, home, away, market, selection,
         prob_model, fair_odd, cuota_real, edge_pct, stake_pct, liga, created_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(match_id, market, selection) DO UPDATE SET
          prob_model=excluded.prob_model,
          fair_odd=excluded.fair_odd,
          cuota_real=COALESCE(excluded.cuota_real, predictions.cuota_real),
          edge_pct=COALESCE(excluded.edge_pct, predictions.edge_pct),
          stake_pct=excluded.stake_pct,
          liga=excluded.liga""",
        (match_id, match.get('utcDate'), match['homeTeam']['id'], match['awayTeam']['id'],
         match['homeTeam']['name'], match['awayTeam']['name'],
         market, selection, prob, fair_odd, cuota_real, edge_pct, stake_pct, liga,
         _utcnow_iso()))
    con.commit(); con.close()
    return match_id


# =============================================================
#  2. MODELO
# =============================================================
def poisson(lam, k):
    if lam <= 0: return 1.0 if k == 0 else 0.0
    return (lam**k * math.exp(-lam)) / math.factorial(k)


def poisson_cdf(lam, x_max):
    return sum(poisson(lam, k) for k in range(0, x_max + 1))


def dc_tau(i, j, lam_h, lam_a, rho=-0.10):
    if i == 0 and j == 0: return 1 - lam_h*lam_a*rho
    if i == 0 and j == 1: return 1 + lam_h*rho
    if i == 1 and j == 0: return 1 + lam_a*rho
    if i == 1 and j == 1: return 1 - rho
    return 1.0


def score_matrix(lam_h, lam_a, max_g=8):
    m = [[poisson(lam_h, i)*poisson(lam_a, j)*dc_tau(i,j,lam_h,lam_a)
          for j in range(max_g+1)] for i in range(max_g+1)]
    s = sum(sum(r) for r in m)
    return [[v/s for v in row] for row in m]


def over_under(lam, linea):
    x = int(math.floor(linea))
    p_under = poisson_cdf(lam, x) * 100
    return round(100 - p_under), round(p_under)


def kelly_stake(prob_pct, odd, bankroll):
    p = prob_pct / 100.0; b = odd - 1
    if b <= 0: return 0.0
    f = (p*b - (1-p)) / b
    if f <= 0: return 0.0
    return round(bankroll * min(f * KELLY_FRACTION, KELLY_CAP), 2)


# =============================================================
#  3. CALIBRACIÓN
# =============================================================
def get_calibration(market):
    con = db(); cur = con.cursor()
    cur.execute("SELECT wins, losses FROM market_performance WHERE market=?", (market,))
    row = cur.fetchone(); con.close()
    if not row: return 1.0
    wins, losses = row
    total = wins + losses
    if total < MIN_MUESTRAS_CALIB: return 1.0
    hit = wins / total
    factor = 0.7 + (hit * 0.6)
    return max(FACTOR_CALIB_MIN, min(FACTOR_CALIB_MAX, factor))


def update_market_performance(market, result):
    con = db(); cur = con.cursor()
    cur.execute("INSERT OR IGNORE INTO market_performance(market) VALUES(?)", (market,))
    if result == "WON":
        cur.execute("UPDATE market_performance SET wins=wins+1, updated_at=? WHERE market=?",
                    (_utcnow_iso(), market))
    elif result == "LOST":
        cur.execute("UPDATE market_performance SET losses=losses+1, updated_at=? WHERE market=?",
                    (_utcnow_iso(), market))
    elif result == "VOID":
        cur.execute("UPDATE market_performance SET voids=voids+1, updated_at=? WHERE market=?",
                    (_utcnow_iso(), market))
    con.commit(); con.close()


# =============================================================
#  4. TELEGRAM
# =============================================================
def enviar_a_telegram(mensaje):
    if not TELEGRAM_TOKEN or "PON_AQUI" in str(TELEGRAM_TOKEN):
        return False
    if not TELEGRAM_CHAT_ID or "PON_AQUI" in str(TELEGRAM_CHAT_ID):
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        return r.json().get("ok", False)
    except Exception:
        return False


# =============================================================
#  5. DATOS DE API (football-data.org)
# =============================================================
_LAST_CALL = [0.0]


def _rate_limit(min_interval=1.5):
    """Rate limit reducido a 1.5s. Con caché persistente solo se llama
    1 vez por equipo cada 12h."""
    elapsed = time.time() - _LAST_CALL[0]
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _LAST_CALL[0] = time.time()


def get_deep_data(team_id):
    cached = cache_get(team_id)
    if cached: return cached
    _rate_limit()
    url = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=20"
    default = {"gf":1.2,"gc":1.1,"gf_1t":1.2*0.44,"gc_1t":1.1*0.44,
               "gf_2t":1.2*0.56,"gc_2t":1.1*0.56}
    try:
        r = requests.get(url, headers=HEADERS, timeout=10).json()
        matches = r.get("matches", [])
        if not matches:
            cache_set(team_id, default); return default
        gf=gc=gf1=gc1=0; n=len(matches)
        for m in matches:
            local = m['homeTeam']['id'] == team_id
            fth = m['score']['fullTime']['home'] or 0
            fta = m['score']['fullTime']['away'] or 0
            ht = m['score'].get('halfTime') or {}
            hth, hta = ht.get('home'), ht.get('away')
            if local:
                gf+=fth; gc+=fta
                gf1 += hth if hth is not None else fth*0.44
                gc1 += hta if hta is not None else fta*0.44
            else:
                gf+=fta; gc+=fth
                gf1 += hta if hta is not None else fta*0.44
                gc1 += hth if hth is not None else fth*0.44
        out = {"gf":gf/n,"gc":gc/n,"gf_1t":gf1/n,"gc_1t":gc1/n,
               "gf_2t":max((gf-gf1)/n,0.01),"gc_2t":max((gc-gc1)/n,0.01)}
        cache_set(team_id, out); return out
    except (requests.RequestException, KeyError, TypeError):
        return default


# =============================================================
#  5b. CUOTAS REALES (The Odds API)
# =============================================================
_ODDS_CACHE = {}

SPORT_KEYS = {
    "PL":  "soccer_epl",
    "PD":  "soccer_spain_la_liga",
    "SA":  "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "BSA": "soccer_brazil_campeonato",
    "ELC": "soccer_efl_champ",
    "CL":  "soccer_uefa_champs_league",
    "ELI": "soccer_uefa_europa_league",
    "CLI": "soccer_conmebol_libertadores",
}

ODDS_KEY_INDEX = [0]
ODDS_KEY_DEAD  = [False] * max(len(ODDS_API_KEYS), 1)


def _get_odds_key():
    for i in range(len(ODDS_API_KEYS)):
        idx = (ODDS_KEY_INDEX[0] + i) % len(ODDS_API_KEYS)
        if not ODDS_KEY_DEAD[idx]:
            ODDS_KEY_INDEX[0] = idx
            return ODDS_API_KEYS[idx], idx
    return None, -1


def _rotate_odds_key(current_idx):
    if 0 <= current_idx < len(ODDS_KEY_DEAD):
        ODDS_KEY_DEAD[current_idx] = True
    for i in range(1, len(ODDS_API_KEYS) + 1):
        nxt = (current_idx + i) % len(ODDS_API_KEYS)
        if not ODDS_KEY_DEAD[nxt]:
            ODDS_KEY_INDEX[0] = nxt
            return True
    return False


def _normalize_name(name):
    n = (name or "").lower().strip()
    for suf in [' cf',' fc',' balompié',' balompie',' sc',' ac',' club',
                ' de futbol',' de fútbol',' athletic',' atletico',' atlético',
                ' real',' sporting',' deportivo',' ud',' cd',' de madrid']:
        n = n.replace(suf, '')
    return n.strip()


def _fetch_odds_liga(sport_key):
    ahora = time.time()
    if sport_key in _ODDS_CACHE:
        ts, datos = _ODDS_CACHE[sport_key]
        if ahora - ts < ODDS_CACHE_TTL_MIN * 60:
            return datos

    key, idx = _get_odds_key()
    if not key:
        _ODDS_CACHE[sport_key] = (ahora, [])
        return []

    try:
        url = f"{ODDS_BASE}/sports/{sport_key}/odds/"
        params = {"apiKey": key, "regions": ODDS_REGIONS,
                  "markets": ODDS_MARKETS, "oddsFormat": "decimal"}
        r = requests.get(url, params=params, timeout=15)
        restantes = r.headers.get("x-requests-remaining", "?")
        try:
            restantes_int = int(restantes)
        except (ValueError, TypeError):
            restantes_int = None

        if restantes_int is not None and restantes_int <= 0:
            if _rotate_odds_key(idx):
                return _fetch_odds_liga(sport_key)
            _ODDS_CACHE[sport_key] = (ahora, [])
            return []

        if r.status_code == 429:
            if _rotate_odds_key(idx):
                return _fetch_odds_liga(sport_key)
            _ODDS_CACHE[sport_key] = (ahora, [])
            return []

        if r.status_code != 200:
            _ODDS_CACHE[sport_key] = (ahora, [])
            return []

        datos = r.json()
        _ODDS_CACHE[sport_key] = (ahora, datos)
        return datos
    except Exception:
        _ODDS_CACHE[sport_key] = (ahora, [])
        return []


def get_cuotas_reales(match):
    if not ODDS_API_KEYS:
        return {}
    liga = match.get("league_code", "")
    sport_key = SPORT_KEYS.get(liga)
    if not sport_key:
        return {}

    eventos = _fetch_odds_liga(sport_key)
    if not eventos:
        return {}

    home = match['homeTeam']['name']
    away = match['awayTeam']['name']
    h_norm = _normalize_name(home)
    a_norm = _normalize_name(away)

    evento = None
    for ev in eventos:
        ev_h = _normalize_name(ev.get("home_team", ""))
        ev_a = _normalize_name(ev.get("away_team", ""))
        if ((h_norm in ev_h or ev_h in h_norm) and
            (a_norm in ev_a or ev_a in a_norm)):
            evento = ev
            break

    if not evento:
        return {}

    home_api = evento.get("home_team", "")
    away_api = evento.get("away_team", "")
    cuotas_h2h = {"home": [], "draw": [], "away": []}
    cuotas_ou = {}

    for bk in evento.get("bookmakers", []):
        for mkt in bk.get("markets", []):
            key = mkt.get("key", "")
            if key == "h2h":
                for o in mkt.get("outcomes", []):
                    nm = o.get("name", "")
                    price = o.get("price")
                    if not price: continue
                    if nm == "Draw":
                        cuotas_h2h["draw"].append(float(price))
                    elif nm == home_api:
                        cuotas_h2h["home"].append(float(price))
                    elif nm == away_api:
                        cuotas_h2h["away"].append(float(price))
            elif key == "totals":
                for o in mkt.get("outcomes", []):
                    pt = str(o.get("point", "")).strip()
                    nm = o.get("name", "")
                    price = o.get("price")
                    if not price or not pt: continue
                    cuotas_ou.setdefault(pt, {"Over": [], "Under": []})
                    if nm in ("Over", "Under"):
                        cuotas_ou[pt][nm].append(float(price))

    def _avg(lst):
        return round(sum(lst)/len(lst), 2) if lst else 0

    resultado = {"1X2": {}, "Goles totales": {}}
    if cuotas_h2h["home"]:
        resultado["1X2"]["Gana " + home] = _avg(cuotas_h2h["home"])
    if cuotas_h2h["away"]:
        resultado["1X2"]["Gana " + away] = _avg(cuotas_h2h["away"])
    if cuotas_h2h["draw"]:
        resultado["1X2"]["Empate"] = _avg(cuotas_h2h["draw"])
    for pt, d in cuotas_ou.items():
        if d["Over"]:  resultado["Goles totales"]["+" + pt] = _avg(d["Over"])
        if d["Under"]: resultado["Goles totales"]["-" + pt] = _avg(d["Under"])

    return {k: v for k, v in resultado.items() if v}


# =============================================================
#  6. EVALUACIÓN (para resolver apuestas)
# =============================================================
def evaluar_seleccion(market, selection, home_name, away_name, ft_h, ft_a, ht_h, ht_a):
    sel = selection
    total = ft_h + ft_a

    if market == "BTTS":
        return "WON" if (ft_h > 0 and ft_a > 0) else "LOST"

    if market == "1X2":
        if sel.startswith("Gana "):
            team = sel[5:].strip()
            if team == home_name: return "WON" if ft_h > ft_a else "LOST"
            if team == away_name: return "WON" if ft_a > ft_h else "LOST"
        if sel.startswith("1X "): return "WON" if ft_h >= ft_a else "LOST"
        if sel.startswith("X2 "): return "WON" if ft_a >= ft_h else "LOST"
        return "VOID"

    def _eval_ou(valor, linea_str):
        linea = float(linea_str)
        if linea_str.startswith("+"): return "WON" if valor > linea else "LOST"
        if linea_str.startswith("-"): return "WON" if valor < linea else "LOST"
        return "VOID"

    if market == "Goles totales":
        return _eval_ou(total, sel)

    if market.startswith("Goles ") and "1T" not in market and "2T" not in market:
        team = market.replace("Goles ", "").strip()
        valor = ft_h if team == home_name else ft_a
        return _eval_ou(valor, sel)

    if market == "Goles 1T":
        return _eval_ou((ht_h or 0) + (ht_a or 0), sel)

    if market == "Goles 2T":
        v = ((ft_h - (ht_h or 0)) + (ft_a - (ht_a or 0)))
        return _eval_ou(v, sel)

    return "VOID"


# =============================================================
#  7. MOTOR DE ANÁLISIS
# =============================================================
MODO_SILENCIOSO = False


def _analizar_match(match, aplicar_calibracion=True, usar_cuotas_reales=True):
    picks = []
    h_n, a_n = match['homeTeam']['name'], match['awayTeam']['name']
    h_s = get_deep_data(match['homeTeam']['id'])
    a_s = get_deep_data(match['awayTeam']['id'])

    exH = ((h_s['gf'] + a_s['gc']) / 2) * 1.10
    exA = ((a_s['gf'] + h_s['gc']) / 2) * 0.95
    total = exH + exA
    exH_1t = ((h_s['gf_1t'] + a_s['gc_1t']) / 2) * 1.10
    exA_1t = ((a_s['gf_1t'] + h_s['gc_1t']) / 2) * 0.95
    exH_2t = ((h_s['gf_2t'] + a_s['gc_2t']) / 2) * 1.10
    exA_2t = ((a_s['gf_2t'] + h_s['gc_2t']) / 2) * 0.95

    mat = score_matrix(exH, exA)
    pL = sum(mat[i][j] for i in range(9) for j in range(9) if i>j)*100
    pV = sum(mat[i][j] for i in range(9) for j in range(9) if j>i)*100
    pE = 100 - pL - pV
    pL, pE, pV = round(pL), round(pE), round(pV)

    def add(market, selection, prob_raw):
        cal = get_calibration(market) if aplicar_calibracion else 1.0
        prob = max(0.01, min(99.9, round(prob_raw * cal, 1)))
        fair_odd = round(1 / (prob / 100.0), 2) if prob > 0 else 0
        picks.append({"market": market, "selection": selection,
                      "prob": prob, "fair_odd": fair_odd,
                      "prob_raw": prob_raw, "calib": cal})

    add("1X2", f"Gana {h_n}", pL)
    add("1X2", f"Gana {a_n}", pV)
    add("1X2", f"1X {h_n}", pL+pE)
    add("1X2", f"X2 {a_n}", pV+pE)

    def add_ou(market, lam, lineas=LINEAS):
        for ln in lineas:
            po, pu = over_under(lam, ln)
            add(market, f"+{ln}", po)
            add(market, f"-{ln}", pu)

    add_ou("Goles totales", total)
    add_ou(f"Goles {h_n}", exH, LINEAS_EQUIPO)
    add_ou(
