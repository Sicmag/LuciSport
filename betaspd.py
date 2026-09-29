# =============================================================
#  LUCI SPORT 4.5
#  - Integración SofaScore (xG, forma, H2H)
#  - Config desde .env / st.secrets
#  - Cálculo de edge (cuota justa vs cuota real)
#  - Persistencia opcional (persistir=True/False)
#  - Timestamps en UTC
#  - hash() determinista (compatible entre procesos)
#  - Submenú de combinadas: manual y automática por cuota objetivo
#  - Filtro estricto: cuota >= 1.50 y prob >= 55% en cada pata
#  - Evita correlaciones y repetición de partidos
#  - Mercados: h2h, totals (plan free de The Odds API)
#  - Rotación entre 2 API keys de The Odds API
#  - Tarjetas con default variable por equipo
#  - Envío automático de picks a Telegram
#  - Excel con una hoja POR EQUIPO (auto-actualizada)
#  - Auto-resolución de apuestas contra la API
#  - Calibración por rendimiento histórico por mercado
#  - Backtest de partidos pasados
#  - Migración automática de BD
# =============================================================
import os, math, time, sqlite3, requests, json, hashlib
from datetime import datetime, timedelta, timezone

try:
    from openpyxl import Workbook, load_workbook
    from openpyxl.styles import Font, PatternFill
    EXCEL_OK = True
except ImportError:
    EXCEL_OK = False

# ---------- CONFIGURACIÓN (desde config.py) ----------
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

# ---------- COLORES ----------
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
    CREATE TABLE IF NOT EXISTS ladder (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        paso            INTEGER NOT NULL,
        bankroll_before REAL NOT NULL,
        stake           REAL NOT NULL,
        odd             REAL NOT NULL,
        resultado       TEXT DEFAULT 'PENDING',
        bankroll_after  REAL,
        nota            TEXT,
        created_at      TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_pred_result ON predictions(result);
    CREATE INDEX IF NOT EXISTS idx_pred_match  ON predictions(match_id);
    CREATE INDEX IF NOT EXISTS idx_pred_home   ON predictions(home);
    CREATE INDEX IF NOT EXISTS idx_pred_away   ON predictions(away);
    """)
    con.commit(); con.close()


def migrate_db():
    con = db(); cur = con.cursor()
    cur.execute("PRAGMA table_info(predictions)")
    cols = [r[1] for r in cur.fetchall()]
    nuevos = [
        ("liga",        "TEXT"),
        ("match_date",  "TEXT"),
        ("home_id",     "INTEGER"),
        ("away_id",     "INTEGER"),
        ("ft_home",     "INTEGER"),
        ("ft_away",     "INTEGER"),
        ("resolved_at", "TEXT"),
        ("cuota_real",  "REAL"),
        ("edge_pct",    "REAL"),
    ]
    for col, tipo in nuevos:
        if col not in cols:
            cur.execute(f"ALTER TABLE predictions ADD COLUMN {col} {tipo}")
            print(f"{G}[MIGRACIÓN] Columna '{col}' añadida.{W}")
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


def ladder_status():
    con = db(); cur = con.cursor()
    cur.execute("SELECT paso, bankroll_after FROM ladder ORDER BY paso DESC LIMIT 1")
    row = cur.fetchone(); con.close()
    return (0, BANKROLL_INICIAL) if not row else (row[0], row[1] or BANKROLL_INICIAL)


def ladder_add(stake, odd, nota=""):
    paso, bankroll = ladder_status()
    con = db(); cur = con.cursor()
    cur.execute("""INSERT INTO ladder(paso, bankroll_before, stake, odd, nota, created_at)
                   VALUES(?,?,?,?,?,?)""",
                (paso+1, bankroll, stake, odd, nota, _utcnow_iso()))
    con.commit(); con.close()
    return paso+1, bankroll


def ladder_resolve(paso, gano):
    con = db(); cur = con.cursor()
    cur.execute("SELECT bankroll_before, stake, odd FROM ladder WHERE paso=?", (paso,))
    row = cur.fetchone()
    if not row: return None
    bb, stake, odd = row
    after = bb - stake + stake*odd if gano else bb - stake
    cur.execute("UPDATE ladder SET resultado=?, bankroll_after=? WHERE paso=?",
                ("WON" if gano else "LOST", after, paso))
    con.commit(); con.close()
    return after


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


def color_prob(p):
    if p < 40: return R
    if p < 60: return Y
    if p < 80: return B
    return G


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
        print(f"{Y}[TELEGRAM] Token no configurado.{W}")
        return False
    if not TELEGRAM_CHAT_ID or "PON_AQUI" in str(TELEGRAM_CHAT_ID):
        print(f"{Y}[TELEGRAM] Chat ID no configurado.{W}")
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": mensaje, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, json=payload, timeout=15)
        data = r.json()
        if not data.get("ok"):
            print(f"{R}[TELEGRAM] Error: {data.get('description','desconocido')}{W}")
            return False
        return True
    except requests.RequestException as e:
        print(f"{R}[TELEGRAM] Red: {e}{W}")
        return False
    except ValueError:
        print(f"{R}[TELEGRAM] Respuesta no-JSON.{W}")
        return False


# =============================================================
#  5. DATOS DE API (football-data.org)
# =============================================================
_LAST_CALL = [0.0]
def _rate_limit(min_interval=6.5):
    elapsed = time.time() - _LAST_CALL[0]
    if elapsed < min_interval: time.sleep(min_interval - elapsed)
    _LAST_CALL[0] = time.time()


def _default_cards(team_id):
    """Default determinista — usa hashlib, no hash() (que varía por proceso)."""
    h = int(hashlib.md5(str(team_id).encode()).hexdigest(), 16)
    return round(1.6 + (h % 13) * 0.1, 2)


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


def get_cards_data(team_id):
    _rate_limit()
    url = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=15"
    default = _default_cards(team_id)
    try:
        r = requests.get(url, headers=HEADERS, timeout=10).json()
        matches = r.get("matches", [])
        total, contados = 0, 0
        for m in matches:
            bookings = m.get("bookings")
            if not bookings: continue
            local = m['homeTeam']['id'] == team_id
            cards = sum(1 for b in bookings
                        if (b.get("team",{}).get("id") == m['homeTeam']['id']) == local)
            total += cards; contados += 1
        if contados > 0:
            return total/contados, True
        return default, False
    except Exception:
        return default, False


# =============================================================
#  5b. CUOTAS REALES (The Odds API con rotación de keys)
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
            print(f"{Y}[ODDS-API] Key #{current_idx+1} agotada. Rotando a #{nxt+1}.{W}")
            return True
    print(f"{R}[ODDS-API] TODAS las keys agotadas.{W}")
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
            print(f"{Y}[ODDS-API] Error {r.status_code}: {r.text[:150]}{W}")
            _ODDS_CACHE[sport_key] = (ahora, [])
            return []

        datos = r.json()
        _ODDS_CACHE[sport_key] = (ahora, datos)
        print(f"{G}[ODDS-API] {sport_key}: {len(datos)} partidos | "
              f"Key #{idx+1} | Restantes: {restantes}{W}")
        return datos
    except Exception as e:
        print(f"{Y}[ODDS-API] Red: {e}{W}")
        _ODDS_CACHE[sport_key] = (ahora, [])
        return []


def get_cuotas_reales(match):
    """Devuelve {mercado: {selección: cuota_promedio}} o {} si no hay datos."""
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
                    e