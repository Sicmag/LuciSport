"""
LuciSport AI — Streamlit
Compatible con Streamlit Community Cloud.
"""
import streamlit as st
import requests
from datetime import datetime, timedelta, timezone

from config import HEADERS
from sofascore_integration import enriquecer_partido
from ai_analyst import analizar_partido_con_ia

# Silenciar logs de betaspd
import betaspd as L
L.MODO_SILENCIOSO = True

st.set_page_config(
    page_title="LuciSport AI",
    page_icon="⚽",
    layout="wide",
)

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


# ---------- ESTADO ----------
if "analisis" not in st.session_state:
    st.session_state.analisis = []
if "boleto" not in st.session_state:
    st.session_state.boleto = []


# ---------- FUNCIONES ----------
@st.cache_data(ttl=1800, show_spinner=False)
def cargar_partidos(codigo: str, dias: int = 5):
    ahora = datetime.now(timezone.utc)
    url = (f"https://api.football-data.org/v4/competitions/{codigo}/matches"
           f"?dateFrom={ahora.strftime('%Y-%m-%d')}"
           f"&dateTo={(ahora+timedelta(days=dias)).strftime('%Y-%m-%d')}")
    try:
        r = requests.get(url, headers=HEADERS, timeout=15)
        if r.status_code != 200:
            return []
        return r.json().get("matches", [])
    except Exception as e:
        st.error(f"Error API: {e}")
        return []


def analizar_partido(match, usar_ia=True):
    try:
        picks = L.analizar_titan(match, solo_picks=True,
                                  notificar_telegram=False, persistir=False)
        sofa = enriquecer_partido(match)
        ia = None
        if usar_ia and picks:
            ia = analizar_partido_con_ia(match, picks, sofa)
        return {
            "partido": f"{match['homeTeam']['name']} vs {match['awayTeam']['name']}",
            "fecha": (match.get('utcDate') or '')[:16],
            "liga": match.get('league_code', ''),
            "picks": picks,
            "sofascore": sofa,
            "ia": ia,
        }
    except Exception as e:
        st.warning(f"Error analizando {match['homeTeam']['name']}: {e}")
        return None


# ---------- SIDEBAR ----------
with st.sidebar:
    st.title("⚽ LuciSport AI")
    st.caption("Análisis deportivo con IA")

    liga_nombre = st.selectbox("Liga", list(LIGAS.keys()))
    dias = st.slider("Días a futuro", 1, 14, 5)
    usar_ia = st.toggle("Análisis con IA (Groq)", value=True)
    max_analizar = st.slider("Máx. partidos a analizar", 1, 10, 3)

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

    st.divider()
    if st.button("📥 Cargar partidos", use_container_width=True, type="primary"):
        st.session_state.partidos = cargar_partidos(LIGAS[liga_nombre], dias)
        st.session_state.analisis = []


# ---------- CUERPO ----------
st.title("⚽ LuciSport AI")
st.caption("Modelo Dixon-Coles + SofaScore + Groq Llama 3.3")

tab1, tab2 = st.tabs(["📅 Partidos y análisis", "📊 Resultados"])

with tab1:
    partidos = st.session_state.get("partidos", [])
    if not partidos:
        st.info("👈 Selecciona una liga y pulsa **Cargar partidos**")
    else:
        st.success(f"{len(partidos)} partidos encontrados en {liga_nombre}")
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
        )

        if st.button("🔍 Analizar seleccionados", type="primary"):
            if not seleccionados:
                st.warning("Selecciona al menos un partido.")
            else:
                st.session_state.analisis = []
                progreso = st.progress(0, text="Analizando...")
                for i, sel in enumerate(seleccionados):
                    idx = opciones[sel]
                    match = partidos[idx]
                    progreso.progress(
                        (i + 1) / len(seleccionados),
                        text=f"Analizando: {match['homeTeam']['name']} vs {match['awayTeam']['name']}"
                    )
                    res = analizar_partido(match, usar_ia=usar_ia)
                    if res:
                        st.session_state.analisis.append(res)
                progreso.empty()
                st.success(f"✅ {len(st.session_state.analisis)} partidos analizados")

        # Mostrar análisis
        for analisis in st.session_state.analisis:
            with st.container(border=True):
                col_t, col_f = st.columns([3, 1])
                with col_t:
                    st.subheader(analisis["partido"])
                with col_f:
                    st.caption(f"🕐 {analisis['fecha']}")

                c1, c2 = st.columns(2)

                with c1:
                    st.markdown("#### 📈 Picks del modelo")
                    for p in analisis["picks"]:
                        with st.container(border=True):
                            st.write(f"**{p['market']}**")
                            st.write(p["selection"])
                            cc1, cc2 = st.columns(2)
                            cc1.metric("Prob", f"{p['prob']}%")
                            cc2.metric("Cuota justa", f"{p['fair_odd']:.2f}")

                            if st.button("➕ Añadir al boleto",
                                         key=f"add_{analisis['partido']}_{p['selection']}"):
                                st.session_state.boleto.append({
                                    "partido": analisis["partido"],
                                    "seleccion": p["selection"],
                                    "cuota": p["fair_odd"],
                                })
                                st.toast(f"Añadido: {p['selection']}")

                with c2:
                    st.markdown("#### 🧠 Análisis IA")
                    ia = analisis.get("ia")
                    if not ia:
                        st.caption("Sin análisis IA")
                    else:
                        st.info(ia.get("analisis", ""))
                        st.write(f"**Pick IA:** {ia.get('pick_recomendado', '-')}")
                        conf = ia.get("confianza", 0)
                        st.progress(min(conf, 10) / 10, text=f"Confianza: {conf}/10")
                        if ia.get("riesgos"):
                            st.warning("⚠️ " + " · ".join(ia["riesgos"]))

                # SofaScore
                sofa = analisis.get("sofascore") or {}
                if sofa.get("home_stats") or sofa.get("away_stats"):
                    st.markdown("#### 📊 SofaScore (últimos 10 partidos)")
                    sc1, sc2 = st.columns(2)
                    for col, key, label in [(sc1, "home_stats", "Local"),
                                             (sc2, "away_stats", "Visita")]:
                        s = sofa.get(key)
                        if s:
                            with col:
                                st.markdown(f"**{s['team_name']}** ({label})")
                                m1, m2 = st.columns(2)
                                m1.metric("Goles a favor", s["goles_favor_prom"])
                                m2.metric("Goles contra", s["goles_contra_prom"])
                                forma_str = " ".join(
                                    f"🟢{c}" if c == "W" else f"🔴{c}" if c == "L" else f"⚪{c}"
                                    for c in s["forma"][-5:]
                                )
                                st.write(f"Forma: {forma_str}")

with tab2:
    st.subheader("Historial de apuestas")
    if not st.session_state.boleto:
        st.info("Boleto vacío.")
    else:
        cuota_total = 1.0
        for b in st.session_state.boleto:
            cuota_total *= b["cuota"]
        st.write(f"**{len(st.session_state.boleto)} selecciones** · "
                 f"Cuota combinada: **{cuota_total:.2f}**")
        for b in st.session_state.boleto:
            st.write(f"• {b['seleccion']} — {b['partido']} @{b['cuota']:.2f}")