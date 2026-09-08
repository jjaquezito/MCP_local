# Football Intelligence MCP

A local **Model Context Protocol** server that gives any MCP host access to a
curated historical football database covering **36,982 matches** across
Europe's seven top competitions from 2010 to 2025.

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
git clone https://github.com/jjaquezito/MCP_local.git
cd MCP_local

python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### PostgreSQL

You need a **running** PostgreSQL server before `createdb`/`pg_restore` will
work — if you don't have one yet:

```bash
# macOS
brew install postgresql@14
brew services start postgresql@14        # keeps running across reboots

# Debian/Ubuntu
sudo apt install postgresql
sudo systemctl start postgresql
```

Then restore the database:

```bash
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

If `createdb`/`pg_restore`/`psql` fail with something like `connection
refused` or `role "postgres" does not exist`, PostgreSQL either isn't
installed or isn't running — that's almost always the actual problem, not
`football.dump` itself. Go back to the PostgreSQL step above.

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

### Adding it to your own chatbot

Your host is its own program, so there's no config file shortcut — it has to
speak JSON-RPC 2.0 over this process's stdin/stdout directly. The minimum
sequence, in any language:

1. **Spawn the server** with `DATABASE_URL` set in its environment, keeping
   stdin/stdout as pipes:
   ```python
   proc = subprocess.Popen(
       ["/absolute/path/to/.venv/bin/python", "/absolute/path/to/server.py"],
       stdin=subprocess.PIPE, stdout=subprocess.PIPE,
       env={**os.environ, "DATABASE_URL": "postgresql://..."},
       text=True, bufsize=1,
   )
   ```
2. **Handshake** — one JSON object per line, written to stdin / read from stdout:
   ```python
   send({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
       "protocolVersion": "2025-06-18", "capabilities": {},
       "clientInfo": {"name": "my-host", "version": "1.0"}}})
   recv()  # -> serverInfo + capabilities
   send({"jsonrpc": "2.0", "method": "notifications/initialized"})  # no "id": no reply comes back
   ```
3. **Fetch the catalog** and translate it to whatever your LLM's tool-call
   format expects. For Claude's Messages API, `inputSchema` maps straight to
   `input_schema` — nothing else about the shape changes:
   ```python
   send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
   tools = recv()["result"]["tools"]
   ```
4. **Call a tool** the same way, matching `id` to correlate request/response:
   ```python
   send({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
       "name": "search_team", "arguments": {"query": "Liverpool"}}})
   result = recv()["result"]  # {"content": [{"type": "text", "text": "..."}], "isError": bool}
   ```

That's the entire contract — see **Protocol** below for the full method and
error reference.

## Protocol

Transport: stdio, newline-delimited JSON. Protocol version `2025-06-18`.

| Method | Notes |
|---|---|
| `initialize` | Handshake. Returns `serverInfo` and capabilities. |
| `notifications/initialized` | Client notification. No `id`, no response. |
| `ping` | Liveness check. Returns `{}`. |
| `tools/list` | Returns the fifteen tool definitions. |
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
  "founded": 1892, "logo": "https://media.api-sports.io/football/teams/40.png",
  "partidos": 762, "primera_temporada": 2010, "ultima_temporada": 2025}]
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

### `get_recent_lineup`

Formation, coach (with photo) and starting XI of a team's **last** match that
has lineup data, each player including their `grid` position (`row:col`,
API-Football's own notation for drawing a formation) and their match
`rating` — enough to render an actual pitch diagram, not just a list. This
is a reasonable stand-in for "probable lineup" — it is not a confirmation of
who will actually start next; injuries and rotation can change it.
Formation/coach data only exists from 2015 onward, same as match statistics.

| Parameter | Type | Required |
|---|---|---|
| `team_id` | integer | yes |

### `get_team_squad`

Every player who appeared in a team's last N matches, with photo and
accumulated goals/assists/average rating, plus `jugador_mas_determinante`
(the player with the most goal involvements in that window) called out
separately.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `team_id` | integer | yes | — |
| `last` | integer | no | 10 |

### `get_league_info`

A competition's crest and flag. There are no trophy images in the database
— API-Football exposes those through a separate endpoint that was never
extracted — this is the closest available visual identity for a league.

| Parameter | Type | Required |
|---|---|---|
| `league_id` | integer | yes |

### `get_head_to_head`

Win/draw/loss balance between two teams, their most recent meetings, and
per-team averages (goals per match, yellow/red cards, fouls, corners) across
their full history — useful context for deciding a bet, not just a prediction
number. Cards/fouls/corners are only available for matches from 2015 onward;
matches without statistics are excluded from those averages, not counted as
zero. Also returns `equipo_a_id`/`equipo_a_logo` and `equipo_b_id`/
`equipo_b_logo` alongside the existing `equipo_a`/`equipo_b` name fields.

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
  "local": "Barcelona", "local_team_id": 529,
  "local_logo": "https://media.api-sports.io/football/teams/529.png",
  "visitante": "Athletic Club", "visitante_team_id": 531,
  "visitante_logo": "https://media.api-sports.io/football/teams/531.png",
  "probabilidad_local": 0.779, "probabilidad_empate": 0.145, "probabilidad_visitante": 0.076,
  "modelo": "logistic_regression",
  "advertencia": "Probabilidad estadistica basada en historial, no una garantia..."
}
```

`*_logo` are direct `media.api-sports.io` URLs, ready to use as an `<img src>` with no
proxying — a UI client doesn't need a separate lookup to render team crests.

The trained model ships in `data/predict_model.joblib` (a few KB — a fitted
scikit-learn pipeline, not raw weights). Retraining requires the full feature
pipeline (`conocimiento/ml/`), which lives in the private project repository,
not here — same relationship as `data/football.dump` to the extraction
pipeline that built it.

### `predict_scoreline`

Complements `predict_match` with a full scoreline probability grid — the
chance of every exact result from 0-0 up to `max_goals`-`max_goals` — using a
Poisson model over each team's recent goals scored/conceded, plus expected
yellow cards and corners for the match and the probability both teams score.
This is a different statistical method from `predict_match`'s trained
classifier, not the same number recomputed, so a small mismatch between the
two (e.g. draw probability) is expected and not a bug.

| Parameter | Type | Required | Default |
|---|---|---|---|
| `home_team_id` | integer | yes | — |
| `away_team_id` | integer | yes | — |
| `max_goals` | integer | no | 5 |

```json
{"name": "predict_scoreline", "arguments": {"home_team_id": 529, "away_team_id": 531}}
```

```json
{
  "local": "Barcelona", "visitante": "Athletic Club",
  "goles_esperados_local": 1.8, "goles_esperados_visitante": 1.3,
  "marcador_mas_probable": "1-1", "probabilidad_marcador_mas_probable": 10.54,
  "probabilidad_ambos_marcan": 59.8,
  "tarjetas_amarillas_esperadas": 3.5, "corners_esperados": 12.8,
  "matriz_marcadores": [[4.5, 5.9, 3.8, "..."], ["..."]]
}
```

`matriz_marcadores[i][j]` is the percentage chance of a `i`-`j` final score
(home goals first), so `matriz_marcadores[1][1]` is the chance of exactly
1-1. Cells beyond `max_goals` in either direction aren't included, so the
grid's total is slightly under 100% by design.

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
