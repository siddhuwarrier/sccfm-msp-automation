"""Progress reporting: commands say what they are doing, while they do it."""

from __future__ import annotations

import pytest

from sccfm_msp.commands import (
    CommandInvoker,
    CreateApiUsersCommand,
    CreateObjectsCommand,
    DeleteApiUsersCommand,
    DeleteObjectsCommand,
    ItemOutcome,
    NullProgress,
    Progress,
)

PORTAL_REGION = "US"


class RecordingProgress(Progress):
    """Captures the sequence a command reports, for assertions."""

    def __init__(self) -> None:
        self.events: list[tuple] = []

    def start(self, total, unit="org"):
        self.events.append(("start", total, unit))

    def step(self, target, message):
        self.events.append(("step", target, message))

    def item(self, outcome: ItemOutcome):
        self.events.append(("item", outcome.target, outcome.ok))

    # Convenience views
    @property
    def kinds(self):
        return [e[0] for e in self.events]

    def of(self, kind):
        return [e for e in self.events if e[0] == kind]


def provision(store, fake_apis, progress=None, **kwargs):
    store.save_portal_key(PORTAL_REGION, "portal-key")
    command = CreateApiUsersCommand(store, portal_region=PORTAL_REGION, **kwargs)
    return CommandInvoker(progress=progress).run(command)


# ----------------------------------------------------------------------
# What commands report
# ----------------------------------------------------------------------


def test_progress_is_reported_as_each_org_finishes(store, fake_apis, tenants):
    """The point of this: results must not all arrive at the end."""
    progress = RecordingProgress()

    provision(store, fake_apis, progress=progress)

    # The workload is announced once, before any org is touched.
    assert progress.events[0] == ("start", len(tenants), "org")

    # Every org produced an item, in order, and each was reported before the next
    # org's work began.
    items = progress.of("item")
    assert [e[1] for e in items] == [t.display_name for t in tenants]

    first_item = progress.kinds.index("item")
    last_step_before = progress.kinds[:first_item].count("step")
    assert last_step_before > 0, "steps for org 1 should precede org 1's result"


def test_the_slow_calls_are_named(store, fake_apis, tenants):
    """A blank terminal reads as a hang; each in-flight call should be nameable."""
    progress = RecordingProgress()

    provision(store, fake_apis, progress=progress)

    messages = {e[2] for e in progress.of("step")}
    assert "adding the user" in messages
    assert "waiting for the add-user transaction" in messages
    assert "issuing its API token" in messages

    # Steps are attributed to an org, so a slow one is identifiable.
    assert {e[1] for e in progress.of("step")} == {t.display_name for t in tenants}


def test_failures_are_reported_as_they_happen_too(store, fake_apis, tenants):
    from scc_firewall_manager_sdk.exceptions import ApiException

    fake_apis["users"].failures = {
        tenants[1].uid: ApiException(status=409, reason="Conflict")
    }
    progress = RecordingProgress()

    provision(store, fake_apis, progress=progress)

    reported = {e[1]: e[2] for e in progress.of("item")}
    assert reported[tenants[1].display_name] is False
    # The run continued: every org still got an item.
    assert len(progress.of("item")) == len(tenants)


@pytest.mark.parametrize("command_name", ["objects create", "objects delete", "api-users delete"])
def test_every_fan_out_command_reports_progress(
    store, fake_apis, tenants, command_name
):
    """All four commands fan out over orgs, so all four should stream."""
    provision(store, fake_apis)
    fake_apis["objects"].existing = {
        f"token-for-{t.uid}": {"lab-net": "NETWORK_OBJECT"} for t in tenants
    }

    commands = {
        "objects create": lambda: CreateObjectsCommand(
            store,
            portal_region=PORTAL_REGION,
            object_name="lab-net-2",
            object_type="NETWORK_OBJECT",
            values=["10.1.1.1"],
        ),
        "objects delete": lambda: DeleteObjectsCommand(
            store, portal_region=PORTAL_REGION, object_name="lab-net"
        ),
        "api-users delete": lambda: DeleteApiUsersCommand(
            store, portal_region=PORTAL_REGION
        ),
    }

    progress = RecordingProgress()
    CommandInvoker(progress=progress).run(commands[command_name]())

    assert progress.events[0][0] == "start"
    assert len(progress.of("item")) == len(tenants)
    assert progress.of("step"), "in-flight calls should be named"


def test_commands_work_without_any_progress(store, fake_apis, tenants):
    """The default reporter discards everything, so library use stays quiet."""
    result = provision(store, fake_apis)  # no progress passed

    assert result.ok
    assert CreateApiUsersCommand.progress.__class__ is NullProgress


# ----------------------------------------------------------------------
# Transaction polling latency
# ----------------------------------------------------------------------


def test_first_poll_comes_back_quickly(monkeypatch):
    """A flat 2s first wait was dead time on every org."""
    from scc_firewall_manager_sdk import CdoTransaction

    from sccfm_msp import tenants as tenants_module

    slept: list[float] = []
    monkeypatch.setattr(tenants_module.time, "sleep", slept.append)

    states = iter(["PENDING", "IN_PROGRESS", "DONE"])

    class FakeTransactions:
        def __init__(self, _client=None):
            pass

        def get_transaction(self, uid):
            return CdoTransaction(transactionUid=uid, cdoTransactionStatus=next(states))

    monkeypatch.setattr(tenants_module, "TransactionsApi", FakeTransactions)

    tenants_module.wait_for_transaction(
        object(), CdoTransaction(transactionUid="t1", cdoTransactionStatus="PENDING")
    )

    assert slept, "it should have waited at least once"
    assert slept[0] <= 0.25, "the first re-check should be soon, not seconds away"
    assert slept == sorted(slept), "the interval should back off, not jump about"
    assert max(slept) <= 2.0, "and should cap rather than grow without bound"


def test_polling_still_gives_up_eventually(monkeypatch):
    from scc_firewall_manager_sdk import CdoTransaction

    from sccfm_msp import tenants as tenants_module
    from sccfm_msp.errors import TransactionFailedError

    monkeypatch.setattr(tenants_module.time, "sleep", lambda _s: None)
    clock = iter([0.0] + [i * 10.0 for i in range(1, 40)])
    monkeypatch.setattr(tenants_module.time, "monotonic", lambda: next(clock))

    class Stuck:
        def __init__(self, _client=None):
            pass

        def get_transaction(self, uid):
            return CdoTransaction(transactionUid=uid, cdoTransactionStatus="PENDING")

    monkeypatch.setattr(tenants_module, "TransactionsApi", Stuck)

    with pytest.raises(TransactionFailedError, match="still 'PENDING'"):
        tenants_module.wait_for_transaction(
            object(),
            CdoTransaction(transactionUid="t1", cdoTransactionStatus="PENDING"),
            timeout_seconds=30.0,
        )
