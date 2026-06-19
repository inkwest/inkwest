"""
AWS connector (IAM Identity Center) — Device Authorization Grant.

Flow:
  1. ``StartDeviceAuthorization`` → a user code + verification URL the user opens
     and approves in their browser (no redirect URI needed — IdC public clients
     only accept loopback redirects, so the device grant is the robust fit).
  2. Poll ``CreateToken`` until approved → SSO access token (provisional
     credential, ``needs_selection``).
  3. SSO portal APIs ``ListAccounts`` + ``ListAccountRoles`` surface the
     account/role pairs the user's SSO actually grants.
  4. ``GetRoleCredentials`` mints temporary STS credentials (~1h) for the chosen
     account+role.

Inkwest never escalates: the available accounts/roles — and therefore the minted
STS credentials — are capped by the user's own SSO assignments.
"""

import asyncio
import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import Any, ClassVar

from app.core.config import get_settings
from app.services.connectors.base import (
    Connector,
    ConnectorError,
    ConnectorStatus,
    ConnectorType,
    MintedCredential,
)

logger = logging.getLogger(__name__)

# Scope required to enumerate accounts/roles and mint role credentials.
_SSO_SCOPE = "sso:account:access"

# AWS SSO throttles ListAccountRoles aggressively: a burst of per-account calls
# (one round-trip each) trips 429 TooManyRequestsException, and at high fan-out
# boto3's default 10-connection pool also starts discarding connections. Cap the
# enumeration fan-out and let botocore's *adaptive* retry mode client-side
# rate-limit so a user with many accounts doesn't fail the whole grant.
_SSO_ENUM_CONCURRENCY = 8


def _sso_boto_config(max_pool: int) -> Any:
    """boto3 ``Config`` tuned for bursty SSO calls.

    ``adaptive`` retry mode adds client-side rate limiting that backs off on 429
    throttling (vs. the default ``standard`` mode, which just retries a few times
    and then surfaces the error). ``max_pool_connections`` is sized to the fan-out
    so concurrent calls reuse the pool instead of discarding connections.
    """
    from botocore.config import Config

    return Config(
        retries={"max_attempts": 10, "mode": "adaptive"},
        max_pool_connections=max(10, max_pool),
    )


def _profile_name(account_name: str, role_name: str) -> str:
    """Build a safe AWS profile name from an account + role (e.g. acme-prod-ReadOnly)."""
    raw = f"{account_name}-{role_name}"
    return re.sub(r"[^A-Za-z0-9_.-]", "_", raw)[:120]


@lru_cache(maxsize=8)
def _compiled_role_filter(pattern: str) -> re.Pattern[str]:
    """Compile (and cache) the role filter. Validity is enforced at startup."""
    return re.compile(pattern, re.IGNORECASE)


def _filter_by_role(
    accounts: list[dict[str, Any]],
    roles: list[dict[str, Any]],
    role_filter: str | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep only roles whose ``<account_name>.<role_name>`` matches the regex.

    Defense in depth, applied at SSO enumeration so a filtered role never reaches
    ``available_roles`` (and is therefore neither selectable nor mintable — the
    ``select_account_roles`` guard rejects anything not offered). Empty/unset
    filter is a pass-through; accounts left with no matching role are dropped so
    they don't surface in the modal. Case-insensitive, unanchored (the operator
    controls anchors, e.g. ``read-?only$``).
    """
    if not role_filter:
        return accounts, roles
    pat = _compiled_role_filter(role_filter)
    name_by_id = {a["account_id"]: a.get("account_name", a["account_id"]) for a in accounts}
    kept_roles = [
        r
        for r in roles
        if pat.search(f"{name_by_id.get(r['account_id'], r['account_id'])}.{r['role_name']}")
    ]
    kept_ids = {r["account_id"] for r in kept_roles}
    kept_accounts = [a for a in accounts if a["account_id"] in kept_ids]
    return kept_accounts, kept_roles


class AwsConnector(Connector):
    type: ClassVar[ConnectorType] = "aws"
    # AWS uses the device grant (not an OAuth redirect), so the REST layer drives
    # it via dedicated device endpoints rather than initiate/callback.
    requires_oauth: ClassVar[bool] = False

    @property
    def enabled(self) -> bool:
        s = get_settings()
        return bool(
            s.connectors_enabled
            and s.aws_sso_enabled
            and s.aws_sso_region
            and s.aws_sso_start_url
            and s.aws_sso_oidc_client_id
            and s.aws_sso_oidc_client_secret
        )

    async def start_device_auth(self) -> dict[str, Any]:
        """Begin the device flow. Returns the user code + verification URL to show."""
        s = get_settings()
        if not self.enabled:
            raise ConnectorError("AWS connector is not configured")
        return await asyncio.to_thread(
            _start_device_authorization,
            s.aws_sso_oidc_client_id,
            s.aws_sso_oidc_client_secret,
            s.aws_sso_start_url,
            s.aws_sso_region,
        )

    async def poll_device_token(self, device_code: str) -> dict[str, Any] | None:
        """Poll once. Returns {access_token, expires_in} if approved, None if pending.

        Raises ConnectorError on terminal failure (expired / denied).
        """
        s = get_settings()
        return await asyncio.to_thread(
            _create_token_device,
            s.aws_sso_oidc_client_id,
            s.aws_sso_oidc_client_secret,
            device_code,
            s.aws_sso_region,
        )

    async def enumerate_accounts_roles(
        self, access_token: str
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """List the accounts/roles the SSO token can assume (role-filter applied).

        The expensive part of a grant: one AWS round-trip per account. Callers cache
        the result on the warm grant so re-opening the picker doesn't re-enumerate.
        """
        s = get_settings()
        accounts, roles = await asyncio.to_thread(
            _list_accounts_and_roles, access_token, s.aws_sso_region
        )
        return _filter_by_role(accounts, roles, s.aws_sso_role_filter)

    async def provisional_from_token(
        self,
        access_token: str,
        expires_in: int,
        refresh_token: str | None = None,
        *,
        accounts: list[dict[str, Any]] | None = None,
        roles: list[dict[str, Any]] | None = None,
    ) -> MintedCredential:
        """Build the provisional (``needs_selection``) credential from the SSO token.

        ``accounts``/``roles`` skip enumeration when a fresh cached list is supplied
        (warm-grant cache); otherwise the SSO portal is queried.
        """
        s = get_settings()
        if accounts is None or roles is None:
            accounts, roles = await self.enumerate_accounts_roles(access_token)
        return MintedCredential(
            connector="aws",
            scope=_SSO_SCOPE,
            expires_at=datetime.now(UTC) + timedelta(seconds=expires_in),
            secret={
                "sso_access_token": access_token,
                "sso_refresh_token": refresh_token,
                "region": s.aws_sso_region,
            },
            selection=None,  # awaiting account/role choice
            available_accounts=accounts,
            available_roles=roles,
        )

    async def select_account_roles(
        self,
        provisional: MintedCredential,
        *,
        selections: list[dict[str, str]],
    ) -> MintedCredential:
        """Mint STS credentials for several account/role pairs at once.

        Each selection is ``{"account_id", "role_name"}``. One AWS profile is built
        per pair; the profiles live in RAM (store) and are projected to a tmpfs
        ``~/.aws`` file at execution time.
        """
        s = get_settings()
        access_token = provisional.secret.get("sso_access_token")
        if not access_token:
            raise ConnectorError("No SSO access token; re-authenticate")

        offered = {(r["account_id"], r["role_name"]) for r in (provisional.available_roles or [])}
        name_by_id = {
            a["account_id"]: a.get("account_name", a["account_id"])
            for a in (provisional.available_accounts or [])
        }
        if not selections:
            raise ConnectorError("No account/role selected")

        picks: list[dict[str, str]] = []
        for sel in selections:
            account_id = sel["account_id"]
            role_name = sel["role_name"]
            # Defense in depth: only mint pairs the user was actually offered.
            if (account_id, role_name) not in offered:
                raise ConnectorError(f"Account/role not authorized: {account_id}/{role_name}")
            picks.append(
                {
                    "account_id": account_id,
                    "role_name": role_name,
                    "account_name": name_by_id.get(account_id, account_id),
                }
            )

        profiles, min_expiration = await _mint_sts_profiles(access_token, picks, s.aws_sso_region)
        return MintedCredential(
            connector="aws",
            scope=f"{len(profiles)} compte(s)/rôle(s) (read-only)",
            expires_at=datetime.fromtimestamp(min_expiration or 0, tz=UTC),
            # Keep the SSO token: it outlives the ~1h STS creds (~8h) and lets
            # remint_profiles() silently re-derive them without a new device flow.
            secret={
                "region": s.aws_sso_region,
                "profiles": profiles,
                "sso_access_token": access_token,
                "sso_refresh_token": provisional.secret.get("sso_refresh_token"),
            },
            renew_until=provisional.expires_at,  # SSO token lifetime
            selection={
                "profiles": [
                    {
                        "name": p["name"],
                        "account_id": p["account_id"],
                        "account_name": p["account_name"],
                        "role_name": p["role_name"],
                    }
                    for p in profiles
                ]
            },
        )

    async def remint_profiles(self, cred: MintedCredential) -> MintedCredential:
        """Silently re-mint the STS profiles of an expired credential.

        Works while the held SSO access token is still alive (``renew_until``):
        same account/role selection (validated at the original pick), fresh ~1h
        STS creds, no device flow and no popup.
        """
        s = get_settings()
        access_token = cred.secret.get("sso_access_token")
        picked = (cred.selection or {}).get("profiles") or []
        if not access_token or not picked:
            raise ConnectorError("No SSO token or saved selection; re-authenticate")
        if not cred.is_renewable():
            raise ConnectorError("SSO session expired; re-authenticate")

        profiles, min_expiration = await _mint_sts_profiles(access_token, picked, s.aws_sso_region)
        return MintedCredential(
            connector="aws",
            scope=cred.scope,
            expires_at=datetime.fromtimestamp(min_expiration or 0, tz=UTC),
            secret={
                "region": s.aws_sso_region,
                "profiles": profiles,
                "sso_access_token": access_token,
                "sso_refresh_token": cred.secret.get("sso_refresh_token"),
            },
            renew_until=cred.renew_until,  # still bounded by the SSO token lifetime
            selection=cred.selection,
        )

    async def revoke(self, cred: MintedCredential) -> None:
        # STS session credentials cannot be invalidated server-side before expiry;
        # dropping them from the RAM store is the effective revocation.
        return None

    def public_status(self, cred: MintedCredential | None) -> ConnectorStatus:
        if cred is None:
            return ConnectorStatus(connector="aws", state="not_connected")
        if cred.is_expired():
            return ConnectorStatus(connector="aws", state="expired")
        if cred.selection is None:
            return ConnectorStatus(
                connector="aws",
                state="needs_selection",
                available_accounts=cred.available_accounts,
                available_roles=cred.available_roles,
            )
        return ConnectorStatus(
            connector="aws",
            state="connected",
            expires_at=cred.expires_at,
            scope=cred.scope,
            selection=cred.selection,
        )


# Each pick is an independent SSO ``GetRoleCredentials`` call, so they mint
# concurrently rather than serially (a "tout cocher" over dozens of accounts was
# N sequential round-trips). A *dedicated* pool — not the shared default
# ``asyncio.to_thread`` executor — caps total in-flight STS calls process-wide
# (8), so a burst of grants can't starve unrelated blocking work (kubectl exec,
# other connectors); the pool size is the de-facto concurrency bound. Sized small
# because GetRoleCredentials is fast and we'd rather not poke SSO throttling.
_STS_MINT_CONCURRENCY = 8
_STS_EXECUTOR = ThreadPoolExecutor(max_workers=_STS_MINT_CONCURRENCY, thread_name_prefix="sts-mint")


def _build_sso_client(region: str | None) -> Any:
    """Construct one SSO client (sync; off the event loop).

    boto3 *client creation* on the shared default session is not guaranteed
    thread-safe, so we build a single client per mint batch and share it across
    the concurrent calls (the low-level client's *methods* are thread-safe — same
    pattern as ``_list_accounts_and_roles``).
    """
    import boto3

    return boto3.client("sso", region_name=region, config=_sso_boto_config(_STS_MINT_CONCURRENCY))


async def _mint_sts_profiles(
    access_token: str, picks: list[dict[str, str]], region: str | None
) -> tuple[list[dict[str, Any]], float | None]:
    """Mint one STS profile per account/role pick. Returns (profiles, min expiration).

    Picks are minted concurrently (bounded by ``_STS_EXECUTOR``); the returned
    profile list preserves the input order. All-or-nothing: if *any* pick fails the
    whole batch raises (no partial grant) — matching the original serial behaviour.
    Authorization is the caller's concern: ``select_account_roles`` validates picks
    against the offered list; ``remint_profiles`` replays an already-validated
    selection.
    """
    loop = asyncio.get_running_loop()
    sso = await loop.run_in_executor(_STS_EXECUTOR, _build_sso_client, region)

    async def mint(pick: dict[str, str]) -> dict[str, Any]:
        account_id = pick["account_id"]
        role_name = pick["role_name"]
        creds = await loop.run_in_executor(
            _STS_EXECUTOR, _get_role_credentials, sso, access_token, account_id, role_name
        )
        account_name = pick.get("account_name", account_id)
        return {
            "name": _profile_name(account_name, role_name),
            "account_id": account_id,
            "account_name": account_name,
            "role_name": role_name,
            "access_key_id": creds["accessKeyId"],
            "secret_access_key": creds["secretAccessKey"],
            "session_token": creds["sessionToken"],
            "region": region,
            "expiration": creds["expiration"] / 1000,
        }

    # return_exceptions=True so one failing pick doesn't leave its siblings as
    # orphaned "exception never retrieved" tasks; we then fail the whole batch.
    minted = await asyncio.gather(*(mint(p) for p in picks), return_exceptions=True)
    errors = [r for r in minted if isinstance(r, BaseException)]
    if errors:
        raise ConnectorError(
            f"Failed to mint {len(errors)}/{len(picks)} STS credential(s): {errors[0]}"
        ) from errors[0]

    min_expiration: float | None = None
    profiles: list[dict[str, Any]] = []
    for p in minted:
        assert isinstance(p, dict)  # narrows: errors were raised above
        exp = p.pop("expiration")
        min_expiration = exp if min_expiration is None else min(min_expiration, exp)
        profiles.append(p)
    return profiles, min_expiration


def _start_device_authorization(
    client_id: str | None, client_secret: str | None, start_url: str | None, region: str | None
) -> dict[str, Any]:
    """Start the device authorization (sync; run in a thread)."""
    import boto3

    oidc: Any = boto3.client("sso-oidc", region_name=region, config=_sso_boto_config(10))
    resp = oidc.start_device_authorization(
        clientId=client_id, clientSecret=client_secret, startUrl=start_url
    )
    return {
        "device_code": resp["deviceCode"],
        "user_code": resp["userCode"],
        "verification_uri": resp["verificationUri"],
        "verification_uri_complete": resp.get("verificationUriComplete", resp["verificationUri"]),
        "interval": int(resp.get("interval", 5)),
        "expires_in": int(resp.get("expiresIn", 600)),
    }


def _create_token_device(
    client_id: str | None, client_secret: str | None, device_code: str, region: str | None
) -> dict[str, Any] | None:
    """Poll CreateToken once. Returns token dict, None if pending, raises on terminal.

    Branches on the AWS error *code* (robust across boto3 versions) rather than on
    modeled exception classes:
      - authorization_pending / slow_down → None (keep polling)
      - invalid_grant (device code already redeemed by a concurrent poll) → None
        (the caller then reports the already-stored credential's status)
      - expired_token / access_denied → terminal failure
    """
    import boto3
    from botocore.exceptions import ClientError

    oidc: Any = boto3.client("sso-oidc", region_name=region, config=_sso_boto_config(10))
    try:
        resp = oidc.create_token(
            clientId=client_id,
            clientSecret=client_secret,
            grantType="urn:ietf:params:oauth:grant-type:device_code",
            deviceCode=device_code,
        )
        return {
            "access_token": resp["accessToken"],
            "expires_in": int(resp.get("expiresIn", 8 * 3600)),
            # Only present when the OIDC client was registered with the
            # refresh_token grant type; captured for future silent SSO renewal.
            "refresh_token": resp.get("refreshToken"),
        }
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("AuthorizationPendingException", "SlowDownException", "InvalidGrantException"):
            return None
        if code in ("ExpiredTokenException", "AccessDeniedException"):
            raise ConnectorError(f"device authorization failed: {code}") from e
        raise ConnectorError(f"AWS create_token error: {code or type(e).__name__}") from e


def _list_accounts_and_roles(
    access_token: str, region: str | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """List accounts and their roles via the SSO portal (sync; run in a thread).

    Roles are listed **per account** — one AWS round-trip each — so we fan those out
    in parallel (the boto3 low-level client is thread-safe) to avoid a slow N×latency
    serial walk when the user has many accounts.
    """
    from concurrent.futures import ThreadPoolExecutor

    import boto3

    sso: Any = boto3.client(
        "sso", region_name=region, config=_sso_boto_config(_SSO_ENUM_CONCURRENCY)
    )
    accounts: list[dict[str, Any]] = []
    for page in sso.get_paginator("list_accounts").paginate(accessToken=access_token):
        for acct in page.get("accountList", []):
            accounts.append(
                {
                    "account_id": acct["accountId"],
                    "account_name": acct.get("accountName", acct["accountId"]),
                }
            )

    def roles_for(account_id: str) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for rpage in sso.get_paginator("list_account_roles").paginate(
            accessToken=access_token, accountId=account_id
        ):
            for role in rpage.get("roleList", []):
                out.append({"account_id": account_id, "role_name": role["roleName"]})
        return out

    roles: list[dict[str, Any]] = []
    if accounts:
        with ThreadPoolExecutor(max_workers=min(_SSO_ENUM_CONCURRENCY, len(accounts))) as ex:
            for res in ex.map(roles_for, [a["account_id"] for a in accounts]):
                roles.extend(res)
    return accounts, roles


def _get_role_credentials(
    sso: Any, access_token: str, account_id: str, role_name: str
) -> dict[str, Any]:
    """Mint temporary STS credentials for an account/role (sync; run in a thread).

    Takes a pre-built SSO client so a batch shares one client across threads
    (see ``_build_sso_client`` — client *creation* isn't thread-safe).
    """
    resp = sso.get_role_credentials(
        accessToken=access_token, accountId=account_id, roleName=role_name
    )
    creds: dict[str, Any] = resp["roleCredentials"]
    return creds
