-- =====================================================================
-- Football Intelligence - Esquema base de la Knowledge Base
-- =====================================================================
-- Principios de diseno:
--   * Los identificadores son los de API-Football (league_id, fixture_id,
--     team_id, player_id...). Nunca se relacionan entidades por nombre.
--   * Las estadisticas de partido solo existen desde 2015; por eso viven
--     en tablas aparte de `fixtures` y no como columnas nulas.
--   * `seasons.coverage` conserva el objeto crudo de la API para que las
--     tools MCP puedan responder "no tengo ese dato" en lugar de inventar.
--   * SQL vainilla: portable a Supabase o cualquier Postgres gestionado.
-- =====================================================================
-- ---------------------------------------------------------------------
-- Catalogo: competiciones, temporadas, sedes, equipos, jugadores
-- ---------------------------------------------------------------------

BEGIN;


CREATE TABLE IF NOT EXISTS leagues (
    league_id   INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    type        TEXT,
    country     TEXT,
    country_code TEXT,
    logo        TEXT,
    flag        TEXT
);

CREATE TABLE IF NOT EXISTS seasons (
    league_id   INTEGER NOT NULL REFERENCES leagues(league_id) ON DELETE CASCADE,
    season      INTEGER NOT NULL,
    start_date  DATE,
    end_date    DATE,
    is_current  BOOLEAN NOT NULL DEFAULT FALSE,
    -- Objeto `coverage` tal cual lo devuelve /leagues.
    coverage    JSONB,
    -- Banderas extraidas para filtrar sin desempaquetar el JSONB.
    has_events              BOOLEAN NOT NULL DEFAULT FALSE,
    has_lineups             BOOLEAN NOT NULL DEFAULT FALSE,
    has_fixture_statistics  BOOLEAN NOT NULL DEFAULT FALSE,
    has_player_statistics   BOOLEAN NOT NULL DEFAULT FALSE,
    has_standings           BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (league_id, season)
);

CREATE TABLE IF NOT EXISTS venues (
    venue_id    INTEGER PRIMARY KEY,
    name        TEXT,
    address     TEXT,
    city        TEXT,
    country     TEXT,
    capacity    INTEGER,
    surface     TEXT,
    image       TEXT
);

CREATE TABLE IF NOT EXISTS teams (
    team_id     INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL,
    code        TEXT,
    country     TEXT,
    founded     INTEGER,
    is_national BOOLEAN NOT NULL DEFAULT FALSE,
    logo        TEXT,
    venue_id    INTEGER REFERENCES venues(venue_id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_teams_name ON teams (lower(name));

CREATE TABLE IF NOT EXISTS team_seasons (
    team_id     INTEGER NOT NULL REFERENCES teams(team_id) ON DELETE CASCADE,
    league_id   INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    PRIMARY KEY (team_id, league_id, season),
    FOREIGN KEY (league_id, season) REFERENCES seasons(league_id, season) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS players (
    player_id   INTEGER PRIMARY KEY,
    name        TEXT,
    firstname   TEXT,
    lastname    TEXT,
    birth_date  DATE,
    birth_place TEXT,
    birth_country TEXT,
    nationality TEXT,
    height_cm   INTEGER,
    weight_kg   INTEGER,
    photo       TEXT
);

CREATE INDEX IF NOT EXISTS idx_players_name ON players (lower(name));

CREATE TABLE IF NOT EXISTS coaches (
    coach_id    INTEGER PRIMARY KEY,
    name        TEXT,
    firstname   TEXT,
    lastname    TEXT,
    nationality TEXT,
    photo       TEXT
);


CREATE TABLE IF NOT EXISTS fixtures (
    fixture_id      INTEGER PRIMARY KEY,
    league_id       INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    round           TEXT,
    kickoff_utc     TIMESTAMPTZ,
    timestamp_unix  BIGINT,
    venue_id        INTEGER REFERENCES venues(venue_id) ON DELETE SET NULL,
    referee         TEXT,
    status_short    TEXT,          -- FT, AET, PEN, PST, CANC...
    status_long     TEXT,
    elapsed         INTEGER,
    home_team_id    INTEGER NOT NULL REFERENCES teams(team_id),
    away_team_id    INTEGER NOT NULL REFERENCES teams(team_id),
    home_winner     BOOLEAN,       -- NULL en empate
    away_winner     BOOLEAN,
    goals_home      INTEGER,
    goals_away      INTEGER,
    ht_home         INTEGER,       -- halftime
    ht_away         INTEGER,
    ft_home         INTEGER,       -- fulltime (90')
    ft_away         INTEGER,
    et_home         INTEGER,       -- extra time
    et_away         INTEGER,
    pen_home        INTEGER,       -- penales
    pen_away        INTEGER,
    -- Resultado 1X2 derivado, unica fuente de verdad para el target de ML.
    result          CHAR(1) GENERATED ALWAYS AS (
        CASE
            WHEN goals_home IS NULL OR goals_away IS NULL THEN NULL
            WHEN goals_home > goals_away THEN 'H'
            WHEN goals_home < goals_away THEN 'A'
            ELSE 'D'
        END
    ) STORED,
    FOREIGN KEY (league_id, season) REFERENCES seasons(league_id, season),
    CONSTRAINT fixtures_teams_differ CHECK (home_team_id <> away_team_id)
);

CREATE INDEX IF NOT EXISTS idx_fixtures_league_season ON fixtures (league_id, season);
CREATE INDEX IF NOT EXISTS idx_fixtures_kickoff      ON fixtures (kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_fixtures_home         ON fixtures (home_team_id, kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_fixtures_away         ON fixtures (away_team_id, kickoff_utc);
CREATE INDEX IF NOT EXISTS idx_fixtures_status       ON fixtures (status_short);


CREATE TABLE IF NOT EXISTS fixture_team_statistics (
    fixture_id          INTEGER NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    team_id             INTEGER NOT NULL REFERENCES teams(team_id),
    is_home             BOOLEAN NOT NULL,
    shots_on_goal       INTEGER,
    shots_off_goal      INTEGER,
    total_shots         INTEGER,
    blocked_shots       INTEGER,
    shots_insidebox     INTEGER,
    shots_outsidebox    INTEGER,
    fouls               INTEGER,
    corner_kicks        INTEGER,
    offsides            INTEGER,
    ball_possession     NUMERIC(5,2),   -- porcentaje ya normalizado
    yellow_cards        INTEGER,
    red_cards           INTEGER,
    goalkeeper_saves    INTEGER,
    passes_total        INTEGER,
    passes_accurate     INTEGER,
    passes_percentage   NUMERIC(5,2),
    expected_goals      NUMERIC(6,3),   -- solo temporadas recientes
    goals_prevented     NUMERIC(6,3),
    PRIMARY KEY (fixture_id, team_id)
);

CREATE INDEX IF NOT EXISTS idx_fts_team ON fixture_team_statistics (team_id);


CREATE TABLE IF NOT EXISTS fixture_events (
    event_id    BIGSERIAL PRIMARY KEY,
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    team_id     INTEGER REFERENCES teams(team_id),
    player_id   INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    assist_id   INTEGER REFERENCES players(player_id) ON DELETE SET NULL,
    elapsed     INTEGER,
    extra       INTEGER,        -- minutos anadidos
    type        TEXT,           -- Goal, Card, subst, Var
    detail      TEXT,           -- Normal Goal, Yellow Card, Substitution 1...
    comments    TEXT,
    -- Evita duplicar el mismo evento al recargar una temporada.
    UNIQUE (fixture_id, team_id, player_id, elapsed, extra, type, detail)
);

CREATE INDEX IF NOT EXISTS idx_events_fixture ON fixture_events (fixture_id);
CREATE INDEX IF NOT EXISTS idx_events_player  ON fixture_events (player_id);
CREATE INDEX IF NOT EXISTS idx_events_type    ON fixture_events (type, detail);


CREATE TABLE IF NOT EXISTS lineups (
    fixture_id  INTEGER NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    team_id     INTEGER NOT NULL REFERENCES teams(team_id),
    formation   TEXT,
    coach_id    INTEGER REFERENCES coaches(coach_id) ON DELETE SET NULL,
    PRIMARY KEY (fixture_id, team_id)
);

CREATE TABLE IF NOT EXISTS lineup_players (
    fixture_id  INTEGER NOT NULL,
    team_id     INTEGER NOT NULL,
    player_id   INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    shirt_number INTEGER,
    position    TEXT,           -- G, D, M, F
    grid        TEXT,           -- fila:columna en el 11 inicial
    is_starter  BOOLEAN NOT NULL,
    PRIMARY KEY (fixture_id, team_id, player_id),
    FOREIGN KEY (fixture_id, team_id) REFERENCES lineups(fixture_id, team_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_lineup_players_player ON lineup_players (player_id);


CREATE TABLE IF NOT EXISTS fixture_player_statistics (
    fixture_id          INTEGER NOT NULL REFERENCES fixtures(fixture_id) ON DELETE CASCADE,
    team_id             INTEGER NOT NULL REFERENCES teams(team_id),
    player_id           INTEGER NOT NULL REFERENCES players(player_id) ON DELETE CASCADE,
    minutes             INTEGER,
    shirt_number        INTEGER,
    position            TEXT,
    rating              NUMERIC(4,2),
    is_captain          BOOLEAN,
    is_substitute       BOOLEAN,
    offsides            INTEGER,
    shots_total         INTEGER,
    shots_on            INTEGER,
    goals_total         INTEGER,
    goals_conceded      INTEGER,
    assists             INTEGER,
    saves               INTEGER,
    passes_total        INTEGER,
    passes_key          INTEGER,
    passes_accuracy     INTEGER,
    tackles_total       INTEGER,
    tackles_blocks      INTEGER,
    tackles_interceptions INTEGER,
    duels_total         INTEGER,
    duels_won           INTEGER,
    dribbles_attempts   INTEGER,
    dribbles_success    INTEGER,
    dribbles_past       INTEGER,
    fouls_drawn         INTEGER,
    fouls_committed     INTEGER,
    cards_yellow        INTEGER,
    cards_red           INTEGER,
    penalty_won         INTEGER,
    penalty_committed   INTEGER,
    penalty_scored      INTEGER,
    penalty_missed      INTEGER,
    penalty_saved       INTEGER,
    PRIMARY KEY (fixture_id, player_id)
);

CREATE INDEX IF NOT EXISTS idx_fps_player ON fixture_player_statistics (player_id);
CREATE INDEX IF NOT EXISTS idx_fps_team   ON fixture_player_statistics (team_id);


CREATE TABLE IF NOT EXISTS standings (
    league_id   INTEGER NOT NULL,
    season      INTEGER NOT NULL,
    team_id     INTEGER NOT NULL REFERENCES teams(team_id),
    stage       TEXT,
    group_name  TEXT,
    rank        INTEGER,
    points      INTEGER,
    goals_diff  INTEGER,
    form        TEXT,
    status      TEXT,
    description TEXT,
    played      INTEGER, win INTEGER, draw INTEGER, lose INTEGER,
    goals_for   INTEGER, goals_against INTEGER,
    home_played INTEGER, home_win INTEGER, home_draw INTEGER, home_lose INTEGER,
    home_goals_for INTEGER, home_goals_against INTEGER,
    away_played INTEGER, away_win INTEGER, away_draw INTEGER, away_lose INTEGER,
    away_goals_for INTEGER, away_goals_against INTEGER,
    PRIMARY KEY (league_id, season, team_id, group_name),
    FOREIGN KEY (league_id, season) REFERENCES seasons(league_id, season) ON DELETE CASCADE
);


CREATE TABLE IF NOT EXISTS sync_runs (
    sync_id         BIGSERIAL PRIMARY KEY,
    league_id       INTEGER NOT NULL,
    season          INTEGER NOT NULL,
    resource        TEXT    NOT NULL,   -- fixtures, details, standings, teams
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    status          TEXT    NOT NULL,   -- running, completed, failed
    records_received INTEGER DEFAULT 0,
    requests_used   INTEGER DEFAULT 0,
    error_message   TEXT
);

CREATE INDEX IF NOT EXISTS idx_sync_lookup
    ON sync_runs (league_id, season, resource, status);

CREATE UNIQUE INDEX IF NOT EXISTS idx_sync_completed_unique
    ON sync_runs (league_id, season, resource)
    WHERE status = 'completed';

COMMIT;
