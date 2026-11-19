"""Row-Level Security: the second tenant-isolation layer, tested on its own.

These queries deliberately have NO tenant filter: only the database policies stand between
a tenant and the other tenant's rows.
"""

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database.models import ChunkRecord, CustomerRecord, DocumentRecord
from app.database.tenancy import tenant_session
from scripts.seed_business_data import load_sources, seed

pytestmark = pytest.mark.integration


@pytest.fixture
async def sessions_with_data(
    sessions: async_sessionmaker[AsyncSession], session_with_returns_policies: AsyncSession
) -> async_sessionmaker[AsyncSession]:
    """Both tenants have documents, chunks and business records."""
    await seed(session_with_returns_policies, load_sources())
    return sessions


async def _tenants_visible(session: AsyncSession, table: type) -> set[str]:
    return set(await session.scalars(select(table.tenant_id).distinct()))


@pytest.mark.parametrize("tenant_id", ["nsi-amer", "nsi-emea"])
@pytest.mark.parametrize("table", [DocumentRecord, ChunkRecord, CustomerRecord])
async def test_a_tenant_session_only_sees_its_own_rows(
    sessions_with_data: async_sessionmaker[AsyncSession], tenant_id: str, table: type
) -> None:
    async with tenant_session(sessions_with_data, tenant_id) as session:
        assert await _tenants_visible(session, table) == {tenant_id}


async def test_the_owner_session_sees_every_tenant(
    sessions_with_data: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions_with_data() as session:  # migrations, seeding, admin scripts
        assert await _tenants_visible(session, CustomerRecord) == {"nsi-amer", "nsi-emea"}


async def test_a_tenant_cannot_write_rows_for_another_tenant(
    sessions_with_data: async_sessionmaker[AsyncSession],
) -> None:
    async with tenant_session(sessions_with_data, "nsi-emea") as session:
        with pytest.raises(ProgrammingError, match="row-level security"):
            async with session.begin():
                await session.execute(
                    text(
                        "INSERT INTO customers (id, tenant_id, customer_number, name, segment,"
                        " country, support_tier) VALUES (gen_random_uuid(), 'nsi-amer', 'C-9',"
                        " 'Planted', 'x', 'US', 'standard')"
                    )
                )


async def test_without_a_tenant_the_app_role_sees_nothing(
    sessions_with_data: async_sessionmaker[AsyncSession],
) -> None:
    async with sessions_with_data() as session, session.begin():
        await session.execute(text("SET LOCAL ROLE northstar_app"))  # role, but no tenant
        count = await session.scalar(select(func.count()).select_from(DocumentRecord))

    assert count == 0  # fail closed


async def test_every_tenant_table_is_protected_by_a_policy(
    sessions: async_sessionmaker[AsyncSession],
) -> None:
    # A new table with a tenant_id column but without RLS fails here, not in production.
    async with sessions() as session:
        rows = await session.execute(
            text(
                """
                SELECT c.table_name, t.relrowsecurity,
                       EXISTS (SELECT FROM pg_policies p WHERE p.tablename = c.table_name)
                FROM information_schema.columns AS c
                JOIN pg_class AS t ON t.relname = c.table_name
                WHERE c.table_schema = 'public' AND c.column_name = 'tenant_id'
                """
            )
        )
        unprotected = [name for name, rls, policy in rows if not (rls and policy)]

    assert unprotected == []
