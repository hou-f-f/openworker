"""[中文] 针对模型选定 URL 的地址守卫（防 SSRF / 地址重绑定）。

`web_fetch` 与 `browser_open_url` 直接接收由模型提供的 URL，而在设计上模型的输入是不可信的 ——
它会读取网页、电子邮件和 Slack 消息，所有这些都被明确界定为“数据，而非指令”。
一个诱导智能体去抓取 `http://169.254.169.254/` 或 `http://127.0.0.1:11434/` 的网页，
会将只读的研究工具变成对其所在机器自身网络位置的内部探测，并且 `web_fetch` 的 `requires_approval=False`，
因此绝不会弹出任何确认提示。

本模块封锁了*仅因* OpenWorker 运行在用户本地机器上才可访问的网络范围：回环地址（loopback）、
RFC1918 及其他私有网络空间、链路本地地址（link-local，涵盖 169.254.169.254 的云元数据端点），
以及保留/组播网段。

每一次跳转（hop）都必须经过检查，而不仅仅是第一跳：否则 `follow_redirects=True` 会允许一个公开 URL
直接 302 重定向到本地回环地址，这是绕过此类过滤器的常见手法。

针对 DNS 重绑定（DNS rebinding）攻击，通过连接级固定（connection-level pinning）进行防范：
`get_checked` 会重写每次跳转的请求，使客户端连接到通过安全检查的确切 IP 地址（在 Host 和 SNI 中保留原始域名，
因此虚拟主机和证书校验仍能看到域名）。因此，在检查与连接之间哪怕一个 ~0 TTL 的 DNS 记录突然翻转到 127.0.0.1
也无法起效 —— 客户端自己从不重新解析该域名。
仅单独使用 `check_url` 时（如 browser_open_url 的预先检查）仍存在二次解析的时间差，因为浏览器拥有独立的连接池，无法在此处被强行固定。

[English]
Address guard for URLs the model chooses.

`web_fetch` and `browser_open_url` take a URL straight from the model, and the model's
input is untrusted by design — it reads web pages, email and Slack messages, all of which
are documented as "data, not instructions". A page that talks the agent into fetching
`http://169.254.169.254/` or `http://127.0.0.1:11434/` turns a read-only research tool into
a probe of the machine's own network position, and `web_fetch` is `requires_approval=False`,
so no prompt ever appears.

This blocks the ranges that are only reachable *because* OpenWorker runs on the user's
machine: loopback, RFC1918 and other private space, link-local (which covers the cloud
metadata endpoint at 169.254.169.254), and the reserved/multicast blocks.

Every hop is checked, not just the first: `follow_redirects=True` otherwise lets a public
URL 302 straight to loopback, which is the standard way this filter is bypassed.

DNS rebinding is closed by connection-level pinning: `get_checked` rewrites each hop so the
client connects to the exact address that passed the check (name in Host and SNI, so virtual
hosting and certificate verification still see the name). A record with a ~0 TTL that flips
to 127.0.0.1 between the check and the connect therefore changes nothing — the client never
resolves the name itself. `check_url` alone (browser_open_url's pre-check) still carries the
resolve-twice gap, because the browser owns its own connections and cannot be pinned from here.
"""

from __future__ import annotations

import ipaddress
import socket
from typing import Optional
from urllib.parse import urljoin, urlsplit, urlunsplit

MAX_REDIRECTS = 5

# [中文] RFC 6598 共享地址空间。Python 自带的 is_private 遗漏了该网段，但它是运营商级 NAT（CGNAT）空间，
# 且 Tailscale 会在此处分配内部主机地址（100.64.0.0/10），因此对它的抓取与 RFC1918 属于同类的“探测机器内网位置”。
# [English] RFC 6598 shared address space. Python's is_private misses it, but it is carrier grade
# NAT space and Tailscale hands out internal hosts here (100.64.0.0/10), so a fetch to it
# is the same "reach the machine's network position" class as RFC1918.
_CGNAT = ipaddress.ip_network("100.64.0.0/10")


def _blocked_reason(ip: ipaddress._BaseAddress) -> Optional[str]:
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local (includes the cloud metadata endpoint)"
    if ip.is_private:
        return "a private network"
    if ip.version == 4 and ip in _CGNAT:
        return "shared address space (CGNAT / RFC 6598)"
    if ip.is_multicast:
        return "multicast"
    if ip.is_reserved or ip.is_unspecified:
        return "a reserved range"
    return None


def _vet(url: str) -> tuple[Optional[str], Optional[str]]:
    """[中文] (拒绝原因, 连接固定的 IP 地址)。

    当 URL 允许被抓取时，原因为 None。
    对于字面 IP 的 URL，地址为 None（因为 URL 本身已指明了连接目标）；
    其他情况下为第一个解析结果 —— 可以安全固定，因为只要*任何*一个解析结果落在被阻止的网段内就会返回拒绝，
    因此同时包含公网和私网 A 记录的域名无法趁机溜过。

    [English]
    (refusal reason, address to pin the connection to).

    The reason is None when the URL may be fetched. The address is None for literal-IP
    URLs (the URL already names the connection target) and the first resolved answer
    otherwise — valid to pin because a refusal is returned when *any* answer lands in a
    blocked range, so a name with both a public and a private A record cannot slip through.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        return "url must start with http:// or https://", None
    host = parts.hostname
    if not host:
        return "url has no host", None

    # [中文] 字面地址不需要解析。 / [English] A literal address needs no lookup.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        reason = _blocked_reason(literal)
        return (f"refusing to fetch {host}: {reason}" if reason else None), None

    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except OSError as exc:
        return f"could not resolve {host}: {exc}", None

    pin: Optional[str] = None
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        # [中文] ::ffff:127.0.0.1 等映射地址必须按其携带的 IPv4 地址进行判定。
        # [English] ::ffff:127.0.0.1 and friends must be judged as the v4 address they carry.
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        reason = _blocked_reason(ip)
        if reason:
            return f"refusing to fetch {host} ({ip}): {reason}", None
        if pin is None:
            pin = raw
    return None, pin


def check_url(url: str) -> Optional[str]:
    """[中文] 如果 URL 允许抓取则返回 None，否则返回人类可读的拒绝原因。

    解析主机并在*任何*一个解析结果落在被阻止网段时予以拒绝，因此同时拥有公网和私网 A 记录的域名无法借机绕过。

    [English]
    None if the URL may be fetched, else a human-readable refusal reason.

    Resolves the host and rejects when *any* answer lands in a blocked range, so a name
    with both a public and a private A record cannot be used to slip through.
    """
    return _vet(url)[0]


def _pinned(url: str, ip: str) -> tuple[str, dict, dict]:
    """[中文] 重写 `url`，使得客户端连接到 `ip`，同时向外呈现原始主机名。

    返回 (request_url, headers, extensions)：URL 携带经过审查的固定地址，因此客户端自身绝不重新解析域名；
    Host 头部携带域名（及显式端口）用于虚拟主机路由；`sni_hostname` 确保 TLS 握手（包含证书验证）
    是针对域名而非 IP 进行验证。

    [English]
    Rewrite `url` so the client connects to `ip` while presenting the original name.

    Returns (request_url, headers, extensions): the URL carries the vetted address so the
    client never resolves the name itself, Host carries the name (and any explicit port)
    for virtual hosting, and `sni_hostname` keeps the TLS handshake — including certificate
    verification — against the name rather than the address.
    """
    parts = urlsplit(url)
    host = parts.hostname
    addr = f"[{ip}]" if ":" in ip else ip
    userinfo, _, _ = parts.netloc.rpartition("@")
    netloc = (f"{userinfo}@" if userinfo else "") + addr
    host_header = host
    if parts.port is not None:
        netloc += f":{parts.port}"
        host_header += f":{parts.port}"
    request_url = urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    extensions = {"sni_hostname": host} if parts.scheme == "https" else {}
    return request_url, {"Host": host_header}, extensions


def get_checked(client, url: str, *, max_redirects: int = MAX_REDIRECTS):
    """[中文] 发送 GET `url` 请求，在每一次跳转前验证并固定目标地址。

    `client` 构建时必须设置 `follow_redirects=False`；在此处手动遍历重定向以便每次的 Location 都能被检查。
    每一跳都连接到通过自身审查的确切地址（参见 `_pinned`），因此 DNS 重绑定域名无法在检查和连接之间掉包目标。
    返回最终响应，并将最终的*逻辑* URL —— 域名而非固定地址 —— 存入 `resp.extensions["logical_url"]` 供展示用调用方获取。
    当某次跳转被拒绝时抛出 `PermissionError`，当重定向超出预算时抛出 `RuntimeError`。

    [English]
    GET `url`, validating and pinning the address before every hop.

    `client` must be built with `follow_redirects=False`; redirects are walked here so each
    Location is checked. Every hop connects to the exact address that passed its check (see
    `_pinned`), so a rebinding name cannot swap targets between check and connect. Returns
    the final response, with the final *logical* URL — the name, not the pinned address —
    stashed as `resp.extensions["logical_url"]` for callers that display it. Raises
    `PermissionError` when a hop is refused, `RuntimeError` when the budget is exhausted.
    """
    seen = url
    for _ in range(max_redirects + 1):
        reason, pin = _vet(seen)
        if reason:
            raise PermissionError(reason)
        if pin is None:
            resp = client.get(seen)
        else:
            request_url, headers, extensions = _pinned(seen, pin)
            resp = client.get(request_url, headers=headers, extensions=extensions)
        if resp.status_code not in (301, 302, 303, 307, 308):
            ext = getattr(resp, "extensions", None)
            if isinstance(ext, dict):
                ext["logical_url"] = seen
            return resp
        location = resp.headers.get("location")
        if not location:
            return resp
        # [中文] 针对逻辑 URL 进行解析，而非 resp.url —— 后者是固定的 IP 地址，而相对 Location 必须保留在原始主机上。
        # [English] Resolved against the logical URL, not resp.url — the latter names the pinned
        # address, and a relative Location must stay on the original host.
        seen = urljoin(seen, location)
    raise RuntimeError(f"too many redirects (>{max_redirects})")
