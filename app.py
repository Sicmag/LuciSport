"""LuciSport AI — Supabase + 5DollarFootballAPI."""
import json, math, time, requests, streamlit as st
from datetime import datetime, timedelta, timezone
from supabase import create_client


# ============================================================
#  CONFIGURACIÓN
# ============================================================
def _get(key, default=""):
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


# ---------- API KEYS ----------
FOOTBALL_KEY = _get("FOOTBALL_API_KEY")
TELEGRAM_TOKEN = _get("TELEGRAM_TOKEN")
TELEGRAM_CHAT = _get("TELEGRAM_CHAT_ID")
ODDS_KEYS = [k.strip() for k in _get("ODDS_API_KEYS", "").split(",") if k.strip()]
GROQ_KEY = _get("GROQ_API_KEY", "")
DOLLAR_KEY = _get("DOLLAR_API_KEY", "")

HEADERS = {'X-Auth-Token': FOOTBALL_KEY}

# ---------- 5DOLLARFOOTBALL (AJUSTA SEGÚN TU DOC) ----------
DOLLAR_BASE = "https://api.5dollarfootball.com"   # ← tu URL base real
DOLLAR_AUTH_MODE = "query"   # opciones: "query" | "header_bearer" | "header_custom"
DOLLAR_AUTH_PARAM = "api_key"   # si query
DOLLAR_AUTH_HEADER = "Authorization"   # si header
DOLLAR_ENDPOINT_MATCHES = "/matches"    # endpoint de partidos
DOLLAR_ENDPOINT_STATS = "/matches/{id}/stats"  # stats por partido
DOLLAR_ENDPOINT_ODDS = "/matches/{id}/odds"    # cuotas por partido

# ---------- PARÁMETROS DEL MODELO ----------
PROB_MIN, CUOTA_MIN = 55.0, 1.50
MAX_PICKS, BANKROLL = 5, 100000
LINEAS = [0.5, 1.5, 2.5, 3.5, 4.5]
LINEAS_CORNERS = [7.5, 8.5, 9.5, 10.5, 11.5]
LINEAS_TARJETAS = [2.5, 3.5, 4.5, 5.5]

SPORT_KEYS = {"PL": "soccer_epl", "PD": "soccer_spain_la_liga",
              "SA": "soccer_italy_serie_a", "BL1": "soccer_germany_bundesliga",
              "FL1": "soccer_france_ligue_one", "CL": "soccer_uefa_champs_league",
              "ELI": "soccer_uefa_europa_league", "CLI": "soccer_conmebol_libertadores"}

LIGAS = {"Premier League": "PL", "La Liga": "PD", "Serie A": "SA",
         "Bundesliga": "BL1", "Ligue 1": "FL1", "Champions": "CL",
         "Europa League": "ELI", "Libertadores": "CLI"}


# ============================================================
#  SUPABASE
# ============================================================
def _limpiar_url(url):
    if not url:
        return ""
    url = url.strip().rstrip("/")
    for marca in ["/rest/v1", "/auth/v1", "/storage/v1", "/realtime/v1"]:
        if marca in url:
            url = url.split(marca)[0]
    return url.rstrip("/")


@st.cache_resource(show_spinner=False)
def get_supabase():
    url = _limpiar_url(_get("SUPABASE_URL", ""))
    key = _get("SUPABASE_KEY", "").strip()
    if not url or not key:
        st.error("⚠️ Faltan SUPABASE_URL o SUPABASE_KEY en los Secrets")
        st.stop()
    return create_client(url, key)


def guardar_pick(match, pick, estado="PENDIENTE"):
    match_id = f"{match['homeTeam']['id']}_{match['awayTeam']['id']}_{(match.get('utcDate') or '')[:16]}"
    fila = {
        "match_id": match_id,
        "fecha_partido": (match.get('utcDate') or '')[:16],
        "liga": match.get('league_code', ''),
        "home": match['homeTeam']['name'],
        "away": match['awayTeam']['name'],
        "mercado": pick['market'],
        "seleccion": pick['selection'],
        "prob_modelo": pick['prob'],
        "cuota_justa": pick['fair_odd'],
        "cuota_real": pick.get('cuota_real'),
        "edge_pct": pick.get('edge_%'),
        "stake_sug": pick.get('stake_sug'),
        "estado": estado,
    }
    try:
        get_supabase().table("picks").upsert(fila).execute()
    except Exception as e:
        st.warning(f"Error guardando pick: {e}")


def listar_picks(estado=None, limite=200):
    try:
        q = get_supabase().table("picks").select("*").order("creado", desc=True).limit(limite)
        if estado:
            q = q.eq("estado", estado)
        return q.execute().data or []
    except Exception as e:
        st.warning(f"Error leyendo picks: {e}")
        return []


def stats_por_estado():
    try:
        rows = get_supabase().table("picks").select("estado").execute().data or []
        res = {}
        for r in rows:
            res[r["estado"]] = res.get(r["estado"], 0) + 1
        return res
    except Exception:
        return {}


def cambiar_estado(pick_id, nuevo_estado):
    try:
        get_supabase().table("picks").update({
            "estado": nuevo_estado,
            "resuelto": datetime.now(timezone.utc).isoformat(),
        }).eq("id", pick_id).execute()
    except Exception as e:
        st.warning(f"Error actualizando: {e}")


def eliminar_pick(pick_id):
    try:
        get_supabase().table("picks").delete().eq("id", pick_id).execute()
    except Exception as e:
        st.warning(f"Error eliminando: {e}")


def stats_rendimiento():
    rows = listar_picks(limite=2000)
    ganados = sum(1 for r in rows if r["estado"] == "GANADO")
    perdidos = sum(1 for r in rows if r["estado"] == "PERDIDO")
    anulados = sum(1 for r in rows if r["estado"] == "ANULADO")
    tot = ganados + perdidos
    hit = round(ganados / tot * 100, 1) if tot else 0

    mercados = {}
    for r in rows:
        if r["estado"] not in ("GANADO", "PERDIDO", "ANULADO"):
            continue
        m = r["mercado"]
        mercados.setdefault(m, {"ganados": 0, "perdidos": 0, "anulados": 0})
        if r["estado"] == "GANADO": mercados[m]["ganados"] += 1
        elif r["estado"] == "PERDIDO": mercados[m]["perdidos"] += 1
        else: mercados[m]["anulados"] += 1

    por_mercado = []
    for m, d in mercados.items():
        tot_m = d["ganados"] + d["perdidos"]
        por_mercado.append({
            "mercado": m,
            "ganados": d["ganados"], "perdidos": d["perdidos"],
            "anulados": d["anulados"],
            "hit": round(d["ganados"] / tot_m * 100, 1) if tot_m else 0,
        })

    return {
        "ganados": ganados, "perdidos": perdidos, "anulados": anulados,
        "hit_rate": hit, "por_mercado": por_mercado,
    }


# ============================================================
#  MODELO MATEMÁTICO
# ============================================================
def poisson(lam, k):
    if lam <= 0:
        return 1.0 if k == 0 else 0.0
    return (lam**k * math.exp(-lam)) / math.factorial(k)


def poisson_cdf(lam, x_max):
    return sum(poisson(lam, k) for k in range(0, x_max + 1))


def dc_tau(i, j, lh, la, rho=-0.10):
    if i == 0 and j == 0: return 1 - lh * la * rho
    if i == 0 and j == 1: return 1 + lh * rho
    if i == 1 and j == 0: return 1 + la * rho
    if i == 1 and j == 1: return 1 - rho
    return 1.0


def score_matrix(lh, la, mg=8):
    m = [[poisson(lh, i) * poisson(la, j) * dc_tau(i, j, lh, la)
          for j in range(mg + 1)] for i in range(mg + 1)]
    s = sum(sum(r) for r in m)
    return [[v / s for v in row] for row in m]


def over_under(lam, linea):
    x = int(math.floor(linea))
    p_under = poisson_cdf(lam, x) * 100
    return round(100 - p_under), round(p_under)


def kelly(prob, odd, bank=BANKROLL):
    p, b = prob / 100.0, odd - 1
    if b <= 0: return 0.0
    f = (p * b - (1 - p)) / b
    if f <= 0: return 0.0
    return round(bank * min(f * 0.25, 0.05), 2)


_LAST = [0.0]
def _rl(t=1.5):
    e = time.time() - _LAST[0]
    if e < t: time.sleep(t - e)
    _LAST[0] = time.time()


# ============================================================
#  FOOTBALL-DATA.ORG
# ============================================================
@st.cache_data(ttl=43200, show_spinner=False)
def get_team_data(tid):
    _rl()
    url = f"https://api.football-data.org/v4/teams/{tid}/matches?status=FINISHED&limit=20"
    d = {"gf": 1.2, "gc": 1.1, "gf_1t": 0.53, "gc_1t": 0.48,
         "gf_2t": 0.67, "gc_2t": 0.62}
    try:
        r = requests.get(url, headers=HEADERS, timeout=10).json()
        ms = r.get("matches", [])
        if not ms: return d
        gf = gc = gf1 = gc1 = 0
        n = len(ms)
        for m in ms:
            loc = m['homeTeam']['id'] == tid
            fh = m['score']['fullTime']['home'] or 0
            fa = m['score']['fullTime']['away'] or 0
            ht = m['score'].get('halfTime') or {}
            hh, ha = ht.get('home'), ht.get('away')
            if loc:
                gf += fh; gc += fa
                gf1 += hh if hh is not None else fh * 0.44
                gc1 += ha if ha is not None else fa * 0.44
            else:
                gf += fa; gc += fh
                gf1 += ha if ha is not None else fa * 0.44
                gc1 += hh if hh is not None else fh * 0.44
        return {"gf": gf / n, "gc": gc / n, "gf_1t": gf1 / n, "gc_1t": gc1 / n,
                "gf_2t": max((gf - gf1) / n, 0.01), "gc_2t": max((gc - gc1) / n, 0.01)}
    except Exception:
        return d


@st.cache_data(ttl=1800, show_spinner=False)
def cargar_partidos(cod, dias=5):
    ah = datetime.now(timezone.utc)
    url = (f"https://api.football-data.org/v4/competitions/{cod}/matches"
           f"?dateFrom={ah.strftime('%Y-%m-%d')}"
           f"&dateTo={(ah + timedelta(days=dias)).strftime('%Y-%m-%d')}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        return r.json().get("matches", []) if r.status_code == 200 else []
    except Exception:
        return []


# ============================================================
#  5DOLLARFOOTBALL
# ============================================================
def _dollar_auth():
    """Devuelve (params, headers) según configuración."""
    params, headers = {}, {}
    if not DOLLAR_KEY:
        return params, headers
    if DOLLAR_AUTH_MODE == "query":
        params[DOLLAR_AUTH_PARAM] = DOLLAR_KEY
    elif DOLLAR_AUTH_MODE == "header_bearer":
        headers[DOLLAR_AUTH_HEADER] = f"Bearer {DOLLAR_KEY}"
    elif DOLLAR_AUTH_MODE == "header_custom":
        headers[DOLLAR_AUTH_HEADER] = DOLLAR_KEY
    return params, headers


@st.cache_data(ttl=1800, show_spinner=False)
def dollar_get(endpoint, extra_params=None):
    """GET genérico a 5DollarFootball. Devuelve dict o None."""
    if not DOLLAR_KEY:
        return None
    params, headers = _dollar_auth()
    if extra_params:
        params.update(extra_params)
    try:
        r = requests.get(f"{DOLLAR_BASE}{endpoint}",
                         params=params, headers=headers, timeout=15)
        if r.status_code == 200:
            return r.json()
        return {"_error": f"HTTP {r.status_code}", "_body": r.text[:200]}
    except Exception as e:
        return {"_error": str(e)}


@st.cache_data(ttl=3600, show_spinner=False)
def get_corners_cards_team(team_name):
    """
    Devuelve {'corners': X, 'cards': Y} promedio de un equipo.
    El endpoint varía según la API — ajusta la lógica si es necesario.
    """
    if not DOLLAR_KEY:
        return None
    data = dollar_get(DOLLAR_ENDPOINT_MATCHES, {"team": team_name, "limit": 10})
    if not data or "_error" in data:
        return None

    # Heurística: la API suele devolver un array con partidos.
    # Busca el patrón común {home, away, corners_home, corners_away, cards_home, cards_away}
    partidos = data if isinstance(data, list) else data.get("matches") or data.get("data") or []
    if not partidos:
        return None

    tot_corners = tot_cards = 0
    n = 0
    for p in partidos[:10]:
        h = p.get("home") or p.get("home_team") or ""
        if team_name.lower() in str(h).lower():
            tot_corners += p.get("corners_home", 0) or 0
            tot_cards += p.get("cards_home", 0) or 0
        else:
            tot_corners += p.get("corners_away", 0) or 0
            tot_cards += p.get("cards_away", 0) or 0
        n += 1

    if n == 0:
        return None
    return {"corners": round(tot_corners / n, 2), "cards": round(tot_cards / n, 2)}


@st.cache_data(ttl=3600, show_spinner=False)
def get_cuotas_dollar(match):
    """
    Cuotas reales de 5DollarFootball. Estructura flexible.
    Devuelve {mercado: {seleccion: cuota}} o {}.
    """
    if not DOLLAR_KEY:
        return {}
    # Intenta buscar el partido por nombre
    h = match['homeTeam']['name']
    a = match['awayTeam']['name']
    data = dollar_get(DOLLAR_ENDPOINT_MATCHES, {"home": h, "away": a})
    if not data or "_error" in data:
        return {}

    partidos = data if isinstance(data, list) else data.get("matches") or data.get("data") or []
    if not partidos:
        return {}

    # Toma el primero que coincida
    p = partidos[0]
    match_id = p.get("id")
    if not match_id:
        return {}

    odds_data = dollar_get(DOLLAR_ENDPOINT_ODDS.format(id=match_id))
    if not odds_data or "_error" in odds_data:
        return {}

    # Estructura esperada: {markets: [{name, outcomes: [{name, price}]}]}
    out = {}
    markets = odds_data.get("markets") or odds_data.get("odds") or []
    for m in markets:
        m_name = m.get("name") or m.get("market") or ""
        sel = {}
        for o in m.get("outcomes", []):
            nm = o.get("name") or o.get("selection") or ""
            price = o.get("price") or o.get("odd")
            if nm and price:
                sel[nm] = float(price)
        if sel:
            out[m_name] = sel
    return out


# ============================================================
#  THE ODDS API
# ============================================================
@st.cache_data(ttl=3600, show_spinner=False)
def fetch_odds(sport_key):
    if not ODDS_KEYS: return []
    try:
        r = requests.get(f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
                         params={"apiKey": ODDS_KEYS[0], "regions": "eu",
                                 "markets": "h2h,totals", "oddsFormat": "decimal"},
                         timeout=15)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def _norm(s):
    n = (s or "").lower().strip()
    for suf in [' cf', ' fc', ' sc', ' ac', ' club', ' ud', ' cd', ' real', ' atletico']:
        n = n.replace(suf, '')
    return n.strip()


def get_odds_oddsapi(match):
    sk = SPORT_KEYS.get(match.get("league_code", ""))
    if not sk: return {}
    evs = fetch_odds(sk)
    if not evs: return {}
    h, a = match['homeTeam']['name'], match['awayTeam']['name']
    hn, an = _norm(h), _norm(a)
    ev = None
    for e in evs:
        if (_norm(e.get("home_team", "")) == hn and _norm(e.get("away_team", "")) == an):
            ev = e
            break
    if not ev: return {}
    ha, aa = ev.get("home_team", ""), ev.get("away_team", "")
    ch = {"h": [], "d": [], "a": []}
    ou = {}
    for bk in ev.get("bookmakers", []):
        for mk in bk.get("markets", []):
            if mk.get("key") == "h2h":
                for o in mk.get("outcomes", []):
                    p = o.get("price"); nm = o.get("name", "")
                    if not p: continue
                    if nm == "Draw": ch["d"].append(float(p))
                    elif nm == ha: ch["h"].append(float(p))
                    elif nm == aa: ch["a"].append(float(p))
            elif mk.get("key") == "totals":
                for o in mk.get("outcomes", []):
                    pt = str(o.get("point", "")).strip()
                    nm = o.get("name", ""); p = o.get("price")
                    if not p or not pt: continue
                    ou.setdefault(pt, {"Over": [], "Under": []})
                    if nm in ("Over", "Under"): ou[pt][nm].append(float(p))
    avg = lambda l: round(sum(l) / len(l), 2) if l else 0
    out = {"1X2": {}, "Goles totales": {}}
    if ch["h"]: out["1X2"]["Gana " + h] = avg(ch["h"])
    if ch["a"]: out["1X2"]["Gana " + a] = avg(ch["a"])
    if ch["d"]: out["1X2"]["Empate"] = avg(ch["d"])
    for pt, d in ou.items():
        if d["Over"]: out["Goles totales"]["+" + pt] = avg(d["Over"])
        if d["Under"]: out["Goles totales"]["-" + pt] = avg(d["Under"])
    return {k: v for k, v in out.items() if v}


# ============================================================
#  MOTOR DE ANÁLISIS (con mercados extra)
# ============================================================
def analizar(match):
    h, a = match['homeTeam']['name'], match['awayTeam']['name']
    hs = get_team_data(match['homeTeam']['id'])
    as_ = get_team_data(match['awayTeam']['id'])

    # ---------- Goles ----------
    exH = ((hs['gf'] + as_['gc']) / 2) * 1.10
    exA = ((as_['gf'] + hs['gc']) / 2) * 0.95
    tot = exH + exA
    exH1 = ((hs['gf_1t'] + as_['gc_1t']) / 2) * 1.10
    exA1 = ((as_['gf_1t'] + hs['gc_1t']) / 2) * 0.95
    exH2 = ((hs['gf_2t'] + as_['gc_2t']) / 2) * 1.10
    exA2 = ((as_['gf_2t'] + hs['gc_2t']) / 2) * 0.95

    mat = score_matrix(exH, exA)
    pL = sum(mat[i][j] for i in range(9) for j in range(9) if i > j) * 100
    pV = sum(mat[i][j] for i in range(9) for j in range(9) if j > i) * 100
    pE = 100 - pL - pV
    pL, pE, pV = round(pL), round(pE), round(pV)

    picks = []
    def add(mk, sel, pr):
        pr = max(0.01, min(99.9, round(pr, 1)))
        fo = round(1 / (pr / 100.0), 2) if pr > 0 else 0
        picks.append({"market": mk, "selection": sel, "prob": pr, "fair_odd": fo})

    # ---------- 1X2 ----------
    add("1X2", f"Gana {h}", pL)
    add("1X2", f"Gana {a}", pV)
    add("1X2", f"1X {h}", pL + pE)
    add("1X2", f"X2 {a}", pV + pE)

    # ---------- Goles totales / equipo / tiempos ----------
    for mk, lam in [("Goles totales", tot), (f"Goles {h}", exH),
                    (f"Goles {a}", exA), ("Goles 1T", exH1 + exA1),
                    ("Goles 2T", exH2 + exA2)]:
        for ln in LINEAS:
            po, pu = over_under(lam, ln)
            add(mk, f"+{ln}", po)
            add(mk, f"-{ln}", pu)

    # ---------- BTTS ----------
    btts = round(((1 - poisson(exH, 0)) * (1 - poisson(exA, 0))) * 100, 1)
    add("BTTS", "Ambos marcan", btts)

    # ---------- CÓRNERS y TARJETAS (5DollarFootball) ----------
    ch = get_corners_cards_team(h)
    ca = get_corners_cards_team(a)
    if ch and ca:
        # Córners totales
        lam_corners = ch["corners"] + ca["corners"]
        if lam_corners > 4:
            for ln in LINEAS_CORNERS:
                po, pu = over_under(lam_corners, ln)
                add("Córners totales", f"+{ln}", po)
                add("Córners totales", f"-{ln}", pu)
            # Córners por equipo
            for ln in [3.5, 4.5, 5.5, 6.5]:
                po, _ = over_under(ch["corners"], ln)
                add(f"Córners {h}", f"+{ln}", po)
                po, _ = over_under(ca["corners"], ln)
                add(f"Córners {a}", f"+{ln}", po)

        # Tarjetas totales
        lam_cards = ch["cards"] + ca["cards"]
        if lam_cards > 1.5:
            for ln in LINEAS_TARJETAS:
                po, pu = over_under(lam_cards, ln)
                add("Tarjetas totales", f"+{ln}", po)
                add("Tarjetas totales", f"-{ln}", pu)

    # ---------- CUOTAS REALES ----------
    # 1. Odds API (para 1X2 y goles totales)
    try:
        cuotas_oddsapi = get_odds_oddsapi(match)
        for p in picks:
            if p["market"] in cuotas_oddsapi and p["selection"] in cuotas_oddsapi[p["market"]]:
                p["cuota_real"] = cuotas_oddsapi[p["market"]][p["selection"]]
                if p["fair_odd"] > 0:
                    p["edge_%"] = round((p["cuota_real"] / p["fair_odd"] - 1) * 100, 2)
    except Exception:
        pass

    # 2. 5DollarFootball (para córners y tarjetas)
    try:
        cuotas_dollar = get_cuotas_dollar(match)
        for p in picks:
            if not p.get("cuota_real") and p["market"] in cuotas_dollar \
                    and p["selection"] in cuotas_dollar[p["market"]]:
                p["cuota_real"] = cuotas_dollar[p["market"]][p["selection"]]
                if p["fair_odd"] > 0:
                    p["edge_%"] = round((p["cuota_real"] / p["fair_odd"] - 1) * 100, 2)
    except Exception:
        pass

    # ---------- FILTRAR + TOP ----------
    validos = [p for p in picks if p["prob"] >= PROB_MIN and p["fair_odd"] >= CUOTA_MIN]
    pm = {}
    for p in validos:
        # Nos quedamos con el mejor pick por combinación mercado+línea
        key = f"{p['market']}|{p['selection']}"
        if key not in pm or p["prob"] > pm[key]["prob"]:
            pm[key] = p
    top = sorted(pm.values(), key=lambda x: (x.get("edge_%") or 0, x["prob"]),
                 reverse=True)[:MAX_PICKS]
    for p in top:
        p["stake_sug"] = kelly(p["prob"], p["fair_odd"])
    return top


def ia_analizar(match, picks):
    if not GROQ_KEY: return None
    h, a = match['homeTeam']['name'], match['awayTeam']['name']
    sys_prompt = """Analista de apuestas. Devuelve JSON:
{"analisis":"3 frases","pick_recomendado":"x","confianza":7,"riesgos":["r1"]}"""
    txt = f"PARTIDO: {h} vs {a}\nPICKS:\n"
    for p in picks[:5]:
        txt += f"- [{p['market']}] {p['selection']}: {p['prob']}%\n"
    try:
        r = requests.post(
            "https://api.groq.com/openai/v1/chat/completions",
            headers={"Authorization": f"Bearer {GROQ_KEY}"},
            json={"model": "llama-3.3-70b-versatile",
                  "messages": [{"role": "system", "content": sys_prompt},
                               {"role": "user", "content": txt}],
                  "temperature": 0.3, "max_tokens": 500,
                  "response_format": {"type": "json_object"}},
            timeout=30)
        if r.status_code == 200:
            return json.loads(r.json()["choices"][0]["message"]["content"])
    except Exception:
        pass
    return None


def tg_send(msg):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT: return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT, "text": msg, "parse_mode": "Markdown"},
            timeout=15)
        return r.json().get("ok", False)
    except Exception:
        return False


# ============================================================
#  UI
# ============================================================
st.set_page_config(page_title="LuciSport AI", page_icon="⚽", layout="wide",
                   initial_sidebar_state="collapsed")

if "partidos" not in st.session_state:
    st.session_state.partidos = []
if "analisis" not in st.session_state:
    st.session_state.analisis = []
if "boleto" not in st.session_state:
    st.session_state.boleto = []


# ---------- SIDEBAR ----------
with st.sidebar:
    st.title("⚽ LuciSport AI")
    liga = st.selectbox("Liga", list(LIGAS.keys()))
    dias = st.slider("Días a futuro", 1, 14, 5)
    usar_ia = st.toggle("IA (Groq)", value=False)
    max_p = st.slider("Máx. partidos", 1, 5, 1)
    st.divider()
    if st.button("📥 Cargar partidos", use_container_width=True, type="primary"):
        with st.spinner("Cargando..."):
            st.session_state.partidos = cargar_partidos(LIGAS[liga], dias)
            st.session_state.analisis = []
        st.rerun()
    st.divider()

    s = stats_por_estado()
    st.caption(
        f"📊 BD: {s.get('PENDIENTE', 0)} pend · "
        f"{s.get('GANADO', 0)} gan · {s.get('PERDIDO', 0)} per · "
        f"{s.get('ANULADO', 0)} anul"
    )

    with st.expander("🔬 Debug APIs"):
        st.caption(f"Supabase URL: `{_limpiar_url(_get('SUPABASE_URL',''))}`")
        st.caption(f"5Dollar key: {'✅' if DOLLAR_KEY else '❌'}")
        st.caption(f"Odds API: {len(ODDS_KEYS)} keys")
        st.caption(f"Groq: {'✅' if GROQ_KEY else '❌'}")
        if st.button("🧪 Probar 5Dollar"):
            r = dollar_get(DOLLAR_ENDPOINT_MATCHES, {"limit": 3})
            st.json(r if r else {"error": "sin datos"})

    st.divider()
    st.subheader("🧾 Boleto")
    if not st.session_state.boleto:
        st.caption("Sin selecciones")
    else:
        for i, b in enumerate(st.session_state.boleto):
            c1, c2 = st.columns([4, 1])
            with c1:
                st.write(f"**{b['seleccion']}**")
                st.caption(f"{b['partido']} · @{b['cuota']:.2f}")
            with c2:
                if st.button("❌", key=f"d_{i}"):
                    st.session_state.boleto.pop(i)
                    st.rerun()
        ct = 1.0
        for b in st.session_state.boleto:
            ct *= b["cuota"]
        st.metric("Cuota total", f"{ct:.2f}")
        if st.button("🗑️ Vaciar", use_container_width=True):
            st.session_state.boleto = []
            st.rerun()


st.title("⚽ LuciSport AI")
st.caption("Dixon-Coles + Groq + Odds API + 5DollarFootball + Supabase")

t1, t2, t3 = st.tabs(["📅 Partidos", "🧾 Boleto", "📚 Historial"])


# ============================================================
#  TAB 1
# ============================================================
with t1:
    if not st.session_state.partidos:
        st.info("👈 Menú » → elige liga → Cargar partidos")
    else:
        st.success(f"{len(st.session_state.partidos)} partidos en **{liga}**")
        for m in st.session_state.partidos:
            m['league_code'] = LIGAS[liga]
        ops = {f"{p['homeTeam']['name']} vs {p['awayTeam']['name']} · "
               f"{(p.get('utcDate') or '')[:16]}": i
               for i, p in enumerate(st.session_state.partidos)}
        sel = st.multiselect("Elige partidos", list(ops.keys())[:30],
                             max_selections=max_p)
        if st.button("🔍 Analizar", type="primary"):
            if not sel:
                st.warning("Selecciona al menos 1")
            else:
                res = []
                for s in sel:
                    m = st.session_state.partidos[ops[s]]
                    with st.spinner(f"Analizando {m['homeTeam']['name']}..."):
                        try:
                            picks = analizar(m)
                            for p in picks:
                                guardar_pick(m, p)
                            ia = ia_analizar(m, picks) if usar_ia else None
                            res.append({
                                "partido": f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}",
                                "fecha": (m.get('utcDate') or '')[:16],
                                "picks": picks,
                                "ia": ia,
                                "error": None,
                            })
                        except Exception as e:
                            import traceback
                            res.append({
                                "partido": f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}",
                                "fecha": "",
                                "picks": [],
                                "ia": None,
                                "error": f"{type(e).__name__}: {e}",
                                "traceback": traceback.format_exc(),
                            })
                st.session_state.analisis = res
                st.success(f"✅ {len(res)} partidos analizados y guardados")
                st.rerun()

        for a in st.session_state.analisis:
            if a.get("error"):
                st.error(f"❌ {a['partido']}: {a['error']}")
                with st.expander("Traceback"):
                    st.code(a.get("traceback", ""))
                continue
            with st.container(border=True):
                st.subheader(a["partido"])
                st.caption(f"🕐 {a['fecha']}")
                c1, c2 = st.columns(2)
                with c1:
                    st.markdown("#### 📈 Picks")
                    if not a["picks"]:
                        st.caption("Sin picks que cumplan filtros")
                    for j, p in enumerate(a["picks"]):
                        with st.container(border=True):
                            st.write(f"**{p['market']}** — {p['selection']}")
                            x1, x2, x3 = st.columns(3)
                            x1.metric("Prob", f"{p['prob']}%")
                            x2.metric("Cuota justa", f"{p['fair_odd']:.2f}")
                            cr = p.get("cuota_real")
                            if cr:
                                edge = p.get("edge_%", 0)
                                x3.metric("Cuota real", f"{cr:.2f}",
                                          delta=f"{edge:+.1f}%")
                            else:
                                x3.metric("Cuota real", "—")
                            if cr and p.get("edge_%", 0) > 5:
                                st.success(f"✅ VALUE: +{p['edge_%']:.1f}%")
                            elif cr and p.get("edge_%", 0) < -5:
                                st.warning(f"⚠️ Cuota baja: {p['edge_%']:.1f}%")
                            if st.button("➕ Boleto", key=f"a_{a['partido']}_{j}"):
                                st.session_state.boleto.append({
                                    "partido": a["partido"],
                                    "seleccion": p["selection"],
                                    "cuota": cr if cr else p["fair_odd"],
                                })
                                st.toast(f"Añadido: {p['selection']}")
                with c2:
                    st.markdown("#### 🧠 IA")
                    ia = a.get("ia")
                    if not ia:
                        st.caption("Sin IA (configura GROQ_API_KEY)")
                    else:
                        st.info(ia.get("analisis", ""))
                        st.write(f"**Pick:** {ia.get('pick_recomendado', '-')}")
                        st.write(f"**Confianza:** {ia.get('confianza', 0)}/10")
                        if ia.get("riesgos"):
                            st.warning("⚠️ " + " · ".join(ia["riesgos"]))


# ============================================================
#  TAB 2
# ============================================================
with t2:
    st.subheader("🧾 Boleto")
    if not st.session_state.boleto:
        st.info("Vacío")
    else:
        ct = 1.0
        for b in st.session_state.boleto:
            ct *= b["cuota"]
        c1, c2 = st.columns(2)
        c1.metric("Selecciones", len(st.session_state.boleto))
        c2.metric("Cuota", f"{ct:.2f}")
        for i, b in enumerate(st.session_state.boleto, 1):
            st.write(f"**{i}.** {b['seleccion']} — _{b['partido']}_ @{b['cuota']:.2f}")
        stake = st.number_input("Stake", 1.0, value=10.0, step=1.0)
        st.success(f"💰 Ganancia: **{stake * ct:.2f}**")
        if st.button("📲 Telegram"):
            msg = "🎯 *BOLETO*\n\n"
            for i, b in enumerate(st.session_state.boleto, 1):
                msg += f"{i}. {b['seleccion']} · @{b['cuota']:.2f}\n"
            msg += f"\nCuota: *{ct:.2f}* | Ganancia: {stake * ct:.2f}"
            st.success("✅ Enviado") if tg_send(msg) else st.warning("⚠️ Falló")


# ============================================================
#  TAB 3
# ============================================================
with t3:
    st.subheader("📚 Historial")
    rend = stats_rendimiento()
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Ganados", rend["ganados"])
    c2.metric("❌ Perdidos", rend["perdidos"])
    c3.metric("⭕ Anulados", rend["anulados"])
    c4.metric("🎯 Hit rate", f"{rend['hit_rate']}%")

    if rend["por_mercado"]:
        with st.expander("📊 Rendimiento por mercado"):
            for m in rend["por_mercado"]:
                st.write(
                    f"**{m['mercado']}** — "
                    f"✅ {m['ganados']} · ❌ {m['perdidos']} · "
                    f"⭕ {m['anulados']} → Hit **{m['hit']}%**"
                )

    st.divider()
    filtro = st.radio("Filtrar:",
                      ["Todos", "PENDIENTE", "GANADO", "PERDIDO", "ANULADO"],
                      horizontal=True)
    estado = None if filtro == "Todos" else filtro
    picks = listar_picks(estado=estado)

    if not picks:
        st.info(f"No hay picks con estado **{filtro}**")
    else:
        st.caption(f"Mostrando {len(picks)} picks")
        for p in picks:
            pid = p["id"]
            with st.container(border=True):
                col_info, col_acc = st.columns([3, 2])
                with col_info:
                    st.write(f"**{p['home']} vs {p['away']}**")
                    st.caption(f"🕐 {p['fecha_partido']} · {p.get('liga','')}")
                    st.write(f"🎯 {p['mercado']}: **{p['seleccion']}**")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Prob", f"{p['prob_modelo']:.0f}%")
                    c2.metric("C. justa", f"{p['cuota_justa']:.2f}")
                    c3.metric("C. real", f"{p['cuota_real']:.2f}" if p.get("cuota_real") else "—")

                    est = p["estado"]
                    if est == "GANADO": st.success("✅ GANADO")
                    elif est == "PERDIDO": st.error("❌ PERDIDO")
                    elif est == "ANULADO": st.info("⭕ ANULADO")
                    else: st.warning("⏳ PENDIENTE")

                with col_acc:
                    st.write("**Cambiar estado:**")
                    b1, b2, b3 = st.columns(3)
                    with b1:
                        if st.button("✅", key=f"g_{pid}", use_container_width=True):
                            cambiar_estado(pid, "GANADO")
                            st.rerun()
                    with b2:
                        if st.button("❌", key=f"p_{pid}", use_container_width=True):
                            cambiar_estado(pid, "PERDIDO")
                            st.rerun()
                    with b3:
                        if st.button("⭕", key=f"a_{pid}", use_container_width=True):
                            cambiar_estado(pid, "ANULADO")
                            st.rerun()
                    if st.button("🗑️ Eliminar", key=f"d_{pid}", use_container_width=True):
                        eliminar_pick(pid)
                        st.rerun()
