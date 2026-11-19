"""`make seed`: the business records from data/source/business, in a real PostgreSQL."""

from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CustomerRecord, InvoiceRecord, OrderRecord
from scripts.seed_business_data import load_sources, seed

pytestmark = pytest.mark.integration


async def test_the_seed_loads_every_tenant_and_can_run_again(session: AsyncSession) -> None:
    sources = load_sources()

    await seed(session, sources)
    async with session.begin():
        first_ids = set(await session.scalars(select(CustomerRecord.id)))
    await seed(session, sources)  # idempotent: replaced, not duplicated

    assert set(await session.scalars(select(CustomerRecord.id))) == first_ids  # stable IDs
    counts = dict(
        (
            await session.execute(
                select(CustomerRecord.tenant_id, func.count()).group_by(CustomerRecord.tenant_id)
            )
        ).all()
    )
    assert counts == {"nsi-amer": 8, "nsi-emea": 3}


async def test_amounts_are_computed_from_the_order_lines(session: AsyncSession) -> None:
    await seed(session, load_sources())

    order = await session.scalar(
        select(OrderRecord).where(OrderRecord.order_number == "SO-2026-0107")
    )
    invoice = await session.scalar(
        select(InvoiceRecord).where(InvoiceRecord.invoice_number == "INV-2026-0107")
    )

    # 3 x CP-150S at list price (USD 6,750) + 5 x CV-50 clearance at USD 1,450.
    assert order is not None
    assert invoice is not None
    assert order.total_amount == invoice.amount == Decimal("27500.00")
