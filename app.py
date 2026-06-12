"""
espn_proxy/app.py
Microservicio Flask que hace de proxy entre Wispbyte y la API de ESPN.
Deploy gratuito en Render.com — sin restricciones de red.
"""
from flask import Flask, jsonify, request
import urllib.request
import json
import os

app = Flask(__name__)

ESPN_HEADERS = {
    "User-Agent": "ESPN-Service/1.0",
    "Accept": "application/json",
}

# Clave simple para que solo tu bot pueda usarlo
API_KEY = os.getenv("PROXY_API_KEY", "cambiame")


def espn_get(url: str) -> dict:
    req = urllib.request.Request(url, headers=ESPN_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _auth():
    if request.headers.get("X-Api-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    return None


@app.route("/partidos")
def partidos():
    err = _auth()
    if err: return err

    fecha = request.args.get("fecha")  # YYYYMMDD, ej: 20260611
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    if fecha:
        url += f"?dates={fecha}"

    try:
        data = espn_get(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    partidos_list = []
    for event in data.get("events", []):
        comp   = event.get("competitions", [{}])[0]
        teams  = comp.get("competitors", [])
        home   = next((t for t in teams if t.get("homeAway") == "home"), teams[0] if teams else {})
        away   = next((t for t in teams if t.get("homeAway") == "away"), teams[1] if len(teams) > 1 else {})
        status = comp.get("status", {})

        partidos_list.append({
            "id":             event.get("id"),
            "fecha":          event.get("date"),
            "local":          home.get("team", {}).get("displayName", "?"),
            "visitante":      away.get("team", {}).get("displayName", "?"),
            "fase":           event.get("season", {}).get("slug", "Fase de grupos"),
            "estado":         status.get("type", {}).get("description", "Scheduled"),
            "score_local":    home.get("score", "-"),
            "score_visitante":away.get("score", "-"),
        })

    return jsonify({"partidos": partidos_list, "total": len(partidos_list)})


@app.route("/eventos/<espn_id>")
def eventos(espn_id: str):
    """
    Devuelve los eventos (goles, tarjetas) de un partido en vivo.
    Cada item tiene: tipo, minuto, equipo, jugador, autogol, penalti
    """
    err = _auth()
    if err: return err

    url = (
        "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/summary"
        f"?event={espn_id}"
    )

    try:
        data = espn_get(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    eventos_list = []

    # ESPN devuelve los eventos en data["keyEvents"] o data["commentary"]
    # La fuente más completa para goles y tarjetas es data["rosters"] + data["header"]
    # pero la más directa es el bloque "keyEvents" del summary
    for ev in data.get("keyEvents", []):
        clock  = ev.get("clock", {}).get("displayValue", "")
        text   = ev.get("text", "")
        etype  = ev.get("type", {}).get("id", "")  # "goal", "yellow-card", "red-card"
        team   = ev.get("team", {}).get("displayName", "")
        atletas = ev.get("athletesInvolved", [])
        jugador = atletas[0].get("displayName", "") if atletas else ""
        autogol = ev.get("ownGoal", False) or "own goal" in text.lower() or "autogol" in text.lower()
        penalti = ev.get("penaltyKick", False) or "penalty" in text.lower()

        eventos_list.append({
            "tipo":    etype,       # "goal" | "yellow-card" | "red-card" | "substitution" | ...
            "minuto":  clock,       # "23'" o "45+2'"
            "equipo":  team,
            "jugador": jugador,
            "autogol": autogol,
            "penalti": penalti,
            "texto":   text,        # descripción raw de ESPN por si acaso
        })

    return jsonify({"espn_id": espn_id, "eventos": eventos_list, "total": len(eventos_list)})


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
