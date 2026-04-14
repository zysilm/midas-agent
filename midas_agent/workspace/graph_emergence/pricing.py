"""Pricing engine for Graph Emergence agent marketplace."""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING

from midas_agent.scheduler.training_log import TrainingLog

if TYPE_CHECKING:
    from midas_agent.workspace.graph_emergence.agent import Agent


class PricingEngineBase(ABC):
    """Abstract pricing interface shared by training and production."""

    @abstractmethod
    def calculate_price(self, agent: Agent) -> int:
        raise NotImplementedError


class PricingEngine(PricingEngineBase):
    def __init__(
        self,
        training_log: TrainingLog,
        buffer_multiplier: float = 1.2,
    ) -> None:
        self._training_log = training_log
        self._buffer_multiplier = buffer_multiplier

    def calculate_price(self, agent: Agent) -> int:
        from midas_agent.scheduler.storage import LogFilter

        entries = self._training_log.get_log_entries(
            LogFilter(entity_id=agent.agent_id, type="consume")
        )

        if not entries:
            base_cost = 100
        else:
            base_cost = sum(e.amount for e in entries) / len(entries)

        price = int(base_cost * self._buffer_multiplier)

        balance = self._training_log.get_balance(agent.agent_id)
        if balance < 0:
            price += abs(balance)

        return max(price, 1)
