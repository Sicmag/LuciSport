import json, requests
from config import GROQ_API_KEY, AI_PROVIDER

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "llama-3.3-70b-versatile"

SYSTEM_PROMPT = """Eres un analista profesional de apuestas deportivas.
Recibes: picks del modelo estadístico + forma reciente de ambos equipos.
Responde SIEMPRE en JSON válido con este formato:
{
  "analisis": "análisis de 3-4 frases",
  "pick_recomendado": "selección concreta",
  "confianza": 7,
  "riesgos": ["riesgo1", "riesgo2"],
  "ajuste_prob": -3
}
confianza: 1-10. ajuste_prob: -10 a +10."""


def analizar_partido_con_ia(match, modelo_picks, sofascore_data=None):
    if not GROQ_API_KEY:
        return None

    home = match['homeTeam']['name']
    away = match['awayTeam']['name']

    prompt = f"PARTIDO: {home} vs {away}\n\nPICKS DEL MODELO:\n"
    for p in modelo_picks[:5]:
        prompt += f"- [{p['market']}] {p['selection']}: {p['prob']}% (cuota justa {p['fair_odd']})\n"

    if sofascore_data:
        hs = sofascore_data.get("home_stats")
        as_ = sofascore_data.get("away_stats")
        if hs:
            prompt += f"\n{home} (últimos {hs['partidos']}): "
            prompt += f"GF {hs['goles_favor_prom']}, GC {hs['goles_contra_prom']}, "
            prompt += f"forma {''.join(hs['forma'][-5:])}\n"
        if as_:
            prompt += f"{away} (últimos {as_['partidos']}): "
            prompt += f"GF {as_['goles_favor_prom']}, GC {as_['goles_contra_prom']}, "
            prompt += f"forma {''.join(as_['forma'][-5:])}\n"

    prompt += "\nGenera el JSON con tu análisis."

    try:
        r = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}",
                     "Content-Type": "application/json"},
            json={
                "model": GROQ_MODEL,
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
        print(f"[IA] Error {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[IA] {e}")
    return None