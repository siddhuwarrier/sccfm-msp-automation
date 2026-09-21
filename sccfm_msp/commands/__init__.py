"""Every action this CLI can perform, one class per action."""

from .base import (
    Command,
    CommandInvoker,
    CommandResult,
    ItemOutcome,
    NullProgress,
    Progress,
)
from .create_api_users import CreateApiUsersCommand
from .create_objects import CreateObjectsCommand
from .delete_api_users import DeleteApiUsersCommand
from .delete_objects import DeleteObjectsCommand
from .list_tenants import ListTenantsCommand
from .login import StoreMspApiKeyCommand

__all__ = [
    "Command",
    "CommandInvoker",
    "CommandResult",
    "ItemOutcome",
    "NullProgress",
    "Progress",
    "CreateApiUsersCommand",
    "CreateObjectsCommand",
    "DeleteApiUsersCommand",
    "DeleteObjectsCommand",
    "ListTenantsCommand",
    "StoreMspApiKeyCommand",
]
