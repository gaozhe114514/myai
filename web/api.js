/**
 * api.js —— 全项目**唯一**碰 fetch 和登录令牌的地方。
 *
 * 为什么要单独一个文件？
 *   登录页和聊天页都要做两件事：带着令牌发请求、令牌失效了跳回登录页。
 *   如果两个页面各写一份，改的时候一定会漏掉一处 ——
 *   这就是代码开始腐烂的起点。
 *
 * 用这个文件只需要记住两件事：
 *   1. 令牌存在浏览器的 localStorage 里，键名叫 myai_token
 *   2. 任何请求只要收到 401，它自己会清掉令牌、跳回登录页。
 *      所以调用方基本不用自己判断"我现在登录了没"。
 */

/**
 * localStorage 是浏览器分给每个网站的一小块存储，关掉浏览器也还在。
 * 名字里带 myai_ 前缀，是为了将来同一个域名下放别的东西时不会撞名。
 *
 * ⚠️ 为什么用 localStorage 存令牌？以及它的代价：
 *    好处 —— 令牌**不会自动随请求发送**（每次都得我们手动放进请求头），
 *            所以天然没有 CSRF 问题；而且零后端改动。
 *    代价 —— 万一页面上有 XSS 漏洞，令牌会被直接偷走（有效期 7 天）。
 *    我们能接受它的前提是：这个页面不引任何第三方脚本，
 *    而且所有不可信文本一律走安全渲染（见 markdown.js）。
 *    真要上生产给别人用，应该换成 HttpOnly 的 Cookie，并补一套 CSRF 防护。
 */
const TOKEN_KEY = "myai_token";

/**
 * 等模型回答要多久？
 *
 * 后端 config.json 里"单次调用"超时是 60 秒，但一次问答内部可能调好几次
 * 模型（拆问题 → 改写检索词 → 最后生成），所以最坏能到两三分钟。
 * 这里给 120 秒：比"正常情况"宽得多，又比"最坏情况"紧一点。
 * 千万别设 30 秒 —— 那会把正常的长回答自己掐断。
 */
const CHAT_TIMEOUT_MS = 120 * 1000;
const NORMAL_TIMEOUT_MS = 15 * 1000;


// ============================ 令牌 ============================

export function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}

export function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

/**
 * 退出去，回登录页。
 *
 * ⚠️ 用 location.replace 而不是 location.href。
 *    replace 不会在浏览器历史里留下这一页。
 *    否则用户点了"退出登录"，再按浏览器"后退"，又回到了聊天页 —— 很别扭。
 *
 * 注意：这里**不需要调后端**。退出登录就是"把令牌扔掉"这么简单。
 * （后端没有、也不需要 logout 接口：令牌是签名的，服务器不记名单。）
 */
export function gotoLogin() {
  clearToken();
  location.replace("login.html");
}


// ============================ 请求 ============================

/**
 * 把出错的响应，变成一句能直接显示给用户的中文。
 *
 * ⚠️ 这里有个新手一定会踩的坑：FastAPI 的两种错误长得完全不一样 ——
 *
 *     401 / 400：{"detail": "用户名或密码不对"}
 *                detail 是一句话，可以直接显示
 *
 *     422      ：{"detail": [{"loc": [...], "msg": "..."}, ...]}
 *                detail 是一个**数组**！直接显示出来是一堆看不懂的东西
 *
 *   422 是"参数校验没过"，比如用户名只填了 1 个字、密码不足 6 位。
 *   这是用户最常碰到的错误之一，所以必须翻译成人话。
 */
async function errorText(res) {
  let data = null;
  try {
    data = await res.json();
  } catch (e) {
    // 后端返回的不是 JSON（比如 500 崩了，返回的是 HTML）
    return `请求失败（HTTP ${res.status}）`;
  }
  if (data && typeof data.detail === "string") {
    return data.detail;
  }
  if (res.status === 422) {
    return "输入不符合要求：用户名要 2-50 个字，密码至少 6 位";
  }
  return `请求失败（HTTP ${res.status}）`;
}

/**
 * 发一个请求。所有接口都从这里走。
 *
 * path 写成 "/api/xxx" 这种**相对路径**，不写完整网址。
 * 因为前端和接口是同一个地址、同一个端口（同源），
 * 所以同一份代码在 127.0.0.1、在服务器 IP、在手机访问时都原样能用。
 */
async function request(path, { method = "GET", body, timeoutMs = NORMAL_TIMEOUT_MS } = {}) {
  const headers = {};

  const token = getToken();
  // 令牌放在这个请求头里 —— 后端 auth.py 就是从这儿把它取出来的
  if (token) {
    headers["Authorization"] = "Bearer " + token;
  }
  if (body !== undefined) {
    headers["Content-Type"] = "application/json";
  }

  // AbortController 用来"等太久就不等了"。
  // 不设的话，万一后端卡住，页面会一直转圈，用户只能自己刷新。
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);

  let res;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
  } catch (e) {
    // fetch 只有在"网络层面"出问题时才会抛异常：断网、连不上、被主动中断
    clearTimeout(timer);
    if (e.name === "AbortError") {
      throw new Error(`等了太久还没回应（超过 ${Math.round(timeoutMs / 1000)} 秒），已经放弃，请重试`);
    }
    throw new Error("连不上服务器，确认一下后端是不是在跑");
  }
  clearTimeout(timer);

  // ⚠️ 401 = 没登录 / 令牌过期 / 用户不存在。三种情况后端都返回 401，
  //    前端的处理方式也一样：清掉令牌，回登录页。
  //    这样写的好处是——前端不用去认后端到底返回了哪句中文提示。
  if (res.status === 401) {
    gotoLogin();
    // 抛出去是为了**打断调用方后面的代码**。
    // 不抛的话，它会拿着 undefined 继续往下画界面，报一堆莫名其妙的错。
    throw new Error("登录已过期，请重新登录");
  }

  if (!res.ok) {
    throw new Error(await errorText(res));
  }

  return res.json();
}


// ============================ 各个接口 ============================
//
// 这里只是把上面的 request 包一层，让调用处读起来像一句话。
// 具体每个接口要什么参数、返回什么，看后端 api_server.py 的注释，
// 或者打开 http://127.0.0.1:8000/docs 点着玩。

export const Api = {
  /** 登录：成功返回 { token, username } */
  login: (username, password) =>
    request("/api/login", { method: "POST", body: { username, password } }),

  /** 注册：成功直接返回 { token, username }，不用再登录一次 */
  register: (username, password) =>
    request("/api/register", { method: "POST", body: { username, password } }),

  /** 当前登录的是谁。令牌无效会走 401 → 自动跳登录页 */
  me: () => request("/api/me"),

  /** 知识库状态：{ status, chunks }。chunks = 0 表示还没建库 */
  health: () => request("/api/health"),

  /** 我的所有对话：[{ id, title, updated_at, message_count }] */
  sessions: () => request("/api/sessions"),

  /** 某段对话里的全部消息：[{ id, role, content, sources, created_at }] */
  messages: (sessionId) => request(`/api/sessions/${sessionId}/messages`),

  /**
   * 提问。
   *
   * ⚠️ sessionId 传 null = 后端**自动新建一段对话**，返回里会带上新的 session_id。
   *    调用方**必须把这个 id 记下来**，否则每问一句都会新开一段对话。
   */
  chat: (question, sessionId) =>
    request("/api/chat", {
      method: "POST",
      body: { question, session_id: sessionId },
      timeoutMs: CHAT_TIMEOUT_MS,
    }),
};
