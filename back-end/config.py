from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "你的数据库配置"
    PORT: int = 3000
    LLM_API_KEY: str = "你的api"
    LLM_BASE_URL: str = "大模型接口地址"
    LLM_MODEL: str = "模型名称"
    REDIS_URL: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
