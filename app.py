"""LuciSport AI — con base de datos."""
import json, math, time, sqlite3, os, requests, streamlit as st
from datetime import datetime, timedelta, timezone

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
HEADERS = {'X-Auth-Token': FOOTBALL_KEY}

PROB_MIN, CUOTA_MIN = 55.0, 1.50
MAX_PICKS, BANKROLL = 3, 100000
LINEAS = [0.5, 1.5, 2.5, 3.5, 4.5]

DB_PATH = "lucisport.db"

SPORT_KEYS = {"PL": "soccer_epl", "PD": "soccer_spain_la_liga",
              "SA": "soccer_italy_serie_a", "BL1": "soccer_germany_bundesliga",
              "FL1": "soccer_france_ligue_one", "CL": "soccer_uefa_champs_league",
              "ELI": "soccer_uefa_europa_league", "CLI": "soccer_conmebol_libertadores"}

LIGAS = {"Premier League": "PL", "La Liga": "PD", "Serie A": "SA",
         "Bundesliga": "BL1", "Ligue 1": "FL1", "Champions": "CL",
         "Europa League": "ELI", "Libertadores": "CLI"}


# ============================================================
#  BASE DE DATOS
# ============================================================
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS picks (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        match_id     TEXT NOT NULL,
        fecha_partido TEXT,
        liga         TEXT,
        home         TEXT,
        away         TEXT,
        mercado      TEXT,
        seleccion    TEXT,
        prob_modelo  REAL,
        cuota_justa  REAL,
        cuota_real   REAL,
        edge_pct     REAL,
        stake_sug    REAL,
        estado       TEXT DEFAULT 'PENDIENTE',
        resultado_ft TEXT,
        creado       TEXT,
        resuelto     TEXT,
        UNIQUE(match_id, mercado, seleccion)
    );
    CREATE INDEX IF NOT EXISTS idx_estado ON picks(estado);
    CREATE INDEX IF NOT EXISTS idx_creado ON picks(creado);
    CREATE INDEX IF NOT EXISTS idx_liga   ON picks(liga);
    """)
    con.commit()
    con.close()


def guardar_pick(match, pick, estado="PENDIENTE"):
    """Guarda o actualiza un pick en la BD."""
    match_id = f"{match['homeTeam']['id']}_{match['awayTeam']['id']}_{match.get('utcDate','')[:16]}"
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        cur.execute("""
            INSERT INTO picks
              (match_id, fecha_partido, liga, home, away, mercado, seleccion,
               prob_modelo, cuota_justa, cuota_real, edge_pct, stake_sug,
               estado, creado)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(match_id, mercado, seleccion) DO UPDATE SET
              prob_modelo=excluded.prob_modelo,
              cuota_justa=excluded.cuota_justa,
              cuota_real=excluded.cuota_real,
              edge_pct=excluded.edge_pct,
              stake_sug=excluded.stake_sug
        """, (
            match_id,
            (match.get('utcDate') or '')[:16],
            match.get('league_code', ''),
            match['homeTeam']['name'],
            match['awayTeam']['name'],
            pick['market'],
            pick['selection'],
            pick['prob'],
            pick['fair_odd'],
            pick.get('cuota_real'),
            pick.get('edge_%'),
            pick.get('stake_sug'),
            estado,
            datetime.now(timezone.utc).isoformat(),
        ))
        con.commit()
    except Exception as e:
        st.warning(f"Error guardando pick: {e}")
    finally:
        con.close()


def listar_picks(estado=None, limite=200):
    """Devuelve picks filtrados por estado."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    if estado:
        cur.execute("""SELECT id, fecha_partido, liga, home, away, mercado,
                              seleccion, prob_modelo, cuota_justa, cuota_real,
                              edge_pct, estado, creado
                       FROM picks WHERE estado=?
                       ORDER BY creado DESC LIMIT ?""", (estado, limite))
    else:
        cur.execute("""SELECT id, fecha_partido, liga, home, away, mercado,
                              seleccion, prob_modelo, cuota_justa, cuota_real,
                              edge_pct, estado, creado
                       FROM picks ORDER BY creado DESC LIMIT ?""", (limite,))
    rows = cur.fetchall()
    con.close()
    return rows


def stats_por_estado():
    """Cuenta picks por estado."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT estado, COUNT(*) FROM picks GROUP BY estado")
    res = dict(cur.fetchall())
    con.close()
    return res


def cambiar_estado(pick_id, nuevo_estado):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""UPDATE picks SET estado=?, resuelto=?
                   WHERE id=?""",
                (nuevo_estado, datetime.now(timezone.utc).isoformat(), pick_id))
    con.commit()
    con.close()


def eliminar_pick(pick_id):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("DELETE FROM picks WHERE id=?", (pick_id,))
    con.commit()
    con.close()


def stats_rendimiento():
    """Devuelve el rendimiento por mercado y global."""
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""SELECT estado, COUNT(*) FROM picks
                   WHERE estado IN ('GANADO','PERDIDO','ANULADO')
                   GROUP BY estado""")
    res = dict(cur.fetchall())
    ganados = res.get('GANADO', 0)
    perdidos = res.get('PERDIDO', 0)
    anulados = res.get('ANULADO', 0)
    total_resueltos = ganados + perdidos
    hit_rate = round(ganados / total_resueltos * 100, 1) if total_resueltos else 0

    # Por mercado
    cur.execute("""SELECT mercado,
                     SUM(CASE WHEN estado='GANADO' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN estado='PERDIDO' THEN 1 ELSE 0 END),
                     SUM(CASE WHEN estado='ANULADO' THEN 1 ELSE 0 END)
                   FROM picks
                   WHERE estado IN ('GANADO','PERDIDO','ANULADO')
                   GROUP BY mercado""")
    mercados = []
    for m, g, p, a in cur.fetchall():
        tot = (g or 0) + (p or 0)
        mercados.append({
            "mercado": m, "ganados": g or 0, "perdidos": p or 0,
            "anulados": a or 0,
            "hit": round((g or 0) / tot * 100, 1) if tot else 0,
        })

    con.close()
    return {
        "ganados": ganados, "perdidos": perdidos, "anulados": anulados,
        "hit_rate": hit_rate, "por_mercado": mercados,
    }


init_db()


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
#  DATOS DE API
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

def get_odds(match):
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
                    p = o.get("price")
                    nm = o.get("name", "")
                    if not p: continue
                    if nm == "Draw": ch["d"].append(float(p))
                    elif nm == ha: ch["h"].append(float(p))
                    elif nm == aa: ch["a"].append(float(p))
            elif mk.get("key") == "totals":
                for o in mk.get("outcomes", []):
                    pt = str(o.get("point", "")).strip()
                    nm = o.get("name", "")
                    p = o.get("price")
                    if not p or not pt: continue
                    ou.setdefault(pt, {"Over": [], "Under": []})
                    if nm in ("Over", "Under"):
                        ou[pt][nm].append(float(p))
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
#  ANÁLISIS
# ============================================================
def analizar(match):
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
        cuotas = get_odds(match)
        for p in picks:
            if p["market"] in cuotas and p["selection"] in cuotas[p["market"]]:
                p["cuota_real"] = cuotas[p["market"]][p["selection"]]
                if p["fair_odd"] > 0:
                    p["edge_%"] = round((p["cuota_real"] / p["fair_odd"] - 1) * 100, 2)
    except Exception:
        pass
    validos = [p for p in picks if p["prob"] >= PROB_MIN and p["fair_odd"] >= CUOTA_MIN]
    pm = {}
    for p in validos:
        if p["market"] not in pm or p["prob"] > pm[p["market"]]["prob"]:
            pm[p["market"]] = p
    top = sorted(pm.values(), key=lambda x: x["prob"], reverse=True)[:MAX_PICKS]
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

    # Stats rápidas
    s = stats_por_estado()
    st.caption(
        f"📊 BD: {s.get('PENDIENTE', 0)} pend · "
        f"{s.get('GANADO', 0)} gan · {s.get('PERDIDO', 0)} per · "
        f"{s.get('ANULADO', 0)} anul"
    )

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


# ---------- CUERPO ----------
st.title("⚽ LuciSport AI")
st.caption("Dixon-Coles + Groq + Odds API + Base de datos")

t1, t2, t3 = st.tabs(["📅 Partidos", "🧾 Boleto", "📚 Historial"])


# ============================================================
#  TAB 1: PARTIDOS
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
                            # Guardar cada pick en BD
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
                st.success(f"✅ {len(res)} partidos analizados y guardados en BD")
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
#  TAB 2: BOLETO
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
#  TAB 3: HISTORIAL
# ============================================================
with t3:
    st.subheader("📚 Historial de picks")

    # ----- Resumen general -----
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

    # ----- Filtros -----
    filtro = st.radio(
        "Filtrar por estado:",
        ["Todos", "PENDIENTE", "GANADO", "PERDIDO", "ANULADO"],
        horizontal=True,
    )

    estado = None if filtro == "Todos" else filtro
    picks = listar_picks(estado=estado)

    if not picks:
        st.info(f"No hay picks con estado **{filtro}**")
    else:
        st.caption(f"Mostrando {len(picks)} picks")

        # Tabla con acciones por pick
        for row in picks:
            (pid, fecha, lg, home, away, mercado, seleccion,
             prob, cuota_justa, cuota_real, edge, estado_actual, creado) = row

            with st.container(border=True):
                col_info, col_acc = st.columns([3, 2])

                with col_info:
                    st.write(f"**{home} vs {away}**")
                    st.caption(f"🕐 {fecha} · {lg}")
                    st.write(f"🎯 {mercado}: **{seleccion}**")
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Prob", f"{prob:.0f}%")
                    c2.metric("C. justa", f"{cuota_justa:.2f}")
                    c3.metric("C. real", f"{cuota_real:.2f}" if cuota_real else "—")

                    # Color del estado
                    if estado_actual == "GANADO":
                        st.success("✅ GANADO")
                    elif estado_actual == "PERDIDO":
                        st.error("❌ PERDIDO")
                    elif estado_actual == "ANULADO":
                        st.info("⭕ ANULADO")
                    else:
                        st.warning("⏳ PENDIENTE")

                with col_acc:
                    st.write("**Cambiar estado:**")
                    b1, b2, b3 = st.columns(3)
                    with b1:
                        if st.button("✅", key=f"g_{pid}", help="Marcar GANADO",
                                     use_container_width=True):
                            cambiar_estado(pid, "GANADO")
                            st.rerun()
                    with b2:
                        if st.button("❌", key=f"p_{pid}", help="Marcar PERDIDO",
                                     use_container_width=True):
                            cambiar_estado(pid, "PERDIDO")
                            st.rerun()
                    with b3:
                        if st.button("⭕", key=f"a_{pid}", help="Marcar ANULADO",
                                     use_container_width=True):
                            cambiar_estado(pid, "ANULADO")
                            st.rerun()

                    if st.button("🗑️ Eliminar", key=f"d_{pid}",
                                 use_container_width=True):
                        eliminar_pick(pid)
                        st.rerun()
