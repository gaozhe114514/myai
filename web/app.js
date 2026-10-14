/**
 * app.js —— 聊天页的全部行为。
 *
 * 这个文件只管"页面上发生什么"，不碰网络（网络在 api.js 里），
 * 也不碰危险渲染（渲染在 markdown.js 里）。
 *
 * 打开页面时的顺序很重要：
 *     ① 没有令牌 → 直接送登录页（连请求都不发）
 *     ② 有令牌   → 拿它问一次 /api/me，确认还有效，顺便拿到用户名
 *     ③ 都通过了 才画界面
 * 这样不会出现"先闪一下聊天框、再被踢回登录页"那种难看的过程。
 */

import { Api, getToken, gotoLogin } from "./api.js";
import { renderMarkdown } from "./markdown.js";


// ============================ 页面元素 ============================

const el = {
  sidebar: document.getElementById("sidebar"),
  sidebarToggle: document.getElementById("sidebar-toggle"),
  backdrop: document.getElementById("backdrop"),
  newChatBtn: document.getElementById("new-chat-btn"),
  sessionList: document.getElementById("session-list"),
  kbStatus: document.getElementById("kb-status"),
  username: document.getElementById("username"),
  logoutBtn: document.getElementById("logout-btn"),
  messages: document.getElementById("messages"),
  composer: document.getElementById("composer"),
  question: document.getElementById("question"),
  sendBtn: document.getElementById("send-btn"),
};

// 引用资料里，每段正文最多显示多少字
const SOURCE_PREVIEW = 280;


// ============================ 页面状态 ============================

const state = {
  /**
   * 当前正在哪段对话里。
   *
   * ⚠️ null 的意思是"新对话"—— 但这时候后端**什么都还没有**。
   *    真正的对话要等第一句话发出去，后端才会建。
   *    所以点"新对话"按钮不需要发任何请求，只要把它清成 null 就行。
   */
  currentSessionId: null,

  /** 正在等回答。用来挡住重复点击和乱切对话 */
  isSending: false,

  /**
   * 递增的序号，用来丢弃"过期的加载结果"。
   *
   * 场景：用户点了对话 A，又马上点了对话 B。两次请求都在飞。
   * 如果 A 的响应后到，它会把 B 的内容覆盖掉 —— 页面上显示的
   * 是 B 的高亮、A 的消息，很难查。
   * 所以每次发请求前先 +1 并记下自己的号，回来时发现号变了就直接丢弃。
   */
  loadSeq: 0,
};


// ============================ 启动 ============================

async function boot() {
  // ① 连令牌都没有，就别发请求了，直接去登录页
  if (!getToken()) {
    location.replace("login.html");
    return;
  }

  // ② 拿着令牌问一句"我是谁"。
  //    令牌过期/无效的话，api.js 会统一处理成 401 → 清令牌 → 跳登录页，
  //    并且抛异常，所以下面这段不会继续往下走。
  let me;
  try {
    me = await Api.me();
  } catch (err) {
    return;   // 已经在去登录页的路上了
  }
  el.username.textContent = me.username;

  // ③ 都通过了才画界面
  el.messages.textContent = "";
  renderEmptyState();

  loadKbStatus();      // 不 await：它慢一点没关系，别挡住对话列表
  await loadSessions();
}

/**
 * 读一次知识库状态。
 *
 * ⚠️ 只在页面加载时调一次，**绝对不要**用 setInterval 定时轮询：
 *    /api/health 内部每次都会把 7.9 MB 的 rag_index.json
 *    整个读进来再解析一遍，轮询会把 CPU 和磁盘打满。
 */
async function loadKbStatus() {
  try {
    const health = await Api.health();
    el.kbStatus.textContent = health.chunks > 0
      ? `知识库：已索引 ${health.chunks} 块`
      : "知识库：还没建库（先在服务器上跑一次 ingest.py）";
  } catch (err) {
    el.kbStatus.textContent = "知识库：状态读取失败";
  }
}


// ============================ 左栏：对话列表 ============================

async function loadSessions() {
  let sessions;
  try {
    sessions = await Api.sessions();
  } catch (err) {
    return;   // 401 已经跳走了；其他错误就不画列表
  }
  renderSessions(sessions);
}

function renderSessions(sessions) {
  // 清空列表。用 textContent = "" 比 innerHTML = "" 稳 —— 它不可能解析出标签
  el.sessionList.textContent = "";

  if (sessions.length === 0) {
    const empty = document.createElement("p");
    empty.className = "session-empty";
    empty.textContent = "还没有对话";
    el.sessionList.append(empty);
    return;
  }

  for (const session of sessions) {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "session-item";
    // 把 id 挂在元素上，切换高亮时就不用去记数组下标了
    item.dataset.sessionId = session.id;

    const title = document.createElement("span");
    title.className = "session-title";
    // ⚠️ 用 textContent。这个标题是**用户第一句话截出来的**，属于不可信内容，
    //    绝对不能往 innerHTML 里塞。
    title.textContent = session.title;

    const meta = document.createElement("span");
    meta.className = "session-meta";
    meta.textContent = `${session.message_count} 条 · ${formatTime(session.updated_at)}`;

    item.append(title, meta);
    item.addEventListener("click", () => openSession(session.id));
    el.sessionList.append(item);
  }

  markActiveSession();
}

/** 把当前对话那一项高亮出来。 */
function markActiveSession() {
  for (const item of el.sessionList.querySelectorAll(".session-item")) {
    const isActive = Number(item.dataset.sessionId) === state.currentSessionId;
    item.classList.toggle("active", isActive);
  }
}

/** 打开某段历史对话。 */
async function openSession(sessionId) {
  // 正在等回答的时候不许切走 —— 见 app.js 顶部 send() 里的说明
  if (state.isSending) return;

  state.currentSessionId = sessionId;
  markActiveSession();
  closeSidebarOnMobile();

  const mySeq = ++state.loadSeq;

  let messages;
  try {
    messages = await Api.messages(sessionId);
  } catch (err) {
    return;
  }

  // 序号变了 = 用户已经点了别的对话，这批结果作废
  if (mySeq !== state.loadSeq) return;

  renderMessages(messages.map((m) => ({
    role: m.role,
    content: m.content,
    sources: m.sources || [],
  })));
}

/** 点"新对话"：只是前端清空，**不发任何请求**。 */
function newChat() {
  if (state.isSending) return;

  state.currentSessionId = null;   // 后端要等第一句话发出去才建会话
  state.loadSeq++;                 // 让还在飞的加载结果作废
  markActiveSession();
  renderMessages([]);
  closeSidebarOnMobile();
  el.question.focus();
}


// ============================ 右侧：消息渲染 ============================

function renderMessages(list) {
  el.messages.textContent = "";

  if (list.length === 0) {
    renderEmptyState();
    return;
  }

  for (const message of list) {
    appendBubble(message);
  }
  scrollToBottom();
}

function renderEmptyState() {
  const box = document.createElement("div");
  box.className = "empty-state";

  const title = document.createElement("p");
  title.className = "empty-title";
  title.textContent = "问点什么吧";

  const hint = document.createElement("p");
  hint.className = "empty-hint";
  hint.textContent = "它会先去你的知识库里找相关资料，再照着资料回答，并标出出处。";

  const example = document.createElement("p");
  example.className = "empty-example";
  example.textContent = "比如：员工报销要在多少天内提交？";

  box.append(title, hint, example);
  el.messages.append(box);
}

/**
 * 往消息区加一条气泡。
 *
 * @param {{role: string, content: string, sources?: Array}} message
 */
function appendBubble({ role, content, sources = [] }) {
  const wrap = document.createElement("div");
  wrap.className = "msg msg-" + role;

  const bubble = document.createElement("div");
  bubble.className = "bubble";

  if (role === "assistant") {
    // ⚠️ 只有模型回答这一处走 markdown 渲染。
    //    它内部是"先转义 → 再替换加粗 → 最后才 innerHTML"，所以是安全的。
    //    见 markdown.js 里那段说明。
    bubble.innerHTML = renderMarkdown(content);
  } else {
    // 用户说的话一律当纯文本 —— 万一他在提问里打了 <script>，也只会原样显示出来
    bubble.textContent = content;
  }

  wrap.append(bubble);

  if (role === "assistant" && sources.length > 0) {
    wrap.append(buildSources(sources));
  }

  el.messages.append(wrap);
  return wrap;
}

/** 构造"依据的 N 段资料"那个可折叠面板。 */
function buildSources(sources) {
  const details = document.createElement("details");
  details.className = "sources";

  const summary = document.createElement("summary");
  summary.textContent = `依据的 ${sources.length} 段资料`;
  details.append(summary);

  for (const source of sources) {
    const box = document.createElement("div");
    box.className = "source";

    const head = document.createElement("div");
    head.className = "source-head";

    const name = document.createElement("span");
    // ⚠️ 文件名也是 textContent
    name.textContent = source.source;

    const score = document.createElement("span");
    score.className = "source-score";
    // 相似度保留 3 位小数，跟 Streamlit 版一致
    score.textContent = "相似度 " + Number(source.score).toFixed(3);

    head.append(name, score);

    const text = document.createElement("p");
    text.className = "source-text";
    const body = String(source.text ?? "");
    text.textContent = body.length > SOURCE_PREVIEW
      ? body.slice(0, SOURCE_PREVIEW) + "…"
      : body;

    box.append(head, text);
    details.append(box);
  }

  return details;
}

/** 加一条"正在思考"的占位气泡，返回两个操作：改文案 / 删掉它。 */
function appendPending() {
  const wrap = document.createElement("div");
  wrap.className = "msg msg-assistant";

  const bubble = document.createElement("div");
  bubble.className = "bubble pending";
  bubble.textContent = "正在查资料并思考…";

  wrap.append(bubble);
  el.messages.append(wrap);

  return {
    setText: (t) => { bubble.textContent = t; },
    remove: () => wrap.remove(),
  };
}

function scrollToBottom() {
  el.messages.scrollTop = el.messages.scrollHeight;
}


// ============================ 发送 ============================

async function send() {
  // ⚠️⚠️ 防重复提交的守卫必须写在**第一行**。
  //
  //     光把按钮设成 disabled 是不够的 —— 因为**在输入框里按回车
  //     不会经过按钮**，照样能重复触发。所以守卫只能放在函数入口。
  if (state.isSending) return;

  const question = el.question.value.trim();
  if (!question) return;

  state.isSending = true;
  setBusy(true);

  el.question.value = "";
  autoGrow();

  appendBubble({ role: "user", content: question });
  const pending = appendPending();
  scrollToBottom();

  // 后端最坏可能等两三分钟，光转圈不够 —— 用户会以为卡死然后刷新。
  // 这里每秒更新一次已等待时间，给他"还在动"的证据。
  let waited = 0;
  const timer = setInterval(() => {
    waited += 1;
    if (waited === 30) {
      pending.setText("还在等，模型有时候比较慢，请别刷新…");
    } else if (waited < 30) {
      pending.setText(`正在查资料并思考…已等待 ${waited} 秒`);
    }
  }, 1000);

  try {
    const data = await Api.chat(question, state.currentSessionId);

    // ⚠️⚠️ 整个前端最容易漏、后果最烦的一行：
    //      把后端返回的 session_id **收养**下来。
    //
    //      因为不传 session_id 的时候，后端会**自动新建一段对话**，
    //      并把新 id 放在返回里。如果这里不记下来，
    //      下一次提问又会当成"新对话"，于是每问一句就新开一段 ——
    //      左栏会堆满只有一条消息的会话。而且页面**看起来还是能用的**，
    //      特别容易漏测。
    state.currentSessionId = data.session_id;

    pending.remove();
    appendBubble({
      role: "assistant",
      content: data.answer,
      sources: data.sources || [],
    });
    scrollToBottom();

    // 刷新左栏，让新会话（或新的消息条数）立刻显示出来
    await loadSessions();

  } catch (err) {
    pending.remove();
    appendBubble({
      role: "assistant",
      content: "出错了：" + err.message,
      sources: [],
    });
    scrollToBottom();
  } finally {
    // finally 保证出错时也能解锁，否则用户会被永久卡住
    clearInterval(timer);
    state.isSending = false;
    setBusy(false);
    el.question.focus();
  }
}

/**
 * 进入/退出"正在发送"状态。
 *
 * 注意这里不只是禁用发送按钮 —— 左栏也一起灰掉。
 * 为什么干脆禁止在发送中切换对话：切换就得中断正在跑的请求，
 * 而后端的检索和生成**已经在花钱跑了**，中断只是浪费，
 * 还会留下"这个问题到底存没存进历史"的疑问。
 *
 * 另外必须让"禁用"**看得出来**（CSS 里把左栏调淡、鼠标变禁止），
 * 而不是点了没反应 —— 那是新手产品最容易被吐槽的点。
 */
function setBusy(busy) {
  el.sendBtn.disabled = busy;
  el.newChatBtn.disabled = busy;
  el.logoutBtn.disabled = busy;
  el.sidebar.classList.toggle("busy", busy);
}


// ============================ 小工具 ============================

/** 把输入框的高度跟着内容长。最多长到 160px，再高就自己滚动。 */
function autoGrow() {
  el.question.style.height = "auto";
  el.question.style.height = Math.min(el.question.scrollHeight, 160) + "px";
}

/**
 * 把后端给的 ISO 时间串显示成 "10-14 13:18"。
 *
 * ⚠️ 后端给的是不带时区的串，如 2026-10-14T13:18:00.123456。
 *    直接交给 new Date() 解析就行（现代浏览器按本地时间处理，是对的）。
 *    千万**不要**先把它改成 "2026-10-14 13:18:00"（中间换成空格）再解析 ——
 *    那种写法的解析结果各家浏览器不一致，Safari 会得到 Invalid Date。
 */
function formatTime(iso) {
  const date = new Date(iso);
  if (isNaN(date.getTime())) return "";
  const pad = (n) => String(n).padStart(2, "0");
  return `${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
         `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

function isMobile() {
  return window.matchMedia("(max-width: 720px)").matches;
}

function toggleSidebar(open) {
  el.sidebar.classList.toggle("open", open);
  el.backdrop.hidden = !open;
}

function closeSidebarOnMobile() {
  if (isMobile()) toggleSidebar(false);
}


// ============================ 事件绑定 ============================

el.composer.addEventListener("submit", (event) => {
  event.preventDefault();   // 拦住表单默认的"整页刷新"
  send();
});

el.question.addEventListener("keydown", (event) => {
  // 回车发送；Shift + 回车 换行（聊天框的通用习惯）
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();   // 不拦的话 textarea 会插入一个换行
    send();
  }
});

el.question.addEventListener("input", autoGrow);

el.newChatBtn.addEventListener("click", newChat);

el.logoutBtn.addEventListener("click", () => {
  if (state.isSending) return;
  // gotoLogin 会清掉 localStorage 里的令牌并跳转。
  // 不需要调后端 —— 后端没有 logout 接口，也不需要：
  // 令牌是签名的，服务器不记"谁在线"的名单。
  gotoLogin();
});

el.sidebarToggle.addEventListener("click", () => toggleSidebar(true));
el.backdrop.addEventListener("click", () => toggleSidebar(false));


// 出发
boot();
