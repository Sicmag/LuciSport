"""LuciSport AI — versión unificada con mercados, combinadas y auto-refresh."""
import json, math, time, requests, streamlit as st
from datetime import datetime, timedelta, timezone
from supabase import create_client
from streamlit_autorefresh import st_autorefresh


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


FOOTBALL_KEY = _get("FOOTBALL_API_KEY")
TELEGRAM_TOKEN = _get("TELEGRAM_TOKEN")
TELEGRAM_CHAT = _get("TELEGRAM_CHAT_ID")
ODDS_KEYS = [k.strip() for k in _get("ODDS_API_KEYS", "").split(",") if k.strip()]
GROQ_KEY = _get("GROQ_API_KEY", "")

HEADERS = {'X-Auth-Token': FOOTBALL_KEY}

PROB_MIN, CUOTA_MIN = 55.0, 1.50
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
def analizar(match, liga_codigo, edge_min=0.0, prob_min=None, solo_con_cuota_real=False):
    if prob_min is None:
        prob_min = PROB_MIN
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

    # Cuotas reales
    try:
        cuotas_oddsapi = get_odds_oddsapi(match)
        for p in picks:
            if p["market"] in cuotas_oddsapi and p["selection"] in cuotas_oddsapi[p["market"]]:
                p["cuota_real"] = cuotas_oddsapi[p["market"]][p["selection"]]
                if p["fair_odd"] > 0:
                    p["edge_%"] = round((p["cuota_real"] / p["fair_odd"] - 1) * 100, 2)
    except Exception:
        pass

    # Filtros
    validos = []
    for p in picks:
        if p["prob"] < prob_min:
            continue
        if p["fair_odd"] < CUOTA_MIN:
            continue
        if solo_con_cuota_real and not p.get("cuota_real"):
            continue
        if p.get("edge_%") is not None and p["edge_%"] < edge_min:
            continue
        validos.append(p)

    pm = {}
    for p in validos:
        key = f"{p['market']}|{p['selection']}"
        if key not in pm or p["prob"] > pm[key]["prob"]:
            pm[key] = p

    top = sorted(pm.values(),
                 key=lambda x: (x.get("edge_%") or -999, x["prob"]),
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


# ============================================================
#  SIDEBAR
# ============================================================
with st.sidebar:
    st.title("⚽ LuciSport AI")

    liga_nombre = st.selectbox("Liga", list(LIGAS.keys()))
    liga_codigo = LIGAS[liga_nombre]

    dias = st.slider("Días a futuro", 1, 14, 5)
    usar_ia = st.toggle("IA (Groq)", value=False)
    max_p = st.slider("Máx. partidos", 1, 5, 1)

    st.divider()
    st.subheader("⚙️ Filtros")
    edge_min = st.slider("Edge mínimo (%)", 0.0, 15.0, 4.0, 0.5,
                         help="Solo picks con edge >= este valor")
    prob_min = st.slider("Prob mínima (%)", 50.0, 85.0, 55.0, 1.0)
    solo_cuota_real = st.checkbox("Solo con cuota real", value=True,
                                  help="Descarta picks sin cuota de casas")

    st.divider()
    if st.button("📥 Cargar partidos", use_container_width=True, type="primary"):
        with st.spinner("Cargando..."):
            st.session_state.partidos = cargar_partidos(liga_codigo, dias)
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
        st.caption(f"Supabase: `{_limpiar_url(_get('SUPABASE_URL',''))[:35]}...`")
        st.caption(f"Odds API: {len(ODDS_KEYS)} keys")
        st.caption(f"Groq: {'✅' if GROQ_KEY else '❌'}")

    st.divider()
    st.subheader("🔄 Actualización")
    refresh_seg = st.selectbox(
        "Auto-refresh",
        [0, 30, 60, 300, 600],
        format_func=lambda x: "Desactivado" if x == 0 else f"Cada {x}s",
        index=0,
    )
    if refresh_seg > 0:
        st_autorefresh(interval=refresh_seg * 1000, key="auto_refresh")
        st.caption("✅ Recarga automática activa")

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
st.caption("Dixon-Coles + Groq + Odds API + Supabase")

t1, t2, t3, t4, t5 = st.tabs([
    "📅 Partidos",
    "📊 Por Mercados",
    "🔗 Combinadas",
    "🧾 Boleto",
    "📚 Historial",
])


# ============================================================
#  TAB 1: PARTIDOS
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
                            picks = analizar(m, liga_codigo,
                                             edge_min=0.0,
                                             prob_min=prob_min,
                                             solo_con_cuota_real=False)
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
#  TAB 2: POR MERCADOS
# ============================================================
with t2:
    st.subheader("📊 Picks por mercado")

    if not st.session_state.analisis:
        st.info("👈 Analiza partidos primero en la pestaña **📅 Partidos**")
    else:
        todos = []
        for a in st.session_state.analisis:
            for p in a["picks"]:
                todos.append({**p, "partido": a["partido"], "fecha": a["fecha"]})

        if not todos:
            st.warning("No hay picks analizados.")
        else:
            por_mercado = {}
            for p in todos:
                por_mercado.setdefault(p["market"], []).append(p)

            c1, c2, c3 = st.columns(3)
            c1.metric("Total picks", len(todos))
            c2.metric("Mercados", len(por_mercado))
            con_edge = [p for p in todos if p.get("edge_%")]
            if con_edge:
                edge_prom = round(sum(p["edge_%"] for p in con_edge) / len(con_edge), 1)
                c3.metric("Edge promedio", f"{edge_prom}%")

            st.divider()

            filtro_orden = st.radio(
                "Ordenar por:",
                ["Edge ↓", "Probabilidad ↓", "Cuota ↓"],
                horizontal=True,
            )

            for mercado, picks in sorted(por_mercado.items()):
                with st.expander(
                    f"**{mercado}** — {len(picks)} picks",
                    expanded=(len(por_mercado) <= 3),
                ):
                    if filtro_orden == "Edge ↓":
                        picks.sort(key=lambda x: x.get("edge_%") or -999, reverse=True)
                    elif filtro_orden == "Probabilidad ↓":
                        picks.sort(key=lambda x: x["prob"], reverse=True)
                    else:
                        picks.sort(key=lambda x: x["fair_odd"], reverse=True)

                    for i, p in enumerate(picks):
                        with st.container(border=True):
                            col1, col2, col3 = st.columns([3, 2, 1])
                            with col1:
                                st.write(f"**{p['selection']}**")
                                st.caption(f"{p['partido']} · {p['fecha']}")
                            with col2:
                                mc1, mc2, mc3 = st.columns(3)
                                mc1.metric("Prob", f"{p['prob']}%")
                                mc2.metric("C. justa", f"{p['fair_odd']:.2f}")
                                if p.get("cuota_real"):
                                    edge = p.get("edge_%", 0)
                                    mc3.metric("C. real", f"{p['cuota_real']:.2f}",
                                               delta=f"{edge:+.1f}%")
                                else:
                                    mc3.metric("C. real", "—")
                            with col3:
                                if st.button("➕", key=f"add_merc_{mercado}_{i}",
                                             help="Añadir al boleto"):
                                    st.session_state.boleto.append({
                                        "partido": p["partido"],
                                        "seleccion": p["selection"],
                                        "cuota": p.get("cuota_real") or p["fair_odd"],
                                    })
                                    st.toast(f"Añadido: {p['selection']}")


# ============================================================
#  TAB 3: COMBINADAS
# ============================================================
with t3:
    st.subheader("🔗 Crear combinada")

    todos_picks = []
    for a in st.session_state.analisis:
        for p in a["picks"]:
            todos_picks.append({**p, "partido": a["partido"], "fecha": a["fecha"]})

    if not todos_picks:
        st.info("👈 Analiza partidos primero")
    else:
        modo = st.radio(
            "Modo de construcción:",
            ["✋ Manual", "🎯 Por cuota objetivo"],
            horizontal=True,
        )

        # ---------- MANUAL ----------
        if modo == "✋ Manual":
            st.caption("Selecciona las patas. No puedes combinar 2 picks del mismo partido.")

            opciones = {}
            for i, p in enumerate(todos_picks):
                label = (f"{p['partido']} → {p['market']}: {p['selection']} "
                         f"@{p.get('cuota_real') or p['fair_odd']:.2f}")
                opciones[label] = p

            seleccion = st.multiselect(
                "Elige las patas:",
                list(opciones.keys()),
                max_selections=6,
                placeholder="Selecciona 2-6 patas",
            )

            if len(seleccion) >= 2:
                patas = [opciones[s] for s in seleccion]
                partidos_unicos = set(p["partido"] for p in patas)

                if len(partidos_unicos) < len(patas):
                    st.error("❌ No puedes combinar 2 picks del mismo partido")
                else:
                    cuota_total = 1.0
                    prob_total = 1.0
                    for p in patas:
                        cuota_total *= p.get("cuota_real") or p["fair_odd"]
                        prob_total *= p["prob"] / 100

                    prob_pct = round(prob_total * 100, 2)
                    cuota_justa_combo = round(1 / prob_total, 2) if prob_total > 0 else 0
                    edge_combo = round((cuota_total / cuota_justa_combo - 1) * 100, 1) if cuota_justa_combo > 0 else 0

                    st.divider()
                    c1, c2, c3 = st.columns(3)
                    c1.metric("Patas", len(patas))
                    c2.metric("Cuota combinada", f"{cuota_total:.2f}")
                    c3.metric("Probabilidad", f"{prob_pct}%")

                    if edge_combo > 0:
                        st.success(f"✅ Edge combinado: **+{edge_combo}%**")
                    else:
                        st.warning(f"⚠️ Edge combinado: **{edge_combo}%**")

                    with st.expander("Ver patas", expanded=True):
                        for p in patas:
                            st.write(f"• **{p['partido']}** — {p['selection']} "
                                     f"@{p.get('cuota_real') or p['fair_odd']:.2f} "
                                     f"({p['prob']}%)")

                    stake = st.number_input("Stake ($)", min_value=1.0,
                                            value=10.0, step=1.0, key="man_stake")
                    ganancia = stake * cuota_total
                    st.info(f"💰 Ganancia potencial: **${ganancia:.2f}**")

                    col_a, col_b = st.columns(2)
                    with col_a:
                        if st.button("📲 Telegram", use_container_width=True,
                                     key="tg_man"):
                            msg = "🔗 *COMBINADA MANUAL*\n\n"
                            for i, p in enumerate(patas, 1):
                                msg += (f"{i}. {p['selection']}\n"
                                        f"   _{p['partido']}_\n"
                                        f"   @{p.get('cuota_real') or p['fair_odd']:.2f}\n")
                            msg += f"\n📊 Cuota total: *{cuota_total:.2f}*"
                            msg += f"\n💰 ${stake:.2f} → ${ganancia:.2f}"
                            st.success("✅ Enviado") if tg_send(msg) else st.warning("⚠️ Falló")
                    with col_b:
                        if st.button("➕ Añadir al boleto", use_container_width=True,
                                     key="bol_man"):
                            for p in patas:
                                st.session_state.boleto.append({
                                    "partido": p["partido"],
                                    "seleccion": f"[COMBO] {p['selection']}",
                                    "cuota": p.get("cuota_real") or p["fair_odd"],
                                })
                            st.success(f"✅ {len(patas)} patas añadidas")
                            st.rerun()

        # ---------- POR CUOTA OBJETIVO ----------
        else:
            st.caption("El sistema busca la combinación que mejor se acerque a tu cuota objetivo.")

            col1, col2 = st.columns(2)
            with col1:
                cuota_obj = st.number_input("Cuota objetivo", min_value=2.0,
                                            max_value=100.0, value=5.0, step=0.5)
            with col2:
                max_legs = st.slider("Máximo de patas", 2, 8, 5)

            with st.expander("⚙️ Filtros avanzados"):
                prob_min_combo = st.slider("Prob mínima por pata (%)", 50.0,
                                           80.0, 55.0, 1.0)
                solo_con_edge = st.checkbox("Solo picks con edge > 0", value=True)

            if st.button("🎯 Construir combinada óptima", type="primary"):
                candidatos = [
                    p for p in todos_picks
                    if p["prob"] >= prob_min_combo
                    and p["fair_odd"] >= 1.5
                    and (not solo_con_edge or (p.get("edge_%") or 0) > 0)
                ]

                if len(candidatos) < 2:
                    st.error("No hay suficientes picks que cumplan los filtros.")
                else:
                    candidatos.sort(
                        key=lambda x: (x.get("edge_%") or 0, x["prob"]),
                        reverse=True,
                    )

                    mejor = None
                    mejor_diff = float('inf')

                    for n_legs in range(2, max_legs + 1):
                        sel = []
                        partidos_usados = set()
                        for p in candidatos:
                            if len(sel) >= n_legs:
                                break
                            if p["partido"] in partidos_usados:
                                continue
                            sel.append(p)
                            partidos_usados.add(p["partido"])

                        if len(sel) < 2:
                            continue

                        prob_acum = 1.0
                        for s in sel:
                            prob_acum *= s["prob"] / 100.0

                        cuota = 1 / prob_acum if prob_acum > 0 else 0
                        diff = abs(cuota - cuota_obj)
                        if cuota >= cuota_obj:
                            diff *= 0.85

                        if diff < mejor_diff:
                            mejor_diff = diff
                            mejor = (list(sel), prob_acum, cuota)

                    if not mejor:
                        st.error("No se pudo construir una combinada. Prueba otra cuota.")
                    else:
                        sel, prob_acum, cuota = mejor
                        prob_pct = round(prob_acum * 100, 2)
                        edge_final = round((cuota / (1/prob_acum) - 1) * 100, 1) if prob_acum > 0 else 0

                        st.divider()
                        st.success(f"✅ Combinada construida ({len(sel)} patas)")

                        c1, c2, c3 = st.columns(3)
                        c1.metric("Cuota real", f"{cuota:.2f}",
                                  delta=f"obj: {cuota_obj}")
                        c2.metric("Probabilidad", f"{prob_pct}%")
                        c3.metric("Edge estimado", f"{edge_final}%")

                        with st.expander("📋 Ver patas", expanded=True):
                            for i, p in enumerate(sel, 1):
                                st.write(f"**{i}. {p['selection']}**")
                                st.caption(f"{p['partido']} · {p['fecha']}")
                                st.caption(f"Prob: {p['prob']}% · "
                                           f"@{p.get('cuota_real') or p['fair_odd']:.2f}")

                        stake = st.number_input("Stake ($)", min_value=1.0,
                                                value=10.0, step=1.0, key="auto_stake")
                        ganancia = stake * cuota
                        st.info(f"💰 Ganancia potencial: **${ganancia:.2f}**")

                        col_a, col_b = st.columns(2)
                        with col_a:
                            if st.button("📲 Telegram", use_container_width=True,
                                         key="tg_auto"):
                                msg = "🔗 *COMBINADA AUTO*\n\n"
                                msg += f"Cuota objetivo: {cuota_obj} · Real: {cuota:.2f}\n"
                                msg += f"Prob: {prob_pct}% | Patas: {len(sel)}\n\n"
                                for i, p in enumerate(sel, 1):
                                    msg += (f"{i}. *{p['selection']}*\n"
                                            f"   _{p['partido']}_\n"
                                            f"   {p['prob']}% @"
                                            f"{p.get('cuota_real') or p['fair_odd']:.2f}\n")
                                msg += f"\n💰 ${stake:.2f} → ${ganancia:.2f}"
                                st.success("✅ Enviado") if tg_send(msg) else st.warning("⚠️ Falló")
                        with col_b:
                            if st.button("➕ Boleto", use_container_width=True,
                                         key="bol_auto"):
                                for p in sel:
                                    st.session_state.boleto.append({
                                        "partido": p["partido"],
                                        "seleccion": f"[COMBO] {p['selection']}",
                                        "cuota": p.get("cuota_real") or p["fair_odd"],
                                    })
                                st.success(f"✅ {len(sel)} patas añadidas")
                                st.rerun()


# ============================================================
#  TAB 4: BOLETO
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
            st.success("✅ Enviado") if tg_send(msg) else st.warning("⚠️ Falló")


# ============================================================
#  TAB 5: HISTORIAL
# ============================================================
with t5:
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
                    c3.metric("C. real",
                              f"{p['cuota_real']:.2f}" if p.get("cuota_real") else "—")

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
