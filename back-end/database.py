import logging
import os

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker, DeclarativeBase

from config import settings

logger = logging.getLogger("database")

engine = create_engine(settings.DATABASE_URL, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)

_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI 依赖注入：获取数据库 session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _stamp_alembic_head() -> None:
    """把当前 schema 标记为最新版本，而不是重放迁移。

    走到这里说明表是刚由 create_all 建出来的（或是引入 Alembic 之前就存在的
    老库），schema 已经等于 models.py 的样子。此时执行迁移会撞上
    "table already exists"，正确做法是 stamp。
    """
    try:
        from alembic import command
        from alembic.config import Config

        config = Config(os.path.join(_BACKEND_DIR, "alembic.ini"))
        config.set_main_option("script_location", os.path.join(_BACKEND_DIR, "migrations"))
        config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)
        command.stamp(config, "head")
        logger.info("Stamped database at Alembic head.")
    except Exception as exc:
        # 没装 alembic 也要能跑起来，只是后续 schema 变更得手工处理
        logger.warning(
            "Could not stamp Alembic revision (%s); manage schema changes manually.",
            type(exc).__name__,
        )


def init_db():
    """首次启动时建表并纳入 Alembic 管理；之后 schema 由迁移负责。

    判据是 ``alembic_version`` 表是否存在：
    - 不存在：新库或引入 Alembic 之前的老库 → create_all 建表 + stamp head
    - 存在：Alembic 已接管 → 什么都不做，改 schema 请写迁移并 ``alembic upgrade head``

    这样既保证 clone 下来直接能跑，又不会让 create_all 和迁移互相打脸
    （create_all 悄悄建好新表，随后 upgrade 撞 already exists）。
    """
    inspector = inspect(engine)
    if "alembic_version" in inspector.get_table_names():
        return

    Base.metadata.create_all(bind=engine)

    # 老库的 messages 可能没有 seq 列;它保证同时间戳消息的排序确定性
    inspector = inspect(engine)
    if "messages" in inspector.get_table_names():
        columns = [col["name"] for col in inspector.get_columns("messages")]
        if "seq" not in columns:
            with engine.connect() as conn:
                conn.execute(text(
                    "ALTER TABLE messages ADD COLUMN seq INT AUTO_INCREMENT UNIQUE KEY"
                ))
                conn.commit()

    _stamp_alembic_head()
