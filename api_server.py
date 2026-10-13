"""
api_server.py —— HTTP 后端（FastAPI）。全栈化的第 1 阶段。

把"知识库问答"包成一个 HTTP 接口，让前端（网页）能调它。

## 启动

开发模式（改代码自动重载）：
    uvicorn api_server:app --reload --port 8000

生产模式（服务器上跑）：
    uvicorn api_server:app --host 0.0.0.0 --port 8000

## 测试

    curl -X POST http://127.0.0.1:8000/api/chat -H "Content-Type: application/json" -d "{\\"question\\": \\"报销要在多少天内提交？\\"}"

## 自动生成的接口文档

FastAPI 会白送一个文档页面，浏览器打开就能看、能试：
    http://127.0.0.1:8000/docs

## 为什么要有后端

    之前：网页（Streamlit）里前后端混在一起，界面是它生成的。
    现在：后端只负责"回答问题"，前端可以是任何东西
          （网页、手机 App、小程序、别人写的程序）。

    这一步之后，"界面"和"能力"就分开了。
"""

from fastapi import FastAPI
from pydantic import BaseModel, Field

import chat
import rag_common

app = FastAPI(
    title="MyAI API",
    description="个人知识库助手的后端接口",
    version="0.1.0",
)


# ============================ 请求 / 响应的数据结构 ============================
#
# 用 Pydantic 的类来定义，好处有三个：
#   1. 前端发来的 JSON 会**自动校验** —— 少字段、类型不对，都会给出明确报错
#   2. 自动生成接口文档（就是上面那个 /docs 页面）
#   3. 返回的数据也按这个结构走，不会漏字段、不会跑出意外的内容
#
# 这比"手动解析 JSON 再自己判断"可靠得多，而且代码更短。

class ChatRequest(BaseModel):
    question: str = Field(..., description="用户的问题", min_length=1)


class Source(BaseModel):
    source: str = Field(..., description="资料来自哪个文件")
    score: float = Field(..., description="相似度，越高越相关")
    text: str = Field(..., description="片段原文")


class ChatResponse(BaseModel):
    answer: str = Field(..., description="模型的回答")
    sources: list[Source] = Field(default_factory=list, description="回答依据的资料片段")


# ============================ 接口 ============================

@app.get("/api/health")
def health():
    """健康检查。

    部署时很有用：nginx、监控、或者你自己，都可以打这个接口确认服务活着。
    它不调模型、不查库，所以很快。
    """
    index = rag_common.load_index()
    return {
        "status": "ok",
        "chunks": index["count"] if index else 0,
    }


@app.post("/api/chat", response_model=ChatResponse)
def api_chat(request: ChatRequest):
    """知识库问答：提问 → 检索 → 生成 → 返回答案和出处。"""

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
