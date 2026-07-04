# Bedrock wire fixtures (redacted)

Real upstream bytes captured from the LLM broker on 2026-07-03, then **redacted**: every
model-generated content string (`text`, `thinking`, `partial_json`, tool-call `input` values,
tool `name` server/vendor segments) is replaced with `REDACTED` / `mcp__redacted__redacted`,
while **all token/usage/metrics numbers are preserved byte-exact** (verified equal to the
verbatim source). Eventstream frames are re-encoded with recomputed CRC32; untouched frames
are emitted verbatim.

Verbatim originals + the redaction/inspection scripts live in `misc/bedrock-fixtures-verbatim/`
(gitignored). Re-run: `backend/.venv/bin/python misc/bedrock-fixtures-verbatim/redact_fixtures.py`.

## Files

| File | Op | Key usage (what the P1 tests assert) |
|---|---|---|
| `invoke_stream_nocache.bin` | `invoke-with-response-stream` | `invocationMetrics: {inputTokenCount:365, outputTokenCount:17}` — **no cache keys** (present only when non-zero) |
| `invoke_stream_cachewrite.bin` | `invoke-with-response-stream` | `invocationMetrics: {inputTokenCount:10, outputTokenCount:349, cacheReadInputTokenCount:0, cacheWriteInputTokenCount:26797}`; Anthropic `usage` also carries TTL split `cache_creation.ephemeral_5m:26797/1h:0` |
| `invoke_stream_cacheread.bin` | `invoke-with-response-stream` | 2nd-turn cache hit — `invocationMetrics: {inputTokenCount:10, outputTokenCount:885, cacheReadInputTokenCount:27082, cacheWriteInputTokenCount:3242}` (**both non-zero** → exercises the full 4-way split) |
| `converse_stream_nocache.bin` | `converse-stream` | terminal `metadata usage: {inputTokens:6472, outputTokens:4, totalTokens:6476}` — no cache keys |
| `converse_nonstream_cachekeys.bin` | `converse` (JSON) | agent turn — `usage` carries BOTH `cacheReadInputTokenCount` AND `cacheReadInputTokens` (both 0); no `cacheWrite*` |
| `converse_nonstream_cachewrite.bin` | `converse` (JSON) | `usage: {cacheWriteInputTokens:12608, cacheReadInputTokens:0, cacheDetails:[{ttl:"5m",inputTokens:12608}], totalTokens:12631}` — captured via boto3 with a `cachePoint` (SDK-parsed body, real values) |
| `converse_nonstream_cacheread.bin` | `converse` (JSON) | 2nd-turn hit — `usage: {cacheReadInputTokens:12608, cacheWriteInputTokens:0}` |
| `invoke_nonstream.bin` | `invoke` (JSON) | Anthropic body `usage` block |

Note the field-name variance: the agent Converse path emits `cacheReadInputTokenCount` **and** `cacheReadInputTokens`; the boto3 Claude path emits only `cacheReadInputTokens`/`cacheWriteInputTokens`. Parser must accept both. `totalTokens` **includes** cache tokens — don't derive cost from it.

## Unobserved — capture, do NOT fabricate

Converse cache write/read + `cacheDetails` field names were unknown until captured live via boto3
(`cachePoint` probe, 2026-07-04) — my initial guesses were wrong (the unsupported error is
`AccessDeniedException`, not `ValidationException`; Converse *does* carry a TTL split). Lesson:
capture, don't guess.

Still unobserved (capture, don't fabricate):

- **1-hour-TTL cache** — only the `ttl:"5m"` `cacheDetails` value was seen; a `ttl:"1h"` turn needs
  a real 1h-TTL `cachePoint`. Parser reads whatever `ttl` string is present; the 1h *value* is
  untested.
