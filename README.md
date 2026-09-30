# astrbot_plugin_miao_search

接入 [Miao-Search](https://searai.rainnya.asia)（`searai.rainnya.asia`）的两阶段联网搜索插件：让大模型在对话中自动联网检索、精读网页正文。

## 功能

| 能力 | 说明 |
| --- | --- |
| LLM 函数工具 `miao_search` | 用户说「帮我搜一下 xxx」「查一下 xxx」时，模型自动联网搜索 |
| LLM 函数工具 `miao_fetch_url` | 用户发来链接说「总结一下这个链接」时，模型自动抓取正文 |
| `/search` 指令 | 不依赖大模型的手动搜索，支持 `/search quick 关键词` 强制极速检索 |
| `/fetch` 指令 | 手动抓取网页正文，例如 `/fetch https://example.com` |
| `/miao_status` 指令 | 查看当前配置与工具注册状态（API Key 已掩码） |
| 插件 Page | WebUI 内提供网址导航、API 文档入口与一键连通性自检 |

## 安装

1. 在 AstrBot 的 `data/plugins/` 下放入本插件目录（或在 WebUI 插件页安装）。
2. 在 WebUI「插件管理」中重载插件。
3. 打开插件配置，填写 **API Key**。

## 获取 API Key

1. 打开 <https://searai.rainnya.asia> 登录控制台。
2. 打开 <https://searai.rainnya.asia/docs> 查看接入文档，并在控制台生成带配额与 QPS 限制的访问令牌（形如 `sk-miao-xxxxxxxx`）。
3. 也可以直接点击插件卡片中的「Miao-Search 导航与测试」Page，里面有官网、文档、控制台入口和连通性自检按钮。

> 只想先试用？文档提供的系统预置体验 Token 是 `sk-miao-admin-default-test-key`，可临时填入验证链路是否通。

## 配置项

| 配置项 | 默认值 | 说明 |
| --- | --- | --- |
| `enable` | `true` | 插件总开关 |
| `api_base` | `https://searai.rainnya.asia` | 服务地址，通常无需修改 |
| `api_key` | 空 | **必填**，访问令牌 |
| `search_mode` | `deep` | `quick` 极速检索 / `deep` 一步直达（搜索 + 抓正文）/ `fetch` 仅正文精读 |
| `default_count` | `5` | 默认返回条数（1~20） |
| `max_fetch_count` | `2` | 一步直达模式抓取的正文篇数（1~5） |
| `max_content_length` | `3000` | 单篇正文截断长度（500~20000） |
| `timeout` | `30` | 请求超时秒数（5~120） |
| `max_retries` | `1` | 网络/5xx 失败重试次数（0~5） |
| `show_source_links` | `true` | 搜索结果是否附带来源链接 |
| `llm_tool.enable_search_tool` | `true` | 是否注册搜索工具 |
| `llm_tool.enable_fetch_tool` | `true` | 是否注册正文精读工具 |
| `command.enable_command` | `true` | 是否启用 `/search`、`/fetch` 指令 |
| `command.admin_only` | `false` | 指令是否仅管理员可用 |

各模式的关系：

- `quick`：只注册 `miao_search`，工具内部调用极速检索，仅返回标题/网址/摘要。
- `deep`：注册全部工具，`miao_search` 会自动深入抓取前 `max_fetch_count` 条网页正文后再交给模型；适合要求准确度的场景。
- `fetch`：只注册 `miao_fetch_url`，不提供搜索能力；适合用户直接发链接、或已有其他搜索源提供链接的场景。

## 调用的大模型能力

| 场景 | 使用的接口 | 关键参数 |
| --- | --- | --- |
| 极速检索 | `GET /api/search` | `q`、`count`、`engines` |
| 正文精读 | `GET /api/fetch` | `url`、`max_length` |
| 一步直达 | `/api/search` + 并发 `/api/fetch` | 上述两者组合 |

鉴权使用 `Authorization: Bearer <token>` 请求头，并同时附带 `x-api-key` 以兼容不同部署；若首次返回 `401`，会自动去掉 Bearer 再用 `x-api-key` 重试一次。

失败时工具不会抛出异常，而是返回可读的中文说明让模型转述，例如：

- `鉴权失败（UNAUTHORIZED），API Key 缺失、格式错误或已失效…`
- `令牌被停用或配额已耗尽（TOKEN_DISABLED / QUOTA_EXCEEDED）…`
- `触发限流（RATE_LIMIT_EXCEEDED），当前令牌的 QPS 已达上限…`

## 目录结构

```text
astrbot_plugin_miao_search/
├─ main.py                      # 插件主体：HTTP 客户端、LLM 工具、指令、Web API
├─ metadata.yaml                # 插件元数据
├─ _conf_schema.json            # WebUI 配置界面定义（含官网/文档/控制台导航链接）
├─ requirements.txt             # 依赖：aiohttp
├─ .astrbot-plugin/i18n/        # 配置项与 Page 的中文文案
└─ pages/settings/              # 插件 Page：网址导航 + 连通性自检
```

## 常见问题

**Q：模型不调用搜索工具？**
需要模型本身支持函数调用（Function Calling），并确认配置中 `llm_tool.enable_search_tool` 为开启状态；任务型 Agent（如内置 Agent 执行器）默认会加载插件注册的工具。

**Q：提示「未配置 API Key」？**
在插件配置里填写令牌后保存，AstrBot 会重新加载插件生效。

**Q：一步直达很慢？**
该模式需要抓取网页正文，可把 `max_fetch_count` 或 `max_content_length` 调小，或把 `search_mode` 改为 `quick`。
