#!/usr/bin/env node

/**
 * iOS Debug Server — MCP Server
 *
 * Thin wrapper that translates MCP tool calls into HTTP requests
 * to the Python log server running on localhost:9100.
 *
 * TODO: Implement in Phase 1c
 *
 * Tools to expose:
 * - tail_logs        → GET /api/v1/logs/stream
 * - query_logs       → GET /api/v1/logs/query
 * - get_log_summary  → GET /api/v1/logs/summary
 * - get_errors       → GET /api/v1/logs/errors
 * - get_build_result → GET /api/v1/builds/latest
 * - get_latest_crash → GET /api/v1/crashes/latest
 * - set_log_filter   → POST /api/v1/logs/filter
 * - list_log_sources → GET /api/v1/logs/sources
 */

console.error("MCP server not yet implemented. Coming in Phase 1c.");
process.exit(1);
