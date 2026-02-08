#!/usr/bin/env python3
"""
Quick smoke test for the Inkwest chat backend.

Connects to the REST API, picks the first cluster, creates a session,
then sends a message over WebSocket and prints streamed events.
Useful for verifying model changes without the frontend.

Usage:
    ./scripts/test_chat.py                         # default prompt
    ./scripts/test_chat.py "list pods in kube-system"
    ./scripts/test_chat.py --base-url http://host:8000 "why are pods crashing?"
    ./scripts/test_chat.py --session-id <uuid> "follow-up question"
    ./scripts/test_chat.py --stop-after 3          # send stop after 3 events
"""

import argparse
import asyncio
import json
import sys
import time

import httpx
import websockets


DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_PROMPT = "What namespaces exist on this cluster?"


async def get_clusters(base_url: str) -> list[dict]:
    async with httpx.AsyncClient(base_url=base_url) as client:
        r = await client.get("/api/clusters")
        r.raise_for_status()
        return r.json()["items"]


async def create_session(base_url: str, cluster_id: str) -> dict:
    async with httpx.AsyncClient(base_url=base_url) as client:
        r = await client.post("/api/sessions", json={"cluster_id": cluster_id})
        r.raise_for_status()
        return r.json()


async def run(
    base_url: str,
    prompt: str,
    session_id: str | None = None,
    stop_after: int | None = None,
) -> None:
    if not session_id:
        clusters = await get_clusters(base_url)
        if not clusters:
            print("ERROR: no clusters found. Add one via the admin UI first.")
            sys.exit(1)

        cluster = clusters[0]
        print(f"Cluster : {cluster['display_name']} ({cluster['id']})")

        session = await create_session(base_url, cluster["id"])
        session_id = session["id"]

    print(f"Session : {session_id}")
    print(f"Prompt  : {prompt}")
    print("-" * 60)

    ws_url = base_url.replace("http", "ws", 1)
    uri = f"{ws_url}/ws/chat/{session_id}"

    event_count = 0
    t0 = time.monotonic()

    async with websockets.connect(uri) as ws:
        await ws.send(json.dumps({"type": "message", "content": prompt}))

        async for raw in ws:
            event = json.loads(raw)
            etype = event.get("type")

            if etype == "text_delta":
                print(event.get("content", ""), end="", flush=True)

            elif etype == "tool_start":
                name = event.get("tool_name", "?")
                inp = json.dumps(event.get("tool_input", {}), indent=2)
                print(f"\n>>> TOOL: {name}\n{inp}")

            elif etype == "tool_result":
                name = event.get("tool_name", "?")
                content = event.get("content", "")
                is_err = event.get("is_error", False)
                tag = "ERROR" if is_err else "RESULT"
                # Truncate long outputs for readability
                preview = content[:500] + "..." if len(content) > 500 else content
                print(f"<<< {tag} ({name}):\n{preview}\n")

            elif etype == "done":
                meta = event.get("metadata", {})
                stopped = meta.get("stopped", False)
                elapsed = time.monotonic() - t0
                print(f"\n{'=' * 60}")
                if stopped:
                    print("STOPPED by user")
                print(f"Done in {elapsed:.1f}s | metadata: {json.dumps(meta)}")
                break

            elif etype == "error":
                print(f"\nERROR: {event.get('content')}")
                break

            elif etype == "pong":
                continue

            event_count += 1

            if stop_after and event_count >= stop_after:
                print(f"\n--- sending stop after {event_count} events ---")
                await ws.send(json.dumps({"type": "stop"}))


def main() -> None:
    parser = argparse.ArgumentParser(description="Smoke-test the Inkwest chat backend")
    parser.add_argument("prompt", nargs="?", default=DEFAULT_PROMPT, help="Chat prompt")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Backend base URL")
    parser.add_argument("--session-id", default=None, help="Reuse existing session")
    parser.add_argument(
        "--stop-after",
        type=int,
        default=None,
        help="Send stop after N events (test cancellation)",
    )
    args = parser.parse_args()

    asyncio.run(run(args.base_url, args.prompt, args.session_id, args.stop_after))


if __name__ == "__main__":
    main()
