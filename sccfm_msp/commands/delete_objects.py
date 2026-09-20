"""`sccfm-msp objects delete` — remove an object by name from many managed tenants.

The mirror image of `objects create`, and region aware for the same reason: this is
a **tenant-level** call, so each one goes to the base URL for the region that tenant
lives in, using that tenant's own API-only user token.

The API deletes by UID, and UIDs are per-tenant, so the name is resolved separately
in every tenant before its own delete is issued.

Unlike `--member` on `objects create`, a missing object is **not** a reason to abort
the run. There a missing reference would make the payload wrong; here it just means
that tenant is already in the desired state, so it is reported and the others carry
on. That makes the command safe to re-run over a partly-cleaned estate.
"""

from __future__ import annotations

from typing import List, NamedTuple, Optional

from scc_firewall_manager_sdk import ObjectManagementApi
from scc_firewall_manager_sdk.exceptions import ApiException

from ..client import tenant_client
from ..credentials import CredentialStore, TenantRecord
from ..errors import MspCliError, server_message
from ..objects import find_object_by_name
from .base import Command, CommandResult, ItemOutcome

DELETE_PATH = "/v1/objects/{uid}"


class _Removed(NamedTuple):
    """One tenant's result, kept structured so the summary needn't parse text."""

    outcome: ItemOutcome
    #: True only when an object was actually deleted from the tenant on this run.
    deleted: bool = False


class DeleteObjectsCommand(Command):
    """Delete an object by name from all, or a subset of, managed tenants."""

    name = "objects delete"

    def __init__(
        self,
        store: CredentialStore,
        portal_region: str,
        object_name: str,
        tenants: Optional[List[str]] = None,
        force: bool = False,
    ) -> None:
        self.store = store
        self.portal_region = portal_region
        self.object_name = object_name
        self.tenants = tenants or []
        self.force = force

    def execute(self) -> CommandResult:
        targets = self._resolve_targets()
        results = [self._delete_in_tenant(record) for record in targets]

        deleted = sum(1 for r in results if r.deleted)
        regions = sorted({record.tenant_region for record in targets})

        return CommandResult(
            summary=(
                f"Deleted '{self.object_name}' from {deleted} of {len(targets)} "
                f"tenant(s) across {len(regions)} region(s): {', '.join(regions)}."
            ),
            outcomes=[r.outcome for r in results],
            data={
                "object_name": self.object_name,
                "deleted": deleted,
                "regions": regions,
            },
        )

    # ------------------------------------------------------------------

    def _resolve_targets(self) -> List[TenantRecord]:
        """Tenants with a stored API-only user, narrowed by --tenant."""
        provisioned = self.store.provisioned_tenants(self.portal_region)

        if not provisioned:
            raise MspCliError(
                "No tenant managed by this MSP portal has an API-only user yet, so "
                "there is no credential to delete objects with.\n"
                f"Run this first:  sccfm-msp api-users create --region {self.portal_region}"
            )

        if not self.tenants:
            return provisioned

        by_key = {}
        for record in provisioned:
            by_key[record.tenant_uid.casefold()] = record
            by_key[record.tenant_name.casefold()] = record

        selected, missing = [], []
        for wanted in self.tenants:
            record = by_key.get(wanted.casefold())
            if record is None:
                missing.append(wanted)
            elif record not in selected:
                selected.append(record)

        if missing:
            ready = ", ".join(f"{r.tenant_name} ({r.tenant_region})" for r in provisioned)
            raise MspCliError(
                "These tenants have no stored API-only user, so objects cannot be "
                f"deleted from them: {', '.join(missing)}\n"
                f"Tenants ready to use: {ready}"
            )

        return selected

    def _delete_in_tenant(self, record: TenantRecord) -> _Removed:
        """One tenant: resolve the name, then delete it. Never raises."""
        context = f"tenant {record.tenant_uid} (region {record.tenant_region})"

        try:
            token = self.store.load_tenant_token(record.tenant_uid)
        except MspCliError as exc:
            return _Removed(ItemOutcome.failure(record.tenant_name, f"{context}: {exc}"))

        try:
            # record.tenant_region — not the portal's region — picks the base URL.
            with tenant_client(record.tenant_region, token) as api_client:
                api = ObjectManagementApi(api_client)

                found = find_object_by_name(api, self.object_name)
                if found is None:
                    return _Removed(
                        ItemOutcome.success(
                            record.tenant_name,
                            f"region {record.tenant_region} — no object named "
                            f"'{self.object_name}' — nothing to do",
                        )
                    )

                api.delete_object(uid=found.uid, forced_delete=self.force or None)
        except ApiException as exc:
            return _Removed(
                ItemOutcome.failure(
                    record.tenant_name,
                    f"{context}: DELETE {DELETE_PATH} failed — "
                    + _describe_api_error(exc),
                )
            )
        except MspCliError as exc:
            return _Removed(ItemOutcome.failure(record.tenant_name, f"{context}: {exc}"))

        return _Removed(
            ItemOutcome.success(
                record.tenant_name,
                f"region {record.tenant_region} — deleted uid {found.uid}",
            ),
            deleted=True,
        )


def _describe_api_error(exc: ApiException) -> str:
    if exc.status == 400:
        return (
            "Security Cloud Control refused the delete — the object may still be in "
            "use; --force covers the in-use-by-another-object case"
            + server_message(exc)
        )
    if exc.status in (401, 403):
        return (
            f"the stored API-only user token was rejected (HTTP {exc.status}) — "
            "it may have been revoked; re-run 'sccfm-msp api-users create --replace-existing'"
            + server_message(exc)
        )
    if exc.status == 404:
        return "the object was gone by the time we tried to delete it"
    return f"HTTP {exc.status}: {exc.reason}" + server_message(exc)
