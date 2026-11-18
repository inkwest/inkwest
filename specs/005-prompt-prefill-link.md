# 005 — Prompt prefill from a link

**Status**: in progress
**Requested by**: Damien Degois
**Date**: 2026-11-18

## Problem

Other tools (alerting, runbooks, dashboards, tickets) cannot hand an investigation over to Inkwest.
The only link formats left (`/start?cluster=&prompt=`, `/#cluster=&q=`) come from the original tool:
`/start` requires a cluster, and both create a session and send the prompt on their own, so the user
cannot read or edit what another tool wrote before an agent with infrastructure access runs it.

## Solution

A tool can open Inkwest with a prompt already typed in the new-conversation box, through a plain link.
Nothing is sent: the user reads the text, edits it if needed, and presses send. A short notice says the
text came from a link. The old auto-send links are removed.

## Scope

- Link format `/#prompt=<form-encoded text>`. The fragment never reaches the server: the prompt stays
  out of server and load-balancer logs and is not bound by the ALB request-line quota (16 K).
- Encoding: the `prompt` value is `application/x-www-form-urlencoded`. Produce it with
  `encodeURIComponent` (JS), `urllib.parse.quote(text, safe='')` or `quote_plus` (Python),
  `url.QueryEscape` (Go). A raw `+` decodes to a space; a literal plus must be sent as `%2B`.
- The prompt survives the OIDC login round-trip (captured at app boot, before the auth guard redirects)
  for 10 minutes; an older captured prompt is dropped.
- A link opened in a tab already on `/` (same-document `hashchange`, no reload) prefills too.
- Notice above the composer: "Prefilled from a link. Review it before sending.", dismissible.
- Removal of the legacy links: `/start` route (`QuickStartPage`), the `#cluster=&q=` handling in
  `NewSessionPage`, the first-message Share button in `MessageBubble`, the `Quick-Start Links` section
  of `docs/ARCHITECTURE.md`.
- Documentation of the link format.

## Out of scope

- POST hand-off (dropped: the GET fragment has no server-side size limit, so it covers every case).
- Auto-send, model/engine/system-prompt/connector selection from the link. Prompt text only.
- Attachments in the link.
- Prefill into an existing conversation (`/session/<id>#prompt=`).
- A "copy prefill link" button in the UI.

## Acceptance criteria

- [ ] **AC1** Given a signed-in user, when they open `/#prompt=<encoded text>`, then the
  new-conversation composer contains exactly that text (newlines, `& # + %`, emoji, HTML kept
  verbatim; a literal `+` arrives encoded as `%2B`), the fragment is removed from the address bar,
  and no session is created. This holds for a fresh load and for a tab already on `/`.
- [ ] ~~**AC2** Cross-origin form `POST /` redirects 303 to the fragment.~~ Dropped: GET only.
- [ ] **AC3** A prefilled prompt is never sent without a user action: no session is created and no
  chat message is emitted until the user presses send.
- [ ] **AC4** Reloading the page after a prefill leaves the composer empty (one-shot).
- [ ] **AC5** Given a signed-out user under OIDC, when they open a prefill link, then after the login
  round-trip completed within 10 minutes the composer contains the prompt (same tab).
- [ ] ~~**AC6** Over-limit POST gets a readable 413.~~ Dropped: GET only.
- [ ] **AC7** While a link prefill is in the composer, a notice "Prefilled from a link. Review it before
  sending." shows above it; it disappears on dismiss or on send, and never shows for a normal start.
- [ ] **AC8** `/start?cluster=…&prompt=…` no longer creates a session or sends anything, `/#cluster=…&q=…`
  no longer auto-sends, the first-message Share button is gone, and
  `grep -rnE "QuickStart|handleShare|/start\?" frontend/src docs` returns nothing.
- [ ] ~~**AC9** POST route works in dev and prod layouts.~~ Dropped: GET only.
- [ ] **AC10** A 150 000-character prompt (ASCII) and a 20 000-character prompt mixing accents and emoji
  both prefill exactly through the link.

## Phases

### Phase 1 — Prefill + notice
- Work: `lib/linkPrefill.ts` (capture at boot into `sessionStorage` when the path is `/`, strip
  `prompt` from the fragment and keep other keys, one-shot take, 10-minute expiry), called from
  `main.tsx`; `StartConversation` takes it on mount and on `hashchange` and injects it through
  `useComposerStore`; notice component.
- **Covers**: AC1, AC3, AC4, AC5, AC7, AC10
- **Data model impact**: none
- **DoD**: vitest green for capture/take (incl. capture → simulated reload → take for AC5, storage
  blocked, other fragment keys kept, non-`/` path ignored) and for the notice; Playwright probe on a
  local dev stack passes AC1, AC3, AC4 and AC10 with 0 sessions created.

### Phase 2 — Legacy removal + docs
- Work: delete `QuickStartPage` and its route, the hash handling in `NewSessionPage`, the Share button
  and its tests; replace `Quick-Start Links` in `docs/ARCHITECTURE.md` with the prefill link format.
- **Covers**: AC8
- **Data model impact**: none
- **DoD**: AC8 grep returns nothing; `npm run lint`, `npm test -- --run`, backend pytest and
  `pre-commit run --all-files` green.

## Data model impact (summary)

None.

## Open questions

None.

## Decisions

- GET fragment only, no POST — the fragment is never sent to the server, so no ALB quota applies;
  the remaining ceiling is the browser URL limit (2 MB in Chromium).
- Prefill is captured only on `/`: a fragment on another path is ignored.
- If the composer already holds text, the existing inject confirm ("Replace the text…") applies. If
  the user cancels it on a same-tab link, the notice still shows: knowing the answer needs a composer
  store contract change, and the user just chose to keep their text.
- Captured prompt expires after 10 minutes — long enough for an OIDC login, short enough that a
  prefill never resurfaces on an unrelated later conversation in the same tab.
- Notice included (AC7) — any site can prefill a prompt for an agent with infrastructure access; no
  auto-send plus a visible origin keeps the user the one who decides.
- A throwaway spike validated AC1, AC3 and AC4 in dev
  with a short prompt. AC5 is unit-tested only; it is not validated against a real OIDC provider.
