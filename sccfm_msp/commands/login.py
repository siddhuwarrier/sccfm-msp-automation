"""`sccfm-msp login` — verify an MSP portal API key and store it in the OS keyring."""

from __future__ import annotations

from scc_firewall_manager_sdk import MSPTenantManagementApi
from scc_firewall_manager_sdk.exceptions import ApiException

from ..client import msp_portal_client
from ..credentials import CredentialStore
from ..errors import MspCliError
from .base import Command, CommandResult, ItemOutcome


class StoreMspApiKeyCommand(Command):
    """Check that an MSP portal API key works, then save it securely.

    The key is verified before it is stored: a key that cannot list managed
    tenants is not an MSP portal key, and storing it would only produce a
    confusing failure later.
    """

    name = "login"

    def __init__(self, store: CredentialStore, portal_region: str, api_key: str) -> None:
        self.store = store
        self.portal_region = portal_region
        self.api_key = api_key.strip()

    def execute(self) -> CommandResult:
        if not self.api_key:
            raise MspCliError("The API key is empty.")

        tenant_count = self._verify()
        self.store.save_portal_key(self.portal_region, self.api_key)

        return CommandResult(
            summary=(
                f"MSP portal API key stored for region '{self.portal_region}'. "
                f"{tenant_count} managed tenant(s) visible."
            ),
            outcomes=[
                ItemOutcome.success(
                    f"region {self.portal_region}", "key verified and stored"
                )
            ],
            data={"portal_region": self.portal_region, "tenant_count": tenant_count},
        )

    def _verify(self) -> int:
        """Return the number of managed tenants, proving the key is an MSP key."""
        try:
            with msp_portal_client(self.portal_region, self.api_key) as api_client:
                page = MSPTenantManagementApi(api_client).get_msp_managed_tenants(limit="1")
        except ApiException as exc:
            if exc.status in (401, 403):
                raise MspCliError(
                    "Security Cloud Control rejected that API key "
                    f"(HTTP {exc.status}).\n"
                    "Check that it is an MSP portal key and that the region is right "
                    f"(you used '{self.portal_region}')."
                ) from exc
            raise MspCliError(
                f"Could not verify the API key (HTTP {exc.status}): {exc.reason}"
            ) from exc

        return page.count if page.count is not None else len(page.items or [])
