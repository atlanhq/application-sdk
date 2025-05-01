from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from application_sdk.common.logger_adaptors import AtlanLoggerAdapter


class AgentInterface(ABC):
    """Abstract interface for agent implementations."""

    logger: AtlanLoggerAdapter

    @abstractmethod
    def run(self, task: Optional[str] = None) -> None:
        """
        Run the agent with an optional task.
        Args:
            task: Optional task string to process
        """
        pass

    @abstractmethod
    def visualize(self) -> Optional[bytes]:
        """Visualize the agent's workflow/graph structure."""
        pass

    @property
    @abstractmethod
    def state(self) -> Optional[Dict[str, Any]]:
        """Get the current state of the agent."""
        pass
