# Configuracion minima del servidor MCP.
#
# A diferencia del proyecto completo, aqui no hay extractor ni claves de
# API-Football: este servidor solo lee una base PostgreSQL ya construida.

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# Cadena de conexion a la Knowledge Base restaurada desde data/football.dump
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://postgres@localhost:5432/football")

# Primera temporada con estadisticas de partido en API-Football. Las anteriores
# solo traen resultados, goles y alineaciones; el servidor lo advierte en sus
# respuestas en lugar de devolver ceros.
ML_FIRST_SEASON = 2015
