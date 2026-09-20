"""Test fixtures: an in-memory keyring and stand-ins for the SDK's API classes.

The tests never reach the network and never touch your real OS keyring.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Dict, List, Optional

import keyring
import pytest
from keyring.backend import KeyringBackend
from scc_firewall_manager_sdk import (
    ApiTokenInfo,
    CdoTransaction,
    ListObjectResponse,
    MspManagedTenantDto,
    ObjectContent,
    ObjectResponse,
    SharedObjectValue,
    UnifiedObjectListView,
    User,
    UserPage,
)

from sccfm_msp.credentials import CredentialStore


class InMemoryKeyring(KeyringBackend):
    """A keyring that lives in a dict, so tests stay off the real keychain."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.store: Dict[str, str] = {}

    def get_password(self, service: str, username: str) -> Optional[str]:
        return self.store.get(f"{service}/{username}")

    def set_password(self, service: str, username: str, password: str) -> None:
        self.store[f"{service}/{username}"] = password

    def delete_password(self, service: str, username: str) -> None:
        self.store.pop(f"{service}/{username}", None)


@pytest.fixture
def fake_keyring(monkeypatch):
    backend = InMemoryKeyring()
    monkeypatch.setattr(keyring, "get_keyring", lambda: backend)
    monkeypatch.setattr(keyring, "set_password", backend.set_password)
    monkeypatch.setattr(keyring, "get_password", backend.get_password)
    monkeypatch.setattr(keyring, "delete_password", backend.delete_password)
    return backend


@pytest.fixture
def store(fake_keyring, tmp_path) -> CredentialStore:
    return CredentialStore(config_dir=tmp_path / "config")


#: Deliberately spread across three regions: the MSP portal is in US, but two of
#: its tenants are not, which is what the region-routing assertions rely on.
TENANT_REGIONS = {"a": "US", "b": "EU", "c": "APJ"}


@pytest.fixture
def tenants() -> List[MspManagedTenantDto]:
    return [
        MspManagedTenantDto(
            uid=f"uid-{n}",
            name=f"tenant-{n}",
            displayName=f"Tenant {n}",
            region=region,
        )
        for n, region in TENANT_REGIONS.items()
    ]


# ----------------------------------------------------------------------
# Stand-ins for the SDK API classes
# ----------------------------------------------------------------------


class FakeUserManagementApi:
    """Records the users it was asked to create, and where the call was sent."""

    #: Tenant UIDs that should fail, mapped to the exception to raise.
    failures: Dict[str, Exception] = {}
    calls: List[dict] = []

    def __init__(self, api_client=None):
        self.region = getattr(api_client, "region", None)
        self.host = getattr(api_client, "host", None)

    def add_users_to_tenant_in_msp_portal(self, tenant_uid, msp_add_users_to_tenant_input):
        if tenant_uid in self.failures:
            raise self.failures[tenant_uid]
        user = msp_add_users_to_tenant_input.users[0]
        type(self).calls.append(
            {
                "tenant_uid": tenant_uid,
                "username": user.username,
                "role": user.role.value,
                "api_only_user": user.api_only_user,
                "region": self.region,
                "host": self.host,
            }
        )
        type(self).created[tenant_uid] = user.username
        return CdoTransaction(
            transactionUid=f"txn-{tenant_uid}",
            cdoTransactionStatus="DONE",
            # Deliberately the TENANT's uid, which is what the real API returns.
            # A command that mistakes this for the new user's uid will 404 when it
            # asks for a token, exactly as the live API did.
            entityUid=tenant_uid,
        )

    #: tenant_uid -> username, populated by add_users, read back by the lookup.
    created: Dict[str, str] = {}

    #: Set True to make the lookup return no matching user, as if creation silently
    #: did not take effect.
    hide_created_user = False

    #: Usernames the fake was asked to delete, per tenant.
    deleted: Dict[str, List[str]] = {}
    #: The same deletes with the region/host they were sent to.
    delete_calls: List[dict] = []

    def delete_users_from_tenant_in_msp_portal(
        self, tenant_uid, msp_delete_users_from_tenant_input
    ):
        if tenant_uid in self.failures:
            raise self.failures[tenant_uid]

        names = list(msp_delete_users_from_tenant_input.usernames)
        type(self).deleted.setdefault(tenant_uid, []).extend(names)
        type(self).delete_calls.append(
            {"tenant_uid": tenant_uid, "usernames": names, "region": self.region,
             "host": self.host}
        )
        # Deleting really removes it, so a second lookup finds nothing.
        type(self).created.pop(tenant_uid, None)

        return CdoTransaction(
            transactionUid=f"txn-del-{tenant_uid}",
            cdoTransactionStatus="DONE",
            entityUid=tenant_uid,
        )

    def get_api_only_users_in_msp_managed_tenant(
        self, tenant_uid, limit=None, offset=None, **kw
    ):
        username = type(self).created.get(tenant_uid)
        if username is None or type(self).hide_created_user:
            return UserPage(count=0, items=[])

        if int(offset or 0) > 0:  # Single page of results.
            return UserPage(count=1, items=[])

        # The real API qualifies the username with the tenant rather than echoing
        # it back verbatim, so the fake does too. A fake that returned the plain
        # username would let an exact-match bug pass here and fail in production.
        return UserPage(
            count=1,
            items=[
                User(
                    uid=f"user-{tenant_uid}",
                    name=f"{username}@CDO_cdo_{tenant_uid}",
                    apiOnlyUser=True,
                )
            ],
        )


class FakeTenantManagementApi:
    """Issues a predictable token per tenant, recording where it was asked."""

    calls: List[dict] = []

    def __init__(self, api_client=None):
        self.region = getattr(api_client, "region", None)

    def generate_api_token_for_user_in_tenant(self, tenant_uid, api_user_uid):
        type(self).calls.append(
            {
                "tenant_uid": tenant_uid,
                "api_user_uid": api_user_uid,
                "region": self.region,
            }
        )
        return ApiTokenInfo(apiToken=f"token-for-{tenant_uid}")


class FakeObjectManagementApi:
    """Records every create_object call: the token, and the region it went to."""

    #: Tenant tokens that should fail, mapped to the exception to raise.
    failures: Dict[str, Exception] = {}
    calls: List[dict] = []

    def __init__(self, api_client=None):
        self.token = getattr(api_client, "token", None)
        self.region = getattr(api_client, "region", None)
        self.host = getattr(api_client, "host", None)

    #: tenant token -> {object name: objectType} already present in that tenant,
    #: so tests can decide which members resolve where.
    existing: Dict[str, Dict[str, str]] = {}

    def get_objects(self, q=None, limit=None, offset=None, **kw):
        """Lookup used to resolve --member names to this tenant's UIDs."""
        wanted = (q or "").partition("name:")[2].strip()
        present = type(self).existing.get(self.token, {})

        items = [
            UnifiedObjectListView(
                uid=f"member-{self.token}-{name}",
                name=name,
                value=SharedObjectValue(
                    objectType=object_type, defaultContent=ObjectContent(literal="1.1.1.1")
                ),
            )
            for name, object_type in present.items()
            if not wanted or name.casefold() == wanted.casefold()
        ]
        return ListObjectResponse(count=len(items), items=items)

    #: Object UIDs the fake was asked to delete, with the force flag used.
    deleted: List[dict] = []

    def delete_object(self, uid, forced_delete=None):
        if self.token in self.failures:
            raise self.failures[self.token]
        type(self).deleted.append(
            {"token": self.token, "region": self.region, "uid": uid, "force": forced_delete}
        )
        # Really gone, so a second run finds nothing.
        for name, entry in list(type(self).existing.get(self.token, {}).items()):
            if f"member-{self.token}-{name}" == uid:
                type(self).existing[self.token].pop(name)
        return None

    def create_object(self, create_request):
        if self.token in self.failures:
            raise self.failures[self.token]

        # Record the serialized request rather than poking at model attributes: the
        # content shape differs per object type, and the JSON is what the API sees.
        sent = json.loads(create_request.to_json())
        type(self).calls.append(
            {
                "token": self.token,
                "region": self.region,
                "host": self.host,
                "name": sent["name"],
                "object_type": sent["value"]["objectType"],
                "content": sent["value"]["defaultContent"],
                "member_uids": sent["value"]["defaultContent"].get(
                    "referencedObjectUids", []
                ),
            }
        )

        # Echo the request back the way the API does, through the real response
        # model — so a response the SDK cannot parse would fail here too.
        return ObjectResponse.from_dict(
            {"uid": f"obj-{self.token}", "name": sent["name"], "value": sent["value"]}
        )


class FakeApiClient:
    """Carries the credential and the region, so tests can assert on routing."""

    def __init__(self, region: str, token: str, host: str) -> None:
        self.region = region
        self.token = token
        self.host = host


@pytest.fixture
def fake_apis(monkeypatch, tenants):
    """Point both command modules at the fakes above.

    The region is still resolved through the real `host_for_region`, so an
    unroutable region fails here exactly as it would in production.
    """
    from sccfm_msp.client import host_for_region
    from sccfm_msp.commands import (
        create_api_users,
        create_objects,
        delete_api_users,
        delete_objects,
        list_tenants,
    )

    for fake in (FakeUserManagementApi, FakeTenantManagementApi, FakeObjectManagementApi):
        fake.calls = []
        fake.failures = {}
    FakeUserManagementApi.created = {}
    FakeUserManagementApi.hide_created_user = False
    FakeUserManagementApi.deleted = {}
    FakeUserManagementApi.delete_calls = []
    FakeObjectManagementApi.existing = {}
    FakeObjectManagementApi.deleted = []

    @contextmanager
    def fake_client(region, token):
        yield FakeApiClient(region=region, token=token, host=host_for_region(region))

    fake_portal_client = fake_client
    fake_tenant_client = fake_client

    monkeypatch.setattr(create_api_users, "msp_portal_client", fake_portal_client)
    monkeypatch.setattr(create_api_users, "MSPUserManagementApi", FakeUserManagementApi)
    monkeypatch.setattr(create_api_users, "MSPTenantManagementApi", FakeTenantManagementApi)
    monkeypatch.setattr(create_api_users, "list_managed_tenants", lambda client: tenants)

    monkeypatch.setattr(create_objects, "tenant_client", fake_tenant_client)
    monkeypatch.setattr(create_objects, "ObjectManagementApi", FakeObjectManagementApi)

    monkeypatch.setattr(delete_objects, "tenant_client", fake_tenant_client)
    monkeypatch.setattr(delete_objects, "ObjectManagementApi", FakeObjectManagementApi)

    monkeypatch.setattr(delete_api_users, "msp_portal_client", fake_portal_client)
    monkeypatch.setattr(delete_api_users, "MSPUserManagementApi", FakeUserManagementApi)
    monkeypatch.setattr(delete_api_users, "list_managed_tenants", lambda client: tenants)

    monkeypatch.setattr(list_tenants, "msp_portal_client", fake_portal_client)
    monkeypatch.setattr(list_tenants, "list_managed_tenants", lambda client: tenants)

    return {
        "users": FakeUserManagementApi,
        "tokens": FakeTenantManagementApi,
        "objects": FakeObjectManagementApi,
    }
