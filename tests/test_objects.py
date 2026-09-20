"""Mapping `--type` and `--value` onto the SDK's object payloads."""

from __future__ import annotations

import json

import pytest

from sccfm_msp.errors import MspCliError
from sccfm_msp.objects import OBJECT_TYPES, build_create_request, is_group


def payload(**kwargs) -> dict:
    """The JSON the SDK would actually send."""
    return json.loads(build_create_request(**kwargs).to_json())


# ----------------------------------------------------------------------
# Single-value types
# ----------------------------------------------------------------------


def test_network_object_carries_the_literal():
    body = payload(name="lab-net", object_type="NETWORK_OBJECT", values=["10.10.10.0/24"])

    assert body["value"]["objectType"] == "NETWORK_OBJECT"
    assert body["value"]["defaultContent"] == {"literal": "10.10.10.0/24"}


def test_url_object_carries_the_url():
    body = payload(name="intranet", object_type="URL_OBJECT", values=["https://x.example"])

    assert body["value"]["defaultContent"] == {"url": "https://x.example"}


def test_service_object_splits_protocol_and_ports():
    body = payload(name="https", object_type="SERVICE_OBJECT", values=["tcp/443"])

    assert body["value"]["defaultContent"] == {
        "protocol": "TCP",  # uppercased: the API rejects lowercase
        "serviceValue": {"literal": "443"},
    }


def test_service_object_accepts_a_bare_protocol():
    body = payload(name="ping", object_type="SERVICE_OBJECT", values=["icmp"])

    assert body["value"]["defaultContent"] == {"protocol": "ICMP"}


# ----------------------------------------------------------------------
# Group types
# ----------------------------------------------------------------------


def test_network_group_holds_every_value():
    body = payload(
        name="lab-nets",
        object_type="NETWORK_GROUP",
        values=["10.10.10.0/24", "10.20.0.0/16"],
    )

    assert body["value"]["defaultContent"] == {
        "literals": [{"literal": "10.10.10.0/24"}, {"literal": "10.20.0.0/16"}]
    }


def test_service_group_holds_mixed_protocols():
    body = payload(
        name="web", object_type="SERVICE_GROUP", values=["TCP/443", "UDP/53"]
    )

    assert body["value"]["defaultContent"]["literals"] == [
        {"protocol": "TCP", "serviceValue": {"literal": "443"}},
        {"protocol": "UDP", "serviceValue": {"literal": "53"}},
    ]


def test_a_group_of_one_is_still_a_group():
    body = payload(name="g", object_type="URL_GROUP", values=["https://x.example"])

    assert body["value"]["defaultContent"] == {"literals": [{"url": "https://x.example"}]}


@pytest.mark.parametrize("object_type", OBJECT_TYPES)
def test_every_supported_type_builds(object_type):
    value = {
        "NETWORK": "10.0.0.0/8",
        "URL": "https://x.example",
        "SERVICE": "TCP/80",
    }[object_type.split("_")[0]]

    body = payload(name="demo", object_type=object_type, values=[value])

    assert body["value"]["objectType"] == object_type
    assert ("literals" in body["value"]["defaultContent"]) is is_group(object_type)


# ----------------------------------------------------------------------
# Rejected input
# ----------------------------------------------------------------------


def test_single_type_rejects_several_values_and_names_the_group_type():
    with pytest.raises(MspCliError, match="NETWORK_GROUP"):
        payload(
            name="x", object_type="NETWORK_OBJECT", values=["10.0.0.0/8", "10.1.0.0/16"]
        )


def test_a_single_type_needs_its_one_value():
    with pytest.raises(MspCliError, match="needs one --value"):
        payload(name="x", object_type="NETWORK_OBJECT", values=[])


def test_blank_values_are_not_values():
    with pytest.raises(MspCliError, match="at least one --value"):
        payload(name="x", object_type="NETWORK_GROUP", values=["", "   "])


def test_unknown_type_is_rejected_with_the_valid_list():
    with pytest.raises(MspCliError, match="NETWORK_OBJECT"):
        payload(name="x", object_type="FIREWALL_OBJECT", values=["10.0.0.0/8"])


def test_type_is_case_insensitive():
    body = payload(name="x", object_type="network_object", values=["10.0.0.0/8"])

    assert body["value"]["objectType"] == "NETWORK_OBJECT"


def test_unknown_protocol_is_reported_without_dumping_every_protocol():
    with pytest.raises(MspCliError) as caught:
        payload(name="x", object_type="SERVICE_OBJECT", values=["NOTAPROTOCOL/80"])

    message = str(caught.value)
    assert "NOTAPROTOCOL" in message
    assert "TCP" in message  # a usable suggestion
    # The raw pydantic error lists ~130 protocols; that must not reach the user.
    assert "BBNRCCMON" not in message
    assert len(message) < 250


def test_service_value_with_a_trailing_slash_is_rejected():
    with pytest.raises(MspCliError, match="no ports after it"):
        payload(name="x", object_type="SERVICE_OBJECT", values=["TCP/"])
