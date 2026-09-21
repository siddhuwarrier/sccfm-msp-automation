# sccfm-msp-automation

[![tests](https://github.com/siddhuwarrier/sccfm-msp-automation/actions/workflows/tests.yml/badge.svg)](https://github.com/siddhuwarrier/sccfm-msp-automation/actions/workflows/tests.yml)

> **Personal project — not affiliated with, endorsed by, or supported by Cisco.**
> A reference implementation to read and fork, not a product. No support
> commitment, no guarantee it tracks the API.

Automate Cisco Security Cloud Control Firewall Manager across every org an MSP
manages, using the official Python SDK. One API key in, fan-out operations out:

```bash
sccfm-msp login                              # store your Manager Org API key
sccfm-msp tenants list                       # see the orgs you manage
sccfm-msp api-users create                   # an API-only user in each one
sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24
```

That last command makes one call per Managed Org, each to the right regional
endpoint, authenticated as that org's own API-only user.

**Contents** — [Concepts](#concepts) · [Install](#install-and-quickstart) · [Walkthrough](#walkthrough) ·
[Which endpoint a call goes to](#which-endpoint-a-call-goes-to) ·
[Where credentials live](#where-credentials-live) · [Tests and CI](#tests) ·
[SDK version notes](#sdk-version-notes)

## Install and Quickstart

```bash
git clone https://github.com/siddhuwarrier/sccfm-msp-automation
cd sccfm-msp-automation
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
sccfm-msp --help
```

Python 3.9+. Verified on 3.12 and 3.14. Not on PyPI — it is meant to be read and
forked, so clone it.

The official SDK this is built on:
[`scc-firewall-manager-sdk`](https://pypi.org/project/scc-firewall-manager-sdk/) ·
[docs](https://scc-firewall-manager-sdk.readthedocs.io/en/stable/) ·
[API reference](https://developer.cisco.com/docs/cisco-security-cloud-control-firewall-manager/introduction/).
Published by Cisco Security Cloud Control TAC; this repository is not.

---

## Concepts

| Term | What it is |
| --- | --- |
| **Manager Org (MSP Portal)** | The org you log into as an MSP. Holds the API key you provide, and manages the others. |
| **Managed Org (Tenant)** | One customer org that the Manager Org manages. Has its own region, its own users, and its own objects. |
| **API-only user** | A non-human user created inside a Managed Org. Its token is what actually writes objects there. |

Two things follow from this, and they shape everything below:

1. **A Manager Org can manage Managed Orgs in other regions.** So "which endpoint
   does this call go to" has two possible answers — see
   [Which endpoint a call goes to](#which-endpoint-a-call-goes-to).
2. **UIDs are per-org.** The same object or user name is a different UID in every
   Managed Org, so anything addressed by name must be resolved org by org.

> **On naming:** the API and SDK still say *tenant* — `/v1/msp/tenants`,
> `tenantUid`, `MspManagedTenantDto`. This README uses the Manager Org / Managed Org
> terminology, but command and flag names (`tenants list`, `--tenant`) deliberately
> match the API, so the mapping to the SDK stays obvious.

---

## Walkthrough

### 1. Store your Manager Org API key

```bash
sccfm-msp login --region US
```

You are prompted for the key, so it never reaches your shell history. It is verified
before being saved — a key that cannot list Managed Orgs is not a Manager Org key,
and `login` says so rather than storing it.

`--region` is where your **Manager Org** (MSP Portal) lives (`US`, `EU`, `APJ`, `AUS`, `IN`,
`UAE`; case-insensitive, default `US`). You never specify your Managed Orgs' regions; the tool figures it out.

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

The Manager Org is in `US` but two of its Managed Orgs are not. That is the case the
rest of this CLI is built around.

### 3. Create an API-only user in every Managed Org

```bash
sccfm-msp api-users create
```

```
  ✓ Acme Retail — API-only user 'msp-automation' created, token stored (tenant region US)
  ✓ Borealis Health — API-only user 'msp-automation' created, token stored (tenant region EU)
  ✓ Cinder Logistics — API-only user 'msp-automation' created, token stored (tenant region APJ)

Created 3 API-only user(s) across 3 tenant(s) in 3 tenant region(s): APJ, EU, US.
```

Security Cloud Control shows each token **exactly once**, when it is generated, so it
goes straight to the OS keyring. The regions in brackets are being *recorded* for
step 4, not used here.

This is the slowest command: four API calls per org, one of which waits on an
asynchronous transaction. It reports as it goes rather than at the end — each org's
result appears the moment it lands, and the call in flight shows on a transient line
that gets overwritten, so the finished output is just the results:

```
Working through 3 orgs…
  [2/3] Borealis Health: waiting for the add-user transaction…      <- transient
```

Re-running is safe — orgs that already have a recorded user are skipped unless you
pass `--replace-existing`.

| Option | Purpose |
| --- | --- |
| `--username NAME` | Name for the user in each org (default `msp-automation`) |
| `--role ROLE_ADMIN` | Role to grant (`ROLE_READ_ONLY`, `ROLE_EDIT_ONLY`, …) |
| `--tenant NAME_OR_UID` | Only these orgs; repeat the flag |
| `--replace-existing` | Issue a fresh user and token even where one exists |

### 4. Create objects across your Managed Orgs

```bash
sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24
```

```
  ✓ Acme Retail — region US — uid 8f2c1e04-…
  ✓ Borealis Health — region EU — uid 41ab77d9-…
  ✓ Cinder Logistics — region APJ — uid c07e5b32-…

Created NETWORK_OBJECT 'lab-net' (10.10.10.0/24) in 3 of 3 tenant(s) across 3 region(s): APJ, EU, US.
```

One call per Managed Org, each to **that org's** regional endpoint as that org's
API-only user — the `EU` org is written to over `api.eu.security.cisco.com`, not your
Manager Org's `US` host. Region and token both come from step 3, so no extra lookup.

#### Object types and values

`--type` is **required**; the type is always stated, never guessed from the value.
`--value` gives literal contents — single types take one, group types take as many as
you repeat the flag.

| `--type` | `--value` format | Values |
| --- | --- | --- |
| `NETWORK_OBJECT` | IP or CIDR — `10.10.10.0/24` | one |
| `NETWORK_GROUP` | IP or CIDR | one or more |
| `URL_OBJECT` | URL — `https://intranet.example.com` | one |
| `URL_GROUP` | URL | one or more |
| `SERVICE_OBJECT` | protocol, optionally with ports — `TCP/443`, `ICMP` | one |
| `SERVICE_GROUP` | protocol with ports | one or more |

```bash
sccfm-msp objects create --type NETWORK_GROUP --name lab-nets \
    --value 10.10.10.0/24 --value 10.20.0.0/16

sccfm-msp objects create --type SERVICE_GROUP --name web-ports \
    --value TCP/443 --value UDP/53
```

Values are validated once, before any org is contacted, so a typo fails immediately
rather than partway through your estate. Passing several values to a single type is
refused and names the fix:

```
Error: NETWORK_OBJECT holds a single value, but 2 were given.
Use --type NETWORK_GROUP to combine several values into one object.
```

#### Groups containing existing objects

A group can reference objects and groups that already exist in each Managed Org, by
name, with `--member`. Mix it with `--value` freely, and a member may itself be a
group, so groups nest.

```bash
sccfm-msp objects create --type NETWORK_GROUP --name estate-nets \
    --value 10.9.0.0/16 \
    --member branch-networks --member datacentre-net
```

```
  ✓ Acme Retail — region US — uid 8f2c1e04-… — 2 member(s) referenced
  ✓ Borealis Health — region EU — uid 41ab77d9-… — 2 member(s) referenced

Created NETWORK_GROUP 'estate-nets' (1 value(s) + 2 member(s)) in 2 of 2 tenant(s) across 2 region(s): EU, US.
```

**Members are validated everywhere before anything is created.** A reference is a
UID, and a UID only means something inside one org — so the same member name resolves
to a *different* UID in each. The command looks every member up in every target org
first, then writes. If any org is missing any member, nothing is created anywhere:

```
Error: Nothing was created. Every --member must already exist in every target
tenant, and these tenants could not satisfy that:
  Borealis Health (uid-b): no object named 'datacentre-net'
```

A member of the wrong family is refused the same way — a `URL_OBJECT` cannot go into
a `NETWORK_GROUP`. Only groups accept `--member`.

#### Seeing the exact request

`--dry-run` prints the JSON that would be sent and the endpoint each org would
receive it on, without writing anything. With `--member` it performs the same
read-only lookups, so it doubles as an existence check across your estate.

```bash
sccfm-msp objects create --type NETWORK_OBJECT --name lab-net \
    --value 10.2.2.2 --dry-run
```

```
  ✓ Acme Retail — uid uid-a — region US — would POST /v1/objects to https://api.us.security.cisco.com/firewall

Dry run — nothing was written. Body for NETWORK_OBJECT 'lab-net':
{
  "name": "lab-net",
  "value": {
    "defaultContent": { "literal": "10.2.2.2" },
    "objectType": "NETWORK_OBJECT"
  }
}
```

#### Targeting a subset

By org name or UID:

```bash
sccfm-msp objects create --type NETWORK_OBJECT --name lab-net --value 10.10.10.0/24 \
    --tenant "Acme Retail" --tenant Cinder-Logistics
```

Naming an org with no API-only user stops the command and says how to fix it, rather
than falling back to the Manager Org key:

```
Error: These tenants have no stored API-only user, so objects cannot be created
in them: Borealis Health
Tenants ready to use: Acme Retail (US), Cinder Logistics (APJ)
To provision the missing ones:  sccfm-msp api-users create --region US --tenant Borealis Health
```

One org failing does not stop the others, and each failure names the **org UID**, the
region, the endpoint and whatever the API said — enough from one line to re-run that
org or raise a support case:

```
  ✗ kim-test-1 — tenant bab405c4-… (region EU): POST /v1/objects failed —
    HTTP 500: Internal Server Error — server said: An unspecified error occurred
```

### 5. Clean up

Both halves are reversible. Both prompt first; `--yes` skips it for unattended runs.

```bash
sccfm-msp objects delete --name lab-net
sccfm-msp api-users delete
```

```
  ✓ Acme Retail — region US — deleted uid 8f2c1e04-…
  ✓ Borealis Health — region EU — deleted uid 41ab77d9-…

Deleted 'lab-net' from 2 of 2 tenant(s) across 2 region(s): EU, US.
```

```
  ✓ Acme Retail — deleted 'msp-automation@CDO_cdo_acme' — stored token and record removed
  ✓ Borealis Health — deleted 'msp-automation@CDO_cdo_bor' — stored token and record removed

Deleted 2 API-only user(s) across 2 tenant(s) in region 'US'.
```

Both are idempotent: an org that has nothing to delete is **reported, not failed**, so
re-running over a partly-cleaned estate is safe. Notes:

* `objects delete` resolves the name per org. `--force` covers an object whose only
  holder is another object.
* `api-users delete` only touches users this CLI **recorded**, so another automation
  account cannot be removed by accident. Note the deleted names — the endpoint
  identifies users by name and the API knows them qualified as
  `requested-name@CDO-tenant-name`, so the name is read back from the API rather than
  reconstructed. Sending `msp-automation` alone would match nothing.
* If a delete call *fails*, the local credential is deliberately kept, so a retry can
  still find the user instead of orphaning it.

---

## Which endpoint a call goes to

Two regions are in play, and mixing them up is invisible until something 404s.

| | What it is | Where it comes from |
| --- | --- | --- |
| **Manager Org region** | Where your Manager Org lives | You, via `--region` |
| **Managed Org region** | Where one Managed Org lives | `MspManagedTenantDto.region`, read from the API |

Which applies depends on the **kind of call**, not on which org is involved:

| Call | Endpoint |
| --- | --- |
| List Managed Orgs | Manager Org region |
| Add an API-only user to a Managed Org | Manager Org region |
| Issue that user's API token | Manager Org region |
| **Create or delete an object in a Managed Org** | **That Managed Org's region** |

The first three are *MSP-level* calls: made with the Manager Org key against the
Manager Org, so they use its endpoint even though they act on another org. Object
operations are *tenant-level*: made with the Managed Org's own credential, so they
must go to that org's endpoint.

Region codes are the API's own (`US`, `EU`, `APJ`, `AUS`, `IN`, `UAE`, plus internal
ones such as `STAGING`). [client.py](sccfm_msp/client.py) derives the region → base
URL table from the SDK's published server list rather than hardcoding hostnames:

```python
REGION_HOSTS = {h["description"].upper(): h["url"]
                for h in Configuration().get_host_settings()}
```

A Managed Org reporting region `UNKNOWN` has no endpoint to call. It still provisions
fine — that is MSP-level work — and fails on its own line at object creation.

## Where credentials live

Two kinds of data, kept deliberately apart:

| What | Where | Why |
| --- | --- | --- |
| Manager Org API key, per-org API tokens | **OS keyring** via [`keyring`](https://pypi.org/project/keyring/) | Encrypted at rest by the OS; no secret is ever written to a file |
| Org UIDs, names, both regions, API-user names, timestamps | `~/.config/sccfm-msp/tenants.json` (mode `0600`) | Lets the CLI show which orgs are ready, and route each to the right region, without reading any secret |

Keyring entries use the service name `sccfm-msp`, as `msp-portal:<region>` and
`tenant:<org-uid>`. On macOS, search for `sccfm-msp` in Keychain Access.

### Keyring support by platform

`keyring` pulls in the right platform support automatically, so a desktop needs
nothing extra:

| Platform | Backend | Works as-is? |
| --- | --- | --- |
| macOS | Keychain | Yes |
| Windows | Credential Locker (`pywin32-ctypes`) | Yes |
| Linux desktop | Secret Service / KWallet (`SecretStorage` + `jeepney`) | Yes, if gnome-keyring or KWallet is running **and unlocked** |
| Headless Linux — container, CI, plain SSH | none | **No** |

That last row is the one that matters, because it is where automation usually runs. A
bare container has no secret service for `keyring` to talk to. You get an explanation
rather than a traceback, with two ways forward:

* Provide a session — install `gnome-keyring` and run under
  `dbus-run-session -- sccfm-msp ...`.
* Point `keyring` at a backend you trust via `PYTHON_KEYRING_BACKEND` or a
  `keyringrc.cfg`. Backends exist for cloud secret managers, and writing one against
  whatever you already use is straightforward.

**This CLI will not silently fall back to plaintext on disk.** `keyrings.alt` has a
file backend that would make the error go away, but it stores API tokens unencrypted
— which defeats the point of capturing them carefully. If that trade suits your
environment it is one dependency and one environment variable away; it is just not
the default here.

## Tests

```bash
pip install pytest
pytest
```

96 tests, no network and no real keyring — so they need no credentials and run
anywhere, including a CI runner that has no OS keyring.

| File | Tests | Covers |
| --- | --- | --- |
| `test_commands.py` | 39 | Command behaviour: one API-only user per org, each object call using that org's own token, members resolved per org and validated everywhere first, subsets, and one failure not stopping the rest |
| `test_objects.py` | 20 | The `--type`/`--value` mapping alone: every type builds, the JSON matches what the API expects, bad input is refused readably |
| `test_delete.py` | 18 | Both deletes, plus the no-keyring path: qualified usernames, per-org object UIDs, idempotence, keeping credentials when a delete fails |
| `test_progress.py` | 9 | That each org is reported as it finishes, the in-flight call is named, and transaction polling backs off rather than waiting a flat 2s |
| `test_cli.py` | 10 | Argument parsing, exit codes, error presentation |

Region behaviour is asserted directly: the fixtures put the Manager Org in `US` with
Managed Orgs in `US`, `EU` and `APJ`, then check that user creation and token issuance
went to the Manager Org's host for *every* org, and that object operations went to
each org's own host.

The fakes deliberately reproduce two API behaviours that caused real bugs: the
add-user transaction returns the **org's** UID rather than the new user's, and
API-only usernames come back qualified as `name@CDO-tenant-name`.

### Continuous integration

[`.github/workflows/tests.yml`](.github/workflows/tests.yml) runs on every push and
pull request:

| Job | What it proves |
| --- | --- |
| `test` | The suite and `pyflakes` pass on Python 3.9 – 3.14, so the `requires-python` claim stays honest |
| `install` | `pip install -e .` then `sccfm-msp --help` works on Linux, macOS and Windows |

The `install` job exists because the README tells people to clone and install, and
that path deserves testing on the platforms they will actually use.

## SDK version notes

Pinned to `scc-firewall-manager-sdk==1.22.1598`. 