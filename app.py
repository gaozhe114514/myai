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

import hmac
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

# 上传的文档存进"我的资料"目录（DOCS_DIRS 的第二个）。
# 它已经在 .gitignore 里，所以你的资料不会跟着项目提交出去。
PRIVATE_DOCS_DIR = rag_common.DOCS_DIRS[1]


# ============================ 启动前的检查 ============================

if not (os.getenv("DEEPSEEK_BASE_URL") and os.getenv("DEEPSEEK_API_KEY")):
    st.error("读不到 API 密钥。")
    st.markdown(
        "请确认两件事：\n"
        "1. 项目目录下有 `.env` 文件（从 `.env.example` 复制，填好地址和密钥）\n"
        "2. **是从项目目录启动的** `streamlit run app.py`"
    )
    st.stop()


# ============================ 访问密码 ============================

def check_password():
    """只有输对密码才让用。返回 True = 放行。

    ⚠️ 为什么这一层必须有：

        网页一旦暴露到公网，**任何知道网址的人都能打开它** ——
        而这个网页默认是用**你的 API key** 去问模型的。
        不设防的话，别人随便点点，你的账单就能上千。

    设计上有个小心思：**没设密码 = 直接放行**。
        这样本机自己用的时候完全无感，只有真的要对公网开放时才去设。

    密码从环境变量 APP_PASSWORD 读（也就是写在 .env 里），
        因为 .env 已经在 .gitignore 里 —— 密码不会跟着项目传出去。
    """
    expected = os.getenv("APP_PASSWORD")

    # 没设密码 = 本机自用模式，不拦
    if not expected:
        return True

    # 已经输过了（存在 session 里，刷新页面还在，关掉浏览器就没了）
    if st.session_state.get("authed"):
        return True

    st.title("🔒 需要密码")
    st.caption("这是私人服务，请输入访问密码。")

    entered = st.text_input("访问密码", type="password")

    if st.button("进入"):
        # 用 compare_digest 而不是 == ：
        # 普通的 == 比较会在第一个不同的字符处就返回，
        # 逐字符的时间差理论上能被用来猜密码。这个函数是匀速比较的。
        if hmac.compare_digest(entered, expected):
            st.session_state.authed = True
            st.rerun()
        st.error("密码不对")

    return False


if not check_password():
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

def list_docs(docs_dir):
    """列出一个目录里有哪些可读的文档。目录不存在就返回空列表。"""
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
    example_files = list_docs(rag_common.DOCS_DIRS[0])
    private_files = list_docs(PRIVATE_DOCS_DIR)
    total_files = len(example_files) + len(private_files)

    if index:
        st.caption(f"已建库：**{index['count']} 块**，来自 **{total_files} 篇**文档")
    else:
        st.caption("⚠️ 还没建库，点下面的按钮")

    use_kb = st.toggle("回答时先查资料", value=True,
                       help="关掉就是普通聊天，对比一下就知道 RAG 有什么用")
    top_k = st.slider("检索几块", 1, 10, rag_common.TOP_K)

    status_holder = st.empty()

    # 上一次"保存文档"之后留下的记号：这次刷新就自动重建知识库。
    # 这样用户不用记得再点一次「重建知识库」—— 上传完就该能用。
    if st.session_state.pop("pending_rebuild", False):
        rebuild_index(status_holder)

    if st.button("🔄 重建知识库", use_container_width=True, type="primary"):
        rebuild_index(status_holder)

    # 两个目录分开列 —— 因为"能不能给别人看"不一样，
    # 界面上就该写清楚，别让用户搞混哪份会被提交
    with st.expander(f"📁 文档（共 {total_files} 个）"):
        st.caption(f"**{rag_common.DOCS_DIRS[0]}/**　示例文档，会跟着项目提交")
        for name in example_files:
            st.caption(f"　· {name}")

        st.caption(f"**{PRIVATE_DOCS_DIR}/**　我的资料，不会提交")
        if private_files:
            for name in private_files:
                st.caption(f"　· {name}")
        else:
            st.caption("　（还是空的 —— 从下面上传的文档会存到这里）")

    # 上传控件带一个"会变的 key"。
    # 为什么要变：Streamlit 的上传框会一直记着你选过哪些文件，
    # 保存完也不清空，文件就一直挂在框里看着难受。
    # 把 key 换掉 = 换一个新的控件，框就空了。
    if "uploader_id" not in st.session_state:
        st.session_state.uploader_id = 0

    uploaded = st.file_uploader(
        f"上传文档（存进 {PRIVATE_DOCS_DIR}/）",
        type=["txt", "md", "pdf", "docx"],
        accept_multiple_files=True,
        key=f"uploader_{st.session_state.uploader_id}",
        help="存进你自己的资料目录，不会跟着项目提交到 git",
    )

    # 用一个按钮来"确认保存" —— 否则控件每次刷新都会重新触发，反复重写文件
    if uploaded and st.button("保存并重建知识库", use_container_width=True, type="primary"):
        os.makedirs(PRIVATE_DOCS_DIR, exist_ok=True)   # 第一次用时目录可能还不存在

        for file in uploaded:
            target = os.path.join(PRIVATE_DOCS_DIR, file.name)
            with open(target, "wb") as out:
                out.write(file.getbuffer())

        st.session_state.uploader_id += 1        # 清空上传框
        st.session_state.pending_rebuild = True  # 下次刷新自动重建
        # 注意：这里必须 st.rerun()。因为 st.rerun() 会立刻掐断当次执行，
        # 所以在它前面写的任何提示都来不及显示 —— 提示交给重建完成后的那个函数去打
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
            hits, queries = rag_common.retrieve(question, model, timeout, top_k)

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

            # 一个问题被拆成了几条检索词时，显示出来 ——
            # 用户能看出"它把我的话理解对没有"
            if queries != [question]:
                st.caption(f"🔍 实际检索词（{len(queries)} 条）：" + " ｜ ".join(queries))
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
