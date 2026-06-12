// Format a free-text argument hint ("url, ticket") as bracketed tokens
// ("[url] [ticket]"), matching Claude Code's argument-hint convention.
export function formatArgs(hint: string): string {
  return hint
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean)
    .map((s) => `[${s}]`)
    .join(' ');
}
