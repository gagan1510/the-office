"""Public agent-adapter seam for backend callers."""

from office_agents import ADAPTERS, AgentAdapter, ClaudeAdapter, CodexAdapter, adapter_for

__all__ = ["ADAPTERS", "AgentAdapter", "ClaudeAdapter", "CodexAdapter", "adapter_for"]
