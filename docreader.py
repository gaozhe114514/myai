"""
docreader.py —— 从各种格式的文件里，把纯文本抠出来。

RAG 的第一步是"读文档"。但真实的文档格式五花八门：
记事本存的 txt、写笔记用的 md、别人发来的 pdf、公司文件是 docx……
而不管前面是什么格式，**后面切块、算向量要的都只是一段纯文本**。

所以这个模块只干一件事：
    给我一个文件，我想办法把里面的字抠出来。

单独拆成一个文件，是因为"解析各种文档格式"和"跟模型对话"是两件不相干的事 ——
以后要加 pptx、html、epub，都在这里加，不用碰别的地方。
"""

import os

# 纯文本类：直接按文本读就行
TEXT_EXTENSIONS = (".txt", ".md")

# 需要额外库才能解析的
PDF_EXTENSIONS = (".pdf",)
DOCX_EXTENSIONS = (".docx",)

# 对外暴露的"我支持哪些格式"
SUPPORTED_EXTENSIONS = TEXT_EXTENSIONS + PDF_EXTENSIONS + DOCX_EXTENSIONS


def read_document(path):
    """读一个文件，把里面的文字抠出来。

    成功 → (文件名, 正文)
    失败 → (None, 一句人能看懂的错误说明)

    统一返回这个格式，是因为调用方（建库脚本、/file 命令）不该关心
    "这个文件到底是 pdf 还是 txt" —— 它们只要文字。
    """
    extension = os.path.splitext(path)[1].lower()

    if extension not in SUPPORTED_EXTENSIONS:
        supported = "、".join(SUPPORTED_EXTENSIONS)
        return None, f"不支持 {extension or '这种没有后缀名的文件'}，目前支持：{supported}"

    if os.path.isdir(path):
        return None, "这是个目录，不是文件"
    if not os.path.exists(path):
        return None, "找不到这个文件"

    name = os.path.basename(path)

    # 按格式分派到对应的解析函数
    if extension in PDF_EXTENSIONS:
        return _read_pdf(path, name)
    if extension in DOCX_EXTENSIONS:
        return _read_docx(path, name)
    return _read_text(path, name)


def _read_text(path, name):
    """读纯文本文件。先按 UTF-8 试，不行再按 GBK 试。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return name, f.read()
    except UnicodeDecodeError:
        # 中文 Windows 上，记事本存的老文件常常是 GBK 编码。
        # 这就是 requirements.txt 那个坑的同一个根：编码对不上而已。
        try:
            with open(path, "r", encoding="gbk") as f:
                return name, f.read()
        except (OSError, UnicodeDecodeError) as error:
            return None, f"读不了（{error}）"
    except OSError as error:
        return None, f"读不了（{error}）"


def _read_pdf(path, name):
    """从 PDF 里抠文字。

    ⚠️ 只对"文字型 PDF"有效 —— 也就是能用鼠标选中文字的 PDF。

    扫描件（本质是一堆图片）抠出来是空的，那需要 OCR，是另一回事。
    遇到这种情况会明确告诉你，而不是默默返回一个空文档。
    """
    try:
        from pypdf import PdfReader
    except ImportError:
        return None, "读 PDF 需要 pypdf，先运行：pip install pypdf"

    try:
        reader = PdfReader(path)
        pages = []
        for page in reader.pages:
            text = page.extract_text() or ""
            if text.strip():
                pages.append(text)
    except Exception as error:
        # 这里故意接得很宽：PDF 是个很乱的格式，pypdf 能抛出各种奇怪的异常。
        # 与其让一个坏文件把整个建库流程搞崩，不如跳过它、继续处理剩下的。
        return None, f"这个 PDF 解析失败（{type(error).__name__}: {error}）"

    if not pages:
        return None, "这个 PDF 里抠不出文字。如果是扫描件（图片），需要 OCR 才能读"

    return name, "\n\n".join(pages)


def _read_docx(path, name):
    """从 Word 文件里抠文字。

    只支持 .docx，不支持老的 .doc —— .doc 是完全不同的二进制格式，
    要读它得用别的库。
    """
    try:
        import docx
    except ImportError:
        return None, "读 Word 需要 python-docx，先运行：pip install python-docx"

    try:
        document = docx.Document(path)

        blocks = [p.text for p in document.paragraphs if p.text.strip()]

        # 表格也要抠 —— 很多规章制度的关键数字就藏在表格里，
        # 只读段落的话会漏掉一整块信息。
        for table in document.tables:
            for row in table.rows:
                cells = [cell.text.strip() for cell in row.cells if cell.text.strip()]
                if cells:
                    # 用竖线拼成一行，保留"这些字属于同一行"的关系
                    blocks.append(" | ".join(cells))
    except Exception as error:
        return None, f"这个 Word 文件解析失败（{type(error).__name__}: {error}）"

    if not blocks:
        return None, "这个 Word 文件里没有文字"

    return name, "\n".join(blocks)
