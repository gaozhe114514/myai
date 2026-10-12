"""
rag_common.py —— RAG 知识库功能里，建库和提问都要用到的工具。

为什么单独拆一个文件？因为 ingest.py（建库）和 ask.py（提问）
两边都要"切块""算向量""算相似度"。写两遍的话，以后改一处就会漏掉另一处。

RAG 一共五步，本文件负责前四步：

    1. 切块     chunk_text()
    2. 向量化   embed()
    3. 存储     save_index() / load_index()
    4. 检索     search()
    5. 生成     （在 ask.py 里：把检索结果拼进 prompt，交给 chat.ask 去问模型）
"""

import json
import os

# 复用已经写好的东西，不重复造：
#   chat_ask       调模型（带流式输出，可静默）
#   read_document  从 txt/md/pdf/docx 里抠出纯文本
from chat import ask as chat_ask
from docreader import SUPPORTED_EXTENSIONS, read_document

# ============================ 配置 ============================

DOCS_DIR = "docs"                 # 文档放这个目录，ingest.py 会全部读进来
INDEX_FILE = "rag_index.json"     # 建好的向量库存这儿

CHUNK_SIZE = 400        # 每块大约多少字
CHUNK_OVERLAP = 80      # 相邻两块重叠多少字（下面会解释为什么要重叠）
TOP_K = 3               # 每次提问，取最相关的几块给模型

# 用哪个 embedding 模型。
# bge-small-zh-v1.5 是中文检索里很常用的一个小模型：效果好、体积小（约 95MB）。
EMBED_MODEL_NAME = "BAAI/bge-small-zh-v1.5"

# BGE 系列的中文模型，官方建议**给问题**加这个前缀（文档不加）。
# 原因是它训练时就用了这个套路：问题带前缀、文档不带。
# 不加的话，问题向量和文档向量不在同一个"频道"，检索会明显变差。
QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："

# 查询改写用的提示词。
# 为什么需要这一步：embedding 模型对"换了个说法"很敏感 ——
# 实测过，问"我出差自己先垫的钱什么时候能要回来"，正确答案在库里排第 6 名；
# 改成"报销要在多少天内提交"就排第 1 名。这一步就是让模型自动做这个"翻译"。
REWRITE_PROMPT = """用户会用口语提问，但知识库里的文档用的是正式书面语。

请把用户的问题改写成适合在文档库里检索的**简短查询词**。

要求：
1. 换成文档里可能出现的那种正式说法（例如"垫的钱"→"报销费用"）
2. 只输出改写后的查询本身，不要解释、不要加引号、不要换行
3. 保持原意，不要漏掉关键信息

用户的问题：{question}"""


# ============================ 第 1 步：读文档 + 切块 ============================

def read_docs(docs_dir=DOCS_DIR):
    """把目录里所有支持的文档读进来。

    返回 (docs, skipped) 两个列表：
        docs    —— [(文件名, 正文), ...]，按文件名排序，保证每次建库顺序一样
        skipped —— [(文件名, 失败原因), ...]

    为什么分成两个列表返回，而不是在里面直接 print？
    因为调用方有两种：命令行要打在屏幕上，网页界面要显示在侧边栏里。
    让它们自己决定怎么展示，比我们在这里写死要灵活。
    """
    if not os.path.isdir(docs_dir):
        raise SystemExit(f"找不到文档目录：{docs_dir}\n先建一个，把文档放进去。")

    docs = []
    skipped = []
    for name in sorted(os.listdir(docs_dir)):
        # 支持哪些格式由 docreader 说了算 —— 这里不写死，加新格式时只改一处
        if not name.lower().endswith(SUPPORTED_EXTENSIONS):
            continue

        filename, content = read_document(os.path.join(docs_dir, name))
        if filename is None:
            skipped.append((name, content))
            continue
        docs.append((filename, content))

    return docs, skipped


def chunk_text(text, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """把一整篇文档切成若干小块。

    为什么要切？
      整篇丢进去太占地方，而且一篇文档里往往只有一两段跟问题有关。
      切成小块之后，检索就能精确定位到"那一段"，而不是把整篇都塞给模型。

    为什么要重叠？
      一句关键的话很可能正好横跨切点，被切成两半 ——
      那样两边都读不通，检索也匹配不上。
      让相邻两块重叠一截，这句话至少能在某一块里保持完整。
    """
    if overlap >= size:
        raise ValueError("overlap 必须小于 size，否则每次只前进 0 个字，会死循环")

    text = text.strip()
    if not text:
        return []

    chunks = []
    start = 0
    while start < len(text):
        end = start + size
        chunks.append(text[start:end])
        if end >= len(text):
            break
        # 下一块从 end 往前退 overlap 个字开始 —— 这一段就是"重叠区"
        start = end - overlap

    return chunks


def build_chunks(docs, size=CHUNK_SIZE, overlap=CHUNK_OVERLAP):
    """把所有文档切成块，并记住每一块来自哪个文件。

    返回 [{"source": 文件名, "text": 块内容}, ...]
    """
    all_chunks = []
    for filename, content in docs:
        pieces = chunk_text(content, size, overlap)
        for piece in pieces:
            # source 一定要记下来 —— 最后要求模型"注明出处"，靠的就是它
            all_chunks.append({"source": filename, "text": piece})
    return all_chunks


# ============================ 第 2 步：向量化 ============================

_model = None   # 模型只加载一次，暂存在这里（每次重加载要好几秒）


def _prefer_local_model():
    """模型已经下到本地了，就切到"离线模式"。

    不切的话会出大问题：HuggingFace 的库**每次启动都要先联网查一次**
    "这个模型有没有更新"。网络一不通（比如 VPN 断了），它会重试 5 次、
    每次等待时间翻倍 —— 我们实测过，本该 4 秒的启动硬是卡了 5 分钟以上。

    模型都在本地了，那次联网检查根本没必要。
    """
    cache_root = os.environ.get("HF_HOME") or os.path.join(
        os.path.expanduser("~"), ".cache", "huggingface")
    model_dir = os.path.join(
        cache_root, "hub", "models--" + EMBED_MODEL_NAME.replace("/", "--"))

    if os.path.isdir(model_dir):
        # 用 setdefault：你自己在外面设了 HF_HUB_OFFLINE 的话，以你的为准
        os.environ.setdefault("HF_HUB_OFFLINE", "1")


def get_model():
    """加载 embedding 模型。第一次调用慢（要下模型），之后直接用缓存的。"""
    global _model

    if _model is None:
        # 必须在 import sentence_transformers **之前**设好，
        # 因为这个环境变量是在它加载的那一刻被读进去的
        _prefer_local_model()

        # 把 import 写在函数里面：这个库加载很慢，
        # 万一你只是想看看报错信息，不用干等它。
        from sentence_transformers import SentenceTransformer

        print(f"正在加载 embedding 模型：{EMBED_MODEL_NAME}")

        if os.environ.get("HF_HUB_OFFLINE") == "1":
            print("（本地已有模型，离线加载，不联网）")
        else:
            print("（第一次运行需要从网上下模型，可能要几分钟；之后就快了）")

        _model = SentenceTransformer(EMBED_MODEL_NAME)
        print("模型加载完毕。")

    return _model


def embed(texts, is_query=False):
    """把一批文本变成向量 —— 这就是"向量化"。

    模型读一句话，吐出一串固定长度的数字（bge-small-zh 是 512 个）。
    关键在于：**意思相近的句子，算出来的数字串也相近**。
    有了这个性质，"按意思找"才有可能 —— 不然就只能靠关键词硬匹配。

    参数 is_query：这批文本是"问题"还是"文档"？两者的处理方式不一样（见 QUERY_PREFIX）。
    """
    model = get_model()

    if is_query:
        texts = [QUERY_PREFIX + t for t in texts]

    # normalize_embeddings=True 会把每个向量除成"长度正好为 1"。
    # 好处在下面 cosine() 那儿 —— 归一化之后，余弦相似度就等于点积，算起来极其简单。
    vectors = model.encode(texts, normalize_embeddings=True)
    return vectors.tolist()


def cosine(a, b):
    """两个向量的余弦相似度：衡量它们"指向同一个方向"的程度。

    正常公式是 (a·b) / (|a|·|b|) —— 点积除以两个向量各自的长度。

    但上面已经归一化过了（每个向量长度都是 1），所以分母恒等于 1，
    整个公式就塌缩成"点积"：对应位置相乘，再全部加起来。

    结果范围 -1 到 1：
        接近 1  → 意思很接近
        接近 0  → 基本没关系
       负数     → 意思相反（中文检索里很少见）
    """
    return sum(x * y for x, y in zip(a, b))


# ============================ 第 3 步：存储 ============================

def save_index(chunks, vectors, path=INDEX_FILE):
    """把切好的块和对应的向量存成一个 json 文件。

    核心约定：chunks[i] 和 vectors[i] 是**一一对应**的 ——
    第 i 块文本，对应的就是第 i 个向量。检索时靠这个下标找回去。
    """
    data = {
        "model": EMBED_MODEL_NAME,
        "chunk_size": CHUNK_SIZE,
        "chunk_overlap": CHUNK_OVERLAP,
        "dim": len(vectors[0]) if vectors else 0,
        "count": len(chunks),
        "chunks": chunks,
        "vectors": vectors,
    }

    with open(path, "w", encoding="utf-8") as f:
        # 这里不加 indent=2：向量有几百维，加缩进文件会大好几倍，也没人看得懂
        json.dump(data, f, ensure_ascii=False)

    return path


def load_index(path=INDEX_FILE):
    """把向量库读回来。不存在就返回 None（让调用方去提示用户先建库）。"""
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# ============================ 第 4 步：检索 ============================

def search(question, index, top_k=TOP_K):
    """在当前问题下，从库里找出"意思最接近"的 top_k 块。

    做法就三步：
      1. 把问题也变成一个向量（注意 is_query=True）
      2. 拿它和库里每一个向量算相似度
      3. 按相似度从高到低排，取前 top_k 个

    这就是"检索"的全部。整个 RAG 最核心的一步，也就这么点事。
    """
    query_vector = embed([question], is_query=True)[0]

    scored = []
    for i, vector in enumerate(index["vectors"]):
        scored.append((cosine(query_vector, vector), i))

    # 按相似度从高到低排。元组比较会先比第一个元素（相似度），正好是我们要的
    scored.sort(reverse=True)

    results = []
    for score, i in scored[:top_k]:
        chunk = index["chunks"][i]
        results.append({
            "score": score,
            "source": chunk["source"],
            "text": chunk["text"],
        })
    return results


# ============================ 检索全流程 ============================

def rewrite_query(question, model, timeout):
    """用模型把口语问题"翻译"成文档用语，再去检索。

    这一步用的是 chat_ask(show=False) —— 它属于幕后工作，不该在屏幕上刷出来。
    """
    messages = [
        {"role": "system", "content": "你是一个检索助手，只输出改写后的查询词。"},
        {"role": "user", "content": REWRITE_PROMPT.format(question=question)},
    ]

    rewritten = chat_ask(messages, model, timeout, show=False)

    # 改写失败不能让整个流程挂掉 —— 退回用原问题检索，大不了效果差一点
    if not rewritten:
        return question

    # 有些模型爱加引号或者句号，顺手清掉
    cleaned = rewritten.strip().strip('"').strip("'").strip("。").strip()
    return cleaned or question


def retrieve(question, model, timeout, top_k=TOP_K, rewrite=True):
    """一次完整的检索：读库 → （可选）改写查询 → 找出最相关的几块。

    返回 (hits, 实际用来检索的词)。
    库还没建时返回 (None, 改写后的词) —— 调用方自己决定怎么提示。

    这个函数是 RAG 的"入口"。ask.py 和 agent.py 都调它，
    所以检索逻辑只有一份，改一处两边都生效。
    """
    index = load_index()
    if index is None:
        return None, question

    search_query = rewrite_query(question, model, timeout) if rewrite else question
    hits = search(search_query, index, top_k)
    return hits, search_query


def format_hits(hits):
    """把检索结果排成一段文本 —— 可以直接塞进 prompt 给模型看。"""
    parts = []
    for hit in hits:
        parts.append(f"[{hit['source']}]（相似度 {hit['score']:.3f}）\n{hit['text']}")
    return "\n\n".join(parts)
