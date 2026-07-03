import { describe, it, expect } from 'vitest';
import { matchesMcpToolName } from './mcpToolName';

describe('matchesMcpToolName', () => {
  it('matches the canonical double-underscore form', () => {
    expect(matchesMcpToolName('mcp__notion__search', 'notion', 'search')).toBe(true);
  });

  it('matches the single-underscore engine variant', () => {
    expect(matchesMcpToolName('mcp_notion_search', 'notion', 'search')).toBe(true);
  });

  it('handles keys/tools that themselves contain underscores or dashes', () => {
    expect(matchesMcpToolName('mcp__mcp-auth-kc__list_users', 'mcp-auth-kc', 'list_users')).toBe(true);
    expect(matchesMcpToolName('mcp_mcp-auth-kc_list_users', 'mcp-auth-kc', 'list_users')).toBe(true);
  });

  it('rejects other servers, tools, and non-MCP names', () => {
    expect(matchesMcpToolName('mcp__other__search', 'notion', 'search')).toBe(false);
    expect(matchesMcpToolName('mcp__notion__write', 'notion', 'search')).toBe(false);
    expect(matchesMcpToolName('Bash', 'notion', 'search')).toBe(false);
  });
});
