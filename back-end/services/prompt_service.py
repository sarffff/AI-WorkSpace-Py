from sqlalchemy.orm import Session

from models import Prompt


class PromptService:
    """提示词模板服务"""

    async def list_prompts(self, db: Session, user_id: str | None = None) -> list[dict]:
        """获取提示词列表：用户的私有 + 所有公开"""
        query = db.query(Prompt)
        if user_id:
            query = query.filter((Prompt.user_id == user_id) | (Prompt.is_public == True))
        else:
            query = query.filter(Prompt.is_public == True)
        prompts = query.order_by(Prompt.updated_at.desc()).all()
        return [self._to_dict(p) for p in prompts]

    async def get_prompt(self, db: Session, prompt_id: str) -> Prompt | None:
        return db.query(Prompt).filter(Prompt.id == prompt_id).first()

    async def create_prompt(
        self,
        db: Session,
        *,
        title: str,
        content: str,
        description: str | None = None,
        category: str = "General",
        user_id: str | None = None,
        is_public: bool = False,
    ) -> Prompt:
        prompt = Prompt(
            title=title,
            description=description,
            category=category,
            content=content,
            user_id=user_id,
            is_public=is_public,
        )
        db.add(prompt)
        db.commit()
        db.refresh(prompt)
        return prompt

    async def update_prompt(
        self, db: Session, prompt_id: str, *, user_id: str, **fields
    ) -> Prompt | None:
        prompt = db.query(Prompt).filter(Prompt.id == prompt_id).first()
        if not prompt or prompt.user_id != user_id:
            return None
        for k, v in fields.items():
            if hasattr(prompt, k) and v is not None:
                setattr(prompt, k, v)
        db.commit()
        db.refresh(prompt)
        return prompt

    async def delete_prompt(self, db: Session, prompt_id: str, user_id: str) -> bool:
        prompt = db.query(Prompt).filter(Prompt.id == prompt_id).first()
        if not prompt or prompt.user_id != user_id:
            return False
        db.delete(prompt)
        db.commit()
        return True

    @staticmethod
    def _to_dict(p: Prompt) -> dict:
        return {
            "id": p.id,
            "title": p.title,
            "description": p.description,
            "category": p.category,
            "content": p.content,
            "isPublic": p.is_public,
            "userId": p.user_id,
            "createdAt": p.created_at.isoformat(),
            "updatedAt": p.updated_at.isoformat(),
        }
