"""`sccfm-msp api-users create` — create an API-only user in each managed tenant.

Every call this command makes is an **MSP-level** call, so all of them go to the
MSP portal's own region — including the ones that act on a tenant. A portal in
`US` provisions its `EU` tenant over the `US` endpoint.

For every tenant selected, this command:

1. adds an API-only user via the MSP portal (an asynchronous transaction),
2. waits for the transaction so it can learn the new user's UID,
3. generates that user's API token, and
4. stores the token in the OS keyring and records the tenant in the local index.

The token is shown exactly once by Security Cloud Control, at step 3. If it is
not captured there it cannot be retrieved again, so storing it immediately is
the point of this command.

Step 4 also records each tenant's own region. Nothing here uses it, but
`sccfm-msp objects create` does: tenant-level calls go to the tenant's region, not the
portal's, so the region is captured now to save a lookup later.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from scc_firewall_manager_sdk import (
    ApiClient,
    MSPTenantManagementApi,
    MSPUserManagementApi,
    MspAddUsersToTenantInput,
    MspManagedTenantDto,
    UserInput,
    UserRole,
)
from ..api_users import (
    DEFAULT_API_USER_NAME,
    find_api_only_user,
    step,
)
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

DEFAULT_ROLE = UserRole.ROLE_ADMIN


class _Provisioned(NamedTuple):
    """One tenant's result, kept structured so the summary needn't parse text."""

    outcome: ItemOutcome
    #: The tenant's region, or None if we failed before determining it.
    region: Optional[str] = None
    #: True only when a user was actually created on this run.
    created: bool = False


class CreateApiUsersCommand(Command):
    """Create and securely store one API-only user per managed tenant."""

    name = "api-users create"

    def __init__(
        self,
        store: CredentialStore,
        portal_region: str,
        username: str = DEFAULT_API_USER_NAME,
        role: UserRole = DEFAULT_ROLE,
        tenants: Optional[List[str]] = None,
        replace_existing: bool = False,
    ) -> None:
        self.store = store
        self.portal_region = portal_region
        self.username = username
        self.role = role
        self.tenants = tenants or []
        self.replace_existing = replace_existing

    def execute(self) -> CommandResult:
        portal_key = self.store.load_portal_key(self.portal_region)

        # One client, on the portal's region: every call below is an MSP-level
        # call, so the tenants' own regions are irrelevant here.
        with msp_portal_client(self.portal_region, portal_key) as portal:
            targets = select_tenants(list_managed_tenants(portal), self.tenants)
            if not targets:
                raise MspCliError("The MSP portal is not managing any tenants yet.")

            results = [self._provision(portal, tenant) for tenant in targets]

        created = sum(1 for r in results if r.created)
        regions = sorted({r.region for r in results if r.region})

        return CommandResult(
            summary=(
                f"Created {created} API-only user(s) across {len(targets)} tenant(s) "
                f"in {len(regions)} tenant region(s): {', '.join(regions) or 'none'}."
            ),
            outcomes=[r.outcome for r in results],
            data={
                "username": self.username,
                "role": self.role.value,
                "regions": regions,
            },
        )

    # ------------------------------------------------------------------

    def _provision(
        self, api_client: ApiClient, tenant: MspManagedTenantDto
    ) -> "_Provisioned":
        """Provision one tenant. Never raises — a bad tenant must not stop the rest."""
        label = tenant.display_name or tenant.name

        existing = self.store.get_tenant_record(tenant.uid)
        if existing and not self.replace_existing:
            return _Provisioned(
                outcome=ItemOutcome.success(
                    label,
                    f"already provisioned as '{existing.api_user_name}' "
                    f"(tenant region {existing.tenant_region}; "
                    "pass --replace-existing to redo)",
                ),
                region=existing.tenant_region,
                created=False,
            )

        # Read first, before any token exists. Not used to route these calls — it is
        # stored so `sccfm-msp objects create` can reach this tenant later. If the tenant
        # reports no region we stop here, rather than issuing a token we would then
        # have to throw away.
        try:
            tenant_region = region_of(tenant)
        except MspCliError as exc:
            return _Provisioned(ItemOutcome.failure(label, str(exc)))

        # Provisioning spans four different endpoints. Each one names itself when it
        # fails, because "HTTP 404" on its own cannot tell them apart.
        try:
            user_uid = self._add_api_only_user(api_client, tenant, tenant_region)
            token = self._generate_token(api_client, tenant, tenant_region, user_uid)
        except MspCliError as exc:
            return _Provisioned(
                ItemOutcome.failure(label, str(exc)), region=tenant_region
            )

        # Store the secret first, then the index entry, so the index never
        # advertises a tenant whose token we failed to keep.
        self.store.save_tenant_token(tenant.uid, token)
        self.store.record_tenant(
            TenantRecord.new(
                tenant_uid=tenant.uid,
                tenant_name=label,
                tenant_region=tenant_region,
                portal_region=self.portal_region,
                api_user_name=self.username,
                api_user_uid=user_uid,
            )
        )

        return _Provisioned(
            outcome=ItemOutcome.success(
                label,
                f"API-only user '{self.username}' created, token stored "
                f"(tenant region {tenant_region})",
            ),
            region=tenant_region,
            created=True,
        )

    def _add_api_only_user(
        self, api_client: ApiClient, tenant: MspManagedTenantDto, tenant_region: str
    ) -> str:
        """Add the user and return its UID once the transaction completes."""
        user_api = MSPUserManagementApi(api_client)

        with self._step(
            "adding the user",
            "POST /v1/msp/tenants/{uid}/users",
            tenant,
            tenant_region,
        ):
            transaction = user_api.add_users_to_tenant_in_msp_portal(
                tenant_uid=tenant.uid,
                msp_add_users_to_tenant_input=MspAddUsersToTenantInput(
                    users=[
                        UserInput(
                            username=self.username,
                            role=self.role,
                            api_only_user=True,
                        )
                    ]
                ),
            )

        with self._step(
            "polling the add-user transaction",
            "GET /v1/transactions/{uid}",
            tenant,
            tenant_region,
        ):
            wait_for_transaction(api_client, transaction)

        # The transaction's `entity_uid` is *not* the new user: it is "the entity the
        # transaction is triggered on", which for this call is the tenant. Using it
        # as the user UID produces a 404 from the token endpoint. The user's real UID
        # has to be looked up.
        return self._find_api_user_uid(api_client, tenant, tenant_region)

    def _find_api_user_uid(
        self, api_client: ApiClient, tenant: MspManagedTenantDto, tenant_region: str
    ) -> str:
        """Find the UID of the API-only user we just created, by name."""
        with self._step(
            f"looking up the new user '{self.username}'",
            "GET /v1/msp/tenants/{uid}/users/api-only",
            tenant,
            tenant_region,
        ):
            found, seen = find_api_only_user(
                MSPUserManagementApi(api_client), tenant, self.username
            )

        if found is not None:
            return found.uid

        raise MspCliError(
            f"created '{self.username}' but could not find it again to issue a "
            "token. API-only users returned: " + (", ".join(seen) or "none")
        )

    def _generate_token(
        self,
        api_client: ApiClient,
        tenant: MspManagedTenantDto,
        tenant_region: str,
        user_uid: str,
    ) -> str:
        with self._step(
            f"issuing the token for user {user_uid}",
            "POST /v1/msp/tenants/{uid}/users/{userUid}/token",
            tenant,
            tenant_region,
        ):
            info = MSPTenantManagementApi(
                api_client
            ).generate_api_token_for_user_in_tenant(
                tenant_uid=tenant.uid,
                api_user_uid=user_uid,
            )

        if not info.api_token:
            raise MspCliError("Security Cloud Control returned an empty API token.")
        return info.api_token

    def _step(
        self,
        what: str,
        path: str,
        tenant: MspManagedTenantDto,
        tenant_region: str,
    ):
        """Shared step wrapper, with this command's portal region filled in."""
        return step(what, path, tenant, self.portal_region, tenant_region)

