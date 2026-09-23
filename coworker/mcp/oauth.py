"""[中文] 针对远程 MCP 服务器的浏览器 OAuth 认证（OAuth 2.1 + PKCE + 动态客户端注册 DCR）。

官方 SDK 的 `OAuthClientProvider` 驱动了整套规范流程 —— 受保护资源元数据发现、
DCR、PKCE、令牌静默刷新 —— 作为接入 streamable-HTTP 传输的 httpx auth 认证器。
我们为其提供三个关键集成点：

  - 令牌持久化（token persistence）→ SecretStore（配置档 `mcp-oauth:<server>`；0600 权限文件，
    绝不保存在 mcp.json 配置中，因为后者是纯文本且便于粘贴共享）
  - 重定向（redirect）             → 在系统默认浏览器中打开授权 URL
  - 回调（callback）               → sidecar 的回环接口 `GET /mcp/oauth/callback` 解析一个
    单槽就绪 future（每次仅处理一个交互式登录 —— 该流程由用户驱动，因此并发没有实际意义）

DCR（Dynamic Client Registration）意味着无需预先在任何地方注册 client id/secret ——
没有需要 ocw-connect broker 托管的中介凭证，因此与托管连接器不同，该流程完全在本地完成。
首个支持的服务端：Granola (https://mcp.granola.ai/mcp)。

[English]
Browser OAuth for remote MCP servers (OAuth 2.1 + PKCE + Dynamic Client Registration).

The official SDK's `OAuthClientProvider` drives the whole spec flow — protected-resource
metadata discovery, DCR, PKCE, token refresh — as an httpx auth plugged into the
streamable-HTTP transport. We supply its three integration points:

  - token persistence  → the SecretStore (profile `mcp-oauth:<server>`; 0600 file,
    never the mcp.json config, which is plain text and paste-shareable)
  - redirect           → open the system browser at the authorize URL
  - callback           → the sidecar's loopback `GET /mcp/oauth/callback` resolves a
    single-slot pending future (one interactive sign-in at a time — the flow is
    user-driven, so concurrency is meaningless)

DCR means there is no client id/secret registered anywhere up front — nothing for the
ocw-connect broker to hold, so unlike the managed connectors this flow is fully local.
First server: Granola (https://mcp.granola.ai/mcp).
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import time
from typing import Any, Optional

from mcp.client.auth import OAuthClientProvider, TokenStorage
from mcp.shared.auth import OAuthClientInformationFull, OAuthClientMetadata, OAuthToken

from ..secrets import SecretStore

logger = logging.getLogger(__name__)

PROFILE_PREFIX = "mcp-oauth:"
CALLBACK_PATH = "/mcp/oauth/callback"
# [中文] 等待用户完成浏览器登录的超时时间。 / [English] How long the connect waits for the user to finish the browser sign-in.
FLOW_TIMEOUT_SECONDS = 300

CLIENT_NAME = "OpenWorker"


def redirect_base() -> str:
    """[中文] sidecar 自身的回环源地址 —— DCR 注册的重定向 URI 必须与之一致。 / [English] The sidecar's own loopback origin — the DCR-registered redirect must match it."""
    port = os.environ.get("COWORKER_PORT") or "8765"
    return f"http://127.0.0.1:{port}"


def _profile(name: str) -> str:
    return PROFILE_PREFIX + name


class SecretStoreTokenStorage(TokenStorage):
    """[中文] 基于 SecretStore 实现的 SDK TokenStorage：每个服务器使用一个配置档，
    保存令牌集合与 DCR 颁发的客户端注册信息（可跨多次登录复用）。

    [English]
    SDK TokenStorage over our SecretStore: one profile per server holding the token
    set and the DCR-issued client registration (re-used across sign-ins).
    """

    def __init__(self, server_name: str, secrets: SecretStore) -> None:
        self._name = server_name
        self._secrets = secrets

    def _data(self) -> dict[str, Any]:
        return self._secrets.get(_profile(self._name)) or {}

    def _merge(self, patch: dict[str, Any]) -> None:
        self._secrets.put(_profile(self._name), {**self._data(), **patch})

    async def get_tokens(self) -> Optional[OAuthToken]:
        data = self._data()
        raw = data.get("tokens")
        if not raw:
            return None
        try:
            tok = OAuthToken.model_validate(raw)
        except Exception:
            return None
        # [中文] SDK 缺陷（mcp 1.29）：`_initialize()` 加载了已存储的令牌但从不计算
        # `token_expiry_time`，而 `is_token_valid()` 将 None 过期时间视为永远有效 ——
        # 因此一个已过去一小时的访问令牌会被原样发送，服务器返回 401，然后 SDK 的 401 分支
        # 会直接跳转到完全的重新授权流程，而根本不尝试使用 refresh token。
        # 非交互式上下文必须拒绝打开浏览器，导致每个会话都提示“需要登录”，而显式连接却看似正常（owner-hit 2026-08-21, DLAI Redshift）。
        # 此处在存储层提供应对方案：当存储的令牌超过保存时记录的生命周期时（未知年龄 = 陈旧），
        # 返回不带 access token 的令牌集合 —— 此时 `is_token_valid()` 自然失效，
        # SDK 会首先执行 refresh-token 授权流程，实现静默自我修复（无需弹出浏览器）。
        # [English] SDK flaw (mcp 1.29): `_initialize()` loads stored tokens but never computes
        # `token_expiry_time`, and `is_token_valid()` treats None expiry as valid
        # forever — so an hour-old access token is sent as-is, the server 401s, and
        # the SDK's 401 branch goes straight to FULL re-authorization without trying
        # the refresh token. Non-interactive contexts must refuse the browser, so
        # every session said "sign-in required" while explicit connects appeared to
        # work (owner-hit 2026-08-21, DLAI Redshift). Countermeasure lives here, in
        # storage: when the stored token is past the lifetime we recorded at save
        # time (unknown age = stale), return the token set WITHOUT the access token —
        # `is_token_valid()` then fails on its own terms and the SDK runs the
        # refresh-token grant FIRST, which self-heals silently (no browser).
        if tok.expires_in is not None:
            issued = data.get("tokens_issued_at")
            if isinstance(issued, (int, float)):
                remaining = int(issued + tok.expires_in - time.time())
            else:
                remaining = -1
            tok = tok.model_copy(update={"expires_in": remaining})
            if remaining <= 60 and tok.refresh_token:
                tok = tok.model_copy(update={"access_token": ""})
        return tok

    async def set_tokens(self, tokens: OAuthToken) -> None:
        self._merge(
            {
                "tokens": tokens.model_dump(mode="json", exclude_none=True),
                "tokens_issued_at": int(time.time()),
            }
        )

    async def get_client_info(self) -> Optional[OAuthClientInformationFull]:
        raw = self._data().get("client_info")
        if not raw:
            return None
        try:
            return OAuthClientInformationFull.model_validate(raw)
        except Exception:
            return None

    async def set_client_info(self, info: OAuthClientInformationFull) -> None:
        self._merge({"client_info": info.model_dump(mode="json", exclude_none=True)})


class InteractiveAuthRequired(RuntimeError):
    """[中文] 服务器要求浏览器登录，但当前上下文禁止弹出浏览器。

    交互式 OAuth（浏览器 + 回环等待）是仅属于显式连接操作的特权：触发此异常的后台上下文
    —— 例如引擎 turn 轮次、工具列表枚举 —— 会直接抛出异常，调用方跳过该服务器。
    如果没有此限制，当某个服务器的 refresh token 被第三方提供商废弃时（Atlassian 会激进轮换令牌），
    任何接触到该服务器的代码路径都会强行劫持用户的浏览器 —— owner-hit 2026-07-20：在应用启动时弹出了授权页面。

    [English]
    The server wants a browser sign-in, but this context must not open one.

    Interactive OAuth (browser + loopback wait) is an explicit-connect-only
    privilege: a background context that hit this — an engine turn, a tools
    listing — raises instead, and the caller skips the server. Without this, a
    server whose refresh token the vendor rejected (Atlassian rotates them
    aggressively) would hijack the user's browser from ANY code path that
    touched it — owner-hit 2026-07-20: an authorize page opened at app launch.
    """


def is_auth_required(exc: BaseException) -> bool:
    """[中文] 如果异常树中包含 InteractiveAuthRequired 则返回 True —— SDK 传输层在 anyio 任务组中运行，
    因此它常常被包装在 ExceptionGroup 中（或作为 cause 链接），而非裸露异常。

    [English]
    True if InteractiveAuthRequired is anywhere in the exception tree — the SDK
    transport runs in anyio task groups, so it often arrives wrapped in an
    ExceptionGroup (or chained as a cause) rather than bare.
    """
    if isinstance(exc, InteractiveAuthRequired):
        return True
    for sub in getattr(exc, "exceptions", None) or []:  # ExceptionGroup
        if is_auth_required(sub):
            return True
    cause = exc.__cause__ or exc.__context__
    return is_auth_required(cause) if cause is not None else False


def is_http_auth_error(exc: BaseException) -> bool:
    """[中文] 如果异常树中任何位置包含 HTTP 401/403 则返回 True —— 匿名连接命中了需要凭据的服务器，
    因此修复方式是登录（将条目切换为 `auth: oauth`），而非更改配置。
    与 is_auth_required 具有相同的遍历逻辑：传输层的 task groups 会随意进行包装和异常链级联。

    [English]
    True if an HTTP 401/403 is anywhere in the exception tree — an anonymous
    connect hit a server that wants credentials, so the fix is sign-in (switch
    the entry to `auth: oauth`), not a different config. Same tree walk as
    is_auth_required: the transport's task groups wrap and chain freely.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status in (401, 403):
        return True
    for sub in getattr(exc, "exceptions", None) or []:  # ExceptionGroup
        if is_http_auth_error(sub):
            return True
    cause = exc.__cause__ or exc.__context__
    return is_http_auth_error(cause) if cause is not None else False


# -- [中文] 单槽交互式流程 / [English] single-slot interactive flow --------------------------------
_pending: Optional[asyncio.Future] = None
# [中文] 最近向用户发送的授权 URL —— 通过 REST 暴露，若浏览器弹窗丢失 GUI 可提供“重新打开登录页面”链接。
# [English] The last authorize URL we sent the user to — surfaced over REST so the GUI can offer
# a "reopen sign-in page" link if the browser popup was lost.
last_authorize_url: Optional[str] = None
# [中文] SDK 放入当前授权 URL 中的 `state` 参数。SDK 自身会重新校验返回的 state（mcp.client.auth.oauth2 compare_digest），
# 因此这并不是 CSRF 防护 —— 它是一个回环门控：没有它，任何本地调用方都可以携带虚假 code 请求 /mcp/oauth/callback
# 并消耗掉唯一的 pending future，从而中断用户真正的登录流程（后者随后会发现没有等待中的流程）。
# 在此处匹配 state 会拒绝这些多余的无效回调，让流程继续等待真实的回调到达。
# [English] The `state` the SDK put in the current authorize URL. The SDK itself re-checks the
# returned state (mcp.client.auth.oauth2 compare_digest), so this is NOT the CSRF guard —
# it's a loopback gate: without it any local caller could hit /mcp/oauth/callback with a
# bogus code and consume the single pending future, aborting the user's real sign-in
# (which then finds no pending flow). Matching state here rejects that stray callback and
# leaves the flow waiting for the genuine one.
_expected_state: Optional[str] = None


def _state_from_url(url: str) -> Optional[str]:
    """[中文] 从授权 URL 中提取 `state` 查询参数（若不存在则为 None）。 / [English] Pull the `state` query param out of an authorize URL (None if absent)."""
    from urllib.parse import parse_qs, urlsplit

    values = parse_qs(urlsplit(url).query).get("state")
    return values[0] if values else None


def deliver_callback(code: str, state: Optional[str]) -> bool:
    """[中文] 由回环路由调用。解析等待中的流程；若无等待流程则返回 False。

    如果回调的 `state` 与挂起流程不匹配，则忽略该回调（返回 False）且不消耗 pending future，
    因此外部杂乱/伪造的本地请求无法中断正在进行的登录 —— 仅有携带 SDK 自身 state 的浏览器重定向才能完成该流程。

    [English]
    Called by the loopback route. Resolves the waiting flow; False if none waits.

    A callback whose `state` doesn't match the pending flow's is ignored (returns False)
    WITHOUT consuming the pending future, so a stray/forged local hit can't abort a live
    sign-in — only the browser redirect carrying the SDK's own state resolves it.
    """
    global _pending
    if _pending is None or _pending.done():
        return False
    # [中文] 仅当为此流程捕获了实际 state 时才强制比对；授权 URL 中无 state 的流程回退到之前的接受任意行为。
    # [English] Only enforce when we actually captured a state for this flow; a flow with no state
    # in its authorize URL falls back to the prior accept-any behavior.
    if _expected_state is not None and (
        state is None or not secrets.compare_digest(state, _expected_state)
    ):
        return False
    pending, _pending = _pending, None
    pending.set_result((code, state))
    return True


async def _open_browser(url: str) -> None:
    global last_authorize_url, _expected_state
    last_authorize_url = url
    _expected_state = _state_from_url(url)
    import webbrowser

    logger.info("mcp oauth: opening browser for sign-in")
    await asyncio.get_running_loop().run_in_executor(None, webbrowser.open, url)


async def _refuse_browser(url: str) -> None:
    """[中文] 非交互式重定向处理器：绝不打开浏览器，但保留 URL，
    以便在拒绝后 GUI 的“重新打开登录页面”交互仍然有效。

    [English]
    Non-interactive redirect handler: never open a browser, but keep the URL so
    the GUI's "reopen sign-in page" affordance still works after the refusal.
    """
    global last_authorize_url
    last_authorize_url = url
    raise InteractiveAuthRequired(
        "sign-in required — reconnect this server from its page"
    )


async def _refuse_callback() -> tuple[str, Optional[str]]:
    raise InteractiveAuthRequired(
        "sign-in required — reconnect this server from its page"
    )


async def _wait_for_callback() -> tuple[str, Optional[str]]:
    global _pending, _expected_state
    if _pending is not None and not _pending.done():
        _pending.cancel()  # [中文] 陈旧的流程丢失了其浏览器标签页；新流程胜出 / [English] a stale flow lost its browser tab; the new one wins
    _pending = asyncio.get_running_loop().create_future()
    try:
        return await asyncio.wait_for(_pending, timeout=FLOW_TIMEOUT_SECONDS)
    except asyncio.TimeoutError:
        raise RuntimeError(
            "sign-in timed out — the browser window was not completed in "
            f"{FLOW_TIMEOUT_SECONDS // 60} minutes"
        )
    finally:
        _pending = None
        _expected_state = None  # [中文] 避免本次流程的 state 门控影响下一次流程 / [English] don't let this flow's state gate the next one


class _MetadataSeededProvider(OAuthClientProvider):
    """[中文] 持久化已发现的授权服务器元数据并在加载时重新填充的 OAuthClientProvider。
    若无此机制，SDK 在请求前的刷新授权会在元数据发现之前运行，并回退到 <origin>/token ——
    这在真实端点位于其他路径的供应商处会导致 404（例如 data.dlai.link 使用 /api/auth/mcp/token），
    从而将每次静默刷新都变成强制完全重新授权（owner-hit 2026-08-21，伴随前述陈旧过期时间缺陷）。

    [English]
    OAuthClientProvider that persists the discovered authorization-server
    metadata and re-seeds it on load. Without this the SDK's pre-request refresh
    grant runs BEFORE discovery and falls back to <origin>/token — a 404 on
    vendors whose real endpoint lives elsewhere (data.dlai.link uses
    /api/auth/mcp/token), which turned every silent refresh into a full re-auth
    demand (owner-hit 2026-08-21, with the stale-expiry flaw above).
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._ocw_storage: SecretStoreTokenStorage = kwargs.get("storage") or self.context.storage  # type: ignore[assignment]

    async def _initialize(self) -> None:
        await super()._initialize()
        raw = self._ocw_storage._data().get("oauth_metadata")
        if raw and self.context.oauth_metadata is None:
            try:
                from mcp.shared.auth import OAuthMetadata

                self.context.oauth_metadata = OAuthMetadata.model_validate(raw)
            except Exception:
                pass  # [中文] 缓存过期/不兼容：发现流程将重新填充它 / [English] stale/incompatible cache: discovery will refill it
        if self.context.oauth_metadata is None and self._ocw_storage._data().get(
            "tokens"
        ):
            # [中文] 尚无缓存（令牌在本次修复之前已存在）：从标准的 well-known 路径尽力进行一次获取，
            # 以便刷新授权能在下一次请求时直接以真实的令牌端点为目标。成功时进行缓存；失败时回退到 SDK 自身的（401 之后）发现流程。
            # [English] No cache yet (tokens predate this fix): one best-effort fetch from the
            # standard well-known location, so the refresh grant can target the real
            # token endpoint on the very next request. Cached on success; any failure
            # falls back to the SDK's own (post-401) discovery.
            try:
                from urllib.parse import urlparse

                import httpx
                from mcp.shared.auth import OAuthMetadata

                pr = urlparse(self.context.server_url)
                url = f"{pr.scheme}://{pr.netloc}/.well-known/oauth-authorization-server"
                async with httpx.AsyncClient(timeout=10) as c:
                    r = await c.get(url, headers={"Accept": "application/json"})
                if r.status_code == 200:
                    self.context.oauth_metadata = OAuthMetadata.model_validate(r.json())
                    self._persist_metadata()
            except Exception:
                pass

    def _persist_metadata(self) -> None:
        md = self.context.oauth_metadata
        if md is not None:
            try:
                self._ocw_storage._merge(
                    {"oauth_metadata": md.model_dump(mode="json", exclude_none=True)}
                )
            except Exception:
                logger.debug("could not persist oauth metadata", exc_info=True)

    async def _handle_token_response(self, response: Any) -> None:
        await super()._handle_token_response(response)
        self._persist_metadata()

    async def _handle_refresh_response(self, response: Any) -> bool:
        ok = await super()._handle_refresh_response(response)
        if ok:
            self._persist_metadata()
        return ok


def build_auth(
    server_name: str,
    server_url: str,
    secrets: SecretStore,
    *,
    interactive: bool = True,
) -> OAuthClientProvider:
    """[中文] 单个 OAuth MCP 服务器的 httpx 认证器（作为 streamablehttp_client(auth=…) 传入）。

    `interactive=False` 仍使用已存储的令牌和静默刷新，但在 SDK 想要浏览器授权的那一刻，
    它会抛出 InteractiveAuthRequired 而不是弹出浏览器 —— 仅有显式连接操作才传入 True。

    [English]
    The httpx auth for one OAuth MCP server (pass as streamablehttp_client(auth=…)).

    `interactive=False` still uses stored tokens and silent refresh, but the moment
    the SDK wants a browser authorization it raises InteractiveAuthRequired instead
    of opening one — only explicit connect actions pass True.
    """
    metadata = OAuthClientMetadata.model_validate(
        {
            "client_name": CLIENT_NAME,
            "redirect_uris": [redirect_base() + CALLBACK_PATH],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            # [中文] 公共客户端：DCR 颁发的密钥原生应用也无法保密。 / [English] Public client: DCR issues no secret a native app could keep anyway.
            "token_endpoint_auth_method": "none",
        }
    )
    return _MetadataSeededProvider(
        server_url=server_url,
        client_metadata=metadata,
        storage=SecretStoreTokenStorage(server_name, secrets),
        redirect_handler=_open_browser if interactive else _refuse_browser,
        callback_handler=_wait_for_callback if interactive else _refuse_callback,
    )


def has_tokens(server_name: str, secrets: SecretStore) -> bool:
    return bool((secrets.get(_profile(server_name)) or {}).get("tokens"))


def sign_out(server_name: str, secrets: SecretStore) -> bool:
    """[中文] 遗忘令牌以及 DCR 注册信息；下次连接将运行全新流程。 / [English] Forget tokens AND the DCR registration; next connect runs a fresh flow."""
    return secrets.delete(_profile(server_name))
