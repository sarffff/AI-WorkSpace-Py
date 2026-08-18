"""
认证相关路由
包括: 注册、登录、登出、获取当前用户信息等
"""
import re
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, EmailStr, Field, field_validator
from sqlalchemy.orm import Session

from auth import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    get_password_hash,
    verify_password,
    blacklist_token,
    security,
    ACCESS_TOKEN_EXPIRE_MINUTES,
    REFRESH_TOKEN_EXPIRE_DAYS,
)
from config import settings
from database import get_db
from models import User, Workspace
from services import workspace_service

# 限流器
from rate_limit import limiter

router = APIRouter(prefix="/auth", tags=["认证"])


# ========== Pydantic 模型 ==========

class RegisterRequest(BaseModel):
    """注册请求"""
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    password: str = Field(..., min_length=8, max_length=100)
    name: Optional[str] = Field(None, max_length=100)
    # 邀请码(可选):填了就加入对应工作区成为 member(共享知识库);
    # 不填则自动创建自己的个人空间并成为其 admin
    invite_code: Optional[str] = Field(None, max_length=16)

    @field_validator("password")
    @classmethod
    def validate_password_strength(cls, v: str) -> str:
        """密码必须包含大小写字母和数字,长度 >= 8"""
        if not re.search(r"[a-z]", v):
            raise ValueError("密码必须包含至少一个小写字母")
        if not re.search(r"[A-Z]", v):
            raise ValueError("密码必须包含至少一个大写字母")
        if not re.search(r"\d", v):
            raise ValueError("密码必须包含至少一个数字")
        return v


class LoginRequest(BaseModel):
    """登录请求"""
    email: str
    password: str


class RefreshRequest(BaseModel):
    """刷新 token 请求"""
    refresh_token: str


class TokenResponse(BaseModel):
    """Token 响应"""
    access_token: str
    refresh_token: Optional[str] = None
    token_type: str = "bearer"
    expires_in: int = ACCESS_TOKEN_EXPIRE_MINUTES * 60  # 秒


class UserResponse(BaseModel):
    """用户信息响应"""
    id: str
    email: str
    username: Optional[str]
    name: Optional[str]
    avatar: Optional[str]
    provider: Optional[str]
    is_active: bool
    is_verified: bool
    created_at: str

    class Config:
        from_attributes = True


class AuthResponse(BaseModel):
    """认证响应 (包含 token 和用户信息)"""
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


# ========== API 端点 ==========

@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("5/minute")
async def register(
    request: Request,
    body: RegisterRequest,
    db: Session = Depends(get_db),
):
    """
    用户注册

    - **email**: 邮箱地址 (唯一)
    - **username**: 用户名 (唯一, 3-50字符, 只能包含字母数字下划线和横线)
    - **password**: 密码 (至少8字符, 须包含大小写字母和数字)
    - **name**: 显示名称 (可选)
    """
    # 检查邮箱是否已存在
    existing_user = db.query(User).filter(User.email == body.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="邮箱已被注册"
        )

    # 检查用户名是否已存在
    existing_username = db.query(User).filter(User.username == body.username).first()
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已被占用"
        )

    # 邀请码预校验放在创建用户之前:无效码在这里就报错,而不是等用户
    # 创建完再加入失败——后者会让用户重试注册时撞上"邮箱已被注册"
    invite_workspace = None
    if body.invite_code and body.invite_code.strip():
        invite_workspace = (
            db.query(Workspace)
            .filter(Workspace.invite_code == body.invite_code.strip().upper())
            .first()
        )
        if invite_workspace is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="邀请码无效",
            )

    # 创建新用户
    hashed_password = get_password_hash(body.password)
    new_user = User(
        email=body.email,
        username=body.username,
        name=body.name or body.username,
        hashed_password=hashed_password,
        provider="local",
        is_active=True,
        is_verified=False
    )

    db.add(new_user)
    db.commit()
    db.refresh(new_user)

    # 工作区初始化:凭邀请码加入(member),或自建个人空间(admin)。
    # 邀请码已在上文预校验,这里按 id 直接加入,不再有失败分支
    if invite_workspace is not None:
        new_user.workspace_id = invite_workspace.id
        new_user.role = workspace_service.ROLE_MEMBER
        db.commit()
        db.refresh(new_user)
    else:
        workspace_service.resolve_for_user(db, new_user)

    # 生成 access token + refresh token
    access_token = create_access_token(data={"sub": new_user.id})
    refresh_token = create_refresh_token(data={"sub": new_user.id})

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse(
            id=new_user.id,
            email=new_user.email,
            username=new_user.username,
            name=new_user.name,
            avatar=new_user.avatar,
            provider=new_user.provider,
            is_active=new_user.is_active,
            is_verified=new_user.is_verified,
            created_at=new_user.created_at.isoformat()
        )
    )


@router.post("/login", response_model=AuthResponse)
@limiter.limit("10/minute")
async def login(
    request: Request,
    body: LoginRequest,
    db: Session = Depends(get_db),
):
    """
    用户登录

    - **email**: 邮箱地址
    - **password**: 密码

    返回 access token、refresh token 和用户信息
    """
    # 查找用户
    user = db.query(User).filter(User.email == body.email).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误"
        )

    # 验证密码
    if not user.hashed_password or not verify_password(body.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误"
        )

    # 检查账号状态
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="账号已被禁用"
        )

    # 生成 access token + refresh token
    access_token = create_access_token(data={"sub": user.id})
    refresh_token = create_refresh_token(data={"sub": user.id})

    return AuthResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse(
            id=user.id,
            email=user.email,
            username=user.username,
            name=user.name,
            avatar=user.avatar,
            provider=user.provider,
            is_active=user.is_active,
            is_verified=user.is_verified,
            created_at=user.created_at.isoformat()
        )
    )


@router.get("/me", response_model=UserResponse)
async def get_current_user_info(
    current_user: User = Depends(get_current_user)
):
    """
    获取当前登录用户信息
    
    需要在 Header 中携带 Bearer Token
    """
    return UserResponse(
        id=current_user.id,
        email=current_user.email,
        username=current_user.username,
        name=current_user.name,
        avatar=current_user.avatar,
        provider=current_user.provider,
        is_active=current_user.is_active,
        is_verified=current_user.is_verified,
        created_at=current_user.created_at.isoformat()
    )


@router.post("/logout")
async def logout(
    current_user: User = Depends(get_current_user),
    credentials: HTTPAuthorizationCredentials = Depends(security),
):
    """
    用户登出

    将当前 access token 加入黑名单,使其立即失效。
    客户端也应同时删除本地存储的 token。
    """
    token = credentials.credentials
    blacklist_token(token)
    return {
        "success": True,
        "message": "登出成功"
    }


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    request: Request,
    body: RefreshRequest,
    db: Session = Depends(get_db),
):
    """
    刷新 access token

    使用 refresh token 换取新的 access token。
    """
    from auth import decode_refresh_token, is_token_blacklisted

    if is_token_blacklisted(body.refresh_token):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 已失效"
        )

    try:
        payload = decode_refresh_token(body.refresh_token)
    except Exception:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 refresh token"
        )

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="无效的 refresh token"
        )

    user = db.query(User).filter(User.id == user_id).first()
    if not user or not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户不存在或已被禁用"
        )

    access_token = create_access_token(data={"sub": user.id})
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=ACCESS_TOKEN_EXPIRE_MINUTES * 60
    )


# ========== 第三方登录端点 (UI 展示用) ==========

@router.get("/oauth/github")
async def github_oauth():
    """
    GitHub OAuth 登录 (展示用)
    
    实际项目中需要:
    1. 重定向到 GitHub OAuth 授权页面
    2. GitHub 回调后获取 code
    3. 用 code 换取 access_token
    4. 用 access_token 获取用户信息
    5. 创建或更新本地用户
    6. 返回 JWT token
    """
    return {
        "message": "GitHub OAuth 登录 (展示功能)",
        "note": "需要配置 GitHub OAuth App 的 Client ID 和 Client Secret",
        "redirect_url": "https://github.com/login/oauth/authorize"
    }


@router.get("/oauth/google")
async def google_oauth():
    """
    Google OAuth 登录 (展示用)
    
    实际项目中需要:
    1. 重定向到 Google OAuth 授权页面
    2. Google 回调后获取 code
    3. 用 code 换取 access_token
    4. 用 access_token 获取用户信息
    5. 创建或更新本地用户
    6. 返回 JWT token
    """
    return {
        "message": "Google OAuth 登录 (展示功能)",
        "note": "需要配置 Google OAuth Client ID 和 Client Secret",
        "redirect_url": "https://accounts.google.com/o/oauth2/v2/auth"
    }


@router.get("/oauth/callback")
async def oauth_callback(
    provider: str,
    code: Optional[str] = None,
    error: Optional[str] = None
):
    """
    OAuth 回调端点 (展示用)
    
    第三方平台会在用户授权后重定向到这个端点
    """
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OAuth 授权失败: {error}"
        )
    
    return {
        "message": f"{provider} OAuth 回调 (展示功能)",
        "code": code,
        "note": "实际项目中这里会用 code 换取用户信息并生成 JWT token"
    }
