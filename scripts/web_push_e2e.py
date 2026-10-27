#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["playwright"]
# ///
"""Spec 003 end-to-end check: a share invite reaches a real Chrome through FCM (003-AC10, 003-AC11).

Prerequisites:
- Inkwest running (combined image or dev backend + SPA) with OIDC_ENABLED=false (dev auth),
  the three VAPID_* vars set (docs/WEB_PUSH.md), outbound HTTPS to fcm.googleapis.com, and the
  metrics exporter reachable (METRICS_ENABLED=true).
- A fresh database migrated with backend/db_migrate.py, recreated for every run: the invitee
  must hold no other subscription, and a pending invite left by a previous run covers the
  Settings toggle. The inviter/invitee users are created on first request.
- Google Chrome stable (`uvx playwright install chrome`, or the distro
  package). Playwright's bundled Chromium has no FCM keys, so push subscribe fails there.
- `psql` on PATH, configured through the standard PG* variables (PGHOST, PGPORT, PGUSER,
  PGPASSWORD, PGDATABASE) to reach the same database. Used only to print the stored row's host.

Run:
    INKWEST_BASE_URL=http://localhost:8000 INKWEST_METRICS_URL=http://localhost:9090/metrics \\
    PGHOST=localhost PGDATABASE=inkwest uv run ./scripts/web_push_e2e.py

Env:
    INKWEST_BASE_URL     app origin (default http://localhost:8000)
    INKWEST_METRICS_URL  Prometheus endpoint (default http://localhost:9090/metrics)
    OUT                  output dir for the Chrome profile, screenshots and run log
                         (default: a new temp dir, printed at start)
    HEADLESS             1 (default) or 0

Identities: the browser is dev@localhost (default dev user, the invitee); API calls run as
alice@localhost via the inkwest_dev_user cookie (the inviter). Exits 1 on the first failed check.
Endpoints and keys are never printed: the endpoint is a capability URL.
"""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

from playwright.async_api import async_playwright

BASE = os.environ.get("INKWEST_BASE_URL", "http://localhost:8000").rstrip("/")
METRICS_URL = os.environ.get("INKWEST_METRICS_URL", "http://localhost:9090/metrics")
OUT = os.environ.get("OUT") or tempfile.mkdtemp(prefix="web-push-e2e-")
HEADLESS = os.environ.get("HEADLESS", "1") == "1"
TOGGLE = "Browser notifications on this device"
# FCM answers 410 to a freshly minted subscription for up to ~10s (seen on Chrome 156), and the
# backend keeps such a young row but counts the send `failed` (no retry): the AC10 push is lost.
FCM_WARMUP_SECONDS = 20
LOG: list[dict] = []


class CheckError(Exception):
    pass


def check(ok: bool, msg: str) -> None:
    # Not `assert`: python -O would strip it.
    if not ok:
        raise CheckError(msg)


def log(step: str, **kw) -> None:
    entry = {"t": time.strftime("%H:%M:%S"), "step": step, **kw}
    LOG.append(entry)
    print(json.dumps(entry), flush=True)


def api(method: str, path: str, body: dict | None = None, user: str | None = "alice"):
    headers = {"Content-Type": "application/json"}
    if user:
        headers["Cookie"] = f"inkwest_dev_user={user}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, method=method, data=data, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        # Never the body: a 422 echoes the request, endpoint included.
        raise CheckError(f"{method} {path}: HTTP {e.code}") from None


def subscription_hosts() -> list[str]:
    sql = (
        "select substring(endpoint from '^https://([^/]+)/') || '|' || u.email"
        " from inkwest.push_subscriptions s join inkwest.users u on u.id = s.user_id"
    )
    out = subprocess.run(["psql", "-Atc", sql], capture_output=True, text=True)
    if out.returncode:
        raise CheckError(f"psql exited {out.returncode}: {out.stderr.strip()}")
    return out.stdout.split()


def push_ok_count() -> float:
    # The labelled series only appears after its first increment: absent = 0.
    with urllib.request.urlopen(METRICS_URL, timeout=5) as r:
        for line in r.read().decode().splitlines():
            if line.startswith('inkwest_push_sent_total{result="ok"}'):
                return float(line.split()[-1])
    return 0.0


def _meta(event: dict) -> dict[str, str]:
    return {m["key"]: m["value"] for m in event["eventMetadata"]}


def _share_id(event: dict) -> str | None:
    try:
        return json.loads(_meta(event).get("Payload", "")).get("share_id")
    except (ValueError, AttributeError):
        return None


async def wait_push_handled(events: list[dict], share_id: str, timeout: float = 60) -> str | None:
    """Status of the worker's push handler for ``share_id``, None if it never ran.

    Chrome's DevTools push log: "Push event dispatched" carries the decrypted payload,
    "Push event completed" the handler outcome once its waitUntil settles; same instanceId.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ids = {
            e["instanceId"]
            for e in events
            if e["eventName"] == "Push event dispatched" and _share_id(e) == share_id
        }
        for e in events:
            if e["eventName"] == "Push event completed" and e["instanceId"] in ids:
                return _meta(e).get("Status")
        await asyncio.sleep(0.5)
    return None


NOTIFS_JS = """async (tag) => {
  const r = await navigator.serviceWorker.ready;
  return (await r.getNotifications({tag})).map(n => ({title: n.title, tag: n.tag}));
}"""

SW_NOTIFS_JS = """async (tag) => {
  const n = await self.registration.getNotifications({tag});
  const wins = await self.clients.matchAll({type: 'window', includeUncontrolled: true});
  return {notifs: n.map(x => ({title: x.title, tag: x.tag})),
          windows: wins.map(w => ({url: w.url, focused: w.focused}))};
}"""


def new_shared_session(title: str, invitee_id: str) -> str:
    st, s = api("POST", "/api/sessions", {"title": title})
    check(st == 201, f"create session: HTTP {st}")
    st, p = api("POST", f"/api/sessions/{s['id']}/shares", {"user_id": invitee_id})
    check(st == 201, f"create share: HTTP {st}")
    return p["id"]  # ParticipantResponse.id is the share id, i.e. the notification tag.


async def sw_state(ctx, tag: str) -> dict:
    for w in ctx.service_workers:
        if w.url.endswith("/sw.js"):
            try:
                return await w.evaluate(SW_NOTIFS_JS, tag)
            except Exception as e:  # the worker may be stopped between events
                return {"error": type(e).__name__}
    return {"error": "no-sw-target"}


async def invite_popup_visible(page, title: str, timeout_ms: int) -> bool:
    try:
        await page.get_by_text(title).wait_for(timeout=timeout_ms)
    except Exception:
        return False
    return await page.get_by_role("button", name="Accept").is_visible()


async def main() -> None:
    log("start", base=BASE, out=OUT, headless=HEADLESS)
    _, invitee = api("GET", "/api/auth/me", user=None)
    _, inviter = api("GET", "/api/auth/me")
    log("identities", invitee=invitee["email"], inviter=inviter["email"])

    async with async_playwright() as p:
        ctx = await p.chromium.launch_persistent_context(
            f"{OUT}/chrome-profile",
            channel="chrome",
            headless=HEADLESS,
            viewport={"width": 1280, "height": 800},
        )
        await ctx.grant_permissions(["notifications"], origin=BASE)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # The first-run onboarding tour overlays the page and swallows clicks: skip it.
        await page.goto(BASE)
        skip = page.get_by_text("Skip", exact=True)
        try:
            await skip.wait_for(timeout=8000)
            await skip.click()
            log("tour_skipped")
        except Exception:
            pass

        # Enable through the real Settings UI.
        await page.goto(f"{BASE}/?settings=notifications")
        await page.wait_for_selector(f'button[aria-disabled="false"]:has-text("{TOGGLE}")')
        try:
            async with page.expect_response(
                lambda r: "/api/me/push-subscriptions" in r.url and r.request.method == "PUT",
                timeout=30000,
            ) as resp:
                await page.locator("button", has_text=TOGGLE).click()
            put_status = (await resp.value).status
        except Exception as e:
            await page.screenshot(path=f"{OUT}/enable-failed.png")
            alert = await page.locator('[role="alert"]').all_inner_texts()
            raise CheckError(f"no PUT after toggle ({type(e).__name__}); UI alert: {alert}")
        check(put_status == 204, f"PUT /api/me/push-subscriptions: HTTP {put_status}, want 204")
        await page.wait_for_selector(f'button[aria-pressed="true"]:has-text("{TOGGLE}")')
        await page.screenshot(path=f"{OUT}/01-settings-enabled.png")
        log("subscribed", rows=subscription_hosts())
        ok_before = push_ok_count()
        await asyncio.sleep(FCM_WARMUP_SECONDS)

        # 003-AC10: no Inkwest window at all when the push arrives.
        blank = await ctx.new_page()
        await blank.goto("about:blank")
        await page.close()
        await blank.bring_to_front()
        title10 = f"E2E push check {time.strftime('%H%M%S')} AC10"
        tag10 = new_shared_session(title10, invitee["id"])
        state: dict = {}
        for _ in range(30):
            await asyncio.sleep(2)
            state = await sw_state(ctx, tag10)
            if state.get("notifs"):
                break
        log("ac10_sw_view", **state)
        # Opening "/" is what the worker's notificationclick does with no Inkwest window open.
        page = await ctx.new_page()
        await page.goto(BASE)
        notifs = await page.evaluate(NOTIFS_JS, tag10)
        log("ac10_notifications", notifs=notifs)
        # 003-AC10: an OS notification with the share id as tag and the session title shown.
        check(
            any(n["tag"] == tag10 and title10 in n["title"] for n in notifs),
            f"003-AC10: no notification tagged with the share id and titled {title10!r}",
        )
        # 003-AC10: the invite popup is shown once Inkwest is open.
        check(
            await invite_popup_visible(page, title10, 15000),
            "003-AC10: invite popup not shown",
        )
        await page.screenshot(path=f"{OUT}/02-ac10-popup.png")
        await page.get_by_role("button", name="Decline").click()
        await page.evaluate(
            "async () => (await (await navigator.serviceWorker.ready).getNotifications())"
            ".forEach(n => n.close())"
        )

        # 003-AC11: an Inkwest window is focused when the push arrives.
        cdp = await ctx.new_cdp_session(page)
        # Playwright emulates focus on every page, so the worker would see Inkwest focused
        # whichever tab is in front: use the browser's real focus instead.
        await cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
        await page.bring_to_front()
        await page.click("body", position={"x": 5, "y": 5})
        check(await page.evaluate("document.hasFocus()"), "003-AC11 precondition: page not focused")
        # Absence only means something once the worker has handled the push: watch it land.
        push_events: list[dict] = []
        cdp.on(
            "BackgroundService.backgroundServiceEventReceived",
            lambda e: push_events.append(e["backgroundServiceEvent"]),
        )
        await cdp.send("BackgroundService.startObserving", {"service": "pushMessaging"})
        await cdp.send(
            "BackgroundService.setRecording", {"shouldRecord": True, "service": "pushMessaging"}
        )
        title11 = f"E2E push check {time.strftime('%H%M%S')} AC11"
        tag11 = new_shared_session(title11, invitee["id"])
        status = await wait_push_handled(push_events, tag11)
        log("ac11_push_handled", status=status, events=[e["eventName"] for e in push_events])
        check(status == "Success", f"003-AC11: push handler for the share: {status}, want Success")
        log("ac11_sw_view", **(await sw_state(ctx, tag11)))
        notifs = await page.evaluate(NOTIFS_JS, tag11)
        # 003-AC11: the worker shows nothing while an Inkwest window is focused.
        check(not notifs, f"003-AC11: {len(notifs)} notification(s) shown on a focused window")
        # 003-AC11: the in-app popup still carries the invite.
        check(
            await invite_popup_visible(page, title11, 5000),
            "003-AC11: invite popup not shown",
        )
        await page.screenshot(path=f"{OUT}/03-ac11-popup.png")

        ok_after = push_ok_count()
        log("metric_ok", before=ok_before, after=ok_after)
        check(
            ok_after - ok_before == 2,
            f'inkwest_push_sent_total{{result="ok"}} went {ok_before} -> {ok_after}, want +2',
        )
        await ctx.close()


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    rc = 0
    try:
        asyncio.run(main())
        print("PASS: 003-AC10 and 003-AC11")
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}", file=sys.stderr)
        rc = 1
    with open(f"{OUT}/run.json", "w") as f:
        json.dump(LOG, f, indent=1)
        f.write("\n")
    raise SystemExit(rc)
