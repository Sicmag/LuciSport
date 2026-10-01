"""LuciSport AI — versión completa con Login + Gemini."""
import json, math, time, requests, streamlit as st
from datetime import datetime, timedelta, timezone
from supabase import create_client


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


FOOTBALL_KEY = _get("FOOTBALL_API_KEY")
TELEGRAM_TOKEN = _get("TELEGRAM_TOKEN")
TELEGRAM_CHAT = _get("TELEGRAM_CHAT_ID")
ODDS_KEYS = [k.strip() for k in _get("ODDS_API_KEYS", "").split(",") if k.strip()]
GROQ_KEY = _get("GROQ_API_KEY", "")
GEMINI_KEY = _get("GEMINI_API_KEY", "")
SUPABASE_URL = _get("SUPABASE_URL", "")
SUPABASE_KEY = _get("SUPABASE_KEY", "")

HEADERS = {'X-Auth-Token': FOOTBALL_KEY}

PROB_MIN, PROB_MAX = 55.0, 70.0
CUOTA_MIN = 1.50
MAX_PICKS, BANKROLL = 5, 100000
LINEAS = [0.5, 1.5, 2.5, 3.5, 4.5]

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
    url = _limpiar_url(SUPABASE_URL)
    if not url or not SUPABASE_KEY:
        st.error("⚠️ Faltan SUPABASE_URL o SUPABASE_KEY")
        st.stop()
    return create_client(url, SUPABASE_KEY)


def login_usuario(email, password):
    try:
        r = get_supabase().auth.sign_in_with_password({"email": email, "password": password})
        return r.user
    except Exception as e:
        return str(e)


def registrar_usuario(email, password):
    try:
        r = get_supabase().auth.sign_up({"email": email, "password": password})
        return r.user
    except Exception as e:
        st.error(f"Error al registrar: {e}")
        return None


def cerrar_sesion():
    try:
        get_supabase().auth.sign_out()
    except Exception:
        pass
    for k in list(st.session_state.keys()):
        del st.session_state[k]
    st.rerun()


# ============================================================
#  BASE DE DATOS
# ============================================================
def guardar_pick(match, pick, user_id, estado="PENDIENTE", es_value=False):
    match_id = f"{match['homeTeam']['id']}_{match['awayTeam']['id']}_{(match.get('utcDate') or '')[:16]}"
    fila = {
        "user_id": user_id,
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
        "es_value": es_value,
    }
    try:
        get_supabase().table("picks").upsert(
            fila, on_conflict="user_id,match_id,mercado,seleccion"
        ).execute()
    except Exception as e:
        st.warning(f"Error guardando pick: {e}")


def listar_picks(user_id, estado=None, limite=200, solo_value=None):
    try:
        q = (get_supabase().table("picks").select("*")
             .eq("user_id", user_id)
             .order("creado", desc=True).limit(limite))
        if estado:
            q = q.eq("estado", estado)
        if solo_value is not None:
            q = q.eq("es_value", solo_value)
        return q.execute().data or []
    except Exception as e:
        st.warning(f"Error leyendo picks: {e}")
        return []


def stats_por_estado(user_id):
    try:
        rows = (get_supabase().table("picks").select("estado")
                .eq("user_id", user_id).execute().data or [])
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


def stats_rendimiento(user_id):
    rows = listar_picks(user_id, limite=2000)
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
        if r["estado"] == "GANADO":
            mercados[m]["ganados"] += 1
        elif r["estado"] == "PERDIDO":
            mercados[m]["perdidos"] += 1
        else:
            mercados[m]["anulados"] += 1

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


def guardar_escalera(user_id, nombre, liga, cuota_obj, stake_ini, n_pasos, pasos, capital_final):
    try:
        fila = {
            "user_id": user_id,
            "nombre": nombre,
            "liga_inicio": liga,
            "cuota_objetivo": cuota_obj,
            "stake_inicial": stake_ini,
            "n_pasos": n_pasos,
            "pasos": pasos,
            "capital_final": capital_final,
            "estado": "ACTIVA",
        }
        get_supabase().table("escaleras").insert(fila).execute()
        return True
    except Exception as e:
        st.error(f"Error guardando escalera: {e}")
        return False


def listar_escaleras(user_id, limite=50):
    try:
        q = (get_supabase().table("escaleras").select("*")
             .eq("user_id", user_id)
             .order("creado", desc=True).limit(limite))
        return q.execute().data or []
    except Exception as e:
        st.warning(f"Error leyendo escaleras: {e}")
        return []


def eliminar_escalera(esc_id):
    try:
        get_supabase().table("escaleras").delete().eq("id", esc_id).execute()
    except Exception as e:
        st.warning(f"Error eliminando: {e}")


def reconstruir_analisis_desde_bd(user_id):
    try:
        rows = (get_supabase().table("picks").select("*")
                .eq("user_id", user_id).eq("estado", "PENDIENTE")
                .order("fecha_partido", desc=False).limit(500)
                .execute().data or [])
    except Exception:
        return []

    if not rows:
        return []

    partidos_dict = {}
    for r in rows:
        key = f"{r['home']} vs {r['away']}_{r['fecha_partido']}"
        if key not in partidos_dict:
            partidos_dict[key] = {
                "partido": f"{r['home']} vs {r['away']}",
                "fecha": r.get("fecha_partido", ""),
                "liga": r.get("liga", ""),
                "liga_nombre": r.get("liga", ""),
                "picks": [],
                "ia": None,
                "error": None,
            }
        partidos_dict[key]["picks"].append({
            "market": r["mercado"],
            "selection": r["seleccion"],
            "prob": r["prob_modelo"],
            "fair_odd": r["cuota_justa"],
            "cuota_real": r.get("cuota_real"),
            "edge_%": r.get("edge_pct"),
            "stake_sug": r.get("stake_sug"),
        })

    return list(partidos_dict.values())


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
#  DATOS API
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
#  MOTOR DE ANÁLISIS
# ============================================================
def analizar(match, liga_codigo):
    h, a = match['homeTeam']['name'], match['awayTeam']['name']
    hs = get_team_data(match['homeTeam']['id'])
    as_ = get_team_data(match['awayTeam']['id'])

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

    add("1X2", f"Gana {h}", pL)
    add("1X2", f"Gana {a}", pV)
    add("1X2", f"1X {h}", pL + pE)
    add("1X2", f"X2 {a}", pV + pE)

    for mk, lam in [("Goles totales", tot), (f"Goles {h}", exH),
                    (f"Goles {a}", exA), ("Goles 1T", exH1 + exA1),
                    ("Goles 2T", exH2 + exA2)]:
        for ln in LINEAS:
            po, pu = over_under(lam, ln)
            add(mk, f"+{ln}", po)
            add(mk, f"-{ln}", pu)

    btts = round(((1 - poisson(exH, 0)) * (1 - poisson(exA, 0))) * 100, 1)
    add("BTTS", "Ambos marcan", btts)

    try:
        cuotas_oddsapi = get_odds_oddsapi(match)
        for p in picks:
            if p["market"] in cuotas_oddsapi and p["selection"] in cuotas_oddsapi[p["market"]]:
                p["cuota_real"] = cuotas_oddsapi[p["market"]][p["selection"]]
                if p["fair_odd"] > 0:
                    p["edge_%"] = round((p["cuota_real"] / p["fair_odd"] - 1) * 100, 2)
    except Exception:
        pass

    for p in picks:
        p["stake_sug"] = kelly(p["prob"], p["fair_odd"])

    return picks


# ============================================================
#  IA CON GEMINI
# ============================================================
def ia_analizar_completo(match, picks, cuotas_reales=None):
    """Análisis avanzado con IA usando Google Gemini."""
    if not GEMINI_KEY:
        return None
    h = match['homeTeam']['name']
    a = match['awayTeam']['name']
    fecha = (match.get('utcDate') or '')[:16]

    sys_prompt = """Eres un analista profesional de apuestas deportivas con 15 años de experiencia.
Recibes datos de un partido de fútbol y los picks generados por un modelo Dixon-Coles.

Tu trabajo:
1. Analizar el partido de forma concisa (2-3 frases de contexto).
2. Identificar la MEJOR apuesta entre los picks (si existe).
3. Evaluar el VALOR de cada pick con edge positivo.
4. Alertar sobre riesgos o señales contradictorias.
5. Dar una confianza del 1 al 10 al pick recomendado.

FORMATO DE RESPUESTA (JSON estricto):
{
  "contexto": "análisis breve de 2-3 frases sobre el partido",
  "mejor_pick": {
    "mercado": "nombre del mercado",
    "seleccion": "selección exacta",
    "razon": "por qué es la mejor opción"
  },
  "confianza": 7,
  "riesgos": ["riesgo1", "riesgo2"],
  "resumen": "una frase final con la recomendación principal"
}"""

    picks_ordenados = sorted(
        picks,
        key=lambda x: (x.get("edge_%") or -999, x["prob"]),
        reverse=True
    )[:8]

    txt = f"""PARTIDO: {h} vs {a}
FECHA: {fecha}

=== PICKS DEL MODELO ===
"""
    for p in picks_ordenados:
        edge = p.get("edge_%")
        cuota_r = p.get("cuota_real")
        linea = f"- [{p['market']}] {p['selection']}: prob {p['prob']}%, cuota justa {p['fair_odd']:.2f}"
        if cuota_r:
            linea += f", cuota real {cuota_r:.2f}"
        if edge is not None:
            linea += f", edge {edge:+.1f}%"
        txt += linea + "\n"

    txt += "\nGenera el análisis en el formato JSON especificado."

    try:
        url = (f"https://generativelanguage.googleapis.com/v1beta/"
               f"models/gemini-1.5-flash:generateContent?key={GEMINI_KEY}")
        payload = {
            "system_instruction": {
                "parts": [{"text": sys_prompt}]
            },
            "contents": [
                {"role": "user", "parts": [{"text": txt}]}
            ],
            "generationConfig": {
                "temperature": 0.3,
                "maxOutputTokens": 800,
                "responseMimeType": "application/json"
            }
        }
        r = requests.post(url, json=payload, timeout=30)
        if r.status_code == 200:
            data = r.json()
            texto = data["candidates"][0]["content"]["parts"][0]["text"]
            return json.loads(texto)
        else:
            st.warning(f"Error Gemini: {r.status_code} - {r.text[:200]}")
    except Exception as e:
        st.warning(f"Error llamando a Gemini: {e}")
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


# ============================================================
#  LOGIN
# ============================================================
if "user" not in st.session_state:
    st.session_state.user = None

if st.session_state.user is None:
    st.markdown("# ⚽ LuciSport AI")
    st.markdown("### Inicia sesión o crea tu cuenta")
    st.divider()

    tab_login, tab_registro = st.tabs(["🔑 Iniciar sesión", "📝 Crear cuenta"])

    with tab_login:
        with st.form("form_login"):
            email = st.text_input("Email", key="login_email")
            password = st.text_input("Contraseña", type="password", key="login_pass")
            submit = st.form_submit_button("Entrar", type="primary",
                                            use_container_width=True)
            if submit:
                if not email or not password:
                    st.error("Completa email y contraseña")
                else:
                    user = login_usuario(email, password)
                    if user and not isinstance(user, str):
                        st.session_state.user = {"id": user.id, "email": user.email}
                        st.session_state.analisis_cargado = False
                        st.success(f"Bienvenido {user.email}")
                        st.rerun()
                    else:
                        msg = user if isinstance(user, str) else "Email o contraseña incorrectos"
                        st.error(f"Error: {msg}")

    with tab_registro:
        with st.form("form_registro"):
            email_r = st.text_input("Email", key="reg_email")
            password_r = st.text_input("Contraseña", type="password", key="reg_pass")
            password_r2 = st.text_input("Repetir contraseña", type="password",
                                         key="reg_pass2")
            submit_r = st.form_submit_button("Crear cuenta", type="primary",
                                              use_container_width=True)
            if submit_r:
                if not email_r or not password_r:
                    st.error("Completa todos los campos")
                elif password_r != password_r2:
                    st.error("Las contraseñas no coinciden")
                elif len(password_r) < 6:
                    st.error("La contraseña debe tener al menos 6 caracteres")
                else:
                    user = registrar_usuario(email_r, password_r)
                    if user:
                        st.success("✅ Cuenta creada. Revisa tu email para confirmar, "
                                   "luego inicia sesión.")
                    else:
                        st.error("No se pudo crear la cuenta")
    st.stop()


# ============================================================
#  APP AUTENTICADA
# ============================================================
USER_ID = st.session_state.user["id"]
USER_EMAIL = st.session_state.user["email"]

if "partidos" not in st.session_state:
    st.session_state.partidos = []
if "analisis" not in st.session_state:
    st.session_state.analisis = []
if "boleto" not in st.session_state:
    st.session_state.boleto = []
if "analisis_cargado" not in st.session_state:
    st.session_state.analisis_cargado = False
if "usar_ia" not in st.session_state:
    st.session_state.usar_ia = True


if not st.session_state.analisis_cargado:
    with st.spinner("Recuperando análisis guardados..."):
        recuperados = reconstruir_analisis_desde_bd(USER_ID)
    if recuperados:
        st.session_state.analisis = recuperados
    st.session_state.analisis_cargado = True


# ============================================================
#  SIDEBAR
# ============================================================
with st.sidebar:
    st.title("⚽ LuciSport AI")
    st.caption(f"👤 {USER_EMAIL}")

    if st.button("🚪 Cerrar sesión", use_container_width=True):
        cerrar_sesion()

    st.divider()

    liga_nombre = st.selectbox("Liga", list(LIGAS.keys()))
    liga_codigo = LIGAS[liga_nombre]
    dias = st.slider("Días a futuro", 1, 14, 5)
    usar_ia = st.toggle("IA (Gemini)", value=st.session_state.usar_ia)
    st.session_state.usar_ia = usar_ia
    max_p = st.slider("Máx. partidos", 1, 10, 5)
    st.divider()

    if st.button("📥 Cargar partidos", use_container_width=True, type="primary"):
        with st.spinner("Cargando..."):
            st.session_state.partidos = cargar_partidos(liga_codigo, dias)
        st.rerun()

    if st.button("🔄 Recargar análisis desde BD", use_container_width=True):
        st.session_state.analisis = []
        st.session_state.analisis_cargado = False
        st.toast("Recargando...")
        st.rerun()

    st.divider()
    s = stats_por_estado(USER_ID)
    st.caption(
        f"📊 BD: {s.get('PENDIENTE', 0)} pend · "
        f"{s.get('GANADO', 0)} gan · {s.get('PERDIDO', 0)} per · "
        f"{s.get('ANULADO', 0)} anul"
    )
    st.caption(f"🧠 Análisis: **{len(st.session_state.analisis)}** partidos")

    with st.expander("🔬 Debug APIs"):
        st.caption(f"Supabase: `{_limpiar_url(SUPABASE_URL)[:35]}...`")
        st.caption(f"Odds API: {len(ODDS_KEYS)} keys")
        st.caption(f"Gemini: {'✅' if GEMINI_KEY else '❌'}")
        st.caption(f"Groq: {'✅' if GROQ_KEY else '❌ (no usado)'}")

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


# ============================================================
#  HEADER
# ============================================================
st.title("⚽ LuciSport AI")
st.caption("Dixon-Coles + Gemini + Odds API + Supabase")

t1, t2, t3, t4, t5, t6 = st.tabs([
    "📅 Partidos",
    "🔺 Escalera",
    "📋 Escaleras",
    "🧾 Boleto",
    "💰 Value Bets",
    "🎲 Otras Apuestas",
])


# ============================================================
#  TAB 1 — PARTIDOS
# ============================================================
with t1:
    if not st.session_state.partidos:
        st.info("👈 Menú » → elige liga → Cargar partidos")
    else:
        st.success(f"{len(st.session_state.partidos)} partidos en **{liga_nombre}**")
        for m in st.session_state.partidos:
            m['league_code'] = liga_codigo
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
                            picks = analizar(m, liga_codigo)
                            for p in picks:
                                if not (PROB_MIN <= p["prob"] <= PROB_MAX
                                        and p["fair_odd"] >= CUOTA_MIN):
                                    continue
                                es_value = (p.get("edge_%") or 0) > 3
                                guardar_pick(m, p, USER_ID, es_value=es_value)
                            ia = ia_analizar_completo(m, picks) if usar_ia else None
                            res.append({
                                "partido": f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}",
                                "fecha": (m.get('utcDate') or '')[:16],
                                "picks": picks,
                                "ia": ia,
                                "liga": liga_codigo,
                                "liga_nombre": liga_nombre,
                                "error": None,
                            })
                        except Exception as e:
                            import traceback
                            res.append({
                                "partido": f"{m['homeTeam']['name']} vs {m['awayTeam']['name']}",
                                "fecha": "",
                                "picks": [],
                                "ia": None,
                                "liga": liga_codigo,
                                "liga_nombre": liga_nombre,
                                "error": f"{type(e).__name__}: {e}",
                                "traceback": traceback.format_exc(),
                            })
                partes_nuevos = {a["partido"] for a in res}
                previos = [a for a in st.session_state.analisis
                           if a.get("partido") not in partes_nuevos]
                st.session_state.analisis = previos + res
                st.success(f"✅ {len(res)} analizados. Total: {len(st.session_state.analisis)}")
                st.rerun()

        if st.session_state.analisis:
            ligas_acum = sorted(set(a.get("liga_nombre", a.get("liga", "?"))
                                    for a in st.session_state.analisis))
            st.caption(f"📊 Acumulados: **{len(ligas_acum)}** liga(s): "
                       f"_{', '.join(ligas_acum)}_")

        for a in st.session_state.analisis:
            if a.get("error"):
                st.error(f"❌ {a['partido']}: {a['error']}")
                with st.expander("Traceback"):
                    st.code(a.get("traceback", ""))
                continue
            with st.container(border=True):
                st.subheader(a["partido"])
                st.caption(f"🕐 {a['fecha']} · {a.get('liga_nombre', a.get('liga',''))}")
                c1, c2 = st.columns([3, 2])
                with c1:
                    st.markdown("#### 📈 Picks del modelo")
                    candidatos = [p for p in a["picks"]
                                  if PROB_MIN <= p["prob"] <= PROB_MAX
                                  and p["fair_odd"] >= CUOTA_MIN]
                    top5 = sorted(candidatos,
                                  key=lambda x: (x.get("edge_%") or -999, x["prob"]),
                                  reverse=True)[:5]
                    if not top5:
                        st.caption("Sin picks que cumplan filtros")
                    for j, p in enumerate(top5):
                        with st.container(border=True):
                            st.write(f"**{p['market']}** — {p['selection']}")
                            x1, x2, x3 = st.columns(3)
                            x1.metric("Prob", f"{p['prob']}%")
                            x2.metric("Justa", f"{p['fair_odd']:.2f}")
                            cr = p.get("cuota_real")
                            if cr:
                                edge = p.get("edge_%", 0)
                                x3.metric("Real", f"{cr:.2f}", delta=f"{edge:+.1f}%")
                            else:
                                x3.metric("Real", "—")
                            if cr and p.get("edge_%", 0) > 5:
                                st.success(f"✅ VALUE: +{p['edge_%']:.1f}%")
                            if st.button("➕ Boleto", key=f"a_{a['partido']}_{j}"):
                                st.session_state.boleto.append({
                                    "partido": a["partido"],
                                    "seleccion": f"[{p['market']}] {p['selection']}",
                                    "cuota": cr if cr else p["fair_odd"],
                                })
                                st.toast(f"Añadido: {p['selection']}")

                with c2:
                    st.markdown("#### 🧠 Análisis IA (Gemini)")
                    ia = a.get("ia")
                    if not ia:
                        st.caption("Sin IA. Activa el toggle **IA (Gemini)** y re-analiza.")
                    else:
                        ctx = ia.get("contexto", "")
                        if ctx:
                            st.info(ctx)
                        mejor = ia.get("mejor_pick") or {}
                        if mejor:
                            st.markdown("**🎯 Mejor pick:**")
                            st.write(f"_{mejor.get('mercado', '-')}_: "
                                     f"**{mejor.get('seleccion', '-')}**")
                            if mejor.get("razon"):
                                st.caption(mejor["razon"])
                        conf = ia.get("confianza", 0)
                        st.write(f"**Confianza:** {conf}/10")
                        if ia.get("riesgos"):
                            st.warning("⚠️ " + " · ".join(ia["riesgos"]))
                        if ia.get("resumen"):
                            st.caption(f"💡 {ia['resumen']}")


# ============================================================
#  TAB 2 — ESCALERA
# ============================================================
with t2:
    st.subheader("🔺 Reto Escalera")
    st.caption("Encadena apuestas con cuota fija sin repetir partido ni solapar horarios.")

    ligas_acum = sorted(set(a.get("liga_nombre", a.get("liga", "?"))
                            for a in st.session_state.analisis))
    if ligas_acum:
        st.info(f"📊 Picks acumulados de **{len(ligas_acum)}** liga(s): "
                f"_{', '.join(ligas_acum)}_")
    else:
        st.warning("👈 Analiza partidos primero.")

    c1, c2, c3 = st.columns(3)
    with c1:
        stake_inicial = st.number_input("Stake inicial ($)", min_value=1.0,
                                        value=10.0, step=1.0, key="esc_stake")
    with c2:
        cuota_escalera = st.number_input("Cuota por paso", min_value=1.20,
                                         max_value=2.00, value=1.40, step=0.05,
                                         key="esc_cuota")
    with c3:
        n_pasos = st.slider("Nº de pasos", 2, 20, 10, key="esc_pasos")

    st.markdown("### 📈 Progresión teórica")
    progresion = []
    capital = stake_inicial
    for i in range(1, n_pasos + 1):
        capital_nuevo = capital * cuota_escalera
        progresion.append({
            "Paso": i,
            "Capital antes": round(capital, 2),
            "Capital después": round(capital_nuevo, 2),
            "Ganancia": round(capital_nuevo - capital, 2),
        })
        capital = capital_nuevo
    st.dataframe(progresion, use_container_width=True, hide_index=True)
    st.success(f"💰 Capital final tras {n_pasos} pasos: **${capital:.2f}**")

    st.divider()
    tolerancia = st.slider("Tolerancia en la cuota (±)", 0.02, 0.20, 0.10, 0.01)

    mejor_por_partido = {}
    for a in st.session_state.analisis:
        for p in a["picks"]:
            cuota_ref = p.get("cuota_real") or p["fair_odd"]
            if abs(cuota_ref - cuota_escalera) <= tolerancia and p["prob"] >= 55.0:
                dist = abs(cuota_ref - cuota_escalera)
                key = a["partido"]
                if key not in mejor_por_partido or dist < mejor_por_partido[key]["distancia"]:
                    mejor_por_partido[key] = {
                        **p,
                        "partido": a["partido"],
                        "fecha": a["fecha"],
                        "liga": a.get("liga", "?"),
                        "liga_nombre": a.get("liga_nombre", "?"),
                        "cuota_ref": cuota_ref,
                        "distancia": dist,
                    }

    candidatos_esc = list(mejor_por_partido.values())
    candidatos_esc.sort(key=lambda x: x["fecha"])

    if candidatos_esc:
        with st.expander(f"🔍 Ver {len(candidatos_esc)} candidatos", expanded=False):
            for i, p in enumerate(candidatos_esc[:20]):
                st.write(f"• **[{p['market']}] {p['selection']}** — {p['partido']} "
                         f"({p['fecha'][11:16]}) @{p['cuota_ref']:.2f}")
    else:
        st.info("👈 Analiza partidos primero.")

    st.divider()
    st.markdown("### 🧮 Simulador")

    if st.button("🎲 Simular escalera", type="primary"):
        seleccionados = []
        ultima_hora_fin = None
        for p in candidatos_esc:
            try:
                hora_inicio = datetime.fromisoformat(p["fecha"].replace(" ", "T"))
            except Exception:
                continue
            if ultima_hora_fin is not None and hora_inicio < ultima_hora_fin:
                continue
            seleccionados.append(p)
            ultima_hora_fin = hora_inicio + timedelta(hours=2)
            if len(seleccionados) >= n_pasos:
                break

        if len(seleccionados) < n_pasos:
            st.warning(f"Solo {len(seleccionados)} partidos sin solapar.")

        if seleccionados:
            simulacion = []
            capital = stake_inicial
            for i, p in enumerate(seleccionados, 1):
                cuota_paso = p["cuota_ref"]
                capital_despues = capital * cuota_paso
                simulacion.append({
                    "Paso": i,
                    "Liga": p.get("liga", "?"),
                    "Mercado": p["market"],
                    "Pick": p["selection"],
                    "Partido": p["partido"],
                    "Hora": p["fecha"][11:16],
                    "Cuota": round(cuota_paso, 2),
                    "Capital antes": round(capital, 2),
                    "Capital después": round(capital_despues, 2),
                })
                capital = capital_despues

            st.dataframe(simulacion, use_container_width=True, hide_index=True)
            st.success(f"💰 Si aciertas los {len(seleccionados)}: **${capital:.2f}**")

            st.session_state["ultima_simulacion"] = {
                "seleccionados": seleccionados,
                "simulacion": simulacion,
                "capital_final": capital,
                "stake_inicial": stake_inicial,
                "cuota_escalera": cuota_escalera,
                "n_pasos": len(seleccionados),
                "liga": liga_codigo,
            }

    sim = st.session_state.get("ultima_simulacion")
    if sim:
        st.divider()
        col_a, col_b = st.columns(2)
        with col_a:
            nombre_esc = st.text_input("Nombre", value=f"Escalera {sim['liga']} "
                                        f"{datetime.now().strftime('%d/%m %H:%M')}",
                                        key="esc_nombre")
            if st.button("💾 Guardar reto", type="primary", use_container_width=True):
                ok = guardar_escalera(USER_ID, nombre_esc, sim["liga"],
                                       sim["cuota_escalera"], sim["stake_inicial"],
                                       sim["n_pasos"], sim["simulacion"],
                                       sim["capital_final"])
                if ok:
                    st.success(f"✅ Guardada: {nombre_esc}")
        with col_b:
            if st.button("📲 Telegram", use_container_width=True):
                msg = "🔺 *RETO ESCALERA*\n\n"
                for s in sim["simulacion"]:
                    msg += (f"*Paso {s['Paso']}* ({s['Hora']}) [{s.get('Liga','?')}]\n"
                            f"  [{s['Mercado']}] {s['Pick']}\n"
                            f"  _{s['Partido']}_ @{s['Cuota']}\n\n")
                msg += f"💰 Final: *${sim['capital_final']:.2f}*"
                st.success("✅") if tg_send(msg) else st.warning("⚠️")
            if st.button("➕ Boleto", use_container_width=True):
                for p in sim["seleccionados"]:
                    st.session_state.boleto.append({
                        "partido": p["partido"],
                        "seleccion": f"[{p['market']}] {p['selection']}",
                        "cuota": p["cuota_ref"],
                    })
                st.success(f"✅ {len(sim['seleccionados'])} añadidos")
                st.rerun()


# ============================================================
#  TAB 3 — ESCALERAS GUARDADAS
# ============================================================
with t3:
    st.subheader("📋 Escaleras guardadas")
    escaleras = listar_escaleras(USER_ID)

    if not escaleras:
        st.info("No has guardado ninguna escalera aún.")
    else:
        st.caption(f"Tienes **{len(escaleras)}** escaleras")
        for esc in escaleras:
            with st.container(border=True):
                col1, col2, col3 = st.columns([3, 2, 1])
                with col1:
                    st.write(f"**{esc['nombre']}**")
                    st.caption(f"🕐 {esc.get('creado', '')[:16]}")
                with col2:
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Stake", f"${esc.get('stake_inicial', 0):.2f}")
                    c2.metric("Pasos", esc.get("n_pasos", 0))
                    c3.metric("Final", f"${esc.get('capital_final', 0):.2f}")
                with col3:
                    if st.button("🗑️", key=f"del_{esc['id']}", use_container_width=True):
                        eliminar_escalera(esc["id"])
                        st.rerun()
                with st.expander("Ver pasos"):
                    for p in esc.get("pasos", []):
                        st.write(f"**Paso {p.get('Paso', '?')}** ({p.get('Hora','?')})")
                        st.write(f"[{p.get('Mercado', '—')}] {p.get('Pick', '?')}")
                        st.caption(f"{p.get('Partido','?')} — @{p.get('Cuota', 0)}")


# ============================================================
#  TAB 4 — BOLETO
# ============================================================
with t4:
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
            st.success("✅") if tg_send(msg) else st.warning("⚠️")


# ============================================================
#  TAB 5 — VALUE BETS
# ============================================================
with t5:
    st.subheader("💰 Value Bets")
    st.caption("Picks con edge > 3% — valor apostable.")

    rend = stats_rendimiento(USER_ID)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("✅ Ganados", rend["ganados"])
    c2.metric("❌ Perdidos", rend["perdidos"])
    c3.metric("⭕ Anulados", rend["anulados"])
    c4.metric("🎯 Hit rate", f"{rend['hit_rate']}%")

    if rend["por_mercado"]:
        with st.expander("📊 Rendimiento por mercado"):
            for m in rend["por_mercado"]:
                st.write(f"**{m['mercado']}** — ✅ {m['ganados']} · ❌ {m['perdidos']} "
                         f"· ⭕ {m['anulados']} → Hit **{m['hit']}%**")

    st.divider()
    filtro = st.radio("Filtrar:", ["Todos", "PENDIENTE", "GANADO", "PERDIDO", "ANULADO"],
                      horizontal=True, key="filtro_v")
    estado = None if filtro == "Todos" else filtro
    picks = listar_picks(USER_ID, estado=estado, limite=500, solo_value=True)

    if not picks:
        st.info(f"No hay Value Bets con estado **{filtro}**")
    else:
        st.caption(f"Mostrando **{len(picks)}** Value Bets")
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
                    edge = p.get("edge_pct") or 0
                    if edge > 3:
                        st.success(f"✅ Edge: +{edge:.1f}%")
                    est = p["estado"]
                    iconos = {"GANADO": "✅", "PERDIDO": "❌", "ANULADO": "⭕"}
                    st.write(f"{iconos.get(est, '⏳')} **{est}**")
                with col_acc:
                    b1, b2, b3 = st.columns(3)
                    with b1:
                        if st.button("✅", key=f"gv_{pid}", use_container_width=True):
                            cambiar_estado(pid, "GANADO"); st.rerun()
                    with b2:
                        if st.button("❌", key=f"pv_{pid}", use_container_width=True):
                            cambiar_estado(pid, "PERDIDO"); st.rerun()
                    with b3:
                        if st.button("⭕", key=f"av_{pid}", use_container_width=True):
                            cambiar_estado(pid, "ANULADO"); st.rerun()
                    if st.button("🗑️", key=f"dv_{pid}", use_container_width=True):
                        eliminar_pick(pid); st.rerun()


# ============================================================
#  TAB 6 — OTRAS APUESTAS
# ============================================================
with t6:
    st.subheader("🎲 Otras Apuestas")
    st.caption("Picks sin edge o edge bajo. Referencia.")

    filtro = st.radio("Filtrar:", ["Todos", "PENDIENTE", "GANADO", "PERDIDO", "ANULADO"],
                      horizontal=True, key="filtro_o")
    estado = None if filtro == "Todos" else filtro
    picks = listar_picks(USER_ID, estado=estado, limite=500, solo_value=False)

    if not picks:
        st.info(f"No hay Otras Apuestas con estado **{filtro}**")
    else:
        st.caption(f"Mostrando **{len(picks)}** Otras Apuestas")
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
                    iconos = {"GANADO": "✅", "PERDIDO": "❌", "ANULADO": "⭕"}
                    st.write(f"{iconos.get(est, '⏳')} **{est}**")
                with col_acc:
                    b1, b2, b3 = st.columns(3)
                    with b1:
                        if st.button("✅", key=f"go_{pid}", use_container_width=True):
                            cambiar_estado(pid, "GANADO"); st.rerun()
                    with b2:
                        if st.button("❌", key=f"po_{pid}", use_container_width=True):
                            cambiar_estado(pid, "PERDIDO"); st.rerun()
                    with b3:
                        if st.button("⭕", key=f"ao_{pid}", use_container_width=True):
                            cambiar_estado(pid, "ANULADO"); st.rerun()
                    if st.button("🗑️", key=f"do_{pid}", use_container_width=True):
                        eliminar_pick(pid); st.rerun()
