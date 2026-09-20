"""The two delete commands: `api-users delete` and `objects delete`."""

from __future__ import annotations

import pytest
from scc_firewall_manager_sdk.exceptions import ApiException

from sccfm_msp.commands import (
    CreateApiUsersCommand,
    DeleteApiUsersCommand,
    DeleteObjectsCommand,
)
from sccfm_msp.errors import MspCliError

PORTAL_REGION = "US"


def provision(store, fake_apis, **kwargs):
    store.save_portal_key(PORTAL_REGION, "portal-key")
    return CreateApiUsersCommand(store, portal_region=PORTAL_REGION, **kwargs).execute()


def stock_objects(fake_apis, tenants, **per_tenant):
    fake_apis["objects"].existing = {
        f"token-for-{tenant.uid}": dict(per_tenant.get(tenant.uid, {}))
        for tenant in tenants
    }


# ----------------------------------------------------------------------
# api-users delete
# ----------------------------------------------------------------------


def test_deletes_the_recorded_user_from_every_tenant(store, fake_apis, tenants):
    provision(store, fake_apis)

    result = DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()

    assert result.ok, [o.detail for o in result.outcomes]
    assert result.data["deleted"] == len(tenants)
    # Every tenant had exactly one delete issued.
    assert set(fake_apis["users"].deleted) == {t.uid for t in tenants}


def test_delete_uses_the_tenant_qualified_name(store, fake_apis, tenants):
    """The endpoint deletes by name, and the API's name is the qualified one.

    Sending the unqualified 'msp-automation' would match nothing.
    """
    provision(store, fake_apis)

    DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()

    for tenant in tenants:
        sent = fake_apis["users"].deleted[tenant.uid]
        assert sent == [f"msp-automation@CDO_cdo_{tenant.uid}"]
        assert "@" in sent[0]


def test_delete_clears_the_stored_token_and_record(store, fake_apis, tenants):
    provision(store, fake_apis)

    DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()

    for tenant in tenants:
        assert store.get_tenant_record(tenant.uid) is None
        with pytest.raises(MspCliError):
            store.load_tenant_token(tenant.uid)
    assert store.provisioned_tenants(PORTAL_REGION) == []


def test_delete_always_uses_the_portal_region(store, fake_apis, tenants):
    """Deleting a user is an MSP-level call, so every one goes to the portal's
    endpoint — even for the EU and APJ tenants."""
    provision(store, fake_apis)

    DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()

    calls = fake_apis["users"].delete_calls
    assert len(calls) == len(tenants)
    assert {c["region"] for c in calls} == {PORTAL_REGION}
    assert {c["host"] for c in calls} == {"https://api.us.security.cisco.com/firewall"}


def test_delete_can_target_a_subset(store, fake_apis, tenants):
    provision(store, fake_apis)

    result = DeleteApiUsersCommand(
        store, portal_region=PORTAL_REGION, tenants=[tenants[1].display_name]
    ).execute()

    assert result.ok
    assert result.data["deleted"] == 1
    # Only the named tenant lost its record.
    assert store.get_tenant_record(tenants[1].uid) is None
    assert store.get_tenant_record(tenants[0].uid) is not None


def test_delete_treats_an_absent_user_as_done(store, fake_apis, tenants):
    """Re-running a delete must not fail; it should still clean up locally."""
    provision(store, fake_apis)
    fake_apis["users"].hide_created_user = True

    result = DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()

    assert result.ok
    assert result.data["deleted"] == 0
    assert all("already gone" in o.detail for o in result.outcomes)
    # Local state is cleaned up regardless, so the command is idempotent.
    assert store.provisioned_tenants(PORTAL_REGION) == []


def test_delete_needs_something_recorded(store, fake_apis):
    store.save_portal_key(PORTAL_REGION, "portal-key")

    with pytest.raises(MspCliError, match="nothing to delete"):
        DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()


def test_delete_keeps_local_state_when_the_api_fails(store, fake_apis, tenants):
    """A failed delete must not drop the token — that would orphan the user."""
    provision(store, fake_apis)
    fake_apis["users"].failures = {
        tenants[0].uid: ApiException(status=500, reason="Internal Server Error")
    }

    result = DeleteApiUsersCommand(store, portal_region=PORTAL_REGION).execute()

    assert not result.ok
    assert store.get_tenant_record(tenants[0].uid) is not None
    assert store.load_tenant_token(tenants[0].uid)
    # The other tenants still completed.
    assert store.get_tenant_record(tenants[1].uid) is None


# ----------------------------------------------------------------------
# objects delete
# ----------------------------------------------------------------------


def test_objects_delete_resolves_the_name_per_tenant(store, fake_apis, tenants):
    """Object UIDs are per-tenant, so each delete must use that tenant's UID."""
    provision(store, fake_apis)
    stock_objects(
        fake_apis, tenants, **{t.uid: {"lab-net": "NETWORK_OBJECT"} for t in tenants}
    )

    result = DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()

    assert result.ok, [o.detail for o in result.outcomes]
    assert result.data["deleted"] == len(tenants)
    deleted = {d["uid"] for d in fake_apis["objects"].deleted}
    assert deleted == {f"member-token-for-{t.uid}-lab-net" for t in tenants}


def test_objects_delete_is_sent_to_each_tenants_own_region(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_objects(
        fake_apis, tenants, **{t.uid: {"lab-net": "NETWORK_OBJECT"} for t in tenants}
    )

    DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()

    routed = {d["uid"]: d["region"] for d in fake_apis["objects"].deleted}
    for tenant in tenants:
        assert routed[f"member-token-for-{tenant.uid}-lab-net"] == tenant.region


def test_objects_delete_reports_a_tenant_that_has_none(store, fake_apis, tenants):
    """Unlike --member, a missing object is not a reason to abort the run."""
    provision(store, fake_apis)
    stock_objects(
        fake_apis,
        tenants,
        **{
            tenants[0].uid: {"lab-net": "NETWORK_OBJECT"},
            # the others have nothing
        },
    )

    result = DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()

    assert result.ok  # Not a failure.
    assert result.data["deleted"] == 1
    nothing = [o for o in result.outcomes if "nothing to do" in o.detail]
    assert len(nothing) == len(tenants) - 1


def test_objects_delete_is_idempotent(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_objects(
        fake_apis, tenants, **{t.uid: {"lab-net": "NETWORK_OBJECT"} for t in tenants}
    )

    first = DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()
    second = DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()

    assert first.data["deleted"] == len(tenants)
    assert second.ok and second.data["deleted"] == 0


def test_objects_delete_passes_force_through(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_objects(
        fake_apis, tenants, **{t.uid: {"lab-net": "NETWORK_OBJECT"} for t in tenants}
    )

    DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net", force=True
    ).execute()

    assert all(d["force"] is True for d in fake_apis["objects"].deleted)


def test_objects_delete_without_force_sends_no_flag(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_objects(
        fake_apis, tenants, **{t.uid: {"lab-net": "NETWORK_OBJECT"} for t in tenants}
    )

    DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()

    assert all(d["force"] is None for d in fake_apis["objects"].deleted)


def test_objects_delete_explains_an_in_use_refusal(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_objects(
        fake_apis, tenants, **{t.uid: {"lab-net": "NETWORK_OBJECT"} for t in tenants}
    )
    fake_apis["objects"].failures = {
        f"token-for-{tenants[0].uid}": ApiException(status=400, reason="Bad Request")
    }

    result = DeleteObjectsCommand(
        store, portal_region=PORTAL_REGION, object_name="lab-net"
    ).execute()

    assert not result.ok
    detail = result.failed[0].detail
    assert tenants[0].uid in detail
    assert "--force" in detail


def test_objects_delete_needs_an_api_only_user(store, fake_apis):
    with pytest.raises(MspCliError, match="no credential to delete objects with"):
        DeleteObjectsCommand(
            store, portal_region=PORTAL_REGION, object_name="lab-net"
        ).execute()


# ----------------------------------------------------------------------
# Platforms without a usable keyring
# ----------------------------------------------------------------------


def test_no_keyring_is_a_readable_error_not_a_traceback(store, monkeypatch):
    """Headless Linux — a container or CI runner — has no secret service.

    That is where automation tends to run, so the failure has to explain itself
    rather than surfacing keyring's own exception.
    """
    import keyring
    from keyring.errors import NoKeyringError

    from sccfm_msp.errors import KeyringUnavailableError, MspCliError

    def unavailable(*_args, **_kwargs):
        raise NoKeyringError("No recommended backend was available")

    monkeypatch.setattr(keyring, "get_password", unavailable)
    monkeypatch.setattr(keyring, "set_password", unavailable)

    for call in (
        lambda: store.load_portal_key("US"),
        lambda: store.save_portal_key("US", "k"),
        lambda: store.load_tenant_token("uid-a"),
        lambda: store.save_tenant_token("uid-a", "t"),
    ):
        with pytest.raises(KeyringUnavailableError) as caught:
            call()
        message = str(caught.value)
        assert "No OS keyring is available" in message  # what happened
        assert "Headless/CI" in message                 # where it usually happens
        assert "PYTHON_KEYRING_BACKEND" in message      # what to do about it

    # It is an MspCliError, so the CLI prints it instead of a traceback.
    assert issubclass(KeyringUnavailableError, MspCliError)


def test_a_missing_credential_is_not_reported_as_a_broken_keyring(store, fake_apis):
    """An empty keyring and an absent keyring are different problems."""
    from sccfm_msp.errors import CredentialNotFoundError, KeyringUnavailableError

    with pytest.raises(CredentialNotFoundError) as caught:
        store.load_portal_key("US")

    assert not isinstance(caught.value, KeyringUnavailableError)
    assert "sccfm-msp login" in str(caught.value)
