/**
 * login.js —— 登录页的行为。
 *
 * 这个页面只干三件事：
 *     1. 在「登录」和「注册」两个模式之间切换
 *     2. 把用户名密码发给后端
 *     3. 成功了就把令牌存下来、跳到聊天页
 *
 * 所有跟网络打交道的事都在 api.js 里，这里只管"用户点了什么、页面上显示什么"。
 */

import { Api, getToken, setToken } from "./api.js";

// 当前是"登录"还是"注册"。点下面那行小字来回切。
let mode = "login";

// 先把要用到的元素都找出来，只找一次
const form = document.getElementById("auth-form");
const usernameInput = document.getElementById("username");
const passwordInput = document.getElementById("password");
const submitBtn = document.getElementById("submit-btn");
const errorBox = document.getElementById("error");
const switchHint = document.getElementById("switch-hint");
const switchLink = document.getElementById("switch-link");


// ============================ 一进来先看：是不是已经登录过了 ============================

// localStorage 里有令牌 = 之前登录过，那就不用再看登录页了。
//
// 这里的令牌可能是**过期的**，但没关系 —— 聊天页会拿它去问一次 /api/me，
// 过期了会被自动踢回这个页面。所以这里粗判一下就够，不用费劲去验证。
if (getToken()) {
  location.replace("index.html");
}


// ============================ 页面上的小工具 ============================

function showError(message) {
  errorBox.textContent = message;
  errorBox.hidden = false;
}

function clearError() {
  errorBox.textContent = "";
  errorBox.hidden = true;
}

/** 在「登录」和「注册」两个模式之间切换。 */
function setMode(next) {
  mode = next;
  const isLogin = mode === "login";

  submitBtn.textContent = isLogin ? "登 录" : "注 册";
  switchHint.textContent = isLogin ? "还没有账号？" : "已经有账号了？";
  switchLink.textContent = isLogin ? "注册一个" : "去登录";
  document.title = (isLogin ? "登录" : "注册") + " · MyAI 知识库助手";

  // 这条是给密码管理器看的：登录时是"已有密码"，注册时是"新密码"，
  // 它会据此决定是提示"保存密码"还是"生成强密码"
  passwordInput.autocomplete = isLogin ? "current-password" : "new-password";

  clearError();
  usernameInput.focus();
}


// ============================ 事件 ============================

switchLink.addEventListener("click", (event) => {
  // ⚠️ 必须拦住 <a href="#"> 的默认行为。
  //    不拦的话地址栏会被加上一个 #，页面还会跳到顶部。
  event.preventDefault();
  setMode(mode === "login" ? "register" : "login");
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();       // 拦住表单默认的"整页刷新提交"
  clearError();

  const username = usernameInput.value.trim();
  const password = passwordInput.value;

  if (!username || !password) {
    showError("用户名和密码都要填");
    return;
  }

  // 防连点。注意：光把按钮设成 disabled 是不够的 ——
  // 因为**在输入框里按回车不会经过按钮**，照样能重复触发 submit。
  // 所以这里还要一个显式的开关挡住。
  if (submitBtn.disabled) return;
  submitBtn.disabled = true;
  const originalText = submitBtn.textContent;
  submitBtn.textContent = mode === "login" ? "登录中…" : "注册中…";

  try {
    const data = mode === "login"
      ? await Api.login(username, password)
      : await Api.register(username, password);

    setToken(data.token);

    // ⚠️ 用 replace 而不是 href：登录页不该留在浏览器历史里。
    //    否则用户从聊天页按"后退"，会退回这个登录表单，很怪。
    //
    //    注意这里写的是 "index.html"，不是 "/index"。
    //    后端的静态托管**不做**"地址找不到就回退到首页"这种事，
    //    所以站内跳转一律要把 .html 写全。
    location.replace("index.html");

  } catch (err) {
    // err.message 直接就是能给人看的中文 —— api.js 已经翻译好了
    showError(err.message);
    submitBtn.disabled = false;
    submitBtn.textContent = originalText;
  }
  // 注意：成功那条路**不**恢复按钮，因为页面马上就跳走了。
  // 万一跳转慢，按钮保持禁用状态正好，防止用户重复点。
});

// 打开页面就把光标放进用户名框里，少点一次鼠标
usernameInput.focus();
