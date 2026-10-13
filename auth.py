"""
auth.py —— 认证：密码怎么存、登录状态怎么记。

## 两个核心问题

### ① 密码怎么存？—— 存哈希，不存密码

    明文存：数据库被拖走 → 所有人的密码全泄露
    存哈希：拖走也没用 → bcrypt 是**单向**的，算不回去

    bcrypt 还故意"算得慢"（每次约 0.1 秒）—— 这不是缺点，
    是设计：让暴力破解的代价高到不可行。

### ② 登录状态怎么记？—— 发一个令牌（JWT）

    HTTP 是"无状态"的：服务器不记得上一次请求是谁发的。
    所以登录成功后发给前端一个**令牌**，前端之后每次请求都带上它。

    令牌长这样（三段，点号分隔）：
        eyJhbGci...  .  eyJzdWIi...  .  4dK7x...

    ⭐ 中间那段是**可读的**（只是 base64，不是加密）—— 谁都能解出来看。
      所以：⚠️ **令牌里绝不能放敏感信息**（密码、身份证号）。
      它的安全性靠的是最后那段"签名"—— 改了内容签名就对不上，
      服务器一验就知道被篡改过。

    这也是为什么 JWT 叫"签名"而不是"加密"。
"""

import os
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt
from fastapi import Depends, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from db import User, get_db

# 签发令牌用的密钥。**必须保密**，写在 .env 里。
# 换了它，所有已发出的令牌立即失效 —— 相当于"强制所有人重新登录"。
SECRET_KEY = os.getenv("JWT_SECRET", "dev-only-secret-change-me")

ALGORITHM = "HS256"
TOKEN_HOURS = 24 * 7        # 令牌有效期：7 天（所以一周内不用重新登录）


# ============================ 密码 ============================

def hash_password(password: str) -> str:
    """把密码变成哈希，用来存进数据库。"""
    # bcrypt 只处理前 72 个字节。新版 bcrypt 遇到超长密码会直接报错，
    # 所以这里主动截断 —— 超过 72 字节的部分本来也不参与计算。
    raw = password.encode("utf-8")[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    """核对密码是否正确。

    注意：这里**不是**"把密码哈希一遍再跟存的比"（虽然结果一样）——
    bcrypt 的哈希里混了随机盐，必须用它自己的 checkpw 来验。
    """
    try:
        return bcrypt.checkpw(password.encode("utf-8")[:72],
                              password_hash.encode("utf-8"))
    except ValueError:
        # 数据库里的哈希格式不对（比如被人改过）
        return False


# ============================ 令牌 ============================

def create_token(user_id: int) -> str:
    """给某个用户签一个令牌。"""
    payload = {
        "sub": str(user_id),        # sub = subject，令牌属于谁
        "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_HOURS),
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


def decode_token(token: str):
    """验证令牌并取出用户 id。无效或过期就返回 None。"""
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return int(payload["sub"])
    except (jwt.PyJWTError, KeyError, ValueError):
        # 签名不对 / 过期 / 内容缺字段 —— 都算无效
        return None


# ============================ FastAPI 依赖 ============================

# auto_error=False：没带令牌时不要自动报错，我们自己在下面给中文提示
_bearer = HTTPBearer(auto_error=False)


def current_user(credentials: HTTPAuthorizationCredentials = Depends(_bearer),
                 db: Session = Depends(get_db)) -> User:
    """从请求头里取出令牌，查出"当前登录的是谁"。

    用法：需要登录的接口，参数里写一句
        user: User = Depends(current_user)
    FastAPI 就会先执行这个函数；没登录就直接 401，不会进到你的代码。

    ⭐ 这就是"数据隔离"的起点 —— 后面所有查询都从 user.id 出发。
    """
    if credentials is None:
        raise HTTPException(status_code=401, detail="请先登录")

    user_id = decode_token(credentials.credentials)
    if user_id is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")

    user = db.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="用户不存在")

    return user
