"""[中文] 实验性连接器 —— 风险自负的集成，从正式发布构建中排除。

本包中的连接器默认隐藏在 experimental-connectors 设置背后，连接时需要逐个进行明确的风险确认，并且在打包官方桌面版时通过 packaging/openworker-server.spec 进行剥离（在自构建二进制文件时可设置 COWORKER_EXPERIMENTAL=1 将其包含）。

新增实验性连接器的方法：定义一个包含用通俗语言明确声明潜在风险的 `risk_notice` 的 `ConnectorDescriptor`，将其追加至 `EXPERIMENTAL_DESCRIPTORS`，并以与第一方连接器相同的方式注册其工具或适配器。无论描述符中如何设置，descriptors.py 中的加载器都会强制开启 `experimental` 标志。

[English]
Experimental connectors — use-at-your-own-risk integrations, excluded from release builds.

Connectors in this package are hidden behind the experimental-connectors setting, require an
explicit per-connector risk acknowledgment to connect, and are stripped from official desktop
builds by packaging/openworker-server.spec (set COWORKER_EXPERIMENTAL=1 at build time to include
them in a self-built binary).

To add one: define a `ConnectorDescriptor` with a `risk_notice` that states the concrete
downside in plain language, append it to `EXPERIMENTAL_DESCRIPTORS`, and register its tools or
adapter the same way first-party connectors do. The `experimental` flag is forced on by the
loader in descriptors.py regardless of what the descriptor sets.
"""

from __future__ import annotations

from ..descriptors import ConnectorDescriptor

EXPERIMENTAL_DESCRIPTORS: list[ConnectorDescriptor] = []
