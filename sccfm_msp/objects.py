"""Turning `--type` and `--value` flags into an SDK create request.

Security Cloud Control models objects as a type plus a content payload. `ObjectContent`
is a single flat schema holding the union of every content field — `literal` for
networks, `url` for URLs, `protocol`/`serviceValue` for services, and `literals` for
groups — and `SharedObjectValue.objectType` says which of them apply. This module is
the one place that mapping lives, so `CreateObjectsCommand` stays about *where* to
send calls rather than *what* to send.

Two families of type:

* **Single objects** (`NETWORK_OBJECT`, `URL_OBJECT`, `SERVICE_OBJECT`) hold exactly
  one value, set directly on `ObjectContent`.
* **Groups** (`NETWORK_GROUP`, `URL_GROUP`, `SERVICE_GROUP`) hold a list of values of
  the corresponding single type, as `ObjectContent.literals` of `SingleContent`.

`ObjectContent` and `SingleContent` take the same field names, so each value is
turned into a plain dict of fields that either model can be built from.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from scc_firewall_manager_sdk import (
    CreateRequest,
    ObjectContent,
    ObjectManagementApi,
    ServiceObjectValueContent,
    SharedObjectValue,
    SingleContent,
    UnifiedObjectListView,
)

from .errors import MspCliError

#: Every object type this CLI can create, in the API's own spelling.
OBJECT_TYPES = (
    "NETWORK_OBJECT",
    "NETWORK_GROUP",
    "URL_OBJECT",
    "URL_GROUP",
    "SERVICE_OBJECT",
    "SERVICE_GROUP",
)

#: What `--value` means for each family, shown in `--help` and in error messages.
VALUE_HELP = {
    "NETWORK": "an IP address or CIDR, e.g. 10.10.10.0/24 or 192.0.2.7",
    "URL": "a URL, e.g. https://intranet.example.com",
    "SERVICE": "a protocol, optionally with ports, e.g. TCP/443 or ICMP",
}

#: The API accepts ~130 IP protocols. Listing them all in an error is unreadable,
#: so only the ones anyone actually types are suggested.
COMMON_PROTOCOLS = ("TCP", "UDP", "TCP_UDP", "ICMP", "ICMP4", "ICMP6", "ESP", "GRE", "ALL")

#: Fields of one value, as accepted by both `ObjectContent` and `SingleContent`.
ContentFields = Dict[str, Any]


def _family(object_type: str) -> str:
    """`NETWORK_GROUP` -> `NETWORK`. Groups share their family's value format."""
    return object_type.split("_")[0]


def is_group(object_type: str) -> bool:
    return object_type.endswith("_GROUP")


def normalize_object_type(object_type: str) -> str:
    code = (object_type or "").strip().upper()
    if code not in OBJECT_TYPES:
        raise MspCliError(
            f"Unknown object type '{object_type}'. "
            f"Choose one of: {', '.join(OBJECT_TYPES)}"
        )
    return code


def _network_fields(value: str) -> ContentFields:
    return {"literal": value}


def _url_fields(value: str) -> ContentFields:
    return {"url": value}


def _service_fields(value: str) -> ContentFields:
    """Parse `TCP/443`, `UDP/53-60` or a bare protocol such as `ICMP`."""
    protocol, separator, ports = value.partition("/")
    protocol = protocol.strip().upper()
    ports = ports.strip()

    if not protocol:
        raise MspCliError(
            f"'{value}' is not a valid service value — expected "
            f"{VALUE_HELP['SERVICE']}"
        )
    if separator and not ports:
        raise MspCliError(
            f"'{value}' has a '/' but no ports after it — expected "
            f"{VALUE_HELP['SERVICE']}"
        )

    # Check the protocol now, so the error names the bad value. Left to the model it
    # would surface as a pydantic dump listing every accepted IP protocol.
    try:
        SingleContent(protocol=protocol)
    except Exception as exc:
        raise MspCliError(
            f"'{protocol}' is not a protocol Security Cloud Control recognises. "
            f"Common choices: {', '.join(COMMON_PROTOCOLS)} "
            "(the API accepts many more IP protocol names)."
        ) from exc

    fields: ContentFields = {"protocol": protocol}
    if ports:
        fields["service_value"] = ServiceObjectValueContent(literal=ports)
    return fields


_FIELD_BUILDERS = {
    "NETWORK": _network_fields,
    "URL": _url_fields,
    "SERVICE": _service_fields,
}


def check_inputs(object_type: str, values: Sequence[str], members: Sequence[str]) -> None:
    """Reject impossible combinations before anything touches the network.

    Separated from :func:`build_create_request` because members are resolved to
    per-tenant UIDs later, so the request itself cannot be built until then — but
    the user should still learn about a bad combination immediately.
    """
    object_type = normalize_object_type(object_type)
    family = _family(object_type)
    values = _clean(values)
    members = _clean(members)

    if is_group(object_type):
        if not values and not members:
            raise MspCliError(
                f"{object_type} needs at least one --value "
                f"({VALUE_HELP[family]}) or one --member."
            )
        return

    if members:
        raise MspCliError(
            f"{object_type} cannot reference other objects — only groups can.\n"
            f"Use --type {family}_GROUP to build a group with --member."
        )
    if not values:
        raise MspCliError(
            f"{object_type} needs one --value ({VALUE_HELP[family]})."
        )
    if len(values) > 1:
        raise MspCliError(
            f"{object_type} holds a single value, but {len(values)} were given.\n"
            f"Use --type {family}_GROUP to combine several values into one object."
        )


def build_create_request(
    name: str,
    object_type: str,
    values: Sequence[str] = (),
    description: Optional[str] = None,
    member_uids: Sequence[str] = (),
) -> CreateRequest:
    """Build the request for one object.

    `member_uids` are UIDs of existing objects or groups to reference. They are
    tenant-specific, so the caller resolves them per tenant and passes them in.
    """
    object_type = normalize_object_type(object_type)
    family = _family(object_type)
    values = _clean(values)
    member_uids = _clean(member_uids)

    check_inputs(object_type, values, member_uids if is_group(object_type) else ())

    build = _FIELD_BUILDERS[family]

    # Anything below can raise pydantic ValidationError — a malformed literal, an
    # unusable value — so it is all translated into a message the user can act on.
    try:
        if is_group(object_type):
            # A group holds inline literals, references to existing objects, or both.
            content = ObjectContent(
                literals=[SingleContent(**build(value)) for value in values] or None,
                referenced_object_uids=list(member_uids) or None,
            )
        else:
            content = ObjectContent(**build(values[0]))

        return CreateRequest(
            name=name,
            description=description,
            value=SharedObjectValue(
                object_type=object_type,
                default_content=content,
            ),
        )
    except MspCliError:
        raise  # Already a clear message; don't wrap it twice.
    except Exception as exc:
        raise MspCliError(
            f"Could not build a {object_type} named '{name}' from "
            f"{values}: {_first_error(exc)}"
        ) from exc


def _clean(items: Sequence[str]) -> List[str]:
    return [item.strip() for item in items if item and item.strip()]


#: How many objects to fetch per page when resolving a name.
OBJECT_PAGE_SIZE = 200


def find_object_by_name(
    object_api: ObjectManagementApi, name: str
) -> Optional[UnifiedObjectListView]:
    """The one object in this tenant whose name matches exactly, or None.

    Object UIDs are per-tenant, so anything addressing an object across tenants —
    `--member` on a group, or deleting by name — has to resolve the name separately
    in each one. The query narrows server-side; the result is still matched exactly
    here, because a Lucene query can return near matches too.

    Takes the API object rather than a client so the caller owns that dependency.
    """
    page = object_api.get_objects(q=f"name:{name}", limit=str(OBJECT_PAGE_SIZE))
    for item in page.items or []:
        if (item.name or "").casefold() == name.casefold():
            return item
    return None


def family_of(object_type: str) -> str:
    """Public form of :func:`_family`, used to check a member's type matches."""
    return _family(normalize_object_type(object_type))


def _first_error(exc: Exception, limit: int = 200) -> str:
    """Pydantic errors are multi-line and can be enormous; keep them readable."""
    lines = [line.strip() for line in str(exc).splitlines() if line.strip()]

    message = str(exc)
    for line in lines:
        if line.startswith("Value error,"):
            message = line[len("Value error,") :].strip()
            break
    else:
        message = lines[-1] if lines else message

    return message if len(message) <= limit else message[:limit] + "…"


def describe_values(object_type: str, values: List[str]) -> str:
    """Short human description used in command summaries."""
    object_type = normalize_object_type(object_type)
    if is_group(object_type) or len(values) > 1:
        return f"{len(values)} value(s)"
    return values[0] if values else "no value"
