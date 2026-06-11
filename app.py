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


@app.route("/partidos")
def partidos():
    if request.headers.get("X-Api-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401

    fecha = request.args.get("fecha")  # YYYYMMDD, ej: 20260611
    url = "https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
    if fecha:
        url += f"?dates={fecha}"

    try:
        data = espn_get(url)
    except Exception as e:
        return jsonify({"error": str(e)}), 502

    partidos = []
    for event in data.get("events", []):
        comp = event.get("competitions", [{}])[0]
        teams = comp.get("competitors", [])
        home = next((t for t in teams if t.get("homeAway") == "home"), teams[0] if teams else {})
        away = next((t for t in teams if t.get("homeAway") == "away"), teams[1] if len(teams) > 1 else {})
        status = comp.get("status", {})

        partidos.append({
            "id":        event.get("id"),
            "fecha":     event.get("date"),
            "local":     home.get("team", {}).get("displayName", "?"),
            "visitante": away.get("team", {}).get("displayName", "?"),
            "fase":      event.get("season", {}).get("slug", "Fase de grupos"),
            "estado":    status.get("type", {}).get("description", "Scheduled"),
            "score_local":    home.get("score", "-"),
            "score_visitante":away.get("score", "-"),
        })

    return jsonify({"partidos": partidos, "total": len(partidos)})


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
