/**
 * Miao-Search 插件 Page：网址导航 + 接口连通性自检。
 * 通过 window.AstrBotPluginPage bridge 调用插件后端 Web API。
 */

const bridge = window.AstrBotPluginPage;

if (!bridge) {
  document.body.innerHTML =
    '<p style="padding:16px">未能加载 AstrBot Plugin Page bridge，请在 AstrBot WebUI 中打开本页面。</p>';
  throw new Error("AstrBotPluginPage bridge unavailable");
}

const SITE = "https://searai.rainnya.asia";

const el = (id) => document.getElementById(id);

const state = {
  testing: false,
};

/** 渲染文案：优先读取插件 i18n，缺失时回退到页面内文本。 */
function render() {
  const t = (key, fallback) => bridge.t(`pages.settings.${key}`, fallback);

  el("heading").textContent = t("heading", "Miao-Search 联网搜索");
  el("subtitle").textContent = t("subtitle", "网址导航、令牌获取与连通性自检");
  el("navTitle").textContent = t("navTitle", "网址导航");
  el("navHome").textContent = t("navHome", "服务官网");
  el("navHomeDesc").textContent = t("navHomeDesc", "Miao-Search 主页，查看服务介绍与状态。");
  el("navDocs").textContent = t("navDocs", "API 接入文档");
  el("navDocsDesc").textContent = t("navDocsDesc", "接口参数、鉴权方式、返回结构与错误码说明。");
  el("navConsole").textContent = t("navConsole", "控制台 / 令牌管理");
  el("navConsoleDesc").textContent = t(
    "navConsoleDesc",
    "登录后生成带配额与 QPS 限制的访问令牌（API Key）。",
  );
  for (const id of ["linkHome", "linkDocs", "linkConsole"]) {
    el(id).textContent = t("open", "打开");
  }
  el("testTitle").textContent = t("testTitle", "连通性自检");
  el("testKeyLabel").textContent = t("testKeyLabel", "当前 API Key");
  el("keyMissing").textContent = t(
    "testKeyMissing",
    "尚未配置 API Key，请先到 AstrBot 的「插件配置」中填写。",
  );
  el("testMode").textContent = t("testMode", "测试模式");
  el("testQuery").textContent = t("testQuery", "测试关键词");
  el("query").placeholder = t("testQueryPlaceholder", "例如：AstrBot 是什么");
  el("run").textContent = state.testing ? t("testRunning", "正在请求…") : t("testRun", "开始测试");
  el("testResult").textContent = t("testResult", "返回内容");
  document.title = t("title", "Miao-Search 导航与测试");
}

/** 设置状态行文本与样式。 */
function setStatus(text, kind) {
  const node = el("status");
  node.textContent = text || "";
  node.className = kind ? `status ${kind}` : "status";
}

/** 执行一次连通性测试。 */
async function runTest() {
  if (state.testing) return;
  state.testing = true;
  render();
  el("run").disabled = true;
  setStatus(bridge.t("pages.settings.testRunning", "正在请求 Miao-Search…"), "");

  const mode = el("mode").value;
  const query = el("query").value.trim();

  try {
    const data = await bridge.apiGet("test", { mode, q: query });
    const ok = Boolean(data && data.ok);
    setStatus(
      ok
        ? bridge.t("pages.settings.testOk", "接口连通正常")
        : bridge.t("pages.settings.testFail", "接口调用失败"),
      ok ? "ok" : "err",
    );
    el("keyValue").textContent = data?.api_key_masked || "—";
    el("keyMissing").classList.toggle("hidden", Boolean(data?.api_key_configured));
    el("result").textContent = data?.result || "(空)";
    el("resultWrap").classList.remove("hidden");
  } catch (error) {
    setStatus(`${bridge.t("pages.settings.testFail", "接口调用失败")}：${error.message}`, "err");
  } finally {
    state.testing = false;
    el("run").disabled = false;
    render();
  }
}

/** 只读取密钥状态，不触发真实搜索请求。 */
async function loadKeyState() {
  try {
    const data = await bridge.apiGet("status");
    el("keyValue").textContent = data?.api_key_masked || "—";
    el("keyMissing").classList.toggle("hidden", Boolean(data?.api_key_configured));
  } catch (error) {
    el("keyValue").textContent = "—";
  }
}

await bridge.ready();

el("linkHome").href = SITE;
el("linkHome").target = "_blank";
el("linkDocs").href = `${SITE}/docs`;
el("linkDocs").target = "_blank";
el("linkConsole").href = `${SITE}/docs`;
el("linkConsole").target = "_blank";

el("run").addEventListener("click", runTest);
el("query").addEventListener("keydown", (event) => {
  if (event.key === "Enter") runTest();
});

render();
bridge.onContext(render);
await loadKeyState();
