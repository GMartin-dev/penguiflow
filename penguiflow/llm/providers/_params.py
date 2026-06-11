"""Shared request-parameter helpers for LLM providers."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..profiles import ModelProfile

logger = logging.getLogger("penguiflow.llm.providers")


def resolve_temperature(
    profile: ModelProfile,
    temperature: float | None,
    *,
    model: str,
    forced_off: bool = False,
) -> float | None:
    """Resolve the temperature value a provider should send.

    Temperature is opt-in: when ``temperature`` is ``None`` no temperature is
    sent and the model uses its provider default.

    When a temperature is explicitly set but the model cannot accept it, the
    value is dropped (with a debug log) instead of triggering a provider 400.
    This happens when either:

    - ``profile.supports_temperature`` is ``False`` (statically known), or
    - ``forced_off`` is ``True`` (a provider learned this at runtime from a
      temperature-related 400 — see ``Provider.mark_temperature_unsupported``).

    Args:
        profile: The model profile.
        temperature: The requested temperature, or ``None`` to omit it.
        model: Model identifier (for logging).
        forced_off: Runtime override that disables temperature for this model.

    Returns:
        The temperature to send, or ``None`` to omit the parameter.
    """
    if temperature is None:
        return None
    if forced_off or not profile.supports_temperature:
        logger.debug(
            "model_does_not_support_temperature",
            extra={
                "model": model,
                "temperature": temperature,
                "reason": "runtime" if forced_off else "profile",
            },
        )
        return None
    return temperature
