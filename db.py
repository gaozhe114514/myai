"""
db.py —— 数据库层：连接 + 表定义。

## 为什么用 SQLAlchemy（ORM）而不是直接写 SQL

**因为换数据库只改一行。**

    开发/测试  →  SQLite       （一个文件，零配置，不用装任何东西）
    生产环境   →  PostgreSQL   （改 DATABASE_URL 就行）

代码一个字不用改。这就是 ORM 最大的价值 —— 它把"不同数据库的方言差异"挡在下面。

（顺便：ORM = Object Relational Mapping，"对象关系映射"。
  它让你用 Python 对象操作数据库，不用手拼 SQL 字符串。
  好处除了可换数据库，还有防 SQL 注入、关系好写。）

## 三张表的关系

    users ──1:N──> sessions ──1:N──> messages

    一个用户有多段对话，一段对话有多条消息。
    N 的那一边用**外键**指回去。
"""

import os
from datetime import datetime

from sqlalchemy import (Column, DateTime, ForeignKey, Integer, String, Text,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

# 加载 .env。规则见 api.py 开头：**谁需要用环境变量，谁就负责把它读进来。**
#
# 这里必须自己读一次，不能指望"别人已经读过了"。
# 之前 api_server.py 能用，是靠"先 import chat"顺带加载的 —— 纯属碰巧。
# 一旦有人单独 import db（比如建表脚本），环境变量是空的，
# DATABASE_URL 就会悄悄退回默认的 SQLite：**连错了库，还不报错。**
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass    # 没装也不崩，这时得靠外面自己把环境变量设好

# 数据库地址。写在 .env 里，不写就默认用 SQLite（开发时零配置）
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///myai.db")

# SQLite 特有问题：FastAPI 用多线程处理请求，而 SQLite 默认禁止跨线程用同一个连接。
# 这个参数是官方的解法。PostgreSQL 不需要，所以只在 SQLite 时加。
_connect_args = {"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {}

engine = create_engine(
    DATABASE_URL,
    connect_args=_connect_args,
    echo=False,     # 改成 True 会把每条 SQL 打出来，调试时很有用
    future=True,
)

# 每次要用数据库，就开一个"会话"（一次数据库对话的上下文）
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

# 所有表都继承它
Base = declarative_base()


# ============================ 表定义 ============================

class User(Base):
    """用户。"""

    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    # unique=True → 用户名不能重复（数据库层面保证，不是靠代码判断）
    # index=True  → 建索引，按用户名查得快
    username = Column(String(50), unique=True, nullable=False, index=True)

    # ⚠️ 存的是哈希，不是密码本身。
    # 就算数据库被人拿走，也反推不出原始密码。
    password_hash = Column(String(200), nullable=False)

    created_at = Column(DateTime, default=datetime.now)

    sessions = relationship("ChatSession", back_populates="user",
                            cascade="all, delete-orphan")


class ChatSession(Base):
    """一段对话。

    名字叫 ChatSession 而不是 Session —— 因为 SQLAlchemy 自己有个 Session 类，
    重名会打架。
    """

    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True)

    # ⚠️ 这个外键，加上查询时的 WHERE user_id = ?，就是"数据隔离"的全部
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)

    title = Column(String(100), default="新对话")
    created_at = Column(DateTime, default=datetime.now)
    updated_at = Column(DateTime, default=datetime.now)

    user = relationship("User", back_populates="sessions")
    messages = relationship("Message", back_populates="session",
                            cascade="all, delete-orphan",
                            order_by="Message.id")


class Message(Base):
    """一条消息。"""

    __tablename__ = "messages"

    id = Column(Integer, primary_key=True)
    session_id = Column(Integer, ForeignKey("sessions.id"), nullable=False, index=True)

    # user = 用户说的，assistant = 模型说的。
    # 只允许这两个值 —— 这是约定，不是数据库强制的（要强制得用 Enum）。
    role = Column(String(20), nullable=False)

    content = Column(Text, nullable=False)

    # 资料出处。存成 JSON 字符串（PostgreSQL 有专门的 JSON 类型，
    # 但为了两种数据库通用，先用 Text）。
    sources_json = Column(Text, default="[]")

    created_at = Column(DateTime, default=datetime.now)

    session = relationship("ChatSession", back_populates="messages")


# ============================ 工具函数 ============================

def init_db():
    """建表。已经存在的表不会动（不会删数据）。

    什么时候调：应用启动时。
    """
    Base.metadata.create_all(engine)


def get_db():
    """给 FastAPI 用的依赖：每个请求开一个数据库会话，用完自动关。

    FastAPI 的 Depends(get_db) 会调它，然后在请求结束时执行 finally 里的清理。
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
