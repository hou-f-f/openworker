"""[中文] `request_connector` 与 `grant_connector` —— 连接器访问权限由人类决策决定。

规范（跨机器连接器 §11.6，所有者裁决 2026-09-05）：

- `request_connector(connector, reason)`：任何具备连接器能力的同事都可以请求人类连接某个可用的服务
  （“连接 GitHub 能让我使看板和代码仓库保持同步”）。卡片提供“连接 / 以后再说”；拒绝是正常结果，
  同事会清楚说明有哪些事情因未连接而无法完成。
- `grant_connector(worker, connector, reason)`：团队负责人（Lead）在人员调配卡片之后，
  请求人类向其某位工人授予连接器。批准会在该工人角色的已声明权限上限内开启该会话的连接。

两者均与 `request_tool` 一样被引擎拦截：用户的带外决策本身即为授权同意，因此绝不会走常规权限路径。
此处的函数体仅在未挂接请求器时运行（例如无法弹出提示的界面）。

`request_connector` and `grant_connector` — connector access is a human decision.

Spec (connectors-across-machines §11.6, owner-ruled 2026-09-05):

- `request_connector(connector, reason)`: any connector-capable coworker may ask the
  human to connect a service it could use ("connecting GitHub lets me keep the board
  and the repo in step"). The card offers Connect / Not now; declining is a normal
  outcome and the coworker says what it could not do.
- `grant_connector(worker, connector, reason)`: a lead asks the human to give one of
  its workers a connector, later than the staffing card. Approval flips the worker's
  per-session connection within the worker persona's declared ceiling.

Both are engine-intercepted like `request_tool`: the user's out-of-band decision IS the
consent, so they never go through the permission path. These bodies only run when no
requester is wired (a surface that cannot prompt).
"""

from __future__ import annotations

from aisuite import ToolMetadata, tool


def request_connector_tool() -> object:
    def request_connector(connector: str, reason: str) -> dict:
        """[中文] 请求用户连接某个你可以使用但目前尚未连接的服务（例如 `github`、`slack`、`linear`）。
        仅当该连接能够让你妥善完成工作时才使用 —— 例如保持跟踪器同步、在团队阅读的平台上发帖 ——
        而不是仅仅为了行个方便。`reason` 为一句话：说明该连接能让你做到什么。
        用户可能会拒绝；若被拒绝，请在没有该服务的情况下继续工作，并直白地说明无法执行哪些操作。
        在同一个会话中切勿对同一个连接器重复请求两次。

        Ask the user to connect a service you could use but that is not connected
        (e.g. `github`, `slack`, `linear`). Use it when a connection would let you do
        the job properly — keeping a tracker in step, posting where the team reads —
        not for a convenience. `reason` is ONE sentence: what the connection lets you
        do. The user may decline; then carry on without it and say plainly what you
        could not do. Never ask twice for the same connector in one session."""
        return {"approved": False, "error": "connector requests aren't available in this surface"}

    return tool(
        request_connector,
        metadata=ToolMetadata(
            category="system",
            risk_level="low",
            capabilities=["request_connector"],
            description="Ask the user to connect a service (GitHub, Slack, Linear, …) you could use.",
        ),
    )


def grant_connector_tool() -> object:
    def grant_connector(worker: str, connector: str, reason: str) -> dict:
        """[中文] 请求用户向你的某位工人授予其尚未拥有的连接器访问权限 —— `worker` 是人员花名册中的呼叫名（例如 "nia"），
        `connector` 是服务标识符（例如 "github"）。工人初始时没有任何连接器；
        仅当具体条目确实需要时才提出请求（例如推送分支、提交工单），并在 `reason` 中说明（一句话）。
        由用户做出决定；若遭拒绝，请通过你自己或由用户来处理该外部步骤。

        Ask the user to give one of your workers access to a connector it does not
        have yet — `worker` is the callname from the staffing roster (e.g. "nia"),
        `connector` the service id (e.g. "github"). Workers start with no connectors;
        ask only when the item genuinely needs it (pushing a branch, filing an issue)
        and say so in `reason` (one sentence). The user decides; if declined, route
        the external step through yourself or the user."""
        return {"approved": False, "error": "connector grants aren't available in this surface"}

    return tool(
        grant_connector,
        metadata=ToolMetadata(
            category="team",
            risk_level="medium",
            capabilities=["team"],
            description="Ask the user to grant one of your workers a connector.",
        ),
    )
