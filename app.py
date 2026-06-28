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

        # Árbitro principal
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


def _detectar_definicion(comp: dict, eventos: list | None = None) -> str:
    try:
        teams = comp.get("competitors", [])
        for t in teams:
            ss = t.get("shootoutScore")
            if ss is not None and str(ss) not in ("", "0", "null"):
                return "PEN"

        for t in teams:
            score = t.get("score", {})
            if isinstance(score, dict) and score.get("shootout"):
                return "PEN"

        desc = (
            comp.get("status", {}).get("type", {}).get("description", "") or ""
        ).lower()
        if any(k in desc for k in ("shootout", "penalty", "penalties")):
            return "PEN"
        if any(k in desc for k in ("overtime", "extra time", "aet")):
            return "ET"

        if eventos:
            for ev in eventos:
                if ev.get("tipo") == "goal":
                    try:
                        minuto = int(str(ev.get("minuto_base", ev.get("minuto", "0"))).replace("'", "").strip())
                        if minuto > 90:
                            return "ET"
                    except (ValueError, TypeError):
                        pass

    except Exception:
        pass

    return "90"

@app.route("/partidos")
def partidos():
    err = _auth()
    if err: return err

    fecha = request.args.get("fecha")

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
                    ev_date = ev.get("date", "")
                    try:
                        ev_dt = datetime.fromisoformat(ev_date.replace("Z", "+00:00"))
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

    for ev in _fetch_scoreboard("fifa.world", fecha):
        eid = ev.get("id")
        if eid and eid not in seen_ids:
            if fecha:
                try:
                    from datetime import datetime as _dt2
                    ev_date_str = ev.get("date", "")
                    if ev_date_str:
                        ev_dt = _dt2.fromisoformat(ev_date_str.replace("Z", "+00:00"))
                        ev_date_only = ev_dt.strftime("%Y%m%d")
                        is_early_utc = (ev_date_only == fecha and 0 <= ev_dt.hour <= 5)
                        is_prev_day  = (ev_date_only < fecha)
                        if is_early_utc or is_prev_day:
                            comp = (ev.get("competitions") or [{}])[0]
                            status_raw  = comp.get("status", {})
                            status_type = status_raw.get("type", {})
                            estado = (
                                status_type.get("name", "") or
                                status_type.get("description", "") or
                                status_type.get("state", "") or
                                str(status_type.get("id", ""))
                            ).lower()
                            app.logger.error(
                                f"[partidos] remanente detectado: id={eid} "
                                f"nombre={ev.get('name','?')} hora_utc={ev_dt.hour}h "
                                f"fecha_utc={ev_date_only} estado='{estado}' "
                                f"status_type={status_type}"
                            )
                            if estado in ("post", "final", "full time", "status_full_time", "ft", "fulltime", "3"):
                                app.logger.error(
                                    f"[partidos] scoreboard DESCARTADO (remanente): "
                                    f"id={eid} nombre={ev.get('name', '?')}"
                                )
                                seen_ids.add(eid)
                                continue
                except Exception as _ef:
                    app.logger.debug(f"[partidos] filtro remanente error: {_ef}")
            seen_ids.add(eid)
            all_events.append(ev)

    app.logger.debug(f"[partidos] scoreboard -> {len(all_events)} eventos para fecha={fecha}")

    if fecha:
        try:
            events_url = (
                f"https://sports.core.api.espn.com/v2/sports/soccer/leagues/fifa.world"
                f"/events?dates={fecha}&limit=100"
            )
            ev_data = espn_get(events_url)
            import re as _re
            new_ids = []
            items = ev_data.get("items", ev_data.get("events", []))
            for item in items:
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
            for eid in new_ids:
                try:
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

                    for t in teams:
                        if "homeAway" not in t:
                            t["homeAway"] = "home" if t.get("homeTeam", False) or t.get("order", 1) == 0 else "away"

                    fecha_ev = (
                        comp.get("date") or
                        comp.get("startDate") or
                        header.get("gameDate") or
                        header.get("date") or
                        ""
                    )
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

        shoot_home = home.get("shootoutScore")
        shoot_away = away.get("shootoutScore")
        def _norm_shoot(v):
            if v is None: return None
            try: return int(v)
            except (ValueError, TypeError): return None
        shoot_home = _norm_shoot(shoot_home)
        shoot_away = _norm_shoot(shoot_away)

        estado_raw = status.get("type", {}).get("name", "").lower()
        es_finalizado = estado_raw in ("post", "final", "status_final", "3") or \
                        status.get("type", {}).get("state", "") == "post"
        definicion = _detectar_definicion(comp) if es_finalizado else None

        partidos_list.append({
            "id":               event.get("id"),
            "fecha":            event.get("date"),
            "local":            home.get("team", {}).get("displayName", "?"),
            "visitante":        away.get("team", {}).get("displayName", "?"),
            "fase":             event.get("season", {}).get("slug", "Fase de grupos"),
            "estado":           status.get("type", {}).get("description", "Scheduled"),
            "score_local":      home.get("score", "-"),
            "score_visitante":  away.get("score", "-"),
            "shootout_local":   shoot_home,
            "shootout_visitante": shoot_away,
            "definicion":       definicion,
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
    _seen_cards: set = set()

    def _minuto_base(clock: str) -> str:
        return re.split(r"[+\'\'']", clock)[0].strip()

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

        if tipo_norm in ("yellow-card", "red-card", "goal") and jugador:
            dedup_key = (jugador.lower(), tipo_norm, equipo.lower(), _minuto_base(clock))
            if dedup_key in _seen_cards:
                continue
            _seen_cards.add(dedup_key)

        eventos_list.append({
            "tipo":        tipo_norm,
            "minuto":      clock,
            "minuto_base": _minuto_base(clock),
            "equipo":      equipo,
            "jugador":     jugador,
            "asistencia":  asistencia,
            "autogol":     autogol,
            "penalti":     penalti,
            "texto":       texto,
        })

    estadisticas = _parsear_estadisticas(data)
    game_info    = _parsear_game_info(data)

    try:
        header_comp = data.get("header", {}).get("competitions", [{}])[0]
        definicion = _detectar_definicion(header_comp, eventos_list)
    except Exception:
        definicion = "90"

    shoot = {}
    try:
        for t in data.get("header", {}).get("competitions", [{}])[0].get("competitors", []):
            nombre = t.get("team", {}).get("displayName", "")
            ss = t.get("shootoutScore")
            if ss is not None:
                try: shoot[nombre] = int(ss)
                except (ValueError, TypeError): pass
    except Exception:
        pass

    return jsonify({
        "espn_id":        espn_id,
        "eventos":        eventos_list,
        "estadisticas":   estadisticas,
        "gameInfo":       game_info,
        "definicion":     definicion,
        "shootoutScores": shoot,
        "total":          len(eventos_list),
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
    """Endpoint de diagnostico — ver que devuelve ESPN crudo."""
    err = _auth()
    if err: return err

    from datetime import datetime as _dt
    fecha = request.args.get("fecha", _dt.utcnow().strftime("%Y%m%d"))

    result = {"fecha": fecha, "scoreboard": [], "event_refs": [], "calendar_error": None}

    try:
        sb_url = f"https://site.api.espn.com/apis/site/v2/sports/soccer/fifa.world/scoreboard?dates={fecha}"
        sb_data = espn_get(sb_url)
        for ev in sb_data.get("events", []):
            result["scoreboard"].append({"id": ev.get("id"), "name": ev.get("name"), "date": ev.get("date")})
    except Exception as e:
        result["scoreboard_error"] = str(e)

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


# ── Grupos / Standings ──────────────────────────────────────────────────────

_NOMBRE_MAP = {
    "Bosnia & Herzegovina": "Bosnia-Herzegovina",
    "Czech Republic":       "Czech Republic",
    "DR Congo":             "DR Congo",
    "Ivory Coast":          "Ivory Coast",
    "USA":                  "United States",
    "Turkey":               "Türkiye",
    "Curaçao":              "Curaçao",
}

def _normalizar_nombre(nombre: str) -> str:
    return _NOMBRE_MAP.get(nombre, nombre)

def _calcular_standings_desde_partidos(matches: list) -> dict:
    from collections import defaultdict
    standings = defaultdict(dict)

    for m in matches:
        score = m.get("score", {}).get("ft")
        if not score:
            continue
        grupo = m.get("group", "?")
        if grupo == "?":
            continue
        t1 = _normalizar_nombre(m["team1"])
        t2 = _normalizar_nombre(m["team2"])
        g1, g2 = int(score[0]), int(score[1])

        for team in [t1, t2]:
            if team not in standings[grupo]:
                standings[grupo][team] = {"pj":0,"pg":0,"pe":0,"pp":0,"gf":0,"gc":0,"pts":0}

        standings[grupo][t1]["pj"] += 1
        standings[grupo][t1]["gf"] += g1
        standings[grupo][t1]["gc"] += g2
        standings[grupo][t2]["pj"] += 1
        standings[grupo][t2]["gf"] += g2
        standings[grupo][t2]["gc"] += g1

        if g1 > g2:
            standings[grupo][t1]["pg"] += 1; standings[grupo][t1]["pts"] += 3
            standings[grupo][t2]["pp"] += 1
        elif g1 == g2:
            standings[grupo][t1]["pe"] += 1; standings[grupo][t1]["pts"] += 1
            standings[grupo][t2]["pe"] += 1; standings[grupo][t2]["pts"] += 1
        else:
            standings[grupo][t2]["pg"] += 1; standings[grupo][t2]["pts"] += 3
            standings[grupo][t1]["pp"] += 1

    return standings

_GRUPOS_EQUIPOS = {
    "Group A": ["Mexico","South Korea","Czech Republic","South Africa"],
    "Group B": ["Canada","Bosnia & Herzegovina","Qatar","Switzerland"],
    "Group C": ["Scotland","Brazil","Morocco","Haiti"],
    "Group D": ["USA","Australia","Turkey","Paraguay"],
    "Group E": ["Germany","Curaçao","Ecuador","Ivory Coast"],
    "Group F": ["Japan","Netherlands","Sweden","Tunisia"],
    "Group G": ["Belgium","Egypt","Iran","New Zealand"],
    "Group H": ["Cape Verde","Saudi Arabia","Spain","Uruguay"],
    "Group I": ["France","Iraq","Norway","Senegal"],
    "Group J": ["Argentina","Algeria","Austria","Jordan"],
    "Group K": ["Colombia","DR Congo","Portugal","Uzbekistan"],
    "Group L": ["Croatia","England","Ghana","Panama"],
}


@app.route("/grupos")
def grupos():
    err = _auth()
    if err: return err

    try:
        data = espn_get("https://raw.githubusercontent.com/openfootball/worldcup.json/master/2026/worldcup.json")
    except Exception as e:
        app.logger.error(f"[grupos] error fetch openfootball: {e}")
        return jsonify({"error": str(e)}), 502

    matches = data.get("matches", [])
    standings = _calcular_standings_desde_partidos(matches)

    grupos_list = []
    for g_key in ["Group A","Group B","Group C","Group D","Group E","Group F",
                  "Group G","Group H","Group I","Group J","Group K","Group L"]:
        g_standings = standings.get(g_key, {})
        equipos_raw = _GRUPOS_EQUIPOS.get(g_key, list(g_standings.keys()))

        equipos = []
        for eq_raw in equipos_raw:
            eq = _normalizar_nombre(eq_raw)
            s  = g_standings.get(eq, g_standings.get(eq_raw, {"pj":0,"pg":0,"pe":0,"pp":0,"gf":0,"gc":0,"pts":0}))
            dg = s["gf"] - s["gc"]
            equipos.append({
                "nombre": eq,
                "pj":  s["pj"],
                "pg":  s["pg"],
                "pe":  s["pe"],
                "pp":  s["pp"],
                "gf":  s["gf"],
                "gc":  s["gc"],
                "dg":  dg,
                "pts": s["pts"],
            })

        equipos.sort(key=lambda x: (-x["pts"], -x["dg"], -x["gf"]))
        grupos_list.append({"nombre": g_key, "equipos": equipos})

    app.logger.info(f"[grupos] {len(grupos_list)} grupos, {len([m for m in matches if m.get('score',{}).get('ft')])} partidos con resultado")
    return jsonify({"grupos": grupos_list, "total": len(grupos_list)})


# ── MTA Stats ──────────────────────────────────────────────────────────────

# Almacén en memoria: { "Lobito": { "money": 10, "points": 8190, ... } }
_mta_stats = {}

MTA_SECRET = os.getenv("MTA_SECRET", "cambiame_mta")

@app.route("/mta/stats", methods=["POST"])
def mta_stats_push():
    """MTA empuja stats via fetchRemote cada vez que un jugador entra o sale."""
    if request.headers.get("X-Mta-Key") != MTA_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True)
    if not data or "nombre" not in data:
        return jsonify({"error": "bad request"}), 400

    nombre = data["nombre"]
    _mta_stats[nombre] = {
        "money":   data.get("money", 0),
        "points":  data.get("points", 0),
        "goals":   data.get("goals", 0),
        "assists": data.get("assists", 0),
        "saves":   data.get("saves", 0),
    }
    app.logger.info(f"[mta] stats actualizadas: {nombre}")
    return jsonify({"ok": True})


@app.route("/mta/stats", methods=["GET"])
def mta_stats_get():
    """El bot de Discord consulta stats de un jugador."""
    err = _auth()
    if err: return err

    nombre = request.args.get("player")
    if not nombre:
        return jsonify({"players": list(_mta_stats.keys())})

    stats = _mta_stats.get(nombre)
    if not stats:
        return jsonify({"error": "player not found"}), 404

    return jsonify({"nombre": nombre, **stats})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
