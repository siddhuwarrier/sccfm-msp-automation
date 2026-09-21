"""The command pattern used throughout this CLI.

Every user-facing action is a :class:`Command` object: it is constructed with
everything it needs, then run with :meth:`Command.execute`. Nothing in a command
touches `click`, and nothing in `cli.py` talks to the SDK. That split is what
makes the commands reusable from a script, a scheduled job, or a test.

    >>> command = CreateApiUsersCommand(store, region="us", username="msp-automation")
    >>> result = CommandInvoker().run(command)
    >>> result.ok
    True
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class ItemOutcome:
    """The result of one unit of work — typically one tenant."""

    target: str
    ok: bool
    detail: str = ""

    @classmethod
    def success(cls, target: str, detail: str = "") -> "ItemOutcome":
        return cls(target=target, ok=True, detail=detail)

    @classmethod
    def failure(cls, target: str, detail: str) -> "ItemOutcome":
        return cls(target=target, ok=False, detail=detail)


@dataclass
class CommandResult:
    """What a command reports back to whoever ran it."""

    summary: str
    outcomes: List[ItemOutcome] = field(default_factory=list)
    data: Dict[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> List[ItemOutcome]:
        return [o for o in self.outcomes if o.ok]

    @property
    def failed(self) -> List[ItemOutcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def ok(self) -> bool:
        """True when nothing failed. Drives the CLI's exit code."""
        return not self.failed


class Progress:
    """Where a command says what it is doing, while it is doing it.

    These commands fan out over every managed org, several API calls each, and one
    of those calls waits on an asynchronous transaction. Without this the terminal
    sits blank until the whole run finishes, which reads as a hang.

    Commands talk to this instead of printing, so nothing in a command knows about
    `click`. The default implementation discards everything, which keeps tests and
    library use quiet.
    """

    def start(self, total: int, unit: str = "org") -> None:
        """Called once, when the command knows how much work there is."""

    def step(self, target: str, message: str) -> None:
        """Progress *within* one item — the calls that make a run feel slow."""

    def item(self, outcome: "ItemOutcome") -> None:
        """One unit of work finished. Report it now, not at the end."""


class NullProgress(Progress):
    """The default: report nothing."""


class Command(ABC):
    """Base class for every action this CLI can perform."""

    #: Short name used in log lines and error messages.
    name: str = "command"

    #: When True, any failed outcome makes the process exit non-zero. Reporting
    #: commands set this to False: they use the ok flag to mean "ready" rather
    #: than "succeeded", and listing something is not itself a failure.
    failures_are_errors: bool = True

    #: Set by the invoker. Commands may call it freely; by default it does nothing.
    progress: Progress = NullProgress()

    @abstractmethod
    def execute(self) -> CommandResult:
        """Do the work and report what happened."""


class CommandInvoker:
    """Runs commands and keeps a history of what ran.

    The history is what you would extend to add dry-run support, retries, or an
    audit log, without touching any individual command.
    """

    def __init__(self, progress: Optional[Progress] = None) -> None:
        self.history: List[Command] = []
        self.progress = progress or NullProgress()

    def run(self, command: Command) -> CommandResult:
        self.history.append(command)
        command.progress = self.progress
        return command.execute()

    def last(self) -> Optional[Command]:
        return self.history[-1] if self.history else None
