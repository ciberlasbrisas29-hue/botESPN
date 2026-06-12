"""
espn_proxy/app.py
Microservicio Flask que hace de proxy entre Wispbyte y la API de ESPN.
Deploy gratuito en Render.com — sin restricciones de red.
"""
from flask import Flask, jsonify, request
import urllib.request
import json
import os
import re

app = Flask(__name__)

ESPN_HEADERS = {
    "User-Agent": "ESPN-Service/1.0",
    "Accept": "application/json",
}

API_KEY = os.getenv("PROXY_API_KEY", "cambiame")


def espn_get(url: str) -> dict:
    req = urllib.request.Request(url, headers=ESPN_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _auth():
    if request.headers.get("X-Api-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    return None


def _parsear_jugador(texto: str) -> str:
    """Extrae el nombre del jugador del texto de ESPN. Ej: 'Alistair Johnston (Canada) is shown...' → 'Alistair Johnston'"""
    if not texto:
        return ""
    m = re.match(r"^([^(]+?)\s*\(", texto.strip())
    # Para goles: "Goal! Canada 0, X 1. Nombre Apellido (Equipo) header..."
    if texto.startswith("Goal!"):
        m = re.search(r"\.\s+([^(]+?)\s*\(", texto)
    return m.group(1).strip() if m else ""


# IDs de tipo ESPN
_TIPOS_GOL      = {"70", "137"}   # gol normal, gol de cabeza/otro
_TIPOS_AMARILLA = {"94"}
_TIPOS_ROJA     = {"93"}          # red card
_TIPOS_INICIO   = {"80", "82"}    # kick off, second half start
_TIPOS_FIN      = {"81", "83"}    # end of half


@app.route("/partidos")
def partidos():
    err = _auth()
    if err: return err

    fecha = request.args.get("fecha")
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
    """Devuelve goles y tarjetas de un partido con jugador parseado del texto."""
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
    for ev in data.get("keyEvents", []):
        tipo_id = str(ev.get("type", {}).get("id", ""))
        texto   = ev.get("text", "")
        equipo  = ev.get("team", {}).get("displayName", "")
        clock   = ev.get("clock", {}).get("displayValue", "")

        # Parsear jugador solo en eventos relevantes (goles y tarjetas)
        jugador = _parsear_jugador(texto) if tipo_id in (_TIPOS_GOL | _TIPOS_AMARILLA | _TIPOS_ROJA) else ""

        # Detectar autogol por texto
        autogol = "own goal" in texto.lower() or "autogol" in texto.lower()
        penalti = "penalty" in texto.lower()

        # Normalizar tipo a string legible
        if tipo_id in _TIPOS_GOL:
            tipo_norm = "goal"
        elif tipo_id in _TIPOS_AMARILLA:
            tipo_norm = "yellow-card"
        elif tipo_id in _TIPOS_ROJA:
            tipo_norm = "red-card"
        elif tipo_id in _TIPOS_INICIO:
            tipo_norm = "kickoff"
        elif tipo_id in _TIPOS_FIN:
            tipo_norm = "end"
        else:
            tipo_norm = tipo_id  # otros: sustitución, lesión, etc.

        eventos_list.append({
            "tipo":    tipo_norm,
            "minuto":  clock,
            "equipo":  equipo,
            "jugador": jugador,
            "autogol": autogol,
            "penalti": penalti,
            "texto":   texto,
        })

    return jsonify({"espn_id": espn_id, "eventos": eventos_list, "total": len(eventos_list)})


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
