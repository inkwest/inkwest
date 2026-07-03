// Agentic engine ids (mirror backend app.services.engines) + their display names.
// Shared by the chat Engine·Model picker and the admin model-settings page.
export const CLAUDE_CLI = 'claude-cli';
export const INKWEST_AGENT = 'inkwest-agent';
export const OPENCODE = 'opencode';

const ENGINE_LABELS: Record<string, string> = {
  [CLAUDE_CLI]: 'Claude Code',
  [INKWEST_AGENT]: 'Inkwest Agent',
  [OPENCODE]: 'opencode',
};

// Friendly name for an engine id; falls back to the raw id so a future engine still
// renders something sane without a code change here.
export const engineLabel = (engine: string): string => ENGINE_LABELS[engine] ?? engine;
