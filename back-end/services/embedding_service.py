import json
import logging

from openai import AsyncOpenAI

from config import settings

logger = logging.getLogger("embedding_service")


class EmbeddingService:
    """智谱 Embedding API 服务"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.LLM_API_KEY,
            base_url=settings.LLM_BASE_URL,
        )
        self.model = settings.EMBEDDING_MODEL

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量生成文本向量"""
        if not texts:
            return []
        try:
            response = await self.client.embeddings.create(
                model=self.model,
                input=texts,
            )
            return [item.embedding for item in response.data]
        except Exception as e:
            logger.error("Embedding failed: %s", e)
            raise

    async def embed_query(self, query: str) -> list[float]:
        """生成查询向量"""
        results = await self.embed_texts([query])
        return results[0] if results else []

    @staticmethod
    def cosine_similarity(a: list[float], b: list[float]) -> float:
        """计算两个向量的余弦相似度"""
        if not a or not b:
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = (sum(x * x for x in a)) ** 0.5
        norm_b = (sum(x * x for x in b)) ** 0.5
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return dot / (norm_a * norm_b)

    @staticmethod
    def serialize(vector: list[float]) -> str:
        return json.dumps(vector)

    @staticmethod
    def deserialize(vector_str: str) -> list[float]:
        return json.loads(vector_str)