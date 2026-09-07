#!/usr/bin/env python3
# Football Intelligence MCP Server
#
# Implementa el Model Context Protocol hablando JSON-RPC 2.0 directamente
# sobre stdio, sin usar ningun SDK de MCP. Los mensajes se intercambian como
# JSON delimitado por saltos de linea, segun el transporte stdio de la
# especificacion 2025-06-18.
#
# Metodos implementados:
#   initialize                 handshake y negociacion de version
#   notifications/initialized  aviso del cliente (no lleva respuesta)
#   tools/list                 catalogo de herramientas
#   tools/call                 ejecucion de una herramienta
#   ping                       verificacion de vida
#
# REGLA CRITICA: stdout transporta unicamente mensajes JSON-RPC. Cualquier
# traza de depuracion va a stderr; un print() suelto corrompe el protocolo y
# el cliente pierde la sesion.
#
# Uso:
#     python mcp_server/server.py

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import engine

PROTOCOL_VERSION = "2025-06-18"
SERVER_NAME = "football-intelligence"
SERVER_VERSION = "0.1.0"

# Codigos de error estandar de JSON-RPC 2.0 (https://www.jsonrpc.org/specification)
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def log(message: str) -> None:
    # La depuracion va a stderr para no contaminar el canal JSON-RPC.
    print(f"[{SERVER_NAME}] {message}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Catalogo de herramientas
# ---------------------------------------------------------------------------
# Cada entrada declara el esquema JSON de sus argumentos. Claude lee esto para
# decidir que herramienta usar y con que parametros.

TOOLS: list[dict[str, Any]] = [
    {
        "name": "search_team",
        "description": (
            "Busca equipos por nombre parcial en la base historica (2010-2025, "
            "7 competiciones europeas). Devuelve team_id, pais y cuantos partidos "
            "tiene registrados. Usar SIEMPRE antes de cualquier herramienta que "
            "requiera un team_id."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Nombre o parte del nombre"},
                "limit": {"type": "integer", "description": "Maximo de resultados", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "search_player",
        "description": (
            "Busca jugadores por nombre. Devuelve player_id, partidos, minutos y "
            "goles acumulados. Usar antes de get_player_season."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Nombre o apellido"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_match",
        "description": (
            "Ficha completa de un partido: marcador, sede, arbitro, estadisticas "
            "de ambos equipos (tiros, posesion, xG cuando existe) y los goles con "
            "minuto y asistente. Los partidos anteriores a 2015 no tienen "
            "estadisticas y la respuesta lo indica explicitamente."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"fixture_id": {"type": "integer"}},
            "required": ["fixture_id"],
        },
    },
    {
        "name": "get_team_form",
        "description": (
            "Forma reciente de un equipo: racha, puntos, goles a favor y en contra "
            "de sus ultimos partidos. El parametro 'before' limita el calculo a "
            "partidos anteriores a esa fecha, lo que permite reconstruir el estado "
            "de un equipo en cualquier momento del pasado."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "last": {"type": "integer", "description": "Cuantos partidos", "default": 5},
                "before": {"type": "string", "description": "Fecha ISO YYYY-MM-DD (opcional)"},
            },
            "required": ["team_id"],
        },
    },
    {
        "name": "get_recent_lineup",
        "description": (
            "Formacion, entrenador (con foto) y once titular del ULTIMO partido "
            "con datos de alineacion de un equipo. Util como 'alineacion probable' "
            "para un analisis o prediccion, pero es honestamente el ultimo once "
            "visto, no una confirmacion del proximo partido -- lesiones y rotacion "
            "pueden cambiarlo. Formacion/entrenador solo existen desde 2015."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"team_id": {"type": "integer"}},
            "required": ["team_id"],
        },
    },
    {
        "name": "get_team_squad",
        "description": (
            "Plantilla que ha jugado los ultimos partidos de un equipo, con foto, "
            "goles, asistencias y rating promedio de cada jugador, y quien es el "
            "'jugador mas determinante' (mas goles + asistencias recientes). Usar "
            "para responder que jugadores clave tiene un equipo ahora mismo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "last": {"type": "integer", "description": "Partidos recientes a considerar", "default": 10},
            },
            "required": ["team_id"],
        },
    },
    {
        "name": "get_league_info",
        "description": "Escudo y bandera de una competicion (no hay imagenes de trofeos en la base).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "league_id": {
                    "type": "integer",
                    "description": "39=Premier, 140=La Liga, 135=Serie A, 78=Bundesliga, 61=Ligue 1, 94=Primeira, 2=Champions",
                },
            },
            "required": ["league_id"],
        },
    },
    {
        "name": "get_head_to_head",
        "description": (
            "Historial directo entre dos equipos: balance de victorias, empates y "
            "derrotas, mas los ultimos enfrentamientos con marcador."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_a": {"type": "integer"},
                "team_b": {"type": "integer"},
                "limit": {"type": "integer", "default": 10},
            },
            "required": ["team_a", "team_b"],
        },
    },
    {
        "name": "get_team_season",
        "description": (
            "Rendimiento de un equipo en una temporada: posicion final, puntos, "
            "goles, y promedios de tiros, posesion, corners y precision de pase."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_id": {"type": "integer"},
                "league_id": {
                    "type": "integer",
                    "description": "39=Premier, 140=La Liga, 135=Serie A, 78=Bundesliga, 61=Ligue 1, 94=Primeira, 2=Champions",
                },
                "season": {"type": "integer", "description": "Ano de inicio, ej. 2024 = 2024-25"},
            },
            "required": ["team_id", "league_id", "season"],
        },
    },
    {
        "name": "get_player_season",
        "description": (
            "Estadisticas de un jugador en una temporada, agregadas partido a "
            "partido: goles, asistencias, minutos, tiros, tarjetas y rating medio."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "player_id": {"type": "integer"},
                "season": {"type": "integer"},
            },
            "required": ["player_id", "season"],
        },
    },
    {
        "name": "compare_teams",
        "description": (
            "Compara dos equipos: forma reciente de cada uno mas su historial "
            "directo. Util antes de analizar un enfrentamiento."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "team_a": {"type": "integer"},
                "team_b": {"type": "integer"},
                "last": {"type": "integer", "default": 10},
            },
            "required": ["team_a", "team_b"],
        },
    },
    {
        "name": "compare_players",
        "description": (
            "Compara dos jugadores en una misma temporada: goles, asistencias, "
            "minutos, tiros, tarjetas y rating medio de cada uno."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "player_a": {"type": "integer"},
                "player_b": {"type": "integer"},
                "season": {"type": "integer"},
            },
            "required": ["player_a", "player_b", "season"],
        },
    },
    {
        "name": "data_coverage",
        "description": (
            "Que datos existen realmente en la base, por liga y temporada. "
            "Consultar esta herramienta antes de afirmar que un dato no existe, "
            "en lugar de suponerlo."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "league_id": {"type": "integer"},
                "season": {"type": "integer"},
            },
        },
    },
    {
        "name": "predict_match",
        "description": (
            "Predice un partido usando un modelo entrenado sobre 23,168 partidos "
            "(2015-2025, 6 ligas domesticas). No requiere que el partido ya exista "
            "en la base: usa el estado mas reciente (Elo, forma, descanso) de cada "
            "equipo, asi que sirve para partidos futuros o de la temporada en "
            "curso (por ejemplo, uno visto con football_live). Devuelve "
            "probabilidad de victoria local, empate y visitante, mas el historial "
            "directo entre ambos. Usar get_head_to_head para contexto adicional "
            "(goles, tarjetas y faltas promedio entre estos dos equipos)."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "home_team_id": {"type": "integer", "description": "Equipo que juega de local"},
                "away_team_id": {"type": "integer", "description": "Equipo que juega de visitante"},
            },
            "required": ["home_team_id", "away_team_id"],
        },
    },
]


# ---------------------------------------------------------------------------
# Implementacion de las herramientas
# ---------------------------------------------------------------------------
# Cada funcion traduce los argumentos del protocolo a una llamada del
# Knowledge Engine. Toda la logica real vive alli, no aqui.

def _parse_date(value: str | None):
    from datetime import date
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"Fecha invalida '{value}'; se espera YYYY-MM-DD")


HANDLERS: dict[str, Callable[..., Any]] = {
    "search_team": lambda query, limit=10: engine.search_team(query, limit),
    "search_player": lambda query, limit=10: engine.search_player(query, limit),
    "get_match": lambda fixture_id: engine.get_match(fixture_id),
    "get_team_form": lambda team_id, last=5, before=None: engine.get_team_form(
        team_id, before=_parse_date(before), last=last
    ),
    "get_recent_lineup": lambda team_id: engine.get_recent_lineup(team_id),
    "get_team_squad": lambda team_id, last=10: engine.get_team_squad(team_id, last),
    "get_league_info": lambda league_id: engine.get_league_info(league_id),
    "get_head_to_head": lambda team_a, team_b, limit=10: engine.get_head_to_head(
        team_a, team_b, limit
    ),
    "get_team_season": lambda team_id, league_id, season: engine.get_team_season(
        team_id, league_id, season
    ),
    "get_player_season": lambda player_id, season: engine.get_player_season(player_id, season),
    "compare_teams": lambda team_a, team_b, last=10: engine.compare_teams(team_a, team_b, last),
    "compare_players": lambda player_a, player_b, season: engine.compare_players(
        player_a, player_b, season
    ),
    "data_coverage": lambda league_id=None, season=None: engine.data_coverage(league_id, season),
    "predict_match": lambda home_team_id, away_team_id: engine.predict_match(
        home_team_id, away_team_id
    ),
}


def ejecutar_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    # Ejecuta una herramienta y la envuelve en el formato de resultado de MCP.
    # Los errores de la herramienta NO son errores de JSON-RPC: se devuelven
    # como contenido con isError=true para que el modelo pueda reaccionar.
    handler = HANDLERS.get(name)
    if handler is None:
        return {
            "content": [{"type": "text", "text": f"Herramienta desconocida: {name}"}],
            "isError": True,
        }
    try:
        resultado = handler(**arguments)
        if resultado is None or resultado == []:
            texto = "Sin resultados para esos parametros."
        else:
            texto = json.dumps(resultado, ensure_ascii=False, indent=2, default=str)
        return {"content": [{"type": "text", "text": texto}]}
    except TypeError as exc:
        return {
            "content": [{"type": "text", "text": f"Argumentos invalidos: {exc}"}],
            "isError": True,
        }
    except Exception as exc:  # noqa: BLE001 - se reporta al modelo, no se propaga
        log(f"error en {name}: {type(exc).__name__}: {exc}")
        return {
            "content": [{"type": "text", "text": f"Error ejecutando {name}: {exc}"}],
            "isError": True,
        }


# ---------------------------------------------------------------------------
# Capa JSON-RPC
# ---------------------------------------------------------------------------

def respuesta(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def manejar(mensaje: dict[str, Any]) -> dict[str, Any] | None:
    # Despacha un mensaje JSON-RPC. Devuelve None para las notificaciones,
    # que por definicion no llevan respuesta.
    metodo = mensaje.get("method")
    request_id = mensaje.get("id")
    params = mensaje.get("params") or {}

    # Las notificaciones se distinguen por no traer 'id'.
    if request_id is None:
        if metodo == "notifications/initialized":
            log("cliente inicializado")
        return None

    if metodo == "initialize":
        # Se responde con la version del cliente si la soportamos; si no, con
        # la nuestra, y el cliente decide si continua.
        pedida = params.get("protocolVersion")
        cliente = (params.get("clientInfo") or {}).get("name", "desconocido")
        log(f"initialize desde '{cliente}' (protocolo {pedida})")
        return respuesta(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if metodo == "ping":
        return respuesta(request_id, {})

    if metodo == "tools/list":
        return respuesta(request_id, {"tools": TOOLS})

    if metodo == "tools/call":
        nombre = params.get("name")
        argumentos = params.get("arguments") or {}
        if not nombre:
            return error(request_id, INVALID_PARAMS, "Falta 'name' en params")
        log(f"tools/call {nombre} {argumentos}")
        return respuesta(request_id, ejecutar_tool(nombre, argumentos))

    return error(request_id, METHOD_NOT_FOUND, f"Metodo no soportado: {metodo}")


def main() -> int:
    log(f"escuchando en stdio (protocolo {PROTOCOL_VERSION}, {len(TOOLS)} herramientas)")
    for linea in sys.stdin:
        linea = linea.strip()
        if not linea:
            continue
        try:
            mensaje = json.loads(linea)
        except json.JSONDecodeError as exc:
            salida = error(None, PARSE_ERROR, f"JSON invalido: {exc}")
        else:
            if not isinstance(mensaje, dict) or mensaje.get("jsonrpc") != "2.0":
                salida = error(mensaje.get("id") if isinstance(mensaje, dict) else None,
                               INVALID_REQUEST, "Se esperaba JSON-RPC 2.0")
            else:
                try:
                    salida = manejar(mensaje)
                except Exception as exc:  # noqa: BLE001
                    log(f"error interno: {type(exc).__name__}: {exc}")
                    salida = error(mensaje.get("id"), INTERNAL_ERROR, str(exc))

        if salida is not None:
            sys.stdout.write(json.dumps(salida, ensure_ascii=False) + "\n")
            sys.stdout.flush()

    log("stdin cerrado, terminando")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
