import logging

import redis

from config import settings

logger = logging.getLogger("redis_service")


class RedisService:
    def __init__(self):
        self.client: redis.Redis | None = None
        self.enabled = bool(settings.REDIS_URL)

        if not self.enabled:
            logger.warning("REDIS_URL not configured. Redis is disabled.")
            return

        try:
            self.client = redis.from_url(settings.REDIS_URL, decode_responses=True)
            self.client.ping()
            logger.info("Redis connected.")
        except redis.ConnectionError as e:
            logger.error("Redis connection failed: %s", e)
            self.client = None

    def set(self, key: str, value: str, ttl: int | None = None) -> None:
        if not self.client:
            return
        if ttl:
            self.client.set(key, value, ex=ttl)
        else:
            self.client.set(key, value)

    def get(self, key: str) -> str | None:
        if not self.client:
            return None
        return self.client.get(key)

    def delete(self, key: str) -> int:
        if not self.client:
            return 0
        return self.client.delete(key)