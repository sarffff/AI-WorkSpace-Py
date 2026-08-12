import json
import logging

from openai import AsyncOpenAI

from config import settings
from services.telemetry import SpanKind, TokenSource, tracer

logger = logging.getLogger("embedding_service")


class EmbeddingService:
    """Embedding API 服务 (默认智谱,支持独立配置或回退到 LLM 配置)"""

    def __init__(self):
        self.client = AsyncOpenAI(
            api_key=settings.embedding_api_key,
            base_url=settings.embedding_base_url,
        )
        self.model = settings.EMBEDDING_MODEL

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        """批量生成文本向量。

        一篇长文档能切出几百个分块，一次性提交容易超过提供商的单请求上限，
        所以按 EMBEDDING_BATCH_SIZE 分批发送后再拼接。
        """
        if not texts:
            return []

        batch_size = max(1, settings.EMBEDDING_BATCH_SIZE)
        async with tracer.span(
            "embedding.embed",
            SpanKind.EMBEDDING,
            model=self.model,
            texts=len(texts),
            batches=(len(texts) + batch_size - 1) // batch_size,
        ) as span:
            vectors: list[list[float]] = []
            prompt_tokens = 0
            for start in range(0, len(texts), batch_size):
                batch = texts[start : start + batch_size]
                try:
                    response = await self.client.embeddings.create(
                        model=self.model,
                        input=batch,
                    )
                except Exception as e:
                    # 不记录异常详情(可能含用户文本),仅记录类型
                    logger.error("Embedding API failed: %s", type(e).__name__)
                    raise
                usage = getattr(response, "usage", None)
                prompt_tokens += getattr(usage, "prompt_tokens", 0) or 0
                vectors.extend(item.embedding for item in response.data)

            if prompt_tokens:
                # embedding 只有输入侧成本
                span.set_usage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=0,
                    source=TokenSource.PROVIDER,
                    model=self.model,
                )
            span.set(dimension=len(vectors[0]) if vectors else None)
            return vectors

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
    def serialize(vector: list[float], model: str | None = None) -> str:
        return json.dumps(
            {"model": model or settings.EMBEDDING_MODEL, "vector": vector}
        )

    @staticmethod
    def deserialize(vector_str: str) -> list[float]:
        value = json.loads(vector_str)
        if isinstance(value, dict):
            vector = value.get("vector")
            return vector if isinstance(vector, list) else []
        return value if isinstance(value, list) else []

    @staticmethod
    def deserialize_model(vector_str: str) -> str | None:
        value = json.loads(vector_str)
        if isinstance(value, dict) and isinstance(value.get("model"), str):
            return value["model"]
        return None
