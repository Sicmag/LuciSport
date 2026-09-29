"""LuciSport AI — Streamlit unificado."""
import streamlit as st
import requests
from datetime import datetime, timedelta, timezone

from config import HEADERS
from sofascore_integration import enriquecer_partido
from ai_analyst import analizar_partido_con_ia

import betaspd as L
L.MODO_SILENCIOSO = True


st.set_page_config(
    page_title="LuciSport AI",
    page_icon="⚽",
    layout="wide",
    initial_sidebar_state="collapsed",
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

if "partidos" not in st.session_state:
    st.session_state.partidos = []
if "analisis" not in st.session_state:
    st.session_state.analisis = []
if "boleto" not in st.session_state:
    st.session_state.boleto = []
if "pendientes" not in st.session_state:
    st.session_state.pendientes = []


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


def analizar_partido(match, usar_ia=True):
    try:
        home = match['homeTeam']['name']
        away = match['awayTeam']['name']

        st.write(f"   📊 Cargando datos de **{home}** y **{away}**...")
        picks = L.analizar_titan(
            match, solo_picks=True,
            notificar_telegram=False, persistir=False,
        )

        sofa = None
        st.write(f"   🔍 Consultando SofaScore...")
        try:
            sofa = enriquecer_partido(match)
            if sofa and sofa.get("ok"):
                st.write(f"   ✅ SofaScore OK")
            else:
                st.write(f"   ⚠️ SofaScore sin datos")
        except Exception as e:
            st.write(f"   ⚠️ SofaScore falló: {e}")

        ia = None
        if usar_ia and picks:
            st.write(f"   🧠 Consultando IA...")
            try:
                ia = analizar_partido_con_ia(match, picks, sofa)
                if ia:
                    st.write(f"   ✅ IA respondió")
            except Exception as e:
                st.write(f"   ⚠️ IA falló: {e}")

        return {
            "partido": f"{home} vs {away}",
            "fecha": (match.get('utcDate') or '')[:16],
            "liga": match.get('league_code', ''),
            "picks": picks,
            "sofascore": sofa,
            "ia": ia,
        }
    except Exception as e:
        st.warning(f"Error analizando {match['homeTeam']['name']}: {e}")
        return None


with st.sidebar:
    st.title("⚽ LuciSport AI")
    st.caption("Análisis deportivo con IA")

    liga_nombre = st.selectbox("Liga", list(LIGAS.keys()))
    dias = st.slider("Días a futuro", 1, 14, 5)
    usar_ia = st.toggle("Análisis con IA (Groq)", value=False)
    max_analizar = st.slider("Máx. partidos a analizar", 1, 5, 1)

    st.divider()

    if st.button("📥 Cargar partidos", use_container_width=True, type="primary"):
        with st.spinner("Cargando partidos..."):
            st.session_state.partidos = cargar_partidos(LIGAS[liga_nombre], dias)
            st.session_state.analisis = []
            st.session_state.pendientes = []
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


st.title("⚽ LuciSport AI")
st.caption("Modelo Dixon-Coles + SofaScore + Groq Llama 3.3")

tab1, tab2 = st.tabs(["📅 Partidos y análisis", "📊 Resultados"])


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
                st.session_state.pendientes = [opciones[s] for s in seleccionados]

        pendientes = st.session_state.pendientes
        if pendientes:
            with st.status(f"Analizando {len(pendientes)} partido(s)...",
                           expanded=True) as status:
                resultados = []
                for idx in pendientes:
                    if idx >= len(partidos):
                        continue
                    match = partidos[idx]
                    st.write(f"🔍 **{match['homeTeam']['name']} vs "
                             f"{match['awayTeam']['name']}**")
                    res = analizar_partido(match, usar_ia=usar_ia)
                    if res:
                        resultados.append(res)
                        st.write(f"   ✅ {len(res['picks'])} picks generados")

                status.update(
                    label=f"✅ {len(resultados)} partido(s) analizados",
                    state="complete",
                )

            st.session_state.analisis = resultados
            st.session_state.pendientes = []
            st.rerun()

        if not st.session_state.pendientes and st.session_state.analisis:
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
                        if not analisis["picks"]:
                            st.caption("Ningún pick supera los filtros")
                        for j, p in enumerate(analisis["picks"]):
                            with st.container(border=True):
                                st.write(f"**{p['market']}**")
                                st.write(p["selection"])
                                cc1, cc2 = st.columns(2)
                                cc1.metric("Prob", f"{p['prob']}%")
                                cc2.metric("Cuota", f"{p['fair_odd']:.2f}")

                                if st.button(
                                    "➕ Añadir al boleto",
                                    key=f"add_{analisis['partido']}_{j}",
                                ):
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
                            st.caption("Sin análisis IA (configura GROQ_API_KEY)")
                        else:
                            st.info(ia.get("analisis", ""))
                            st.write(f"**Pick IA:** {ia.get('pick_recomendado', '-')}")
                            conf = ia.get("confianza", 0)
                            st.write(f"Confianza: **{conf}/10**")
                            if ia.get("riesgos"):
                                st.warning("⚠️ " + " · ".join(ia["riesgos"]))

                    sofa = analisis.get("sofascore") or {}
                    if sofa.get("home_stats") or sofa.get("away_stats"):
                        st.markdown("#### 📊 SofaScore (últimos 10)")
                        sc1, sc2 = st.columns(2)
                        for col, key, label in [
                            (sc1, "home_stats", "Local"),
                            (sc2, "away_stats", "Visita"),
                        ]:
                            s = sofa.get(key)
                            if s:
                                with col:
                                    st.markdown(f"**{s['team_name']}** ({label})")
                                    m1, m2 = st.columns(2)
                                    m1.metric("GF", s["goles_favor_prom"])
                                    m2.metric("GC", s["goles_contra_prom"])
                                    forma_str = " ".join(
                                        "🟢" if c == "W" else
                                        "🔴" if c == "L" else "⚪"
                                        for c in s["forma"][-5:]
                                    )
                                    st.write(f"Forma: {forma_str}")


with tab2:
    st.subheader("🧾 Boleto actual")

    if not st.session_state.boleto:
        st.info("Tu boleto está vacío. Añade picks desde la pestaña anterior.")
    else:
        cuota_total = 1.0
        for b in st.session_state.boleto:
            cuota_total *= b["cuota"]

        col1, col2 = st.columns(2)
        col1.metric("Selecciones", len(st.session_state.boleto))
        col2.metric("Cuota combinada", f"{cuota_total:.2f}")

        st.divider()

        for i, b in enumerate(st.session_state.boleto, 1):
            with st.container(border=True):
                st.write(f"**{i}. {b['seleccion']}**")
                st.caption(f"{b['partido']} · @{b['cuota']:.2f}")

        st.divider()

        stake = st.number_input("Importe a apostar", min_value=1.0, value=10.0, step=1.0)
        ganancia = stake * cuota_total
        st.success(f"💰 Ganancia potencial: **{ganancia:.2f}**")

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🗑️ Vaciar boleto", use_container_width=True):
                st.session_state.boleto = []
                st.rerun()
        with col_b:
            if st.button("📲 Enviar a Telegram", use_container_width=True):
                msg = "🎯 *LUCI SPORT — BOLETO*\n\n"
                for i, b in enumerate(st.session_state.boleto, 1):
                    msg += f"{i}. {b['seleccion']}\n   _{b['partido']}_ · @{b['cuota']:.2f}\n"
                msg += f"\n📊 Cuota total: *{cuota_total:.2f}*"
                msg += f"\n💰 Stake: {stake:.2f} → Ganancia: {ganancia:.2f}"
                try:
                    ok = L.enviar_a_telegram(msg)
                    if ok:
                        st.success("✅ Enviado a Telegram")
                    else:
                        st.warning("⚠️ No se pudo enviar (revisa secrets)")
                except Exception as e:
                    st.error(f"Error: {e}")
