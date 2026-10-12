"""
app.py —— 网页界面。

启动（**必须在项目目录下运行**）：
    streamlit run app.py

然后浏览器会自动打开一个页面。左边是设置，右边是聊天窗口。

它是怎么复用前面几步的：
    chat.py        —— 借它的 api_url / api_headers / 配置读取
    rag_common.py  —— 借它的 retrieve（改写 + 检索）
    ingest.py      —— 借它的 build_index（点按钮就能重建知识库）
没有一处是重新实现的。
"""

import json
import os
import time

import requests
import streamlit as st

import chat
import ingest
import rag_common

# ============================ 页面设置 ============================

st.set_page_config(page_title="MyAI 知识库助手", page_icon="🔎", layout="wide")


# ============================ 启动前的检查 ============================

if not (os.getenv("DEEPSEEK_BASE_URL") and os.getenv("DEEPSEEK_API_KEY")):
    st.error("读不到 API 密钥。")
    st.markdown(
        "请确认两件事：\n"
        "1. 项目目录下有 `.env` 文件（从 `.env.example` 复制，填好地址和密钥）\n"
        "2. **是从项目目录启动的** `streamlit run app.py`"
    )
    st.stop()


# ============================ 流式请求 ============================

def stream_answer(messages, model, timeout):
    """流式请求的"生成器"版本：每收到一小块就 yield 出来。

    这是给 st.write_stream 用的 —— 它要的就是一个"一次吐一点"的东西。

    命令行那边的 chat.ask() 是直接 print 到屏幕，
    网页这边的区别只是"把每一块写到页面上"，**底层请求完全一样**
    （都借用 chat.py 里那几个拼请求的函数）。
    """
    payload = {"model": model, "messages": messages, "stream": True}

    try:
        response = requests.post(chat.api_url(), headers=chat.api_headers(),
                                 json=payload, timeout=timeout, stream=True)
    except requests.exceptions.RequestException as error:
        yield f"**请求发不出去**：{error}"
        return

    if response.status_code != 200:
        yield f"**服务端返回 {response.status_code}**\n\n```\n{response.text[:400]}\n```"
        return

    for raw_line in response.iter_lines():
        if not raw_line:
            continue
        line = raw_line.decode("utf-8")
        if not line.startswith("data:"):
            continue

        data = line[len("data:"):].strip()
        if data == "[DONE]":
            break

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            continue

        choices = chunk.get("choices")
        if not choices:
            continue

        piece = (choices[0].get("delta") or {}).get("content")
        if piece:
            yield piece


# ============================ 知识库 ============================

def doc_stats():
    """看一眼 docs/ 目录里有什么。"""
    docs_dir = rag_common.DOCS_DIR
    if not os.path.isdir(docs_dir):
        return []
    return sorted(f for f in os.listdir(docs_dir)
                  if f.lower().endswith(rag_common.SUPPORTED_EXTENSIONS))


def rebuild_index(status_holder):
    """重建向量库，过程中把进度写到页面上。"""
    def on_step(text):
        status_holder.info(f"⏳ {text}")

    try:
        stats = ingest.build_index(on_step=on_step)
    except Exception as error:
        status_holder.error(f"建库失败：{error}")
        return None

    status_holder.success(
        f"✅ 建好了：{stats['docs']} 篇 → {stats['chunks']} 块"
        f"（每块 {stats['dim']} 维）"
    )
    for name, reason in stats["skipped"]:
        st.warning(f"跳过了 {name}：{reason}")
    return stats


# ============================ 侧边栏 ============================

config = chat.load_config()

with st.sidebar:
    st.title("🔎 MyAI 知识库助手")

    st.subheader("对话设置")
    model = st.text_input("模型", value=config.get("model") or chat.DEFAULT_MODEL)
    system_prompt = st.text_area(
        "人设（system prompt）",
        value=config.get("system") or chat.DEFAULT_SYSTEM,
        height=90,
    )

    st.divider()
    st.subheader("知识库")

    index = rag_common.load_index()
    files = doc_stats()

    if index:
        st.caption(f"已建库：**{index['count']} 块**，来自 **{len(files)} 篇**文档")
    else:
        st.caption("⚠️ 还没建库，点下面的按钮")

    use_kb = st.toggle("回答时先查资料", value=True,
                       help="关掉就是普通聊天，对比一下就知道 RAG 有什么用")
    top_k = st.slider("检索几块", 1, 10, rag_common.TOP_K)

    status_holder = st.empty()
    if st.button("🔄 重建知识库", use_container_width=True, type="primary"):
        rebuild_index(status_holder)

    with st.expander(f"docs/ 里的 {len(files)} 个文件"):
        for name in files:
            st.caption(f"· {name}")

    uploaded = st.file_uploader(
        "上传文档到 docs/",
        type=["txt", "md", "pdf", "docx"],
        accept_multiple_files=True,
    )
    # 注意：上传控件每次刷新都会重新触发，所以用一个按钮来"确认保存"，
    # 否则每点一次页面都会把文件重写一遍
    if uploaded and st.button("保存到 docs/"):
        for file in uploaded:
            target = os.path.join(rag_common.DOCS_DIR, file.name)
            with open(target, "wb") as out:
                out.write(file.getbuffer())
        status_holder.success(f"已保存 {len(uploaded)} 个文件，记得点「重建知识库」")
        st.rerun()

    st.divider()
    if st.button("🗑 清空对话", use_container_width=True):
        st.session_state.messages = []
        st.rerun()


# ============================ 对话状态 ============================

# Streamlit 每次交互都会把整个脚本重跑一遍，
# 所以"聊过什么"必须存在 session_state 里，否则一刷新就没了
if "messages" not in st.session_state:
    st.session_state.messages = []


# ============================ 主区域 ============================

st.title("问点什么")

if not st.session_state.messages:
    st.caption(
        "试试问：**报销要在多少天内提交？** · **供应商准入要求营业执照满几年？** · "
        "**年度预算是多少？**"
    )

# ---- 把聊过的内容显示出来 ----
for message in st.session_state.messages:
    with st.chat_message(message["role"]):
        st.markdown(message["content"])

        if message.get("sources"):
            with st.expander(f"📎 依据的 {len(message['sources'])} 段资料"):
                for hit in message["sources"]:
                    st.markdown(f"**{hit['source']}** · 相似度 `{hit['score']:.3f}`")
                    st.caption(hit["text"][:280] + ("…" if len(hit["text"]) > 280 else ""))
                    st.divider()


# ---- 接收新提问 ----
question = st.chat_input("输入你的问题…")

if question:
    st.session_state.messages.append({"role": "user", "content": question})
    with st.chat_message("user"):
        st.markdown(question)

    timeout = config.get("timeout") or chat.DEFAULT_TIMEOUT

    # ---- 检索 ----
    sources = []
    augmented = question

    if use_kb:
        with st.spinner("在知识库里找相关资料…"):
            hits, search_query = rag_common.retrieve(question, model, timeout, top_k)

        if hits:
            sources = hits
            # 把检索到的几段拼进提问里 —— 注意只发这几块，不是整个文档库
            parts = ["以下是检索到的资料片段，请优先依据它们回答，并注明来源文件：", ""]
            for i, hit in enumerate(hits, 1):
                parts.append(f"[片段{i}] 来源：{hit['source']}")
                parts.append(hit["text"])
                parts.append("")
            parts.append("我的问题：" + question)
            augmented = "\n".join(parts)

            if search_query != question:
                st.caption(f"🔍 改写后的检索词：**{search_query}**")
        else:
            st.caption("📭 知识库里没有找到相关资料，这次不参考资料回答")

    # ---- 组装发给模型的消息 ----
    history = [{"role": m["role"], "content": m["content"]}
               for m in st.session_state.messages[:-1]]
    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(history)
    messages.append({"role": "user", "content": augmented})

    # ---- 流式显示回答 ----
    with st.chat_message("assistant"):
        started = time.time()
        answer = st.write_stream(stream_answer(messages, model, timeout))
        elapsed = time.time() - started

        st.caption(f"用了 {elapsed:.1f} 秒"
                   + (f" · 检索了知识库的 {len(sources)} 块" if sources else ""))

        if sources:
            with st.expander(f"📎 依据的 {len(sources)} 段资料"):
                for hit in sources:
                    st.markdown(f"**{hit['source']}** · 相似度 `{hit['score']:.3f}`")
                    st.caption(hit["text"][:280] + ("…" if len(hit["text"]) > 280 else ""))
                    st.divider()

    st.session_state.messages.append({
        "role": "assistant",
        "content": answer,
        "sources": sources,
    })
