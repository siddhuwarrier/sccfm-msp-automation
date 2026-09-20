"""Shared plumbing for the API-only user commands.

`api-users create` and `api-users delete` both have to deal with the same two
awkward facts about Security Cloud Control's user API, so the handling lives here
rather than being written twice:

* **Usernames are not echoed back verbatim.** A user created as `msp-automation`
  is reported as `msp-automation@CDO-tenant-name`, so a lookup cannot compare the
  whole string. Delete is the sharper case: it identifies users *by name*, so
  sending the unqualified name would target the wrong thing or nothing at all.

* **Failures need to say which call failed.** Provisioning and removal each span
  several endpoints, and a bare `HTTP 404` cannot distinguish them.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Generator, List, Optional, Tuple

from scc_firewall_manager_sdk import (
    MSPUserManagementApi,
    MspManagedTenantDto,
    User,
)
from scc_firewall_manager_sdk.exceptions import ApiException

from .errors import MspCliError, server_message

DEFAULT_API_USER_NAME = "msp-automation"

#: How many API-only users to fetch per page when looking one up by name.
USER_PAGE_SIZE = 200


def matches_username(reported_name: Optional[str], wanted: str) -> bool:
    """Does a listed user correspond to the name we asked for?

    Compares the part before the `@`, since the API qualifies the name with the
    tenant. The whole string is compared too, in case a caller passed a name that
    was already qualified.
    """
    name = (reported_name or "").strip().casefold()
    target = (wanted or "").strip().casefold()
    return bool(target) and (name == target or name.split("@", 1)[0] == target)


def qualified_name(username: str, tenant: MspManagedTenantDto) -> str:
    """The name the API is expected to report for `username` in this tenant."""
    return f"{username}@{tenant.name}"


def find_api_only_user(
    user_api: MSPUserManagementApi,
    tenant: MspManagedTenantDto,
    username: str,
) -> Tuple[Optional[User], List[str]]:
    """Find one API-only user by name, returning it and every name considered.

    Takes the API object rather than a client so the caller owns that dependency —
    which also keeps it substitutable in tests.

    The query narrows server-side; the rows are still matched locally, because a
    Lucene query can return near matches too. The names considered come back so a
    caller reporting "not found" can say what it did see.
    """
    page = user_api.get_api_only_users_in_msp_managed_tenant(
        tenant_uid=tenant.uid,
        limit=str(USER_PAGE_SIZE),
        q=f"name:{qualified_name(username, tenant)}",
    )

    candidates = page.items or []
    seen = [user.name or "?" for user in candidates]

    for user in candidates:
        if matches_username(user.name, username):
            return user, seen

    return None, seen


@contextmanager
def step(
    what: str,
    path: str,
    tenant: MspManagedTenantDto,
    portal_region: str,
    tenant_region: str,
) -> Generator[None, None, None]:
    """Tag any API failure inside the block with the call that caused it."""
    try:
        yield
    except ApiException as exc:
        raise MspCliError(
            f"failed {what} ({path}): "
            + describe_error(exc, tenant, portal_region, tenant_region)
        ) from exc


def describe_error(
    exc: Exception,
    tenant: MspManagedTenantDto,
    portal_region: str,
    tenant_region: str,
) -> str:
    """Explain a failed call, including what the server actually said."""
    if not isinstance(exc, ApiException):
        return str(exc)

    if exc.status == 409:
        return (
            "a user with that name already exists in this tenant "
            "(use --username to pick a different one)"
        )

    if exc.status in (401, 403):
        return (
            f"the MSP portal key is not allowed to do this (HTTP {exc.status})"
            + server_message(exc)
        )

    if exc.status == 404:
        hint = (
            f"HTTP 404 on the {portal_region} endpoint for tenant {tenant.uid}"
            + server_message(exc)
        )
        if tenant_region != portal_region:
            hint += (
                f". Note this tenant reports region {tenant_region}, not "
                f"{portal_region} — if MSP calls for a tenant must go to the "
                "tenant's own region, that would explain the 404"
            )
        return hint

    return f"HTTP {exc.status}: {exc.reason}" + server_message(exc)
