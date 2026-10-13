"""
api_server.py —— HTTP 后端（FastAPI）。

## 启动

开发模式（改代码自动重载）：
    uvicorn api_server:app --reload --port 8000

生产模式（服务器上跑）：
    uvicorn api_server:app --host 0.0.0.0 --port 8000

## 接口一览

    GET  /api/health        健康检查（不用登录）
    POST /api/register      注册，成功直接返回登录令牌
    POST /api/login         登录，返回令牌
    GET  /api/me            当前登录的是谁（需要令牌）
    POST /api/chat          知识库问答（暂时不需要登录，下一步加上）

## 自动生成的接口文档

    http://127.0.0.1:8000/docs

## 认证怎么用

登录成功后拿到 `token`，之后每次请求都带上这个请求头：

    Authorization: Bearer <你的token>

在 /docs 页面里，右上角有个 🔒 Authorize 按钮，填一次就自动带上了。
"""

from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import chat
import rag_common
from auth import create_token, current_user, hash_password, verify_password
from db import User, get_db, init_db


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动/关闭时要做的事。

    这里只做一件：**建表**（已经存在的表不会动，不会删数据）。
    用 lifespan 而不是写在模块顶层，是为了让"建表"发生在**服务启动时**，
    而不是"任何人 import 这个文件时"。
    """
    init_db()
    yield


app = FastAPI(
    title="MyAI API",
    description="个人知识库助手的后端接口（含用户系统）",
    version="0.2.0",
    lifespan=lifespan,
)


# ============================ 请求 / 响应的数据结构 ============================
#
# 用 Pydantic 的类来定义，好处有三个：
#   1. 前端发来的 JSON 会**自动校验** —— 少字段、类型不对，都会给出明确报错
#   2. 自动生成接口文档（就是上面那个 /docs 页面）
#   3. 返回的数据也按这个结构走，不会漏字段、不会跑出意外的内容

class RegisterRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=50,
                          description="用户名，2-50 个字符")
    password: str = Field(..., min_length=6, max_length=72,
                          description="密码，至少 6 位")


class TokenResponse(BaseModel):
    token: str = Field(..., description="登录令牌。之后每次请求都要带上它")
    username: str


class MeResponse(BaseModel):
    id: int
    username: str
    created_at: datetime


class ChatRequest(BaseModel):
    question: str = Field(..., description="用户的问题", min_length=1)


class Source(BaseModel):
    source: str = Field(..., description="资料来自哪个文件")
    score: float = Field(..., description="相似度，越高越相关")
    text: str = Field(..., description="片段原文")


class ChatResponse(BaseModel):
    answer: str = Field(..., description="模型的回答")
    sources: list[Source] = Field(default_factory=list, description="回答依据的资料片段")


# ============================ 基础接口 ============================

@app.get("/api/health")
def health():
    """健康检查。

    部署时很有用：nginx、监控、或者你自己，都可以打这个接口确认服务活着。
    它不调模型、不查库，所以很快。
    """
    index = rag_common.load_index()
    return {"status": "ok", "chunks": index["count"] if index else 0}


# ============================ 用户认证 ============================

@app.post("/api/register", response_model=TokenResponse)
def api_register(request: RegisterRequest, db: Session = Depends(get_db)):
    """注册。成功就直接返回令牌 —— 不用再登录一次。"""
    exists = db.query(User).filter(User.username == request.username).first()
    if exists:
        raise HTTPException(status_code=400, detail="这个用户名已经被注册了")

    user = User(
        username=request.username,
        # ⚠️ 存的是哈希，不是密码本身。db.py 里的注释解释过为什么。
        password_hash=hash_password(request.password),
    )
    db.add(user)
    db.commit()
    db.refresh(user)        # 提交后重新读一下，拿到数据库分配的 id

    return TokenResponse(token=create_token(user.id), username=user.username)


@app.post("/api/login", response_model=TokenResponse)
def api_login(request: RegisterRequest, db: Session = Depends(get_db)):
    """登录。"""
    user = db.query(User).filter(User.username == request.username).first()

    # ⚠️ 注意：这里"用户名不存在"和"密码错"返回的是**同一句话**。
    #
    # 这是故意的。如果分开提示，别人就能靠"用户名不存在"这句话，
    # 把系统里到底有哪些用户名一个一个试出来 —— 这叫"用户名枚举"，
    # 是攻击的第一步（试出有效用户名，再去撞密码）。
    if user is None or not verify_password(request.password, user.password_hash):
        raise HTTPException(status_code=401, detail="用户名或密码不对")

    return TokenResponse(token=create_token(user.id), username=user.username)


@app.get("/api/me", response_model=MeResponse)
def api_me(user: User = Depends(current_user)):
    """看看当前登录的是谁。需要带上令牌。

    前端的用法：打开页面时调一下 —— 令牌还有效就继续用，401 了就让用户重新登录。
    """
    return MeResponse(id=user.id, username=user.username, created_at=user.created_at)


# ============================ 知识库问答 ============================

@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest):
    """知识库问答：提问 → 检索 → 生成 → 返回答案和出处。

    暂时不用登录（下一步会和对话历史一起加上）。
    """

    config = chat.load_config()
    model = config.get("model") or chat.DEFAULT_MODEL
    timeout = config.get("timeout") or chat.DEFAULT_TIMEOUT

    # ---- 第 1 步：检索（拆问题 → 改写 → 找最相关的几块）----
    hits, _queries = rag_common.retrieve(request.question, model, timeout)

    if hits is None:
        return ChatResponse(
            answer="知识库还没建好。先在服务器上跑一次 ingest.py。",
            sources=[],
        )

    # ---- 第 2 步：生成（只把检索到的几块拼进 prompt，不是整个文档库）----
    messages = rag_common.build_rag_messages(request.question, hits)
    message = chat.complete(messages, model, timeout)

    if not message:
        return ChatResponse(answer="模型没有响应，请稍后重试。", sources=[])

    return ChatResponse(
        answer=message.get("content") or "（模型没有返回文字）",
        sources=[Source(**hit) for hit in hits],
    )
