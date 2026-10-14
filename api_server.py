"""
api_server.py —— HTTP 后端（FastAPI）。

## 启动

开发模式（改代码自动重载）：
    uvicorn api_server:app --reload --port 8000

生产模式（服务器上跑）：
    uvicorn api_server:app --host 0.0.0.0 --port 8000

## 接口一览

    GET  /api/health                   健康检查（不用登录）
    POST /api/register                 注册，成功直接返回登录令牌
    POST /api/login                    登录，返回令牌
    GET  /api/me                       当前登录的是谁（需要令牌）
    POST /api/chat                     问答，并把这一问一答存进历史（需要令牌）
    GET  /api/sessions                 我的所有对话（需要令牌）
    GET  /api/sessions/{id}/messages   某段对话的全部消息（需要令牌）

## 自动生成的接口文档

    http://127.0.0.1:8000/docs

## 认证怎么用

登录成功后拿到 `token`，之后每次请求都带上这个请求头：

    Authorization: Bearer <你的token>

在 /docs 页面里，右上角有个 🔒 Authorize 按钮，填一次就自动带上了。
"""

import json
from contextlib import asynccontextmanager
from datetime import datetime
# ⚠️ 用 Optional[int] 而不是写成 int | None。
#    后者是 Python 3.10 才有的新写法，而我本机虚拟环境是 3.9，服务器是 3.10 ——
#    写了它，代码在服务器上能跑，在本机反而起不来。
#    两边都认的写法，才是能用的写法。
from typing import Optional

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

import chat
import rag_common
from auth import create_token, current_user, hash_password, verify_password
from db import ChatSession, Message, User, get_db, init_db


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
    version="0.3.0",
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
    session_id: Optional[int] = Field(
        None,
        description="接着哪段对话问。**不填就自动新建一段** —— 相当于点了「新对话」",
    )


class Source(BaseModel):
    source: str = Field(..., description="资料来自哪个文件")
    score: float = Field(..., description="相似度，越高越相关")
    text: str = Field(..., description="片段原文")


class ChatResponse(BaseModel):
    session_id: int = Field(..., description="这次问答存进了哪段对话。下次接着问就把它带回来")
    answer: str = Field(..., description="模型的回答")
    sources: list[Source] = Field(default_factory=list, description="回答依据的资料片段")


class SessionSummary(BaseModel):
    """对话列表里的一行（只有摘要，不含消息内容）。"""

    id: int
    title: str = Field(..., description="对话标题，取第一句话")
    updated_at: datetime
    message_count: int = Field(..., description="这段对话里一共多少条消息")


class MessageOut(BaseModel):
    """一条消息。"""

    id: int
    role: str = Field(..., description="user = 用户说的，assistant = 模型说的")
    content: str
    sources: list[Source] = Field(default_factory=list, description="这条回答依据的资料")
    created_at: datetime


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


# ============================ 对话历史 ============================
#
# 这一节干两件事：
#     写 —— 每问一次，把"用户问的"和"模型答的"连同出处存进 messages 表
#     读 —— 列出我的对话、打开某段对话看里面的消息
#
# ⚠️ 这里**每一处**查询都带着 user_id 过滤。
#    它不是"顺手加的"，而是唯一防止别人翻你聊天记录的东西。
#    漏掉一处，别人把网址里的 id 改一下就能看别人的对话。
#
# 为什么这些函数直接写在这儿，而不像 rag_common 那样单独开一个文件？
# 因为**现在只有这一个地方用得到**。rag_common 当初被抽出来，
# 是因为 ask.py / app.py / agent.py 三边都要用同一份代码。
# 等哪天真的有第二个地方要用，再抽 —— 别提前抽。

TITLE_MAX = 20


def _make_title(question: str) -> str:
    """用第一句话给对话起个名。

    真实产品（比如 ChatGPT）会再调一次模型来总结标题，
    那要多花一次钱、多等一两秒。现在先简单截断，够用。
    """
    title = " ".join(question.split())      # 把换行和多余空格压平
    if len(title) <= TITLE_MAX:
        return title
    return title[:TITLE_MAX] + "…"


def _get_session(db: Session, user: User,
                 session_id: Optional[int], question: str) -> ChatSession:
    """确定这次的一问一答要写进哪段对话。

        没给 session_id → 新建一段（就是用户点「新对话」的样子）
        给了 session_id → 查出来，而且**必须是他自己的**
    """
    if session_id is None:
        session = ChatSession(user_id=user.id, title=_make_title(question))
        db.add(session)
        db.commit()
        db.refresh(session)     # 提交后重新读一次，拿到数据库分配的 id
        return session

    session = (db.query(ChatSession)
                 .filter(ChatSession.id == session_id,
                         ChatSession.user_id == user.id)        # ⚠️ 这道墙
                 .first())

    if session is None:
        # 故意说"不存在"，而不是"这不是你的对话"。
        # 后者等于告诉对方"这个编号是真实存在的"，他就能一个个编号试过来。
        raise HTTPException(status_code=404, detail="对话不存在")
    return session


def _save_exchange(db: Session, session: ChatSession,
                   question: str, answer: str, sources: list[dict]):
    """把一问一答写进 messages 表。"""
    db.add(Message(session_id=session.id, role="user", content=question))
    db.add(Message(
        session_id=session.id,
        role="assistant",
        content=answer,
        # 出处存成 JSON 字符串。
        # ensure_ascii=False → 中文按原样存，而不是存成 你好 那种形式
        sources_json=json.dumps(sources, ensure_ascii=False),
    ))

    # 改一下更新时间，这样这段对话会排到列表最前面（就像微信的新消息置顶）
    session.updated_at = datetime.now()
    db.commit()


def _parse_sources(text: str) -> list[dict]:
    """把数据库里的 sources_json 还原成列表。存坏了也不崩。"""
    try:
        data = json.loads(text or "[]")
        return data if isinstance(data, list) else []
    except ValueError:
        return []


@app.get("/api/sessions", response_model=list[SessionSummary])
def api_sessions(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """列出我的所有对话，最近说过的排前面。"""
    sessions = (db.query(ChatSession)
                  .filter(ChatSession.user_id == user.id)       # ⚠️
                  .order_by(ChatSession.updated_at.desc())
                  .all())

    # 注意：len(s.messages) 会让 SQLAlchemy 给每段对话**再查一次库**（这叫 N+1 查询）。
    # 现在对话少，无所谓；等真到几百段，再改成一条聚合查询搞定。
    return [SessionSummary(id=s.id, title=s.title,
                           updated_at=s.updated_at,
                           message_count=len(s.messages))
            for s in sessions]


@app.get("/api/sessions/{session_id}/messages", response_model=list[MessageOut])
def api_session_messages(session_id: int,
                         user: User = Depends(current_user),
                         db: Session = Depends(get_db)):
    """打开某段对话，按时间顺序返回里面的消息。"""
    session = (db.query(ChatSession)
                 .filter(ChatSession.id == session_id,
                         ChatSession.user_id == user.id)        # ⚠️
                 .first())
    if session is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    # session.messages 这个"关系"在 db.py 里已经按 id 排好序了
    return [MessageOut(id=m.id, role=m.role, content=m.content,
                       sources=[Source(**s) for s in _parse_sources(m.sources_json)],
                       created_at=m.created_at)
            for m in session.messages]


# ============================ 知识库问答 ============================

@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest,
             user: User = Depends(current_user),        # ⚠️ 现在必须登录了
             db: Session = Depends(get_db)):
    """知识库问答：提问 → 检索 → 生成 → 存进历史 → 返回答案和出处。"""

    # 先决定这一次要写进哪段对话。
    # 特意放在最前面 —— 免得辛苦算完答案，才发现会话不是自己的。
    session = _get_session(db, user, request.session_id, request.question)

    config = chat.load_config()
    model = config.get("model") or chat.DEFAULT_MODEL
    timeout = config.get("timeout") or chat.DEFAULT_TIMEOUT

    # ---- 第 1 步：检索（拆问题 → 改写 → 找最相关的几块）----
    hits, _queries = rag_common.retrieve(request.question, model, timeout)

    if hits is None:
        answer = "知识库还没建好。先在服务器上跑一次 ingest.py。"
        _save_exchange(db, session, request.question, answer, [])
        return ChatResponse(session_id=session.id, answer=answer)

    # ---- 第 2 步：生成（只把检索到的几块拼进 prompt，不是整个文档库）----
    messages = rag_common.build_rag_messages(request.question, hits)
    message = chat.complete(messages, model, timeout)

    if not message:
        answer = "模型没有响应，请稍后重试。"
        _save_exchange(db, session, request.question, answer, [])
        return ChatResponse(session_id=session.id, answer=answer)

    sources = [Source(**hit) for hit in hits]
    answer = message.get("content") or "（模型没有返回文字）"

    # 存历史：用户问的 + 模型答的 + 出处，一起落库
    _save_exchange(db, session, request.question, answer,
                   [s.model_dump() for s in sources])

    return ChatResponse(session_id=session.id, answer=answer, sources=sources)
