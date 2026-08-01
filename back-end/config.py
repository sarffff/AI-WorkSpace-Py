"""
应用配置模块
使用 pydantic-settings 自动从环境变量和 .env 文件读取配置
"""

import os
from typing import Optional

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """
    应用配置类
    """
    # ========== 数据库配置 ==========
    DATABASE_URL: str = "mysql+pymysql://root:password@localhost:3306/ai_workspace_py"
    
    # ========== 服务器配置 ==========
    PORT: int = 3000
    
    # ========== LLM API 配置 ==========
    LLM_API_KEY: str = "your_api_key_here"
    LLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"
    LLM_MODEL: str = "glm-4.5-air"
    
    # ========== Redis 配置 (可选) ==========
    REDIS_URL: Optional[str] = None
    
    # ========== Embedding 配置 ==========
    EMBEDDING_MODEL: str = "embedding-2"

    # ========== JWT 认证配置 ==========
    JWT_SECRET_KEY: str = "your-secret-key-change-this-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRE_MINUTES: int = 10080  # 7天 = 60 * 24 * 7

    class Config:
        """Pydantic 配置"""
        # .env 文件路径 (基于当前文件所在目录,避免因工作目录不同而找不到)
        env_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        # 编码格式
        env_file_encoding = "utf-8"
        # 大小写敏感 (环境变量名必须完全匹配)
        case_sensitive = True

settings = Settings()


# 在开发环境下打印配置信息 (生产环境应该禁用)
if __name__ == "__main__":
    print("当前配置:")
    print(f"DATABASE_URL: {settings.DATABASE_URL}")
    print(f"PORT: {settings.PORT}")
    print(f"LLM_MODEL: {settings.LLM_MODEL}")
    print(f"JWT_SECRET_KEY: {'*' * 20} (已隐藏)")
    print(f"JWT_EXPIRE_MINUTES: {settings.JWT_EXPIRE_MINUTES}")
