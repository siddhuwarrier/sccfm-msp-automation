"""The click layer: argument parsing, exit codes, and error presentation."""

from __future__ import annotations

import pytest
from click.testing import CliRunner

from sccfm_msp.cli import cli


@pytest.fixture
def run(monkeypatch, tmp_path, fake_keyring):
    """Invoke the CLI with an isolated config dir and keyring."""
    monkeypatch.setenv("SCCFM_MSP_HOME", str(tmp_path / "config"))

    def _run(*args):
        return CliRunner().invoke(cli, list(args), catch_exceptions=False)

    return _run


def test_help_lists_the_commands(run):
    result = run("--help")

    assert result.exit_code == 0
    for expected in ("login", "tenants", "api-users", "object"):
        assert expected in result.output


def test_object_create_without_provisioning_exits_nonzero(run, fake_apis):
    result = run(
        "objects", "create", "--type", "NETWORK_OBJECT",
        "--name", "lab-net", "--value", "10.10.10.0/24",
    )

    assert result.exit_code == 1
    assert "has an API-only user yet" in result.output


def test_end_to_end_provision_then_create_objects(run, fake_apis, tenants):
    from sccfm_msp.credentials import CredentialStore

    CredentialStore().save_portal_key("US", "portal-key")

    provision = run("api-users", "create", "--username", "automation")
    assert provision.exit_code == 0, provision.output
    assert "Created 3 API-only user(s)" in provision.output

    create = run(
        "objects", "create", "--type", "NETWORK_OBJECT",
        "--name", "lab-net", "--value", "10.10.10.0/24",
    )
    assert create.exit_code == 0, create.output
    assert "in 3 of 3 tenant(s)" in create.output
    assert len(fake_apis["objects"].calls) == len(tenants)


def test_subset_flag_limits_the_tenants_touched(run, fake_apis, tenants):
    from sccfm_msp.credentials import CredentialStore

    CredentialStore().save_portal_key("US", "portal-key")
    run("api-users", "create")

    result = run(
        "objects",
        "create",
        "--type",
        "NETWORK_OBJECT",
        "--name",
        "lab-net",
        "--value",
        "10.10.10.0/24",
        "--tenant",
        tenants[0].display_name,
    )

    assert result.exit_code == 0, result.output
    assert "in 1 of 1 tenant(s)" in result.output
    assert len(fake_apis["objects"].calls) == 1


def test_object_type_must_be_stated(run, fake_apis):
    """--type is compulsory: inferring the type from the value would be guesswork."""
    result = run("objects", "create", "--name", "lab-net", "--value", "10.10.10.0/24")

    assert result.exit_code == 2
    assert "--type" in result.output


def test_unknown_region_is_rejected_by_the_parser(run):
    result = run("tenants", "list", "--region", "mars")

    assert result.exit_code == 2
    assert "Invalid value for '--region'" in result.output


def test_region_input_is_case_insensitive(run, fake_apis):
    """`--region eu` and `--region EU` must reach the same stored key."""
    from sccfm_msp.credentials import CredentialStore

    CredentialStore().save_portal_key("EU", "portal-key")

    result = run("tenants", "list", "--region", "eu")

    assert result.exit_code == 0, result.output


def test_tenants_list_shows_each_tenants_region(run, fake_apis):
    from sccfm_msp.credentials import CredentialStore

    CredentialStore().save_portal_key("US", "portal-key")

    result = run("tenants", "list")

    assert result.exit_code == 0, result.output
    # The portal is in US, but its tenants span three regions.
    for region in ("US", "EU", "APJ"):
        assert f"region {region}" in result.output
    assert "3 region(s) (APJ, EU, US)" in result.output
