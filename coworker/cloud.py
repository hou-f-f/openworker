"""[中文] OpenWorker Cloud 客户端：用户登录与托管的一键式连接器（Managed Connectors）。

此处所有功能均为可选（OPTIONAL）。未登录状态下应用功能完全正常 —— 每个连接器均支持手动粘贴 Token（登录后该功能依然保留）。Cloud 登录仅解锁一键式托管 OAuth 流程以及附带的元数据便利。

流程（移植自 opencoworker-cloud 中经过验证的 `ocw_cli` 参考实现）：
- 登录：Auth0 授权码 + PKCE。Sidecar 生成 PKCE 密钥对，浏览器完成登录，Auth0 重定向至 Sidecar 的本地回环 `GET /auth/callback`，并在本地完成授权码交换。Cloud 会话 Token 保存在 SecretStore 的 `cloud:auth` 项下。
- 托管连接：经过认证的 `POST /v1/oauth/{provider}/start` 返回提供商授权 URL；Broker 回调页面以 form-POST 方式将 Token 负载发送至 Sidecar 的本地回环 `POST /oauth/callback`；配置项写入本地。连接器 Token 绝不触碰云端存储。
- 刷新：托管配置（包含 refresh_token + connection_id）在即将过期前通过 Broker 自动续期；手动配置绝不触动。

[English]
OpenWorker Cloud client: sign-in and managed one-click connectors.

Everything here is OPTIONAL. The app is fully functional signed out — manual
token paste stays available for every connector (and remains available after
sign-in too). Cloud sign-in only unlocks the one-click managed OAuth path and
the metadata conveniences that come with it.

Flows (ported from the proven `ocw_cli` reference in opencoworker-cloud):

- Sign-in: Auth0 Authorization Code + PKCE. The sidecar generates the PKCE
  pair, the browser signs in, Auth0 redirects to the sidecar's loopback
  `GET /auth/callback`, and the code is exchanged here. Cloud session tokens
  live in the SecretStore under `cloud:auth`.
- Managed connect: authenticated `POST /v1/oauth/{provider}/start` returns the
  provider authorize URL; the broker's callback page form-POSTs the token
  payload to the sidecar's loopback `POST /oauth/callback`; the profile is
  written locally. Connector tokens never touch cloud storage.
- Refresh: managed profiles (they have refresh_token + connection_id) renew
  through the broker just before expiry; manual profiles are never touched.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets as _secrets
import time
import urllib.parse
from typing import Any, Optional

import httpx

from .config import Config
from .secrets import SecretStore

CLOUD_AUTH_PROFILE = "cloud:auth"
LOGIN_SCOPES = "openid profile email offline_access"

from . import __version__ as APP_VERSION  # noqa: E402

# [中文] 连接器 ID（规范名称，等于描述符名称）-> Broker 提供商标识键
# [English] connector id (canonical, = descriptor name) -> broker provider key
PROVIDER_FOR_CONNECTOR = {
    "gmail": "google",
    "google_calendar": "google",
    "google_drive": "google",
    "slack": "slack",
    "notion": "notion",
    "attio": "attio",
    "hubspot": "hubspot",
    "github": "github",
    "outlook": "microsoft",
}

# [中文] 按 OAuth state 索引的待处理 PKCE verifier；仅保存在进程内存中。如果登录会话跨越了 Sidecar 进程重启，只需重新发起登录即可。
# [English]
# Pending PKCE verifiers keyed by OAuth state; in-process only. A login that
# outlives the sidecar process simply has to be restarted.
_pending_logins: dict[str, dict[str, float | str]] = {}
_PENDING_TTL = 600
# [中文] 每个待处理的托管连接记录其启动时间以及（可选的）授权的目标机器 —— 回调据此记录进行路由。
# [English]
# Each pending managed connect records when it started and (optionally) which
# machine the grant is destined for — the callback routes on that record.
_pending_managed_states: dict[str, dict[str, Any]] = {}
_MANAGED_STATE_TTL = 600


# [中文] 将字节数组转换为不带补位 '=' 的 URL 安全 Base64 字符串
# [English] Convert bytes to unpadded URL-safe Base64 string
def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _now() -> float:
    return time.time()


# --- sign-in -----------------------------------------------------------------


def begin_login(config: Config) -> dict[str, Any]:
    """[中文] 创建 PKCE 登录流程并返回浏览器授权 URL。Sidecar 的 GET /auth/callback 将完成后续流程。

    重定向经过 Broker 的稳定回调地址，Broker 再将浏览器跳转回实际绑定的回环端口（在 state 的 `.port` 后缀中传递 —— Auth0 会原样回显 state）。在打包的应用中直接使用本地回环重定向是行不通的：Auth0 的白名单会拒绝未注册的端口，而桌面外壳会为 Sidecar 绑定一个随机空闲端口。

    [English]
    Create a PKCE login and return the browser URL. The sidecar's
    GET /auth/callback completes it.

    The redirect goes through the BROKER's stable callback, which bounces the
    browser to our actual loopback port (carried as state's `.port` suffix —
    Auth0 echoes state untouched). Direct loopback redirects can't work in the
    packaged app: Auth0's allow-list rejects unregistered ports, and the
    desktop shell binds the sidecar to a RANDOM free port. This shipped once
    as "Firefox can't connect to 127.0.0.1:8765" right after Auth0 finished.
    """
    verifier = _b64url(_secrets.token_bytes(48))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    port = os.environ.get("COWORKER_PORT") or config.port
    state = f"{_secrets.token_urlsafe(16)}.{port}"

    for key, pending in list(_pending_logins.items()):  # expire stale attempts
        if float(pending["created"]) < _now() - _PENDING_TTL:
            _pending_logins.pop(key, None)
    _pending_logins[state] = {"verifier": verifier, "created": _now()}

    redirect_uri = config.cloud_base_url.rstrip("/") + "/v1/auth/callback"
    authorize_url = (
        f"https://{config.cloud_auth_domain}/authorize?"
        + urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": config.cloud_client_id,
                "redirect_uri": redirect_uri,
                "scope": LOGIN_SCOPES,
                "audience": config.cloud_audience,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    return {"authorize_url": authorize_url, "state": state}


# [中文] 完成 PKCE 授权码交换并将 Cloud 令牌持久化到 SecretStore
# [English] Complete PKCE code exchange and persist cloud tokens to SecretStore
def complete_login(
    secrets: SecretStore, config: Config, code: str, state: str
) -> dict[str, Any]:
    pending = _pending_logins.pop(state, None)
    if pending is None or float(pending["created"]) < _now() - _PENDING_TTL:
        return {"ok": False, "error": "unknown or expired sign-in attempt"}

    resp = httpx.post(
        f"https://{config.cloud_auth_domain}/oauth/token",
        data={
            "grant_type": "authorization_code",
            "client_id": config.cloud_client_id,
            "code": code,
            "code_verifier": pending["verifier"],
            # MUST byte-match begin_login's authorize redirect_uri (RFC 6749 §4.1.3) — the
            # broker bounce, not the loopback. The bounce change (eda23c9) updated only the
            # authorize leg; the stale loopback here made Auth0 reject every exchange
            # ("token exchange failed" on all sign-ins from 07-09 to 07-11).
            "redirect_uri": config.cloud_base_url.rstrip("/") + "/v1/auth/callback",
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return {"ok": False, "error": "token exchange failed"}
    _store_cloud_tokens(secrets, resp.json())

    # Best-effort profile fetch so the GUI can show who is signed in.
    me = fetch_me(secrets, config)
    if me:
        profile = secrets.get(CLOUD_AUTH_PROFILE) or {}
        profile["account"] = me.get("user", {}).get("email") or ""
        profile["user_id"] = me.get("user", {}).get("user_id") or ""
        secrets.put(CLOUD_AUTH_PROFILE, profile)
    # Connection restore (sync_connections) deliberately does NOT run here: it is
    # best-effort metadata work, and doing it inline held the browser's "Signed in"
    # page + the GUI's signed-in flip hostage to an extra broker round trip (slow
    # sign-in complaint, 2026-07-16). The /auth/callback route kicks it off in the
    # background after responding.
    return {"ok": True, **status(secrets)}


def sync_connections(secrets: SecretStore, config: Config) -> dict[str, Any]:
    """[中文] 云端登录后，从 Broker 的元数据行（GET /v1/connections）重建本地托管连接状态。

    在全新安装时，仅 GitHub 可以完全自动恢复：其记录行仅为路由元数据（installation ID 与登录名），安装 Token 是按需动态生成的 —— 本地无需保存任何机密。按设计，所有其他连接器的 Token 仅保存在本地，因此需要进行一键重新授权。

    [English]
    Rebuild local managed-connection state from the broker's metadata rows
    (GET /v1/connections) after a cloud sign-in.

    Only GitHub restores fully on a fresh install: its rows are routing metadata
    (installation ids + logins) and installation tokens mint on demand — nothing
    secret ever needs to live here. Every other connector's tokens are local-only
    by design, so those need a one-click re-consent instead."""
    token = fresh_access_token(secrets, config)
    if not token:
        return {"ok": False, "error": "not signed in"}
    try:
        resp = httpx.get(
            config.cloud_base_url.rstrip("/") + "/v1/connections",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return {"ok": False, "error": "cloud unreachable"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"connections fetch failed ({resp.status_code})"}

    from .connectors.github_installs import managed_connect_install

    restored: list[str] = []
    for row in resp.json().get("connections", []):
        if row.get("connector") != "github" or row.get("status") != "connected":
            continue
        meta = row.get("tenant_metadata") or {}
        installs = meta.get("installations") or []
        if not installs and meta.get("installation_id"):
            installs = [meta]  # pre-restore-era rows carry only the primary install
        for inst in installs:
            out = managed_connect_install(
                secrets,
                {
                    "installation_id": str(inst.get("installation_id") or ""),
                    "account_login": inst.get("account_login", ""),
                    "account_type": inst.get("account_type", ""),
                    "repo_selection": inst.get("repo_selection", ""),
                    "github_login": meta.get("github_login", ""),
                    "connection_id": row.get("connection_id", ""),
                },
            )
            if out.get("ok"):
                restored.append(out["installation_id"])
    return {"ok": True, "restored": restored}


# [中文] 将 Cloud 访问令牌与刷新令牌持久化到 SecretStore
# [English] Persist cloud access and refresh tokens to SecretStore
def _store_cloud_tokens(secrets: SecretStore, token: dict) -> None:
    profile = secrets.get(CLOUD_AUTH_PROFILE) or {"type": "oauth", "enabled": True}
    profile["access_token"] = token.get("access_token", "")
    if token.get("refresh_token"):  # rotating refresh tokens: keep the newest
        profile["refresh_token"] = token["refresh_token"]
    profile["expires"] = _now() + int(token.get("expires_in") or 3600) - 60
    secrets.put(CLOUD_AUTH_PROFILE, profile)


# [中文] 查询 Cloud 登录状态（是否已登录、账号邮箱、用户 ID）
# [English] Query cloud sign-in status (signed-in flag, account email, user ID)
def status(secrets: SecretStore) -> dict[str, Any]:
    profile = secrets.get(CLOUD_AUTH_PROFILE) or {}
    return {
        "signed_in": bool(profile.get("access_token")),
        "account": profile.get("account") or "",
        "user_id": profile.get("user_id") or "",
    }


# [中文] 登出 Cloud 并清除本地存储的认证凭据
# [English] Log out from cloud and remove local auth credentials
def logout(secrets: SecretStore) -> dict[str, Any]:
    secrets.delete(CLOUD_AUTH_PROFILE)
    return {"ok": True, "signed_in": False}


def fresh_access_token(secrets: SecretStore, config: Config) -> Optional[str]:
    """[中文] 获取有效的 Cloud 会话 Token，临近过期时静默刷新；未登录或无法续期时返回 None（GUI 提示“请重新登录”）。

    [English]
    Valid cloud session token, silently refreshed near expiry; None when
    signed out or the session can't be renewed (GUI shows "sign in again")."""
    profile = secrets.get(CLOUD_AUTH_PROFILE) or {}
    if not profile.get("access_token"):
        return None
    if float(profile.get("expires") or 0) > _now():
        return profile["access_token"]
    if not profile.get("refresh_token"):
        return None
    resp = httpx.post(
        f"https://{config.cloud_auth_domain}/oauth/token",
        data={
            "grant_type": "refresh_token",
            "client_id": config.cloud_client_id,
            "refresh_token": profile["refresh_token"],
        },
        timeout=15,
    )
    if resp.status_code != 200:
        return None
    _store_cloud_tokens(secrets, resp.json())
    return (secrets.get(CLOUD_AUTH_PROFILE) or {}).get("access_token")


# [中文] 获取当前登录用户的个人信息
# [English] Fetch profile of currently authenticated user
def fetch_me(secrets: SecretStore, config: Config) -> Optional[dict]:
    token = fresh_access_token(secrets, config)
    if not token:
        return None
    try:
        resp = httpx.get(
            config.cloud_base_url.rstrip("/") + "/v1/me",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return None
    return resp.json() if resp.status_code == 200 else None


# --- telemetry (Phase 5) ---------------------------------------------------------
# [中文] 遥测统计（Phase 5）：
# 仅记录一句话：启动了哪种类型的 Coworker 以及启动时间 —— 无任何其他内容。
# 仅限已登录用户，默认开启且提供退出选项；未登录用户（或选择退出者）不发送任何数据。
# 绝不发送：会话标题、Prompt 内容、输出结果、工具参数、文件路径、连接器数据。
# [English]
# One sentence: which coworker type was started and when — nothing else. Signed-in
# users only, default-on with an opt-out; signed out (or opted out) sends NOTHING.
# Never sent: titles, prompts, outputs, tool args, file paths, connector content.

TELEMETRY_PROFILE = "cloud:telemetry"


def install_id(secrets: SecretStore) -> str:
    """[中文] 稳定的随机安装实例 ID，在首次使用时生成（spec Phase 5）。

    [English]
    Stable random per-install id, minted on first use (spec Phase 5)."""
    profile = secrets.get(TELEMETRY_PROFILE) or {}
    if not profile.get("install_id"):
        profile["install_id"] = "ins_" + _secrets.token_hex(12)
        secrets.put(TELEMETRY_PROFILE, profile)
    return profile["install_id"]


# [中文] 检查是否开启了遥测功能（默认开启，仅对已登录用户生效）
# [English] Check if telemetry is enabled (default on, only active when signed in)
def telemetry_enabled(secrets: SecretStore) -> bool:
    profile = secrets.get(TELEMETRY_PROFILE) or {}
    return bool(profile.get("enabled", True))  # default-on (only matters signed in)


# [中文] 设置遥测功能的启用状态
# [English] Set telemetry enabled flag
def set_telemetry_enabled(secrets: SecretStore, enabled: bool) -> dict[str, Any]:
    profile = secrets.get(TELEMETRY_PROFILE) or {}
    profile["enabled"] = bool(enabled)
    secrets.put(TELEMETRY_PROFILE, profile)
    return {"ok": True, "telemetry_enabled": bool(enabled)}


def emit_session_created(
    secrets: SecretStore,
    config: Config,
    *,
    session_id: str,
    persona_id: str,
    persona_family: str,
    workspace_kind: str,
) -> bool:
    """[中文] 尽力而为、不含任何实质内容的会话创建事件上报。除非用户已登录且遥测开关处于开启状态，否则直接无操作返回；所有异常均被吞掉（遥测绝不能破坏会话运行）。

    [English]
    Best-effort, content-free session event. Hard no-op unless signed in AND
    the toggle is on; failures are swallowed (telemetry must never break a session)."""
    import platform as _platform
    import sys

    if not telemetry_enabled(secrets):
        return False
    token = fresh_access_token(secrets, config)
    if not token:
        return False  # signed out: local-only users send nothing, by design
    body = {
        "event": "coworker_session_created",
        "install_id": install_id(secrets),
        "app_version": APP_VERSION,
        "platform": {"darwin": "macos", "win32": "windows"}.get(
            sys.platform, _platform.system().lower() or "unknown"
        ),
        "session": {
            "session_id_hash": "sha256:"
            + hashlib.sha256(session_id.encode()).hexdigest(),
            "persona_id": persona_id,
            "persona_family": persona_family,
            "workspace_kind": workspace_kind,
        },
    }
    try:
        resp = httpx.post(
            config.cloud_base_url.rstrip("/") + "/v1/telemetry/events",
            json=body,
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
        return resp.status_code == 200
    except httpx.HTTPError:
        return False


# --- managed connectors --------------------------------------------------------


def begin_managed_connect(
    secrets: SecretStore,
    config: Config,
    connector: str,
    *,
    access: str = "",
    flow: str = "",
    machine_id: str = "",
    machine_name: str = "",
) -> dict[str, Any]:
    """[中文] 经过认证的连接流程发起：返回供浏览器打开的提供商授权 URL。
    需要登录 —— 无论如何手动粘贴 Token 途径依然可用。
    `access` 指定 Broker 定义的授权许可级别（例如 hubspot 的 read | write）；桌面客户端从不发送具体的 scopes。
    `flow` 仅供 GitHub 使用："" 表示 GitHub App 安装页面；"authorize" 用于将协作者关联到现有的安装上。

    [English]
    Authenticated start: returns the provider consent URL for the browser.
    Requires sign-in — the manual token path stays available regardless.
    `access` names a broker-defined consent tier (hubspot read | write); the
    desktop never sends scopes. `flow` is GitHub-only: "" = the App install
    page; "authorize" links a teammate to an existing installation."""
    provider = PROVIDER_FOR_CONNECTOR.get(connector)
    if provider is None:
        return {"ok": False, "error": f"{connector} has no managed OAuth path"}
    token = fresh_access_token(secrets, config)
    if not token:
        return {"ok": False, "error": "not signed in", "signed_in": False}

    app_state = _secrets.token_urlsafe(16)
    # The broker form-POSTs the tokens back to THIS process's loopback. Use the
    # actually-bound port (published by run.py), falling back to config.port —
    # the packaged app runs the sidecar on a random port, not 8765.
    port = os.environ.get("COWORKER_PORT") or config.port
    try:
        resp = httpx.post(
            config.cloud_base_url.rstrip("/") + f"/v1/oauth/{provider}/start",
            json={
                "connector": connector,
                "redirect": f"http://127.0.0.1:{port}/oauth/callback",
                "app_state": app_state,
                **({"access": access} if access else {}),
                **({"flow": flow} if flow else {}),
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError as exc:
        return {"ok": False, "error": f"cloud unreachable: {type(exc).__name__}"}
    if resp.status_code != 200:
        return {"ok": False, "error": f"start failed ({resp.status_code})"}
    _pending_managed_states[app_state] = {
        "created": _now(),
        # A machine-targeted connect (machines spec §Remote OAuth): the callback
        # ships the grant to this machine instead of storing it locally.
        "machine_id": machine_id,
        "machine_name": machine_name,
    }
    return {
        "ok": True,
        "authorize_url": resp.json()["authorize_url"],
        "app_state": app_state,
    }


def consume_managed_state(state: str) -> Optional[dict[str, Any]]:
    """[中文] 严格一次性消费最近的托管 OAuth 回调状态。

    返回待处理记录（{"machine_id": …, "machine_name": …}），以便回调能正确路由针对特定机器的授权；若状态未知或已过期则返回 None。

    [English]
    Consume one recent managed-OAuth callback state exactly once.

    Returns the pending record ({"machine_id": …, "machine_name": …}) so the
    callback can route a machine-targeted grant, or None for an unknown or
    expired state."""
    if not state:
        return None
    record = _pending_managed_states.pop(state, None)
    if record is None or record["created"] < _now() - _MANAGED_STATE_TTL:
        return None
    return record


def managed_profile_from_callback(form: dict[str, str]) -> dict[str, Any]:
    """[中文] 从 Broker 的 form-POST 负载构建本地连接器配置。

    字段与手动粘贴格式兼容（包含 `access_token` 等），因此工具与权限关卡能以完全相同的方式处理两条路径；托管特有字段（refresh_token、connection_id）用于支持 Broker 自动续期与云端解绑。

    [English]
    Local connector profile from the broker's form-POST payload.

    Field-compatible with a manual paste (`access_token` etc.) so tools and
    gating treat both paths identically; the managed extras (refresh_token,
    connection_id) are what enable broker refresh and cloud disconnect.
    """
    profile = {
        "type": "oauth",
        "enabled": True,
        "managed": True,
        "access_token": form.get("access_token", ""),
        "refresh_token": form.get("refresh_token", ""),
        "scope": form.get("scope", ""),
        "connection_id": form.get("connection_id", ""),
        "provider": form.get("provider", ""),
        "account": form.get("account", ""),
    }
    if form.get("account_id"):
        # The stable id behind the display name (workspace/portal id) — what
        # the generic accounts layer keys multi-account profiles by.
        profile["account_id"] = form["account_id"]
    if form.get("expires_in"):  # absent ⇒ non-expiring token (e.g. Slack bot tokens)
        profile["expires"] = _now() + int(form["expires_in"]) - 60
    return profile


def delegate_connection(
    secrets: SecretStore,
    config: Config,
    connection_id: str,
    *,
    seal_pubkey: str = "",
    machine_id: str = "",
) -> Optional[dict[str, str]]:
    """[中文] 移交步骤（machines 规范 §Remote OAuth）：在 Broker 端将托管连接标记为由特定机器持有，使机器可以通过持有凭据进行自主续期。

    返回 {"user_id": …, "machine_credential": …} —— 两者均随授权传递。仅当提供了 `seal_pubkey`（机器的固定封印公钥）时才会下发凭据：Broker 随后会生成连接作用域的机密，用于验证机器的事件轮询（规范 §Managed events），并将该连接的中继事件切换到该机器的加密队列。`machine_id`（规范 §Fly sandboxes）指定托管控制平面所认知的持有机器，以便 Broker 在队列中放入事件后唤醒休眠的沙箱；仅托管环境记录（`cloud:`）具有此 ID。返回 None 表示未成功委派。

    [English]
    Handoff step (machines spec §Remote OAuth): mark a managed connection
    machine-held at the broker, so the machine can renew it by possession.

    Returns {"user_id": …, "machine_credential": …} — both travel with the
    grant. The credential arrives only when `seal_pubkey` (the machine's
    pinned sealing key) is sent: the broker then mints the connection-scoped
    secret that authenticates the machine's event polling (spec §Managed
    events) and switches the connection's relay events to that machine's
    sealed queue. `machine_id` (spec §Fly sandboxes) names the holding
    machine as the hosted control plane knows it, so the broker can wake a
    sleeping sandbox after queueing an event; only hosted (`cloud:`) rows
    have one. None = not delegated."""
    token = fresh_access_token(secrets, config)
    if not token:
        return None
    body: dict[str, str] = {}
    if seal_pubkey:
        body["seal_pubkey"] = seal_pubkey
    if machine_id:
        body["machine_id"] = machine_id
    try:
        resp = httpx.post(
            config.cloud_base_url.rstrip("/")
            + f"/v1/connections/{connection_id}/delegate",
            headers={"Authorization": f"Bearer {token}"},
            **({"json": body} if body else {}),
            timeout=20,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    body = resp.json()
    user_id = str(body.get("user_id") or "")
    if not user_id:
        return None
    return {
        "user_id": user_id,
        "machine_credential": str(body.get("machine_credential") or ""),
    }


def refresh_managed_token(
    secrets: SecretStore,
    config: Config,
    connector: str,
    *,
    profile_key: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """[中文] 通过 Broker 续期托管连接器的 Token。返回更新后的配置，如果无法通过该方式续期（或无需续期）则返回 None。手动配置绝不触动。`profile_key` 针对按账户索引的配置（例如 `gmail:account:<email>`）；默认值为 `<name>:default`。

    [English]
    Renew a managed connector token through the broker. Returns the updated
    profile, or None if this profile can't be (or doesn't need to be) renewed
    that way. Manual profiles are never touched. `profile_key` targets an
    account-keyed profile (`gmail:account:<email>`); default = `<name>:default`."""
    key = profile_key or f"{connector}:default"
    profile = secrets.get(key) or {}
    if not (profile.get("managed") and profile.get("refresh_token")):
        return None
    provider = profile.get("provider") or PROVIDER_FOR_CONNECTOR.get(connector)
    if not provider:
        return None
    token = fresh_access_token(secrets, config)
    if token:
        url = config.cloud_base_url.rstrip("/") + f"/v1/oauth/{provider}/refresh"
        headers = {"Authorization": f"Bearer {token}"}
        body = {
            "refresh_token": profile["refresh_token"],
            "connection_id": profile.get("connection_id", ""),
            "connector": connector,
        }
    else:
        # Machine-held grant (machines spec §Remote OAuth): no cloud session
        # here — renew by possession against the delegated route. Works only
        # for grants a signed-in user delegated at handoff; the broker's
        # guardrails (pacing, uniform 401s, failure auto-suspend) apply.
        user_id = str(profile.get("broker_user_id") or "")
        connection_id = str(profile.get("connection_id") or "")
        if not (user_id and connection_id):
            return None
        url = (
            config.cloud_base_url.rstrip("/")
            + f"/v1/oauth/{provider}/refresh-delegated"
        )
        headers = {}
        body = {
            "refresh_token": profile["refresh_token"],
            "connection_id": connection_id,
            "user_id": user_id,
            "connector": connector,
        }
    try:
        resp = httpx.post(url, json=body, headers=headers, timeout=20)
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    fresh = resp.json()
    profile["access_token"] = fresh.get("access_token", "")
    if fresh.get("refresh_token"):
        profile["refresh_token"] = fresh["refresh_token"]
    profile["expires"] = _now() + int(fresh.get("expires_in") or 3600) - 60
    secrets.put(key, profile)
    return profile


def ensure_fresh_connector_token(
    secrets: SecretStore,
    config: Config,
    connector: str,
    *,
    profile_key: Optional[str] = None,
    leeway: int = 120,
) -> None:
    """[中文] 连接器工具的“过期自动刷新”钩子：如果这是即将过期的托管配置，则在就地续期。对手动配置无操作。

    [English]
    Refresh-on-expiry hook for connector tools: if this is a managed profile
    about to expire, renew it in place. No-op for manual profiles."""
    key = profile_key or f"{connector}:default"
    profile = secrets.get(key) or {}
    if not profile.get("managed"):
        return
    expires = float(profile.get("expires") or 0)
    if expires and expires > _now() + leeway:
        return
    refresh_managed_token(secrets, config, connector, profile_key=profile_key)


def revoke_connector_connections(
    secrets: SecretStore, config: Config, connector: str
) -> int:
    """[中文] 按名称撤销指定连接器的所有活动 Broker 连接。

    机器持有场景（machines 规范 §Managed events）：节点机器自身断开时只删除本地副本，无法触达 Broker（因设计上无 Cloud 会话），这会导致委派残留且事件丢失。桌面端持有会话，因此由桌面端执行撤销 —— 无需本地存在该配置（桌面端在移交时已遗忘授权）。返回被撤销的连接数。

    [English]
    Revoke every live broker connection for a connector, by NAME.

    The machine-held case (machines spec §Managed events, drill finding): a
    box's own disconnect deletes its copy but cannot reach the broker (no
    session — by design), which would leave the delegation lingering and the
    connection's events black-holed. The DESKTOP holds the session, so it
    performs the revocation — without needing any local profile (the desktop
    forgot the grant at handoff). Returns how many connections were revoked."""
    token = fresh_access_token(secrets, config)
    if not token:
        return 0
    base = config.cloud_base_url.rstrip("/")
    try:
        resp = httpx.get(
            base + "/v1/connections",
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return 0
    if resp.status_code != 200:
        return 0
    revoked = 0
    for row in resp.json().get("connections", []):
        if row.get("connector") != connector or row.get("status") == "disconnected":
            continue
        try:
            r = httpx.post(
                base + f"/v1/connections/{row['connection_id']}/disconnect",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
        except httpx.HTTPError:
            continue
        if r.status_code == 200:
            revoked += 1
    return revoked


def cloud_disconnect(
    secrets: SecretStore,
    config: Config,
    connector: str,
    *,
    profile_key: Optional[str] = None,
) -> None:
    """[中文] 尽力而为：通知云端托管连接已移除，以便将其元数据状态置为已断开。无论云端通知成功与否，本地删除始终执行。

    [English]
    Best-effort: tell the cloud a managed connection is gone so its metadata
    flips to disconnected. Local deletion always proceeds regardless."""
    profile = secrets.get(profile_key or f"{connector}:default") or {}
    connection_id = profile.get("connection_id")
    if not (profile.get("managed") and connection_id):
        return
    token = fresh_access_token(secrets, config)
    if not token:
        return
    try:
        httpx.post(
            config.cloud_base_url.rstrip("/")
            + f"/v1/connections/{connection_id}/disconnect",
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError:
        pass


# [中文] installation_id -> (token, expires_epoch)。设计上仅保存在内存中：GitHub 安装 Token 有效期约 1 小时且可随时向 Broker 重新签发；按 github-relay-spec §4 规范，绝不可写入 SecretStore。
# [English]
# installation_id -> (token, expires_epoch). MEMORY ONLY by design: GitHub
# installation tokens live ~1 h and are re-minted from the broker; they must
# never touch the secret store (github-relay-spec §4).
_GITHUB_TOKEN_CACHE: dict[str, tuple[str, float]] = {}
_GITHUB_TOKEN_LEEWAY = 600  # re-mint when < 10 min of life remains


def github_installation_token(
    secrets: SecretStore, config: Config, installation_id: str, *, force: bool = False
) -> str:
    """[中文] 获取用于 GitHub API 调用的实时安装访问 Token，通过经过认证的 Broker 路由签发并在内存中缓存（约 50 分钟）。`force` 跳过缓存 —— 用于 401 重试路径。不可用时返回空字符串（未登录 / 已撤销安装 / 无法连接云端）。

    [English]
    A live installation access token for GitHub API calls, minted via the
    authenticated broker route and cached in memory (~50 min). `force` skips
    the cache — the 401 retry path. Empty string when unavailable (signed
    out / revoked installation / cloud unreachable)."""
    installation_id = str(installation_id or "").strip()
    if not installation_id:
        return ""
    if not force:
        cached = _GITHUB_TOKEN_CACHE.get(installation_id)
        if cached and cached[1] > _now() + _GITHUB_TOKEN_LEEWAY:
            return cached[0]
    token = fresh_access_token(secrets, config)
    if token:
        url = config.cloud_base_url.rstrip("/") + "/v1/github/token"
        payload: dict[str, str] = {"installation_id": installation_id}
        headers = {"Authorization": f"Bearer {token}"}
    else:
        # Machine-held GitHub (machines spec §Managed events, increment 2):
        # a box has no cloud session — it mints by machine credential on the
        # delegated route, using the stamps the handoff wrote to the pointer.
        pointer = secrets.get("github:default") or {}
        credential = str(pointer.get("machine_credential") or "")
        if not (
            credential
            and pointer.get("connection_id")
            and pointer.get("broker_user_id")
        ):
            return ""
        url = config.cloud_base_url.rstrip("/") + "/v1/machine/github/mint"
        payload = {
            "installation_id": installation_id,
            "connection_id": str(pointer["connection_id"]),
            "user_id": str(pointer["broker_user_id"]),
        }
        headers = {"Authorization": f"Bearer {credential}"}
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=20)
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    body = resp.json()
    minted = body.get("token", "")
    # expires_at is ISO-8601 from GitHub; parse defensively, default 1 h.
    expires = _now() + 3600
    try:
        from datetime import datetime

        raw = str(body.get("expires_at", ""))
        if raw:
            expires = datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except ValueError:
        pass
    if minted:
        _GITHUB_TOKEN_CACHE[installation_id] = (minted, expires)
    return minted


def clear_github_token(installation_id: str) -> None:
    """[中文] 从内存缓存中丢弃某个 GitHub 安装 Token（用于断开连接或撤销时）。

    [English]
    Drop a cached installation token (disconnect / revocation)."""
    _GITHUB_TOKEN_CACHE.pop(str(installation_id or "").strip(), None)


def github_disconnect_installation(
    secrets: SecretStore, config: Config, installation_id: str
) -> None:
    """[中文] 尽力而为：删除当前用户关于该 GitHub 安装的中继路由记录，使云端停止向其推送事件。无论如何本地配置删除始终执行（云端记录仅负责路由）。

    [English]
    Best-effort: delete this user's relay routing rows for one installation
    so the cloud stops pushing its events. Local profile deletion always
    proceeds regardless (the row only routes)."""
    clear_github_token(installation_id)
    token = fresh_access_token(secrets, config)
    if not token:
        return
    try:
        httpx.post(
            config.cloud_base_url.rstrip("/") + "/v1/relay/github/disconnect",
            json={"installation_id": installation_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError:
        pass


def slack_disconnect_workspace(
    secrets: SecretStore, config: Config, team_id: str
) -> None:
    """[中文] 尽力而为：删除当前用户关于该 Slack 工作区的中继路由记录，使云端停止向其推送事件。无论如何本地 Token 删除始终执行（云端记录仅负责路由；没有桌面端 Token 本来也无法发送消息）。

    [English]
    Best-effort: delete this user's relay routing row for one workspace so the
    cloud stops pushing its events. Local token deletion always proceeds regardless
    (the row only routes; without the desktop token nothing can be sent anyway)."""
    token = fresh_access_token(secrets, config)
    if not token:
        return
    try:
        httpx.post(
            config.cloud_base_url.rstrip("/") + "/v1/relay/slack/uninstall",
            json={"team_id": team_id},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError:
        pass


# --- persona gallery -----------------------------------------------------------


# [中文] 向云端 Persona 画廊 API 发送带认证的 GET 请求
# [English] Send authenticated GET request to cloud Persona gallery API
def _gallery_get(secrets: SecretStore, config: Config, path: str) -> Optional[dict]:
    token = fresh_access_token(secrets, config)
    if not token:
        return None
    try:
        resp = httpx.get(
            config.cloud_base_url.rstrip("/") + path,
            headers={"Authorization": f"Bearer {token}"},
            timeout=15,
        )
    except httpx.HTTPError:
        return None
    return resp.json() if resp.status_code == 200 else None


def gallery_list(secrets: SecretStore, config: Config) -> Optional[dict]:
    """[中文] 获取对当前用户租户可见的精选 Persona 卡片列表；未登录或无法连接云端时返回 None（按设计画廊需要登录）。

    [English]
    Curated persona cards visible to this user's tenant; None when signed
    out or the cloud is unreachable (gallery requires sign-in by design)."""
    return _gallery_get(secrets, config, "/v1/personas/gallery")


# [中文] 获取指定 Persona 的清单（manifest）详情
# [English] Fetch manifest details for a persona slug
def gallery_manifest(secrets: SecretStore, config: Config, slug: str) -> Optional[dict]:
    return _gallery_get(secrets, config, f"/v1/personas/gallery/{slug}/manifest")


def gallery_install_event(secrets: SecretStore, config: Config, slug: str) -> None:
    """[中文] 尽力而为的产品遥测（仅上报 slug 与版本，绝不含实质内容）。

    [English]
    Best-effort product telemetry (slug/version only, no content)."""
    token = fresh_access_token(secrets, config)
    if not token:
        return
    try:
        httpx.post(
            config.cloud_base_url.rstrip("/")
            + f"/v1/personas/gallery/{slug}/install-events",
            json={"platform": __import__("sys").platform},
            headers={"Authorization": f"Bearer {token}"},
            timeout=10,
        )
    except httpx.HTTPError:
        pass


def gallery_detail(secrets: SecretStore, config: Config, slug: str) -> Optional[dict]:
    """[中文] 独立详情页数据载荷：云端卡片 + 发布者宣传介绍，其中能力事实通过桌面端自身的严格解析器在本地从清单中推导得出 —— 宣传介绍绝不可能夸大安装时许可审查所不显示的内容，因为两个视图源自同一个解析后的清单。

    [English]
    Solo-page payload: the cloud card + publisher pitch, with capability
    facts derived LOCALLY from the manifest via the desktop's own strict
    parser — the pitch can never advertise what install-time consent wouldn't
    show, because both views come from the same parsed manifest."""
    card = _gallery_get(secrets, config, f"/v1/personas/gallery/{slug}")
    manifest = gallery_manifest(secrets, config, slug)
    if card is None or manifest is None:
        return None
    try:
        from .personas.loading import consent_summary
        from .personas.manifest import parse_manifest

        m = parse_manifest(manifest.get("manifest_markdown", ""), fallback_id=slug)
        capabilities = consent_summary(m)
        recommends = [
            {"kind": r.kind, "ref": r.ref, "reason": r.reason, "tier": r.tier}
            for r in m.recommends
        ]
    except Exception as exc:  # malformed manifest: surface, don't crash
        return {"ok": False, "error": f"manifest failed local validation: {exc}"}
    return {
        "ok": True,
        "card": card,
        "capabilities": capabilities,
        "recommends": recommends,
    }


def broker_request(
    secrets: SecretStore,
    config: Config,
    method: str,
    path: str,
    body: Optional[dict[str, Any]] = None,
) -> tuple[int, Any]:
    """[中文] 为桌面 GUI 的云端视图向 Broker 发起带用户认证的单一调用（UX-049 4c）。返回 (status, json)；未登录时返回 (401, …)；无法连接时返回 (0, …)。Token 绝不离开本进程。

    [English]
    One user-authed call to the broker for the desktop GUI's cloud views
    (UX-049 4c). Returns (status, json); (401, …) when signed out; (0, …)
    when unreachable. The token stays in this process."""
    token = fresh_access_token(secrets, config)
    if not token:
        return 401, {"error": "not signed in"}
    url = config.cloud_base_url.rstrip("/") + path
    try:
        resp = httpx.request(
            method, url, json=body, headers={"Authorization": f"Bearer {token}"}, timeout=20
        )
    except httpx.HTTPError:
        return 0, {"error": "cloud unreachable"}
    try:
        data = resp.json()
    except ValueError:
        data = {"error": resp.text[:200]}
    return resp.status_code, data
