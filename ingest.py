"""
ingest.py —— 建知识库（RAG 的第 1~3 步：读文档 → 切块 → 向量化 → 存起来）。

用法：
    python ingest.py                 读 docs/ 目录里的所有文档
    python ingest.py 别的目录          换一个目录
    python ingest.py 别的目录 600 100   顺便改块大小和重叠

建完会生成 rag_index.json，那是你的"向量库"。
文档改了、加新文档了，都要重新跑一次这个脚本。

注意：这个脚本**不调用聊天模型**，只调用 embedding 模型。
"""

import sys

import rag_common


def main():
    # 从命令行拿参数，没给就用默认值
    docs_dir = sys.argv[1] if len(sys.argv) > 1 else rag_common.DOCS_DIR
    size = int(sys.argv[2]) if len(sys.argv) > 2 else rag_common.CHUNK_SIZE
    overlap = int(sys.argv[3]) if len(sys.argv) > 3 else rag_common.CHUNK_OVERLAP

    print("=" * 50)
    print("第 1 步：读取文档")
    print("=" * 50)

    docs = rag_common.read_docs(docs_dir)
    if not docs:
        print(f"{docs_dir} 里没有 .txt 或 .md 文件。")
        return 1
    for filename, content in docs:
        print(f"  {filename}（{len(content)} 字）")
    print(f"共 {len(docs)} 篇。")

    print()
    print("=" * 50)
    print("第 2 步：切块")
    print("=" * 50)

    chunks = rag_common.build_chunks(docs, size, overlap)
    print(f"每块约 {size} 字，相邻重叠 {overlap} 字")
    print(f"切出 {len(chunks)} 块。")

    # 打印第一块的样子，让你直观看清"块"长什么样。
    # 新手最容易卡在"块到底是什么"这个抽象概念上，看一眼就明白了。
    print()
    print("第一块长这样：")
    print(f"  来自：{chunks[0]['source']}")
    print(f"  内容：{chunks[0]['text'][:120]}...")

    print()
    print("=" * 50)
    print("第 3 步：向量化（最慢的一步）")
    print("=" * 50)

    vectors = rag_common.embed([c["text"] for c in chunks])
    print(f"算完 {len(vectors)} 个向量，每个 {len(vectors[0])} 维。")

    # 顺手展示一下"向量"到底是什么：就是一小串数字而已
    print()
    print("第一个向量长这样（只显示前 8 个数）：")
    print(f"  {[round(v, 4) for v in vectors[0][:8]]}")
    print("  512 个这样的数字，就代表了一段话的'意思'。")

    print()
    print("=" * 50)
    print("存储")
    print("=" * 50)

    path = rag_common.save_index(chunks, vectors)
    print(f"已写入 {path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
