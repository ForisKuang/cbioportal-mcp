# cBioPortal MCP Server

> **WARNING ⚠️: This is still under construction**

A wrapper around the [mcp-clickhouse server](https://github.com/ClickHouse/mcp-clickhouse) adding a [cBioPortal-specific system prompt](https://github.com/cBioPortal/cbioportal-mcp/blob/main/src/cbioportal_mcp/prompts/cbioportal_prompt.py).

## Installation

```bash
# Navigate to the project directory
cd cbioportal-mcp

# Create a virtual environment
python -m venv venv

# Activate the virtual environment
# On macOS/Linux:
source venv/bin/activate
# On Windows:
# venv\Scripts\activate

# Upgrade pip
pip install --upgrade pip

# Install the package in development mode
pip install -e .

# Or install with development dependencies
pip install -e "."
```

## Configuration

Set the same environment variables used by mcp-clickhouse:

```bash
export CLICKHOUSE_HOST=your-clickhouse-host
export CLICKHOUSE_PORT=9000
export CLICKHOUSE_USER=your-username
export CLICKHOUSE_PASSWORD=your-password
export CLICKHOUSE_DATABASE=your-cbioportal-database  # see "Preparing the database" below
export CLICKHOUSE_SECURE=true  # or false for insecure connections
export CLICKHOUSE_MCP_SERVER_TRANSPORT=stdio # or http or sse
# Optional: mount the HTTP endpoint under a sub-path (default: /mcp).
# Set when reverse-proxied behind a prefix so trailing-slash redirects
# include it, e.g. /db/mcp when served at https://host/db/mcp.
# export CLICKHOUSE_MCP_HTTP_PATH=/db/mcp
```

### Query Limits & Latency Controls

The server reuses a single thread-safe ClickHouse HTTP client (`autogenerate_session_id=False`),
executes ClickHouse queries off the asyncio event loop so concurrent MCP requests run in parallel,
and enforces both server-side and client-side guardrails configurable via environment variables:

| Environment Variable | Default | Purpose |
|---|---|---|
| `CBIOPORTAL_MCP_MAX_EXECUTION_TIME` | `25` | ClickHouse server-side `max_execution_time` (seconds). Kept below the client timeout so ClickHouse cancels slow queries server-side instead of letting them run until `send_receive_timeout`. Set to `0` to disable. |
| `CBIOPORTAL_MCP_QUERY_TIMEOUT` | `30` | Client-side query timeout (seconds); also used as the default ClickHouse HTTP `send_receive_timeout` when `CLICKHOUSE_SEND_RECEIVE_TIMEOUT` is unset. |
| `CBIOPORTAL_MCP_MAX_RESULT_ROWS` | `10000` | ClickHouse server-side `max_result_rows` sent with `result_overflow_mode='break'`. Set to `0` to disable. |
| `CBIOPORTAL_MCP_MAX_RESULT_BYTES` | `50000000` | ClickHouse server-side `max_result_bytes` (~50 MB) sent with `result_overflow_mode='break'`. Set to `0` to disable. |
| `CBIOPORTAL_MCP_MAX_ROWS` | `1000` | Maximum rows returned to the LLM by `clickhouse_run_select_query`. Exceeding results are capped and include `"truncated": true` plus a guidance `"message"`. Set to `0` to disable client-side row capping. |
| `CBIOPORTAL_MCP_LLMOBS_MAX_OUTPUT_CHARS` | `16384` | Maximum characters of tool output attached to Datadog LLMObs spans. Oversized outputs are truncated without re-serializing the full JSON payload on the event loop. Set to `0` to omit output capture. |

> **ClickHouse `readonly` profile note:** Per-query ClickHouse limits and `log_comment` require a
> ClickHouse user with `readonly = 2` (read-only queries, writable per-query settings). If the user
> has `readonly = 1`, readonly server settings are omitted with a one-time warning while `query_id`
> is still sent for `system.query_log` correlation.

### Datadog Tool Metrics

The server emits OpenTelemetry spans (`mcp.tool/<tool>`, `db.query/<query_label>`, and
`db.client.setup`) and attaches `query_id` + structured JSON `log_comment` (`service`,
`query_label`, `query_id`, `tool`, `trace_id`, `span_id`) to every ClickHouse query so
`system.query_log` entries can be joined directly to Datadog traces. It can also emit
DogStatsD metrics for dashboard-level aggregates:

| Metric | Type | Purpose |
|---|---|---|
| `cbioportal_mcp.tool.calls` | counter | Tool-call volume by `tool`, `success`, `client_kind`, and `client_name` |
| `cbioportal_mcp.tool.duration_ms` | distribution | Tool latency, including p50/p95/p99 by tool |
| `cbioportal_mcp.tool.errors` | counter | Tool-call failures by tool/client |
| `cbioportal_mcp.db_query.calls` | counter | ClickHouse query volume by `query_label` and `success` |
| `cbioportal_mcp.db_query.duration_ms` | distribution | ClickHouse query execution latency by `query_label` (excludes client setup and Python dict conversion) |
| `cbioportal_mcp.db_query.errors` | counter | ClickHouse query failures by `query_label` |
| `cbioportal_mcp.db_client.connects` | counter | ClickHouse client initialization/reconnect count by `success` |
| `cbioportal_mcp.db_client.setup_duration_ms` | distribution | ClickHouse client setup/handshake latency, separated from `db_query.duration_ms` |
| `cbioportal_mcp.db_client.errors` | counter | ClickHouse client setup failures |

DogStatsD metrics are enabled by default when `DD_AGENT_HOST` or
`DD_DOGSTATSD_HOST` is configured:

```bash
export DD_AGENT_HOST=<datadog-agent-host>
# Optional overrides:
export DD_DOGSTATSD_HOST=<dogstatsd-host>
export DD_DOGSTATSD_PORT=8125
export DD_SERVICE=cbioportal-mcp
export DD_ENV=prod
export CBIOPORTAL_MCP_DD_METRICS_ENABLED=true
export CBIOPORTAL_MCP_DD_METRIC_PREFIX=cbioportal_mcp
```

Set `CBIOPORTAL_MCP_DD_METRICS_ENABLED=false` to disable DogStatsD metrics.
The checked-in dashboard definition at
[`datadog/cbioagent-tool-metrics-dashboard.json`](datadog/cbioagent-tool-metrics-dashboard.json)
can be imported into Datadog or used as the source for updating the existing
cBioAgent dashboard.

## Preparing the database

**We strongly recommend pointing the MCP at a *separate* ClickHouse database, not your production cBioPortal database directly.** Two reasons:

1. **LLM-friendly fixes are destructive.** The agent works much better against a schema that's been cleaned up (misleading columns dropped, column comments added, OncoTree fields denormalized, named cohorts materialized). Applying those changes to your production database would interfere with the cBioPortal application.
2. **Isolation.** A separate database with a read-only user (`SELECT`-only) means agent traffic — including pathological queries — can't degrade production performance or accidentally expose data your portal users shouldn't see.

The recommended pattern is a periodic clone job: copy your production cBioPortal database into a separate ClickHouse database, then apply the SQL files in [`sql/`](sql/) — these add column comments, drop misleading columns, denormalize OncoTree, and materialize the `cancer_study_query_preferences` table the agent uses for cohort lookups. Point the MCP at this cloned-and-prepped database. See [`sql/README.md`](sql/README.md) for the full schema-prep contract and how to add deployment-specific preferences.

To apply the SQL files manually (e.g. for ad-hoc testing), use the helper script:

```bash
export CLICKHOUSE_HOST=... CLICKHOUSE_DATABASE=your-prepped-db
export CLICKHOUSE_ADMIN_USER=...  CLICKHOUSE_ADMIN_PASSWORD=...
./scripts/apply_sql.sh
```

Note the deliberately separate `CLICKHOUSE_ADMIN_*` env vars — admin credentials with DDL rights are kept out of the MCP server's runtime environment (which only ever needs `SELECT`).

For an end-to-end reference deployment (Kubernetes CronJob that handles the clone + SQL apply + atomic pointer-flip), see the cBioPortal team's daily clone CronJob in [knowledgesystems-k8s-deployment](https://github.com/knowledgesystems/knowledgesystems-k8s-deployment).

## Development

### Inspecting the Server with MCP Inspector

To connect to the MCP server and see requests and replies, use MCP Inspector.
You can run it with:
```bash
fastmcp dev inspector src/cbioportal_mcp/server.py
```

### Running the Server
```bash
# For development
python -m cbioportal_mcp.server

# Or using the installed script
cbioportal-mcp
```

### Running in Docker
```bash
# Build the image
docker build -t cbioportal-mcp -f docker/Dockerfile .
docker run -i -p 8000:8000 cbioportal-mcp
```

## License

MIT License - see LICENSE file for details.

## Related Projects

- [cBioPortal](https://github.com/cBioPortal/cbioportal) - The main cBioPortal platform
- [mcp-clickhouse](https://github.com/ClickHouse/mcp-clickhouse) - ClickHouse MCP server
