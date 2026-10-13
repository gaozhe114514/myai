"""
run_eval.py —— 跑评估，输出检索命中率。

用法：
    python run_eval.py                     用默认设置跑
    python run_eval.py --top-k 5           检索 5 块（命中判定更宽松）
    python run_eval.py --no-rewrite        关掉查询改写，看它到底有多大用
    python run_eval.py --no-decompose      关掉查询分解

## 怎么算"答对"

对每道题：
    1. 用正常流程检索（拆问题 → 改写 → 检索）
    2. 看**标准答案来源**有没有出现在前 K 名里
       ├─ 在    → 命中 ✅（说明它找对了文件）
       └─ 不在  → 未命中 ❌
    3. 记下来

**注意：这个评估只考"检索"，不考"生成"。**
只要它找对了文件就算过，哪怕模型组织语言不好 ——
因为 RAG 里最容易坏的是检索，生成那一步通常不出问题。

## 输出的重点不是那个百分比

**是最后那几道没命中的题。** 命中的不用管；没命中的才是你要修的。

## 顺带一个用法

加上 --no-rewrite / --no-decompose 各跑一次，就能量出这两个优化各值多少：

    python run_eval.py                     → 比如 90%
    python run_eval.py --no-rewrite        → 比如 65%   （改写值 25 个点）
    python run_eval.py --no-decompose      → 比如 85%   （分解值 5 个点）
"""

import argparse
import json
import sys

import chat
import rag_common


DEFAULT_FILE = "eval_questions.json"


def main():
    parser = argparse.ArgumentParser(description="跑检索评估")
    parser.add_argument("--file", default=DEFAULT_FILE, help="题库文件")
    parser.add_argument("--top-k", type=int, default=rag_common.TOP_K,
                        help="检索几块算命中范围")
    parser.add_argument("--no-rewrite", action="store_true",
                        help="关掉查询改写")
    parser.add_argument("--no-decompose", action="store_true",
                        help="关掉查询分解")
    args = parser.parse_args()

    try:
        items = json.load(open(args.file, encoding="utf-8"))
    except FileNotFoundError:
        print(f"找不到题库 {args.file}，先生成：python make_eval.py")
        return 1

    index = rag_common.load_index()
    if index is None:
        print("还没有知识库，先运行：python ingest.py")
        return 1

    config = chat.load_config()
    model = config.get("model") or chat.DEFAULT_MODEL
    timeout = config.get("timeout") or chat.DEFAULT_TIMEOUT

    features = []
    if not args.no_rewrite:
        features.append("查询改写")
    if not args.no_decompose:
        features.append("查询分解")

    print("=" * 64)
    print(f"题库：{args.file}（{len(items)} 道题）")
    print(f"知识库：{index['count']} 块")
    print(f"命中范围：前 {args.top_k} 名")
    print(f"启用的优化：{'、'.join(features) if features else '（全关，这是基线）'}")
    print("=" * 64)
    print()

    hit_count = 0
    misses = []

    for i, item in enumerate(items, start=1):
        question = item["question"]
        expected = item["expected_source"]

        results, _ = rag_common.retrieve(
            question, model, timeout, args.top_k,
            rewrite=not args.no_rewrite,
            decompose=not args.no_decompose,
        )
        sources = [hit["source"] for hit in (results or [])]

        if expected in sources:
            rank = sources.index(expected) + 1
            hit_count += 1
            print(f"  ✅ [{i:2d}] 第 {rank} 名   {question[:44]}")
        else:
            misses.append((question, expected, sources))
            print(f"  ❌ [{i:2d}] 没找到    {question[:44]}")

    total = len(items)
    rate = hit_count / total * 100 if total else 0

    print()
    print("=" * 64)
    print(f"命中率：{hit_count}/{total} = {rate:.0f}%")
    print("=" * 64)

    if misses:
        print()
        print("=== 没命中的题（这几道才是要修的）===")
        for question, expected, got in misses:
            print(f"\n  问：{question}")
            print(f"  标准答案应该来自：{expected}")
            print(f"  实际检索到：")
            for source in got:
                print(f"    · {source}")
    else:
        print()
        print("全部命中 —— 没有要找的问题。可以把 --top-k 调小，或者加更难的题。")

    return 0


if __name__ == "__main__":
    sys.exit(main())
