# AI Tool Use Loop - Deep Dive

## Overview

The tool use loop is the core mechanism that allows Claude to interact with Kubernetes. This document covers the complete implementation including edge cases, error handling, streaming, and optimization.

## State Machine

```
                                    ┌─────────────────┐
                                    │                 │
                                    │   USER INPUT    │
                                    │                 │
                                    └────────┬────────┘
                                             │
                                             ▼
                              ┌──────────────────────────────┐
                              │  ✂ cancel_event check        │
                              │                              │
                              │    CALL CLAUDE API           │
                              │    (with tools + history)    │
                              │                              │
                              └──────────────┬───────────────┘
                                             │
                                             ▼
                              ┌──────────────────────────────┐
                              │  ✂ cancel_event check        │
                              │                              │
                              │    PROCESS RESPONSE          │
                              │    (stream text blocks)      │
                              │                              │
                              └──────────────┬───────────────┘
                                             │
                           ┌─────────────────┼─────────────────┐
                           │                 │                  │
                           ▼                 ▼                  ▼
              ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
              │  stop_reason:    │ │  stop_reason:    │ │  cancel_event    │
              │  "end_turn"      │ │  "tool_use"      │ │  is set          │
              └────────┬─────────┘ └────────┬─────────┘ └────────┬─────────┘
                       │                    │                    │
                       ▼                    ▼                    ▼
              ┌──────────────────┐ ┌──────────────────┐ ┌──────────────────┐
              │  SAVE & RETURN   │ │ ✂ cancel check   │ │  YIELD done      │
              │  (done)          │ │ EXECUTE TOOL(S)  │ │  (stopped=true)  │
              └──────────────────┘ │ (may be parallel)│ └──────────────────┘
                                   └────────┬─────────┘
                                            │
                                            ▼
                                   ┌──────────────────┐
                                   │ APPEND MESSAGES:  │
                                   │ - assistant msg   │
                                   │ - tool_result msg │
                                   └────────┬─────────┘
                                            │
                                   ┌────────┴─────────┐
                                   │ iteration < max?  │
                                   └────────┬─────────┘
                                            │
                           ┌────────────────┴────────────────┐
                           │ YES                             │ NO
                           ▼                                 ▼
              ┌──────────────────┐              ┌──────────────────┐
              │  LOOP BACK TO    │              │  FORCE END       │
              │  "CALL CLAUDE"   │              │  (safety limit)  │
              └──────────────────┘              └──────────────────┘
```

✂ = cancellation checkpoint. At each checkpoint, if `cancel_event.is_set()`, the orchestrator yields `done_event(stopped=True)` and returns immediately. Partial content already streamed to the frontend is preserved as-is.

## Message Format Deep Dive

### Claude API Message Structure

The messages array follows a strict format. Understanding this is crucial:

```python
messages = [
    # 1. Initial user message (simple string)
    {
        "role": "user",
        "content": "Why is my nginx pod crashing?"
    },

    # 2. Assistant response WITH tool use (array of content blocks)
    {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "I'll check the pod status to understand what's happening."
            },
            {
                "type": "tool_use",
                "id": "toolu_01ABC123",  # Claude generates this ID
                "name": "kubectl_get",
                "input": {
                    "resource": "pods",
                    "namespace": "default",
                    "selector": "app=nginx"
                }
            }
        ]
    },

    # 3. Tool result (MUST be role: "user" with tool_result content)
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_01ABC123",  # Must match the tool_use id
                "content": "NAME                     READY   STATUS             RESTARTS\nnginx-7d9b8c8b4-x2k3j   0/1     CrashLoopBackOff   5"
            }
        ]
    },

    # 4. Assistant asks for more info
    {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "The pod is in CrashLoopBackOff with 5 restarts. Let me check the logs from the previous crashed container."
            },
            {
                "type": "tool_use",
                "id": "toolu_02DEF456",
                "name": "kubectl_logs",
                "input": {
                    "pod_name": "nginx-7d9b8c8b4-x2k3j",
                    "namespace": "default",
                    "previous": True
                }
            }
        ]
    },

    # 5. Another tool result
    {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_02DEF456",
                "content": "2024/01/15 10:23:45 [emerg] 1#1: \"location\" directive is not allowed here in /etc/nginx/nginx.conf:42"
            }
        ]
    },

    # 6. Final answer (no tool_use, just text)
    {
        "role": "assistant",
        "content": [
            {
                "type": "text",
                "text": "I found the issue! Your nginx pod is crashing because..."
            }
        ]
    }
]
```

### Key Rules

1. **Tool results MUST have role: "user"** - This is counter-intuitive but required by the API
2. **tool_use_id must match** - The `tool_result.tool_use_id` must exactly match the `tool_use.id`
3. **Content can be string or array** - Simple messages can be strings, but tool-related messages use arrays
4. **Order matters** - Messages must alternate user/assistant (tool_result counts as user)

### Persisted block order == stream order

The block order saved in an assistant message's `content` array **must match the
causal order of the live stream** (steps then report, with text interleaved
between tool calls preserved). JSONB keeps array order, and the frontend renders
`content` by index — so the only way reload can differ from live is if the
backend reorders blocks at save time.

Build the saved `content` as a **single list appended in stream arrival order**:
coalesce consecutive text deltas into the trailing text block, push each
`tool_use` / `tool_result` as it arrives. Do **not** accumulate text and tools in
two separate lists and concatenate text-first — that flattens interleaving and
shows the report above the steps on reload (a past regression).

## Parallel Tool Execution

Claude can request multiple tools in a single response:

```python
# Claude's response might include multiple tool calls
{
    "role": "assistant",
    "content": [
        {
            "type": "text",
            "text": "Let me check both the pod status and recent events."
        },
        {
            "type": "tool_use",
            "id": "toolu_01_pods",
            "name": "kubectl_get",
            "input": {"resource": "pods", "namespace": "default"}
        },
        {
            "type": "tool_use",
            "id": "toolu_02_events",
            "name": "kubectl_events",
            "input": {"namespace": "default"}
        }
    ]
}
```

Handle this with parallel execution:

```python
import asyncio

async def execute_tools_parallel(
    tool_calls: list[dict],
    kubeconfig: str
) -> list[dict]:
    """Execute multiple tool calls in parallel."""

    async def execute_one(tool_call: dict) -> dict:
        result = await execute_tool(
            tool_call["name"],
            tool_call["input"],
            kubeconfig
        )
        return {
            "type": "tool_result",
            "tool_use_id": tool_call["id"],
            "content": result
        }

    # Execute all tools concurrently
    results = await asyncio.gather(
        *[execute_one(tc) for tc in tool_calls],
        return_exceptions=True
    )

    # Handle any exceptions
    processed_results = []
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            processed_results.append({
                "type": "tool_result",
                "tool_use_id": tool_calls[i]["id"],
                "content": f"Error: {str(result)}",
                "is_error": True
            })
        else:
            processed_results.append(result)

    return processed_results
```

## Error Handling

### Tool Execution Errors

When a kubectl command fails, return the error to Claude - it can often interpret and explain it:

```python
async def execute_tool_safe(
    tool_name: str,
    tool_input: dict,
    kubeconfig: str
) -> tuple[str, bool]:
    """
    Execute tool and return (result, is_error).
    Errors are returned as content, not raised as exceptions.
    """
    try:
        # Validate inputs first
        validation_error = validate_tool_input(tool_name, tool_input)
        if validation_error:
            return (f"Validation error: {validation_error}", True)

        # Execute the command
        result = await execute_kubectl(tool_name, tool_input, kubeconfig)
        return (result, False)

    except subprocess.TimeoutExpired:
        return ("Command timed out after 30 seconds. The cluster may be slow or unreachable.", True)

    except FileNotFoundError:
        return ("kubectl not found. Please ensure kubectl is installed.", True)

    except PermissionError:
        return ("Permission denied accessing kubeconfig.", True)

    except Exception as e:
        return (f"Unexpected error: {type(e).__name__}: {str(e)}", True)


# When sending back to Claude, mark errors:
tool_result = {
    "type": "tool_result",
    "tool_use_id": tool_call["id"],
    "content": result_text,
    "is_error": is_error  # Claude will see this flag
}
```

### API Errors

```python
from anthropic import APIError, RateLimitError, APIConnectionError

async def call_claude_with_retry(
    client: Anthropic,
    messages: list,
    max_retries: int = 3
) -> Message:
    """Call Claude API with exponential backoff retry."""

    for attempt in range(max_retries):
        try:
            return await client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=4096,
                messages=messages,
                tools=KUBECTL_TOOLS,
            )

        except RateLimitError as e:
            if attempt < max_retries - 1:
                wait_time = (2 ** attempt) + random.random()
                await asyncio.sleep(wait_time)
                continue
            raise

        except APIConnectionError as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1)
                continue
            raise

        except APIError as e:
            # Don't retry other API errors (bad request, etc.)
            raise
```

### Conversation Recovery

If the loop crashes mid-conversation, you need to recover:

```python
async def recover_conversation(session_id: str) -> list[dict]:
    """
    Load conversation and ensure it's in a valid state.
    Fix any incomplete tool calls.
    """
    messages = await load_session_messages(session_id)

    if not messages:
        return []

    last_message = messages[-1]

    # If last message is assistant with pending tool calls, we crashed during execution
    if last_message["role"] == "assistant":
        tool_calls = [
            block for block in last_message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "tool_use"
        ]

        if tool_calls:
            # Add error results for the incomplete tools
            error_results = [
                {
                    "type": "tool_result",
                    "tool_use_id": tc["id"],
                    "content": "Tool execution was interrupted. Please try again.",
                    "is_error": True
                }
                for tc in tool_calls
            ]
            messages.append({"role": "user", "content": error_results})

    return messages
```

## Streaming Implementation

For real-time UI updates, stream responses as they arrive:

```python
from anthropic import Anthropic

async def chat_with_streaming(
    session_id: str,
    user_message: str,
    kubeconfig: str
) -> AsyncIterator[ChatEvent]:
    """
    Full streaming implementation with tool use.
    """
    client = Anthropic()
    messages = await load_session_messages(session_id)
    messages.append({"role": "user", "content": user_message})

    iteration = 0
    max_iterations = 10

    while iteration < max_iterations:
        iteration += 1

        # Use streaming API
        collected_content = []
        current_text = ""
        tool_calls = []

        with client.messages.stream(
            model="claude-sonnet-4-20250514",
            max_tokens=4096,
            messages=messages,
            tools=KUBECTL_TOOLS,
        ) as stream:

            for event in stream:
                # Handle different event types
                if event.type == "content_block_start":
                    if event.content_block.type == "text":
                        current_text = ""
                    elif event.content_block.type == "tool_use":
                        tool_calls.append({
                            "id": event.content_block.id,
                            "name": event.content_block.name,
                            "input": {}
                        })

                elif event.type == "content_block_delta":
                    if hasattr(event.delta, "text"):
                        # Stream text chunks immediately
                        yield ChatEvent(type="text_delta", content=event.delta.text)
                        current_text += event.delta.text

                    elif hasattr(event.delta, "partial_json"):
                        # Tool input is streamed as partial JSON
                        # Accumulate until complete
                        pass

                elif event.type == "content_block_stop":
                    if current_text:
                        collected_content.append({
                            "type": "text",
                            "text": current_text
                        })

                elif event.type == "message_stop":
                    # Message complete
                    pass

            # Get the final message
            final_message = stream.get_final_message()

        # Check if we need to execute tools
        if final_message.stop_reason == "end_turn":
            messages.append({"role": "assistant", "content": final_message.content})
            await save_session_messages(session_id, messages)
            yield ChatEvent(type="done")
            return

        elif final_message.stop_reason == "tool_use":
            # Extract tool calls from final message
            tool_calls = [
                {
                    "id": block.id,
                    "name": block.name,
                    "input": block.input
                }
                for block in final_message.content
                if block.type == "tool_use"
            ]

            # Add assistant message to history
            messages.append({"role": "assistant", "content": final_message.content})

            # Execute tools
            tool_results = []
            for tool_call in tool_calls:
                yield ChatEvent(
                    type="tool_start",
                    tool_name=tool_call["name"],
                    tool_input=tool_call["input"]
                )

                result, is_error = await execute_tool_safe(
                    tool_call["name"],
                    tool_call["input"],
                    kubeconfig
                )

                yield ChatEvent(
                    type="tool_result",
                    tool_name=tool_call["name"],
                    content=result,
                    is_error=is_error
                )

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_call["id"],
                    "content": result,
                    "is_error": is_error
                })

            # Add results and continue loop
            messages.append({"role": "user", "content": tool_results})

    yield ChatEvent(type="error", content="Maximum iterations reached")
```

## Output Truncation

kubectl can return huge outputs. Handle this gracefully:

```python
MAX_TOOL_OUTPUT_CHARS = 50000  # ~12k tokens
TRUNCATION_MESSAGE = "\n\n[Output truncated - showing first {shown} of {total} characters]"

def truncate_output(output: str) -> str:
    """Truncate large outputs to avoid token limits."""
    if len(output) <= MAX_TOOL_OUTPUT_CHARS:
        return output

    truncated = output[:MAX_TOOL_OUTPUT_CHARS]

    # Try to truncate at a line boundary
    last_newline = truncated.rfind('\n')
    if last_newline > MAX_TOOL_OUTPUT_CHARS * 0.8:
        truncated = truncated[:last_newline]

    return truncated + TRUNCATION_MESSAGE.format(
        shown=len(truncated),
        total=len(output)
    )


# For structured output, be smarter:
def truncate_kubectl_output(output: str, resource_type: str) -> str:
    """Smart truncation based on resource type."""

    if len(output) <= MAX_TOOL_OUTPUT_CHARS:
        return output

    lines = output.split('\n')

    # For lists, keep header + first N items + count
    if resource_type in ["pods", "services", "deployments"]:
        header = lines[0] if lines else ""
        items = lines[1:]

        # Calculate how many items we can keep
        max_items = 100
        if len(items) > max_items:
            kept = items[:max_items]
            return (
                header + '\n' +
                '\n'.join(kept) +
                f"\n\n[Showing {max_items} of {len(items)} items]"
            )

    # For logs, keep most recent (tail)
    if resource_type == "logs":
        if len(output) > MAX_TOOL_OUTPUT_CHARS:
            return (
                "[Earlier logs truncated]\n\n" +
                output[-MAX_TOOL_OUTPUT_CHARS:]
            )

    return truncate_output(output)
```

## Token Management

Long conversations walk into the model's context window. Inkwest does not estimate tokens
locally (no tokenizer, no `count_tokens` call): the context occupancy is the prompt of the turn's
last model call as the engine reported it on the wire (fresh input + cache read + cache write),
carried as `context_tokens` on the terminal `done` when the engine reported any, and persisted in
`messages.context_tokens`. The
row's `input_tokens + cache_read_tokens + cache_write_tokens` sum every tool iteration of the turn
and overstate it. The frontend renders `context_tokens` of the last assistant row that has it
against the model's context window (`app/services/context_window.py`,
`frontend/src/lib/contextUsage.ts`); rows without it are skipped; no gauge until a row after the
last boundary has it.

Reclaiming context is **compaction** (`app/services/compaction.py`), not truncation:

1. The pre-boundary transcript is dumped to `/workdir/.inkwest/history-<n>.md` in the
   conversation's own workdir, or to a fresh `/workdir/.inkwest-<hex>/` when `.inkwest` is a
   symlink, a file or a directory this process cannot link into.
2. A normal, billed turn on the session's engine writes a summary
   (`prompts.get_compaction_prompt`). It replays the engine's usual message window, no larger,
   and the prompt points it at the segment's archive file for anything older.
3. A `role='system'` boundary row commits, carrying that summary.
4. `get_session_messages` then replays only the rows after the boundary, and the summary rides
   the assembled system prompt (fenced as untrusted, see `docs/SYSTEM_PROMPTS.md`).

`<n>` is one past the highest archive on disk, so deleting a boundary row to undo a compaction
never lets the next one overwrite its archive. A compaction with nothing after the last boundary
is refused by the WS route before any turn starts. With a sandbox, one whose archive could not be written
(even into a fresh `.inkwest-<hex>/` it creates when `.inkwest` is a plant or unwritable) is refused inside the turn before the engine runs (no tokens, no rows, no abort marker), and an engine with no transcript to summarize
(claude-cli without a `cli_session_id`, opencode without its store) stops before it spawns. A
compaction that ends without a boundary rolls back its rows and archive and leaves no "turn
aborted" marker. Engine state (the CLI resume id, the opencode store) changes only after the
boundary commits, the resume id in the same transaction. opencode records the boundary each run
ran under (`<session_dir>/opencode.boundary`, outside the sandbox mounts); a store written under an
older boundary is reset again and never resumed. A boundary commit that raises after the server
applied it is detected and kept.

Nothing is deleted: Postgres keeps every row, the UI renders the whole thread with a divider,
and the model recalls details by grepping the archive with the file tools it already has. See
`specs/006-context-pressure.md`.

## Security: Input Validation

Never trust tool inputs - validate everything:

```python
import re
from typing import Optional

ALLOWED_RESOURCES = {
    "pods", "services", "deployments", "replicasets", "statefulsets",
    "daemonsets", "jobs", "cronjobs", "configmaps", "secrets",
    "persistentvolumeclaims", "persistentvolumes", "nodes",
    "namespaces", "events", "ingresses", "networkpolicies",
    "serviceaccounts", "roles", "rolebindings", "clusterroles",
    "clusterrolebindings", "storageclasses", "endpoints",
}

NAMESPACE_PATTERN = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?$')
NAME_PATTERN = re.compile(r'^[a-z0-9]([-a-z0-9]*[a-z0-9])?(\.[a-z0-9]([-a-z0-9]*[a-z0-9])?)*$')
LABEL_PATTERN = re.compile(r'^[a-zA-Z0-9]([-_.a-zA-Z0-9]*[a-zA-Z0-9])?=[a-zA-Z0-9]([-_.a-zA-Z0-9]*[a-zA-Z0-9])?$')


def validate_tool_input(tool_name: str, tool_input: dict) -> Optional[str]:
    """
    Validate tool input. Returns error message or None if valid.
    """

    # Validate resource type
    resource = tool_input.get("resource", "").lower()
    if resource and resource not in ALLOWED_RESOURCES:
        # Allow shortnames too
        if resource not in ["po", "svc", "deploy", "rs", "sts", "ds", "cm", "pvc", "pv", "ns", "ep", "ing", "sa"]:
            return f"Unknown resource type: {resource}"

    # Validate namespace
    namespace = tool_input.get("namespace", "")
    if namespace and not NAMESPACE_PATTERN.match(namespace):
        return f"Invalid namespace format: {namespace}"

    # Validate resource name
    name = tool_input.get("name", "") or tool_input.get("pod_name", "")
    if name and not NAME_PATTERN.match(name):
        return f"Invalid resource name format: {name}"

    # Validate selector (prevent injection)
    selector = tool_input.get("selector", "")
    if selector:
        # Selectors can have multiple key=value pairs separated by comma
        for part in selector.split(","):
            part = part.strip()
            if part and not LABEL_PATTERN.match(part):
                return f"Invalid label selector format: {part}"

    # Validate tail lines
    tail = tool_input.get("tail")
    if tail is not None:
        if not isinstance(tail, int) or tail < 1 or tail > 10000:
            return f"Tail must be between 1 and 10000"

    return None  # Valid


def build_kubectl_command(tool_name: str, tool_input: dict, kubeconfig: str) -> list[str]:
    """
    Build kubectl command as a list (not a string!) to prevent shell injection.
    """
    # NEVER use shell=True or string concatenation

    cmd = ["kubectl", "--kubeconfig", kubeconfig]

    if tool_name == "kubectl_get":
        cmd.append("get")
        cmd.append(tool_input["resource"])

        if tool_input.get("name"):
            cmd.append(tool_input["name"])

        if tool_input.get("namespace"):
            cmd.extend(["-n", tool_input["namespace"]])
        elif not tool_input.get("all_namespaces"):
            cmd.extend(["-n", "default"])

        if tool_input.get("all_namespaces"):
            cmd.append("-A")

        if tool_input.get("selector"):
            cmd.extend(["-l", tool_input["selector"]])

        if tool_input.get("output"):
            cmd.extend(["-o", tool_input["output"]])

    elif tool_name == "kubectl_logs":
        cmd.append("logs")
        cmd.append(tool_input["pod_name"])

        if tool_input.get("namespace"):
            cmd.extend(["-n", tool_input["namespace"]])

        if tool_input.get("container"):
            cmd.extend(["-c", tool_input["container"]])

        if tool_input.get("tail"):
            cmd.extend(["--tail", str(tool_input["tail"])])

        if tool_input.get("previous"):
            cmd.append("--previous")

    # ... other tools

    return cmd
```

## Complete WebSocket Handler

The WS handler uses a dual-task pattern so `stop` messages can arrive during AI streaming:

```python
from fastapi import WebSocket, WebSocketDisconnect
import asyncio

@app.websocket("/ws/chat/{session_id}")
async def websocket_chat(websocket: WebSocket, session_id: str):
    await websocket.accept()
    # ... validate auth and session ...

    queue: asyncio.Queue[dict] = asyncio.Queue()
    cancel_event: asyncio.Event | None = None

    async def receive_loop():
        """Always listening — handles stop/ping, queues messages."""
        nonlocal cancel_event
        while True:
            data = await websocket.receive_json()
            msg_type = data.get("type")
            if msg_type == "stop":
                if cancel_event:
                    cancel_event.set()
            elif msg_type == "ping":
                await websocket.send_json({"type": "pong"})
            else:
                await queue.put(data)

    async def process_loop():
        """Processes queued messages with cancellable orchestration."""
        nonlocal cancel_event
        while True:
            data = await queue.get()
            if data.get("type") == "message":
                cancel_event = asyncio.Event()
                async for event in orchestrate(
                    ..., cancel_event=cancel_event
                ):
                    await websocket.send_json(event.to_dict())
                cancel_event = None

    receive_task = asyncio.create_task(receive_loop())
    process_task = asyncio.create_task(process_loop())
    await asyncio.wait(
        {receive_task, process_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
```

## Frontend Event Handling

```typescript
interface ChatEvent {
  type: 'text_delta' | 'tool_start' | 'tool_result' | 'done' | 'error' | 'pong';
  content?: string;
  tool_name?: string;
  tool_input?: Record<string, unknown>;
  is_error?: boolean;
  metadata?: Record<string, unknown>;
}

interface SendMessage {
  type: 'message' | 'ping' | 'stop';
  content?: string;
}
```

The `ChatWebSocket` class provides `sendMessage(content)` and `sendStop()` methods. The `useWebSocket` hook handles events:

- `done` with `metadata.stopped === true`: appends "*Response interrupted by user.*" to the conversation and marks in-progress/pending todos as `interrupted`
- The stop button (red square) replaces the send button during streaming
- The textarea remains enabled during streaming so the user can type ahead

## Summary

The tool use loop is:

1. **Stateless for Claude** - You rebuild context each turn from your database
2. **Stateful for your app** - You persist messages and manage the loop
3. **Streaming** - For good UX, stream both text and tool events
4. **Cancellable** - `cancel_event` checked at loop boundaries; yields `done(stopped=True)` on cancel
5. **Resilient** - Handle errors gracefully, Claude can interpret error messages
6. **Secure** - Validate all inputs, use argument lists not shell strings
7. **Bounded** - Always have a max iteration limit
