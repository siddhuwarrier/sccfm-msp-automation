"""Regions, base URLs, and the two kinds of SDK client this CLI uses.

**Region codes are the API's own.** `MspManagedTenantDto.region` comes back as
`US`, `EU`, `APJ`, `AUS`, `IN`, `UAE` (plus internal ones like `STAGING`), and
every one of those maps to an entry in the SDK's published server list. This
module derives the region → base URL table from that list rather than hardcoding
hostnames, so it stays correct if Cisco changes one.

Which base URL to use depends on *what kind of call* it is:

* :func:`msp_portal_client` — authenticated with the **MSP portal API key**, always
  against the **portal's own region**. Every MSP-level call uses this, including
  the ones that act on a tenant (listing tenants, adding a user to a tenant,
  issuing that user's token). A portal in `US` does all of that over the `US`
  endpoint even for its `EU` tenants.

* :func:`tenant_client` — authenticated with one tenant's **API-only user token**,
  against **that tenant's own region**. Tenant-level work is where region matters,
  and creating objects is the case in this CLI.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Dict, Generator

from scc_firewall_manager_sdk import ApiClient, Configuration

from .errors import MspCliError

#: Regions an MSP portal is normally deployed in. Offered as CLI choices; the
#: full set below also covers Cisco-internal regions a tenant may report.
PRODUCTION_REGIONS = ("US", "EU", "APJ", "AUS", "IN", "UAE")

DEFAULT_REGION = "US"

#: Reported by the API when a tenant's region could not be determined.
UNKNOWN_REGION = "UNKNOWN"


def _host_registry() -> Dict[str, str]:
    """Region code → base URL, taken from the SDK's own server list.

    The SDK labels each server with a description (`US`, `Staging`, …) that
    matches the region codes the tenant API returns, once upper-cased.
    """
    return {
        host["description"].upper(): host["url"]
        for host in Configuration().get_host_settings()
    }


#: Built once at import time; the SDK's server list is static.
REGION_HOSTS: Dict[str, str] = _host_registry()


def normalize_region(region: str) -> str:
    """Accept `eu` or `EU` and return the canonical API spelling."""
    return (region or "").strip().upper()


def host_for_region(region: str) -> str:
    """Base URL for a region code, with a message the user can act on."""
    code = normalize_region(region)

    if not code:
        raise MspCliError("No region was given.")

    if code == UNKNOWN_REGION:
        raise MspCliError(
            "Security Cloud Control reports this tenant's region as UNKNOWN, so "
            "there is no API endpoint to call. Check the tenant in the MSP portal."
        )

    try:
        return REGION_HOSTS[code]
    except KeyError:
        raise MspCliError(
            f"No API endpoint known for region '{code}'. "
            f"Known regions: {', '.join(sorted(REGION_HOSTS))}"
        ) from None


@contextmanager
def _client(region: str, access_token: str) -> Generator[ApiClient, None, None]:
    configuration = Configuration(
        host=host_for_region(region),
        access_token=access_token,
    )
    with ApiClient(configuration) as api_client:
        yield api_client


@contextmanager
def msp_portal_client(
    region: str, portal_api_key: str
) -> Generator[ApiClient, None, None]:
    """MSP portal key, pointed at the portal's own region."""
    with _client(region, portal_api_key) as api_client:
        yield api_client


@contextmanager
def tenant_client(
    region: str, tenant_token: str
) -> Generator[ApiClient, None, None]:
    """One tenant's API-only user token, pointed at that tenant's region."""
    with _client(region, tenant_token) as api_client:
        yield api_client
