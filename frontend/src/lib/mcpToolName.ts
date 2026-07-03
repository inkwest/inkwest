// Does an engine-reported tool name refer to MCP server `key`'s tool `tool`?
// Engines drift on the separator: claude-cli has been seen emitting BOTH
// mcp__<key>__<tool> and mcp_<key>_<tool> — accept either. Exact-string compares:
// the single-underscore form is ambiguous when the key/tool themselves contain
// underscores, so it's a best-effort fallback after the canonical double form.
export function matchesMcpToolName(name: string, key: string, tool: string): boolean {
  return name === `mcp__${key}__${tool}` || name === `mcp_${key}_${tool}`;
}
