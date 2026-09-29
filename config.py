"""
Configuración unificada.
- En Streamlit Cloud: lee de st.secrets (configurado en la web)
- En local: lee de .env
"""
import os

def _get(key, default=""):
    # 1. st.secrets (Streamlit Cloud)
    try:
        import streamlit as st
        if key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    # 2. .env / os.environ (local)
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
AI_PROVIDER      = _get("AI_PROVIDER", "groq")

HEADERS = {'X-Auth-Token': FOOTBALL_API_KEY}

# Parámetros del modelo
PROB_MINIMA       = 55.0
CUOTA_MINIMA      = 1.50
MAX_PICKS_PARTIDO = 3
KELLY_FRACTION    = 0.25
KELLY_CAP         = 0.05
BANKROLL_INICIAL  = 100000