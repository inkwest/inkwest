"""
External access connector routes.

Brokers temporary, scoped credentials to external services on explicit per-session
user consent. Responses only ever carry the secret-free :class:`ConnectorStatus`
projection — credentials live in the in-RAM store and never reach the client.
"""

import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.dependencies import CurrentUser, get_db
from app.core.config import get_settings
from app.core.constants import CONNECTOR_STATE_TTL_SECONDS
from app.models.connector_audit import ConnectorAuditLog
from app.models.connector_pending_state import ConnectorPendingState
from app.models.session import Session
from app.models.user import User
from app.services.connectors.aws import EnumProgressCb
from app.services.connectors.base import (
    ConnectorError,
    ConnectorStatus,
    MintedCredential,
    generate_pkce,
)
from app.services.connectors.grants import aws_selections_from_grant, derive_prior_grants
from app.services.connectors.registry import all_connectors, get_connector
from app.services.connectors.store import get_credential_store
from app.services.connectors.warm import (
    grant_aws_provisional_from_warm,
    grant_gitlab_from_warm,
    store_aws_warm,
    store_gitlab_warm,
    update_aws_warm_options,
)
from app.services.eks_refresh import schedule_session_eks_refresh
from app.services.messages import save_session_event
from app.tools.connector_exec import write_aws_profiles

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors", tags=["connectors"])

_MAX_PENDING_STATES = 10000

# AWS device authorizations in flight, keyed by (user_id, session_id) -> deviceCode.
# Process RAM (same single-replica constraint as the credential store).
_aws_device_pending: dict[tuple[str, str], str] = {}

# Live account-enumeration progress, keyed by (user_id, session_id). Written from
# the enumeration thread, read by the progress poll endpoint (GIL-safe for these
# int/str fields; a stale read just shows a slightly older count). Entry lives only
# for the duration of an enumeration — set on start, popped in a finally.
#
# Process-local, like the credential store: the progress poll must hit the same
# replica that runs the enumeration, so it relies on the same single-replica /
# session-affinity deployment constraint (see store.py). Cross-replica, the poll
# just returns "idle" → the UI degrades to closing the popup on the poll's return.
#
# Each entry carries a unique ``token``: ``_end_enum_progress`` only pops the entry
# it created, so a concurrent enumeration for the same key (e.g. a double poll) can't
# yank a still-running sibling's progress out from under it.
_aws_enum_progress: dict[tuple[str, str], dict[str, Any]] = {}


def _progress_cb(key: tuple[str, str], token: object, phase: str) -> EnumProgressCb:
    """Build an owner-guarded (done, total) publisher for ``phase``.

    Owner-guarded: it only writes while the entry still carries *this* token, so a
    superseded task (e.g. an EKS discovery still running after a fresh re-grant
    replaced the entry) goes silent instead of resurrecting or clobbering it.
    """

    def cb(done: int, total: int) -> None:
        cur = _aws_enum_progress.get(key)
        if cur is not None and cur.get("token") is token:
            _aws_enum_progress[key] = {
                "phase": phase,
                "done": done,
                "total": total,
                "token": token,
            }

    return cb


def _begin_enum_progress(
    key: tuple[str, str], phase: str = "enumerating"
) -> tuple[object, EnumProgressCb]:
    """Publish an initial progress entry and return its (token, callback)."""
    token = object()
    _aws_enum_progress[key] = {"phase": phase, "done": 0, "total": 0, "token": token}
    return token, _progress_cb(key, token, phase)


def _end_enum_progress(key: tuple[str, str], token: object) -> None:
    """Drop the progress entry — but only if we still own it (see token rationale)."""
    cur = _aws_enum_progress.get(key)
    if cur is not None and cur.get("token") is token:
        _aws_enum_progress.pop(key, None)


# --------------------------------------------------------------------------- #
# Response / request models (secret-free by construction)
# --------------------------------------------------------------------------- #
class ConnectorStatusResponse(BaseModel):
    connector: str
    state: str
    expires_at: datetime | None = None
    scope: str | None = None
    selection: dict[str, Any] | None = None
    available_accounts: list[dict[str, Any]] | None = None
    available_roles: list[dict[str, Any]] | None = None
    error: str | None = None

    @classmethod
    def from_status(cls, s: ConnectorStatus) -> "ConnectorStatusResponse":
        return cls(
            connector=s.connector,
            state=s.state,
            expires_at=s.expires_at,
            scope=s.scope,
            selection=s.selection,
            available_accounts=s.available_accounts,
            available_roles=s.available_roles,
            error=s.error,
        )


class RedirectResponseModel(BaseModel):
    redirect_url: str


class AwsOptionsResponse(BaseModel):
    accounts: list[dict[str, Any]]
    roles: list[dict[str, Any]]


class AwsEnumProgressResponse(BaseModel):
    # phase: "idle" (nothing running) | "enumerating" (SSO account listing) |
    # "granting" (STS minting) | "discovering" (EKS cluster discovery).
    phase: str
    done: int = 0
    total: int = 0


class AwsSelectItem(BaseModel):
    account_id: str
    role_name: str


class AwsSelectRequest(BaseModel):
    # One or more account/role pairs → one AWS profile each.
    selections: list[AwsSelectItem]


class AwsDeviceStartResponse(BaseModel):
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    interval: int
    expires_in: int


class AwsDevicePollResponse(BaseModel):
    # pending | needs_selection | connected | error
    status: str
    detail: str | None = None


class PriorGrantResponse(BaseModel):
    """A connector this session previously granted and can renew (secret-free)."""

    connector: str
    live: bool  # a usable (non-expired) credential is currently held
    state: str  # public_status state of the held credential (or "not_connected")
    aws_selection: dict[str, Any] | None = None  # AWS account/role profiles to re-apply


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def _require_enabled() -> None:
    if not get_settings().connectors_enabled:
        raise HTTPException(status_code=404, detail="Connectors are not enabled")


def _redirect_uri() -> str:
    s = get_settings()
    return s.connector_redirect_base or f"{s.frontend_url}/api/connectors/callback"


async def _verify_session_owner(db: AsyncSession, session_id: str, user: User) -> uuid.UUID:
    """Ensure the session exists and belongs to the user (admins may act on any)."""
    try:
        sid = uuid.UUID(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid session id") from e
    result = await db.execute(select(Session).where(Session.id == sid))
    session = result.scalar_one_or_none()
    if session is None:
        raise HTTPException(status_code=404, detail="Session not found")
    if not user.is_admin() and session.user_id is not None and session.user_id != user.id:
        raise HTTPException(status_code=403, detail="You do not have access to this session")
    return sid


async def _audit(
    db: AsyncSession,
    *,
    user_id: uuid.UUID | None,
    session_id: uuid.UUID,
    connector: str,
    action: str,
    cred: MintedCredential | None = None,
) -> None:
    # The audit log is the only durable record of connector grants — it is what
    # `derive_prior_grants` reads to offer renewal — so it is always written, not
    # gated behind a flag. Disabling it would silently break session renewal.
    db.add(
        ConnectorAuditLog(
            user_id=user_id,
            session_id=session_id,
            connector=connector,
            action=action,
            scope=cred.scope if cred else None,
            selection=cred.selection if cred else None,
        )
    )
    await db.commit()


def _aws_targets(cred: MintedCredential | None) -> list[dict[str, Any]]:
    """Structured ``{account_id, account_name, role}`` targets from an AWS selection.

    Kept as data (not a joined string) so the conversation event is a parse-free audit
    record; the frontend renders the wording.
    """
    profiles = (cred.selection or {}).get("profiles") if cred and cred.selection else None
    if not profiles:
        return []
    return [
        {
            "account_id": p.get("account_id"),
            "account_name": p.get("account_name"),
            "role": p.get("role_name"),
        }
        for p in profiles
    ]


def _get_enabled_connector(name: str) -> Any:
    connector = get_connector(name)
    if connector is None:
        raise HTTPException(status_code=404, detail=f"Connector '{name}' is not available")
    return connector


async def _apply_aws_selection(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    sid: uuid.UUID,
    conn: Any,
    provisional: MintedCredential,
    selections: list[dict[str, str]],
    event: str = "granted",
) -> ConnectorStatusResponse:
    """Mint STS creds for the given account/role pairs, persist, refresh EKS, audit.

    Shared tail of ``aws_select`` (manual pick → "granted") and ``aws_renew_select``
    (saved pick → "renewed"); ``event`` distinguishes the two for the inline note.
    AWS becomes usable only here (after the pick), so this — not the device step — is
    where the conversation event with the chosen account/role is emitted.
    """
    # One progress entry spans both visible phases of the grant: STS minting
    # ("granting") below, then EKS discovery ("discovering") in the background task.
    # The token lets the slow background task own the entry's release without a
    # racing re-grant yanking it (see _progress_cb / _end_enum_progress).
    key = (str(user_id), str(sid))
    token, prog_cb = _begin_enum_progress(key, phase="granting")
    # Release the entry in a finally unless ownership is handed to the background EKS
    # task — so it never dangles on ANY pre-handoff failure (ConnectorError, a tmpfs
    # OSError from write_aws_profiles, a raw boto error from the STS client build,
    # a DB error in the audit/event writes), not just the explicit ConnectorError.
    handed_off = False
    try:
        try:
            cred = await conn.select_account_roles(
                provisional, selections=selections, progress_cb=prog_cb
            )
        except ConnectorError as e:
            raise HTTPException(status_code=400, detail=str(e)) from e
        await get_credential_store().put(user_id, sid, cred)
        # Build the per-session ~/.aws projection now — the account/role pick is the
        # trigger. On a shared creds volume this is what the sandbox container
        # bind-mounts, so the granted profiles are present before the first turn
        # provisions the sandbox.
        write_aws_profiles(sid, cred)
        # Discover EKS clusters for the freshly minted profiles and generate one (tmpfs)
        # kubeconfig each — async, must not block the response. Only keep the progress
        # entry alive into discovery when an eks-* role was picked: only those map into a
        # cluster's RBAC, so a grant without one has no EKS wait worth showing (the UI
        # then closes on the next idle poll). Other picks still trigger discovery,
        # untracked. Case-insensitive prefix match — intentionally narrower than
        # _dedup_prefer_eks's substring "eks" heuristic: we wait only when the user
        # explicitly picked an eks-* role.
        want_eks = any(s.get("role_name", "").lower().startswith("eks-") for s in selections)
        profiles = cred.secret.get("profiles") or []
        if want_eks and profiles:
            disc_cb = _progress_cb(key, token, "discovering")
            # Seed the real profile count so a poll landing in the scheduling gap reads
            # a determinate "discovering 0/N" — never an ambiguous 0/0 that reads as done.
            disc_cb(0, len(profiles))
            # Ownership of the progress entry transfers to the background task, which
            # ends it via on_done; the finally below must not also release it.
            schedule_session_eks_refresh(
                sid, cred, progress_cb=disc_cb, on_done=lambda: _end_enum_progress(key, token)
            )
            handed_off = True
        else:
            schedule_session_eks_refresh(sid, cred)
        await _audit(
            db, user_id=user_id, session_id=sid, connector="aws", action="select", cred=cred
        )
        await save_session_event(db, sid, "aws", event, targets=_aws_targets(cred))
        return ConnectorStatusResponse.from_status(conn.public_status(cred))
    finally:
        if not handed_off:
            _end_enum_progress(key, token)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@router.get("/{session_id}/status", response_model=list[ConnectorStatusResponse])
async def get_status(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[ConnectorStatusResponse]:
    """Per-connector status for a session. Never includes a secret."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    store = get_credential_store()
    out: list[ConnectorStatusResponse] = []
    for connector in all_connectors():
        cred = await store.get(current_user.id, sid, connector.type)
        out.append(ConnectorStatusResponse.from_status(connector.public_status(cred)))
    return out


@router.post("/{session_id}/{connector}/initiate", response_model=RedirectResponseModel)
async def initiate(
    session_id: str,
    connector: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> RedirectResponseModel:
    """Start an OAuth flow: persist PKCE state and return the IdP authorize URL."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector(connector)
    if not conn.requires_oauth:
        raise HTTPException(status_code=400, detail=f"{connector} is not an OAuth connector")

    # Housekeeping + atomic insert with a count guard (mirrors the OIDC login flow).
    cutoff = datetime.now(UTC) - timedelta(seconds=CONNECTOR_STATE_TTL_SECONDS)
    await db.execute(delete(ConnectorPendingState).where(ConnectorPendingState.created_at < cutoff))

    state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = generate_pkce()
    result = await db.execute(
        text(
            """
            INSERT INTO inkwest.connector_pending_states
                (state, code_verifier, connector, user_id, session_id)
            SELECT :state, :verifier, :connector, :user_id, :session_id
            WHERE (SELECT count(*) FROM inkwest.connector_pending_states) < :max_states
            RETURNING id
            """
        ),
        {
            "state": state,
            "verifier": code_verifier,
            "connector": connector,
            "user_id": str(current_user.id),
            "session_id": str(sid),
            "max_states": _MAX_PENDING_STATES,
        },
    )
    if result.first() is None:
        raise HTTPException(status_code=429, detail="Too many pending connector requests")
    await db.commit()

    try:
        url = conn.build_authorize_url(
            state=state, code_challenge=code_challenge, redirect_uri=_redirect_uri()
        )
    except ConnectorError as e:
        raise HTTPException(status_code=500, detail=str(e)) from e
    return RedirectResponseModel(redirect_url=url)


@router.get("/callback")
async def callback(
    state: str = Query(...),
    code: str | None = Query(None),
    error: str | None = Query(None),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """OAuth redirect target. Identity comes from the consumed state row, not a JWT."""
    settings = get_settings()
    frontend = f"{settings.frontend_url}/connectors/callback"

    # Atomic consume-once + TTL validation (mirrors the OIDC callback).
    row = (
        await db.execute(
            delete(ConnectorPendingState)
            .where(ConnectorPendingState.state == state)
            .returning(
                ConnectorPendingState.code_verifier,
                ConnectorPendingState.connector,
                ConnectorPendingState.user_id,
                ConnectorPendingState.session_id,
                ConnectorPendingState.created_at,
            )
        )
    ).first()
    await db.commit()

    cutoff = datetime.now(UTC) - timedelta(seconds=CONNECTOR_STATE_TTL_SECONDS)
    if row is None or row.created_at < cutoff:
        return RedirectResponse(url=f"{frontend}#status=error&reason=invalid_or_expired_state")
    sid_q = f"&session_id={row.session_id}"
    if error or not code:
        reason = quote(error or "no_code")
        return RedirectResponse(
            url=f"{frontend}#connector={row.connector}&status=error&reason={reason}{sid_q}"
        )

    conn = get_connector(row.connector)
    if conn is None:
        return RedirectResponse(url=f"{frontend}#status=error&reason=connector_unavailable")

    try:
        cred = await conn.exchange_code(
            code=code, code_verifier=row.code_verifier, redirect_uri=_redirect_uri()
        )
    except ConnectorError as e:
        logger.warning("connector_exchange_failed", extra={"connector": row.connector})
        return RedirectResponse(
            url=f"{frontend}#connector={row.connector}&status=error&reason={quote(str(e))}{sid_q}"
        )

    if row.connector == "gitlab":
        # The refresh token is user-scoped warm material: it moves to the warm
        # store (rotation-safe, shared across the user's conversations) and never
        # stays in the session credential.
        await store_gitlab_warm(row.user_id, cred.secret.pop("refresh_token", None))
    await get_credential_store().put(row.user_id, row.session_id, cred)
    await _audit(
        db,
        user_id=row.user_id,
        session_id=row.session_id,
        connector=row.connector,
        action="grant",
        cred=cred,
    )
    await save_session_event(db, row.session_id, row.connector, "granted")
    logger.info(
        "connector_granted",
        extra={"connector": row.connector, "session_id": str(row.session_id)},
    )
    result_state = conn.public_status(cred).state
    return RedirectResponse(
        url=f"{frontend}#connector={row.connector}&status={result_state}{sid_q}"
    )


@router.post("/{session_id}/aws/device/start", response_model=AwsDeviceStartResponse)
async def aws_device_start(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AwsDeviceStartResponse:
    """Begin the AWS device authorization. Returns the user code + verification URL."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("aws")
    try:
        info = await conn.start_device_auth()
    except ConnectorError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    _aws_device_pending[(str(current_user.id), str(sid))] = info["device_code"]
    return AwsDeviceStartResponse(
        user_code=info["user_code"],
        verification_uri=info["verification_uri"],
        verification_uri_complete=info["verification_uri_complete"],
        interval=info["interval"],
        expires_in=info["expires_in"],
    )


@router.post("/{session_id}/aws/device/poll", response_model=AwsDevicePollResponse)
async def aws_device_poll(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AwsDevicePollResponse:
    """Poll the device authorization. The frontend calls this every ``interval`` s."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("aws")
    key = (str(current_user.id), str(sid))
    device_code = _aws_device_pending.get(key)
    existing = await get_credential_store().get(current_user.id, sid, "aws")

    # Idempotent: if a (concurrent) poll already obtained the credential, just
    # report its status instead of redeeming the device code again.
    if device_code is None:
        if existing is not None:
            return AwsDevicePollResponse(status=conn.public_status(existing).state)
        raise HTTPException(status_code=409, detail="No AWS device authorization in progress")

    try:
        token = await conn.poll_device_token(device_code)
    except ConnectorError as e:
        _aws_device_pending.pop(key, None)
        return AwsDevicePollResponse(status="error", detail=str(e))

    if token is None:
        # Still pending — or a concurrent poll just finished it.
        if existing is not None:
            _aws_device_pending.pop(key, None)
            return AwsDevicePollResponse(status=conn.public_status(existing).state)
        return AwsDevicePollResponse(status="pending")

    # Approved → mint the provisional (needs_selection) credential and store it.
    # Enumerating accounts/roles is the slow part; publish progress so a concurrent
    # /aws/options/progress poll can show "retrieving N accounts" while this blocks.
    prog_token, prog_cb = _begin_enum_progress(key)
    try:
        cred = await conn.provisional_from_token(
            token["access_token"],
            token["expires_in"],
            refresh_token=token.get("refresh_token"),
            progress_cb=prog_cb,
        )
    except Exception as e:  # noqa: BLE001 — boto raises varied client errors during enumeration
        # The device code is already redeemed and can't be reused, so don't leave it
        # pending (the next poll would re-redeem a spent code and 500). Drop it and
        # surface a clean error — the user re-runs the device flow. Mirrors the
        # refresh path, which also treats enumeration failure as terminal.
        _aws_device_pending.pop(key, None)
        logger.warning(
            "aws_enumeration_failed",
            extra={"session_id": str(sid), "error": type(e).__name__},
        )
        return AwsDevicePollResponse(status="error", detail="AWS account enumeration failed")
    finally:
        _end_enum_progress(key, prog_token)
    # The SSO token is user-scoped: keep it warm so the user's other conversations
    # can grant access with just the account/role selection — no new device flow.
    await store_aws_warm(current_user.id, cred)
    await get_credential_store().put(current_user.id, sid, cred)
    _aws_device_pending.pop(key, None)
    await _audit(
        db, user_id=current_user.id, session_id=sid, connector="aws", action="grant", cred=cred
    )
    logger.info("connector_granted", extra={"connector": "aws", "session_id": str(sid)})
    return AwsDevicePollResponse(status=conn.public_status(cred).state)


@router.get("/{session_id}/aws/options", response_model=AwsOptionsResponse)
async def aws_options(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AwsOptionsResponse:
    """List the accounts/roles the authenticated SSO session can assume."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    cred = await get_credential_store().get(current_user.id, sid, "aws")
    if cred is None or cred.selection is not None:
        raise HTTPException(status_code=409, detail="No pending AWS authentication; re-initiate")
    return AwsOptionsResponse(
        accounts=cred.available_accounts or [],
        roles=cred.available_roles or [],
    )


@router.post("/{session_id}/aws/options/refresh", response_model=AwsOptionsResponse)
async def aws_options_refresh(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> AwsOptionsResponse:
    """Force a re-enumeration of the SSO accounts/roles, bypassing the warm cache.

    Drives the picker's refresh button: re-queries the SSO portal with the live
    token, updates the provisional credential and the warm-grant cache so later
    opens see the fresh list too.
    """
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("aws")
    cred = await get_credential_store().get(current_user.id, sid, "aws")
    if cred is None or cred.selection is not None:
        raise HTTPException(status_code=409, detail="No pending AWS authentication; re-initiate")
    access_token = cred.secret.get("sso_access_token")
    if not access_token:
        raise HTTPException(status_code=409, detail="No SSO token; re-initiate")
    key = (str(current_user.id), str(sid))
    prog_token, prog_cb = _begin_enum_progress(key)
    try:
        accounts, roles = await conn.enumerate_accounts_roles(access_token, progress_cb=prog_cb)
    except Exception as e:  # noqa: BLE001 — boto raises varied client errors here
        raise HTTPException(status_code=502, detail="AWS enumeration failed") from e
    finally:
        _end_enum_progress(key, prog_token)
    cred.available_accounts = accounts
    cred.available_roles = roles
    await get_credential_store().put(current_user.id, sid, cred)
    await update_aws_warm_options(current_user.id, accounts, roles)
    return AwsOptionsResponse(accounts=accounts, roles=roles)


@router.get("/{session_id}/aws/options/progress", response_model=AwsEnumProgressResponse)
async def aws_options_progress(
    session_id: str,
    current_user: CurrentUser,
) -> AwsEnumProgressResponse:
    """Report live AWS grant/refresh progress (drives the picker's progress bar).

    Covers every slow phase reported on ``AwsEnumProgressResponse``: ``enumerating``
    (SSO account listing on device-auth/refresh), ``granting`` (STS minting) and
    ``discovering`` (EKS cluster discovery, which outlives the grant response).
    Returns ``phase="idle"`` when nothing is running. Polled on its own ~1s timer
    for the whole span, so it deliberately skips the session-ownership DB lookup the
    other routes do: the registry key is scoped to ``current_user.id``, so a user can
    only ever read their own progress regardless of the session id. The id is still
    parsed to reject malformed input and to match the canonical key.
    """
    _require_enabled()
    try:
        sid = uuid.UUID(session_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail="Invalid session id") from e
    p = _aws_enum_progress.get((str(current_user.id), str(sid)))
    if not p:
        return AwsEnumProgressResponse(phase="idle")
    return AwsEnumProgressResponse(
        phase=str(p["phase"]), done=int(p["done"]), total=int(p["total"])
    )


@router.post("/{session_id}/aws/select", response_model=ConnectorStatusResponse)
async def aws_select(
    session_id: str,
    body: AwsSelectRequest,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorStatusResponse:
    """Mint temporary STS credentials for the chosen account/role pairs."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("aws")
    provisional = await get_credential_store().get(current_user.id, sid, "aws")
    if provisional is None:
        raise HTTPException(status_code=409, detail="No pending AWS authentication; re-initiate")
    return await _apply_aws_selection(
        db,
        user_id=current_user.id,
        sid=sid,
        conn=conn,
        provisional=provisional,
        selections=[s.model_dump() for s in body.selections],
    )


@router.post("/{session_id}/{connector}/revoke", response_model=ConnectorStatusResponse)
async def revoke(
    session_id: str,
    connector: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorStatusResponse:
    """Drop a session's credential and best-effort revoke it provider-side."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector(connector)
    cred = await get_credential_store().revoke(current_user.id, sid, conn.type)
    if cred is not None:
        try:
            await conn.revoke(cred)
        except Exception:  # noqa: BLE001 — revocation is best-effort
            logger.debug("provider revoke failed (best-effort)", extra={"connector": connector})
        if connector == "aws":
            # Clear the tmpfs ~/.aws + EKS kubeconfigs IN PLACE: the conv container may
            # still be running with these dirs bind-mounted, so keep the dir inodes
            # (keep_dirs) — the agent then sees empty/revoked creds, and a re-grant in
            # the same conversation reaches it through the same mount. Hold the session
            # lock so this can't interleave with an in-flight EKS refresh's publish.
            from app.services.eks_refresh import session_lock
            from app.tools.connector_exec import cleanup_aws_files

            async with session_lock(sid):
                cleanup_aws_files(sid, keep_dirs=True)
        await _audit(
            db, user_id=current_user.id, session_id=sid, connector=connector, action="revoke"
        )
        await save_session_event(db, sid, connector, "revoked")
    return ConnectorStatusResponse.from_status(conn.public_status(None))


@router.post("/{session_id}/{connector}/grant-from-warm", response_model=ConnectorStatusResponse)
async def grant_from_warm(
    session_id: str,
    connector: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorStatusResponse:
    """Grant this session from the user's warm platform auth — no login flow.

    AWS: enumerates accounts/roles from the warm SSO token → ``needs_selection``
    (the picker opens immediately). GitLab: mints a fresh access token from the
    warm refresh token → ``connected``. 409 when the user has no live warm grant:
    the frontend falls back to the regular login flow. The minted credential is
    session-scoped and isolated like any other — only the *master* auth is shared.
    """
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector(connector)
    cred: MintedCredential | None = None
    # Keyed by the ACTING user, not the session owner. Warm grants are per-user;
    # the minted credential is stored under (current_user.id, sid) and read back
    # under the same key at tool-exec time — so an admin acting on another user's
    # session grants from *their own* warm auth and the owner's turns can't see it.
    # Do not "fix" this to session.user_id: that would inject one user's master
    # auth into another user's conversation. Same identity model as the device flow.
    if connector == "aws":
        cred = await grant_aws_provisional_from_warm(current_user.id)
    elif connector == "gitlab":
        cred = await grant_gitlab_from_warm(current_user.id)
    if cred is None:
        raise HTTPException(status_code=409, detail="No warm grant; run the login flow")
    await get_credential_store().put(current_user.id, sid, cred)
    await _audit(
        db, user_id=current_user.id, session_id=sid, connector=connector, action="grant", cred=cred
    )
    # AWS lands on needs_selection here — defer its event to the account/role pick
    # (where account+role are known); emit now only for connectors granted outright.
    if conn.public_status(cred).state == "connected":
        await save_session_event(db, sid, connector, "granted")
    logger.info(
        "connector_granted_from_warm", extra={"connector": connector, "session_id": str(sid)}
    )
    return ConnectorStatusResponse.from_status(conn.public_status(cred))


# --------------------------------------------------------------------------- #
# Renew previously-granted connectors (re-grant the same set; never add new)
# --------------------------------------------------------------------------- #
@router.get("/{session_id}/renewable", response_model=list[PriorGrantResponse])
async def renewable(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> list[PriorGrantResponse]:
    """The connectors this session previously granted (from the audit log).

    Drives a single "Renew access" action: only connectors still enabled and
    previously granted are returned; AWS carries its prior account/role selection.
    """
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    store = get_credential_store()
    out: list[PriorGrantResponse] = []
    for grant in await derive_prior_grants(db, sid):
        conn = get_connector(grant.connector)
        if conn is None:
            continue  # connector disabled since the grant — not renewable
        cred = await store.get(current_user.id, sid, conn.type)
        out.append(
            PriorGrantResponse(
                connector=grant.connector,
                live=cred is not None,
                state=conn.public_status(cred).state,
                aws_selection=grant.selection if grant.connector == "aws" else None,
            )
        )
    return out


@router.post("/{session_id}/gitlab/renew", response_model=ConnectorStatusResponse)
async def gitlab_renew(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorStatusResponse:
    """Silently renew GitLab from the user's warm refresh token.

    The refresh token is user-scoped warm material, so this works for any of the
    user's sessions — including past access-token expiry. 409 when no live warm
    grant exists: the frontend then falls back to the normal OAuth re-initiate.
    """
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("gitlab")
    renewed = await grant_gitlab_from_warm(current_user.id)
    if renewed is None:
        raise HTTPException(
            status_code=409, detail="GitLab cannot be renewed silently; re-initiate"
        )
    await get_credential_store().put(current_user.id, sid, renewed)
    await _audit(
        db,
        user_id=current_user.id,
        session_id=sid,
        connector="gitlab",
        action="grant",
        cred=renewed,
    )
    await save_session_event(db, sid, "gitlab", "renewed")
    return ConnectorStatusResponse.from_status(conn.public_status(renewed))


@router.post("/{session_id}/aws/renew-select", response_model=ConnectorStatusResponse)
async def aws_renew_select(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorStatusResponse:
    """After a fresh device auth, re-apply the session's previously-saved AWS selection.

    Lets renewal skip the manual account/role picker. 409 if there's no provisional
    cred (re-auth first) or no saved selection; 400 if a saved pair is no longer
    authorized (the frontend then falls back to the manual picker).
    """
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("aws")
    provisional = await get_credential_store().get(current_user.id, sid, "aws")
    if provisional is None or provisional.selection is not None:
        raise HTTPException(status_code=409, detail="No pending AWS authentication; re-initiate")
    aws_grant = next((g for g in await derive_prior_grants(db, sid) if g.connector == "aws"), None)
    selections = aws_selections_from_grant(aws_grant) if aws_grant else []
    if not selections:
        raise HTTPException(status_code=409, detail="No saved AWS selection; choose manually")
    return await _apply_aws_selection(
        db,
        user_id=current_user.id,
        sid=sid,
        conn=conn,
        provisional=provisional,
        selections=selections,
        event="renewed",
    )
