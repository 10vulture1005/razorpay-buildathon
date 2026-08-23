"""Explicit migration entrypoint (P1-6). Run in CI/CD BEFORE rolling out the API:

    python -m scripts.migrate upgrade head

Handles empty DBs and legacy create_all DBs exactly as the old startup path did,
but now under operational control instead of at app boot.
"""
import sys


def _alembic_cfg():
    from alembic.config import Config

    import app.config as config

    cfg = Config(str(config.APP_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(config.APP_ROOT / "migrations"))
    return cfg


def main(argv: list[str]) -> int:
    if not argv or argv[0] != "upgrade" or len(argv) < 2:
        print("usage: python -m scripts.migrate upgrade <revision|head>", file=sys.stderr)
        return 2
    target = argv[1]

    import app.config as config
    from alembic import command
    from sqlalchemy import create_engine, inspect

    if "sqlite" in config.DATABASE_URL:
        from app.db.session import init_db

        init_db()
        print("sqlite target: create_all applied (alembic not used for tests)")
        return 0

    cfg = _alembic_cfg()
    engine = create_engine(config.DATABASE_URL)
    with engine.connect() as conn:
        insp = inspect(conn)
        has_version = insp.has_table("alembic_version")
        table_count = len(insp.get_table_names())
    if not has_version and table_count > 0:
        # Legacy DB built by create_all before Alembic existed.
        command.stamp(cfg, "head")
        print("legacy schema detected: stamped head")
        return 0
    command.upgrade(cfg, target)
    print(f"migrated to {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
