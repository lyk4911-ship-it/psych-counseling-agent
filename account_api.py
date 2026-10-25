"""Account and history HTTP API. Browser authentication uses revocable cookies."""

import hmac
import os
import re
import threading
import time
from collections import OrderedDict
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from account_store import token_hash


router = APIRouter()
AUTH_COOKIE = "psych_auth"
CSRF_COOKIE = "psych_csrf"
LOGIN_TTL = 7 * 24 * 60 * 60


def get_store(request: Request):
    return request.app.state.store


def current_user(request: Request, store=Depends(get_store)):
    user, csrf_hash = store.login_user(request.cookies.get(AUTH_COOKIE))
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        csrf = request.headers.get("X-CSRF-Token", "")
        if not csrf or len(csrf) > 256 or not hmac.compare_digest(token_hash(csrf), csrf_hash):
            raise HTTPException(403, "请求验证失败，请刷新页面后重试")
    return user


User = Annotated[dict, Depends(current_user)]


class LoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)

    @field_validator("username")
    @classmethod
    def username_format(cls, value):
        value = value.strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{3,32}", value):
            raise ValueError("用户名需为 3—32 位字母、数字或下划线")
        return value


class RegisterRequest(LoginRequest):
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(default="", max_length=60)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        return value.strip()


class ProfileRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=60)

    @field_validator("display_name")
    @classmethod
    def clean_name(cls, value):
        if not value.strip():
            raise ValueError("昵称不能为空")
        return value.strip()


class PasswordRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


class DeleteAccountRequest(BaseModel):
    password: str = Field(min_length=1, max_length=128)


class ConversationRequest(BaseModel):
    title: str = Field(default="新对话", min_length=1, max_length=80)

    @field_validator("title")
    @classmethod
    def clean_title(cls, value):
        return value.strip() or "新对话"


class MemoryRequest(BaseModel):
    content: str = Field(min_length=1, max_length=500)

    @field_validator("content")
    @classmethod
    def clean_content(cls, value):
        if not value.strip():
            raise ValueError("记忆内容不能为空")
        return value.strip()


class AuthLimiter:
    """Bounded per-process throttle for a small single-instance application.

    Do not trust X-Forwarded-For supplied by callers. Use an edge limiter when
    deploying multiple workers or accepting traffic through a reverse proxy.
    """
    def __init__(self):
        self.attempts = OrderedDict()
        self.lock = threading.Lock()

    def check(self, request):
        key = request.client.host if request.client else "unknown"
        now = time.monotonic()
        with self.lock:
            attempts = [t for t in self.attempts.pop(key, []) if now - t < 300]
            if len(attempts) >= 20:
                self.attempts[key] = attempts
                raise HTTPException(429, "尝试过于频繁，请五分钟后重试", headers={"Retry-After": "300"})
            attempts.append(now)
            self.attempts[key] = attempts
            while len(self.attempts) > 4096:
                self.attempts.popitem(last=False)


auth_limiter = AuthLimiter()


def set_login(response, store, user_id):
    token, csrf = store.create_login(user_id, LOGIN_TTL)
    secure = os.getenv("COOKIE_SECURE", "0") == "1"
    response.set_cookie(AUTH_COOKIE, token, max_age=LOGIN_TTL, httponly=True,
                        secure=secure, samesite="lax", path="/")
    response.set_cookie(CSRF_COOKIE, csrf, max_age=LOGIN_TTL, httponly=False,
                        secure=secure, samesite="lax", path="/")


def clear_login(response):
    response.delete_cookie(AUTH_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")


@router.post("/auth/register", status_code=201)
def register(body: RegisterRequest, request: Request, response: Response, store=Depends(get_store)):
    auth_limiter.check(request)
    user = store.register(body.username, body.password, body.display_name)
    set_login(response, store, user["id"])
    return {"user": user}


@router.post("/auth/login")
def login(body: LoginRequest, request: Request, response: Response, store=Depends(get_store)):
    auth_limiter.check(request)
    user = store.authenticate(body.username, body.password)
    set_login(response, store, user["id"])
    return {"user": user}


@router.get("/auth/me")
def me(user: User):
    return {"user": user}


@router.post("/auth/logout")
def logout(request: Request, response: Response, user: User, store=Depends(get_store)):
    store.logout(request.cookies[AUTH_COOKIE])
    clear_login(response)
    return {"ok": True}


@router.patch("/account")
def update_profile(body: ProfileRequest, user: User, store=Depends(get_store)):
    return {"user": store.update_profile(user["id"], body.display_name)}


@router.post("/account/password")
def change_password(body: PasswordRequest, response: Response, user: User, store=Depends(get_store)):
    store.change_password(user["id"], body.current_password, body.new_password)
    set_login(response, store, user["id"])
    return {"ok": True}


@router.delete("/account")
def delete_account(body: DeleteAccountRequest, response: Response, user: User, store=Depends(get_store)):
    store.delete_user(user["id"], body.password)
    clear_login(response)
    return {"ok": True}


@router.get("/account/export")
def export_account(user: User, store=Depends(get_store)):
    return store.export(user["id"])


@router.get("/conversations")
def conversations(user: User, limit: int = Query(100, ge=1, le=100), offset: int = Query(0, ge=0), store=Depends(get_store)):
    rows = store.list_conversations(user["id"], limit + 1, offset)
    return {"conversations": rows[:limit], "has_more": len(rows) > limit}


@router.post("/conversations", status_code=201)
def create_conversation(body: ConversationRequest, user: User, store=Depends(get_store)):
    return store.create_conversation(user["id"], body.title)


@router.get("/conversations/{conversation_id}")
def conversation(conversation_id: str, user: User, limit: int = Query(100, ge=1, le=200),
                 before: int | None = Query(None, ge=1), store=Depends(get_store)):
    return store.conversation(user["id"], conversation_id, limit, before)


@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, body: ConversationRequest, user: User, store=Depends(get_store)):
    store.rename_conversation(user["id"], conversation_id, body.title)
    return {"ok": True}


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, user: User, store=Depends(get_store)):
    store.delete_conversation(user["id"], conversation_id)
    return {"ok": True}


@router.delete("/history")
def clear_history(user: User, store=Depends(get_store)):
    store.clear_history(user["id"])
    return {"ok": True}


@router.get("/memories")
def memories(user: User, store=Depends(get_store)):
    return {"memories": store.memories(user["id"])}


@router.post("/memories", status_code=201)
def add_memory(body: MemoryRequest, user: User, store=Depends(get_store)):
    return {"id": store.save_memory(user["id"], body.content)}


@router.patch("/memories/{memory_id}")
def update_memory(memory_id: str, body: MemoryRequest, user: User, store=Depends(get_store)):
    store.save_memory(user["id"], body.content, memory_id)
    return {"ok": True}


@router.delete("/memories/{memory_id}")
def delete_memory(memory_id: str, user: User, store=Depends(get_store)):
    store.delete_memory(user["id"], memory_id)
    return {"ok": True}
