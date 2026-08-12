"""Alembic 运行环境。

连接串从 config.settings 读取而不是 alembic.ini，这样数据库密码只存在于 .env，
不会因为 alembic.ini 被提交进仓库而泄露。
"""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from config import settings

# 导入 models 让 Base.metadata 包含全部表定义，autogenerate 才能做差异比对
import models  # noqa: F401
from database import Base

config = context.config
config.set_main_option("sqlalchemy.url", settings.DATABASE_URL)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=settings.DATABASE_URL,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            # 默认不比较类型变化，容易漏掉 String(50) -> String(100) 这类改动
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
