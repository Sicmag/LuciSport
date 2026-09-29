"""
⚽ LuciSport AI — Todo en uno.
Modelo Dixon-Coles + Groq IA + The Odds API.
Sin dependencias externas de módulos propios.
"""
import json
import math
import time
import requests
import streamlit as st
from datetime import datetime, timedelta, timezone


# ============================================================
#  1. CONFIGURACIÓN
# ============================================================
def _get(key, default=""):
    """Lee de st.secrets (Cloud) o .env (local)."""
    try:
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    import os
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass
    return os.getenv(key, default)


FOOTBALL_API_KEY = _get("FOOTBALL_API_KEY")
TELEGRAM_TOKEN   = _get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = _get("TELEGRAM_CHAT_ID")
ODDS_API_KEYS    = [k.strip() for k in _get("ODDS_API_KEYS", "").split(",") if k.strip()]
GROQ_API_KEY     = _get("GROQ_API_KEY", "")

HEADERS = {'X-Auth-Token': FOOTBALL_API_KEY}

PROB_MINIMA       = 55.0
CUOTA_MINIMA      = 1.50
MAX_PICKS_PARTIDO = 3
KELLY_FRACTION    = 0.25
KELLY_CAP         = 0.05
BANKROLL          = 100000

LINEAS        = [0.5, 1.5, 2.5, 3.5, 4.5]
LINEAS_EQUIPO = [0.5, 1.5, 2.5]

SPORT_KEYS = {
    "PL": "soccer_epl",
    "PD": "soccer_spain_la_liga",
    "SA": "soccer_italy_serie_a",
    "BL1": "soccer_germany_bundesliga",
    "FL1": "soccer_france_ligue_one",
    "DED": "soccer_netherlands_eredivisie",
    "PPL": "soccer_portugal_primeira_liga",
    "BSA": "soccer_brazil_campeonato",
    "ELC": "soccer_efl_champ",
    "CL": "soccer_uefa_champs_league",
    "ELI": "soccer_uefa_europa_league",
    "CLI": "soccer_conmebol_libertadores",
}

LIGAS = {
    "Premier League": "PL",
    "La Liga":        "PD",
    "Serie A":        "SA",
    "Bundesliga":     "BL1",
    "Ligue 1":        "FL1",
    "Champions":      "CL",
    "Europa League":  "ELI",
    "Libertadores":   "CLI",
}


# ============================================================
#  2. MODELO MATEMÁTICO
# ============================================================
def poisson(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam**k * math.exp(-lam)) / math.factorial(k)


def poisson_cdf(lam, x_max):
    return sum(poisson(lam, k) for k in range(0, x_max + 1))


def dc_tau(i, j, lam_h, lam_a, rho=-0.10):
    if i == 0 and j == 0: return 1 - lam_h * lam_a * rho
    if i == 0 and j == 1: return 1 + lam_h * rho
    if i == 1 and j == 0: return 1 + lam_a * rho
    if i == 1 and j == 1: return 1 - rho
    return 1.0


def score_matrix(lam_h, lam_a, max_g=8):
    m = [[poisson(lam_h, i) * poisson(lam_a, j) * dc_tau(i, j, lam_h, lam_a)
          for j in range(max_g + 1)] for i in range(max_g + 1)]
    s = sum(sum(r) for r in m)
    return [[v / s for v in row] for row in m]


def over_under(lam, linea):
    x = int(math.floor(linea))
    p_under = poisson_cdf(lam, x) * 100
    return round(100 - p_under), round(p_under)


def kelly_stake(prob_pct, odd, bankroll=BANKROLL):
    p = prob_pct / 100.0
    b = odd - 1
    if b <= 0: return 0.0
    f = (p * b - (1 - p)) / b
    if f <= 0: return 0.0
    return round(bankroll * min(f * KELLY_FRACTION, KELLY_CAP), 2)


# ============================================================
#  3. DATOS DE EQUIPO (football-data.org)
# ============================================================
_LAST_CALL = [0.0]


def _rate_limit(min_interval=1.5):
    elapsed = time.time() - _LAST_CALL[0]
    if elapsed < min_interval:
        time.sleep(min_interval - elapsed)
    _LAST_CALL[0] = time.time()


@st.cache_data(ttl=43200, show_spinner=False)
def get_deep_data(team_id):
    """Estadísticas de un equipo: goles a favor/contra (últ. 20 partidos). Cache 12h."""
    _rate_limit()
    url = f"https://api.football-data.org/v4/teams/{team_id}/matches?status=FINISHED&limit=20"
    default = {"gf": 1.2, "gc": 1.1,
               "gf_1t": 1.2 * 0.44, "gc_1t": 1.1 * 0.44,
               "gf_2t": 1.2 * 0.56, "gc_2t": 1.1 * 0.56}
    try:
        r = requests.get(url, headers=HEADERS, timeout=10).json()
        matches = r.get("matches", [])
        if not matches:
            return default
        gf = gc = gf1 = gc1 = 0
        n = len(matches)
        for m in matches:
            local = m['homeTeam']['id'] == team_id
            fth = (m['score']['fullTime']['home'] or 0)
            fta = (m['score']['fullTime']['away'] or 0)
            ht = m['score'].get('halfTime') or {}
            hth, hta = ht.get('home'), ht.get('away')
            if local:
                gf += fth; gc += fta
                gf1 += hth if hth is not None else fth * 0.44
                gc1 += hta if hta is not None else fta * 0.44
            else:
                gf += fta; gc += fth
                gf1 += hta if hta is not None else fta * 0.44
                gc1 += hth if hth is not None else fth * 0.44
        return {
            "gf": gf / n, "gc": gc / n,
            "gf_1t": gf1 / n, "gc_1t": gc1 / n,
            "gf_2t": max((gf - gf1) / n, 0.01),
            "gc_2t": max((gc - gc1) / n, 0.01),
        }
    except Exception as e:
        print(f"[football-data] Error {team_id}: {e}")
        return default


# ============================================================
#  4. CUOTAS REALES (The Odds API)
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def _fetch_odds_liga(sport_key):
    if not ODDS_API_KEYS:
        return []
    key = ODDS_API_KEYS[0]
    try:
        url = f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/"
        params = {"apiKey": key, "regions": "eu",
                  "markets": "h2h,totals", "oddsFormat": "decimal"}
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        return r.json()
    except Exception:
        return []


def _normalize_name(name):
    n = (name or "").lower().strip()
    for suf in [' cf', ' fc', ' sc', ' ac', ' club', ' ud', ' cd', ' deportivo',
                ' real', ' atletico', ' atlético', ' balompié', ' balompie']:
        n = n.replace(suf, '')
    return n.strip()


def get_cuotas_reales(match):
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
        if (h_norm in ev_h or ev_h in h_norm) and (a_norm in ev_a or ev_a in a_norm):
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
            k = mkt.get("key", "")
            if k == "h2h":
                for o in mkt.get("outcomes", []):
                    nm = o.get("name", "")
                    price = o.get("price")
                    if not price:
                        continue
                    if nm == "Draw":
                        cuotas_h2h["draw"].append(float(price))
                    elif nm == home_api:
                        cuotas_h2h["home"].append(float(price))
                    elif nm == away_api:
                        cuotas_h2h["away"].append(float(price))
            elif k == "totals":
                for o in mkt.get("outcomes", []):
                    pt = str(o.get("point", "")).strip()
                    nm = o.get("name", "")
                    price = o.get("price")
                    if not price or not pt:
                        continue
                    cuotas_ou.setdefault(pt, {"Over": [], "Under": []})
                    if nm in ("Over", "Under"):
                        cuotas_ou[pt][nm].append(float(price))

    def _avg(lst):
        return round(sum(lst) / len(lst), 2) if lst else 0

    resultado = {"1X2": {}, "Goles totales": {}}
    if cuotas_h2h["home"]:
        resultado["1X2"]["Gana " + home] = _avg(cuotas_h2h["home"])
    if cuotas_h2h["away"]:
        resultado["1X2"]["Gana " + away] = _avg(cuotas_h2h["away"])
    if cuotas_h2h["draw"]:
        resultado["1X2"]["Empate"] = _avg(cuotas_h2h["draw"])
    for pt, d in cuotas_ou.items():
        if d["Over"]:
            resultado["Goles totales"]["+" + pt] = _avg(d["Over"])
        if d["Under"]:
            resultado["Goles totales"]["-" + pt] = _avg(d["Under"])

    return {k: v for k, v in resultado.items() if v}


# ============================================================
#  5. MOTOR DE ANÁLISIS
# ============================================================
def analizar_match(match, usar_cuotas_reales=True):
    """Genera todos los picks posibles de un partido."""
    picks = []
    h_n = match['homeTeam']['name']
    a_n = match['awayTeam']['name']
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
    pL = sum(mat[i][j] for i in range(9) for j in range(9) if i > j) * 100
    pV = sum(mat[i][j] for i in range(9) for j in range(9) if j > i) * 100
    pE = 100 - pL - pV
    pL, pE, pV = round(pL), round(pE), round(pV)

    def add(market, selection, prob_raw):
        prob = max(0.01, min(99.9, round(prob_raw, 1)))
        fair_odd = round(1 / (prob / 100.0), 2) if prob > 0 else 0
        picks.append({"market": market, "selection": selection,
                      "prob": prob, "fair_odd": fair_odd})

    add("1X2", f"Gana {h_n}", pL)
    add("1X2", f"Gana {a_n}", pV)
    add("1X2", f"1X {h_n}", pL + pE)
    add("1X2", f"X2 {a_n}", pV + pE)

    def add_ou(market, lam, lineas=LINEAS):
        for ln in lineas:
            po, pu = over_under(lam, ln)
            add(market, f"+{ln}", po)
            add(market, f"-{ln}", pu)

    add_ou("Goles totales", total)
    add_ou(f"Goles {h_n}", exH, LINEAS_EQUIPO)
    add_ou(f"Goles {a_n}", exA, LINEAS_EQUIPO)
    add_ou("Goles 1T", exH_1t + exA_1t)
    add_ou("Goles 2T", exH_2t + exA_2t)

    btts = round(((1 - poisson(exH, 0)) * (1 - poisson(exA, 0))) * 100, 1)
    add("BTTS", "Ambos marcan", btts)

    # Cuotas reales y edge
    if usar_cuotas_reales:
        try:
            cuotas_reales = get_cuotas_reales(match)
            if cuotas_reales:
                for p in picks:
                    m = p["market"]
                    if m in cuotas_reales and p["selection"] in cuotas_reales[m]:
                        p["cuota_real"] = cuotas_reales[m][p["selection"]]
                        if p["fair_odd"] > 0:
                            p["edge_%"] = round(
                                (p["cuota_real"] / p["fair_odd"] - 1) * 100, 2)
        except Exception:
            pass

    return picks


def top_picks(match, usar_cuotas_reales=True):
    """Filtra y devuelve los mejores picks (prob ≥55% y cuota ≥1.50)."""
    picks = analizar_match(match, usar_cuotas_reales)
    validos = [p for p in picks
               if p["prob"] >= PROB_MINIMA and p["fair_odd"] >= CUOTA_MINIMA]
    por_mercado = {}
    for p in validos:
        if p["market"] not in por_mercado or p["prob"] > por_mercado[p["market"]]["prob"]:
            por_mercado[p["market"]] = p
    candidatos = sorted(por_mercado.values(), key=lambda x: x["prob"], reverse=True)
    return candidatos[:MAX_PICKS_PARTIDO]


# ============================================================
#  6. IA (Groq)
# ============================================================
SYSTEM_PROMPT = """Eres analista profesional de apuestas deportivas.
Recibes picks del modelo estadístico de un partido de fútbol.
Responde SIEMPRE en JSON válido con este formato:
{
  "analisis": "3-4 frases del contexto del partido",
  "pick_recomendado": "selección concreta",
  "confianza": 7,
  "riesgos": ["riesgo1", "riesgo2"]
}
confianza va de 1 a 10."""


def analizar_con_ia(match, picks):
    if not GROQ_API_KEY:
        return None
    home = match['homeTeam']['name']
    away = match['awayTeam']['name']
    prompt = f"PARTIDO: {home} vs {away}\n\nPICKS DEL MODELO:\n"
    for p in picks[:5]:
        prompt += f"- [{p['market']}] {p['selection']}: {p['prob']}%\n"
    prompt += "\nGenera el JSON."
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": "llama-3.3-70b-versatile",
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "temperature": 0.3,
                "max_tokens": 700,
                "response_format": {"type": "json_object"},
            },
            timeout=30,
        )
        if r.status_code == 200:
            return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        pass
    return None


# ============================================================
#  7. TELEGRAM
# ============================================================
def enviar_telegram(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID,
                                      "text": msg,
                                      "parse_mode": "Markdown"}, timeout=15)
        return r.json().get("ok", False)
    except Exception:
        return False


# ============================================================
#  8. STREAMLIT UI
# ============================================================
st.set_page_config(
    page_title="LuciSport AI",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="collapsed",
)


# ---------- CARGA DE PARTIDOS ----------
@st.cache_data(ttl=1800, show_spinner=False)
def cargar_partidos(codigo: str, dias: int = 5):
    ahora = datetime.now(timezone.utc)
    url = (f"https://api.football-data.org/v4/competitions/{codigo}/matches"
           f"?dateFrom={ahora.strftime('%Y-%m-%d')}"
           f"&dateTo={(ahora + timedelta(days=dias)).strftime('%Y-%m-%d')}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        return r.json().get("matches", [])
    except Exception as e:
        st.error(f"Error API: {e}")
        return []


# ---------- ESTADO ----------
if "partidos" not in st.session_state:
    st.session_state.partidos = []
if "analisis" not in st.session_state:
    st.session_state.analisis = []
if "boleto" not in st.session_state:
    st.session_state.boleto = []


# ---------- SIDEBAR ----------
with st.sidebar:
    st.title("⚽ LuciSport AI")
    st.caption("Dixon-Coles + IA + Odds API")

    liga_nombre = st.selectbox("Liga", list(LIGAS.keys()))
    dias = st.slider("Días a futuro", 1, 14, 5)
    usar_ia = st.toggle("Análisis con IA (Groq)", value=False)
    max_analizar = st.slider("Máx. partidos a analizar", 1, 5, 1)

    st.divider()

    if st.button("📥 Cargar partidos", use_container_width=True, type="primary"):
        with st.spinner("Cargando partidos..."):
            st.session_state.partidos = cargar_partidos(LIGAS[liga_nombre], dias)
            st.session_state.analisis = []
        st.rerun()

    st.divider()
    st.subheader("🧾 Boleto")

    if not st.session_state.boleto:
        st.caption("Sin selecciones")
    else:
        for i, b in enumerate(st.session_state.boleto):
            col1, col2 = st.columns([4, 1])
            with col1:
                st.write(f"**{b['seleccion']}**")
                st.caption(f"{b['partido']} · @{b['cuota']:.2f}")
            with col2:
                if st.button("❌", key=f"del_{i}"):
                    st.session_state.boleto.pop(i)
                    st.rerun()

        cuota_total = 1.0
        for b in st.session_state.boleto:
            cuota_total *= b["cuota"]
        st.metric("Cuota total", f"{cuota_total:.2f}")

        if st.button("🗑️ Vaciar boleto", use_container_width=True):
            st.session_state.boleto = []
            st.rerun()


# ---------- CUERPO ----------
st.title("⚽ LuciSport AI")
st.caption("Modelo Dixon-Coles + Groq Llama 3.3")

tab1, tab2 = st.tabs(["📅 Partidos y análisis", "📊 Resultados"])


# ============================================================
#  TAB 1
# ============================================================
with tab1:
    partidos = st.session_state.partidos

    if not partidos:
        st.info("👈 Abre el menú lateral (» arriba izquierda), "
                "elige liga y pulsa **Cargar partidos**")
    else:
        st.success(f"{len(partidos)} partidos encontrados en **{liga_nombre}**")

        for m in partidos:
            m['league_code'] = LIGAS[liga_nombre]

        opciones = {
            f"{p['homeTeam']['name']} vs {p['awayTeam']['name']} · "
            f"{(p.get('utcDate') or '')[:16]}": i
            for i, p in enumerate(partidos)
        }

        seleccionados = st.multiselect(
            "Elige partidos para analizar",
            list(opciones.keys())[:30],
            max_selections=max_analizar,
            placeholder=f"Selecciona hasta {max_analizar} partido(s)",
        )

        if st.button("🔍 Analizar seleccionados", type="primary"):
            if not seleccionados:
                st.warning("Selecciona al menos un partido.")
            else:
                resultados = []
                for sel in seleccionados:
                    idx = opciones[sel]
                    match = partidos[idx]
                    with st.spinner(
                        f"Analizando {match['homeTeam']['name']} vs "
                        f"{match['awayTeam']['name']}..."
                    ):
                        try:
                            picks = top_picks(match)
                            ia = analizar_con_ia(match, picks) if usar_ia else None
                            resultados.append({
                                "partido": f"{match['homeTeam']['name']} vs "
                                           f"{match['awayTeam']['name']}",
                                "fecha": (match.get('utcDate') or '')[:16],
                                "liga": match.get('league_code', ''),
                                "picks": picks,
                                "ia": ia,
                                "error": None,
                            })
                        except Exception as e:
                            import traceback
                            resultados.append({
                                "partido": f"{match['homeTeam']['name']} vs "
                                           f"{match['awayTeam']['name']}",
                                "fecha": (match.get('utcDate') or '')[:16],
                                "liga": match.get('league_code', ''),
                                "picks": [],
                                "ia": None,
                                "error": f"{type(e).__name__}: {e}",
                                "traceback": traceback.for
