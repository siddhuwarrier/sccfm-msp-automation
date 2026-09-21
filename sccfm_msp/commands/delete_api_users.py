"""`sccfm-msp api-users delete` — remove the API-only user this CLI created in each tenant.

The mirror image of `api-users create`, and it only ever touches users this CLI
recorded: the local index says which tenant holds which user, so a run cannot
delete somebody else's automation account by accident.

Per tenant, all against the MSP portal's own region because these are MSP-level
calls:

1. look the recorded user up, to get the exact name the API knows it by,
2. delete it (an asynchronous transaction, so wait for it),
3. drop the stored token and the index entry.

Step 1 matters more here than in `create`. The delete endpoint identifies users by
**name**, and the API reports names qualified as `requested-name@CDO-tenant-name`.
Sending the unqualified name would delete nothing, or — worse, if a plain name ever
did match — something unintended. So the name is read back from the API rather than
reconstructed.

A user already gone from the tenant is not an error: the local credential is still
cleaned up, so a half-finished delete can simply be re-run.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional

from scc_firewall_manager_sdk import (
    ApiClient,
    MSPUserManagementApi,
    MspDeleteUsersFromTenantInput,
    MspManagedTenantDto,
)

from ..api_users import find_api_only_user, step
from ..client import msp_portal_client
from ..credentials import CredentialStore, TenantRecord
from ..errors import MspCliError
from ..tenants import (
    list_managed_tenants,
    region_of,
    select_tenants,
    wait_for_transaction,
)
from .base import Command, CommandResult, ItemOutcome

DELETE_PATH = "/v1/msp/tenants/{uid}/users/delete"


class _Removed(NamedTuple):
    """One tenant's result, kept structured so the summary needn't parse text."""

    outcome: ItemOutcome
    #: True only when a user was actually deleted from the tenant on this run.
    deleted: bool = False


class DeleteApiUsersCommand(Command):
    """Delete the recorded API-only user from each managed tenant."""

    name = "api-users delete"

    def __init__(
        self,
        store: CredentialStore,
        portal_region: str,
        tenants: Optional[List[str]] = None,
    ) -> None:
        self.store = store
        self.portal_region = portal_region
        self.tenants = tenants or []

    def execute(self) -> CommandResult:
        portal_key = self.store.load_portal_key(self.portal_region)
        records = self._recorded()

        # One client on the portal's region: every call below is MSP-level.
        with msp_portal_client(self.portal_region, portal_key) as portal:
            targets = select_tenants(list_managed_tenants(portal), self.tenants)
            if not targets:
                raise MspCliError("This Manager Org is not managing any orgs yet.")

            self.progress.start(len(targets))
            results = []
            for tenant in targets:
                removed = self._remove(portal, tenant, records)
                self.progress.item(removed.outcome)
                results.append(removed)

        deleted = sum(1 for r in results if r.deleted)
        return CommandResult(
            summary=(
                f"Deleted {deleted} API-only user(s) across "
                f"{len(targets)} tenant(s) in region '{self.portal_region}'."
            ),
            outcomes=[r.outcome for r in results],
            data={"deleted": deleted, "portal_region": self.portal_region},
        )

    # ------------------------------------------------------------------

    def _recorded(self) -> Dict[str, TenantRecord]:
        """Tenant UID -> record, for every tenant this portal has provisioned."""
        records = {
            record.tenant_uid: record
            for record in self.store.provisioned_tenants(self.portal_region)
        }
        if not records:
            raise MspCliError(
                "No Managed Org has a recorded API-only user, "
                "so there is nothing to delete.\n"
                f"'sccfm-msp api-users create --region {self.portal_region}' creates them."
            )
        return records

    def _remove(
        self,
        api_client: ApiClient,
        tenant: MspManagedTenantDto,
        records: Dict[str, TenantRecord],
    ) -> _Removed:
        """Remove one tenant's user. Never raises, so one failure isn't fatal."""
        label = tenant.display_name or tenant.name
        record = records.get(tenant.uid)

        if record is None:
            # Selected but never provisioned by this CLI: leave it alone.
            return _Removed(
                ItemOutcome.success(label, "no API-only user recorded — nothing to do")
            )

        try:
            tenant_region = region_of(tenant)
        except MspCliError as exc:
            return _Removed(ItemOutcome.failure(label, str(exc)))

        try:
            exact_name = self._delete_user(api_client, tenant, tenant_region, record)
        except MspCliError as exc:
            return _Removed(ItemOutcome.failure(label, str(exc)))

        # Local cleanup happens whether or not the user was still there, so a
        # partially-completed delete can just be re-run.
        self.store.forget_tenant_token(tenant.uid)
        self.store.forget_tenant(tenant.uid)

        if exact_name is None:
            return _Removed(
                ItemOutcome.success(
                    label,
                    f"'{record.api_user_name}' was already gone — "
                    "stored token and record removed",
                )
            )

        return _Removed(
            ItemOutcome.success(
                label, f"deleted '{exact_name}' — stored token and record removed"
            ),
            deleted=True,
        )

    def _delete_user(
        self,
        api_client: ApiClient,
        tenant: MspManagedTenantDto,
        tenant_region: str,
        record: TenantRecord,
    ) -> Optional[str]:
        """Delete the recorded user, returning the name used, or None if absent."""
        username = record.api_user_name

        self.progress.step(tenant.display_name or tenant.name, f"looking up '{username}'")

        with step(
            f"looking up '{username}'",
            "GET /v1/msp/tenants/{uid}/users/api-only",
            tenant,
            self.portal_region,
            tenant_region,
        ):
            found, _seen = find_api_only_user(
                MSPUserManagementApi(api_client), tenant, username
            )

        if found is None:
            return None

        # The API deletes by name, and the name it knows is the qualified one.
        exact_name = found.name or username

        self.progress.step(tenant.display_name or tenant.name, "deleting the user")

        with step(
            f"deleting '{exact_name}'",
            DELETE_PATH,
            tenant,
            self.portal_region,
            tenant_region,
        ):
            transaction = MSPUserManagementApi(
                api_client
            ).delete_users_from_tenant_in_msp_portal(
                tenant_uid=tenant.uid,
                msp_delete_users_from_tenant_input=MspDeleteUsersFromTenantInput(
                    usernames=[exact_name]
                ),
            )

        with step(
            "polling the delete transaction",
            "GET /v1/transactions/{uid}",
            tenant,
            self.portal_region,
            tenant_region,
        ):
            wait_for_transaction(api_client, transaction)

        return exact_name
