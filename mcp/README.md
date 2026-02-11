# Quern Debug MCP Server

MCP (Model Context Protocol) server that wraps the Quern Debug Server HTTP API, letting AI agents query iOS device logs, crash reports, and build results.

## Prerequisites

- The Python Quern Debug Server must be running (`quern-debug-server` on port 9100)
- Node.js 18+

## Usage

### With Claude Desktop

Add to your `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "quern-debug": {
      "command": "npx",
      "args": ["-y", "quern-debug-mcp"]
    }
  }
}
```

### With Cursor

Add to your MCP settings:

```json
{
  "quern-debug": {
    "command": "npx",
    "args": ["-y", "quern-debug-mcp"]
  }
}
```

### From Source

```bash
cd mcp
npm install
npm run build
node dist/index.js
```

## Configuration

| Environment Variable | Default | Description |
|---|---|---|
| `QUERN_DEBUG_SERVER_URL` | `http://127.0.0.1:9100` | Python server URL |

The API key is read automatically from `~/.quern/api-key`.

## Tools

| Tool | Description |
|---|---|
| `tail_logs` | Show recent log entries (most recent first) |
| `query_logs` | Full-featured log search with time ranges and text search |
| `get_log_summary` | AI-optimized summary with cursor-based delta polling |
| `get_errors` | Error-level entries and crash reports |
| `get_build_result` | Most recent parsed xcodebuild result |
| `get_latest_crash` | Recent crash reports with parsed details |
| `set_log_filter` | Reconfigure capture filters |
| `list_log_sources` | List active log source adapters |

## Resources

| URI | Description |
|---|---|
| `logserver://guide` | Tool selection guide and recommended workflows |
| `logserver://troubleshooting` | Common iOS error patterns and crash report reading guide |
