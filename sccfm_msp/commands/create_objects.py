"""`sccfm-msp objects create` — create one object in many managed tenants.

The object's type and values come from the command line: a single object such as
`NETWORK_OBJECT` takes one `--value`, while a group such as `NETWORK_GROUP` takes
several. Turning those flags into an SDK payload is `sccfm_msp/objects.py`'s job.

A group can also reference **existing** objects and groups by name, via `--member`.
Those references are UIDs in the payload, and a UID only means something inside one
tenant — so the same member name resolves to a different UID in every tenant. That
shapes the command:

1. **Validate.** Every member name is looked up in *every* target tenant. If any
   tenant is missing any member, nothing is created anywhere.
2. **Create.** Each tenant gets a request carrying its own resolved member UIDs.

This command makes **one write call per tenant**, and each call is region aware: it
goes to the base URL for the region that tenant lives in, using that tenant's own
API-only user token. Both facts come from the record written by `sccfm-msp api-users
create`, so no extra lookup against the portal is needed.

It deliberately does not fall back to the MSP portal key: the portal key manages
tenants, it does not write objects inside them. A tenant with no stored API-only
user therefore cannot be targeted, and the command says so instead of guessing.
"""

from __future__ import annotations

import json
from typing import Dict, List, Optional, Sequence

from scc_firewall_manager_sdk import ApiClient, CreateRequest, ObjectManagementApi
from scc_firewall_manager_sdk.exceptions import ApiException

from ..client import host_for_region, tenant_client
from ..credentials import CredentialStore, TenantRecord
from ..errors import MspCliError, server_message
from ..objects import (
    build_create_request,
    check_inputs,
    describe_values,
    family_of,
    find_object_by_name,
    normalize_object_type,
)
from .base import Command, CommandResult, ItemOutcome

#: Endpoint objects are created on, named in output so failures are traceable.
OBJECTS_PATH = "/v1/objects"

#: tenant UID -> the member UIDs resolved inside that tenant.
MemberUids = Dict[str, List[str]]


class CreateObjectsCommand(Command):
    """Create the same object across all, or a subset of, managed tenants."""

    name = "objects create"

    def __init__(
        self,
        store: CredentialStore,
        portal_region: str,
        object_name: str,
        object_type: str,
        values: Sequence[str] = (),
        members: Sequence[str] = (),
        description: Optional[str] = None,
        tenants: Optional[List[str]] = None,
        dry_run: bool = False,
    ) -> None:
        self.store = store
        self.portal_region = portal_region
        self.object_name = object_name
        self.object_type = object_type
        self.values = list(values)
        self.members = list(members)
        self.description = description
        self.tenants = tenants or []
        self.dry_run = dry_run

    def execute(self) -> CommandResult:
        # Cheap checks first: a bad flag combination should fail before any call.
        object_type = normalize_object_type(self.object_type)
        check_inputs(object_type, self.values, self.members)

        targets = self._resolve_targets()

        # Resolve members everywhere before creating anything. Read-only, so it also
        # runs under --dry-run, where it doubles as the existence check.
        member_uids = self._resolve_members_everywhere(targets)

        if self.dry_run:
            return self._describe_dry_run(object_type, targets, member_uids)

        self.progress.start(len(targets))
        outcomes = []
        for record in targets:
            outcome = self._create_in_tenant(record, member_uids.get(record.tenant_uid, []))
            self.progress.item(outcome)
            outcomes.append(outcome)
        created = sum(1 for outcome in outcomes if outcome.ok)
        regions = sorted({record.tenant_region for record in targets})

        return CommandResult(
            summary=(
                f"Created {object_type} '{self.object_name}' "
                f"({self._describe_contents(object_type)}) in {created} of "
                f"{len(targets)} tenant(s) across {len(regions)} region(s): "
                f"{', '.join(regions)}."
            ),
            outcomes=outcomes,
            data={
                "object_name": self.object_name,
                "object_type": object_type,
                "values": self.values,
                "members": self.members,
                "regions": regions,
            },
        )

    # ------------------------------------------------------------------
    # Describing what was asked for
    # ------------------------------------------------------------------

    def _describe_contents(self, object_type: str) -> str:
        parts = []
        if self.values:
            parts.append(describe_values(object_type, self.values))
        if self.members:
            parts.append(f"{len(self.members)} member(s)")
        return " + ".join(parts)

    def _describe_dry_run(
        self, object_type: str, targets: List[TenantRecord], member_uids: MemberUids
    ) -> CommandResult:
        """Show the exact request body and where it would go, without writing."""
        outcomes = [
            ItemOutcome.success(
                record.tenant_name,
                f"uid {record.tenant_uid} — region {record.tenant_region} — would "
                f"POST {OBJECTS_PATH} to {host_for_region(record.tenant_region)}",
            )
            for record in targets
        ]

        data = {"object_type": object_type, "member_uids": member_uids}

        if self.members:
            # Member UIDs differ per tenant, so the bodies do too. Show each.
            bodies = {
                record.tenant_uid: _pretty(
                    self._request_for(member_uids.get(record.tenant_uid, []))
                )
                for record in targets
            }
            rendered = "\n\n".join(
                f"{record.tenant_name} ({record.tenant_uid}):\n{bodies[record.tenant_uid]}"
                for record in targets
            )
            summary = (
                f"Dry run — nothing was written. {len(self.members)} member(s) found "
                f"in all {len(targets)} tenant(s). Body per tenant:\n{rendered}"
            )
            data["bodies"] = bodies
        else:
            body = _pretty(self._request_for([]))
            summary = (
                f"Dry run — nothing was written. Body for {object_type} "
                f"'{self.object_name}':\n{body}"
            )
            data["body"] = body

        return CommandResult(summary=summary, outcomes=outcomes, data=data)

    # ------------------------------------------------------------------
    # Target selection
    # ------------------------------------------------------------------

    def _resolve_targets(self) -> List[TenantRecord]:
        """Pick the tenants to write to, enforcing that each has an API-only user."""
        provisioned = self.store.provisioned_tenants(self.portal_region)

        if not provisioned:
            raise MspCliError(
                "No Managed Org has an API-only user yet, so "
                "there is no credential to create objects with.\n"
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
                f"created in them: {', '.join(missing)}\n"
                f"Tenants ready to use: {ready}\n"
                f"To provision the missing ones:  sccfm-msp api-users create --region "
                f"{self.portal_region} --tenant {' --tenant '.join(missing)}"
            )

        return selected

    # ------------------------------------------------------------------
    # Phase 1: resolve members in every tenant, or refuse
    # ------------------------------------------------------------------

    def _resolve_members_everywhere(self, targets: List[TenantRecord]) -> MemberUids:
        """Look every member up in every tenant, or raise without writing anything.

        Reported in one go rather than failing on the first tenant: if a member is
        missing from several tenants, you want to know about all of them now.
        """
        if not self.members:
            return {}

        resolved: MemberUids = {}
        problems: List[str] = []

        for record in targets:
            self.progress.step(record.tenant_name, "checking members exist")
            try:
                token = self.store.load_tenant_token(record.tenant_uid)
                with tenant_client(record.tenant_region, token) as api_client:
                    resolved[record.tenant_uid] = self._resolve_members(api_client)
            except ApiException as exc:
                problems.append(
                    f"  {record.tenant_name} ({record.tenant_uid}): "
                    + _describe_api_error(exc, record.tenant_region)
                )
            except MspCliError as exc:
                problems.append(f"  {record.tenant_name} ({record.tenant_uid}): {exc}")

        if problems:
            raise MspCliError(
                "Nothing was created. Every --member must already exist in every "
                "target tenant, and these tenants could not satisfy that:\n"
                + "\n".join(problems)
            )

        return resolved

    def _resolve_members(self, api_client: ApiClient) -> List[str]:
        """Member names -> UIDs within the tenant this client is pointed at."""
        api = ObjectManagementApi(api_client)
        family = family_of(self.object_type)

        uids: List[str] = []
        missing: List[str] = []
        mismatched: List[str] = []

        for member in self.members:
            found = find_object_by_name(api, member)
            if found is None:
                missing.append(member)
                continue

            member_type = found.value.object_type if found.value else None
            if member_type and family_of(member_type) != family:
                mismatched.append(f"'{member}' is a {member_type}")
                continue

            uids.append(found.uid)

        faults = []
        if missing:
            faults.append(f"no object named {', '.join(repr(m) for m in missing)}")
        if mismatched:
            faults.append(
                f"wrong type for a {family} group — {', '.join(mismatched)}"
            )
        if faults:
            raise MspCliError("; ".join(faults))

        return uids

    # ------------------------------------------------------------------
    # Phase 2: create
    # ------------------------------------------------------------------

    def _request_for(self, member_uids: Sequence[str]) -> CreateRequest:
        return build_create_request(
            name=self.object_name,
            object_type=self.object_type,
            values=self.values,
            description=self.description,
            member_uids=member_uids,
        )

    def _create_in_tenant(
        self, record: TenantRecord, member_uids: Sequence[str]
    ) -> ItemOutcome:
        """One tenant, one write call, in that tenant's own region.

        Never raises, so one tenant failing does not stop the rest.
        """
        # Every failure names the tenant UID: the display name is not enough to open
        # a support case or to re-run against just that tenant.
        context = f"tenant {record.tenant_uid} (region {record.tenant_region})"
        self.progress.step(record.tenant_name, "creating the object")

        try:
            token = self.store.load_tenant_token(record.tenant_uid)
            request = self._request_for(member_uids)
        except MspCliError as exc:
            return ItemOutcome.failure(record.tenant_name, f"{context}: {exc}")

        try:
            # record.tenant_region — not the portal's region — picks the base URL.
            with tenant_client(record.tenant_region, token) as api_client:
                created = ObjectManagementApi(api_client).create_object(request)
        except ApiException as exc:
            return ItemOutcome.failure(
                record.tenant_name,
                f"{context}: POST {OBJECTS_PATH} failed — "
                + _describe_api_error(exc, record.tenant_region),
            )
        except MspCliError as exc:
            return ItemOutcome.failure(record.tenant_name, f"{context}: {exc}")

        detail = f"region {record.tenant_region} — "
        detail += f"uid {created.uid}" if created.uid else "created"
        if member_uids:
            detail += f" — {len(member_uids)} member(s) referenced"
        return ItemOutcome.success(record.tenant_name, detail)


def _pretty(request: CreateRequest) -> str:
    return json.dumps(json.loads(request.to_json()), indent=2, sort_keys=True)


def _describe_api_error(exc: ApiException, region: str) -> str:
    if exc.status == 409:
        return "an object with that name already exists in this tenant"
    if exc.status == 400:
        return "Security Cloud Control rejected the object" + server_message(exc)
    if exc.status in (401, 403):
        return (
            f"the stored API-only user token was rejected (HTTP {exc.status}) — "
            "it may have been revoked; re-run 'sccfm-msp api-users create --replace-existing'"
            + server_message(exc)
        )
    if exc.status == 404:
        return (
            f"HTTP 404 on the {region} endpoint — check this tenant really lives in "
            f"{region}" + server_message(exc)
        )
    return f"HTTP {exc.status}: {exc.reason}" + server_message(exc)
