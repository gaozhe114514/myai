"""
agent.py —— 第 7 步：给它装上手和脚（工具调用 / Agent 循环）。

用法：
    python agent.py "现在几点？"
    python agent.py "读一下 data.txt，告诉我里面有几个人的名字"
    python agent.py "算一下 (12 + 8) * 3 等于多少"
    python agent.py --yes "把结果写到 result.txt 里"

前面几步，模型只会"说话"。
这一步之后，它能**自己动手**：查时间、算数、读文件、写文件。

关键在于一个循环 —— 这个循环就是所有 AI Agent 的心脏：

    1. 把问题 + "你有哪些工具"一起发给模型
    2. 模型可能不回答，而是说"我要调用 read_file，参数是 xxx"
    3. **你的程序**去执行那个函数，把结果作为一条 tool 消息发回去
    4. 带着新信息再问一次 → 回到第 2 步
    5. 直到模型说"我做完了"，给出最终文字回答

注意第 3 步：**模型自己执行不了任何东西**。它只能"请求"。
真正动手的永远是这段 Python 代码 —— 当然，它也就能拒绝。
"""

import argparse
import ast
import json
import operator
import os
import sys
from datetime import datetime

# 复用 chat.py 里的：调模型（一次性拿全）、读配置、检查密钥
from chat import (DEFAULT_TIMEOUT, check_config, complete, default_model,
                  load_config)
# 复用第 6 步写好的 RAG 检索 —— 一份实现，两处使用
import rag_common

# 最大轮数：防止模型来回调工具停不下来（真的会）
DEFAULT_MAX_ROUNDS = 6

# 命令行 --yes 打开它，写文件就不再逐个确认
AUTO_YES = False

# 下面两个由 run_agent 在循环开始前填好。
# 为什么非得用全局变量？因为工具函数的参数是**模型给的**，
# 我们没机会在调用时额外塞一个"用哪个模型"进去 ——
# 函数签名必须跟发给模型的 schema 严格对应，多一个参数就报错。
AGENT_MODEL = None
AGENT_TIMEOUT = 60


AGENT_SYSTEM_PROMPT = """你是一个可以使用工具的助手。

规则：
1. 不要凭空猜。需要当前时间就用工具查，需要算数就用工具算，需要看文件就去读。
2. 涉及公司制度、产品说明、内部规定、流程规范这类问题，**必须先调用
   search_knowledge_base 查资料**，不要凭记忆回答。资料里没有就说没有。
3. 可以连续调用多次工具，直到你有足够信息回答。
4. 信息够了就直接给最终回答，不要再多调一次工具。
5. 用中文回答，简洁一点。"""


# ============================ 五个工具 ============================

def get_current_time():
    """工具 1：获取当前时间。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


# 只允许这些运算符。
# 别的（函数调用、属性访问、导入模块）一律拒绝 —— 见下面 calculate 的说明。
_ALLOWED_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval_node(node):
    """递归计算语法树的节点。只认数字和运算符，别的一律拒绝。"""
    # 叶子节点：一个数字
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError(f"只支持数字，不支持 {type(node.value).__name__}")

    # 一元运算：-5 里的那个负号
    if isinstance(node, ast.UnaryOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持这种运算符")
        return op(_eval_node(node.operand))

    # 二元运算：a + b
    if isinstance(node, ast.BinOp):
        op = _ALLOWED_OPS.get(type(node.op))
        if op is None:
            raise ValueError("不支持这种运算符")

        left = _eval_node(node.left)
        right = _eval_node(node.right)

        # 挡住 10 ** 10 ** 10 这种 —— 会算到天荒地老，把程序卡死
        if isinstance(node.op, ast.Pow) and abs(right) > 1000:
            raise ValueError("指数太大了（超过 1000），算不动")

        return op(left, right)

    raise ValueError(f"表达式里出现了不允许的内容：{type(node).__name__}")


def calculate(expression):
    """工具 2：安全地计算一个数学表达式。

    **为什么不用 eval()？**

    eval() 会把字符串当**代码**执行。模型完全可以生成这么一串：

        __import__('os').system('一堆破坏命令')

    那不是"算数"，那是**任意代码执行** —— 有人让你算 1+1，
    结果你的电脑被人接管了。

    所以这里用 ast：先把表达式解析成"语法树"，
    然后自己逐个节点检查，只放行数字和运算符。
    这不是多此一举 —— 这是"能让模型碰的东西，必须限定死"。
    """
    try:
        tree = ast.parse(expression, mode="eval")
    except SyntaxError:
        return f"这个表达式看不懂，解析不了：{expression}"

    try:
        value = _eval_node(tree.body)
    except ValueError as error:
        return f"算不了：{error}"
    except ZeroDivisionError:
        return "除数不能是 0"

    # 整数就别显示成 6.0，看着别扭
    if isinstance(value, float) and value.is_integer():
        value = int(value)

    return f"{expression} = {value}"


def _safe_path(path):
    """把文件路径限制在**当前工作目录**里。

    模型说"读 C:\\Windows\\system32\\..."或者"写 ..\\..\\什么东西"的时候，
    这里会直接拒绝。

    这是 Agent 最基础的一道安全线：**能力越大，越要划清它能碰的范围。**
    你等下会看到，模型是真的会去试各种路径的。
    """
    base = os.path.abspath(os.getcwd())
    full = os.path.abspath(os.path.join(base, path))

    # 必须正好在工作目录里，或者在它的子目录里
    if full != base and not full.startswith(base + os.sep):
        raise ValueError(f"不允许访问工作目录以外的路径：{path}")

    return full


def read_file(path):
    """工具 3：读一个文本文件。"""
    full = _safe_path(path)

    if os.path.isdir(full):
        return f"{path} 是个目录，不是文件"
    if not os.path.exists(full):
        return f"文件不存在：{path}"

    try:
        with open(full, "r", encoding="utf-8") as f:
            content = f.read()
    except UnicodeDecodeError:
        return f"{path} 不是 UTF-8 编码的文本文件，读出来是乱码，跳过"
    except OSError as error:
        return f"读不了 {path}：{error}"

    # 太长的文件只给前面一截 —— 免得一个几万行的日志把上下文撑爆
    if len(content) > 4000:
        return (content[:4000]
                + f"\n...（文件太长，只给前 4000 字；全文共 {len(content)} 字）")

    return content or "(文件是空的)"


def write_file(path, content):
    """工具 4：写文件（同名文件会被覆盖）。

    覆盖文件是**不可逆**的操作，所以这里加了一道确认。
    Claude Code 每次要改你的文件时也会先问你 —— 同一个道理。
    """
    full = _safe_path(path)

    action = "覆盖" if os.path.exists(full) else "新建"
    print(f"\n  [需要你确认] 模型要{action}文件：{full}")

    if not AUTO_YES:
        try:
            answer = input("  允许吗？输入 y 允许，其它任何键拒绝：").strip().lower()
        except (KeyboardInterrupt, EOFError):
            answer = ""
        if answer != "y":
            # 拒绝也是正常结果 —— 把"被拒绝"作为工具结果告诉模型，
            # 它就会换个办法或者直接说明情况，而不是卡死
            return "用户拒绝了这次写入，文件没有被修改。"

    try:
        with open(full, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as error:
        return f"写不了 {path}：{error}"

    return f"已{action} {path}，写入 {len(content)} 字。"


def search_knowledge_base(query):
    """工具 5：在本地文档库里检索资料。

    这个工具是把第 6 步那整套 RAG（查询改写 + 向量检索）**包成了一件工具**。

    包进来之后，模型就多了一个选择：遇到"公司规定是什么"这类问题，
    它可以不再靠记忆瞎答，而是先去查。

    注意这里只是"把资料捞出来给模型看"，真正组织语言回答的还是模型自己 ——
    所以这个工具返回的是**原始片段**，不是最终答案。
    """
    # retrieve 返回 hits=None 只有一个原因：库还没建
    hits, queries = rag_common.retrieve(query, AGENT_MODEL, AGENT_TIMEOUT)

    if hits is None:
        return "知识库还没建。请先在命令行里运行：python ingest.py"

    if not hits:
        return "知识库里没有找到相关内容。"

    # 顺手把"实际用来检索的词"告诉模型。
    # 万一拆解或改写跑偏了，模型能从这个信息里看出来，换个词再查一次。
    header = ""
    if queries != [query]:
        header = "（实际用来检索的词：" + " ｜ ".join(queries) + "）\n\n"

    return header + rag_common.format_hits(hits)


# ============================ 工具说明书 ============================

# 这份 JSON 就是发给模型的"工具说明书"。
# 注意 description 写得多细 —— 模型只能靠这些文字判断"该不该用这个工具"。
# 描述写得含糊，它就会用错工具，或者该用的时候不用。
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "获取当前的日期和时间。用户问'现在几点''今天几号''今天星期几'时用这个，不要自己猜。",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "calculate",
            "description": "计算一个数学表达式，支持 + - * / // % ** 和括号。需要算术时用它，不要心算，容易错。",
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "要计算的表达式，例如 '(12 + 8) * 3'",
                    },
                },
                "required": ["expression"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取当前工作目录下的一个文本文件。需要看文件内容时用它，不要凭印象回答。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件路径，相对当前工作目录，例如 'data.txt'",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容写入当前工作目录下的文件（同名文件会被覆盖）。只在用户明确要求写文件时才用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "文件路径，例如 'note.txt'"},
                    "content": {"type": "string", "description": "要写入的完整内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_knowledge_base",
            "description": (
                "在本地文档库里检索资料。用户问公司制度、产品说明、内部规定、"
                "流程规范这类需要依据文档才能确认的问题时，必须先调用它，"
                "不要凭记忆回答。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "检索用的关键词或短句，例如 '报销提交时限'",
                    },
                },
                "required": ["query"],
            },
        },
    },
]

# 名字 → 真正要执行的函数
TOOL_FUNCTIONS = {
    "get_current_time": get_current_time,
    "calculate": calculate,
    "read_file": read_file,
    "write_file": write_file,
    "search_knowledge_base": search_knowledge_base,
}


def execute_tool(name, raw_arguments):
    """按名字找到对应的函数并执行。

    raw_arguments 是模型给的**字符串**（内容是一段 JSON），
    不是字典 —— 所以得先 json.loads 解析。
    """
    func = TOOL_FUNCTIONS.get(name)
    if func is None:
        return f"没有叫 {name} 的工具"

    try:
        args = json.loads(raw_arguments) if raw_arguments else {}
    except json.JSONDecodeError as error:
        return f"参数不是合法的 JSON，没法执行：{error}"

    if not isinstance(args, dict):
        return "参数格式不对，应该是一个 JSON 对象"

    # 工具内部出任何错，都当成"这次调用失败"返回给模型，
    # 而不是让整个程序崩掉 —— 模型看到失败信息通常会自己换个做法
    try:
        return str(func(**args))
    except TypeError as error:
        return f"参数不对：{error}"
    except Exception as error:
        return f"执行出错：{type(error).__name__}: {error}"


# ============================ Agent 循环 ============================

def run_agent(question, model, timeout, max_rounds):
    """Agent 的主循环。"""

    # 工具函数拿不到"用哪个模型"，所以在这里填进全局变量，
    # 让 search_knowledge_base 做"拆问题 + 改写查询"时能用上
    global AGENT_MODEL, AGENT_TIMEOUT
    AGENT_MODEL = model
    AGENT_TIMEOUT = timeout

    messages = [
        {"role": "system", "content": AGENT_SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for round_no in range(1, max_rounds + 1):
        print(f"\n{'=' * 52}")
        print(f"第 {round_no} 轮：把消息发给模型（同时告诉它有哪 {len(TOOLS)} 个工具）")
        print("=" * 52)

        message = complete(messages, model, timeout, tools=TOOLS)
        if message is None:
            return 1

        tool_calls = message.get("tool_calls")

        # ---- 模型没要求调工具 → 它给出最终回答了，循环结束 ----
        if not tool_calls:
            print("\n【模型决定：不再调用工具，直接回答】\n")
            print(message.get("content") or "(模型没有返回文字)")
            return 0

        # ---- 模型要求调用工具 ----
        # 这一步很关键：必须把模型这条"我要调工具"的消息**也存进历史**。
        # 因为下面的 tool 消息必须紧跟在带 tool_calls 的 assistant 消息后面，
        # 少了它，下一次请求会被服务端直接拒绝。
        messages.append(message)

        print(f"\n【模型决定：调用 {len(tool_calls)} 个工具】")

        for call in tool_calls:
            name = call["function"]["name"]
            raw_args = call["function"]["arguments"]

            print(f"\n  -> 工具名：{name}")
            print(f"     参数（模型给的原始 JSON）：{raw_args}")

            result = execute_tool(name, raw_args)

            # 结果太长就截断显示，屏幕上别刷屏
            preview = result if len(result) <= 200 else result[:200] + " ...(已截断)"
            print(f"     执行结果：{preview}")

            # 把结果作为一条 tool 消息发回去。
            # tool_call_id 必须对得上刚才那一次调用 ——
            # 模型一次可能要调好几个工具，靠这个 id 才能分清谁对应谁。
            messages.append({
                "role": "tool",
                "tool_call_id": call["id"],
                "content": result,
            })

    # 轮数用完了还没收尾，说明模型陷在里面了
    print(f"\n已经调用 {max_rounds} 轮，停下了。")
    print("（防止模型来回调工具停不下来。想让它是多试几轮，加 --max-rounds）")
    return 1


# ============================ 入口 ============================

def parse_args():
    parser = argparse.ArgumentParser(description="能使用工具的 AI 助手")
    parser.add_argument("question", nargs="*", help="你要它做的事")
    parser.add_argument("--model", help="用哪个模型（优先于 config.json）")
    parser.add_argument("--max-rounds", type=int, default=DEFAULT_MAX_ROUNDS,
                        help="最多循环几轮")
    parser.add_argument("--yes", action="store_true",
                        help="写文件不再逐个确认（小心用）")
    return parser.parse_args()


def main():
    global AUTO_YES

    args = parse_args()
    AUTO_YES = args.yes

    question = " ".join(args.question).strip()
    if not question:
        print('用法：python agent.py "你要它做的事"')
        print('例如：python agent.py "现在几点？"')
        return 1

    if not check_config():
        return 1

    config = load_config()
    return run_agent(
        question,
        default_model(args, config),
        config.get("timeout") or DEFAULT_TIMEOUT,
        args.max_rounds,
    )


if __name__ == "__main__":
    sys.exit(main())
