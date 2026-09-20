"""Secure credential storage.

Two kinds of data are kept, deliberately in two different places:

* **Secrets** (the MSP portal API key, and each tenant's API-only user token) go
  into the operating system keyring via the `keyring` package — macOS Keychain,
  Windows Credential Locker, or Secret Service / KWallet on Linux. They are
  encrypted at rest by the OS and never written to a file by this CLI.

* **Non-secret metadata** (which tenant we provisioned, what we named the user,
  when) goes into a plain JSON index at `~/.config/sccfm-msp/tenants.json`.
  This lets `sccfm-msp tenants list` and `sccfm-msp objects create` see which tenants are ready
  without reading any secret out of the keyring.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

import keyring

from .client import normalize_region
from .errors import CredentialNotFoundError

#: Namespace used for every entry this CLI puts in the OS keyring.
KEYRING_SERVICE = "sccfm-msp"

#: Where the non-secret index lives. Override with SCCFM_MSP_HOME for tests.
DEFAULT_CONFIG_DIR = Path.home() / ".config" / "sccfm-msp"


@dataclass
class TenantRecord:
    """What we know about an API-only user we created in one managed tenant.

    Two regions are recorded, and the distinction matters:

    * `tenant_region` is where the tenant itself lives. It decides which base URL
      to call when doing anything to that tenant.
    * `portal_region` is where the MSP portal that manages it lives. It groups
      tenants by the portal key that can reach them.

    A portal in one region can manage tenants in several others, so these differ.
    """

    tenant_uid: str
    tenant_name: str
    tenant_region: str
    portal_region: str
    api_user_name: str
    api_user_uid: str
    created_at: str

    @classmethod
    def new(
        cls,
        tenant_uid: str,
        tenant_name: str,
        tenant_region: str,
        portal_region: str,
        api_user_name: str,
        api_user_uid: str,
    ) -> "TenantRecord":
        return cls(
            tenant_uid=tenant_uid,
            tenant_name=tenant_name,
            tenant_region=tenant_region,
            portal_region=portal_region,
            api_user_name=api_user_name,
            api_user_uid=api_user_uid,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )


class CredentialStore:
    """Reads and writes the MSP portal key, tenant tokens, and tenant index."""

    def __init__(self, config_dir: Optional[Path] = None) -> None:
        env_home = os.environ.get("SCCFM_MSP_HOME")
        self.config_dir = Path(config_dir or env_home or DEFAULT_CONFIG_DIR)
        self.index_path = self.config_dir / "tenants.json"

    # ------------------------------------------------------------------
    # MSP portal API key
    # ------------------------------------------------------------------

    # Region codes are normalised here too, so `us` and `US` can never end up as
    # two different keyring entries.
    @staticmethod
    def _portal_key_id(region: str) -> str:
        return f"msp-portal:{normalize_region(region)}"

    def save_portal_key(self, region: str, api_key: str) -> None:
        keyring.set_password(KEYRING_SERVICE, self._portal_key_id(region), api_key)

    def load_portal_key(self, region: str) -> str:
        token = keyring.get_password(KEYRING_SERVICE, self._portal_key_id(region))
        if not token:
            raise CredentialNotFoundError(
                f"No MSP portal API key stored for region '{region}'.\n"
                f"Run:  sccfm-msp login --region {region}"
            )
        return token

    # ------------------------------------------------------------------
    # Per-tenant API-only user tokens
    # ------------------------------------------------------------------

    # Tenant UIDs are globally unique, so no region is needed to key a token.
    @staticmethod
    def _tenant_token_id(tenant_uid: str) -> str:
        return f"tenant:{tenant_uid}"

    def save_tenant_token(self, tenant_uid: str, token: str) -> None:
        keyring.set_password(KEYRING_SERVICE, self._tenant_token_id(tenant_uid), token)

    def load_tenant_token(self, tenant_uid: str) -> str:
        token = keyring.get_password(KEYRING_SERVICE, self._tenant_token_id(tenant_uid))
        if not token:
            raise CredentialNotFoundError(
                f"No API-only user token stored for tenant {tenant_uid}.\n"
                "Run:  sccfm-msp api-users create"
            )
        return token

    def forget_tenant_token(self, tenant_uid: str) -> None:
        """Drop a tenant's token. Deleting one that was never stored is fine."""
        try:
            keyring.delete_password(KEYRING_SERVICE, self._tenant_token_id(tenant_uid))
        except keyring.errors.PasswordDeleteError:
            pass

    # ------------------------------------------------------------------
    # Non-secret tenant index
    # ------------------------------------------------------------------

    def _read_index(self) -> Dict[str, dict]:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text())
        except json.JSONDecodeError:
            return {}

    def _write_index(self, index: Dict[str, dict]) -> None:
        # 0700 on the directory and 0600 on the file: the index holds no secrets,
        # but it does reveal the customer's tenant list.
        self.config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.index_path.write_text(json.dumps(index, indent=2, sort_keys=True) + "\n")
        self.index_path.chmod(0o600)

    def record_tenant(self, record: TenantRecord) -> None:
        index = self._read_index()
        index[record.tenant_uid] = asdict(record)
        self._write_index(index)

    def forget_tenant(self, tenant_uid: str) -> None:
        """Remove a tenant's index entry, leaving the keyring untouched."""
        index = self._read_index()
        if index.pop(tenant_uid, None) is not None:
            self._write_index(index)

    def get_tenant_record(self, tenant_uid: str) -> Optional[TenantRecord]:
        entry = self._read_index().get(tenant_uid)
        return TenantRecord(**entry) if entry else None

    def provisioned_tenants(self, portal_region: str) -> List[TenantRecord]:
        """Tenants managed by this MSP portal that have a stored API-only user.

        Filtered by the *portal's* region, not the tenants' own: one portal key
        may reach tenants spread across several regions.
        """
        wanted = normalize_region(portal_region)
        records = [
            TenantRecord(**entry)
            for entry in self._read_index().values()
            if normalize_region(entry.get("portal_region", "")) == wanted
        ]
        return sorted(records, key=lambda r: r.tenant_name)
