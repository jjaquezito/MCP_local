# Football Intelligence MCP

A local **Model Context Protocol** server that gives any MCP host — Claude
Desktop, Claude Code, or a custom chatbot — access to a curated historical
football database covering **36,982 matches** across Europe's seven top
competitions from 2010 to 2025.

The protocol is implemented **directly over JSON-RPC 2.0, without using any
MCP SDK**: every message is built and parsed by hand following the
[2025-06-18 specification](https://modelcontextprotocol.io/specification/2025-06-18).

## What's in the database

Data originally extracted from [API-Football](https://www.api-football.com/),
normalized into PostgreSQL.

| Competition | League id | Seasons |
|---|---|---|
| Premier League | 39 | 2010–2025 |
| La Liga | 140 | 2010–2025 |
| Serie A | 135 | 2010–2025 |
| Bundesliga | 78 | 2010–2025 |
| Ligue 1 | 61 | 2010–2025 |
| Primeira Liga | 94 | 2010–2025 |
| UEFA Champions League | 2 | 2011–2025 |

| Table | Rows |
|---|---:|
| `fixtures` | 36,982 |
| `fixture_events` | 476,186 |
| `fixture_player_statistics` | 881,739 |
| `lineup_players` | 1,436,165 |
| `fixture_team_statistics` | 51,822 |
| `players` | 26,996 |
| `standings` | 2,329 |

### Known coverage limits

The server reports these instead of guessing, and so should you:

- **Match statistics start in 2015.** Seasons 2010–2014 contain results,
  goals, events and lineups, but no shots, possession or passing data.
- **Expected goals (xG) only exists from 2023** onward, and never for the
  Champions League.
- **Formation and coach data starts in 2015.**
- 58 matches (0.16%) have a goal missing from their event list — an upstream
  gap in API-Football. Scorelines are unaffected; they come from the fixture
  record, not from summing events.

Call the `data_coverage` tool to check what exists for any league and season.

## Installation

Requires **Python 3.10+** and **PostgreSQL 14+**.

```bash
git clone https://github.com/jaq23369/football-intelligence-mcp.git
cd football-intelligence-mcp

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

createdb football
pg_restore -d football data/football.dump

cp .env.example .env      # edit DATABASE_URL if your setup differs
```

Verify the restore:

```bash
psql -d football -c "SELECT count(*) FROM fixtures;"
#  count
# -------
#  36982
```

## Running the server

```bash
python server.py
```

The server speaks JSON-RPC over **stdio**. It is normally launched by an MCP
host rather than by hand, but you can drive it directly:

```bash
printf '%s\n' \
  '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"manual","version":"1.0"}}}' \
  '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' \
  | python server.py
```

### Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "football": {
      "command": "/absolute/path/to/football-intelligence-mcp/.venv/bin/python",
      "args": ["/absolute/path/to/football-intelligence-mcp/server.py"]
    }
  }
}
```

### Claude Code

```bash
claude mcp add football -- /absolute/path/to/.venv/bin/python /absolute/path/to/server.py
```

## Protocol

Transport: stdio, newline-delimited JSON. Protocol version `2025-06-18`.

| Method | Notes |
|---|---|
| `initialize` | Handshake. Returns `serverInfo` and capabilities. |
| `notifications/initialized` | Client notification. No `id`, no response. |
| `ping` | Liveness check. Returns `{}`. |
| `tools/list` | Returns the eleven tool definitions. |
| `tools/call` | Executes a tool. |

Errors follow JSON-RPC 2.0: `-32700` parse error, `-32600` invalid request,
`-32601` method not found, `-32602` invalid params, `-32603` internal error.

Tool-level failures are **not** JSON-RPC errors — they return a normal result
with `isError: true`, so the model can read the message and recover.

**stdout carries only JSON-RPC.** All diagnostics go to stderr.

## Tools

### `search_team`

Finds teams by partial name, ranked by how many matches they have on record.
Call this first — every other team tool needs a `team_id`.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `query` | string | yes | — |
| `limit` | integer | no | 10 |

```json
{"name": "search_team", "arguments": {"query": "Liverpool", "limit": 1}}
```

```json
[{"team_id": 40, "name": "Liverpool", "country": "England",
  "founded": 1892, "partidos": 762,
  "primera_temporada": 2010, "ultima_temporada": 2025}]
```

### `search_player`

Finds players by name, ranked by minutes played.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `query` | string | yes | — |
| `limit` | integer | no | 10 |

### `get_match`

Full match record: score, venue, referee, per-team statistics and every goal
with minute and assist. Matches before 2015 return an explicit note instead of
empty statistics.

| Parameter | Type | Required |
|---|---|---|
| `fixture_id` | integer | yes |

### `get_team_form`

Recent form: streak, points, goals for and against.

`before` restricts the calculation to matches strictly earlier than that date,
which lets you reconstruct a team's state at any past moment. This is the
guard against look-ahead bias when building predictive features.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `team_id` | integer | yes | — |
| `last` | integer | no | 5 |
| `before` | string (`YYYY-MM-DD`) | no | — |

```json
{"name": "get_team_form",
 "arguments": {"team_id": 40, "last": 5, "before": "2020-01-01"}}
```

### `get_head_to_head`

Win/draw/loss balance between two teams, their most recent meetings, and
per-team averages (goals per match, yellow/red cards, fouls, corners) across
their full history — useful context for deciding a bet, not just a prediction
number. Cards/fouls/corners are only available for matches from 2015 onward;
matches without statistics are excluded from those averages, not counted as
zero.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `team_a` | integer | yes | — |
| `team_b` | integer | yes | — |
| `limit` | integer | no | 10 |

### `get_team_season`

Final league position, points and goals, alongside per-match averages for
shots, possession, corners and passing accuracy.

| Parameter | Type | Required |
|---|---|---|
| `team_id` | integer | yes |
| `league_id` | integer | yes |
| `season` | integer | yes |

Seasons are named by their starting year: `2024` means the 2024-25 season.

### `get_player_season`

Per-season player totals aggregated from match-level records: goals, assists,
minutes, shots, key passes, cards and average rating.

| Parameter | Type | Required |
|---|---|---|
| `player_id` | integer | yes |
| `season` | integer | yes |

### `compare_teams`

Recent form for two teams plus their head-to-head record, in one call.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `team_a` | integer | yes | — |
| `team_b` | integer | yes | — |
| `last` | integer | no | 10 |

### `compare_players`

Two players' totals for the same season, side by side.

| Parameter | Type | Required |
|---|---|---|
| `player_a` | integer | yes |
| `player_b` | integer | yes |
| `season` | integer | yes |

### `data_coverage`

What actually exists in the database, per league and season. Use it before
claiming a data point is missing.

| Parameter | Type | Required |
|---|---|---|
| `league_id` | integer | no |
| `season` | integer | no |

### `predict_match`

Win/draw/loss probability for a match, from a model trained on 23,168 matches
(2015–2025, six domestic leagues — Champions League excluded, its knockout
format isn't comparable to a round-robin table). Two candidates were compared
head to head on a held-out validation season (logistic regression vs. a
gradient-boosted trees classifier); the one with the better validation log
loss was kept. See `model.metricas_prueba_2025` in the tool's own response
for the honest, never-touched-during-selection test score.

The match does **not** need to already exist in the database. Each team's
current Elo, recent form and rest days are kept in a `team_current_form`
snapshot, refreshed independently of any single fixture — so this works for
a match scheduled for next week just as well as one played five years ago.

| Parameter | Type | Required |
|---|---|---|
| `home_team_id` | integer | yes |
| `away_team_id` | integer | yes |

```json
{"name": "predict_match", "arguments": {"home_team_id": 529, "away_team_id": 531}}
```

```json
{
  "local": "Barcelona", "visitante": "Athletic Club",
  "probabilidad_local": 0.779, "probabilidad_empate": 0.145, "probabilidad_visitante": 0.076,
  "modelo": "logistic_regression",
  "advertencia": "Probabilidad estadistica basada en historial, no una garantia..."
}
```

The trained model ships in `data/predict_model.joblib` (a few KB — a fitted
scikit-learn pipeline, not raw weights). Retraining requires the full feature
pipeline (`conocimiento/ml/`), which lives in the private project repository,
not here — same relationship as `data/football.dump` to the extraction
pipeline that built it.

## Example session

> **Which team won the Premier League in 2015?**

```
→ search_team {"query": "Leicester"}
→ get_team_season {"team_id": 46, "league_id": 39, "season": 2015}
```

> Leicester City, with 81 points from 23 wins, 12 draws and 3 losses — and
> only 42.7% average possession, unusual for a champion.

## Architecture

```
MCP host  ──JSON-RPC/stdio──>  server.py  ──>  knowledge/engine.py  ──>  PostgreSQL
```

`server.py` owns the protocol and nothing else. All query logic lives in
`knowledge/engine.py`, which returns plain dictionaries and has no knowledge of
MCP — so it can be tested, or reused, entirely on its own.

## License

MIT. Football data originates from API-Football and is redistributed here for
academic use.

---

Built for CC3067 Redes, Universidad del Valle de Guatemala.
