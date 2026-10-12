"""
ingest.py —— 建知识库：把 docs/ 里的文档，变成可以按"意思"检索的向量库。

用法：
    python ingest.py                    读 docs/ 目录
    python ingest.py 别的目录
    python ingest.py 别的目录 600 100    顺便改块大小和重叠

建完会生成 rag_index.json。文档改了、加新文档了，都要重新跑一次。

支持 txt / md / pdf / docx（能读哪些格式由 docreader.py 说了算）。

注意：这个脚本**不调用聊天模型**，只用 embedding 模型 ——
而 embedding 模型是跑在你自己电脑上的，所以这一步不花 API 的钱。
"""

import sys

import rag_common


def build_index(docs_dirs=None, size=None, overlap=None, on_step=None):
    """建库的完整流程：读文档 → 切块 → 向量化 → 存盘。

    返回一个统计字典（读了几篇、切了几块、存到哪了）。

    docs_dirs：要看哪些目录。默认是 rag_common.DOCS_DIRS（docs/ + mydocs/）。
               传一个字符串就只看那一个目录。

    参数 on_step 是个回调函数：每进入一个阶段就调一次，传一句进度文字。
    命令行用它打进度，网页界面用它更新进度条。
    不传就什么都不显示 —— 这样网页那边不会往终端里刷一堆东西。

    抽成函数而不是全写在 main() 里，就是为了让网页界面能直接调它 ——
    否则同样的流程要写两遍，改一处漏一处。
    """
    def step(text):
        if on_step:
            on_step(text)

    docs_dirs = docs_dirs or rag_common.DOCS_DIRS
    if isinstance(docs_dirs, str):
        docs_dirs = (docs_dirs,)

    size = size or rag_common.CHUNK_SIZE
    overlap = rag_common.CHUNK_OVERLAP if overlap is None else overlap

    # ---- 第 1 步：读文档 ----
    step(f"读取 {'、'.join(docs_dirs)} ……")
    docs, skipped = rag_common.read_all_docs(docs_dirs)
    if not docs:
        raise RuntimeError(f"这些目录里都没有可读的文档：{'、'.join(docs_dirs)}")
    step(f"读到 {len(docs)} 篇文档")

    # ---- 第 2 步：切块 ----
    step("切块……")
    chunks = rag_common.build_chunks(docs, size, overlap)
    step(f"切成 {len(chunks)} 块")

    # ---- 第 3 步：向量化（最慢的一步）----
    step("向量化（第一次运行要加载模型，会慢一点）……")
    vectors = rag_common.embed([chunk["text"] for chunk in chunks])
    step(f"算完 {len(vectors)} 个向量")

    # ---- 第 4 步：存盘 ----
    step("保存向量库……")
    path = rag_common.save_index(chunks, vectors)

    return {
        "docs": len(docs),
        "skipped": skipped,
        "chunks": len(chunks),
        "dim": len(vectors[0]) if vectors else 0,
        "path": path,
        "first_chunk": chunks[0] if chunks else None,
        "first_vector": vectors[0][:8] if vectors else [],
        "size": size,
        "overlap": overlap,
    }


def main():
    # 不给参数 = 读默认目录（docs/ + mydocs/）；给了一个就只看那一个
    docs_dirs = (sys.argv[1],) if len(sys.argv) > 1 else rag_common.DOCS_DIRS
    size = int(sys.argv[2]) if len(sys.argv) > 2 else rag_common.CHUNK_SIZE
    overlap = int(sys.argv[3]) if len(sys.argv) > 3 else rag_common.CHUNK_OVERLAP

    print("=" * 52)
    try:
        stats = build_index(docs_dirs, size, overlap,
                            on_step=lambda text: print(f"[{text}]"))
    except RuntimeError as error:
        print(error)
        return 1

    # 命令行这边多讲一点 —— 网页界面不需要这些，所以放在 main() 里，不在 build_index 里
    for name, reason in stats["skipped"]:
        print(f"  跳过 {name}：{reason}")

    print()
    print(f"每块约 {stats['size']} 字，相邻重叠 {stats['overlap']} 字")
    print(f"共 {stats['docs']} 篇 → {stats['chunks']} 块 → 每块 {stats['dim']} 维向量")

    print()
    print("第一块长这样：")
    print(f"  来自：{stats['first_chunk']['source']}")
    print(f"  内容：{stats['first_chunk']['text'][:100]}...")

    print()
    print("第一个向量长这样（只显示前 8 个数）：")
    print(f"  {[round(v, 4) for v in stats['first_vector']]}")
    print(f"  {stats['dim']} 个这样的数字，就代表了一段话的'意思'。")

    print()
    print(f"已写入 {stats['path']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
