"""milos: run claude-agent-sdk agents securely on your own GCP project, with
policy-as-code, audited tool calls, and exportable compliance evidence."""

from .client import MilosClient, query
from .errors import (
    MilosError,
    OptionsError,
    PolicyError,
    SessionExists,
    SessionTerminated,
)
from .options import AgentOptions, default_prompt
from .types import (
    AssistantMessage,
    ContentBlock,
    Message,
    PermissionResult,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

__all__ = [
    "AgentOptions",
    "AssistantMessage",
    "ContentBlock",
    "Message",
    "MilosClient",
    "MilosError",
    "OptionsError",
    "PermissionResult",
    "PermissionResultAllow",
    "PermissionResultDeny",
    "PolicyError",
    "ResultMessage",
    "SessionExists",
    "SessionTerminated",
    "SystemMessage",
    "TextBlock",
    "ThinkingBlock",
    "ToolResultBlock",
    "ToolUseBlock",
    "UserMessage",
    "default_prompt",
    "query",
]
