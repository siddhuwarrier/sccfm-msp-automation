"""Command-line surface.

This module only parses arguments, builds a Command, hands it to the invoker, and
prints the result. All the real work lives in `sccfm_msp/commands/`.
"""

from __future__ import annotations

import sys
from typing import Tuple

import click
from scc_firewall_manager_sdk import UserRole

from . import __version__
from .api_users import DEFAULT_API_USER_NAME
from .client import DEFAULT_REGION, PRODUCTION_REGIONS
from .commands import (
    Command,
    CommandInvoker,
    CommandResult,
    CreateApiUsersCommand,
    CreateObjectsCommand,
    DeleteApiUsersCommand,
    DeleteObjectsCommand,
    ListTenantsCommand,
    StoreMspApiKeyCommand,
)
from .credentials import CredentialStore
from .errors import MspCliError
from .objects import OBJECT_TYPES

REGION_CHOICE = click.Choice(PRODUCTION_REGIONS, case_sensitive=False)

# Roles that make sense for an automation user. Spelled exactly as the API's
# UserRole enum, so what you type here is what the SDK sends.
ROLE_CHOICE = click.Choice(
    ["ROLE_ADMIN", "ROLE_SUPER_ADMIN", "ROLE_READ_ONLY", "ROLE_EDIT_ONLY", "ROLE_DEPLOY_ONLY"]
)


class UpperChoice(click.ParamType):
    """A choice that accepts any casing but always reports the API's spelling.

    `click.Choice(case_sensitive=False)` lower-cases the values in its help and
    error text, which would show `network_object` for a type the API calls
    `NETWORK_OBJECT`. Since `--type` is required, that error is the first thing a
    user sees when they forget it, so it is worth getting right.
    """

    name = "choice"

    def __init__(self, choices: Tuple[str, ...]) -> None:
        self.choices = choices

    def convert(self, value, param, ctx):  # noqa: ANN001 - click's signature
        code = str(value).strip().upper()
        if code not in self.choices:
            self.fail(
                f"{value!r} is not one of {', '.join(self.choices)}.", param, ctx
            )
        return code

    def get_missing_message(self, *args, **kwargs) -> str:
        return "Choose from:\n  " + "\n  ".join(self.choices)


OBJECT_TYPE_CHOICE = UpperChoice(OBJECT_TYPES)

_region_option = click.option(
    "--region",
    "portal_region",
    type=REGION_CHOICE,
    default=DEFAULT_REGION,
    show_default=True,
    metavar="REGION",
    help=(
        "Region your Manager Org (MSP Portal) lives in "
        f"({', '.join(PRODUCTION_REGIONS)}). All MSP-level calls go here. Object "
        "creation instead uses each Managed Org's own region, detected from the API."
    ),
)

_tenant_option = click.option(
    "--tenant",
    "tenants",
    multiple=True,
    metavar="NAME_OR_UID",
    help="Limit to specific tenants. Repeat the flag. Omit it to target all tenants.",
)


def _run(command: Command) -> None:
    """Execute a command, print its report, and set the exit code.

    An expected `MspCliError` becomes a `ClickException`, which click prints as a
    single `Error: ...` line rather than a traceback.
    """
    try:
        result = CommandInvoker().run(command)
    except MspCliError as exc:
        raise click.ClickException(str(exc)) from exc

    _report(result)

    if command.failures_are_errors and not result.ok:
        raise click.exceptions.Exit(1)


def _report(result: CommandResult) -> None:
    for outcome in result.outcomes:
        mark = click.style("✓", fg="green") if outcome.ok else click.style("✗", fg="red")
        detail = f" — {outcome.detail}" if outcome.detail else ""
        click.echo(f"  {mark} {outcome.target}{detail}")

    if result.outcomes:
        click.echo()
    click.echo(click.style(result.summary, bold=True))


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__, prog_name="sccfm-msp")
def cli() -> None:
    """Automate Security Cloud Control Firewall Manager across your Managed Orgs.

    Typical first run:

    \b
      sccfm-msp login                 # store your Manager Org API key
      sccfm-msp tenants list          # see the Managed Orgs you manage
      sccfm-msp api-users create      # create an API-only user in each of them
      sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24
    """


# ----------------------------------------------------------------------
# sccfm-msp login
# ----------------------------------------------------------------------


@cli.command("login")
@_region_option
@click.option(
    "--api-key",
    help=(
        "Manager Org (MSP Portal) API key. Omit it and you are prompted instead, so "
        "the key is not echoed and does not land in your shell history."
    ),
)
def login(portal_region: str, api_key: str) -> None:
    """Verify a Manager Org (MSP Portal) API key and store it in the OS keyring."""
    if not api_key:
        api_key = click.prompt("Manager Org API key", hide_input=True)

    _run(
        StoreMspApiKeyCommand(
            CredentialStore(), portal_region=portal_region, api_key=api_key
        )
    )


# ----------------------------------------------------------------------
# sccfm-msp tenants
# ----------------------------------------------------------------------


@cli.group("tenants")
def tenants_group() -> None:
    """Inspect the Managed Orgs (tenants) your Manager Org manages."""


@tenants_group.command("list")
@_region_option
def tenants_list(portal_region: str) -> None:
    """List managed tenants, their regions, and which have an API-only user."""
    _run(ListTenantsCommand(CredentialStore(), portal_region=portal_region))


# ----------------------------------------------------------------------
# sccfm-msp api-users
# ----------------------------------------------------------------------


@cli.group("api-users")
def api_user_group() -> None:
    """Manage the API-only users this CLI uses to reach each Managed Org."""


@api_user_group.command("create")
@_region_option
@_tenant_option
@click.option(
    "--username",
    default=DEFAULT_API_USER_NAME,
    show_default=True,
    help="Name to give the API-only user in each tenant.",
)
@click.option(
    "--role",
    type=ROLE_CHOICE,
    default="ROLE_ADMIN",
    show_default=True,
    help="Role granted to the API-only user.",
)
@click.option(
    "--replace-existing",
    is_flag=True,
    help="Create a new user even where one is already recorded, replacing the stored token.",
)
def api_user_create(
    portal_region: str,
    tenants: Tuple[str, ...],
    username: str,
    role: str,
    replace_existing: bool,
) -> None:
    """Create an API-only user in each managed tenant and store its token securely.

    These are MSP-level calls, so they all go to your portal's region. Each
    tenant's own region is recorded at the same time, ready for object creation.

    Security Cloud Control shows each token exactly once, so it is written to the
    OS keyring the moment it is issued.
    """
    _run(
        CreateApiUsersCommand(
            CredentialStore(),
            portal_region=portal_region,
            username=username,
            role=UserRole(role),
            tenants=list(tenants),
            replace_existing=replace_existing,
        )
    )


@api_user_group.command("delete")
@_region_option
@_tenant_option
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt. Required when running unattended.",
)
def api_users_delete(
    portal_region: str, tenants: Tuple[str, ...], assume_yes: bool
) -> None:
    """Delete the API-only user this CLI created in each managed tenant.

    Only users recorded locally by 'api-users create' are touched, so another
    automation account cannot be removed by accident. The stored token and the
    index entry go too, which makes this the clean way to undo a demo.

    A tenant whose user is already gone is reported, not failed — the local
    credential is still cleaned up, so a half-finished delete can be re-run.
    """
    store = CredentialStore()

    if not assume_yes:
        recorded = store.provisioned_tenants(portal_region)
        scope = (
            f"{len(recorded)} tenant(s)"
            if not tenants
            else f"{len(tenants)} selected tenant(s)"
        )
        click.confirm(
            f"Delete the API-only user and stored token for {scope} "
            f"in region {portal_region}?",
            abort=True,
        )

    _run(
        DeleteApiUsersCommand(
            store, portal_region=portal_region, tenants=list(tenants)
        )
    )


# ----------------------------------------------------------------------
# sccfm-msp objects
# ----------------------------------------------------------------------


@cli.group("objects")
def objects_group() -> None:
    """Create and delete objects across your Managed Orgs."""


@objects_group.command("create")
@_region_option
@_tenant_option
@click.option("--name", "object_name", required=True, help="Name for the new object.")
@click.option(
    "--type",
    "object_type",
    type=OBJECT_TYPE_CHOICE,
    required=True,
    metavar="TYPE",
    help=(
        "Object type to create, stated explicitly: "
        + " | ".join(OBJECT_TYPES)
        + "."
    ),
)
@click.option(
    "--value",
    "values",
    multiple=True,
    metavar="VALUE",
    help=(
        "Literal value for the object. Repeat for group types. "
        "NETWORK_*: 10.10.10.0/24 — URL_*: https://example.com — "
        "SERVICE_*: TCP/443 or ICMP."
    ),
)
@click.option(
    "--member",
    "members",
    multiple=True,
    metavar="NAME",
    help=(
        "Name of an existing object or group to include in the group. Repeat the "
        "flag. Group types only. Each name is looked up in every target tenant "
        "before anything is created."
    ),
)
@click.option("--description", help="Optional description stored with the object.")
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the exact request body and the tenants it would go to; send nothing.",
)
def objects_create(
    portal_region: str,
    tenants: Tuple[str, ...],
    object_name: str,
    object_type: str,
    values: Tuple[str, ...],
    members: Tuple[str, ...],
    description: str,
    dry_run: bool,
) -> None:
    """Create an object in every managed tenant, or in selected tenants.

    --type is always stated. Single types take one --value; group types take one or
    more, and can also reference existing objects by name with --member:

    \b
      sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24
      sccfm-msp objects create --type NETWORK_GROUP --name lab-nets \\
          --value 10.10.10.0/24 --value 10.20.0.0/16
      sccfm-msp objects create --type NETWORK_GROUP --name estate-nets \\
          --value 10.9.0.0/16 --member branch-networks
      sccfm-msp objects create --type SERVICE_OBJECT --name https --value TCP/443

    One write call is made per tenant, sent to that tenant's own region and
    authenticated as that tenant's API-only user. Tenants without one are reported
    rather than silently skipped.

    Members are UIDs in the payload and UIDs are per-tenant, so every --member is
    looked up in every target tenant first; if any tenant is missing any member,
    nothing is created anywhere.
    """
    _run(
        CreateObjectsCommand(
            CredentialStore(),
            portal_region=portal_region,
            object_name=object_name,
            object_type=object_type,
            values=list(values),
            members=list(members),
            description=description,
            tenants=list(tenants),
            dry_run=dry_run,
        )
    )

@objects_group.command("delete")
@_region_option
@_tenant_option
@click.option("--name", "object_name", required=True, help="Name of the object to delete.")
@click.option(
    "--force",
    is_flag=True,
    help="Delete even when the object is in use by another object.",
)
@click.option(
    "--yes",
    "assume_yes",
    is_flag=True,
    help="Skip the confirmation prompt. Required when running unattended.",
)
def objects_delete(
    portal_region: str,
    tenants: Tuple[str, ...],
    object_name: str,
    force: bool,
    assume_yes: bool,
) -> None:
    """Delete an object by name from every managed tenant, or selected tenants.

    Object UIDs are per-tenant, so the name is resolved separately in each tenant
    before its own delete is issued. One delete call is made per tenant, sent to
    that tenant's own region.

    A tenant that has no object by that name is reported, not failed, so this is
    safe to re-run over a partly-cleaned estate.
    """
    store = CredentialStore()

    if not assume_yes:
        scope = (
            f"{len(tenants)} selected tenant(s)"
            if tenants
            else f"all {len(store.provisioned_tenants(portal_region))} provisioned tenant(s)"
        )
        click.confirm(
            f"Delete object '{object_name}' from {scope} in region {portal_region}?",
            abort=True,
        )

    _run(
        DeleteObjectsCommand(
            store,
            portal_region=portal_region,
            object_name=object_name,
            tenants=list(tenants),
            force=force,
        )
    )


def main() -> None:
    """Console-script entry point.

    Click's standalone mode is switched off so unexpected exceptions still reach
    the developer as a traceback, while the errors we expect print as one line.
    """
    try:
        cli(standalone_mode=False)
    except click.ClickException as exc:
        exc.show()
        sys.exit(exc.exit_code)
    except click.exceptions.Exit as exc:
        # Raised by --help, --version, and reported command failures.
        sys.exit(exc.exit_code)
    except click.Abort:
        click.echo("Aborted.", err=True)
        sys.exit(130)


if __name__ == "__main__":
    main()
