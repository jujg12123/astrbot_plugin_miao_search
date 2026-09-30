"""Miao-Search（searai.rainnya.asia）联网搜索插件。

提供两阶段联网搜索能力：
1. 极速目录检索：毫秒级返回标题、网址与摘要。
2. 正文精读提取：抓取目标网页并输出已消毒的 Markdown 正文。

同时以 LLM 函数工具的形式注册到 AstrBot，让大模型可以在对话中自动调用。
"""

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import aiohttp
from pydantic import Field
from pydantic.dataclasses import dataclass as pydantic_dataclass

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.api.web import error_response, json_response, request
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import FunctionTool, ToolExecResult
from astrbot.core.astr_agent_context import AstrAgentContext

PLUGIN_NAME = "astrbot_plugin_miao_search"
DEFAULT_API_BASE = "https://searai.rainnya.asia"

SEARCH_TOOL_NAME = "miao_search"
FETCH_TOOL_NAME = "miao_fetch_url"

# 配置默认值，供运行时读取与 /miao_status 展示共用。
DEFAULT_COUNT = 5
DEFAULT_FETCH_COUNT = 2
DEFAULT_MAX_LENGTH = 3000

# /api/search 的参数 Schema。名称字段与请求参数名保持一致，避免框架重命名。
SEARCH_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": "搜索关键词或自然语言问题。",
        },
        "count": {
            "type": "integer",
            "description": "返回结果条数，1~20。不填则使用插件配置的默认值。",
            "minimum": 1,
            "maximum": 20,
        },
        "engines": {
            "type": "array",
            "items": {"type": "string"},
            "description": "指定搜索引擎，例如 [\"google\", \"bing\"]。不填则由服务端决定。",
        },
    },
    "required": ["query"],
}

# /api/fetch 的参数 Schema。
FETCH_TOOL_PARAMETERS: dict[str, Any] = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "需要精读的网页完整链接，必须以 http:// 或 https:// 开头。",
        },
        "max_length": {
            "type": "integer",
            "description": "返回正文的最大字符数，500~20000。不填则使用插件配置的默认值。",
            "minimum": 500,
            "maximum": 20000,
        },
    },
    "required": ["url"],
}


class MiaoSearchError(Exception):
    """Miao-Search API 调用失败。"""


@dataclass
class SearchItem:
    """单条搜索结果。"""

    title: str = ""
    url: str = ""
    snippet: str = ""
    engine: str = ""
    score: float | None = None


@dataclass
class SearchResponse:
    """搜索接口的结构化返回。"""

    query: str = ""
    total: int = 0
    cost_ms: int | None = None
    results: list[SearchItem] = field(default_factory=list)


@dataclass
class FetchResponse:
    """正文提取接口的结构化返回。"""

    url: str = ""
    title: str = ""
    markdown: str = ""
    length: int = 0
    cached: bool = False
    strategy: str = ""


class MiaoSearchClient:
    """Miao-Search HTTP 客户端。

    鉴权统一使用 ``Authorization: Bearer <token>``，该方式对文档中的
    ``/api/search``、``/api/fetch`` 与 Tavily 兼容的 ``/v1/search`` 均有效。

    Attributes:
        api_base: 服务地址，例如 https://searai.rainnya.asia。
        api_key: 访问令牌。
        timeout: 单次请求超时时间（秒）。
        max_retries: 网络错误或 5xx 错误时的重试次数。
    """

    def __init__(
        self,
        api_base: str,
        api_key: str,
        timeout: int = 30,
        max_retries: int = 1,
    ) -> None:
        self.api_base = (api_base or DEFAULT_API_BASE).strip().rstrip("/")
        self.api_key = (api_key or "").strip()
        self.timeout = max(5, int(timeout))
        self.max_retries = max(0, min(5, int(max_retries)))
        self._session: aiohttp.ClientSession | None = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """获取或懒加载 aiohttp 会话。"""
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=self.timeout),
            )
        return self._session

    async def close(self) -> None:
        """关闭底层 HTTP 会话，释放连接池。"""
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    def _check_ready(self) -> None:
        """校验必要配置，未配置时提前失败。

        Raises:
            MiaoSearchError: API Key 或 API 地址为空。
        """
        if not self.api_key:
            raise MiaoSearchError(
                "未配置 API Key，请在插件配置中填写访问令牌（形如 sk-miao-xxxxxxxx）。"
            )
        if not self.api_base.startswith(("http://", "https://")):
            raise MiaoSearchError(
                f"API 地址无效：{self.api_base}，需要以 http:// 或 https:// 开头。"
            )

    @staticmethod
    def _mask(key: str) -> str:
        """隐藏令牌中间部分，避免日志泄露。

        Args:
            key: 原始令牌。

        Returns:
            掩码后的令牌。
        """
        if len(key) <= 12:
            return "***"
        return f"{key[:9]}...{key[-4:]}"

    async def _request(
        self,
        path: str,
        params: dict[str, Any],
        *,
        allow_auth_fallback: bool = True,
    ) -> dict[str, Any]:
        """发起 GET 请求并返回解析后的 JSON。

        Args:
            path: 接口路径，例如 /api/search。
            params: 查询参数。
            allow_auth_fallback: 首次收到 401 时，是否允许改用 x-api-key 请求头重试一次。

        Returns:
            服务端返回的 JSON 对象。

        Raises:
            MiaoSearchError: 配置缺失、网络异常、非 2xx 响应或响应不是合法 JSON。
        """
        self._check_ready()
        url = f"{self.api_base}{path}"
        # 文档说明 Authorization: Bearer 与 x-api-key 均可，两个都带上以兼容不同部署。
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "x-api-key": self.api_key,
            "Accept": "application/json",
        }

        last_error: str = ""
        auth_retried = False
        attempt = 0
        while attempt <= self.max_retries:
            retry_with_x_api_key = False
            try:
                session = await self._get_session()
                async with session.get(url, params=params, headers=headers) as resp:
                    text = await resp.text()
                    if 200 <= resp.status < 300:
                        try:
                            data = json.loads(text)
                        except ValueError as exc:
                            raise MiaoSearchError(
                                f"接口返回的不是合法 JSON（HTTP {resp.status}）：{text[:200]}"
                            ) from exc
                        if not isinstance(data, dict):
                            raise MiaoSearchError("接口返回的 JSON 结构不是对象。")
                        return data

                    last_error = self._describe_http_error(resp.status, text)
                    if resp.status == 401 and allow_auth_fallback and not auth_retried:
                        # 部分部署只识别 x-api-key；此时 Bearer 会被判为无效令牌。
                        retry_with_x_api_key = True
                    elif resp.status < 500:
                        # 4xx 属于请求本身的问题，重试没有意义，直接报错。
                        raise MiaoSearchError(last_error)
            except MiaoSearchError:
                raise
            except asyncio.TimeoutError:
                last_error = f"请求超时（{self.timeout} 秒）"
            except aiohttp.ClientError as exc:
                last_error = f"网络请求失败：{type(exc).__name__}: {exc}"
            except OSError as exc:
                last_error = f"网络不可达：{exc}"

            if retry_with_x_api_key:
                # 鉴权回落不占用配置的重试次数，否则 max_retries=0 时无法生效。
                auth_retried = True
                headers = {"x-api-key": self.api_key, "Accept": "application/json"}
                logger.debug(
                    "[%s] %s 返回 401，改用 x-api-key 重试一次。",
                    PLUGIN_NAME,
                    path,
                )
                continue

            attempt += 1
            if attempt <= self.max_retries:
                logger.debug(
                    "[%s] %s 第 %d 次请求失败，准备重试：%s",
                    PLUGIN_NAME,
                    path,
                    attempt,
                    last_error,
                )
                await asyncio.sleep(1.0 * attempt)

        detail = last_error or "请求未获得有效响应"
        raise MiaoSearchError(
            f"{detail}。请检查 API 地址（{self.api_base}）与服务器网络连通性。"
        )

    @staticmethod
    def _describe_http_error(status: int, text: str) -> str:
        """把 HTTP 状态码翻译成可读的中文错误信息。

        Args:
            status: HTTP 状态码。
            text: 响应体原文。

        Returns:
            面向用户的中文错误描述。
        """
        known = {
            400: "请求参数非法（INVALID_PARAM），请检查搜索关键词或链接格式。",
            401: "鉴权失败（UNAUTHORIZED），API Key 缺失、格式错误或已失效，请在插件配置中重新填写。",
            403: "令牌被停用或配额已耗尽（TOKEN_DISABLED / QUOTA_EXCEEDED），请前往服务控制台检查令牌状态与配额。",
            429: "触发限流（RATE_LIMIT_EXCEEDED），当前令牌的 QPS 已达上限，请稍后重试。",
            502: "上游目标站超时或搜索引擎维护中（UPSTREAM_TIMEOUT），请稍后重试。",
            504: "上游目标站超时（UPSTREAM_TIMEOUT），请稍后重试。",
        }
        message = known.get(status, f"接口返回 HTTP {status}。")
        code = ""
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                raw_code = payload.get("code") or payload.get("error")
                if isinstance(raw_code, dict):
                    code = str(raw_code.get("code") or raw_code.get("message") or "")
                elif raw_code:
                    code = str(raw_code)
                if not code and payload.get("message"):
                    code = str(payload["message"])
        except ValueError:
            code = text[:200].strip()
        return f"{message}（服务端信息：{code}）" if code else message

    async def search(
        self,
        query: str,
        count: int = 10,
        engines: list[str] | None = None,
    ) -> SearchResponse:
        """调用 /api/search 进行极速目录检索。

        Args:
            query: 搜索关键词。
            count: 返回结果条数，服务端限制 1~20。
            engines: 指定搜索引擎列表，为空则由服务端决定。

        Returns:
            结构化的搜索结果。

        Raises:
            MiaoSearchError: 请求失败或服务端返回 success=false。
        """
        query = (query or "").strip()
        if not query:
            raise MiaoSearchError("搜索关键词不能为空。")

        params: dict[str, Any] = {
            "q": query,
            "count": max(1, min(20, int(count))),
        }
        if engines:
            engines_str = ",".join(str(e).strip() for e in engines if str(e).strip())
            if engines_str:
                params["engines"] = engines_str

        data = await self._request("/api/search", params)
        if data.get("success") is False:
            raise MiaoSearchError(
                self._describe_http_error(200, json.dumps(data, ensure_ascii=False))
            )

        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        raw_results = payload.get("results")
        results: list[SearchItem] = []
        if isinstance(raw_results, list):
            for item in raw_results:
                if not isinstance(item, dict):
                    continue
                score = item.get("score")
                results.append(
                    SearchItem(
                        title=str(item.get("title") or "").strip(),
                        url=str(item.get("url") or "").strip(),
                        snippet=str(item.get("snippet") or "").strip(),
                        engine=str(item.get("engine") or "").strip(),
                        score=float(score) if isinstance(score, (int, float)) else None,
                    )
                )

        total = payload.get("total")
        cost_ms = data.get("cost_ms")
        return SearchResponse(
            query=str(payload.get("query") or query),
            total=int(total) if isinstance(total, (int, float)) else len(results),
            cost_ms=int(cost_ms) if isinstance(cost_ms, (int, float)) else None,
            results=results,
        )

    async def fetch(self, url: str, max_length: int = 3000) -> FetchResponse:
        """调用 /api/fetch 抓取并提取网页正文。

        Args:
            url: 目标网页完整链接。
            max_length: 返回正文的截断上限（字符）。

        Returns:
            结构化的正文提取结果。

        Raises:
            MiaoSearchError: 链接非法、请求失败或服务端返回 success=false。
        """
        url = (url or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise MiaoSearchError(
                f"链接无效：{url or '(空)'}，需要是完整的 http:// 或 https:// 链接。"
            )

        params: dict[str, Any] = {
            "url": url,
            "max_length": max(500, min(20000, int(max_length))),
        }
        data = await self._request("/api/fetch", params)
        if data.get("success") is False:
            raise MiaoSearchError(
                self._describe_http_error(200, json.dumps(data, ensure_ascii=False))
            )

        payload = data.get("data") if isinstance(data.get("data"), dict) else {}
        markdown = str(payload.get("markdown") or "")
        length = payload.get("length")
        return FetchResponse(
            url=str(payload.get("url") or url),
            title=str(payload.get("title") or "").strip(),
            markdown=markdown,
            length=int(length)
            if isinstance(length, (int, float))
            else len(markdown),
            cached=bool(payload.get("cached")),
            strategy=str(payload.get("strategy") or ""),
        )


def _format_search_text(resp: SearchResponse, with_links: bool = True) -> str:
    """把搜索结果格式化成便于大模型阅读的纯文本。

    Args:
        resp: 搜索结果。
        with_links: 是否附带来源链接。

    Returns:
        格式化后的文本；没有结果时返回提示语。
    """
    if not resp.results:
        return f"未搜索到与「{resp.query}」相关的结果。"

    lines = [f"关键词：{resp.query}，共 {resp.total} 条结果。"]
    for index, item in enumerate(resp.results, start=1):
        lines.append(f"{index}. {item.title or '(无标题)'}")
        if with_links and item.url:
            lines.append(f"   链接：{item.url}")
        if item.snippet:
            lines.append(f"   摘要：{item.snippet}")
        if item.engine:
            lines.append(f"   来源引擎：{item.engine}")
    return "\n".join(lines)


def _format_fetch_text(resp: FetchResponse) -> str:
    """把正文提取结果格式化成便于大模型阅读的纯文本。

    Args:
        resp: 正文提取结果。

    Returns:
        格式化后的文本。
    """
    header = f"网页标题：{resp.title or '(无标题)'}"
    if resp.cached:
        header += "（命中缓存）"
    return f"{header}\n正文：\n{resp.markdown or '(正文为空)'}"


@pydantic_dataclass
class MiaoSearchTool(FunctionTool[AstrAgentContext]):
    """LLM 搜索工具：调用 Miao-Search 极速检索或一步直达检索。

    必须以 pydantic dataclass 方式重新装饰：FunctionTool 自身是 pydantic
    dataclass，普通子类会继承其校验器并把父类字段全部视为必填。
    """

    name: str = SEARCH_TOOL_NAME
    description: str = (
        "联网搜索工具。当用户询问实时资讯、近期事件、天气、新闻、价格、"
        "你不确定的或训练数据之外的信息时，调用本工具获取网页搜索结果。"
        "输入搜索关键词或问题，返回标题、链接与摘要。"
    )
    parameters: dict = Field(default_factory=lambda: SEARCH_TOOL_PARAMETERS)

    plugin: Any = None

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        """执行搜索。

        Args:
            context: Agent 运行上下文，用于读取事件信息。
            **kwargs: query / count / engines。

        Returns:
            搜索结果文本，或可读的错误说明。
        """
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "错误：缺少搜索关键词 query。"

        count = kwargs.get("count")
        engines = kwargs.get("engines")
        if isinstance(engines, str):
            engines = [e.strip() for e in engines.split(",") if e.strip()]
        if not isinstance(engines, list):
            engines = None

        return await self.plugin.run_search(
            query=query,
            count=count,
            engines=engines,
            mode_override=None,
        )


@pydantic_dataclass
class MiaoFetchTool(FunctionTool[AstrAgentContext]):
    """LLM 正文精读工具：抓取指定链接的 Markdown 正文。"""

    name: str = FETCH_TOOL_NAME
    description: str = (
        "网页正文精读工具。当用户提供链接并要求总结、翻译、提取或回答与该网页有关的问题时，"
        "调用本工具抓取该网页的正文（已去除广告、导航与脚本，输出纯 Markdown）。"
    )
    parameters: dict = Field(default_factory=lambda: FETCH_TOOL_PARAMETERS)

    plugin: Any = None

    async def call(
        self,
        context: ContextWrapper[AstrAgentContext],
        **kwargs: Any,
    ) -> ToolExecResult:
        """执行正文抓取。

        Args:
            context: Agent 运行上下文，用于读取事件信息。
            **kwargs: url / max_length。

        Returns:
            网页正文文本，或可读的错误说明。
        """
        url = str(kwargs.get("url") or "").strip()
        if not url:
            return "错误：缺少网页链接 url。"

        max_length = kwargs.get("max_length")
        return await self.plugin.run_fetch(url=url, max_length=max_length)


class MiaoSearchPlugin(Star):
    """Miao-Search 联网搜索插件主体。"""

    def __init__(self, context: Context, config: AstrBotConfig | None = None) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = dict(config) if config else {}
        self.client = self._build_client()

        tools: list[FunctionTool] = []
        mode = self._mode()
        if self._tool_enabled("enable_search_tool") and mode != "fetch":
            tools.append(MiaoSearchTool(plugin=self))
        if self._tool_enabled("enable_fetch_tool") and mode != "quick":
            tools.append(MiaoFetchTool(plugin=self))
        if tools:
            self.context.add_llm_tools(*tools)
            logger.info(
                "[%s] 已注册 LLM 工具：%s",
                PLUGIN_NAME,
                "、".join(t.name for t in tools),
            )

        self.context.register_web_api(
            f"/{PLUGIN_NAME}/test",
            self.api_test,
            ["GET"],
            "测试 Miao-Search 连通性",
        )
        self.context.register_web_api(
            f"/{PLUGIN_NAME}/status",
            self.api_status,
            ["GET"],
            "读取 Miao-Search 插件状态",
        )
        logger.info(
            "[%s] 插件已加载，API 地址 %s，搜索模式 %s",
            PLUGIN_NAME,
            self.client.api_base,
            mode,
        )

    # ------------------------------------------------------------------
    # 配置读取
    # ------------------------------------------------------------------

    def _build_client(self) -> MiaoSearchClient:
        """依据当前配置创建 API 客户端。"""
        return MiaoSearchClient(
            api_base=str(self.config.get("api_base") or DEFAULT_API_BASE),
            api_key=str(self.config.get("api_key") or ""),
            timeout=self._int_config("timeout", 30, 5, 120),
            max_retries=self._int_config("max_retries", 1, 0, 5),
        )

    def _int_config(self, key: str, default: int, low: int, high: int) -> int:
        """安全读取整型配置并夹取到合法区间。

        Args:
            key: 配置项名称。
            default: 缺省值。
            low: 允许的最小值。
            high: 允许的最大值。

        Returns:
            夹取后的整数。
        """
        try:
            value = int(self.config.get(key, default))
        except (TypeError, ValueError):
            return default
        return max(low, min(high, value))

    def _bool_config(self, key: str, default: bool = True) -> bool:
        """安全读取布尔配置。

        Args:
            key: 配置项名称。
            default: 缺省值。

        Returns:
            布尔值。
        """
        return self._bool_config_from(self.config.get(key), default)

    def _mode(self) -> str:
        """读取并校验搜索模式。"""
        mode = str(self.config.get("search_mode") or "deep").strip().lower()
        return mode if mode in ("quick", "deep", "fetch") else "deep"

    def _tool_enabled(self, key: str) -> bool:
        """读取 llm_tool 分组下的开关。

        Args:
            key: 小组件内的配置名。

        Returns:
            是否启用。
        """
        group = self.config.get("llm_tool")
        if isinstance(group, dict) and key in group:
            return self._bool_config_from(group.get(key), True)
        return True

    @staticmethod
    def _bool_config_from(value: Any, default: bool = True) -> bool:
        """把任意配置值转成布尔值。

        Args:
            value: 原始配置值。
            default: value 为 None 时的缺省值。

        Returns:
            布尔值。
        """
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() not in ("false", "0", "no", "off", "")
        return bool(value)

    def _command_enabled(self) -> bool:
        """指令功能是否启用。"""
        group = self.config.get("command")
        if isinstance(group, dict) and "enable_command" in group:
            return self._bool_config_from(group.get("enable_command"), True)
        return True

    def _admin_only(self) -> bool:
        """指令是否仅管理员可用。"""
        group = self.config.get("command")
        if isinstance(group, dict) and "admin_only" in group:
            return self._bool_config_from(group.get("admin_only"), False)
        return False

    def _reload_client(self) -> None:
        """在配置变更后重建 HTTP 客户端。"""
        self.client = self._build_client()

    # ------------------------------------------------------------------
    # 业务方法（供 LLM 工具与指令共用）
    # ------------------------------------------------------------------

    async def run_search(
        self,
        query: str,
        count: Any = None,
        engines: list[str] | None = None,
        mode_override: str | None = None,
    ) -> str:
        """执行一次搜索，返回可直接交给大模型或用户的文本。

        Args:
            query: 搜索关键词。
            count: 结果条数，None 时使用配置默认值。
            engines: 指定搜索引擎列表。
            mode_override: 覆盖配置中的搜索模式（quick / deep）。

        Returns:
            搜索结果文本；失败时返回以「搜索失败：」开头的说明。
        """
        if not self._bool_config("enable", True):
            return "搜索失败：插件已在配置中停用。"

        mode = mode_override or self._mode()
        if mode == "fetch":
            mode = "quick"

        try:
            count_value = (
                int(count)
                if count is not None
                else self._int_config("default_count", DEFAULT_COUNT, 1, 20)
            )
        except (TypeError, ValueError):
            count_value = self._int_config("default_count", DEFAULT_COUNT, 1, 20)
        count_value = max(1, min(20, count_value))

        try:
            if mode == "deep":
                text = await self._search_and_fetch(query, count_value, engines)
            else:
                resp = await self.client.search(query, count_value, engines)
                text = _format_search_text(resp, self._bool_config("show_source_links", True))
                logger.info(
                    "[%s] 搜索完成：%s，返回 %d 条，耗时 %s ms",
                    PLUGIN_NAME,
                    query,
                    len(resp.results),
                    resp.cost_ms,
                )
            return text
        except MiaoSearchError as exc:
            logger.warning("[%s] 搜索失败：%s", PLUGIN_NAME, exc)
            return f"搜索失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底，避免插件崩溃
            logger.error("[%s] 搜索出现未预期错误：%s", PLUGIN_NAME, exc, exc_info=True)
            return f"搜索失败：插件内部错误 {type(exc).__name__}: {exc}"

    async def _search_and_fetch(
        self,
        query: str,
        count: int,
        engines: list[str] | None,
    ) -> str:
        """一步直达：先搜索，再并发抓取前 N 条网页正文。

        Args:
            query: 搜索关键词。
            count: 搜索结果条数。
            engines: 指定搜索引擎列表。

        Returns:
            汇总了搜索结果与正文的文本。
        """
        resp = await self.client.search(query, count, engines)
        text = _format_search_text(resp, self._bool_config("show_source_links", True))
        if not resp.results:
            return text

        fetch_count = self._int_config("max_fetch_count", DEFAULT_FETCH_COUNT, 1, 5)
        max_length = self._int_config(
            "max_content_length", DEFAULT_MAX_LENGTH, 500, 20000
        )
        targets = [item for item in resp.results if item.url][:fetch_count]
        if not targets:
            return text

        async def fetch_one(item: SearchItem) -> tuple[SearchItem, str]:
            try:
                page = await self.client.fetch(item.url, max_length)
                return item, _format_fetch_text(page)
            except MiaoSearchError as exc:
                logger.debug("[%s] 正文抓取失败 %s：%s", PLUGIN_NAME, item.url, exc)
                return item, f"（抓取失败：{exc}）"

        fetched = await asyncio.gather(
            *(fetch_one(item) for item in targets),
            return_exceptions=False,
        )

        blocks = [text, "", f"以下是前 {len(fetched)} 条网页的正文精读结果："]
        for index, (item, body) in enumerate(fetched, start=1):
            blocks.append(f"\n===== 网页 {index}：{item.title or item.url} =====")
            blocks.append(f"来源：{item.url}")
            blocks.append(body)
        logger.info(
            "[%s] 一步直达完成：%s，抓取 %d 篇正文",
            PLUGIN_NAME,
            query,
            len(fetched),
        )
        return "\n".join(blocks)

    async def run_fetch(self, url: str, max_length: Any = None) -> str:
        """抓取并返回单个网页的正文。

        Args:
            url: 目标网页链接。
            max_length: 正文长度上限，None 时使用配置默认值。

        Returns:
            网页正文文本；失败时返回以「正文提取失败：」开头的说明。
        """
        if not self._bool_config("enable", True):
            return "正文提取失败：插件已在配置中停用。"

        try:
            length_value = (
                int(max_length)
                if max_length is not None
                else self._int_config(
                    "max_content_length", DEFAULT_MAX_LENGTH, 500, 20000
                )
            )
        except (TypeError, ValueError):
            length_value = self._int_config(
                "max_content_length", DEFAULT_MAX_LENGTH, 500, 20000
            )

        try:
            resp = await self.client.fetch(url, length_value)
            logger.info(
                "[%s] 正文提取完成：%s，长度 %d，策略 %s，缓存 %s",
                PLUGIN_NAME,
                resp.url,
                resp.length,
                resp.strategy or "unknown",
                resp.cached,
            )
            return _format_fetch_text(resp)
        except MiaoSearchError as exc:
            logger.warning("[%s] 正文提取失败：%s", PLUGIN_NAME, exc)
            return f"正文提取失败：{exc}"
        except Exception as exc:  # noqa: BLE001 - 兜底，避免插件崩溃
            logger.error("[%s] 正文提取出现未预期错误：%s", PLUGIN_NAME, exc, exc_info=True)
            return f"正文提取失败：插件内部错误 {type(exc).__name__}: {exc}"

    # ------------------------------------------------------------------
    # 指令
    # ------------------------------------------------------------------

    @filter.command("search", alias={"搜索"})
    async def search_command(self, event: AstrMessageEvent):
        """联网搜索。用法：/search 关键词。"""
        if not self._command_enabled():
            yield event.plain_result("搜索指令已在插件配置中关闭。")
            return

        event.stop_event()
        denied = self._check_admin(event)
        if denied:
            yield event.plain_result(denied)
            return

        query = self._command_arg(event, "search", "搜索")
        if not query:
            yield event.plain_result(
                "请输入搜索关键词，例如：/search AstrBot 是什么\n"
                "可选：/search quick 关键词（仅极速检索）"
            )
            return

        mode_override = None
        parts = query.split(maxsplit=1)
        if len(parts) == 2 and parts[0].lower() in ("quick", "deep"):
            mode_override = parts[0].lower()
            query = parts[1].strip()
        if not query:
            yield event.plain_result("关键词不能为空。")
            return

        yield event.plain_result(f"正在搜索「{query}」…")
        text = await self.run_search(query=query, mode_override=mode_override)
        yield event.plain_result(text)

    @filter.command("fetch", alias={"网页精读"})
    async def fetch_command(self, event: AstrMessageEvent):
        """抓取网页正文。用法：/fetch https://example.com。"""
        if not self._command_enabled():
            yield event.plain_result("正文精读指令已在插件配置中关闭。")
            return

        event.stop_event()
        denied = self._check_admin(event)
        if denied:
            yield event.plain_result(denied)
            return

        url = self._extract_url(self._command_arg(event, "fetch", "网页精读"))
        if not url:
            yield event.plain_result(
                "请输入要精读的网页链接，例如：/fetch https://example.com"
            )
            return

        yield event.plain_result("正在抓取网页正文…")
        text = await self.run_fetch(url=url)
        yield event.plain_result(text)

    @filter.command("miao_status", alias={"搜索状态"})
    async def status_command(self, event: AstrMessageEvent):
        """查看 Miao-Search 插件的当前配置状态。"""
        event.stop_event()
        denied = self._check_admin(event)
        if denied:
            yield event.plain_result(denied)
            return

        info = self.build_status_info()
        yield event.plain_result(
            "\n".join(
                [
                    "Miao-Search 插件状态",
                    f"启用：{'是' if info['enabled'] else '否'}",
                    f"API 地址：{info['api_base']}",
                    f"API Key：{info['api_key_masked'] or '未配置'}",
                    f"搜索模式：{info['mode']}",
                    f"默认条数：{info['default_count']}",
                    f"一步直达抓取篇数：{info['max_fetch_count']}",
                    f"搜索工具：{info['search_tool'] or '未注册'}",
                    f"精读工具：{info['fetch_tool'] or '未注册'}",
                    f"指令：{'已启用' if info['command_enabled'] else '已关闭'}"
                    f"（{'仅管理员' if info['admin_only'] else '所有人可用'}）",
                ]
            )
        )

    def build_status_info(self) -> dict[str, Any]:
        """汇总当前运行状态，供指令与 Web API 共用。

        Returns:
            包含启用状态、API 配置、模式与工具开关的字典。令牌已做掩码处理。
        """
        key = self.client.api_key
        return {
            "enabled": self._bool_config("enable", True),
            "api_base": self.client.api_base,
            "api_key_configured": bool(key),
            "api_key_masked": MiaoSearchClient._mask(key) if key else "",
            "mode": self._mode(),
            "default_count": self._int_config("default_count", DEFAULT_COUNT, 1, 20),
            "max_fetch_count": self._int_config(
                "max_fetch_count", DEFAULT_FETCH_COUNT, 1, 5
            ),
            "search_tool": SEARCH_TOOL_NAME
            if self._tool_enabled("enable_search_tool") and self._mode() != "fetch"
            else "",
            "fetch_tool": FETCH_TOOL_NAME
            if self._tool_enabled("enable_fetch_tool") and self._mode() != "quick"
            else "",
            "command_enabled": self._command_enabled(),
            "admin_only": self._admin_only(),
        }

    # ------------------------------------------------------------------
    # 指令辅助
    # ------------------------------------------------------------------

    def _check_admin(self, event: AstrMessageEvent) -> str | None:
        """按配置校验管理员权限。

        Args:
            event: 消息事件。

        Returns:
            无权限时返回提示语，有权限时返回 None。
        """
        if not self._admin_only():
            return None
        try:
            if event.is_admin():
                return None
        except Exception:  # noqa: BLE001 - 个别平台适配器可能不支持权限查询
            return "无法确认你的管理员身份，已按配置拒绝执行。"
        return "该指令仅限管理员使用。"

    @staticmethod
    def _command_arg(event: AstrMessageEvent, *commands: str) -> str:
        """从原始消息中截取指令后的参数。

        Args:
            event: 消息事件。
            *commands: 该指令的所有名称（不含前缀斜杠），用于兼容别名。例如 "search", "搜索"。

        Returns:
            去掉指令名后的参数文本。
        """
        message = (event.message_str or "").strip()
        for command in commands:
            for prefix in (f"/{command}", f"／{command}"):
                if message.startswith(prefix):
                    return message[len(prefix) :].strip()
        return message

    @staticmethod
    def _extract_url(text: str) -> str:
        """从指令参数中提取第一个 http(s) 链接。

        用户常把链接和说明混在一起发送（例如「/fetch https://a.com 帮我总结 一下」），
        这里按空白切分并剥离常见中英文标点，避免把标点带进 URL。

        Args:
            text: 指令后的原始参数文本。

        Returns:
            提取到的链接；没有链接时返回空字符串。
        """
        for token in (text or "").split():
            candidate = token.strip().strip("<>\"'“”‘’《》()（）[]【】,，。;；!！?？、")
            if candidate.startswith(("http://", "https://")):
                return candidate
        return ""

    # ------------------------------------------------------------------
    # Web API
    # ------------------------------------------------------------------

    async def api_status(self):
        """WebUI 状态接口：GET /api/v1/plugins/extensions/<plugin>/status。

        Returns:
            JSON 响应，只包含配置与开关状态，不发起任何搜索请求。
        """
        return json_response(self.build_status_info())

    async def api_test(self):
        """WebUI 连通性测试接口：GET /api/v1/plugins/extensions/<plugin>/test。

        Query 参数：
            q: 测试用的关键词，或 fetch 模式下的网页链接，默认 AstrBot。
            mode: quick / deep / fetch，默认 quick。

        Returns:
            JSON 响应，包含配置信息、是否成功以及截断后的测试结果。
        """
        if not self._bool_config("enable", True):
            return error_response("插件已在配置中停用", status_code=400)

        mode = request.query.get("mode", "quick")
        if mode not in ("quick", "deep", "fetch"):
            return error_response("mode 只能是 quick、deep 或 fetch", status_code=400)

        query = (request.query.get("q") or "AstrBot").strip()
        if mode == "fetch":
            result = await self.run_fetch(url=query)
        else:
            result = await self.run_search(query=query, count=3, mode_override=mode)

        return json_response(
            {
                "api_base": self.client.api_base,
                "api_key_configured": bool(self.client.api_key),
                "api_key_masked": MiaoSearchClient._mask(self.client.api_key)
                if self.client.api_key
                else "",
                "mode": mode,
                "ok": not result.startswith(("搜索失败", "正文提取失败")),
                "result": result[:4000],
            }
        )

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    async def terminate(self) -> None:
        """插件卸载/停用时关闭 HTTP 会话。"""
        await self.client.close()
        logger.info("[%s] 插件已卸载，HTTP 会话已关闭。", PLUGIN_NAME)
