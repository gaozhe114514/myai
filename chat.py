"""
chat.py —— 第 5 步：让它能读你的本地文件。

用法：
    python chat.py                            进入交互式聊天
    python chat.py "你好"                      只问一句就退出
    python chat.py --model deepseek-chat       换个模型
    python chat.py --system "你是一个毒舌吐槽助手"   换个人设
    python chat.py --resume                    接着最近一次对话继续聊

交互模式里的命令：
    /file <路径> [路径...]   加载本地 txt/md 文件当参考资料（可一次给多个）
    /list                    列出所有存过的对话
    /clear                   清空历史，另开一段新对话（也会卸掉已加载的文件）
    /exit                    退出

第 5 步新增的是 /file：把本地文档读进来，拼进 system 消息一起发给模型。
"""

import argparse                  # 解析命令行参数（--model / --system / --resume）
import json                      # 把对话存成 json、从 json 读回来
import os                        # 读环境变量、操作文件路径
import sys                       # 设置退出码
from datetime import datetime    # 生成带时间戳的文件名

import requests                  # 发 HTTP 请求

# 本项目自己的模块：从各种格式的文件里把文字抠出来
from docreader import read_document

# 读 .env、拼请求地址和请求头 —— 这些零件都收在 api.py 里了。
# 因为 docreader（图片识别）也要用，而 docreader 不能 import chat（会循环依赖）。
# 在这里 import 之后，chat.api_url 依然能用（模块属性），app.py 不用改。
# HAS_DOTENV 也一并从那边拿 —— "有没有装 dotenv"只该判断一次。
from api import HAS_DOTENV, api_headers, api_url


# ============================ 常量与内置默认值 ============================

CONFIG_FILE = "config.json"     # 默认值放这儿
SESSIONS_DIR = "sessions"       # 对话存档放这儿

# 能读哪些格式，定义在 docreader.py 里（那里也负责把各种格式转成纯文本）。
# 收窄在这里是有意的：免得你手一滑把 .env（里面有密钥）塞进 prompt。

# 这些是"走到最后没人管"时用的兜底值。
# 实际生效的顺序是：命令行参数 > config.json > 环境变量 > 这里的默认值
DEFAULT_MODEL = "deepseek-flash"
DEFAULT_SYSTEM = "你是一个乐于助人的中文 AI 助手，回答尽量简洁。"
DEFAULT_TIMEOUT = 60


# ============================ 配置 ============================

def load_config():
    """读取 config.json，返回一个字典。

    文件不存在、或者内容被改坏了，都不让程序崩 —— 返回空字典，
    等于这次全用内置默认值，同时提醒一句。
    """
    if not os.path.exists(CONFIG_FILE):
        return {}

    try:
        # encoding="utf-8" 必须写！
        # 中文 Windows 上不写这一句，Python 会按 GBK 去读文件，
        # 里面的中文就可能变乱码或直接报错（就是 requirements.txt 那个坑）。
        # 这里我们明确指定了编码，所以这个文件不需要 BOM。
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as error:
        print(f"[警告] {CONFIG_FILE} 读不了（{error}），这次先用内置默认值。")
        return {}


def check_config():
    """检查密钥和地址是否齐全。齐了返回 True，缺东西就打印提示并返回 False。"""
    base_url = os.getenv("DEEPSEEK_BASE_URL")
    api_key = os.getenv("DEEPSEEK_API_KEY")

    if base_url and api_key:
        return True

    missing = []
    if not base_url:
        missing.append("DEEPSEEK_BASE_URL")
    if not api_key:
        missing.append("DEEPSEEK_API_KEY")
    print("缺少环境变量：" + "、".join(missing))
    print("请确认目录下有 .env 文件，并且里面填好了值。")
    if not HAS_DOTENV:
        print("另外：你还没装 python-dotenv，.env 文件现在读不进来。")
        print("先运行：pip install -r requirements.txt")
    return False


# ============================ 对话存档 ============================

def now_str():
    """当前时间，形如 2026-10-10 14:30:22。给人看的。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def new_session_id():
    """生成一个会话编号，形如 20261010-143022。给文件名用的。

    用时间当编号有两个好处：
      1. 不会重名
      2. 按名字排序 = 按时间排序，所以"最近一次"就是名字排在最后的那个
    """
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def ensure_sessions_dir():
    """确保 sessions/ 目录存在。exist_ok=True = "已经有了就别报错"。"""
    os.makedirs(SESSIONS_DIR, exist_ok=True)


def session_path(session_id):
    """会话编号 → 文件路径。"""
    return os.path.join(SESSIONS_DIR, session_id + ".json")


def save_session(session):
    """把整个会话写进 sessions/<编号>.json。

    每聊完一轮就调一次 —— 这样就算程序被强行关掉，聊天记录也不会丢。
    """
    ensure_sessions_dir()
    session["updated_at"] = now_str()

    with open(session_path(session["id"]), "w", encoding="utf-8") as f:
        # ensure_ascii=False 很重要：
        # 不写的话，中文会被存成 你好 这种转义码 —— 程序读得回来，
        # 但你自己打开文件看就是一坨看不懂的东西。
        # indent=2 只是为了让文件有缩进、人能看懂。
        json.dump(session, f, ensure_ascii=False, indent=2)


def load_session(session_id):
    """把某个会话从文件里读回来。读不了就返回 None。"""
    try:
        with open(session_path(session_id), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError) as error:
        print(f"[读不了会话 {session_id}] {error}")
        return None


def all_session_ids():
    """列出所有存档的会话编号，老的在前。"""
    ensure_sessions_dir()
    # name[:-5] 是去掉结尾的 ".json"（正好 5 个字符）
    ids = [name[:-5] for name in os.listdir(SESSIONS_DIR) if name.endswith(".json")]
    # 名字是时间格式，所以直接按字符串排序就等于按时间排
    return sorted(ids)


def describe_session(session_id):
    """给 /list 用：读一个存档，压成一行简介。"""
    session = load_session(session_id)
    if not session:
        return None

    messages = session.get("messages", [])

    # 拿"你问的第一句话"当作这段对话的标题
    preview = ""
    for message in messages:
        if message.get("role") == "user":
            preview = message.get("content", "").replace("\n", " ")
            break
    if len(preview) > 18:
        preview = preview[:18] + "…"

    # 一问一答算一轮，所以"轮数"就是你问过几次
    turns = sum(1 for m in messages if m.get("role") == "user")

    return f'{session_id}   {session.get("updated_at", "?")}   {turns} 轮   {preview}'


def list_sessions():
    """处理 /list 命令：把所有存档列出来。"""
    ids = all_session_ids()
    if not ids:
        print("还没有任何存档。聊几句就会自动存下来。")
        return

    print(f"共 {len(ids)} 段对话（新的在最下面）：")
    for session_id in ids:
        line = describe_session(session_id)
        if line:
            print("  " + line)


# ============================ 本地文件（第 5 步） ============================
# 注意：真正"把文件读成文字"的活儿在 docreader.py 里 ——
# 它要处理 txt / md / pdf / docx 好几种格式，属于另一件事，所以单独放。

def handle_file_command(session, user_input):
    """处理 /file 命令：加载文件，或者列出已经加载了哪些。"""

    # "/file a.md b.txt" → ["a.md", "b.txt"]
    # 注意：直接用空格切，所以路径里**不能有空格**。
    paths = user_input.split()[1:]

    # 不带参数 → 列出当前装了哪些
    if not paths:
        docs = session["docs"]
        if not docs:
            print("还没加载任何文件。用法：/file 路径1 [路径2 ...]")
            return
        print(f"已加载 {len(docs)} 个文件：")
        total = 0
        for doc in docs:
            total += len(doc["content"])
            print(f"  {doc['name']}（{len(doc['content'])} 字）")
        print(f"合计 {total} 字，每一轮提问都会全部重发一遍。")
        return

    # 带了参数 → 一个个读进来
    for path in paths:
        name, result = read_document(path)
        if name is None:
            print(f"  跳过 {path}：{result}")
            continue

        # 同一个文件名重复加载时，先把旧的去掉，免得塞两份
        session["docs"] = [d for d in session["docs"] if d["name"] != name]
        session["docs"].append({"name": name, "content": result})
        print(f"  已加载 {name}（{len(result)} 字）")

    if session["docs"]:
        print(f"现在共加载 {len(session['docs'])} 个文件。直接提问就行。")


# ============================ 会话 ============================

def new_session(system_prompt, model):
    """开一段全新的对话，返回一个 session 字典。"""
    return {
        "id": new_session_id(),        # 编号（也是存档文件名）
        "created_at": now_str(),       # 这段对话什么时候开始的
        "model": model,                # 用的哪个模型
        "system": system_prompt,       # 用的什么人设
        # 消息数组 —— 就是第 2 步那个"记忆"。
        # 多存两个字段是为了以后能知道这段对话当时是怎么配的。
        "messages": [{"role": "system", "content": system_prompt}],
        # 用 /file 加载进来的本地文件，每项是 {"name": 文件名, "content": 正文}。
        # 注意这里**只存正文一份** —— 发出去的时候才现拼进 system 消息，
        # 这样存档文件里不会把同一份文件正反存两遍。
        "docs": [],
    }


def default_model(args, config):
    """算出该用哪个模型：命令行 > config.json > 环境变量 > 内置默认。

    用 or 串起来正好：or 会返回第一个"有值"的结果，一路都为空就落到最后的默认值。
    """
    return args.model or config.get("model") or os.getenv("DEEPSEEK_MODEL") or DEFAULT_MODEL


def default_system(args, config):
    """算出该用哪个人设：命令行 > config.json > 内置默认。"""
    return args.system or config.get("system") or DEFAULT_SYSTEM


def start_session(args, config):
    """准备一段对话，返回一个 session 字典。

    加了 --resume 就接着最近一次；否则开一段新的。
    """
    if args.resume:
        ids = all_session_ids()
        session_id = ids[-1] if ids else None     # 名字是时间格式，排最后面的就是最近的
        session = load_session(session_id) if session_id else None

        if session and session.get("messages"):
            print(f"已恢复会话 {session_id}（{len(session['messages'])} 条消息）")

            # 恢复时以存档里记的为准 —— 上次用的什么模型、什么人设，这次接着用。
            # 但命令行**显式**指定的优先：你明确说了，就听你的。
            if args.model:
                session["model"] = args.model
            if args.system:
                session["system"] = args.system
                # 人设存在消息数组的第一条；换人设就是把它替换掉
                if session["messages"][0].get("role") == "system":
                    session["messages"][0] = {"role": "system", "content": args.system}
                else:
                    session["messages"].insert(0, {"role": "system", "content": args.system})
            return session

        if session_id:
            print(f"会话 {session_id} 是空的或读不了，直接开一段新的。")
        else:
            print("没找到可恢复的历史对话，直接开一段新的。")

    return new_session(default_system(args, config), default_model(args, config))


# ============================ 组装消息 + 调用模型 ============================

def build_system_message(session):
    """把"人设"和"已加载的文件"拼成一条 system 消息。

    为什么挂在 system 里？因为模型每一轮都会重发 system 消息，
    文件就自动跟着走了 —— 不用我们每次手动往对话历史里塞一遍。
    """

    parts = [session["system"]]

    docs = session.get("docs") or []
    if docs:
        # 加一句"没有就说没有"，是为了压住模型瞎编的冲动。
        # 第 6 步做 RAG 时，这句话会变成关键。
        parts.append("以下是我给你的参考资料，回答时优先依据它们。"
                     "如果资料里找不到答案，就直接说'资料里没有'，不要自己编。")
        for doc in docs:
            parts.append(f"===== 文件：{doc['name']} =====\n{doc['content']}")

    # 用空行拼起来，人看着清楚，模型也更容易分辨"哪段是资料"
    return "\n\n".join(parts)


def build_outgoing(session, user_input):
    """组装这一次真正要发出去的消息数组。

    和 session["messages"] 有个关键区别：开头那条 system 是**现拼的**，
    带着人设 + 文件正文。存档里的 messages 只留纯人设，
    这样同一份文件不会在存档里存两遍。

    还有一个好处：这个数组是"临时的"。发失败了直接丢掉就行，
    session 根本没被动过，不需要像以前那样再"撤回"一次。
    """
    outgoing = [{"role": "system", "content": build_system_message(session)}]

    # 跳过 messages 开头那条纯人设，只取后面的对话历史
    messages = session["messages"]
    if messages and messages[0].get("role") == "system":
        outgoing.extend(messages[1:])
    else:
        outgoing.extend(messages)

    if user_input is not None:
        outgoing.append({"role": "user", "content": user_input})

    return outgoing


# ============================ 调用模型 ============================

def explain_api_error(response, model):
    """把服务端的拒绝，翻译成一句人话。"""
    print(f"[服务端返回 {response.status_code}] {response.text}")

    # 按报错内容给提示。
    # 注意顺序：必须先判断"上下文太长"，因为服务端那句话里也含 "model"
    # （"This model's maximum context length is ..."），
    # 先判断 "model" 的话会把它误报成"模型名不认识"。
    error_text = response.text.lower()
    if "context length" in error_text or "maximum context" in error_text:
        print("\n提示：这次发出去的内容太长了，超过了模型的上下文上限。")
        print("少加载几个文件，或者开一段新对话再试。")
    elif "model" in error_text:
        print(f'提示：可能是不认识模型名 "{model}"，换一个 --model 试试。')


def ask(messages, model, timeout, show=True):
    """流式请求：回答一边收一边打印到屏幕上。

    返回拼好的完整回答（要存进历史用）；失败返回 None。

    show=False 时不往屏幕上打任何东西，只把文本收回来 ——
    用来做"幕后"的调用（比如 ask.py 里先让模型改写检索词），
    那种步骤用户不需要看见。
    """

    payload = {
        "model": model,
        "messages": messages,   # ← 整段历史，一条不落地发过去
        "stream": True,         # ← 第 4 步的改动：请服务端"边生成边发给我"
    }

    try:
        # stream=True 是给 requests 库看的：别等整个响应下载完再给我，
        # 数据一到就交出来。
        # 这一条和请求体里的 "stream": True 是两回事 —— 两个都得有，
        # 只改请求体的话，requests 还是会把响应攒齐了再给我们，那就白流了。
        response = requests.post(api_url(), headers=api_headers(), json=payload,
                                 timeout=timeout, stream=True)
    except requests.exceptions.RequestException as error:
        print(f"[请求发不出去] {error}")
        print("检查一下网络，以及 .env 里的 DEEPSEEK_BASE_URL 是不是写对了。")
        return None

    if response.status_code != 200:
        explain_api_error(response, model)
        return None

    # 请求成功了，可以放心打印前缀
    if show:
        print("\nAI > ", end="", flush=True)

    pieces = []   # 收到的碎片先攒着，最后拼成完整回答

    # iter_lines() 一次给你一行，而不是等全部收完。
    # 这就是"打字机效果"的来源：数据一小块一小块地到，我们就一小块一小块地打印。
    for raw_line in response.iter_lines():
        if not raw_line:
            continue        # 空行是分隔符，跳过

        line = raw_line.decode("utf-8")     # requests 给的是字节，先解码成文字

        # 这种流式格式叫 SSE。每一行形如： data: {"choices":[...]}
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()

        # 流说完的时候，会发一个特殊的 data: [DONE] 当结束标记
        if data == "[DONE]":
            break

        try:
            chunk = json.loads(data)
        except json.JSONDecodeError:
            # 极小概率会收到不完整的一行，跳过就行，别让整段对话崩掉
            continue

        choices = chunk.get("choices")
        if not choices:
            continue        # 有些块不带正文（比如只有结束标记），跳过

        # 注意这里是 delta，不是 message ——
        # 非流式返回的是"整个回答"，流式返回的是"这一小块新增的内容"。
        delta = choices[0].get("delta") or {}
        piece = delta.get("content")
        if piece:
            # end=""    → 不换行，下一个碎片接着打在同一行
            # flush=True → 立刻显示出来，别攒在缓冲区里
            if show:
                print(piece, end="", flush=True)
            pieces.append(piece)

    if show:
        print()   # 全部收完，换行
    return "".join(pieces)


def complete(messages, model, timeout, tools=None):
    """非流式调用：一次拿全模型的**整条回复**，返回一个字典。

    和 ask() 的区别：
        ask()       边收边打印，要的是回答的**文字**
        complete()  一次拿全，要的是**整条消息** ——
                    包括"模型有没有要求调用工具、调哪一个、参数是什么"

    Agent（第 7 步）必须用它：工具调用藏在消息的 tool_calls 字段里，
    那是结构化数据，流式的文字片段里根本看不见。

    tools 不为空时，请求里会多一个 tools 字段，
    相当于把"你手上有这几件工具"告诉模型。
    """
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,     # ← 就是要一次拿全，所以这里不用流式
    }

    # 只有给了工具才加这个字段；不加的话，这个请求跟普通聊天一模一样
    if tools:
        payload["tools"] = tools

    try:
        response = requests.post(api_url(), headers=api_headers(), json=payload,
                                 timeout=timeout)
    except requests.exceptions.RequestException as error:
        print(f"[请求发不出去] {error}")
        return None

    if response.status_code != 200:
        explain_api_error(response, model)
        return None

    # 返回 choices[0]["message"] **这一整条**。
    # 注意不是 ["content"] —— content 只是这条消息里的一个字段，
    # tool_calls 是并列的另一个字段，两个都要看。
    return response.json()["choices"][0]["message"]


# ============================ 两种运行方式 ============================

def ask_once(question, args, config):
    """问一句就退出。"""
    timeout = config.get("timeout") or DEFAULT_TIMEOUT
    messages = [
        {"role": "system", "content": default_system(args, config)},
        {"role": "user", "content": question},
    ]
    # 回答已经在 ask() 里边收边打印完了，这里只需要判断成没成
    answer = ask(messages, default_model(args, config), timeout)
    if answer is None:
        return 1
    return 0


def chat_loop(args, config):
    """交互式聊天：一直读你输入，直到你输 /exit。"""
    timeout = config.get("timeout") or DEFAULT_TIMEOUT
    session = start_session(args, config)

    # 注意：模型和人设都从 session 里读，不要在这儿重新算一遍。
    # 因为加了 --resume 的时候，实际生效的是存档里记的那套 ——
    # 如果这里还按命令行重算，屏幕上显示的就会跟真正在用的对不上，
    # 变成"程序骗你"。这种显示和实际不一致的 bug 最难查。
    # 用 .get(...) 而不是直接 [] 取值：万一你手工编辑过存档、少了字段，
    # 这里也能退回默认值，而不是让程序崩掉
    model = session.get("model") or default_model(args, config)
    system_prompt = session.get("system") or default_system(args, config)

    print(f"模型：{model}")
    print(f"人设：{system_prompt}")
    print(f"存档：{session_path(session['id'])}")
    if session["docs"]:
        print(f"已加载文件：{len(session['docs'])} 个")
    print("可用命令：")
    print("  /file <路径>   加载本地 txt/md 当参考资料（可一次给多个）")
    print("  /list          列出所有存过的对话")
    print("  /clear         清空历史，重新开始（也会卸掉已加载的文件）")
    print("  /exit          退出")
    print("-" * 46)

    turn = 0  # 这一段对话已经聊了几轮

    while True:
        # ---- 1. 读你这一轮输入 ----
        try:
            user_input = input("\n你 > ").strip()
        except (KeyboardInterrupt, EOFError):
            # 你按了 Ctrl+C 或 Ctrl+Z —— 别甩一串报错，好好道个别
            print("\n再见！")
            return 0

        if not user_input:
            continue

        # ---- 2. 处理命令 ----
        if user_input == "/exit":
            print("再见！")
            return 0

        if user_input == "/clear":
            # 换一个新的 session（新编号、新文件），旧的那份原样留在磁盘上。
            # 新 session 的 docs 是空列表，所以加载过的文件也一起卸掉了。
            session = new_session(system_prompt, model)
            turn = 0
            print(f"已清空历史，开始新的一段：{session['id']}（已加载的文件也卸掉了）")
            continue

        if user_input == "/list":
            list_sessions()
            continue

        # 注意这里要写成两种判断：只用 startswith("/file") 的话，
        # "/files"、"/filename" 这种也会被误认成 /file 命令。
        if user_input == "/file" or user_input.startswith("/file "):
            handle_file_command(session, user_input)
            continue

        if user_input.startswith("/"):
            print("只认识 /file、/list、/clear、/exit 这四个命令。")
            continue

        # ---- 3. 组装这次要发出去的消息 ----
        # 开头那条 system 在这里现拼：人设 + 已加载的文件正文。
        outgoing = build_outgoing(session, user_input)

        # ---- 4. 把整段对话（含文件）发出去 ----
        answer = ask(outgoing, model, timeout)

        if answer is None:
            # 失败就什么都不记。
            # outgoing 是临时拼的，session 从头到尾没被碰过，
            # 所以不需要像早先那样再"撤回"一次 —— 这种写法更省心。
            continue

        # ---- 5. 成功了才真正写进历史 ----
        session["messages"].append({"role": "user", "content": user_input})
        session["messages"].append({"role": "assistant", "content": answer})

        # ---- 6. 马上存档 ----
        # 放在每一轮结束时存，而不是等退出时存 ——
        # 这样程序被强关（或者电脑断电）也不会丢掉之前的聊天。
        save_session(session)

        turn += 1
        # 顺便报一下这次发出去多少字 —— 这一步的"塞不下了"就是靠这个数字看出来的：
        # 加载的文件越多，这个数越大，而且每一轮都全额重发一次。
        sent_chars = sum(len(m["content"]) for m in outgoing)
        print(f"\n[第 {turn} 轮 · 历史 {len(session['messages'])} 条"
              f" · 本次发出 {sent_chars} 字 · 已存档]")


# ============================ 入口 ============================

def parse_args():
    """定义并解析命令行参数。"""
    parser = argparse.ArgumentParser(
        description="MyAI —— 我的命令行 AI 助理",
        # 这行会让 --help 里自动显示每个参数的默认值
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    # 位置参数：python chat.py "你好" 里的 "你好"
    # nargs="*" 表示"可以有 0 个或多个"；不写就是 0 个，那就进交互模式
    parser.add_argument(
        "question",
        nargs="*",
        help="直接问一句就退出；不写就进入交互式聊天",
    )

    # 可选参数：都以 -- 开头，写不写都行
    parser.add_argument("--model", help="用哪个模型（优先于 config.json）")
    parser.add_argument("--system", help="自定义人设，也就是 system 消息的内容")
    parser.add_argument(
        "--resume",
        action="store_true",   # 写了就是 True，不写就是 False
        help="接着最近一次对话继续聊，而不是重新开始",
    )

    return parser.parse_args()


def main():
    args = parse_args()

    if not check_config():
        return 1

    config = load_config()

    # 命令行后面跟了内容 → 问一句就退出；什么都没跟 → 进入交互式聊天
    if args.question:
        return ask_once(" ".join(args.question).strip(), args, config)

    return chat_loop(args, config)


if __name__ == "__main__":
    # 只有直接运行 python chat.py 时才走这里
    sys.exit(main())
