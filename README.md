# sccfm-msp-automation

> **Personal project — not affiliated with, endorsed by, or supported by Cisco.**
> A reference implementation for people building their own automation, not a
> product. There is no support commitment and no guarantee it tracks the API.
> Fork it, read it, adapt it — don't depend on it.

A reference implementation of how a managed service provider can automate
[Security Cloud Control Firewall Manager](https://scc-firewall-manager-sdk.readthedocs.io/en/stable/)
across every tenant it manages, using the official Python SDK.

The patterns are the point more than the commands are:

* **One credential per boundary.** `msp_portal_client()` carries the MSP portal key;
  `tenant_client()` carries one tenant's API-only user token. Both require a region,
  so no call can be routed without saying which endpoint it means.
* **MSP-level vs tenant-level calls go to different regions.** Getting this wrong is
  invisible until it 404s. See [Regions](#regions).
* **Validate across the whole estate before changing any of it.** Group members are
  resolved in every target tenant first; if one tenant can't satisfy the request,
  nothing is created anywhere.
* **Every action is a command object**, so the same logic runs from the CLI, a
  script, or a test. See [How the code is organised](#how-the-code-is-organised).
* **Secrets go to the OS keyring, never to a file.** See
  [How credentials are stored](#how-credentials-are-stored).

You give it one MSP portal API key. From there it can:

1. **`sccfm-msp api-users create`** — create an API-only user in each managed tenant and
   store that user's API token securely.
2. **`sccfm-msp objects create`** — create the same object in every managed tenant, or in
   a named subset, making one write call per tenant. You state the object type and
   its name, then give it literal values, references to objects that already exist
   in each tenant, or both.

Both have a matching `delete`, so a demo can be undone cleanly.

Step 2 deliberately depends on step 1: objects are created using each tenant's own
API-only user, never the MSP portal key.

An MSP portal in one region can manage tenants in others, and the two steps treat
that differently:

* Step 1 is made of **MSP-level** calls, which always go to your **portal's**
  region — even the ones acting on a tenant.
* Step 2 is a **tenant-level** call, so it goes to each **tenant's own** region,
  read from that tenant's `region` field rather than assumed from your portal.

Step 1 records each tenant's region as it goes, so step 2 knows where to send its
calls without another lookup.

---

## Install

```bash
git clone https://github.com/siddhuwarrier/sccfm-msp-automation
cd sccfm-msp-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

Requires Python 3.9 or newer. Verified on 3.12 and 3.14. Not published to PyPI —
it is meant to be read and forked, so clone it.

## Use it

### 1. Store your MSP portal API key

```bash
sccfm-msp login --region US
```

`--region` is where your **MSP portal** lives (`US`, `EU`, `APJ`, `AUS`, `IN`,
`UAE`; case-insensitive, default `US`). All MSP-level calls go there. You never
specify your tenants' regions — those are detected automatically and only used
when creating objects.

You are prompted for the key, so it never lands in your shell history. The key is
checked against the API before it is saved — if it cannot list managed tenants,
it is not an MSP portal key and `login` tells you so instead of storing it.

```
  ✓ region US — key verified and stored

MSP portal API key stored for region 'US'. 3 managed tenant(s) visible.
```

### 2. See what you manage

```bash
sccfm-msp tenants list
```

```
  ✗ Acme Retail — region US — no API-only user yet
  ✗ Borealis Health — region EU — no API-only user yet
  ✗ Cinder Logistics — region APJ — no API-only user yet

3 managed tenant(s) across 3 region(s) (APJ, EU, US); 0 ready for object creation.
```

Note the portal is in `US` but two of its tenants are not. That is the case the
rest of this CLI is built to handle.

### 3. Create an API-only user in every tenant

```bash
sccfm-msp api-users create
```

```
  ✓ Acme Retail — API-only user 'msp-automation' created, token stored (tenant region US)
  ✓ Borealis Health — API-only user 'msp-automation' created, token stored (tenant region EU)
  ✓ Cinder Logistics — API-only user 'msp-automation' created, token stored (tenant region APJ)

Created 3 API-only user(s) across 3 tenant(s) in 3 tenant region(s): APJ, EU, US.
```

Every call here goes to your portal's region — the `EU` tenant is provisioned over
the `US` endpoint. The tenant regions shown in brackets are being *recorded*, not
used yet; they are what step 4 will route on.

Security Cloud Control shows each API token **exactly once**, at the moment it is
generated. This command writes it straight to your OS keyring, so it is captured
before it is lost.

Re-running is safe: tenants that already have a recorded user are left alone
unless you pass `--replace-existing`. Useful options:

| Option | Purpose |
| --- | --- |
| `--username NAME` | Name for the user in each tenant (default `msp-automation`) |
| `--role ROLE_ADMIN` | Role to grant (`ROLE_READ_ONLY`, `ROLE_EDIT_ONLY`, …) |
| `--tenant NAME_OR_UID` | Only these tenants; repeat the flag |
| `--replace-existing` | Issue a fresh user and token even where one exists |

`sccfm-msp api-users delete` reverses this — see [Clean up](#5-clean-up).

### 4. Create objects across your tenants

All managed tenants:

```bash
sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24
```

```
  ✓ Acme Retail — region US — uid 8f2c1e04-…
  ✓ Borealis Health — region EU — uid 41ab77d9-…
  ✓ Cinder Logistics — region APJ — uid c07e5b32-…

Created NETWORK_OBJECT 'lab-net' (10.10.10.0/24) in 3 of 3 tenant(s) across 3 region(s): APJ, EU, US.
```

This is where region matters. Each line is a separate call to **that tenant's**
regional endpoint, authenticated as that tenant's API-only user — the `EU` tenant
is written to over `api.eu.security.cisco.com`, not your portal's `US` host. Both
the region and the token come from the record written in step 3, so no extra
portal lookup is needed.

#### Object types and values

`--type` is **required** — the object type is always stated, never inferred from
what the value looks like. `--value` supplies literal contents: single types take
one, group types take as many as you repeat the flag.

| `--type` | `--value` format | Values |
| --- | --- | --- |
| `NETWORK_OBJECT` | IP or CIDR — `10.10.10.0/24` | one |
| `NETWORK_GROUP` | IP or CIDR | one or more |
| `URL_OBJECT` | URL — `https://intranet.example.com` | one |
| `URL_GROUP` | URL | one or more |
| `SERVICE_OBJECT` | protocol, optionally with ports — `TCP/443`, `ICMP` | one |
| `SERVICE_GROUP` | protocol with ports | one or more |

```bash
# A group of two networks
sccfm-msp objects create --type NETWORK_GROUP --name lab-nets \
    --value 10.10.10.0/24 --value 10.20.0.0/16

# A service group spanning protocols
sccfm-msp objects create --type SERVICE_GROUP --name web-ports \
    --value TCP/443 --value UDP/53

# A single URL object
sccfm-msp objects create --type URL_OBJECT --name intranet \
    --value https://intranet.example.com
```

#### Groups of existing objects

A group can also reference objects and groups that already exist in each tenant,
by name, with `--member`. Mix it with `--value` freely:

```bash
# A group holding one literal network plus two existing objects
sccfm-msp objects create --type NETWORK_GROUP --name estate-nets \
    --value 10.9.0.0/16 \
    --member branch-networks --member datacentre-net
```

```
  ✓ Acme Retail — region US — uid 8f2c1e04-… — 2 member(s) referenced
  ✓ Borealis Health — region EU — uid 41ab77d9-… — 2 member(s) referenced

Created NETWORK_GROUP 'estate-nets' (1 value(s) + 2 member(s)) in 2 of 2 tenant(s) across 2 region(s): EU, US.
```

A member may itself be a group, so groups nest.

**Members are validated everywhere before anything is created.** A reference is a
UID in the payload, and a UID only means something inside one tenant — so the same
member name resolves to a *different* UID in every tenant. The command therefore
runs in two phases: look every member up in every target tenant, then write. If any
tenant is missing any member, nothing is created anywhere:

```
Error: Nothing was created. Every --member must already exist in every target
tenant, and these tenants could not satisfy that:
  Borealis Health (uid-b): no object named 'datacentre-net'
```

A member of the wrong family is refused the same way — a `URL_OBJECT` cannot go
into a `NETWORK_GROUP`:

```
  Acme Retail (uid-a): wrong type for a NETWORK group — 'a-url' is a URL_OBJECT
```

Only groups can take `--member`; asking a single type to reference something names
the group type to use instead. `--dry-run` performs the same lookups (they are
read-only) and prints the resolved body for each tenant, so you can confirm every
member was found without writing anything.

Passing several values to a single type is refused, with the fix named:

```
Error: NETWORK_OBJECT holds a single value, but 2 were given.
Use --type NETWORK_GROUP to combine several values into one object.
```

Values are validated once, before any tenant is contacted — a typo fails
immediately rather than partway through your estate.

#### Seeing the request body

`--dry-run` prints the exact JSON that would be sent, and the endpoint each tenant
would receive it on, without making any call:

```bash
sccfm-msp objects create --type NETWORK_OBJECT --name kims-favourite-ip-1 \
    --value 10.2.2.2 --dry-run
```

```
  ✓ kim-test-1 — uid bab405c4-… — region EU — would POST /v1/objects to https://api.eu.security.cisco.com/firewall

Dry run — nothing was sent. Body for NETWORK_OBJECT 'kims-favourite-ip-1':
{
  "name": "kims-favourite-ip-1",
  "value": {
    "defaultContent": {
      "literal": "10.2.2.2"
    },
    "objectType": "NETWORK_OBJECT"
  }
}
```

#### Targeting a subset

By tenant name or UID:

```bash
sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24 \
    --tenant "Acme Retail" --tenant Cinder-Logistics
```

If you name a tenant that has no API-only user yet, the command stops and tells
you how to fix it rather than falling back to the portal key:

```
Error: These tenants have no stored API-only user, so objects cannot be created
in them: Borealis Health
Tenants ready to use: Acme Retail (US), Cinder Logistics (APJ)
To provision the missing ones:  sccfm-msp api-users create --region US --tenant Borealis Health
```

One tenant failing does not stop the others — each tenant is reported on its own
line, and the exit code is non-zero if any of them failed.

#### When a create fails

Every object failure names the **tenant UID**, the region, the endpoint, and
whatever the API said — enough from one line to re-run a single tenant or raise a
support case:

```
  ✗ kim-test-1 — tenant bab405c4-… (region EU): POST /v1/objects failed —
    HTTP 500: Internal Server Error — server said: An unspecified error occurred
```

### 5. Clean up

Both halves are reversible, and both prompt before acting (`--yes` skips it, for
unattended runs).

Remove the object from every tenant, by name:

```bash
sccfm-msp objects delete --name lab-net
```

```
  ✓ Acme Retail — region US — deleted uid 8f2c1e04-…
  ✓ Borealis Health — region EU — deleted uid 41ab77d9-…

Deleted 'lab-net' from 2 of 2 tenant(s) across 2 region(s): EU, US.
```

Object UIDs are per-tenant, so the name is resolved separately in each tenant before
its own delete. A tenant that has no object by that name is **reported, not failed** —
so re-running over a partly-cleaned estate is safe:

```
  ✓ Acme Retail — region US — no object named 'lab-net' — nothing to do
```

An object still in use cannot be deleted; `--force` covers the case where the only
thing holding it is another object.

Then remove the API-only users and the credentials they came with:

```bash
sccfm-msp api-users delete
```

```
  ✓ Acme Retail — deleted 'msp-automation@CDO_cdo_acme' — stored token and record removed
  ✓ Borealis Health — deleted 'msp-automation@CDO_cdo_bor' — stored token and record removed

Deleted 2 API-only user(s) across 2 tenant(s) in region 'US'.
```

Only users this CLI **recorded** are touched, so another automation account cannot
be removed by accident. Note the deleted names: the delete endpoint identifies users
by name and the API knows them by their qualified form
(`requested-name@CDO-tenant-name`), so the name is read back from the API rather than
reconstructed — sending `msp-automation` alone would match nothing.

A user already gone is reported rather than failed, and the local token and record
are cleaned up either way. If the API call *fails*, the local credential is
deliberately kept, so a retry can still find the user instead of orphaning it.

---

## Regions

Two different regions are in play, and the CLI keeps them apart:

| | What it is | Where it comes from |
| --- | --- | --- |
| **Portal region** | Where your MSP portal lives | You, via `--region` |
| **Tenant region** | Where one managed tenant lives | `MspManagedTenantDto.region`, read from the API |

Which one applies depends on the kind of call, not on which tenant is involved:

| Call | Endpoint used |
| --- | --- |
| List managed tenants | Portal region |
| Add an API-only user to a tenant | Portal region |
| Issue that user's API token | Portal region |
| **Create an object in a tenant** | **That tenant's region** |

The first three are MSP-level calls: they are made with the portal key against the
portal, and go to the portal's endpoint even though they act on a tenant. Object
creation is a tenant-level call, made with the tenant's own credential, so it must
go to the tenant's endpoint.

Region codes are the API's own (`US`, `EU`, `APJ`, `AUS`, `IN`, `UAE`, plus
Cisco-internal ones such as `STAGING`). [client.py](sccfm_msp/client.py) derives the
region → base URL table from the SDK's published server list rather than
hardcoding hostnames, so it stays correct if a hostname changes:

```python
REGION_HOSTS = {h["description"].upper(): h["url"]
                for h in Configuration().get_host_settings()}
```

If a tenant reports region `UNKNOWN`, there is no tenant endpoint to call. Such a
tenant still provisions fine — that is MSP-level work — and then fails on its own
line at object creation, with the others still proceeding.

## How credentials are stored

Two kinds of data, kept deliberately apart:

| What | Where | Why |
| --- | --- | --- |
| MSP portal API key, per-tenant API tokens | **OS keyring** via [`keyring`](https://pypi.org/project/keyring/) — macOS Keychain, Windows Credential Locker, Secret Service/KWallet on Linux | Encrypted at rest by the OS; this CLI never writes a secret to a file |
| Tenant UIDs, names, **both regions**, API-user names, timestamps | `~/.config/sccfm-msp/tenants.json` (mode `0600`) | Lets the CLI show which tenants are ready, and route each one to the right region, without reading any secret |

Keyring entries live under the service name `sccfm-msp`, as
`msp-portal:<region>` and `tenant:<tenant-uid>` (tenant UIDs are globally unique,
so no region is needed to key a token). To inspect them on macOS, search for
`sccfm-msp` in Keychain Access.

An index entry looks like this — `tenant_region` is what picks the base URL:

```json
{
  "uid-b": {
    "tenant_uid": "uid-b",
    "tenant_name": "Borealis Health",
    "tenant_region": "EU",
    "portal_region": "US",
    "api_user_name": "msp-automation",
    "api_user_uid": "user-uid-b",
    "created_at": "2026-09-17T14:32:11+00:00"
  }
}
```

## How the code is organised

Every action is a **command object**: constructed with everything it needs, then
run via `execute()`. Nothing in a command knows about `click`, and nothing in the
CLI layer talks to the SDK — so the same commands can be driven from a script, a
scheduled job, or a test.

```
sccfm_msp/
  cli.py                     # click layer only: parse args → build a Command → print
  client.py                  # region → base URL, plus MSP-portal vs. tenant clients
  credentials.py             # keyring-backed secret storage + non-secret index
  tenants.py                 # tenant pagination, region lookup, name matching, polling
  objects.py                 # --type/--value -> SDK object payloads
  api_users.py               # shared user lookup/matching for the api-users commands
  commands/
    base.py                  # Command, CommandResult, ItemOutcome, CommandInvoker
    login.py                 # StoreMspApiKeyCommand
    list_tenants.py          # ListTenantsCommand
    create_api_users.py      # CreateApiUsersCommand
    delete_api_users.py      # DeleteApiUsersCommand
    create_objects.py        # CreateObjectsCommand
    delete_objects.py        # DeleteObjectsCommand
tests/                       # no network, no real keyring
```

The separation in `client.py` is the boundary worth copying. It pairs each
credential with the endpoint that credential belongs to:
`msp_portal_client()` takes the portal key and the portal's region;
`tenant_client()` takes one tenant's API-only token and that tenant's region, and
is the only thing that writes objects. Both require a region argument, so a caller
cannot route a call without saying which endpoint it means.

To add a command, write a `Command` subclass and give it a thin `click` wrapper.

### Adding a different kind of object

All six types live in [objects.py](sccfm_msp/objects.py), the only place that knows how
a `--value` becomes an SDK payload.

`ObjectContent` and `SingleContent` take the same field names, so each builder just
returns a dict of content fields; the caller decides whether to spread it onto an
`ObjectContent` (single object) or onto `SingleContent` entries under
`ObjectContent.literals` (group). To add a type, add a builder to `_FIELD_BUILDERS`
and list the type in `OBJECT_TYPES` — the command and the CLI need no changes.

Service values currently use the simple ports form (`TCP/443`), which maps to
`ServiceObjectValueContent(literal=...)`. That model also carries `source` /
`destination` port pairs and `icmp4Type` / `icmp4Code` / `icmp6Type` / `icmp6Code`,
so richer service values slot into `_service_fields()` without touching anything else.

## Tests

```bash
pip install pytest
pytest
```

83 tests across four files:

| File | Tests | Covers |
| --- | --- | --- |
| `test_commands.py` | 39 | What each command does: one API-only user per tenant, each object call using that tenant's own token, group members resolved per tenant and validated everywhere first, subsets, refusing an unprovisioned tenant, and one failure not stopping the rest |
| `test_objects.py` | 20 | The `--type`/`--value` mapping on its own: every type builds, the serialized JSON is what the API expects, and bad input is refused readably rather than as a pydantic dump |
| `test_delete.py` | 16 | Both deletes: the qualified name the user endpoint needs, per-tenant object UIDs, idempotence, and keeping local credentials when a delete fails |
| `test_cli.py` | 8 | Argument parsing, exit codes, and error presentation |

The region behaviour is asserted directly. The fixtures put the MSP portal in `US`
with tenants in `US`, `EU` and `APJ`, then check that user creation and token
issuance went to the portal's host for *every* tenant, that object creation went
to each tenant's own host, that both regions are recorded per tenant, and that an
`UNKNOWN`-region tenant provisions but fails at object creation.

Tests use an in-memory keyring and stubbed API classes, so they touch neither the
network nor your real keychain. The fakes deliberately mimic two API quirks that
caused real bugs: the add-user transaction returns the *tenant* UID in
`entityUid`, and API-only usernames come back qualified as `name@CDO-tenant-name`.

## SDK version

`pyproject.toml` pins `scc-firewall-manager-sdk==1.22.1598`.

Two defects that shaped earlier versions of this example are fixed in this release,
noted here because you may still hit them on an older pin:

* **Object content was an unusable `oneOf`.** `ObjectContent` was a `oneOf` whose
  `ServiceObjectContent` and `GroupContent` variants declared no required fields, so
  they matched any payload and the union never resolved. Every object **response**
  raised `Multiple matches found when deserializing ... ObjectContent`, *after* the
  object had been created. `ObjectContent` is now one flat schema holding the union
  of the content fields, with `SharedObjectValue.objectType` saying which apply, so
  responses deserialize normally and this CLI uses `create_object()` directly.

* **The package could not be imported at all** between 1.22.625 and roughly 1.22.15xx:
  `BackupResult` subclassed `TaskResultDetails` while `TaskResultDetails` imported
  `BackupResult` for its own `oneOf`, a hard circular import not fixable by import
  ordering.

If you pin an older release, expect one or both. 1.22.624 was the last importable
version before the fixes, and needed a raw-response workaround for object creation.
