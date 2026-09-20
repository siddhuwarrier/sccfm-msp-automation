"""`sccfm-msp tenants list` — show managed tenants and which ones are ready to use."""

from __future__ import annotations

from ..client import msp_portal_client, normalize_region
from ..credentials import CredentialStore
from ..tenants import list_managed_tenants
from .base import Command, CommandResult, ItemOutcome


class ListTenantsCommand(Command):
    """List the MSP portal's tenants, flagging which have a stored API-only user."""

    name = "tenants list"
    failures_are_errors = False  # "not ready" is information, not an error.

    def __init__(self, store: CredentialStore, portal_region: str) -> None:
        self.store = store
        self.portal_region = portal_region

    def execute(self) -> CommandResult:
        portal_key = self.store.load_portal_key(self.portal_region)

        with msp_portal_client(self.portal_region, portal_key) as api_client:
            tenants = list_managed_tenants(api_client)

        rows = []
        ready_count = 0
        for tenant in tenants:
            record = self.store.get_tenant_record(tenant.uid)
            # Each tenant's own region is what decides its base URL.
            region = normalize_region(tenant.region) or "unknown region"

            if record:
                ready_count += 1
                detail = f"region {region} — API-only user '{record.api_user_name}' ready"
            else:
                detail = f"region {region} — no API-only user yet"

            rows.append(
                ItemOutcome(
                    target=tenant.display_name or tenant.name,
                    ok=record is not None,
                    detail=detail,
                )
            )

        regions = sorted({normalize_region(t.region) for t in tenants if t.region})
        return CommandResult(
            summary=(
                f"{len(tenants)} managed tenant(s) across {len(regions)} region(s) "
                f"({', '.join(regions) or 'none'}); "
                f"{ready_count} ready for object creation."
            ),
            outcomes=rows,
            data={
                "tenant_count": len(tenants),
                "ready_count": ready_count,
                "regions": regions,
            },
        )
