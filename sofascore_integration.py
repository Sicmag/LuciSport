"""SofaScore con caché en memoria (compatible con Streamlit Cloud)."""
import json, time, requests
from datetime import datetime, timedelta

SOFA_BASE = "https://api.sofascore.com/api/v1"
SOFA_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Mobile Safari/537.36"),
    "Accept": "application/json",
    "Referer": "https://www.sofascore.com/",
}

_CACHE = {}
TTL = timedelta(hours=6)


def _cache_get(key):
    if key in _CACHE:
        ts, data = _CACHE[key]
        if datetime.utcnow() - ts < TTL:
            return data
    return None


def _cache_set(key, data):
    _CACHE[key] = (datetime.utcnow(), data)


def _normalize(name):
    n = (name or "").lower().strip()
    for suf in [' cf',' fc',' sc',' ac',' club',' ud',' cd',' deportivo',
                ' real',' atletico',' atlético',' balompié',' balompie',
                ' srl',' 1909',' 1913',' calcio',' de futbol',' de fútbol']:
        n = n.replace(suf, '')
    return n.strip()


def _sofa_get(path, timeout=15):
    try:
        r = requests.get(f"{SOFA_BASE}{path}", headers=SOFA_HEADERS, timeout=timeout)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[SOFA] {e}")
    return None


def buscar_equipo(nombre):
    key = f"team_search:{nombre}"
    c = _cache_get(key)
    if c is not None:
        return c
    data = _sofa_get(f"/search/all?q={requests.utils.quote(nombre)}")
    if not data:
        _cache_set(key, None); return None
    n_norm = _normalize(nombre)
    mejor = None
    for item in data.get("results", []):
        if item.get("type") != "team":
            continue
        entity = item.get("entity", {})
        if _normalize(entity.get("name", "")) == n_norm:
            mejor = {"id": entity.get("id"), "name": entity.get("name")}
            break
    if not mejor:
        for item in data.get("results", []):
            if item.get("type") == "team":
                e = item.get("entity", {})
                mejor = {"id": e.get("id"), "name": e.get("name")}
                break
    _cache_set(key, mejor)
    return mejor


def get_stats_equipo(team_id, team_name=""):
    key = f"team_stats:{team_id}"
    c = _cache_get(key)
    if c is not None:
        return c
    data = _sofa_get(f"/team/{team_id}/events/last/0")
    if not data:
        _cache_set(key, None); return None
    eventos = data.get("events", [])[-10:]
    if not eventos:
        _cache_set(key, None); return None

    gf = gc = 0
    forma = []
    for ev in eventos:
        es_local = ev.get("homeTeam", {}).get("id") == team_id
        hs = ev.get("homeScore", {}).get("current", 0) or 0
        as_ = ev.get("awayScore", {}).get("current", 0) or 0
        if es_local:
            gf += hs; gc += as_
            forma.append("W" if hs > as_ else "L" if hs < as_ else "D")
        else:
            gf += as_; gc += hs
            forma.append("W" if as_ > hs else "L" if as_ < hs else "D")

    n = len(eventos)
    resultado = {
        "team_id": team_id,
        "team_name": team_name,
        "partidos": n,
        "goles_favor_prom": round(gf / n, 2),
        "goles_contra_prom": round(gc / n, 2),
        "forma": forma,
        "victorias_ult5": forma[-5:].count("W"),
    }
    _cache_set(key, resultado)
    return resultado


def enriquecer_partido(match):
    home = match['homeTeam']['name']
    away = match['awayTeam']['name']
    home_team = buscar_equipo(home)
    away_team = buscar_equipo(away)

    res = {"home_stats": None, "away_stats": None, "ok": False}
    if home_team:
        res["home_stats"] = get_stats_equipo(home_team["id"], home_team["name"])
    if away_team:
        res["away_stats"] = get_stats_equipo(away_team["id"], away_team["name"])

    if res["home_stats"] and res["away_stats"]:
        hs = res["home_stats"]; as_ = res["away_stats"]
        res["lambda_home_sofascore"] = round(
            (hs["goles_favor_prom"] + as_["goles_contra_prom"]) / 2, 3)
        res["lambda_away_sofascore"] = round(
            (as_["goles_favor_prom"] + hs["goles_contra_prom"]) / 2, 3)
        res["ok"] = True
    return res