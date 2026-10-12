"""
api.py —— 跟模型服务通信的"底层零件"：地址、请求头、发请求。

**为什么单独抽一个文件**：

    现在有两个地方要发请求：
        chat.py       —— 对话、工具调用
        docreader.py  —— 图片识别（把扫描件/图片里的字读出来）

    但 docreader **不能** import chat —— 因为 chat 已经 import 了 docreader，
    那就成了循环依赖，Python 会直接报错。

    所以把"怎么发请求"这件事放到最底层：谁都能用，它谁都不依赖。

    依赖方向应该永远是这样，不能反过来：
        最底层  api.py
                  ↑
                docreader.py / chat.py
                  ↑
                rag_common.py / agent.py
                  ↑
                app.py（网页界面）
"""

import base64
import os

import requests

# ============================ 加载 .env ============================

# **这件事必须放在这一层做。**
#
# 原因：api.py 是最底层，谁 import 它都行 ——
# 包括 docreader（图片识别要用）。而 docreader 并不知道 .env 的存在。
#
# 之前 load_dotenv() 写在 chat.py 里 —— 那是"碰巧能用"：
# 只要走 chat 进来就会加载。但一旦有人直接 import docreader，
# 环境变量就是空的，报错还是 "NoneType has no attribute rstrip" 这种
# 完全看不出原因的错。
#
# 规则：**谁需要用环境变量，谁就负责把 .env 读进来。**
try:
    from dotenv import load_dotenv

    load_dotenv()
    HAS_DOTENV = True
except ImportError:
    # 没装 python-dotenv 也不崩，只是这次得靠外面自己设好环境变量
    HAS_DOTENV = False


def api_url():
    """请求地址 = 环境变量里的接口地址 + 固定路径。"""
    return os.getenv("DEEPSEEK_BASE_URL").rstrip("/") + "/chat/completions"


def api_headers():
    """请求头：带上密钥，并声明发过去的是 JSON。"""
    return {
        "Authorization": f"Bearer {os.getenv('DEEPSEEK_API_KEY')}",
        "Content-Type": "application/json",
    }


def ask_about_image(image_bytes, prompt, model, timeout, image_format="png"):
    """把一张图发给模型，让它按 prompt 的要求看图，返回它说的文字。

    用途：**OCR**。模型本身就能"看图"，所以图片和扫描件里的字也能读出来 ——
    不用额外装 Tesseract / PaddleOCR 那类 OCR 引擎（那要么得装个 exe，
    要么得拉 1-2GB 的依赖）。

    代价：这一步是**真的调模型**，按图片大小算 token，要花钱。
    所以 docreader 里的策略是"能本地抠出文字就不走这里，抠不出来才用"。

    失败就抛 RuntimeError，把原因写在消息里（调用方负责接住并展示）。
    """
    encoded = base64.b64encode(image_bytes).decode()

    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/{image_format};base64,{encoded}"
                        },
                    },
                ],
            }
        ],
        "stream": False,
    }

    try:
        response = requests.post(api_url(), headers=api_headers(), json=payload,
                                 timeout=timeout)
    except requests.exceptions.RequestException as error:
        raise RuntimeError(f"图片识别时请求发不出去：{error}") from error

    if response.status_code != 200:
        raise RuntimeError(
            f"图片识别失败，服务端返回 {response.status_code}：{response.text[:200]}")

    message = response.json()["choices"][0]["message"]
    return (message.get("content") or "").strip()
