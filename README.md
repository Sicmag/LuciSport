# ⚽ LuciSport AI

> Análisis de fútbol con modelo estadístico + IA + datos avanzados de SofaScore.
> Picks diarios, value betting y combinadas automáticas.

[![Streamlit App](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://lucisport.streamlit.app)
![Python](https://img.shields.io/badge/Python-3.10%2B-blue)
![License](https://img.shields.io/badge/License-MIT-green)

---

## 🎯 ¿Qué hace?

LuciSport AI combina tres capas de análisis para generar picks con valor esperado positivo:

| Capa | Fuente | Aporta |
|------|--------|--------|
| **Modelo estadístico** | Dixon-Coles + Poisson | Probabilidades base por partido |
| **Datos avanzados** | SofaScore | xG, forma reciente, H2H |
| **Inteligencia artificial** | Groq (Llama 3.3 70B) | Contexto, riesgos, ajuste fino |

Y con esos datos:
- 🎯 Genera **picks filtrados** (prob ≥ 55 %, cuota ≥ 1.50)
- 💰 Calcula **edge** (cuota real vs cuota justa)
- 🧮 Sugiere **stake óptimo** (Kelly fraccionado)
- 🔗 Construye **combinadas automáticas** por cuota objetivo
- 📊 Exporta todo a **Excel** (una hoja por equipo)
- 📲 Envía a **Telegram** automáticamente

---

## 🚀 Demo

👉 **[lucisport.streamlit.app](https://lucisport.streamlit.app)** *(reemplaza con tu URL real después de desplegar)*

---

## 🖼️ Screenshots

| Análisis de partido | Boleto |
|---|---|
| *[añade captura aquí]* | *[añade captura aquí]* |

---

## 🏗️ Arquitectura
