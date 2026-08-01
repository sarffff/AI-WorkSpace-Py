from sqlalchemy import create_engine, text, inspect
from config import settings
from database import Base

# 导入所有模型，使其注册到 Base.metadata
import models

# 从 DATABASE_URL 提取数据库信息
# 格式: mysql+pymysql://user:password@host:port/database
from urllib.parse import urlparse

parsed_url = urlparse(settings.DATABASE_URL)
db_name = parsed_url.path.lstrip('/')
db_user = parsed_url.username
db_password = parsed_url.password
db_host = parsed_url.hostname
db_port = parsed_url.port or 3306

# 创建临时连接到 MySQL 服务器（不指定数据库）
temp_engine = create_engine(
    f"mysql+pymysql://{db_user}:{db_password}@{db_host}:{db_port}",
    isolation_level="AUTOCOMMIT"
)

# 创建数据库（如果不存在）
try:
    with temp_engine.connect() as conn:
        conn.execute(text(f"CREATE DATABASE IF NOT EXISTS {db_name}"))
        print(f"✓ 数据库 '{db_name}' 已确认存在")
except Exception as e:
    print(f"✗ 创建数据库失败: {e}")

temp_engine.dispose()

# 现在连接到具体的数据库
from database import engine

print("\n删除现有表...")
Base.metadata.drop_all(bind=engine)
print("✓ 现有表已删除")

print("\n创建新表...")
Base.metadata.create_all(bind=engine)
print("✓ 所有表已创建")

# 验证表是否创建成功
inspector = inspect(engine)
tables = inspector.get_table_names()
print(f"\n✓ 数据库已重置，共创建 {len(tables)} 个表:")
for table in tables:
    print(f"  - {table}")
