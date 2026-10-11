"""
ask.py —— 知识库问答（RAG 的第 4~5 步：检索 → 生成）。

用法：
    python ask.py "报销要在多少天内提交？"
    python ask.py --top 5 "音箱保修多久？"        检索 5 块而不是默认的 3 块

跟 chat.py 最本质的区别：
    chat.py 是把**整段对话历史**发给模型；
    ask.py 是先从知识库里**只挑出几块最相关的**，再把这几块发过去。

这就是 RAG 的全部意义 —— 不是让模型读更多，是别让它读那么多。

前提：先跑过 python ingest.py 建好库（生成 rag_index.json）。
"""

import argparse
import os
import sys

import rag_common
# 直接复用 chat.py 里已经写好的东西，不重复造轮子：
#   ask           调模型（带流式输出）
#   load_config   读 config.json
#   default_model 决定用哪个模型（命令行 > config.json > 环境变量 > 默认）
#   check_config  检查 .env 里的密钥和地址
from chat import DEFAULT_TIMEOUT, ask, check_config, default_model, load_config

# 知识库问答专用的 system 提示词。
# 注意最后两条：要求注明出处、要求不知道就说不知道 ——
# 这两条是 RAG 能不能"可信"的关键。
RAG_SYSTEM_PROMPT = """你是一个严谨的知识库问答助手。

下面会给你若干条从资料库里检索出来的片段，请**只依据这些片段**回答我的问题。

要求：
1. 回答时注明依据来自哪个文件，用 [文件名] 的形式写在句子后面。
2. 如果片段里找不到答案，就直接说"资料库里没有相关内容"，不要自己编。
3. 回答尽量简洁。"""


def build_user_message(question, hits):
    """把检索到的几块 + 提问，拼成一条 user 消息。"""
    parts = ["以下是检索到的资料片段：", ""]

    for i, hit in enumerate(hits, 1):
        # 把相似度也写进去，方便排查"检索是不是找偏了"
        parts.append(f"[片段{i}] 来源：{hit['source']}（相似度 {hit['score']:.3f}）")
        parts.append(hit["text"])
        parts.append("")

    parts.append("我的问题：" + question)
    return "\n".join(parts)


def print_hits(hits):
    """在屏幕上展示"检索到了什么" —— 这一步最值得看。"""
    print(f"\n检索到 {len(hits)} 个相关片段：")
    for i, hit in enumerate(hits, 1):
        # 相似度是个很好的"体检指标"：
        # 都在 0.5 以下，说明库里可能压根没有相关内容，别指望模型答对。
        preview = hit["text"][:40].replace("\n", " ")
        print(f"  {i}. 相似度 {hit['score']:.3f}   {hit['source']}")
        print(f"     {preview}...")
    print("-" * 46)


def parse_args():
    parser = argparse.ArgumentParser(description="知识库问答（RAG）")
    parser.add_argument("question", nargs="*", help="你要问的问题")
    parser.add_argument("--top", type=int, default=rag_common.TOP_K,
                        help="检索几块最相关的资料")
    parser.add_argument("--model", help="用哪个模型（优先于 config.json）")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="关掉查询改写，直接用原问题检索（用来对比效果）")
    return parser.parse_args()


def main():
    args = parse_args()

    question = " ".join(args.question).strip()
    if not question:
        print('用法：python ask.py "你的问题"')
        return 1

    if not check_config():
        return 1

    # ---- 第 4 步：检索 ----
    index = rag_common.load_index()
    if index is None:
        print(f"还没有知识库（找不到 {rag_common.INDEX_FILE}）。")
        print("先运行：python ingest.py")
        return 1

    print(f"知识库：{index['count']} 块，来自 {rag_common.DOCS_DIR}/ 目录")

    config = load_config()
    model = default_model(args, config)
    timeout = config.get("timeout") or DEFAULT_TIMEOUT

    # ---- 第 4 步：检索 ----
    # 检索对"用词"非常敏感，所以先让模型把口语问题翻译成文档用语，再去找。
    # 这一整套流程（改写 + 检索）收在 rag_common.retrieve 里，
    # 这样 agent.py 那边也能用同一份实现。
    hits, search_query = rag_common.retrieve(
        question, model, timeout, args.top, rewrite=not args.no_rewrite)

    if search_query != question:
        print(f"\n改写后的检索词：{search_query}")
    print_hits(hits)

    # ---- 第 5 步：生成 ----
    # 注意：发给模型的是**原问题**，不是改写后的那句。
    # 改写只负责"找得准"，回答还是照着用户真正想问的来。
    messages = [
        {"role": "system", "content": RAG_SYSTEM_PROMPT},
        {"role": "user", "content": build_user_message(question, hits)},
    ]

    # 注意：这里发出去的内容里**只有这几块**，不是整个文档库。
    # 这就是 RAG 比"全塞进去"省的地方。
    sent = sum(len(m["content"]) for m in messages)

    # 回答的打印（流式）在 chat.ask 里面完成
    answer = ask(messages, model, timeout)

    if answer is None:
        return 1

    print(f"\n[本次发出 {sent} 字，只用了知识库的 {len(hits)}/{index['count']} 块]")
    return 0


if __name__ == "__main__":
    sys.exit(main())
