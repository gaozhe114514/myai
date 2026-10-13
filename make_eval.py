"""
make_eval.py —— 生成检索评估集（也就是"考卷"）。

用法：
    python make_eval.py            生成 20 道题
    python make_eval.py 30         生成 30 道

## 核心思路

**题目是从知识库的"块"里生成的，不是凭空出的。**

这样"标准答案"是**已知的** —— 就是那块来自哪个文件。
如果凭空出题，标准答案就得靠人判断，又慢又容易出错。

```
  挑一块资料  →  让模型出一道"只有这块能答"的题  →  标准答案 = 这块来自哪个文件
                   （这是已知的，不是模型编的）
```

## 为什么要覆盖不同文件

如果 20 道题全抽自同一个文件，那考卷就只考那一个文件，量不出整体水平。
所以这里的抽样是"按文件轮着来"，保证覆盖面。

## 结果存哪

`eval_questions.json` —— 是个普通 JSON，**你可以自己加题、改题、删题**。
加题很简单：照着现有格式加一条就行。
"""

import json
import random
import sys
from collections import defaultdict

import chat
import rag_common


OUTPUT_FILE = "eval_questions.json"

# 固定的随机种子 —— 同样的知识库，每次抽到的是同一批块，方便复现
RANDOM_SEED = 42

PROMPT = """下面是一份资料里的片段。

请你出一道**只有看过这段资料才能答对**的问题。

要求：
1. 问题要具体 —— 答案必须是这段资料里**明确写着的**某个事实
2. 不要出"这段资料讲了什么"这种笼统的问题
3. 只输出问题本身。不要加答案、不要加引号、不要解释

资料片段：
{text}"""


def pick_chunks(index, count):
    """挑出用来出题的块 —— 按文件轮流挑，保证覆盖面。

    做法：先把块按来源文件分组，然后一轮一轮地扫，
    每轮从每个文件里抽一块，直到凑够数量。
    这样不会出现"全抽自同一个文件"的情况。
    """
    by_source = defaultdict(list)
    for chunk in index["chunks"]:
        by_source[chunk["source"]].append(chunk)

    random.seed(RANDOM_SEED)
    for chunks in by_source.values():
        random.shuffle(chunks)

    picked = []
    round_number = 0
    while len(picked) < count and round_number < 10:
        for source in sorted(by_source):
            if len(picked) >= count:
                break
            chunks = by_source[source]
            if round_number < len(chunks):
                picked.append(chunks[round_number])
        round_number += 1

    return picked


def main():
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 20

    index = rag_common.load_index()
    if index is None:
        print(f"还没有知识库，先运行：python ingest.py")
        return 1

    config = chat.load_config()
    model = config.get("model") or chat.DEFAULT_MODEL
    timeout = config.get("timeout") or chat.DEFAULT_TIMEOUT

    chunks = pick_chunks(index, count)
    print(f"从 {index['count']} 块里挑了 {len(chunks)} 块来出题")
    print(f"覆盖 {len({c['source'] for c in chunks})} 个文件")
    print()

    items = []
    for i, chunk in enumerate(chunks, 1):
        # 注意：**不把文件名告诉模型** —— 否则它可能出成
        # "《xxx》里写了什么"，那就不是自然的问题了
        messages = [
            {"role": "system", "content": "你是一个出题助手，只输出问题本身。"},
            {"role": "user", "content": PROMPT.format(text=chunk["text"])},
        ]

        question = chat.ask(messages, model, timeout, show=False)
        if not question:
            print(f"  [{i:2d}] 出题失败，跳过")
            continue

        # 先把首尾的引号和标点都清掉，再统一补一个问号 ——
        # 不这样的话，模型自己带了问号就会变成"？？"
        question = question.strip().strip('"').strip()
        question = question.rstrip("。.！!？?") + "？"
        source = chunk["source"]

        items.append({
            "question": question,
            "expected_source": source,
        })
        print(f"  [{i:2d}] {question[:44]}")
        print(f"        标准答案来源：{source}")

    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)

    print()
    print(f"已写入 {OUTPUT_FILE}，共 {len(items)} 道题")
    print("你可以自己打开改题、加题 —— 那是个普通 JSON 文件。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
