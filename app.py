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

# IDs de tipo ESPN
_TIPOS_GOL      = {"70", "137"}
_TIPOS_AMARILLA = {"94"}
_TIPOS_ROJA     = {"93"}
_TIPOS_INICIO   = {"80", "82"}
_TIPOS_FIN      = {"81", "83"}

# Estadísticas a incluir (label ESPN → label para el bot)
_STATS_LABELS = {
    "Possession":    "Posesión",
    "SHOTS":         "Tiros",
    "ON GOAL":       "Al arco",
    "Corner Kicks":  "Córners",
    "Fouls":         "Faltas",
    "Saves":         "Atajadas",
    "Yellow Cards":  "Amarillas",
    "Red Cards":     "Rojas",
}


def espn_get(url: str) -> dict:
    req = urllib.request.Request(url, headers=ESPN_HEADERS)
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def _auth():
    if request.headers.get("X-Api-Key") != API_KEY:
        return jsonify({"error": "unauthorized"}), 401
    return None


def _parsear_jugador(texto: str) -> str:
    """Extrae nombre del jugador del texto ESPN."""
    if not texto:
        return ""
    if texto.startswith("Goal!"):
        m = re.search(r"\.\s+([^(]+?)\s*\(", texto)
    else:
        m = re.match(r"^([^(]+?)\s*\(", texto.strip())
    return m.group(1).strip() if m else ""


def _parsear_asistencia(texto: str) -> str:
    """Extrae el nombre del asistente del texto ESPN."""
    if not texto:
        return ""
    m = re.search(r"[Aa]ssisted by ([^.]+)\.", texto)
    return m.group(1).strip() if m else ""


def _parsear_estadisticas(data: dict) -> dict:
    """Extrae estadísticas relevantes del boxscore para ambos equipos."""
    resultado = {}
    try:
        teams = data.get("boxscore", {}).get("teams", [])
        for t in teams:
            nombre = t.get("team", {}).get("displayName", "?")
            stats_raw = {s["label"]: s["displayValue"] for s in t.get("statistics", [])}
            stats_filtradas = {}
            for label_espn, label_bot in _STATS_LABELS.items():
                val = stats_raw.get(label_espn, "-")
                if label_espn == "Possession" and val != "-":
                    try:
                        val = f"{float(val):.0f}%"
                    except Exception:
                        val = f"{val}%"
                stats_filtradas[label_bot] = val
            resultado[nombre] = stats_filtradas
    except Exception:
        pass
    return resultado


def _parsear_game_info(data: dict) -> dict:
    """Extrae venue, attendance y árbitro principal del gameInfo de ESPN."""
    resultado = {}
    try:
        gi = data.get("gameInfo", {})

        # Venue
        venue_raw = gi.get("venue", {})
        if venue_raw:
            resultado["venue"] = {
                "id":        venue_raw.get("id", ""),
                "fullName":  venue_raw.get("fullName", ""),
                "shortName": venue_raw.get("shortName", ""),
                "address": {
                    "city":    venue_raw.get("address", {}).get("city", ""),
                    "country": venue_raw.get("address", {}).get("country", ""),
                },
            }

        # Attendance
        attendance = gi.get("attendance")
        if attendance:
            resultado["attendance"] = attendance

        # Árbitro principal (opcional, por si lo querés usar después)
        officials = gi.get("officials", [])
        referee = next(
            (o.get("fullName") for o in officials
             if o.get("position", {}).get("id") == "1"),
            None
        )
        if referee:
            resultado["referee"] = referee

    except Exception:
        pass
    return resultado


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

    # ESPN a veces pagina el scoreboard — recorremos todas las páginas
    all_events = list(data.get("events", []))
    import logging
    logging.info(f"[partidos] ESPN devolvio {len(all_events)} eventos en pagina 1 para fecha={fecha}")
    pageCount = data.get("pageCount", 1)
    pageIndex = data.get("pageIndex", 1)
    while pageIndex < pageCount:
        pageIndex += 1
        paged_url = url + ("&" if "?" in url else "?") + f"page={pageIndex}"
        try:
            paged_data = espn_get(paged_url)
            all_events.extend(paged_data.get("events", []))
            pageCount = paged_data.get("pageCount", pageCount)
        except Exception:
            break

    partidos_list = []
    for event in all_events:
        comp   = event.get("competitions", [{}])[0]
        teams  = comp.get("competitors", [])
        home   = next((t for t in teams if t.get("homeAway") == "home"), teams[0] if teams else {})
        away   = next((t for t in teams if t.get("homeAway") == "away"), teams[1] if len(teams) > 1 else {})
        status = comp.get("status", {})

        partidos_list.append({
            "id":              event.get("id"),
            "fecha":           event.get("date"),
            "local":           home.get("team", {}).get("displayName", "?"),
            "visitante":       away.get("team", {}).get("displayName", "?"),
            "fase":            event.get("season", {}).get("slug", "Fase de grupos"),
            "estado":          status.get("type", {}).get("description", "Scheduled"),
            "score_local":     home.get("score", "-"),
            "score_visitante": away.get("score", "-"),
        })

    return jsonify({"partidos": partidos_list, "total": len(partidos_list)})


@app.route("/eventos/<espn_id>")
def eventos(espn_id: str):
    """Devuelve goles (con asistencia), tarjetas, estadísticas y gameInfo del partido."""
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
    # Deduplicar tarjetas/goles dentro del mismo JSON de ESPN.
    # Usamos minuto_base para tolerar variaciones de "+N'" en vivo.
    _seen_cards: set = set()

    def _minuto_base(clock: str) -> str:
        """Normaliza '45+2\'' -> '45', '10\'' -> '10'."""
        return re.split(r"[+\'\']", clock)[0].strip()

    for ev in data.get("keyEvents", []):
        tipo_id = str(ev.get("type", {}).get("id", ""))
        texto   = ev.get("text", "")
        equipo  = ev.get("team", {}).get("displayName", "")
        clock   = ev.get("clock", {}).get("displayValue", "")

        es_relevante = tipo_id in (_TIPOS_GOL | _TIPOS_AMARILLA | _TIPOS_ROJA)
        jugador      = _parsear_jugador(texto)    if es_relevante else ""
        asistencia   = _parsear_asistencia(texto) if tipo_id in _TIPOS_GOL else ""
        autogol      = "own goal" in texto.lower() or "autogol" in texto.lower()
        penalti      = "penalty" in texto.lower()

        if tipo_id in _TIPOS_GOL:        tipo_norm = "goal"
        elif tipo_id in _TIPOS_AMARILLA: tipo_norm = "yellow-card"
        elif tipo_id in _TIPOS_ROJA:     tipo_norm = "red-card"
        elif tipo_id in _TIPOS_INICIO:   tipo_norm = "kickoff"
        elif tipo_id in _TIPOS_FIN:      tipo_norm = "end"
        else:                            tipo_norm = tipo_id

        # Deduplicar dentro del mismo response (ESPN a veces duplica keyEvents)
        if tipo_norm in ("yellow-card", "red-card", "goal") and jugador:
            dedup_key = (jugador.lower(), tipo_norm, equipo.lower(), _minuto_base(clock))
            if dedup_key in _seen_cards:
                continue
            _seen_cards.add(dedup_key)

        eventos_list.append({
            "tipo":        tipo_norm,
            "minuto":      clock,
            "minuto_base": _minuto_base(clock),  # expuesto para que el bot lo use en su hash
            "equipo":      equipo,
            "jugador":     jugador,
            "asistencia":  asistencia,
            "autogol":     autogol,
            "penalti":     penalti,
            "texto":       texto,
        })

    estadisticas = _parsear_estadisticas(data)
    game_info    = _parsear_game_info(data)

    return jsonify({
        "espn_id":      espn_id,
        "eventos":      eventos_list,
        "estadisticas": estadisticas,
        "gameInfo":     game_info,
        "total":        len(eventos_list),
    })



@app.route("/debug/<espn_id>")
def debug_stats(espn_id: str):
    """Devuelve los labels RAW del boxscore para diagnosticar estadísticas faltantes."""
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

    resultado = {}
    for t in data.get("boxscore", {}).get("teams", []):
        nombre = t.get("team", {}).get("displayName", "?")
        resultado[nombre] = {s["label"]: s["displayValue"] for s in t.get("statistics", [])}

    return jsonify(resultado)


@app.route("/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
