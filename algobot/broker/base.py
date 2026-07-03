"""Broker abstraction: paper and live implementations share one interface.

The OrderManager talks only to :class:`BrokerInterface`; whether an order hits
Fyers or the paper simulator is decided by the ``Mode -> broker`` map it is
constructed with.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from algobot.core.models import Order, Position


class BrokerInterface(ABC):
    """Minimal order/quote/position surface every broker must provide."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short broker identifier: ``"paper"`` or ``"fyers"``."""

    @abstractmethod
    def place_order(self, order: Order) -> Order:
        """Place ``order`` and return it updated (broker_order_id, status).

        Raises :class:`algobot.core.exceptions.BrokerError` on rejection.
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel a pending order. Returns True when the cancel was accepted."""

    @abstractmethod
    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        """Last traded price per symbol (missing symbols simply absent)."""

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """Open positions as seen by the broker."""

    @abstractmethod
    def get_funds(self) -> float:
        """Available funds (rupees) for new positions."""
