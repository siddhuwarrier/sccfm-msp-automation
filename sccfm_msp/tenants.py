"""Helpers for working with the MSP portal's managed tenants."""

from __future__ import annotations

import time
from typing import List

from scc_firewall_manager_sdk import (
    ApiClient,
    CdoTransaction,
    MSPTenantManagementApi,
    MspManagedTenantDto,
    TransactionsApi,
)

from .client import normalize_region
from .errors import MspCliError, TransactionFailedError

#: The API returns tenants a page at a time; this is how many we ask for per call.
PAGE_SIZE = 200

#: Terminal states for an asynchronous Security Cloud Control transaction.
TRANSACTION_DONE = "DONE"
TRANSACTION_ERROR = "ERROR"


def list_managed_tenants(api_client: ApiClient) -> List[MspManagedTenantDto]:
    """Every tenant the MSP portal manages, following pagination to the end."""
    api = MSPTenantManagementApi(api_client)
    tenants: List[MspManagedTenantDto] = []
    offset = 0

    while True:
        page = api.get_msp_managed_tenants(limit=str(PAGE_SIZE), offset=str(offset))
        batch = page.items or []
        tenants.extend(batch)

        # Stop when this page came back short, or we have everything the API counted.
        if len(batch) < PAGE_SIZE:
            break
        if page.count is not None and len(tenants) >= page.count:
            break
        offset += PAGE_SIZE

    return tenants


def region_of(tenant: MspManagedTenantDto) -> str:
    """The region a tenant lives in.

    This is not used for MSP-level calls, which always go to the portal's region.
    It decides the base URL for **tenant-level** calls — creating objects — so it
    is recorded when the tenant's API-only user is created.

    The API always populates this field, but be explicit rather than silently
    defaulting to the portal's region and later calling the wrong endpoint.
    """
    region = normalize_region(tenant.region)
    if not region:
        raise MspCliError(
            f"Tenant '{tenant.name}' did not report a region, so its API "
            "endpoint cannot be determined."
        )
    return region


def select_tenants(
    all_tenants: List[MspManagedTenantDto], wanted: List[str]
) -> List[MspManagedTenantDto]:
    """Narrow the tenant list to the names or UIDs the user asked for.

    Matching is case-insensitive on tenant name, display name, or UID, so the
    customer can paste whichever identifier they have on hand. Anything that
    matches nothing is an error — quietly skipping a typo would mean silently
    not deploying to a tenant.
    """
    if not wanted:
        return all_tenants

    by_key = {}
    for tenant in all_tenants:
        for key in (tenant.uid, tenant.name, tenant.display_name):
            if key:
                by_key[key.casefold()] = tenant

    selected, unknown = [], []
    for item in wanted:
        tenant = by_key.get(item.casefold())
        if tenant is None:
            unknown.append(item)
        elif tenant not in selected:
            selected.append(tenant)

    if unknown:
        available = ", ".join(sorted(t.name for t in all_tenants))
        raise MspCliError(
            "These tenants are not managed by this MSP portal: "
            f"{', '.join(unknown)}\nManaged tenants are: {available}"
        )

    return selected


def wait_for_transaction(
    api_client: ApiClient,
    transaction: CdoTransaction,
    timeout_seconds: float = 120.0,
    first_interval: float = 0.25,
    max_interval: float = 2.0,
) -> CdoTransaction:
    """Poll an async transaction until it reaches DONE, or raise.

    Adding a user to a tenant is asynchronous: the API hands back a transaction
    and the user does not exist yet. We need the finished transaction because its
    `entity_uid` is the UID of the user that was created.
    """
    if not transaction.transaction_uid:
        raise MspCliError("Security Cloud Control did not return a transaction to poll.")

    api = TransactionsApi(api_client)
    deadline = time.monotonic() + timeout_seconds
    interval = first_interval
    current = transaction

    while True:
        status = current.cdo_transaction_status

        if status == TRANSACTION_DONE:
            return current

        if status == TRANSACTION_ERROR:
            detail = current.error_message or current.error_details or "no detail given"
            raise TransactionFailedError(f"Transaction failed: {detail}")

        if time.monotonic() >= deadline:
            raise TransactionFailedError(
                f"Transaction {current.transaction_uid} was still '{status}' after "
                f"{timeout_seconds:.0f}s. It may still finish — re-run to pick it up."
            )

        # Check again soon, then ease off: most of these finish quickly, and a flat
        # two-second wait was dead time on every org.
        time.sleep(interval)
        interval = min(interval * 2, max_interval)
        current = api.get_transaction(current.transaction_uid)
