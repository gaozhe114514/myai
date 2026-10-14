/**
 * markdown.js —— 全项目**唯一**允许把字符串塞进 innerHTML 的地方。
 *
 * 模型回答里带 Markdown 标记，比如：
 *     报销需在费用发生当月起的 **60 天内**提交
 * 我们希望中间的"60 天内"显示成粗体，而不是把那两个星号原样显示出来。
 *
 * 但这件事有个大坑，必须先讲清楚 —— innerHTML 和 textContent 的区别：
 *
 *     el.textContent = s    把 s 当**纯文本**。
 *                           浏览器负责转义，写进去的标签只会"显示出来"，
 *                           不可能被执行。**安全是构造性的**，不用你操心。
 *
 *     el.innerHTML  = s     把 s 当 **HTML 源码解析**。
 *                           写进去的标签会被真的当成标签、真的执行。
 *
 * ⚠️ 一个反直觉、但必须知道的点：
 *     innerHTML **不会**执行 <script> 标签（规范就是这么定的）。
 *     所以"我看模型输出里没有 script 标签，应该安全吧"这句话是**错的**。
 *     真正能打进来的是这些：
 *         <img src=x onerror=alert(1)>
 *         <svg onload=alert(1)>
 *         <a href="javascript:alert(1)">点我</a>
 *     都是不需要 script 标签就能执行代码的写法。
 *
 * 所以规矩是：**除了模型回答这一处，其他所有地方一律用 textContent。**
 * 而模型回答这一处，必须走下面这个函数 —— 它保证顺序正确。
 */

/**
 * 把危险字符换成"长得一样、但没有特殊含义"的实体。
 *
 * 做完这一步，字符串里就**没有任何能当标签用的东西**了。
 */
export function escapeHtml(text) {
  return String(text ?? "")
    .replace(/&/g, "&amp;")     // ⚠️ 必须第一个换。
                                //    顺序错了会把后面换出来的 & 又换一遍
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/**
 * 渲染模型回答。目前只处理两件事：**加粗** 和换行。
 *
 * ⚠️ 三步的**顺序不能变**：
 *
 *     ① 先转义          原始文本 → 一堆惰性字符（已经不是 HTML 了）
 *     ② 再替换加粗      在我们自己造的字符串里插 <strong>
 *     ③ 最后才 innerHTML
 *
 * 为什么会错：如果先替换加粗、再转义，那我们自己插进去的 <strong>
 * 也会被转义成 &lt;strong&gt; —— 页面上就会显示出一堆尖括号。
 * 而如果跳过第 ① 步直接 innerHTML，那模型返回什么就能执行什么。
 *
 * 做完第 ① 步之后，字符串里**只剩我们自己写的标签**，
 * 所以这时候用 innerHTML 是安全的。
 *
 * 以后想支持列表、代码块，都必须遵守同样的顺序，否则立刻破防。
 */
export function renderMarkdown(text) {
  const safe = escapeHtml(text);
  const bolded = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  return bolded.replace(/\n/g, "<br>");
}
