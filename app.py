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

    # ESPN tiene el Mundial fragmentado: el endpoint principal de fifa.world
    # solo devuelve algunos partidos. Para obtener todos usamos el endpoint
    # de calendar/ondays que devuelve los event IDs completos del dia,
    # y como fallback combinamos multiples slugs conocidos del Mundial 2026.
    all_events = []
    seen_ids = set()

    def _fetch_scoreboard(slug, fecha_str):
        url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/{slug}/scoreboard"
        if fecha_str:
            url += f"?dates={fecha_str}"
        try:
            data = espn_get(url)
            evs = data.get("events", [])
            app.logger.info(f"[partidos] slug={slug} fecha={fecha_str} -> {len(evs)} eventos")
            return evs
        except Exception as e:
            app.logger.warning(f"[partidos] slug={slug} error: {e}")
            return []

    # Primero: fetch nocturno del dia siguiente para pre-cargar seen_ids
    # con partidos que pertenecen a HOY en UTC-6 pero tienen fecha UTC del dia siguiente.
    # Esto evita que el scoreboard principal los duplique.
    if fecha:
        from datetime import datetime, timedelta
        try:
            dt_base = datetime.strptime(fecha, "%Y%m%d")
            fecha_next = (dt_base + timedelta(days=1)).strftime("%Y%m%d")
            next_url = (
                f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard"
                f"?dates={fecha_next}"
            )
            next_data = espn_get(next_url)
            for ev in next_data.get("events", []):
                eid = ev.get("id")
                if eid and eid not in seen_ids:
                    # Solo incluir si la hora UTC cae dentro del dia local buscado
                    # (es decir, antes de las 06:00 UTC del dia siguiente)
                    ev_date = ev.get("date", "")
                    try:
                        ev_dt = datetime.fromisoformat(ev_date.replace("Z", "+00:00"))
                        # Solo incluir si la fecha UTC del partido es exactamente
                        # fecha_next (dia+1) y la hora es entre 00:00-05:59 UTC,
                        # lo que corresponde a 18:00-23:59h en UTC-6 del dia buscado.
                        ev_date_only = ev_dt.strftime("%Y%m%d")
                        if ev_date_only == fecha_next and 0 <= ev_dt.hour <= 5:
                            seen_ids.add(eid)
                            all_events.append(ev)
                            app.logger.error(
                                f"[partidos] nocturno agregado: id={eid} "
                                f"fecha_utc={ev_date} nombre={ev.get('name','?')}"
                            )
                        else:
                            app.logger.error(
                                f"[partidos] nocturno DESCARTADO (hora={ev_dt.hour} fecha={ev_date_only}): "
                                f"id={eid} nombre={ev.get('name','?')}"
                            )
                    except Exception as ep:
                        app.logger.debug(f"[partidos] parse fecha nocturno error: {ep} fecha={ev_date}")
            app.logger.debug(f"[partidos] nocturno pre-cargado, seen_ids={len(seen_ids)}")
        except Exception as en:
            app.logger.debug(f"[partidos] fecha_next error: {en}")

    # Segundo: scoreboard del dia actual — los nocturnos ya estan en seen_ids y se saltan
    for ev in _fetch_scoreboard("fifa.world", fecha):
        eid = ev.get("id")
        if eid and eid not in seen_ids:
            seen_ids.add(eid)
            all_events.append(ev)

    app.logger.debug(f"[partidos] scoreboard -> {len(all_events)} eventos para fecha={fecha}")

    # Complementar con la API core v2 que lista eventos por fecha con paginacion
    if fecha:
        try:
            # Este endpoint devuelve TODOS los eventos del dia, no solo los "activos"
            events_url = (
                f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/fifa.world"
                f"/events?dates={fecha}&limit=100"
            )
            ev_data = espn_get(events_url)
            import re as _re
            new_ids = []
            # Puede venir como lista directa o como {items: [...]}
            items = ev_data.get("items", ev_data.get("events", []))
            for item in items:
                # Cada item puede ser un objeto completo o un $ref
                href = item.get("$ref", "")
                if href:
                    m = _re.search(r"/events/(\d+)", href)
                    eid = m.group(1) if m else None
                else:
                    eid = str(item.get("id", "")) or None
                if eid and eid not in seen_ids:
                    new_ids.append(eid)
                    seen_ids.add(eid)
            app.logger.debug(f"[partidos] core/events -> {len(items)} items, {len(new_ids)} nuevos IDs")
            # Fetchear cada evento nuevo individualmente
            for eid in new_ids:
                try:
                    # Usamos el mismo endpoint de summary que ya parseamos en /eventos
                    sum_url = (
                        f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world"
                        f"/summary?event={eid}"
                    )
                    sd = espn_get(sum_url)
                    header = sd.get("header", {})
                    comps  = header.get("competitions", [{}])
                    comp   = comps[0] if comps else {}
                    teams  = comp.get("competitors", [])
                    status = comp.get("status", {})

                    # El summary usa homeTeam/order en vez de homeAway — normalizar
                    for t in teams:
                        if "homeAway" not in t:
                            t["homeAway"] = "home" if t.get("homeTeam", False) or t.get("order", 1) == 0 else "away"

                    # El summary puede tener la fecha en distintos campos segun el estado
                    fecha_ev = (
                        comp.get("date") or
                        comp.get("startDate") or
                        header.get("gameDate") or
                        header.get("date") or
                        ""
                    )
                    # Normalizar displayName: el summary a veces lo tiene en team.shortDisplayName
                    for t in teams:
                        tm = t.get("team", {})
                        if not tm.get("displayName"):
                            tm["displayName"] = tm.get("shortDisplayName") or tm.get("name") or "?"
                            t["team"] = tm

                    all_events.append({
                        "id":          eid,
                        "date":        fecha_ev,
                        "season":      header.get("season", {}),
                        "competitions": [{
                            "competitors": teams,
                            "status":      status,
                            "date":        fecha_ev,
                        }],
                    })
                    app.logger.info(
                        f"[partidos] evento {eid} agregado: fecha={fecha_ev} "
                        f"teams={[t.get('team',{}).get('displayName') for t in teams]}"
                    )
                except Exception as ef:
                    app.logger.debug(f"[partidos] fetch event {eid} FALLO: {ef}")
        except Exception as ec:
            app.logger.debug(f"[partidos] core/events FALLO: {ec}")

    app.logger.info(f"[partidos] Total final: {len(all_events)} partidos unicos para fecha={fecha}")

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



@app.route("/debug/partidos")
def debug_partidos():
    """Endpoint de diagnostico — ver que devuelve ESPN crudo. SIN AUTH temporal."""
    err = _auth()
    if err: return err

    from datetime import datetime as _dt
    fecha = request.args.get("fecha", _dt.utcnow().strftime("%Y%m%d"))

    result = {"fecha": fecha, "scoreboard": [], "event_refs": [], "calendar_error": None}

    # Scoreboard
    try:
        sb_url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates={fecha}"
        sb_data = espn_get(sb_url)
        for ev in sb_data.get("events", []):
            result["scoreboard"].append({"id": ev.get("id"), "name": ev.get("name"), "date": ev.get("date")})
    except Exception as e:
        result["scoreboard_error"] = str(e)

    # Calendar ondays
    try:
        cal_url = (
            f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/fifa.world"
            f"/events?dates={fecha}&limit=100"
        )
        cal_data = espn_get(cal_url)
        result["events_raw_keys"] = list(cal_data.keys())
        import re as _re
        for ref in cal_data.get("items", cal_data.get("events", [])):
            href = ref.get("$ref", "")
            m = _re.search(r"/events/(\d+)", href)
            result["event_refs"].append({"ref": href, "id": m.group(1) if m else None})
    except Exception as e:
        result["events_error"] = str(e)

    return jsonify(result)

if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
