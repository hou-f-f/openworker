"""[中文] 与提供商解耦的模型访问层。

运行时绝不会直接导入提供商的 SDK —— 它始终与 `ProviderClient` 进行交互。
具体实现包括：`OpenAIResponsesProvider`（通过 `/v1/responses` 的原生 OpenAI 接口）、
`OpenAIProvider`（Chat Completions 兼容世界接口），以及原生 Anthropic/Gemini/Bedrock/Vertex
提供商，所有这些均由注册表/路由器进行选择。

Provider-agnostic model access layer.

The runtime never imports a provider SDK directly — it talks to a `ProviderClient`.
Implementations: `OpenAIResponsesProvider` (native OpenAI via `/v1/responses`),
`OpenAIProvider` (Chat Completions — the compat world), and the native
Anthropic/Gemini/Bedrock/Vertex providers, all selected by the registry/router.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class ToolCall:
    """[中文] 模型请求的单次工具调用，包含已解析的参数。
    A single tool call requested by the model, with parsed arguments."""

    id: str
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass
class TokenUsage:
    """[中文] 单次模型往返的规范化 Token 计数。

    `input` 仅统计全新的（未缓存的）提示词 Token；缓存的提示词 Token 会拆分为
    `cache_read`/`cache_write`。未报告缓存拆分的提供商（Ollama、多数兼容供应商）
    会将缓存字段保留为 0。在供应商将思考计入输出费用的情况下（如 Gemini），`output` 包含思考 Token。

    Normalized token counts for one model round-trip.

    `input` counts only fresh (uncached) prompt tokens; cached prompt tokens are
    split into `cache_read`/`cache_write`. Providers that don't report a cache
    split (Ollama, most compat vendors) leave the cache fields at 0. `output`
    includes thinking tokens where the vendor bills them as output (Gemini).
    """

    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0

    @property
    def context_tokens(self) -> int:
        """[中文] 提示词端总数 —— 实际占据上下文窗口的 Token 数量。
        Prompt-side total — what actually occupied the context window."""
        return self.input + self.cache_read + self.cache_write

    def as_dict(self) -> dict[str, int]:
        return {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_write": self.cache_write,
        }


@dataclass
class AssistantTurn:
    """[中文] 单次助手回复：自由文本和/或一组工具调用。
    One assistant response: free text and/or a set of tool calls."""

    text: Optional[str] = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: Optional[str] = None
    raw: Any = field(default=None, repr=False, compare=False)
    # [中文] 模型的思考文本（DeepSeek 的 reasoning_content、Gemini 的思考摘要等）。
    # 仅用于展示：作为 `reasoning` 侧车附加在助手消息上并持久化，显示在 GUI 中，
    # 但在每次向提供商发起调用前会被剔除 —— 绝不作为上下文回放。
    # The model's thinking text (DeepSeek reasoning_content, Gemini thought summaries, …).
    # Display-only: persisted on the assistant message as the `reasoning` sidecar and shown
    # in the GUI, but stripped before every provider call — never replayed as context.
    reasoning: Optional[str] = None
    # [中文] 需要持久化在标准助手消息上的提供商私有侧车数据
    # （以下划线为前缀的键，例如 `_gemini` 思考签名）。约定：归属的提供商在转换历史记录时使用自己的键；
    # 其他所有提供商在发送连线请求前必须剔除或忽略外来的下划线键。
    # Provider-private sidecars to persist on the canonical assistant message
    # (underscore-prefixed keys, e.g. `_gemini` thought signatures). Contract: the
    # owning provider consumes its own key when converting history; every other
    # provider must strip or ignore foreign underscore keys before its wire call.
    extras: dict[str, Any] = field(default_factory=dict)
    # [中文] 本次往返的 Token 计数，跨提供商规范化。当后端未报告用量时为 None（某些兼容服务器）—— 绝不主观猜测。
    # Token counts for this round-trip, normalized across providers. None when the
    # backend didn't report usage (some compat servers) — never guessed.
    usage: Optional[TokenUsage] = None
    # [中文] 提供商在本次请求中实际发送的每条回复输出 Token 上限
    # （`max_tokens` / `max_completion_tokens` / `max_output_tokens`），
    # 经过提供商端的任意调整（Anthropic 预算下限、OpenAI 字段重命名）。
    # 当提供商交由服务器决定时为 None。作为 `max_output_tokens` 侧车持久化在助手消息上，
    # 以便运行记录明确其上限（OPE-177）。
    # The per-reply output-token ceiling the provider actually sent on this request
    # (`max_tokens` / `max_completion_tokens` / `max_output_tokens`), after any
    # provider-side adjustment (Anthropic budget floor, OpenAI rename). None when the
    # provider left it to the server. Persisted on the assistant message as the
    # `max_output_tokens` sidecar so a run record states its ceiling (OPE-177).
    output_limit: Optional[int] = None
    # [中文] 本次请求使用的推理力度映射（OPE-176）：
    # {requested, effective, param?, note?} —— 参见 providers/effort.py。
    # 当未配置级别或提供商没有此类调节旋钮时为 None。作为 `reasoning_effort` 侧车持久化在助手消息上。
    # The reasoning-effort mapping used for this request (OPE-176):
    # {requested, effective, param?, note?} — see providers/effort.py. None when no
    # level was configured or the provider has no such knob. Persisted on the
    # assistant message as the `reasoning_effort` sidecar.
    effort: Optional[dict[str, Any]] = None
    # [中文] 提供此回复的上游主机（当路由器指明时，例如 OpenRouter 的顶层 `provider`，如 "Together"）。
    # 作为 `served_by` 侧车持久化，以便固定到某台主机的运行可以证明每次回复都遵循了该固定策略。
    # The upstream host that served this reply, when a router names it (OpenRouter's
    # top-level `provider`, e.g. "Together"). Persisted as the `served_by` sidecar so a
    # run pinned to one host can prove the pin held on every reply.
    served_by: Optional[str] = None

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


@dataclass(frozen=True)
class ModelCapabilities:
    """[中文] 特定模型/提供商支持的能力；用于优雅降级。
    What a given model/provider can do; used for graceful degradation."""

    tools: bool = True
    vision: bool = False
    # [中文] 原生 PDF 摄入（OpenAI `file` 部分 / Anthropic document / Gemini inline_data）。
    # 不具备此特性的模型采用本地回退方案：文本提取或页面渲染为图像（pdf_support.py）。
    # Native PDF ingestion (OpenAI `file` part / Anthropic document / Gemini inline_data).
    # Models without it get a local fallback: text extraction or page images (pdf_support.py).
    pdf: bool = False
    parallel_tool_calls: bool = True
    streaming: bool = True


@dataclass
class StreamChunk:
    """[中文] 单个流式传输片段：文本和/或思考增量，和/或（最终的）完整轮次。
    One streamed piece: a text and/or reasoning delta, and/or (final) the full turn."""

    text_delta: Optional[str] = None
    reasoning_delta: Optional[str] = None
    turn: Optional[AssistantTurn] = None


class ProviderClient(ABC):
    """[中文] 单次调用的、提供商无关的模型补全接口。

    刻意设计为阻塞式（TurnEngine 在 `asyncio.to_thread` 中包装它），
    且刻意没有设计 `max_turns` 循环 —— 由运行时拥有智能体循环的所有权。

    Single-shot, provider-agnostic completion interface.

    Deliberately blocking (the turn engine wraps it in `asyncio.to_thread`) and
    deliberately without a `max_turns` loop — the runtime owns the agent loop.
    """

    @abstractmethod
    def complete(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ) -> AssistantTurn:
        """[中文] 针对给定的消息/工具返回单次助手轮次。
        Return one assistant turn for the given messages/tools."""

    @abstractmethod
    def capabilities(self, model: str) -> ModelCapabilities:
        """[中文] 返回指定模型的能力标志。
        Return capability flags for the given model."""

    def stream(
        self,
        *,
        model: str,
        messages: list[dict[str, Any]],
        tools: Optional[list[dict[str, Any]]] = None,
        **settings: Any,
    ):
        """[中文] 生成 StreamChunk。默认行为：不支持 Token 流式传输 —— 产生包含完整轮次的单个最终 chunk。
        支持流式传输的提供商（如 OpenAIProvider）会覆盖此方法。

        Yield StreamChunks. Default: no token streaming — one final chunk with the
        full turn. Providers that support streaming (OpenAIProvider) override this."""
        yield StreamChunk(
            turn=self.complete(model=model, messages=messages, tools=tools, **settings)
        )
