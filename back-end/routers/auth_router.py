"""
认证相关路由
包括: 注册、登录、登出、获取当前用户信息等
"""
from datetime import timedelta
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy.orm import Session

from auth import (
    create_access_token,
    get_current_user,
    get_password_hash,
    verify_password,
    ACCESS_TOKEN_EXPIRE_MINUTES
)
from config import settings
from database import get_db
from models import User

router = APIRouter(prefix="/auth", tags=["认证"])


# ========== Pydantic 模型 ==========

class RegisterRequest(BaseModel):
    """注册请求"""
    email: EmailStr
    username: str = Field(..., min_length=3, max_length=50, pattern="^[a-zA-Z0-9_-]+$")
    password: str = Field(..., min_length=6, max_length=100)
    name: Optional[str] = Field(None, max_length=100)


class LoginRequest(BaseModel):
    """登录请求"""
    email: str
    password: str


class TokenResponse(BaseModel):
    """Token 响应"""
    access_token: str
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
    token_type: str = "bearer"
    expires_in: int
    user: UserResponse


# ========== API 端点 ==========

@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
async def register(
    request: RegisterRequest,
    db: Session = Depends(get_db)
):
    """
    用户注册
    
    - **email**: 邮箱地址 (唯一)
    - **username**: 用户名 (唯一, 3-50字符, 只能包含字母数字下划线和横线)
    - **password**: 密码 (至少6字符)
    - **name**: 显示名称 (可选)
    """
    # 检查邮箱是否已存在
    existing_user = db.query(User).filter(User.email == request.email).first()
    if existing_user:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="邮箱已被注册"
        )
    
    # 检查用户名是否已存在
    existing_username = db.query(User).filter(User.username == request.username).first()
    if existing_username:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="用户名已被占用"
        )
    
    # 创建新用户
    hashed_password = get_password_hash(request.password)
    new_user = User(
        email=request.email,
        username=request.username,
        name=request.name or request.username,
        hashed_password=hashed_password,
        provider="local",
        is_active=True,
        is_verified=False
    )
    
    db.add(new_user)
    db.commit()
    db.refresh(new_user)
    
    # 生成 access token
    access_token = create_access_token(
        data={"sub": new_user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    # 返回认证信息
    return AuthResponse(
        access_token=access_token,
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
async def login(
    request: LoginRequest,
    db: Session = Depends(get_db)
):
    """
    用户登录
    
    - **email**: 邮箱地址
    - **password**: 密码
    
    返回 access token 和用户信息
    """
    # 查找用户
    user = db.query(User).filter(User.email == request.email).first()
    
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="邮箱或密码错误"
        )
    
    # 验证密码
    if not user.hashed_password or not verify_password(request.password, user.hashed_password):
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
    
    # 生成 access token
    access_token = create_access_token(
        data={"sub": user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
    # 返回认证信息
    return AuthResponse(
        access_token=access_token,
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
    current_user: User = Depends(get_current_user)
):
    """
    用户登出
    
    JWT 是无状态的,登出主要由客户端删除 token 实现
    这个端点主要用于服务端记录日志或其他业务逻辑
    """
    return {
        "success": True,
        "message": "登出成功"
    }


@router.post("/refresh", response_model=TokenResponse)
async def refresh_token(
    current_user: User = Depends(get_current_user)
):
    """
    刷新 access token
    
    使用当前有效的 token 换取新的 token
    """
    # 生成新的 access token
    access_token = create_access_token(
        data={"sub": current_user.id},
        expires_delta=timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    
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
