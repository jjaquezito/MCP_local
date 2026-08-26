# Knowledge Engine: capa de consulta sobre la Football Knowledge Base.
#
# Estas funciones son la logica real del proyecto y no saben nada de MCP ni de
# Claude: reciben argumentos simples y devuelven diccionarios serializables a
# JSON. El servidor MCP de la fase 15 solo las envuelve. Mantener esa frontera
# permite probar el conocimiento sin gastar tokens y reutilizarlo desde la CLI.

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

import psycopg
from psycopg.rows import dict_row

from config import settings

# Temporadas sin estadisticas de partido; se reportan como cobertura parcial.
STATS_FIRST_SEASON = settings.ML_FIRST_SEASON


def _connect() -> psycopg.Connection:
    # Conexion con filas como diccionarios para devolver JSON directo.
    return psycopg.connect(settings.DATABASE_URL, row_factory=dict_row)


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def _one(sql: str, params: tuple = ()) -> dict[str, Any] | None:
    result = _rows(sql, params)
    return result[0] if result else None


# ---------------------------------------------------------------------------
# Resolucion de entidades
# ---------------------------------------------------------------------------

def search_team(query: str, limit: int = 10) -> list[dict[str, Any]]:
    # Busca equipos por nombre parcial, priorizando los que mas han jugado
    # para que "United" devuelva primero al Manchester United.
    return _rows(
        """
        SELECT t.team_id, t.name, t.country, t.founded, t.logo,
               count(f.fixture_id) AS partidos,
               min(f.season) AS primera_temporada,
               max(f.season) AS ultima_temporada
        FROM teams t
        LEFT JOIN fixtures f
               ON f.home_team_id = t.team_id OR f.away_team_id = t.team_id
        WHERE t.name ILIKE %s
        GROUP BY t.team_id, t.name, t.country, t.founded, t.logo
        ORDER BY partidos DESC, t.name
        LIMIT %s
        """,
        (f"%{query}%", limit),
    )


def search_player(query: str, limit: int = 10) -> list[dict[str, Any]]:
    # Busca jugadores por nombre. Se ordena por minutos jugados porque la ficha
    # biografica esta vacia (no se descargo /players) y los minutos son la
    # unica senal de relevancia disponible.
    return _rows(
        """
        SELECT p.player_id, p.name, p.photo,
               count(DISTINCT s.fixture_id) AS partidos,
               coalesce(sum(s.minutes), 0) AS minutos,
               coalesce(sum(s.goals_total), 0) AS goles
        FROM players p
        JOIN fixture_player_statistics s USING (player_id)
        WHERE p.name ILIKE %s
        GROUP BY p.player_id, p.name, p.photo
        ORDER BY minutos DESC
        LIMIT %s
        """,
        (f"%{query}%", limit),
    )


# ---------------------------------------------------------------------------
# Partidos
# ---------------------------------------------------------------------------

def get_match(fixture_id: int) -> dict[str, Any] | None:
    # Ficha completa de un partido: marcador, sede, estadisticas de ambos
    # equipos y goles. Devuelve None si el id no existe.
    match = _one(
        """
        SELECT f.fixture_id, l.name AS liga, f.season, f.round,
               f.kickoff_utc, f.referee, v.name AS estadio, v.city AS ciudad,
               f.status_short AS estado,
               ht.team_id AS local_id,  ht.name AS local,
               at.team_id AS visita_id, at.name AS visita,
               f.goals_home, f.goals_away, f.ht_home, f.ht_away, f.result
        FROM fixtures f
        JOIN leagues l USING (league_id)
        JOIN teams ht ON ht.team_id = f.home_team_id
        JOIN teams at ON at.team_id = f.away_team_id
        LEFT JOIN venues v ON v.venue_id = f.venue_id
        WHERE f.fixture_id = %s
        """,
        (fixture_id,),
    )
    if match is None:
        return None

    match["estadisticas"] = _rows(
        """
        SELECT t.name AS equipo, s.is_home AS es_local,
               s.total_shots AS tiros, s.shots_on_goal AS tiros_a_puerta,
               s.ball_possession AS posesion, s.corner_kicks AS corners,
               s.fouls AS faltas, s.yellow_cards AS amarillas, s.red_cards AS rojas,
               s.passes_total AS pases, s.passes_percentage AS precision_pases,
               s.expected_goals AS xg
        FROM fixture_team_statistics s
        JOIN teams t USING (team_id)
        WHERE s.fixture_id = %s
        ORDER BY s.is_home DESC
        """,
        (fixture_id,),
    )
    match["goles"] = _rows(
        """
        SELECT e.elapsed AS minuto, t.name AS equipo,
               p.name AS jugador, a.name AS asistencia, e.detail AS tipo
        FROM fixture_events e
        LEFT JOIN teams t ON t.team_id = e.team_id
        LEFT JOIN players p ON p.player_id = e.player_id
        LEFT JOIN players a ON a.player_id = e.assist_id
        WHERE e.fixture_id = %s AND e.type = 'Goal'
        ORDER BY e.elapsed
        """,
        (fixture_id,),
    )
    # Aviso honesto: sin estadisticas no se puede analizar el partido a fondo.
    if not match["estadisticas"]:
        match["nota"] = (
            f"Temporada {match['season']}: API-Football no publica estadisticas "
            f"de partido antes de {STATS_FIRST_SEASON}."
        )
    return match


def get_team_form(
    team_id: int, before: date | None = None, last: int = 5
) -> dict[str, Any]:
    # Forma reciente de un equipo. `before` limita a partidos anteriores a esa
    # fecha: es la barrera contra data leakage cuando se construyen features,
    # porque impide mirar resultados posteriores al que se quiere predecir.
    matches = _rows(
        """
        SELECT f.fixture_id, f.kickoff_utc, l.name AS liga, f.season,
               (f.home_team_id = %(team)s) AS de_local,
               CASE WHEN f.home_team_id = %(team)s THEN at.name ELSE ht.name END AS rival,
               CASE WHEN f.home_team_id = %(team)s THEN f.goals_home ELSE f.goals_away END AS goles_favor,
               CASE WHEN f.home_team_id = %(team)s THEN f.goals_away ELSE f.goals_home END AS goles_contra,
               CASE
                   WHEN f.result = 'D' THEN 'E'
                   WHEN (f.result = 'H') = (f.home_team_id = %(team)s) THEN 'V'
                   ELSE 'D'
               END AS resultado
        FROM fixtures f
        JOIN leagues l USING (league_id)
        JOIN teams ht ON ht.team_id = f.home_team_id
        JOIN teams at ON at.team_id = f.away_team_id
        WHERE (f.home_team_id = %(team)s OR f.away_team_id = %(team)s)
          AND f.result IS NOT NULL
          AND (%(before)s::date IS NULL OR f.kickoff_utc < %(before)s::date)
        ORDER BY f.kickoff_utc DESC
        LIMIT %(last)s
        """,
        {"team": team_id, "before": before, "last": last},
    )

    team = _one("SELECT team_id, name FROM teams WHERE team_id = %s", (team_id,))
    victorias = sum(1 for m in matches if m["resultado"] == "V")
    empates = sum(1 for m in matches if m["resultado"] == "E")
    derrotas = sum(1 for m in matches if m["resultado"] == "D")

    return {
        "equipo": team["name"] if team else None,
        "team_id": team_id,
        "partidos_considerados": len(matches),
        "hasta": before.isoformat() if before else "hoy",
        "racha": "".join(m["resultado"] for m in matches),
        "victorias": victorias,
        "empates": empates,
        "derrotas": derrotas,
        "puntos": victorias * 3 + empates,
        "goles_favor": sum(m["goles_favor"] or 0 for m in matches),
        "goles_contra": sum(m["goles_contra"] or 0 for m in matches),
        "partidos": matches,
    }


def get_head_to_head(team_a: int, team_b: int, limit: int = 10) -> dict[str, Any]:
    # Historial directo entre dos equipos.
    matches = _rows(
        """
        SELECT f.fixture_id, f.kickoff_utc, l.name AS liga, f.season,
               ht.name AS local, at.name AS visita,
               f.goals_home, f.goals_away
        FROM fixtures f
        JOIN leagues l USING (league_id)
        JOIN teams ht ON ht.team_id = f.home_team_id
        JOIN teams at ON at.team_id = f.away_team_id
        WHERE ((f.home_team_id = %s AND f.away_team_id = %s)
            OR (f.home_team_id = %s AND f.away_team_id = %s))
          AND f.result IS NOT NULL
        ORDER BY f.kickoff_utc DESC
        LIMIT %s
        """,
        (team_a, team_b, team_b, team_a, limit),
    )
    tally = _one(
        """
        SELECT
          count(*) FILTER (WHERE (f.result='H') = (f.home_team_id=%(a)s)
                             AND f.result <> 'D') AS gana_a,
          count(*) FILTER (WHERE f.result = 'D')  AS empates,
          count(*) FILTER (WHERE (f.result='H') = (f.home_team_id=%(b)s)
                             AND f.result <> 'D') AS gana_b,
          count(*) AS total
        FROM fixtures f
        WHERE ((f.home_team_id=%(a)s AND f.away_team_id=%(b)s)
            OR (f.home_team_id=%(b)s AND f.away_team_id=%(a)s))
          AND f.result IS NOT NULL
        """,
        {"a": team_a, "b": team_b},
    )
    goles = _one(
        """
        SELECT avg(f.goals_home + f.goals_away) AS goles_promedio_partido, count(*) AS total
        FROM fixtures f
        WHERE ((f.home_team_id = %(a)s AND f.away_team_id = %(b)s)
            OR (f.home_team_id = %(b)s AND f.away_team_id = %(a)s))
          AND f.result IS NOT NULL
        """,
        {"a": team_a, "b": team_b},
    )
    # Tarjetas/faltas/corners solo existen desde ML_FIRST_SEASON; el JOIN
    # (en vez de LEFT JOIN) descarta automaticamente los partidos sin stats.
    stats_por_equipo = _rows(
        """
        SELECT fts.team_id,
               avg(fts.yellow_cards) AS amarillas_promedio,
               avg(fts.red_cards)    AS rojas_promedio,
               avg(fts.fouls)        AS faltas_promedio,
               avg(fts.corner_kicks) AS corners_promedio,
               count(*)              AS partidos_con_estadisticas
        FROM fixtures f
        JOIN fixture_team_statistics fts ON fts.fixture_id = f.fixture_id
        WHERE ((f.home_team_id = %(a)s AND f.away_team_id = %(b)s)
            OR (f.home_team_id = %(b)s AND f.away_team_id = %(a)s))
          AND f.result IS NOT NULL
          AND fts.team_id IN (%(a)s, %(b)s)
        GROUP BY fts.team_id
        """,
        {"a": team_a, "b": team_b},
    )
    stats_lookup = {r["team_id"]: r for r in stats_por_equipo}

    names = _rows("SELECT team_id, name FROM teams WHERE team_id IN (%s, %s)", (team_a, team_b))
    lookup = {r["team_id"]: r["name"] for r in names}

    def _promedios(team_id: int) -> dict[str, Any]:
        s = stats_lookup.get(team_id)
        if s is None:
            return {"_nota": f"Sin estadisticas de partido (solo disponibles desde {STATS_FIRST_SEASON})"}
        return {
            "amarillas_promedio": s["amarillas_promedio"],
            "rojas_promedio": s["rojas_promedio"],
            "faltas_promedio": s["faltas_promedio"],
            "corners_promedio": s["corners_promedio"],
            "partidos_con_estadisticas": s["partidos_con_estadisticas"],
        }

    return {
        "equipo_a": lookup.get(team_a),
        "equipo_b": lookup.get(team_b),
        "historial": tally,
        "ultimos_partidos": matches,
        "goles_promedio_partido": goles["goles_promedio_partido"] if goles else None,
        "promedios_equipo_a": _promedios(team_a),
        "promedios_equipo_b": _promedios(team_b),
    }


# ---------------------------------------------------------------------------
# Agregados de temporada
# ---------------------------------------------------------------------------

def get_team_season(team_id: int, league_id: int, season: int) -> dict[str, Any] | None:
    # Rendimiento de un equipo en una temporada, combinando la clasificacion
    # oficial con los promedios calculados desde las estadisticas de partido.
    standing = _one(
        """
        SELECT s.rank AS posicion, s.points AS puntos, s.played AS jugados,
               s.win AS ganados, s.draw AS empatados, s.lose AS perdidos,
               s.goals_for AS goles_favor, s.goals_against AS goles_contra,
               s.form AS racha_final
        FROM standings s
        WHERE s.team_id = %s AND s.league_id = %s AND s.season = %s
        """,
        (team_id, league_id, season),
    )
    averages = _one(
        """
        SELECT round(avg(st.total_shots), 1)       AS tiros_promedio,
               round(avg(st.shots_on_goal), 1)     AS tiros_a_puerta_promedio,
               round(avg(st.ball_possession), 1)   AS posesion_promedio,
               round(avg(st.corner_kicks), 1)      AS corners_promedio,
               round(avg(st.passes_percentage), 1) AS precision_pases,
               round(avg(st.expected_goals), 2)    AS xg_promedio,
               count(*)                            AS partidos_con_stats
        FROM fixture_team_statistics st
        JOIN fixtures f USING (fixture_id)
        WHERE st.team_id = %s AND f.league_id = %s AND f.season = %s
        """,
        (team_id, league_id, season),
    )
    team = _one("SELECT name FROM teams WHERE team_id = %s", (team_id,))
    if standing is None and (averages is None or not averages["partidos_con_stats"]):
        return None

    result = {
        "equipo": team["name"] if team else None,
        "temporada": season,
        "clasificacion": standing,
        "promedios": averages,
    }
    if averages and not averages["partidos_con_stats"]:
        result["nota"] = (
            f"Sin estadisticas de partido para {season}; "
            f"solo disponibles desde {STATS_FIRST_SEASON}."
        )
    return result


def get_player_season(player_id: int, season: int) -> dict[str, Any] | None:
    # Estadisticas de temporada agregadas desde los partidos. No se descargo el
    # endpoint /players (costaba ~1,900 requests): se calculan aqui con un
    # GROUP BY, lo que ademas permite acotarlas a cualquier rango de fechas.
    stats = _one(
        """
        SELECT p.name AS jugador, t.name AS equipo, l.name AS liga,
               count(*) AS partidos,
               count(*) FILTER (WHERE NOT s.is_substitute) AS titularidades,
               sum(s.minutes) AS minutos,
               sum(s.goals_total) AS goles,
               sum(s.assists) AS asistencias,
               sum(s.shots_total) AS tiros,
               sum(s.shots_on) AS tiros_a_puerta,
               sum(s.passes_key) AS pases_clave,
               sum(s.cards_yellow) AS amarillas,
               sum(s.cards_red) AS rojas,
               round(avg(s.rating) FILTER (WHERE s.rating IS NOT NULL), 2) AS rating_promedio
        FROM fixture_player_statistics s
        JOIN players p USING (player_id)
        JOIN teams t ON t.team_id = s.team_id
        JOIN fixtures f USING (fixture_id)
        JOIN leagues l USING (league_id)
        WHERE s.player_id = %s AND f.season = %s
        GROUP BY p.name, t.name, l.name
        ORDER BY sum(s.minutes) DESC
        LIMIT 1
        """,
        (player_id, season),
    )
    return stats


# ---------------------------------------------------------------------------
# Comparaciones
# ---------------------------------------------------------------------------

def compare_teams(team_a: int, team_b: int, last: int = 10) -> dict[str, Any]:
    # Compara forma reciente y enfrentamientos directos de dos equipos.
    return {
        "forma_a": get_team_form(team_a, last=last),
        "forma_b": get_team_form(team_b, last=last),
        "head_to_head": get_head_to_head(team_a, team_b),
    }


def compare_players(player_a: int, player_b: int, season: int) -> dict[str, Any]:
    # Compara dos jugadores en una misma temporada.
    return {
        "temporada": season,
        "jugador_a": get_player_season(player_a, season),
        "jugador_b": get_player_season(player_b, season),
    }


# ---------------------------------------------------------------------------
# Prediccion
# ---------------------------------------------------------------------------

# Entrenado por conocimiento/ml/train_predict_match.py sobre 2015-2025.
_MODEL_PATH = Path(__file__).resolve().parent.parent / "data" / "predict_model.joblib"
_modelo_cache: dict[str, Any] = {}


def _cargar_modelo() -> dict[str, Any]:
    if "modelo" not in _modelo_cache:
        import joblib
        _modelo_cache["modelo"] = joblib.load(_MODEL_PATH)
    return _modelo_cache["modelo"]


def _dias_descanso(ultimo_partido: datetime | None) -> int | None:
    if ultimo_partido is None:
        return None
    dias = (datetime.now(timezone.utc) - ultimo_partido).days
    return min(dias, 30)  # un parate de meses entre temporadas no es "descanso"


def _partidos_temporada_actual(team_id: int) -> int:
    # La temporada en curso puede no tener fixtures locales todavia (se
    # sincroniza aparte); por eso el default natural es 0, no un error.
    fila = _one(
        """
        SELECT count(*) AS n FROM fixtures
        WHERE season = (SELECT max(season) + 1 FROM fixtures)
          AND (home_team_id = %s OR away_team_id = %s)
          AND result IS NOT NULL
        """,
        (team_id, team_id),
    )
    return fila["n"] if fila else 0


def predict_match(home_team_id: int, away_team_id: int) -> dict[str, Any]:
    # Predice un partido que puede no existir todavia como fixture (por
    # ejemplo uno agendado para la temporada en curso, visto via
    # MCP_Remoto). En vez de depender de un fixture_id ya cargado, usa el
    # estado MAS RECIENTE de cada equipo (team_current_form) para calcular
    # las mismas features con las que se entreno el modelo.
    home = _one("SELECT * FROM team_current_form WHERE team_id = %s", (home_team_id,))
    away = _one("SELECT * FROM team_current_form WHERE team_id = %s", (away_team_id,))
    if home is None or away is None:
        sin_datos = home_team_id if home is None else away_team_id
        return {
            "error": f"No hay historial suficiente para el equipo {sin_datos}. "
                     "Verifica el id con search_team.",
        }

    # Mismo criterio de orientacion que build_features.py: el marcador se
    # guarda con la pareja ordenada y se reorienta a quien juega de local hoy.
    par = tuple(sorted((home_team_id, away_team_id)))
    tally = _one(
        """
        SELECT
          count(*) FILTER (WHERE (f.result='H') = (f.home_team_id=%(a)s) AND f.result <> 'D') AS gana_a,
          count(*) FILTER (WHERE f.result = 'D') AS empates,
          count(*) FILTER (WHERE (f.result='H') = (f.home_team_id=%(b)s) AND f.result <> 'D') AS gana_b
        FROM fixtures f
        WHERE ((f.home_team_id=%(a)s AND f.away_team_id=%(b)s)
            OR (f.home_team_id=%(b)s AND f.away_team_id=%(a)s))
          AND f.result IS NOT NULL
        """,
        {"a": par[0], "b": par[1]},
    )
    if par[0] == home_team_id:
        h2h_home_wins, h2h_draws, h2h_away_wins = tally["gana_a"], tally["empates"], tally["gana_b"]
    else:
        h2h_home_wins, h2h_draws, h2h_away_wins = tally["gana_b"], tally["empates"], tally["gana_a"]

    home_rest = _dias_descanso(home["last_match_utc"])
    away_rest = _dias_descanso(away["last_match_utc"])
    rest_diff = (home_rest - away_rest) if home_rest is not None and away_rest is not None else None

    fila = {
        "home_elo": home["elo"], "away_elo": away["elo"],
        "elo_diff": home["elo"] - away["elo"],
        "home_form_pts_5": home["form_pts_5"], "away_form_pts_5": away["form_pts_5"],
        "home_gf_avg_5": home["gf_avg_5"], "away_gf_avg_5": away["gf_avg_5"],
        "home_ga_avg_5": home["ga_avg_5"], "away_ga_avg_5": away["ga_avg_5"],
        "home_home_pts_5": home["home_pts_5"], "away_away_pts_5": away["away_pts_5"],
        "home_shots_avg_5": home["shots_avg_5"], "away_shots_avg_5": away["shots_avg_5"],
        "home_sot_avg_5": home["sot_avg_5"], "away_sot_avg_5": away["sot_avg_5"],
        "home_poss_avg_5": home["poss_avg_5"], "away_poss_avg_5": away["poss_avg_5"],
        "home_corners_avg_5": home["corners_avg_5"], "away_corners_avg_5": away["corners_avg_5"],
        "home_pass_acc_avg_5": home["pass_acc_avg_5"], "away_pass_acc_avg_5": away["pass_acc_avg_5"],
        "home_rest_days": home_rest, "away_rest_days": away_rest, "rest_diff": rest_diff,
        "home_matches_played": _partidos_temporada_actual(home_team_id),
        "away_matches_played": _partidos_temporada_actual(away_team_id),
        "h2h_home_wins": h2h_home_wins, "h2h_draws": h2h_draws, "h2h_away_wins": h2h_away_wins,
    }

    modelo = _cargar_modelo()
    vector = [[
        float(fila[c]) if fila[c] is not None else float("nan")
        for c in modelo["feature_columns"]
    ]]
    proba = modelo["pipeline"].predict_proba(vector)[0]
    orden = list(modelo["pipeline"].named_steps["clf"].classes_)
    prob_por_clase = dict(zip(orden, proba))

    nombres = _rows("SELECT team_id, name FROM teams WHERE team_id IN (%s, %s)", (home_team_id, away_team_id))
    lookup = {r["team_id"]: r["name"] for r in nombres}
    ultima_actualizacion = min(
        (t for t in (home["last_match_utc"], away["last_match_utc"]) if t is not None),
        default=None,
    )

    return {
        "local": lookup.get(home_team_id),
        "visitante": lookup.get(away_team_id),
        "probabilidad_local": round(float(prob_por_clase.get("H", 0.0)), 3),
        "probabilidad_empate": round(float(prob_por_clase.get("D", 0.0)), 3),
        "probabilidad_visitante": round(float(prob_por_clase.get("A", 0.0)), 3),
        "elo_local": home["elo"],
        "elo_visitante": away["elo"],
        "head_to_head": {
            "gana_local": h2h_home_wins, "empates": h2h_draws, "gana_visitante": h2h_away_wins,
        },
        "modelo": modelo["modelo"],
        "entrenado_con": f"{modelo['filas_entrenamiento']:,} partidos, 2015-2025, 6 ligas domesticas (sin Champions)",
        "estado_de_forma_al": ultima_actualizacion,
        "advertencia": (
            "Probabilidad estadistica basada en historial, no una garantia. "
            "El modelo acierta ~52-55% de los resultados en prueba historica; "
            "usar junto con contexto actual (lesiones, motivacion) antes de decidir."
        ),
    }


# ---------------------------------------------------------------------------
# Honestidad sobre los datos
# ---------------------------------------------------------------------------

def data_coverage(league_id: int | None = None, season: int | None = None) -> list[dict]:
    # Que hay y que no hay en la base. Es la funcion que permite a las tools
    # MCP responder "no tengo ese dato" en lugar de inventarlo.
    return _rows(
        """
        SELECT l.name AS liga, s.season AS temporada,
               s.has_events AS eventos, s.has_lineups AS alineaciones,
               s.has_fixture_statistics AS stats_equipo,
               s.has_player_statistics AS stats_jugador,
               count(f.fixture_id) AS partidos_cargados
        FROM seasons s
        JOIN leagues l USING (league_id)
        LEFT JOIN fixtures f ON f.league_id = s.league_id AND f.season = s.season
        WHERE (%s::int IS NULL OR s.league_id = %s)
          AND (%s::int IS NULL OR s.season = %s)
        GROUP BY l.name, s.season, s.has_events, s.has_lineups,
                 s.has_fixture_statistics, s.has_player_statistics
        HAVING count(f.fixture_id) > 0
        ORDER BY l.name, s.season
        """,
        (league_id, league_id, season, season),
    )
