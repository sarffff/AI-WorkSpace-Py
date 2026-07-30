from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://root:2216@localhost:3306/ai_workspace"
    PORT: int = 3000
    LLM_API_KEY: str = "9aced57f3f7e492db501c7a9e97b00e4.DTb6DY07uatScrFF"
    LLM_BASE_URL: str = "https://open.bigmodel.cn/api/paas/v4/"
    LLM_MODEL: str = "glm-4.5-air"
    REDIS_URL: str = ""

    class Config:
        env_file = ".env"


settings = Settings()
