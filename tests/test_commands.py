"""Behaviour of the two commands an MSP actually runs."""

from __future__ import annotations

import pytest
from scc_firewall_manager_sdk.exceptions import ApiException

from sccfm_msp.commands import CreateApiUsersCommand, CreateObjectsCommand
from sccfm_msp.errors import MspCliError

#: The MSP portal lives in US; its tenants are spread over US, EU and APJ.
PORTAL_REGION = "US"


def provision(store, fake_apis, **kwargs):
    """Run `api-users create` over every tenant, as the CLI would."""
    store.save_portal_key(PORTAL_REGION, "portal-key")
    return CreateApiUsersCommand(store, portal_region=PORTAL_REGION, **kwargs).execute()


# ----------------------------------------------------------------------
# api-users create
# ----------------------------------------------------------------------


def test_creates_one_api_only_user_per_tenant(store, fake_apis, tenants):
    result = provision(store, fake_apis)

    assert result.ok
    assert len(result.succeeded) == len(tenants)

    created = fake_apis["users"].calls
    assert [c["tenant_uid"] for c in created] == [t.uid for t in tenants]
    assert all(c["api_only_user"] is True for c in created)
    assert all(c["role"] == "ROLE_ADMIN" for c in created)


def test_stores_a_token_and_index_entry_per_tenant(store, fake_apis, tenants):
    provision(store, fake_apis)

    for tenant in tenants:
        assert store.load_tenant_token(tenant.uid) == f"token-for-{tenant.uid}"
        record = store.get_tenant_record(tenant.uid)
        assert record is not None
        assert record.api_user_name == "msp-automation"
        assert record.api_user_uid == f"user-{tenant.uid}"


def test_token_is_requested_for_the_newly_created_user(store, fake_apis, tenants):
    provision(store, fake_apis)

    # Region is the portal's for every tenant: issuing a token is an MSP-level call.
    assert fake_apis["tokens"].calls == [
        {"tenant_uid": t.uid, "api_user_uid": f"user-{t.uid}", "region": PORTAL_REGION}
        for t in tenants
    ]


def test_one_failing_tenant_does_not_stop_the_others(store, fake_apis, tenants):
    fake_apis["users"].failures = {tenants[1].uid: ApiException(status=409, reason="Conflict")}

    result = provision(store, fake_apis)

    assert not result.ok
    assert len(result.succeeded) == 2
    assert len(result.failed) == 1
    assert "already exists" in result.failed[0].detail
    # The failed tenant was not recorded, so it cannot be targeted later.
    assert store.get_tenant_record(tenants[1].uid) is None


def test_rerun_is_idempotent_unless_replace_is_requested(store, fake_apis, tenants):
    provision(store, fake_apis)
    fake_apis["users"].calls = []

    result = provision(store, fake_apis)

    assert result.ok
    assert fake_apis["users"].calls == []  # No duplicate users created.
    assert all("already provisioned" in o.detail for o in result.outcomes)

    provision(store, fake_apis, replace_existing=True)
    assert len(fake_apis["users"].calls) == len(tenants)


def test_requires_a_stored_portal_key(store, fake_apis):
    with pytest.raises(MspCliError, match="No Manager Org"):
        CreateApiUsersCommand(store, portal_region=PORTAL_REGION).execute()


# ----------------------------------------------------------------------
# Region awareness
# ----------------------------------------------------------------------


def test_user_creation_always_uses_the_portal_region(store, fake_apis, tenants):
    """These are MSP-level calls: the EU and APJ tenants are still provisioned
    over the portal's US endpoint, not their own."""
    provision(store, fake_apis)

    portal_host = "https://api.us.security.cisco.com/firewall"

    assert {c["region"] for c in fake_apis["users"].calls} == {PORTAL_REGION}
    assert {c["host"] for c in fake_apis["users"].calls} == {portal_host}
    assert {c["region"] for c in fake_apis["tokens"].calls} == {PORTAL_REGION}


def test_each_tenants_region_is_recorded_alongside_the_portals(store, fake_apis, tenants):
    provision(store, fake_apis)

    for tenant in tenants:
        record = store.get_tenant_record(tenant.uid)
        assert record.tenant_region == tenant.region
        assert record.portal_region == PORTAL_REGION


def test_object_creation_is_sent_to_each_tenants_own_region(store, fake_apis, tenants):
    """This is the tenant-level call, so here the region does matter."""
    provision(store, fake_apis)

    CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
    ).execute()

    routed = {c["token"]: (c["region"], c["host"]) for c in fake_apis["objects"].calls}

    expected_hosts = {
        "US": "https://api.us.security.cisco.com/firewall",
        "EU": "https://api.eu.security.cisco.com/firewall",
        "APJ": "https://api.apj.security.cisco.com/firewall",
    }
    for tenant in tenants:
        region, host = routed[f"token-for-{tenant.uid}"]
        assert region == tenant.region
        assert host == expected_hosts[tenant.region]


def test_summary_reports_the_tenant_regions(store, fake_apis):
    result = provision(store, fake_apis)

    assert result.data["regions"] == ["APJ", "EU", "US"]
    assert "3 tenant region(s): APJ, EU, US" in result.summary


def test_token_is_never_requested_with_the_tenant_uid(store, fake_apis, tenants):
    """Regression: the add-user transaction's `entity_uid` is the *tenant*, not the
    new user. Passing it to the token endpoint produced a live 404."""
    provision(store, fake_apis)

    for call in fake_apis["tokens"].calls:
        assert call["api_user_uid"] != call["tenant_uid"]
        assert call["api_user_uid"] == f"user-{call['tenant_uid']}"


def test_lookup_tolerates_the_tenant_qualified_username(store, fake_apis, tenants):
    """The API reports API-only users as `requested-name@CDO-tenant-name`, so the
    match cannot be an exact string comparison."""
    result = provision(store, fake_apis, username="siddhu-test")

    assert result.ok, [o.detail for o in result.outcomes]
    assert len(fake_apis["tokens"].calls) == len(tenants)
    for tenant in tenants:
        assert store.get_tenant_record(tenant.uid).api_user_uid == f"user-{tenant.uid}"


def test_user_uid_comes_from_the_lookup_not_the_transaction(store, fake_apis, tenants):
    """If the created user cannot be found, we must fail loudly rather than guess."""
    fake_apis["users"].hide_created_user = True

    result = provision(store, fake_apis)

    assert not result.ok
    assert "could not find it again" in result.failed[0].detail
    # No token was requested with a guessed UID.
    assert fake_apis["tokens"].calls == []
    assert store.get_tenant_record(tenants[0].uid) is None


def test_failure_says_which_call_broke_and_what_the_server_said(
    store, fake_apis, tenants
):
    """A bare "HTTP 404" is not diagnosable; the detail must localise the failure."""
    fake_apis["users"].failures = {
        tenants[0].uid: ApiException(
            status=404, reason="Not Found", body='{"message": "Tenant not found"}'
        )
    }

    result = provision(store, fake_apis)

    detail = result.failed[0].detail
    assert "failed adding the user" in detail             # which step broke
    assert "POST /v1/msp/tenants/{uid}/users" in detail   # which endpoint
    assert f"on the {PORTAL_REGION} endpoint" in detail   # where it was sent
    assert "Tenant not found" in detail                   # what the API said


def test_cross_region_404_points_at_the_region_mismatch(store, fake_apis, tenants):
    """tenants[1] is EU while the portal is US, so the hint should say so."""
    fake_apis["users"].failures = {
        tenants[1].uid: ApiException(status=404, reason="Not Found")
    }

    result = provision(store, fake_apis)

    detail = result.failed[0].detail
    assert "reports region EU" in detail
    assert f"not {PORTAL_REGION}" in detail


def test_unroutable_region_provisions_but_fails_at_object_creation(
    store, fake_apis, tenants
):
    """UNKNOWN has no endpoint.

    Provisioning still succeeds — it is an MSP-level call on the portal's region,
    and the once-only token must not be thrown away. The failure surfaces at
    object creation, which is what actually needs the tenant's endpoint.
    """
    tenants[1].region = "UNKNOWN"

    provisioned = provision(store, fake_apis)
    assert provisioned.ok
    assert store.get_tenant_record(tenants[1].uid).tenant_region == "UNKNOWN"

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
    ).execute()

    assert not result.ok
    assert len(result.succeeded) == 2
    assert "UNKNOWN" in result.failed[0].detail
    # The other two tenants were still written to.
    assert len(fake_apis["objects"].calls) == 2


# ----------------------------------------------------------------------
# object create
# ----------------------------------------------------------------------


def test_object_create_refuses_without_an_api_only_user(store, fake_apis):
    command = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
    )

    with pytest.raises(MspCliError, match="has an API-only user yet"):
        command.execute()

    assert fake_apis["objects"].calls == []


def test_object_create_hits_every_tenant_with_its_own_token(store, fake_apis, tenants):
    provision(store, fake_apis)

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
        description="from the MSP CLI",
    ).execute()

    assert result.ok
    calls = fake_apis["objects"].calls
    assert len(calls) == len(tenants)  # One API call per tenant.
    # Each call used that tenant's own API-only user token, not the portal key.
    assert {c["token"] for c in calls} == {f"token-for-{t.uid}" for t in tenants}
    assert all(c["name"] == "lab-net" for c in calls)
    assert all(c["content"] == {"literal": "10.10.10.0/24"} for c in calls)
    assert all(c["object_type"] == "NETWORK_OBJECT" for c in calls)


def test_object_create_can_target_a_subset(store, fake_apis, tenants):
    provision(store, fake_apis)

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
        tenants=[tenants[0].display_name, tenants[2].uid],
    ).execute()

    assert result.ok
    assert {c["token"] for c in fake_apis["objects"].calls} == {
        f"token-for-{tenants[0].uid}",
        f"token-for-{tenants[2].uid}",
    }


def test_object_create_rejects_a_tenant_without_an_api_only_user(store, fake_apis, tenants):
    fake_apis["users"].failures = {tenants[1].uid: ApiException(status=409, reason="Conflict")}
    provision(store, fake_apis)

    command = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
        tenants=[tenants[1].display_name],
    )

    with pytest.raises(MspCliError, match="no stored API-only user"):
        command.execute()

    assert fake_apis["objects"].calls == []


def test_object_failures_name_the_tenant_uid_and_endpoint(store, fake_apis, tenants):
    """A display name is not enough to open a support case or re-run one tenant."""
    provision(store, fake_apis)
    fake_apis["objects"].failures = {
        f"token-for-{tenants[0].uid}": ApiException(
            status=500,
            reason="Internal Server Error",
            body='{"errorMessage":"An unspecified error occurred"}',
        )
    }

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="x",
        object_type="NETWORK_OBJECT",
        values=["10.2.2.2"],
    ).execute()

    detail = result.failed[0].detail
    assert tenants[0].uid in detail
    assert "POST /v1/objects" in detail
    assert "An unspecified error occurred" in detail


@pytest.mark.parametrize(
    "object_type, values",
    [
        ("NETWORK_OBJECT", ["10.2.2.2"]),
        ("NETWORK_GROUP", ["10.0.0.0/8", "10.1.0.0/16"]),
        ("URL_OBJECT", ["https://intranet.example"]),
        ("SERVICE_OBJECT", ["TCP/443"]),
        ("SERVICE_GROUP", ["TCP/443", "UDP/53"]),
    ],
)
def test_created_uid_is_read_from_the_response(
    store, fake_apis, tenants, object_type, values
):
    """The SDK must be able to parse back what it sent, for every content shape.

    The fake echoes the request through the real `ObjectResponse` model, so a
    content shape the SDK cannot round-trip would fail here rather than in
    production. This is what regressed when `ObjectContent` was an ambiguous oneOf.
    """
    provision(store, fake_apis)

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-obj",
        object_type=object_type,
        values=values,
    ).execute()

    assert result.ok, [o.detail for o in result.outcomes]
    for tenant in tenants:
        assert any(f"obj-token-for-{tenant.uid}" in o.detail for o in result.outcomes)


# ----------------------------------------------------------------------
# Group members (--member)
# ----------------------------------------------------------------------


def stock_members(fake_apis, tenants, **per_tenant):
    """Declare which named objects already exist in which tenant."""
    fake_apis["objects"].existing = {
        f"token-for-{tenant.uid}": per_tenant.get(tenant.uid, {}) for tenant in tenants
    }


def group_with_members(store, members, values=(), object_type="NETWORK_GROUP", **kw):
    return CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-group",
        object_type=object_type,
        values=list(values),
        members=list(members),
        **kw,
    )


def test_members_resolve_to_each_tenants_own_uid(store, fake_apis, tenants):
    """A member name is one object per tenant, each with a different UID."""
    provision(store, fake_apis)
    stock_members(
        fake_apis, tenants, **{t.uid: {"shared-net": "NETWORK_OBJECT"} for t in tenants}
    )

    result = group_with_members(store, ["shared-net"]).execute()

    assert result.ok, [o.detail for o in result.outcomes]
    by_token = {c["token"]: c["member_uids"] for c in fake_apis["objects"].calls}
    for tenant in tenants:
        token = f"token-for-{tenant.uid}"
        assert by_token[token] == [f"member-{token}-shared-net"]


def test_a_group_can_mix_literals_and_members(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_members(
        fake_apis, tenants, **{t.uid: {"shared-net": "NETWORK_OBJECT"} for t in tenants}
    )

    result = group_with_members(store, ["shared-net"], values=["10.9.0.0/16"]).execute()

    assert result.ok
    content = fake_apis["objects"].calls[0]["content"]
    assert content["literals"] == [{"literal": "10.9.0.0/16"}]
    assert len(content["referencedObjectUids"]) == 1


def test_a_group_can_contain_another_group(store, fake_apis, tenants):
    """Nesting works because a member is resolved by name, whatever its type."""
    provision(store, fake_apis)
    stock_members(
        fake_apis, tenants, **{t.uid: {"inner-group": "NETWORK_GROUP"} for t in tenants}
    )

    result = group_with_members(store, ["inner-group"]).execute()

    assert result.ok, [o.detail for o in result.outcomes]
    assert fake_apis["objects"].calls[0]["member_uids"]


def test_nothing_is_created_if_a_member_is_missing_from_any_tenant(
    store, fake_apis, tenants
):
    """The whole point of validating first: a partial rollout is worse than none."""
    provision(store, fake_apis)
    stock_members(
        fake_apis,
        tenants,
        **{
            tenants[0].uid: {"shared-net": "NETWORK_OBJECT"},
            tenants[1].uid: {"shared-net": "NETWORK_OBJECT"},
            # tenants[2] does not have it.
        },
    )

    command = group_with_members(store, ["shared-net"])

    with pytest.raises(MspCliError) as caught:
        command.execute()

    message = str(caught.value)
    assert "Nothing was created" in message
    assert tenants[2].uid in message
    assert "shared-net" in message
    # Not one tenant was written to, including the two that could have succeeded.
    assert fake_apis["objects"].calls == []


def test_every_offending_tenant_is_listed_at_once(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_members(fake_apis, tenants)  # nobody has it

    with pytest.raises(MspCliError) as caught:
        group_with_members(store, ["shared-net"]).execute()

    message = str(caught.value)
    for tenant in tenants:
        assert tenant.uid in message


def test_a_member_of_the_wrong_family_is_refused(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_members(
        fake_apis, tenants, **{t.uid: {"a-url": "URL_OBJECT"} for t in tenants}
    )

    with pytest.raises(MspCliError, match="wrong type"):
        group_with_members(store, ["a-url"]).execute()

    assert fake_apis["objects"].calls == []


def test_single_types_cannot_take_members(store, fake_apis):
    with pytest.raises(MspCliError, match="cannot reference other objects"):
        group_with_members(
            store, ["shared-net"], values=["10.0.0.0/8"], object_type="NETWORK_OBJECT"
        ).execute()


def test_a_group_needs_a_value_or_a_member(store, fake_apis):
    with pytest.raises(MspCliError, match="at least one --value"):
        group_with_members(store, [], values=[]).execute()


def test_service_groups_take_members_too(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_members(
        fake_apis, tenants, **{t.uid: {"https": "SERVICE_OBJECT"} for t in tenants}
    )

    result = group_with_members(
        store, ["https"], values=["UDP/53"], object_type="SERVICE_GROUP"
    ).execute()

    assert result.ok, [o.detail for o in result.outcomes]
    content = fake_apis["objects"].calls[0]["content"]
    assert content["literals"] == [{"protocol": "UDP", "serviceValue": {"literal": "53"}}]
    assert len(content["referencedObjectUids"]) == 1


def test_dry_run_validates_members_without_writing(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_members(
        fake_apis, tenants, **{t.uid: {"shared-net": "NETWORK_OBJECT"} for t in tenants}
    )

    result = group_with_members(store, ["shared-net"], dry_run=True).execute()

    assert result.ok
    assert fake_apis["objects"].calls == []  # No writes.
    # A body per tenant, each with that tenant's resolved UID.
    assert set(result.data["bodies"]) == {t.uid for t in tenants}
    for tenant in tenants:
        assert f"member-token-for-{tenant.uid}-shared-net" in result.data["bodies"][tenant.uid]


def test_dry_run_still_refuses_a_missing_member(store, fake_apis, tenants):
    provision(store, fake_apis)
    stock_members(fake_apis, tenants)

    with pytest.raises(MspCliError, match="Nothing was created"):
        group_with_members(store, ["shared-net"], dry_run=True).execute()


def test_dry_run_shows_the_body_and_sends_nothing(store, fake_apis, tenants):
    provision(store, fake_apis)

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-nets",
        object_type="NETWORK_GROUP",
        values=["10.10.10.0/24", "10.20.0.0/16"],
        dry_run=True,
    ).execute()

    assert result.ok
    assert fake_apis["objects"].calls == []  # Nothing was sent.
    assert '"objectType": "NETWORK_GROUP"' in result.data["body"]
    assert "10.20.0.0/16" in result.data["body"]
    # Each tenant line names its uid and the endpoint it would have been sent to.
    for tenant in tenants:
        assert any(tenant.uid in o.detail for o in result.outcomes)


def test_object_create_reports_a_revoked_token_clearly(store, fake_apis, tenants):
    provision(store, fake_apis)
    fake_apis["objects"].failures = {
        f"token-for-{tenants[0].uid}": ApiException(status=401, reason="Unauthorized")
    }

    result = CreateObjectsCommand(
        store,
        portal_region=PORTAL_REGION,
        object_name="lab-net",
        object_type="NETWORK_OBJECT",
        values=["10.10.10.0/24"],
    ).execute()

    assert not result.ok
    assert len(result.succeeded) == 2
    assert "revoked" in result.failed[0].detail
