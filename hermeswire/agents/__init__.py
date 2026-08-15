"""Agent backends for managing AI coding sessions."""

from .base import AgentBackend
from .tmux import TmuxAgent

__all__ = ["AgentBackend", "TmuxAgent", "get_agent_backend"]


def get_agent_backend(config: dict) -> AgentBackend:
    """Get the tmux agent backend.

    Args:
        config: Configuration dict

    Returns:
        TmuxAgent instance
    """
    return TmuxAgent(config)
