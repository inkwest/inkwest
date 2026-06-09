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
from app.services.connectors.base import (
    ConnectorError,
    ConnectorStatus,
    MintedCredential,
    generate_pkce,
)
from app.services.connectors.grants import aws_selections_from_grant, derive_prior_grants
from app.services.connectors.registry import all_connectors, get_connector
from app.services.connectors.store import get_credential_store
from app.services.eks_refresh import schedule_session_eks_refresh

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/connectors", tags=["connectors"])

_MAX_PENDING_STATES = 10000

# AWS device authorizations in flight, keyed by (user_id, session_id) -> deviceCode.
# Process RAM (same single-replica constraint as the credential store).
_aws_device_pending: dict[tuple[str, str], str] = {}


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
    if not get_settings().connector_audit_enabled:
        return
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
) -> ConnectorStatusResponse:
    """Mint STS creds for the given account/role pairs, persist, refresh EKS, audit.

    Shared tail of ``aws_select`` (manual pick) and ``aws_renew_select`` (saved pick).
    """
    try:
        cred = await conn.select_account_roles(provisional, selections=selections)
    except ConnectorError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    await get_credential_store().put(user_id, sid, cred)
    # Discover EKS clusters for the freshly minted profiles and generate one
    # (tmpfs) kubeconfig each — async, must not block the response.
    schedule_session_eks_refresh(sid, cred)
    await _audit(db, user_id=user_id, session_id=sid, connector="aws", action="select", cred=cred)
    return ConnectorStatusResponse.from_status(conn.public_status(cred))


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


@router.post("/{session_id}/coralogix/activate", response_model=ConnectorStatusResponse)
async def activate_coralogix(
    session_id: str,
    current_user: CurrentUser,
    db: AsyncSession = Depends(get_db),
) -> ConnectorStatusResponse:
    """Activate the fixed read-only Coralogix key for this session."""
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("coralogix")
    try:
        cred = await conn.activate(current_user.id, sid)
    except ConnectorError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    await get_credential_store().put(current_user.id, sid, cred)
    await _audit(
        db,
        user_id=current_user.id,
        session_id=sid,
        connector="coralogix",
        action="grant",
        cred=cred,
    )
    logger.info("connector_granted", extra={"connector": "coralogix", "session_id": str(sid)})
    return ConnectorStatusResponse.from_status(conn.public_status(cred))


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

    await get_credential_store().put(row.user_id, row.session_id, cred)
    await _audit(
        db,
        user_id=row.user_id,
        session_id=row.session_id,
        connector=row.connector,
        action="grant",
        cred=cred,
    )
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
    cred = await conn.provisional_from_token(token["access_token"], token["expires_in"])
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
            # Remove the tmpfs ~/.aws projection for this session.
            from app.tools.connector_exec import cleanup_aws_files

            cleanup_aws_files(sid)
        await _audit(
            db, user_id=current_user.id, session_id=sid, connector=connector, action="revoke"
        )
    return ConnectorStatusResponse.from_status(conn.public_status(None))


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
    """Silently renew GitLab from the held refresh token.

    409 when no live cred with a refresh token is held (token evicted on expiry):
    the frontend then falls back to the normal OAuth re-initiate.
    """
    _require_enabled()
    sid = await _verify_session_owner(db, session_id, current_user)
    conn = _get_enabled_connector("gitlab")
    cred = await get_credential_store().get(current_user.id, sid, "gitlab")
    if cred is None or not cred.secret.get("refresh_token"):
        raise HTTPException(
            status_code=409, detail="GitLab cannot be renewed silently; re-initiate"
        )
    try:
        renewed = await conn.refresh(cred)
    except ConnectorError as e:
        raise HTTPException(status_code=409, detail=str(e)) from e
    await get_credential_store().put(current_user.id, sid, renewed)
    await _audit(
        db,
        user_id=current_user.id,
        session_id=sid,
        connector="gitlab",
        action="grant",
        cred=renewed,
    )
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
    )
