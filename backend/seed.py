#!/usr/bin/env python3
"""Seed the database with demo configuration.

Idempotent: skips records that already exist (matched by unique key).

Usage:
    docker compose exec backend python seed.py
    make seed

Environment:
    DATABASE_URL: PostgreSQL connection string (default: local dev)
"""

import asyncio
import os
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

sys.path.insert(0, os.path.dirname(__file__))

from app.models.cluster import Cluster

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+asyncpg://inkwest:inkwest@localhost:5432/inkwest",  # pragma: allowlist secret
)

DEMO_CLUSTERS = [
    {
        "name": "demo01",
        "display_name": "demo01",
        "kubeconfig_path": "/etc/inkwest/kubeconfigs/demo01",
        "description": "demo01",
    },
    {
        "name": "demo02",
        "display_name": "demo02",
        "kubeconfig_path": "/etc/inkwest/kubeconfigs/demo02",
        "description": "demo02",
    },
    {
        "name": "demo03",
        "display_name": "demo03",
        "kubeconfig_path": "/etc/inkwest/kubeconfigs/demo03",
        "description": "demo03",
    },
]


async def seed() -> None:
    engine = create_async_engine(DATABASE_URL)
    async_session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async with async_session() as session:
        created = 0

        for cluster_data in DEMO_CLUSTERS:
            result = await session.execute(
                select(Cluster).where(Cluster.name == cluster_data["name"])
            )
            if result.scalar_one_or_none():
                print(f"  cluster '{cluster_data['name']}' already exists, skipping")
                continue

            session.add(Cluster(**cluster_data))
            created += 1
            print(f"  + cluster '{cluster_data['name']}'")

        await session.commit()
        print(f"\nSeed complete: {created} record(s) created")

    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(seed())
