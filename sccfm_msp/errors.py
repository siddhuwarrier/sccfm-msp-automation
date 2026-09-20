"""Errors raised by the CLI.

Anything raised as a `MspCliError` is a problem the user can act on, so the CLI
prints the message on its own and does not show a traceback.
"""

from __future__ import annotations

import json

from scc_firewall_manager_sdk.exceptions import ApiException


class MspCliError(Exception):
    """A problem the user can fix (bad input, missing credential, API refusal)."""


class CredentialNotFoundError(MspCliError):
    """A credential we expected to find in the keyring is not there."""


class TransactionFailedError(MspCliError):
    """An asynchronous Security Cloud Control transaction ended in ERROR."""


#: Fields Security Cloud Control uses for the human-readable part of an error.
#: `errorMsg` is the one the Firewall Manager API actually sends.
_MESSAGE_KEYS = (
    "errorMsg",      # Firewall Manager MSP endpoints
    "errorMessage",  # object endpoints
    "message",
    "detail",
    "error_description",
    "error",
    "title",
)


def server_message(exc: ApiException) -> str:
    """The API's own error text, which usually says more than the status code.

    Returned with a leading separator so it can be appended to a message, or is
    empty when the response had no usable body. The raw body is a wall of JSON, so
    the readable message is pulled out and the request ID kept for support.
    """
    body = (exc.body or "").strip()
    if not body:
        return ""

    try:
        payload = json.loads(body)
    except ValueError:
        return f" — server said: {_clip(body)}"

    if not isinstance(payload, dict):
        return f" — server said: {_clip(body)}"

    parts = []
    for key in _MESSAGE_KEYS:
        if payload.get(key):
            parts.append(str(payload[key]).strip())
            break
    else:
        parts.append(_clip(body))

    if payload.get("errorCode"):
        parts.append(f"[{payload['errorCode']}]")

    # Worth surfacing verbatim: this is what Cisco support will ask for.
    request_id = (payload.get("details") or {}).get("requestId")
    if request_id:
        parts.append(f"(request ID {request_id})")

    return " — server said: " + " ".join(parts)


def _clip(text: str, limit: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "…"
