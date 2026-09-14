"""milos: a secure agent platform on Google Cloud.

Scripts drive sessions with the Agent SDK shape (`query`, `MilosClient`,
`MilosOptions`) or the lower-level `Client`; see `docs/sdk.ipynb`.
"""

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

from .client import ApiError, Client
from .sdk import Message, MilosClient, MilosOptions, query

__version__ = "0.2.0"

__all__ = [
    "ApiError",
    "AssistantMessage",
    "Client",
    "Message",
    "MilosClient",
    "MilosOptions",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "ResultMessage",
    "SystemMessage",
    "TextBlock",
    "ToolPermissionContext",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
    "__version__",
    "query",
]
