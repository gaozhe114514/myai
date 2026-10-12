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

import io
import os

# Pillow：图片的打开、缩放、切片、转码都靠它
from PIL import Image

# 图片识别要发请求，零件在 api.py —— 它不依赖本项目任何东西，所以能安全 import
from api import ask_about_image

# 纯文本类：直接按文本读就行
# CSV 也归在这里 —— 它本来就是逗号分隔的纯文本，只是后缀不同
TEXT_EXTENSIONS = (".txt", ".md", ".csv")

# 需要额外库才能解析的
PDF_EXTENSIONS = (".pdf",)
DOCX_EXTENSIONS = (".docx",)
PPTX_EXTENSIONS = (".pptx",)
XLSX_EXTENSIONS = (".xlsx", ".xls")   # 新旧两种都收（内容才是准的，见 _read_excel）

# 图片类：本地抠不出文字，得让模型"看图"（见 _read_image）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# 对外暴露的"我支持哪些格式"
SUPPORTED_EXTENSIONS = (TEXT_EXTENSIONS + PDF_EXTENSIONS + DOCX_EXTENSIONS
                        + PPTX_EXTENSIONS + XLSX_EXTENSIONS + IMAGE_EXTENSIONS)


# ============================ 图片识别（OCR）的配置 ============================

# 图片识别要调模型，所以得知道"用哪个模型、等多久"。
# 由调用方通过 configure_vision() 设置（一般是读 config.json 之后设进来）；
# 不设就用下面这个兜底值。
_vision_model = None
_vision_timeout = 60


def configure_vision(model=None, timeout=None):
    """告诉 docreader：做图片识别时用哪个模型、等多久。

    为什么不在这里直接读 config.json：
        docreader 的定位是"解析文件"，不该去管项目配置。
        配置由上层（ingest / app / agent）读好之后传进来。
    """
    global _vision_model, _vision_timeout
    if model:
        _vision_model = model
    if timeout:
        _vision_timeout = timeout


def _vision_settings():
    """算出这次图片识别用哪个模型、等多久。"""
    model = _vision_model or os.getenv("DEEPSEEK_MODEL") or "deepseek-flash"
    return model, _vision_timeout


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
    if extension in PPTX_EXTENSIONS:
        return _read_pptx(path, name)
    if extension in XLSX_EXTENSIONS:
        return _read_excel(path, name)
    if extension in IMAGE_EXTENSIONS:
        return _read_image(path, name)
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
        # 一个字的正文都没抠出来 → 多半是扫描件（每页其实都是图片）。
        # 走到这一步说明本地解析没辙了，才动用"让模型看图"那条路 ——
        # 因为它**要花钱**，能不用就不用。
        return _ocr_pdf(path, name)

    return name, "\n\n".join(pages)


# 让模型"看图读字"的提示词。
# 写得这么啰嗦是有原因的：不写清楚它就会"帮你总结"，
# 而我们要的是**照抄原文** —— 总结过的东西没法用来做检索。
OCR_PROMPT = """把这张图里的所有文字**完整地**抄下来。

要求：
1. 逐字照抄，不要漏字、不要改写、不要总结、不要翻译
2. 保留原来的换行和大致结构
3. 如果有表格，用 | 把同一行的单元格隔开
4. 图里没有文字就直接输出：（这张图里没有文字）
5. 不要加任何解释或说明"""


def _ocr_pdf(path, name):
    """扫描版 PDF：把每一页渲染成图片，让模型读字。

    ⚠️ **这一步要花钱**，而且是"每页一次模型调用"。
    一份 20 页的扫描件 = 20 次调用。所以只在"本地抠不出字"时才走这里。
    """
    try:
        import pypdfium2
    except ImportError:
        return None, ("这份 PDF 像是扫描件，需要 OCR，但缺 pypdfium2，"
                      "先运行：pip install pypdfium2")

    model, timeout = _vision_settings()

    try:
        pdf = pypdfium2.PdfDocument(path)
    except Exception as error:
        return None, f"这个 PDF 打不开（{type(error).__name__}: {error}）"

    pages = []
    try:
        for index in range(len(pdf)):
            # scale=2 大约相当于 144 DPI —— 够看清字，图又不会太大
            image = pdf[index].render(scale=2).to_pil()

            # 渲染出来的图也可能超过上限（大页面），所以走同一套处理
            page_pieces = _plan_image_slices(image)
            page_texts = []
            for piece in page_pieces:
                try:
                    piece_text = ask_about_image(
                        _encode_for_vision(piece), OCR_PROMPT,
                        model, timeout, image_format="png")
                except RuntimeError as error:
                    return None, f"扫描件第 {index + 1} 页识别失败：{error}"

                if piece_text and not piece_text.startswith("（这张图里没有文字"):
                    page_texts.append(piece_text)

            if page_texts:
                pages.append(f"【第 {index + 1} 页】\n" + "\n".join(page_texts))
    finally:
        pdf.close()

    if not pages:
        return None, "这份 PDF 渲染出来了，但模型说里面没有文字"

    return name, "\n\n".join(pages)


def _read_pptx(path, name):
    """从 PowerPoint 里抠文字。

    每张幻灯片单独一块、带上页码 —— 因为 PPT 的内容是按页组织的，
    全混在一起会丢掉"哪句话在哪一页"这个信息。
    """
    try:
        from pptx import Presentation
    except ImportError:
        return None, "读 PPT 需要 python-pptx，先运行：pip install python-pptx"

    try:
        deck = Presentation(path)
        slides = []

        for index, slide in enumerate(deck.slides, start=1):
            lines = []
            for shape in slide.shapes:
                # 文本框里的字
                if shape.has_text_frame:
                    for paragraph in shape.text_frame.paragraphs:
                        text = "".join(run.text for run in paragraph.runs).strip()
                        if text:
                            lines.append(text)

                # 表格里的字 —— PPT 里也常有表格
                if shape.has_table:
                    for row in shape.table.rows:
                        cells = [cell.text.strip() for cell in row.cells
                                 if cell.text.strip()]
                        if cells:
                            lines.append(" | ".join(cells))

            if lines:
                slides.append(f"【第 {index} 页】\n" + "\n".join(lines))
    except Exception as error:
        return None, f"这个 PPT 解析失败（{type(error).__name__}: {error}）"

    if not slides:
        return None, "这个 PPT 里没有文字（可能整份都是图片或图表）"

    return name, "\n\n".join(slides)


def _read_image(path, name):
    """图片：让模型看图读字。

    这条路跟扫描件 PDF 是同一个原理 —— 都是"字在图上，本地读不出来"。
    """
    model, timeout = _vision_settings()

    try:
        image = Image.open(path)
        image.load()
    except Exception as error:
        return None, f"这个图片打不开（{type(error).__name__}: {error}）"

    pieces = _plan_image_slices(image)
    texts = []

    for index, piece in enumerate(pieces, start=1):
        try:
            text = ask_about_image(_encode_for_vision(piece), OCR_PROMPT,
                                   model, timeout, image_format="png")
        except RuntimeError as error:
            return None, f"图片识别失败：{error}"

        if text and not text.startswith("（这张图里没有文字"):
            # 切成多块时要标出"这是第几块"，否则内容接不上
            label = f"【第 {index}/{len(pieces)} 块】\n" if len(pieces) > 1 else ""
            texts.append(label + text)

    if not texts:
        return None, "模型说这张图里没有文字"

    return name, "\n\n".join(texts)


# 单块图片的最长边。
# 实测：3627 × 18156、8.58MB 的 PNG 会被服务端直接 400 打回
# （它的报错说 "unsupported image"，很误导 —— 其实只是太大了）。
MAX_IMAGE_SIDE = 2000
# 切片之间留一段重叠，免得正好把一行字从中间切断
SLICE_OVERLAP = 120


def _encode_for_vision(image):
    """把图片编码成 PNG 字节，顺便处理掉模型不认的形式。

    两个必须处理的问题：
      1. RGBA（带透明通道）—— 很多接口对透明通道支持不好。
         直接转 RGB 会把透明区域变成**黑色**，所以要垫一张白底再合成。
      2. 调色板模式（P）之类的老式 PNG —— 一并归一化成 RGB。
    """
    if image.mode not in ("RGB", "L"):
        # 垫白底，再合成上去 —— 透明区域就变成白色而不是黑色
        canvas = Image.new("RGB", image.size, "white")
        canvas.paste(image, mask=image.convert("RGBA").split()[-1])
        image = canvas
    elif image.mode == "L":
        image = image.convert("RGB")

    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _plan_image_slices(image):
    """把一张图切成若干块，每块都不超过 MAX_IMAGE_SIDE。

    **为什么是"切"而不是"缩小"**：
        一张 3627 × 18156 的路线图，如果直接缩到能通过的大小，
        高度还剩一万多像素，字会小到完全看不清 —— 那样识别出来全是错的。
        切成几块之后，每块的字都保持原始清晰度。

    太宽的话先等比缩窄（宽度方向的信息密度通常低一些），再纵向切。
    """
    width, height = image.size

    # 先处理宽度：超过上限就等比缩小
    if width > MAX_IMAGE_SIDE:
        ratio = MAX_IMAGE_SIDE / width
        image = image.resize((MAX_IMAGE_SIDE, max(1, int(height * ratio))),
                             Image.LANCZOS)
        width, height = image.size

    # 高度够短 → 整张发过去就行
    if height <= MAX_IMAGE_SIDE:
        return [image]

    # 纵向切片，块与块之间留 SLICE_OVERLAP 的重叠
    pieces = []
    top = 0
    step = MAX_IMAGE_SIDE - SLICE_OVERLAP
    while top < height:
        bottom = min(top + MAX_IMAGE_SIDE, height)
        pieces.append(image.crop((0, top, width, bottom)))
        if bottom >= height:
            break
        top += step

    return pieces


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


# 文件头（magic bytes）—— 用来判断"内容到底是什么格式"。
#
# **为什么要看文件头，而不只看后缀**：
#     真实世界里的文件经常被改名。比如老系统导出的 .xls，
#     交付时被改成 .xlsx —— 双击能打开，看着也是 Excel，
#     但里面根本不是新版格式。只看后缀就会读错。
ZIP_SIGNATURE = b"PK\x03\x04"                          # 新版 Office(.xlsx/.docx) 本质是 zip
OLE2_SIGNATURE = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 老的 .xls / .doc（复合文档）


def _read_excel(path, name):
    """从 Excel 里抠文字 —— 新老两种格式都认。

    做法：每一行拼成一行文本，单元格之间用 | 隔开，前面带上工作表名字。

    **为什么要保留"同一行的关系"**：
        表格里的数字单独拿出来是没有意义的 —— "128" 是什么？
        必须知道它属于哪一行的哪一列。用 | 拼起来，
        模型才能看出"年度预算 | 128 万元"是一对。

    先看文件头再决定用哪个库：
        以 PK 开头    → 新版 .xlsx，用 openpyxl
        以 OLE2 开头  → 老版 .xls，用 xlrd
        都不是        → 说明白，不硬读
    """
    with open(path, "rb") as f:
        signature = f.read(8)

    if signature.startswith(OLE2_SIGNATURE):
        return _read_xls(path, name)

    if not signature.startswith(ZIP_SIGNATURE):
        return None, (f"这个文件既不是 .xlsx 也不是 .xls（文件头是 "
                      f"{signature[:4].hex()}）—— 后缀可能是改的")

    try:
        from openpyxl import load_workbook
    except ImportError:
        return None, "读新版 Excel 需要 openpyxl，先运行：pip install openpyxl"

    try:
        # read_only=True      → 大文件不把整个工作簿读进内存
        # data_only=True      → 读公式的**计算结果**，而不是公式本身
        #                        （不然你抠出来的是 "=SUM(A1:A9)"，没用）
        workbook = load_workbook(path, read_only=True, data_only=True)
    except Exception as error:
        # 跟 PDF 一样故意接得很宽：Excel 被改坏的情况五花八门，
        # 别让一个坏文件把整个建库流程搞崩
        return None, f"这个 Excel 解析失败（{type(error).__name__}: {error}）"

    try:
        sheets = []
        for sheet in workbook.worksheets:
            lines = []
            for row in sheet.iter_rows(values_only=True):
                # 空单元格直接跳过。
                # 代价是列的位置会错开，换来的是行更短、噪声更少 ——
                # 检索时我们关心的是"提到了哪些词"，不是表格排版。
                cells = [str(value).strip() for value in row
                         if value is not None and str(value).strip()]
                if cells:
                    lines.append(" | ".join(cells))

            if lines:
                sheets.append(f"【工作表：{sheet.title}】\n" + "\n".join(lines))
    finally:
        # read_only 模式下必须显式关闭，否则文件句柄会一直占着
        workbook.close()

    if not sheets:
        return None, "这个 Excel 里没有文字（可能全是空的）"

    return name, "\n\n".join(sheets)


def _read_xls(path, name):
    """读老的 .xls（Excel 97-2003）。用 xlrd。

    正好跟 openpyxl 互补：xlrd 从 2.0 起就**不再支持** .xlsx 了，
    只认这个老格式。
    """
    try:
        import xlrd
    except ImportError:
        return None, ("这个是老的 .xls 格式（只是后缀被改成了 .xlsx），"
                      "读它需要 xlrd，先运行：pip install xlrd")

    try:
        book = xlrd.open_workbook(path)
        sheets = []
        for sheet in book.sheets():
            lines = []
            for row_index in range(sheet.nrows):
                cells = []
                for value in sheet.row_values(row_index):
                    # xlrd 把数字都读成浮点，直接 str() 会变成 "1.0"、"30.0"。
                    # 整数就还原成整数，看着才像人写的表。
                    if isinstance(value, float) and value.is_integer():
                        value = int(value)
                    text = str(value).strip()
                    if text:
                        cells.append(text)
                if cells:
                    lines.append(" | ".join(cells))
            if lines:
                sheets.append(f"【工作表：{sheet.name}】\n" + "\n".join(lines))
    except Exception as error:
        return None, f"这个老式 Excel 解析失败（{type(error).__name__}: {error}）"

    if not sheets:
        return None, "这个 Excel 里没有文字（可能全是空的）"

    return name, "\n\n".join(sheets)
