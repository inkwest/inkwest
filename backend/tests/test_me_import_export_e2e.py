"""
End-to-end export -> import round-trip against a real Postgres.

Hermetic suites mock the DB; this one exercises the actual UNIQUE(user_id, name) constraint,
the upsert, and the replace_all delete-then-insert against a live database. It SKIPS unless
``INKWEST_TEST_DATABASE_URL`` is set to a ``postgresql+asyncpg://`` DSN for a migrated
Inkwest DB, so CI stays hermetic. Run it with that env var set, e.g.::

    INKWEST_TEST_DATABASE_URL=<asyncpg-dsn> pytest tests/test_me_import_export_e2e.py
"""

import json
import os
import uuid

import pytest
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.api.routes.me import export_my_config, import_my_config
from app.models.schemas import ConfigImport
from app.models.skill import Skill
from app.models.user import User
from app.models.user_skill import UserSkill

pytestmark = pytest.mark.e2e

_URL = os.environ.get("INKWEST_TEST_DATABASE_URL")
_SKILL = "---\nname: {n}\ndescription: d{n}\n---\nBody {n} $ARGUMENTS\n"


class _CurrentUser:
    def __init__(self, uid: uuid.UUID) -> None:
        self.id = uid


@pytest.mark.asyncio
async def test_export_import_round_trip_live() -> None:
    if not _URL:
        pytest.skip("INKWEST_TEST_DATABASE_URL unset — needs a migrated Postgres")

    engine = create_async_engine(
        _URL, connect_args={"server_settings": {"search_path": "inkwest,public"}}
    )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    uid = uuid.uuid4()
    user = _CurrentUser(uid)

    try:
        async with maker() as db:
            try:
                await db.execute(select(UserSkill).limit(1))
            except Exception:  # noqa: BLE001 — schema not migrated / unreachable
                pytest.skip("test DB not migrated / unreachable")

            db.add(
                User(
                    id=uid,
                    oidc_subject=f"s-{uid}",
                    oidc_issuer="local",
                    email=f"{uid}@x",
                    display_name="X",
                    roles=[],
                )
            )
            db.add(
                Skill(
                    name=f"shared-{uid.hex[:8]}", content=_SKILL.format(n="shared"), description="a"
                )
            )
            shared_name = f"shared-{uid.hex[:8]}"
            db.add(
                UserSkill(
                    user_id=uid, name="alpha", content=_SKILL.format(n="alpha"), is_active=True
                )
            )
            db.add(
                UserSkill(
                    user_id=uid, name="beta", content=_SKILL.format(n="beta"), is_active=False
                )
            )
            await db.commit()

            # Export, then re-import verbatim (upsert): state must be byte-identical.
            exported = await export_my_config(user, db)  # type: ignore[arg-type]
            dumped = json.loads(exported.model_dump_json())
            await import_my_config(
                ConfigImport.model_validate(dumped), user, replace_all=False, db=db
            )  # type: ignore[arg-type]
            await db.commit()
            rows = (
                (
                    await db.execute(
                        select(UserSkill).where(UserSkill.user_id == uid).order_by(UserSkill.name)
                    )
                )
                .scalars()
                .all()
            )
            assert [(r.name, r.is_active, r.is_override) for r in rows] == [
                ("alpha", True, False),
                ("beta", False, False),
            ]

            # Upsert a doc whose skill collides with an admin skill name -> is_override true;
            # beta (absent from the file) must survive the upsert.
            doc = {
                "schema_version": 1,
                "preferences": {"ui": {"x": 1}},
                "skills": [{"content": _shared_md(shared_name)}],
            }
            await import_my_config(ConfigImport.model_validate(doc), user, replace_all=False, db=db)  # type: ignore[arg-type]
            await db.commit()
            by_name = {
                r.name: (r.is_active, r.is_override)
                for r in (await db.execute(select(UserSkill).where(UserSkill.user_id == uid)))
                .scalars()
                .all()
            }
            assert "beta" in by_name  # survived upsert
            assert by_name[shared_name][1] is True  # shadows the admin skill

            # replace_all wipes then applies only the file.
            doc2 = {
                "schema_version": 1,
                "preferences": {},
                "skills": [{"content": _SKILL.format(n="only"), "is_active": True}],
            }
            await import_my_config(ConfigImport.model_validate(doc2), user, replace_all=True, db=db)  # type: ignore[arg-type]
            await db.commit()
            names = [
                r.name
                for r in (await db.execute(select(UserSkill).where(UserSkill.user_id == uid)))
                .scalars()
                .all()
            ]
            assert names == ["only"]

        async with maker() as db:  # cleanup in a fresh session
            await db.execute(delete(UserSkill).where(UserSkill.user_id == uid))
            await db.execute(delete(Skill).where(Skill.name == shared_name))
            await db.execute(delete(User).where(User.id == uid))
            await db.commit()
    finally:
        await engine.dispose()


def _shared_md(name: str) -> str:
    """A SKILL.md whose frontmatter name is the (already-slugified) admin skill name."""
    return f"---\nname: {name}\ndescription: forked\n---\nForked body $ARGUMENTS\n"
