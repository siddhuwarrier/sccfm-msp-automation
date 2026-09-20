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
from .errors import CredentialNotFoundError, KeyringUnavailableError

#: What to tell someone whose platform has no usable keyring. The failure is most
#: likely on a headless Linux box, which is exactly where automation tends to run,
#: so it is worth naming the options rather than leaving a library traceback.
NO_KEYRING_HELP = """No OS keyring is available, so credentials cannot be stored or read.

  macOS          Keychain is built in; this should work as-is.
  Windows        Credential Locker is built in; this should work as-is.
  Linux desktop  gnome-keyring or KWallet must be running and unlocked.
  Headless/CI    There is no OS keyring in a bare container or CI runner. Either
                 run inside a session that provides one, for example
                 'dbus-run-session -- sccfm-msp ...' with gnome-keyring
                 installed, or point keyring at a backend you trust using
                 PYTHON_KEYRING_BACKEND.

This CLI deliberately does not fall back to writing secrets in plaintext. For
unattended use, store them in whatever secret manager you already trust and hand
them to keyring through a backend."""

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


def _get_secret(service: str, key: str) -> Optional[str]:
    try:
        return keyring.get_password(service, key)
    except keyring.errors.KeyringError as exc:
        raise KeyringUnavailableError(f"{NO_KEYRING_HELP}\n\nkeyring said: {exc}") from exc


def _set_secret(service: str, key: str, secret: str) -> None:
    try:
        keyring.set_password(service, key, secret)
    except keyring.errors.KeyringError as exc:
        raise KeyringUnavailableError(f"{NO_KEYRING_HELP}\n\nkeyring said: {exc}") from exc


def _delete_secret(service: str, key: str) -> None:
    """Deleting something that was never stored is not an error."""
    try:
        keyring.delete_password(service, key)
    except keyring.errors.PasswordDeleteError:
        pass
    except keyring.errors.KeyringError as exc:
        raise KeyringUnavailableError(f"{NO_KEYRING_HELP}\n\nkeyring said: {exc}") from exc


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
        _set_secret(KEYRING_SERVICE, self._portal_key_id(region), api_key)

    def load_portal_key(self, region: str) -> str:
        token = _get_secret(KEYRING_SERVICE, self._portal_key_id(region))
        if not token:
            raise CredentialNotFoundError(
                f"No Manager Org (MSP Portal) API key stored for region '{region}'.\n"
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
        _set_secret(KEYRING_SERVICE, self._tenant_token_id(tenant_uid), token)

    def load_tenant_token(self, tenant_uid: str) -> str:
        token = _get_secret(KEYRING_SERVICE, self._tenant_token_id(tenant_uid))
        if not token:
            raise CredentialNotFoundError(
                f"No API-only user token stored for tenant {tenant_uid}.\n"
                "Run:  sccfm-msp api-users create"
            )
        return token

    def forget_tenant_token(self, tenant_uid: str) -> None:
        """Drop a tenant's token. Deleting one that was never stored is fine."""
        _delete_secret(KEYRING_SERVICE, self._tenant_token_id(tenant_uid))

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
