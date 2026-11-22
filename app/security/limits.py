"""Abuse and cost limits: requests per user per minute, model tokens per tenant per day.

Both are fixed-window counters in PostgreSQL (``usage_counters``), incremented with one
atomic upsert::

    INSERT ... VALUES (tenant, counter, window, n)
    ON CONFLICT (tenant_id, counter, window_start) DO UPDATE SET value = value + n
    RETURNING value

so every API replica shares the same limits without a separate service, and two
concurrent requests can never both read "one left". Each update runs in its own short
transaction under the tenant's Row-Level Security context.

- **Rate limit** (OWASP API4, LLM10): counted before the endpoint runs; over the limit is a
  429 with ``Retry-After``. Rejected requests count too, so hammering does not help.
- **Token budget** ("denial of wallet", LLM10): checked before any model call and
  charged afterwards with the tokens the provider reported. A request that starts under
  the budget may finish slightly over it; the overrun is bounded by the per-request
  limits (``AGENT_MAX_STEPS``, ``LLM_MAX_OUTPUT_TOKENS``).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import UsageCounterRecord
from app.database.tenancy import tenant_session
from app.tools.authorization import Principal

TOKENS = "tokens"


class LimitExceeded(Exception):
    def __init__(self, detail: str, retry_after_seconds: int) -> None:
        super().__init__(detail)
        self.retry_after_seconds = max(1, retry_after_seconds)


class UsageLimits(Protocol):
    async def count_request(self, principal: Principal) -> None: ...
    async def ensure_token_budget(self, tenant_id: str) -> None: ...
    async def charge_tokens(self, tenant_id: str, tokens: int) -> None: ...


class PostgresUsageLimits:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        requests_per_minute: int,
        daily_token_budget: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._sessions = sessions
        self._requests_per_minute = requests_per_minute
        self._daily_token_budget = daily_token_budget
        self._clock = clock

    async def count_request(self, principal: Principal) -> None:
        now = self._clock()
        window = now.replace(second=0, microsecond=0)
        count = await self._add(principal.tenant_id, f"requests:{principal.user_id}", window, 1)
        if count > self._requests_per_minute:
            retry = math.ceil((window + timedelta(minutes=1) - now).total_seconds())
            raise LimitExceeded("rate limit exceeded", retry)

    async def ensure_token_budget(self, tenant_id: str) -> None:
        now = self._clock()
        day = _day(now)
        async with tenant_session(self._sessions, tenant_id) as session:
            used = await session.scalar(
                select(UsageCounterRecord.value).where(
                    UsageCounterRecord.tenant_id == tenant_id,
                    UsageCounterRecord.counter == TOKENS,
                    UsageCounterRecord.window_start == day,
                )
            )
        if (used or 0) >= self._daily_token_budget:
            retry = math.ceil((day + timedelta(days=1) - now).total_seconds())
            raise LimitExceeded("daily model token budget exhausted for this tenant", retry)

    async def charge_tokens(self, tenant_id: str, tokens: int) -> None:
        if tokens > 0:
            await self._add(tenant_id, TOKENS, _day(self._clock()), tokens)

    async def tokens_used_today(self, tenant_id: str) -> int:
        async with tenant_session(self._sessions, tenant_id) as session:
            used = await session.scalar(
                select(UsageCounterRecord.value).where(
                    UsageCounterRecord.tenant_id == tenant_id,
                    UsageCounterRecord.counter == TOKENS,
                    UsageCounterRecord.window_start == _day(self._clock()),
                )
            )
        return used or 0

    async def _add(self, tenant_id: str, counter: str, window: datetime, amount: int) -> int:
        row = insert(UsageCounterRecord).values(
            tenant_id=tenant_id, counter=counter, window_start=window, value=amount
        )
        upsert = row.on_conflict_do_update(
            index_elements=["tenant_id", "counter", "window_start"],
            set_={"value": UsageCounterRecord.value + row.excluded.value},
        ).returning(UsageCounterRecord.value)
        async with tenant_session(self._sessions, tenant_id) as session, session.begin():
            value = await session.scalar(upsert)
        return int(value or 0)


class Unlimited:
    """No limits (scripts and tests that are not about limits)."""

    async def count_request(self, principal: Principal) -> None:
        return None

    async def ensure_token_budget(self, tenant_id: str) -> None:
        return None

    async def charge_tokens(self, tenant_id: str, tokens: int) -> None:
        return None


class TokenCounter(BaseCallbackHandler):
    """Adds up the tokens every model call reports (LangChain callback).

    Passed in a run's config, it sees each call the run makes, including the agent's
    calls inside the graph, so a request is charged for exactly what it consumed.
    """

    run_inline = True  # called in the event loop, not in a thread

    def __init__(self) -> None:
        self.input_tokens = 0
        self.output_tokens = 0

    @property
    def total(self) -> int:
        return self.input_tokens + self.output_tokens

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None)
                if usage:
                    self.input_tokens += int(usage.get("input_tokens", 0))
                    self.output_tokens += int(usage.get("output_tokens", 0))


def _day(moment: datetime) -> datetime:
    return moment.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
