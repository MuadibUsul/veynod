# Veynod 架构重构工程方案：基于真实三库架构的 IM 级派发神经与 Diagram-Kernel 融合蓝图

> **落地状态（2026-11-02）**：四步全部完成，实现记录与偏离见
> [`docs/refactor/IM_IMPLEMENTATION.md`](refactor/IM_IMPLEMENTATION.md)；
> Diagram↔Kernel 的验收见 [`docs/refactor/phase-2/RESULT.md`](refactor/phase-2/RESULT.md)。
>
> **对齐基准**：`main` @ `d37b781`（对应 `PROJECT_ARCHITECTURE_AND_PATHS.md` 真实路径与表结构）  
> **核心目标**：
> 1. **根治断联与假在线**：废弃 `/v1/inbox/stream` 单向 SSE 脆弱监听，在 `AgentGraph` 与 `AgentGraph/cli` 之间构建微信级 **WebSocket 全双工 + $200\text{ ms}$ 双勾确认（QoS ACK）+ Sidecar 进程隔离** 通信中枢。
> 2. **终结状态分裂（解决 R-006 / R-016）**：收口 `collab_tasks` 的 $9+$ 个状态写入方至 `task_lifecycle.py`，全面对齐 `lease_epoch`（Fencing Token）。
> 3. **打通百万行工程闭环（解决 §9.1 / §9.2）**：终结三条代码交付路径并存的乱局，将 `diagrams.py`（图计划层）与 `veynod_kernel`（DAG 写集锁 + 5 阶段确定性门禁）深度绑定，防止大型工程状态漂移与代码腐化。

---

## 1. 基于 `main` 现状的深度病灶诊断

结合你之前的痛点描述与 `PROJECT_ARCHITECTURE_AND_PATHS.md` 的真实代码映射，当前让你感觉“体系搞乱了、派发不稳定”的四大病根已经完全定位：

### 1.1 病灶一：通信神经脆弱（单向 SSE + 身份错位 + 阻塞式 Hook）
* **真实代码位置**：
  * 服务端流：`AgentGraph/app/api/inbox_stream.py` (`/v1/inbox/stream`)、`AgentGraph/app/services/pokes.py`、`AgentGraph/app/services/presence.py`
  * 客户端监听：`AgentGraph/cli/` (`npx veynod listen` / `runner.mjs`)
  * 涉及数据表：`veynod` 库中的 `agent_directory`、`agent_presence`、`agent_tokens`、`poke_deliveries`、`inbox_acks`、`runner_hosts`
* **架构级缺陷**：
  1. **收音机式盲发**：SSE 是单向 HTTP 流。当 `pokes.py` 发出 `task.assigned` 时，TCP 半开连接（Half-open socket）不会立即报错，服务端没有毫秒级的底层 `ACK` 超时熔断机制，只能被动等待或依赖配置繁琐的 `webhook_deliveries` (`wake_url`)。
  2. **Host 与 Agent 身份混淆**：宿主机 Runtime（`runner_hosts` 表，如 `grok-bot-vm-...`）在线，不代表业务智能体（`agent_directory` / `agent_presence` 表，如 `grok-dou`）的监听流在线。
  3. **子令牌死锁**：`agent_tokens`（及 `token_tree.py`）禁用自动续期后，HTTP SSE 无法在不中断流的情况下完成凭证热轮换。
  4. **听命与执行同进程阻塞**：`npx veynod listen` 收到 Poke 后，在同一进程或紧耦合链路中执行 `orientation.py` 上下文组装。一旦 `orientation` 或 MCP 调用卡死（如连接池 503），整个监听通道随之假死。

### 1.2 病灶二：`AgentGraph` 单进程 Lifespan 调度过载
* **真实代码位置**：`AgentGraph/app/main.py`
* **架构级缺陷**：`main.py` 的 lifespan 在同一个 `uvicorn` API 进程内启动了十几个后台循环（租约 reaper、webhook、外部动作、索引、访问日志、情况评估、event_dispatch、runtime_projection、code_projection 等）。当并发上升时，后台轮询任务与前台 HTTP/SSE 请求激烈争抢数据库连接池与事件循环，导致派发延迟抖动甚至 503。

### 1.3 病灶三：状态多头写入与三条交付路径割裂（R-006 & §9.1）
* **真实代码位置**：`AgentGraph/app/services/` 下 `tasks.py`、`leases.py`、`collab.py`、`scheduler.py`、`reviews.py`、`diagrams.py`、`audit.py`、`quality.py` 共 $9+$ 个模块直接修改 `collab_tasks` 状态。
* **架构级缺陷**：尽管 `agent_runtime` (`agent_runs.lease_epoch`) 和 `veynod_kernel` 已经有了较好的隔离设计，但在最顶层的产品控制面（`AgentGraph`），任务状态缺乏带 Fencing Token 的单一状态机守卫，且同时并存 A（Diagram 绕过 Kernel）、B（Host 直租）、C（Runtime -> Kernel）三条路径，造成认知与维护灾难。

### 1.4 病灶四：百万行代码“图拆解”与确定性内核脱节（§9.2）
* **真实代码位置**：`AgentGraph/app/services/diagrams.py` vs `veynod_kernel/app/engine/scheduler.py`
* **架构级缺陷**：你想用“图（Diagram）”来拆解十万/百万行工程并防止状态漂移，但目前 `diagrams.py` 全文无 `kernel` 字样！`diagram_chunks` 的 `allowed_write_paths` 只做了简单的 `fnmatch`，完全没有接入 `veynod_kernel` 中强大的 **Rust AST 作用域校验（`AST_SCOPE_DIFF`）、写集锁代数（`lock algebra`）和 5 阶段确定性门禁（`verification_gate.py`）**。

---

## 2. 历史断联顽疾与真实代码改造映射表

| 你遇到的断联/混乱现象 | 对应 `main` 真实模块与表 | 本方案外科手术式根治手段 |
| :--- | :--- | :--- |
| **1. Listen 进程没挂住 / 假在线** | `api/inbox_stream.py`<br>`services/presence.py`<br>`agent_presence` 表 | 新增 `/v1/ws/nerve` 全双工端点；`agent_presence` 强绑定内存级 `conn_id` 与 $5\text{ s}$ 双向心跳，超时 $12\text{ s}$ 直接由内存字典抹除在线态。 |
| **2. 身份错位 (`grok-bot-vm` vs `grok-dou`)** | `services/runners.py` (`runner_hosts`)<br>`services/directory.py` (`agent_directory`) | 严格拆分二元组：WS 握手头强制携带 `X-Host-Id` 与 `X-Agent-Id`，派活路由只认 `agent_id` 的活跃 WS Session，绝不看 `runner_hosts` 状态。 |
| **3. 子令牌过期断实时** | `services/token_tree.py`<br>`models.py` (`agent_tokens`) | 在 WS 通道内实现 `auth.refresh` 帧：只要 WS 通道不断且心跳正常，允许 Messenger 在到期前 $5$ 分钟无感滑动换发新子令牌，无需重连。 |
| **4. 离线兜底 (`wake_url`) Pending** | `services/pokes.py`<br>`poke_deliveries` / `webhook_deliveries` | CLI 改造为轻量级常驻 Sidecar 守护进程 (`veynod-messenger.mjs`)；若 WS 不在线且无备用 Agent，才异步降级触发 `wake_url`。 |
| **5. 有 Poke 但卡死在 Orientation** | `services/orientation.py`<br>`AgentGraph/cli/runner.mjs` | **双勾机制**：Sidecar 收到任务 $50\text{ ms}$ 内回第一勾（`NET_ACK`），随后以独立进程组启动 `runner.mjs`；若 $4.5\text{ s}$ 内未跑完 Orientation 回传第二勾（`EXEC_ACK`），直接 `kill -9 -pid` 强杀进程树并释放租约。 |

---

## 3. 数据库层手术：`veynod` 库增量迁移 (`v3_79`)

在 `AgentGraph/sql/migrations/` 下新增迁移文件 `v3_79_im_nerve_and_fencing.sql`。
本迁移解决三个核心问题：
1. 为 `collab_tasks` 和 `leases` 引入递增纪元号 **`lease_epoch`（Fencing Token）**，杜绝脑裂覆盖。
2. 为 `poke_deliveries` 和 `agent_presence` 增加毫秒级双勾 SLA 追踪字段及 `host_id` / `agent_id` 解耦字段。
3. 引入 PostgreSQL 原生 `NOTIFY veynod_nerve_dispatch` 触发器，使派发延迟从“轮询秒级”降至 **$<5\text{ ms}$**。

```sql
-- 文件路径: AgentGraph/sql/migrations/v3_79_im_nerve_and_fencing.sql

BEGIN;

-- 1. 为 collab_tasks 增加 Fencing Token (lease_epoch) 与双勾硬超时水位线
ALTER TABLE collab_tasks
  ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS dispatch_state VARCHAR(32) NOT NULL DEFAULT 'IDLE',
  -- dispatch_state 枚举: IDLE | AWAITING_NET_ACK | AWAITING_EXEC_ACK | ACTIVE_LEASE
  ADD COLUMN IF NOT EXISTS ack_deadline_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_collab_tasks_ack_deadline
  ON collab_tasks (dispatch_state, ack_deadline_at)
  WHERE dispatch_state IN ('AWAITING_NET_ACK', 'AWAITING_EXEC_ACK');

-- 2. 为 leases 表同步引入 lease_epoch，确保持有旧租约的僵尸 Agent 无法提交或续期
ALTER TABLE leases
  ADD COLUMN IF NOT EXISTS lease_epoch BIGINT NOT NULL DEFAULT 0,
  ADD COLUMN IF NOT EXISTS worker_pid INTEGER,
  ADD COLUMN IF NOT EXISTS conn_id UUID;

-- 3. 重构 agent_presence 表：彻底解耦 Host (runner_hosts) 与 Agent (agent_directory)
ALTER TABLE agent_presence
  ADD COLUMN IF NOT EXISTS host_id VARCHAR(128),
  ADD COLUMN IF NOT EXISTS active_conn_id UUID,
  ADD COLUMN IF NOT EXISTS transport_kind VARCHAR(16) NOT NULL DEFAULT 'ws_nerve',
  ADD COLUMN IF NOT EXISTS worker_busy BOOLEAN NOT NULL DEFAULT FALSE,
  ADD COLUMN IF NOT EXISTS active_task_key VARCHAR(128),
  ADD COLUMN IF NOT EXISTS last_rtt_ms INTEGER;

-- 4. 升级 poke_deliveries 表：支持微信级“双勾”延迟审计
ALTER TABLE poke_deliveries
  ADD COLUMN IF NOT EXISTS lease_epoch BIGINT,
  ADD COLUMN IF NOT EXISTS qos_status VARCHAR(32) NOT NULL DEFAULT 'SENT',
  -- qos_status 枚举: SENT | NET_ACKED (单勾/双勾1) | EXEC_ACKED (双勾2) | TIMEOUT_REVOKED
  ADD COLUMN IF NOT EXISTS net_ack_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS exec_ack_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS net_rtt_ms INTEGER,
  ADD COLUMN IF NOT EXISTS exec_startup_ms INTEGER;

-- 5. 建立 PG 原生毫秒级事件通知函数：当任务需要派发时，0 轮询瞬间唤醒 API 网关
CREATE OR REPLACE FUNCTION fn_notify_nerve_dispatch()
RETURNS TRIGGER AS $$
BEGIN
  IF (TG_OP = 'INSERT' AND NEW.status = 'open') OR
     (TG_OP = 'UPDATE' AND NEW.status = 'open' AND OLD.status <> 'open') OR
     (TG_OP = 'UPDATE' AND NEW.assignee_id IS DISTINCT FROM OLD.assignee_id AND NEW.assignee_id IS NOT NULL) THEN
    PERFORM pg_notify(
      'veynod_nerve_dispatch',
      json_build_object(
        'workspace_id', NEW.workspace_id,
        'task_key', NEW.task_key,
        'assignee_id', NEW.assignee_id,
        'lease_epoch', NEW.lease_epoch,
        'ts', extract(epoch from clock_timestamp()) * 1000
      )::text
    );
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_collab_tasks_nerve_dispatch ON collab_tasks;
CREATE TRIGGER trg_collab_tasks_nerve_dispatch
  AFTER INSERT OR UPDATE ON collab_tasks
  FOR EACH ROW
  EXECUTE FUNCTION fn_notify_nerve_dispatch();

INSERT INTO schema_migrations (version, applied_at)
VALUES ('v3_79_im_nerve_and_fencing', NOW())
ON CONFLICT (version) DO NOTHING;

COMMIT;
```

---

## 4. 契约层激活：将通信帧写入 `contracts/`

`PROJECT_ARCHITECTURE_AND_PATHS.md` §2.4 指出目前 `contracts/` 没有任何服务导入。我们首先在 `contracts/` 中定义跨层共享的 IM 神经通信契约，让 `AgentGraph` 正式引用它。

```python
# 新增文件路径: contracts/nerve_protocol.py
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, Optional
import time
import uuid


class NerveFrameType(str, Enum):
    # 1. 握手与通道内子令牌无感热续签
    AUTH_ACK = "auth.ack"
    TOKEN_REFRESH_REQ = "auth.token_refresh.req"
    TOKEN_REFRESH_RES = "auth.token_refresh.res"

    # 2. 应用层双向心跳 (区分 Host 与 Agent 状态)
    PING = "sys.ping"
    PONG = "sys.pong"

    # 3. 微信级任务派发与双勾回执 (QoS 1)
    TASK_POKE = "task.poke"               # Hub -> Messenger: 派发任务
    TASK_ACK_NET = "task.ack.net"         # Messenger -> Hub: 第一勾 (200ms 硬超时，网络送达)
    TASK_ACK_EXEC = "task.ack.exec"       # Messenger -> Hub: 第二勾 (5000ms 硬超时，越过 Orientation)
    TASK_BUSY_REJECT = "task.busy.reject" # Messenger -> Hub: 当前 Worker 正忙，拒绝接单

    # 4. 租约续命与冷酷强杀指令
    LEASE_HEARTBEAT = "lease.heartbeat"   # Messenger -> Hub: 携带 lease_epoch 续期
    LEASE_REVOKED = "lease.revoked"       # Hub -> Messenger: 租约已被剥夺，命令立刻强杀本地进程树


@dataclass
class NerveEnvelope:
    type: NerveFrameType
    payload: Dict[str, Any]
    frame_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    ts_ms: int = field(default_factory=lambda: int(time.time() * 1000))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "frame_id": self.frame_id,
            "type": self.type.value,
            "ts_ms": self.ts_ms,
            "payload": self.payload,
        }
```

---

## 5. 服务端改造：`AgentGraph` 全双工神经中枢与冷酷状态机

### 5.1 新增 IM 神经管理器 (`AgentGraph/app/services/nerve_hub.py`)

该服务直接替代原来脆弱的 `inbox_stream.py` + `pokes.py` 被动等待模式：
* **第一勾（$200\text{ ms}$）**：发送 `TASK_POKE` 后，通过 `asyncio.wait_for(net_ack_future, timeout=0.2)` 严格计时。若超过 $200\text{ ms}$ 未收到回执，**当场判定该 Agent 通道假死，立即关闭 Socket、抹除 `agent_presence` 并回滚任务租约**。
* **第二勾（$5000\text{ ms}$）**：收到第一勾后，等待客户端拉起子进程并越过 `orientation` 阶段回传 `TASK_ACK_EXEC`。超时 $5\text{ s}$ 立即回滚任务并下发 `LEASE_REVOKED` 强杀指令。

```python
# 新增文件路径: AgentGraph/app/services/nerve_hub.py
import asyncio
import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
from fastapi import WebSocket
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from contracts.nerve_protocol import NerveEnvelope, NerveFrameType

logger = logging.getLogger("veynod.nerve_hub")


@dataclass
class ActiveAgentSocket:
    workspace_id: str
    agent_id: str          # 真实执行 Agent 身份 (如 grok-dou)，绝不与 host_id 混用
    host_id: str           # 宿主机身份 (如 grok-bot-vm-01)
    conn_id: str
    ws: WebSocket
    connected_at_ms: int
    last_seen_ms: int
    worker_busy: bool = False
    active_task_key: Optional[str] = None
    active_lease_epoch: Optional[int] = None
    last_rtt_ms: int = 0


class NerveHubManager:
    """
    AgentGraph 内存级全双工神经中枢：
    管理所有在线 Agent 的 WebSocket 连接、200ms/5000ms 双勾 Future 挂起表与毫秒级任务投递。
    """

    def __init__(self) -> None:
        # key: (workspace_id, agent_id) -> ActiveAgentSocket
        self._connections: Dict[Tuple[str, str], ActiveAgentSocket] = {}
        # frame_id -> Future (第一勾 200ms 确认)
        self._net_ack_futures: Dict[str, asyncio.Future] = {}
        # frame_id -> Future (第二勾 5000ms Worker 越过 Orientation 确认)
        self._exec_ack_futures: Dict[str, asyncio.Future] = {}
        self._lock = asyncio.Lock()

    async def register_connection(
        self,
        db: AsyncSession,
        workspace_id: str,
        agent_id: str,
        host_id: str,
        ws: WebSocket,
    ) -> ActiveAgentSocket:
        conn_id = str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        key = (workspace_id, agent_id)

        async with self._lock:
            old = self._connections.get(key)
            if old is not None:
                logger.warning(f"Evicting stale socket for agent={agent_id} old_conn={old.conn_id}")
                try:
                    await old.ws.close(code=4000, reason="SUPERSEDED_BY_NEW_CONNECTION")
                except Exception:
                    pass

            session = ActiveAgentSocket(
                workspace_id=workspace_id,
                agent_id=agent_id,
                host_id=host_id,
                conn_id=conn_id,
                ws=ws,
                connected_at_ms=now_ms,
                last_seen_ms=now_ms,
            )
            self._connections[key] = session

        # 同步刷新数据库 agent_presence 表（明确区分 host_id 与 agent_id）
        await db.execute(
            text("""
                INSERT INTO agent_presence (
                    workspace_id, agent_id, host_id, active_conn_id,
                    transport_kind, status, worker_busy, updated_at
                ) VALUES (
                    :ws_id, :agent_id, :host_id, CAST(:conn_id AS UUID),
                    'ws_nerve', 'online', FALSE, NOW()
                )
                ON CONFLICT (workspace_id, agent_id) DO UPDATE SET
                    host_id = EXCLUDED.host_id,
                    active_conn_id = EXCLUDED.active_conn_id,
                    transport_kind = 'ws_nerve',
                    status = 'online',
                    worker_busy = FALSE,
                    updated_at = NOW()
            """),
            {"ws_id": workspace_id, "agent_id": agent_id, "host_id": host_id, "conn_id": conn_id},
        )
        await db.commit()
        return session

    async def unregister_connection(self, db: AsyncSession, session: ActiveAgentSocket, reason: str) -> None:
        key = (session.workspace_id, session.agent_id)
        removed = False
        async with self._lock:
            current = self._connections.get(key)
            if current and current.conn_id == session.conn_id:
                del self._connections[key]
                removed = True

        if removed:
            logger.info(f"Agent offline: {session.agent_id} (conn={session.conn_id}, reason={reason})")
            await db.execute(
                text("""
                    UPDATE agent_presence
                    SET status = 'offline',
                        active_conn_id = NULL,
                        worker_busy = FALSE,
                        updated_at = NOW()
                    WHERE workspace_id = :ws_id
                      AND agent_id = :agent_id
                      AND active_conn_id = CAST(:conn_id AS UUID)
                """),
                {"ws_id": session.workspace_id, "agent_id": session.agent_id, "conn_id": session.conn_id},
            )
            await db.commit()

    def on_frame_received(self, session: ActiveAgentSocket, raw_data: Dict) -> Optional[Dict]:
        """处理客户端发来的 ACK、心跳与状态上报帧"""
        session.last_seen_ms = int(time.time() * 1000)
        frame_type = raw_data.get("type")
        frame_id = raw_data.get("frame_id")
        payload = raw_data.get("payload") or {}

        if frame_type == NerveFrameType.TASK_ACK_NET.value:
            fut = self._net_ack_futures.get(frame_id)
            if fut and not fut.done():
                fut.set_result(payload)
            return None

        if frame_type == NerveFrameType.TASK_ACK_EXEC.value:
            fut = self._exec_ack_futures.get(frame_id)
            if fut and not fut.done():
                fut.set_result(payload)
            return None

        if frame_type == NerveFrameType.TASK_BUSY_REJECT.value:
            for fut_map in (self._net_ack_futures, self._exec_ack_futures):
                fut = fut_map.get(frame_id)
                if fut and not fut.done():
                    fut.set_exception(RuntimeError(f"AGENT_REJECTED: {payload.get('reason')}"))
            return None

        if frame_type == NerveFrameType.PING.value:
            session.worker_busy = bool(payload.get("worker_busy", False))
            session.active_task_key = payload.get("active_task_key")
            return NerveEnvelope(
                type=NerveFrameType.PONG,
                frame_id=frame_id,
                payload={"server_ts_ms": session.last_seen_ms},
            ).to_dict()

        return None

    async def dispatch_task_with_double_ack(
        self,
        db: AsyncSession,
        workspace_id: str,
        agent_id: str,
        task_key: str,
        task_payload: Dict,
    ) -> Tuple[bool, str]:
        """
        核心：微信级毫秒派发 + 200ms 第一勾 + 5000ms 第二勾原子确认
        """
        session = self._connections.get((workspace_id, agent_id))
        if not session:
            return False, "AGENT_NOT_CONNECTED_ON_WS_NERVE"
        if session.worker_busy:
            return False, "AGENT_WORKER_BUSY"

        # 1. 在数据库中原子递增 lease_epoch，并将 dispatch_state 设为 AWAITING_NET_ACK
        row = (
            await db.execute(
                text("""
                    UPDATE collab_tasks
                    SET lease_epoch = lease_epoch + 1,
                        assignee_id = :agent_id,
                        dispatch_state = 'AWAITING_NET_ACK',
                        ack_deadline_at = NOW() + INTERVAL '250 milliseconds',
                        updated_at = NOW()
                    WHERE workspace_id = :ws_id
                      AND task_key = :task_key
                      AND status IN ('open', 'ready')
                      AND dispatch_state = 'IDLE'
                    RETURNING id, lease_epoch, title, description
                """),
                {"ws_id": workspace_id, "agent_id": agent_id, "task_key": task_key},
            )
        ).fetchone()

        if not row:
            return False, "TASK_NOT_CLAIMABLE_OR_ALREADY_DISPATCHING"

        lease_epoch = int(row.lease_epoch)
        poke_id = str(uuid.uuid4())
        envelope = NerveEnvelope(
            type=NerveFrameType.TASK_POKE,
            frame_id=poke_id,
            payload={
                "task_key": task_key,
                "lease_epoch": lease_epoch,
                "title": row.title,
                "orientation_timeout_ms": 4500,
                **task_payload,
            },
        )

        # 记录 poke_deliveries 初始审计记录
        await db.execute(
            text("""
                INSERT INTO poke_deliveries (
                    id, workspace_id, target_agent_id, task_key,
                    lease_epoch, qos_status, created_at
                ) VALUES (
                    CAST(:poke_id AS UUID), :ws_id, :agent_id, :task_key,
                    :epoch, 'SENT', NOW()
                )
            """),
            {
                "poke_id": poke_id,
                "ws_id": workspace_id,
                "agent_id": agent_id,
                "task_key": task_key,
                "epoch": lease_epoch,
            },
        )
        await db.commit()

        loop = asyncio.get_running_loop()
        net_fut = loop.create_future()
        exec_fut = loop.create_future()
        self._net_ack_futures[poke_id] = net_fut
        self._exec_ack_futures[poke_id] = exec_fut
        session.worker_busy = True

        t0 = time.perf_counter()
        try:
            # 发送 WebSocket 帧
            await session.ws.send_json(envelope.to_dict())

            # === 第一勾校验：200ms 内必须收到网络层回执 ===
            await asyncio.wait_for(net_fut, timeout=0.200)
            net_rtt_ms = int((time.perf_counter() - t0) * 1000)
            session.last_rtt_ms = net_rtt_ms

            await db.execute(
                text("""
                    UPDATE collab_tasks
                    SET dispatch_state = 'AWAITING_EXEC_ACK',
                        ack_deadline_at = NOW() + INTERVAL '5000 milliseconds'
                    WHERE workspace_id = :ws_id AND task_key = :task_key AND lease_epoch = :epoch;

                    UPDATE poke_deliveries
                    SET qos_status = 'NET_ACKED', net_ack_at = NOW(), net_rtt_ms = :rtt
                    WHERE id = CAST(:poke_id AS UUID);
                """),
                {
                    "ws_id": workspace_id,
                    "task_key": task_key,
                    "epoch": lease_epoch,
                    "rtt": net_rtt_ms,
                    "poke_id": poke_id,
                },
            )
            await db.commit()

            # === 第二勾校验：5000ms 内 Worker 子进程必须拉起并越过 Orientation ===
            exec_ack_payload = await asyncio.wait_for(exec_fut, timeout=5.000)
            exec_startup_ms = int((time.perf_counter() - t0) * 1000)
            worker_pid = exec_ack_payload.get("worker_pid")

            # 正式签发带 lease_epoch Fencing Token 的执行租约
            await db.execute(
                text("""
                    UPDATE collab_tasks
                    SET status = 'in_progress',
                        dispatch_state = 'ACTIVE_LEASE',
                        ack_deadline_at = NULL,
                        updated_at = NOW()
                    WHERE workspace_id = :ws_id AND task_key = :task_key AND lease_epoch = :epoch;

                    INSERT INTO leases (
                        id, workspace_id, task_key, holder_agent_id,
                        lease_epoch, worker_pid, conn_id, expires_at, created_at
                    ) VALUES (
                        gen_random_uuid(), :ws_id, :task_key, :agent_id,
                        :epoch, :pid, CAST(:conn_id AS UUID),
                        NOW() + INTERVAL '45 seconds', NOW()
                    )
                    ON CONFLICT (workspace_id, task_key) DO UPDATE SET
                        holder_agent_id = EXCLUDED.holder_agent_id,
                        lease_epoch = EXCLUDED.lease_epoch,
                        worker_pid = EXCLUDED.worker_pid,
                        conn_id = EXCLUDED.conn_id,
                        expires_at = EXCLUDED.expires_at,
                        updated_at = NOW();

                    UPDATE poke_deliveries
                    SET qos_status = 'EXEC_ACKED', exec_ack_at = NOW(), exec_startup_ms = :startup_ms
                    WHERE id = CAST(:poke_id AS UUID);
                """),
                {
                    "ws_id": workspace_id,
                    "task_key": task_key,
                    "agent_id": agent_id,
                    "epoch": lease_epoch,
                    "pid": worker_pid,
                    "conn_id": session.conn_id,
                    "startup_ms": exec_startup_ms,
                    "poke_id": poke_id,
                },
            )
            await db.commit()
            session.active_task_key = task_key
            session.active_lease_epoch = lease_epoch
            logger.info(
                f"[✓✓] Double ACK success: task={task_key} agent={agent_id} "
                f"epoch={lease_epoch} net={net_rtt_ms}ms exec={exec_startup_ms}ms"
            )
            return True, "DISPATCHED_AND_EXECUTING"

        except asyncio.TimeoutError:
            stage = "NET_ACK_200MS" if not net_fut.done() else "EXEC_ACK_5000MS"
            reason = f"QOS_TIMEOUT_{stage}"
            logger.warning(f"[✗] Dispatch timeout ({reason}) for agent={agent_id} task={task_key}")
            await self._rollback_failed_dispatch(
                db, session, workspace_id, task_key, lease_epoch, poke_id, reason, kick_socket=(stage == "NET_ACK_200MS")
            )
            return False, reason

        except Exception as exc:
            reason = f"DISPATCH_ERROR:{exc}"
            await self._rollback_failed_dispatch(
                db, session, workspace_id, task_key, lease_epoch, poke_id, reason, kick_socket=False
            )
            return False, reason

        finally:
            self._net_ack_futures.pop(poke_id, None)
            self._exec_ack_futures.pop(poke_id, None)

    async def _rollback_failed_dispatch(
        self,
        db: AsyncSession,
        session: ActiveAgentSocket,
        workspace_id: str,
        task_key: str,
        lease_epoch: int,
        poke_id: str,
        reason: str,
        kick_socket: bool,
    ) -> None:
        session.worker_busy = False
        session.active_task_key = None
        # 原子回滚任务至 open 状态，以便毫秒级重新派发给其他健康 Agent
        await db.execute(
            text("""
                UPDATE collab_tasks
                SET status = 'open',
                    dispatch_state = 'IDLE',
                    ack_deadline_at = NULL,
                    updated_at = NOW()
                WHERE workspace_id = :ws_id AND task_key = :task_key AND lease_epoch = :epoch;

                UPDATE poke_deliveries
                SET qos_status = 'TIMEOUT_REVOKED'
                WHERE id = CAST(:poke_id AS UUID);
            """),
            {"ws_id": workspace_id, "task_key": task_key, "epoch": lease_epoch, "poke_id": poke_id},
        )
        await db.commit()

        # 向客户端补发强杀指令，防止客户端在超时后才拉起孤儿进程
        try:
            await session.ws.send_json(
                NerveEnvelope(
                    type=NerveFrameType.LEASE_REVOKED,
                    payload={"task_key": task_key, "lease_epoch": lease_epoch, "reason": reason},
                ).to_dict()
            )
            if kick_socket:
                await session.ws.close(code=4008, reason="QOS_200MS_NET_ACK_TIMEOUT")
        except Exception:
            pass


nerve_hub = NerveHubManager()
```

### 5.2 挂载 WebSocket 路由与通道内子令牌热续签 (`AgentGraph/app/api/ws_nerve_routes.py`)

在 `AgentGraph/app/api/` 下新增路由文件，并在 `AgentGraph/app/main.py` 中 `include_router`。
特别注意 **`TOKEN_REFRESH_REQ` 处理分支**：它直接调用 `token_tree.py` 为不断连的活跃 Agent 签发新子令牌，彻底根治“子令牌过期导致断实时”的顽疾。

```python
# 新增文件路径: AgentGraph/app/api/ws_nerve_routes.py
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.auth import verify_agent_token_ws
from app.services.nerve_hub import nerve_hub
from app.services.token_tree import issue_sliding_subtoken
from contracts.nerve_protocol import NerveEnvelope, NerveFrameType

router = APIRouter(tags=["ws-nerve"])


@router.websocket("/v1/ws/nerve")
async def ws_nerve_endpoint(
    websocket: WebSocket,
    db: AsyncSession = Depends(get_db),
):
    # 1. 强校验 Headers：严格区分 X-Agent-Id 与 X-Host-Id
    auth_header = websocket.headers.get("authorization", "")
    agent_id = websocket.headers.get("x-agent-id", "").strip()
    host_id = websocket.headers.get("x-host-id", "unknown-host").strip()

    principal = await verify_agent_token_ws(db, auth_header, expected_agent_id=agent_id)
    if not principal:
        await websocket.close(code=4001, reason="UNAUTHORIZED_OR_IDENTITY_MISMATCH")
        return

    await websocket.accept()
    session = await nerve_hub.register_connection(
        db=db,
        workspace_id=principal.workspace_id,
        agent_id=agent_id,
        host_id=host_id,
        ws=websocket,
    )

    # 下发握手确认帧
    await websocket.send_json(
        NerveEnvelope(
            type=NerveFrameType.AUTH_ACK,
            payload={
                "conn_id": session.conn_id,
                "agent_id": agent_id,
                "host_id": host_id,
                "workspace_id": principal.workspace_id,
            },
        ).to_dict()
    )

    try:
        while True:
            raw_msg = await websocket.receive_json()
            msg_type = raw_msg.get("type")

            # 处理通道内子令牌无感热续签（解决子令牌过期掉线问题）
            if msg_type == NerveFrameType.TOKEN_REFRESH_REQ.value:
                new_token, exp_ts = await issue_sliding_subtoken(
                    db=db,
                    workspace_id=principal.workspace_id,
                    agent_id=agent_id,
                    parent_token_id=principal.token_id,
                )
                await websocket.send_json(
                    NerveEnvelope(
                        type=NerveFrameType.TOKEN_REFRESH_RES,
                        frame_id=raw_msg.get("frame_id"),
                        payload={"token": new_token, "expires_at_ms": exp_ts},
                    ).to_dict()
                )
                continue

            # 处理租约心跳续期（强制校验 lease_epoch Fencing Token）
            if msg_type == NerveFrameType.LEASE_HEARTBEAT.value:
                payload = raw_msg.get("payload") or {}
                task_key = payload.get("task_key")
                lease_epoch = payload.get("lease_epoch")
                res = await db.execute(
                    """
                    UPDATE leases
                    SET expires_at = NOW() + INTERVAL '45 seconds', updated_at = NOW()
                    WHERE workspace_id = :ws_id
                      AND task_key = :task_key
                      AND holder_agent_id = :agent_id
                      AND lease_epoch = :epoch
                    """,
                    {
                        "ws_id": principal.workspace_id,
                        "task_key": task_key,
                        "agent_id": agent_id,
                        "epoch": lease_epoch,
                    },
                )
                await db.commit()
                if res.rowcount == 0:
                    # 租约已被剥夺，命令客户端立刻自杀清理 Worker
                    await websocket.send_json(
                        NerveEnvelope(
                            type=NerveFrameType.LEASE_REVOKED,
                            payload={"task_key": task_key, "lease_epoch": lease_epoch, "reason": "FENCING_EPOCH_EXPIRED"},
                        ).to_dict()
                    )
                continue

            # 交由内存 Hub 处理 ACK 与 PING
            reply = nerve_hub.on_frame_received(session, raw_msg)
            if reply:
                await websocket.send_json(reply)

    except WebSocketDisconnect:
        await nerve_hub.unregister_connection(db, session, reason="CLIENT_DISCONNECTED")
    except Exception as exc:
        await nerve_hub.unregister_connection(db, session, reason=f"SOCKET_ERROR:{exc}")
```

---

## 6. 客户端改造：`AgentGraph/cli/` Sidecar 信使与 Worker 进程树隔离

在 `AgentGraph/cli/` 中，我们将原来紧耦合的 `npx veynod listen` 拆分为两个职责极其纯粹的模块：
1. **`veynod-messenger.mjs`（常驻信使守护进程）**：纯 WebSocket 网络 I/O，零大模型逻辑、零 Orientation 逻辑。收到 `TASK_POKE` 后 **在 $<5\text{ ms}$ 内直接塞回第一勾 (`TASK_ACK_NET`)**。
2. **`runner.mjs`（瞬态执行进程组）**：由信使通过 `spawn(..., { detached: true })` 在独立进程组中拉起。如果卡在 `orientation` 超过 $4.5\text{ s}$，或者收到服务端的 `LEASE_REVOKED`，信使直接调用 `process.kill(-pid, 'SIGKILL')` **连同其派生的所有孙子进程（git/pytest/mcp）一并连根抹杀**。

```javascript
// 新增/替换文件路径: AgentGraph/cli/bin/veynod-messenger.mjs
import WebSocket from 'ws';
import { spawn } from 'node:child_process';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';

export class VeynodMessengerSidecar {
  constructor({ apiBaseUrl, agentId, token, runnerScriptPath }) {
    this.wsUrl = apiBaseUrl.replace(/^http/, 'ws') + '/v1/ws/nerve';
    this.agentId = agentId;
    this.hostId = os.hostname();
    this.token = token;
    this.runnerScriptPath = runnerScriptPath || path.resolve(import.meta.dirname, './runner.mjs');

    this.ws = null;
    this.workerProc = null;
    this.activeTask = null; // { task_key, lease_epoch, poke_frame_id }
    this.orientationWatchdog = null;
    this.pingTimer = null;
    this.tokenRefreshTimer = null;
    this.reconnectAttempts = 0;
  }

  start() {
    this.#connect();
  }

  #connect() {
    this.ws = new WebSocket(this.wsUrl, {
      headers: {
        'Authorization': `Bearer ${this.token}`,
        'X-Agent-Id': this.agentId,
        'X-Host-Id': this.hostId,
      },
    });

    this.ws.on('open', () => {
      console.log(`[Veynod-Nerve] Online as agent=${this.agentId} on host=${this.hostId}`);
      this.reconnectAttempts = 0;
      this.#startLoops();
    });

    this.ws.on('message', (data) => {
      try {
        const frame = JSON.parse(data.toString('utf8'));
        this.#onFrame(frame);
      } catch (err) {
        console.error('[Veynod-Nerve] Malformed frame:', err);
      }
    });

    this.ws.on('close', (code, reason) => {
      console.warn(`[Veynod-Nerve] Socket closed (${code}: ${reason.toString()}). Reconnecting...`);
      this.#stopLoops();
      this.#scheduleReconnect();
    });

    this.ws.on('error', () => {
      try { this.ws.terminate(); } catch {}
    });
  }

  #onFrame(frame) {
    const { frame_id, type, payload } = frame;

    switch (type) {
      case 'auth.token_refresh.res': {
        if (payload?.token) {
          this.token = payload.token;
          console.log(`[Veynod-Nerve] Sub-token hot-renewed in-band.`);
        }
        break;
      }

      case 'task.poke': {
        // 1. 铁律：微秒级立刻回传第一勾 (task.ack.net)，绝不在此处跑任何阻塞逻辑！
        if (this.workerProc !== null) {
          this.#send('task.busy.reject', { reason: 'WORKER_ALREADY_BUSY' }, frame_id);
          return;
        }
        this.#send('task.ack.net', { received_at_ms: Date.now() }, frame_id);

        // 2. 异步拉起独立进程组运行 Worker，并挂载 4500ms Orientation 硬看门狗
        this.#launchIsolatedWorker(frame_id, payload);
        break;
      }

      case 'lease.revoked': {
        const { task_key, lease_epoch, reason } = payload || {};
        if (this.activeTask && this.activeTask.task_key === task_key && this.activeTask.lease_epoch <= lease_epoch) {
          console.warn(`[Veynod-Nerve] Lease revoked by Hub (${reason}). Killing worker process tree!`);
          this.#killWorkerTree();
        }
        break;
      }
    }
  }

  #launchIsolatedWorker(pokeFrameId, taskPayload) {
    const { task_key, lease_epoch, orientation_timeout_ms = 4500 } = taskPayload;
    this.activeTask = { task_key, lease_epoch, pokeFrameId };

    // 使用 detached: true 创建独立进程组，确保超时能连根拔起所有孙子进程
    this.workerProc = spawn(
      process.execPath,
      [this.runnerScriptPath, '--task', task_key, '--epoch', String(lease_epoch)],
      {
        detached: process.platform !== 'win32',
        stdio: ['ignore', 'pipe', 'pipe', 'ipc'],
        env: {
          ...process.env,
          VEYNOD_AGENT_ID: this.agentId,
          VEYNOD_AGENT_TOKEN: this.token,
          VEYNOD_TASK_BUNDLE_JSON: JSON.stringify(taskPayload),
        },
      }
    );

    // 挂载 Orientation 阶段硬超时看门狗 (4.5s)
    this.orientationWatchdog = setTimeout(() => {
      console.error(`[Veynod-Nerve] Worker stuck in orientation > ${orientation_timeout_ms}ms! Killing tree.`);
      this.#killWorkerTree();
    }, orientation_timeout_ms);

    // 监听子进程 IPC 信号：越过 Orientation 后立即触发第二勾 (task.ack.exec)
    this.workerProc.on('message', (ipcMsg) => {
      if (ipcMsg?.type === 'IPC_ORIENTATION_READY') {
        if (this.orientationWatchdog) {
          clearTimeout(this.orientationWatchdog);
          this.orientationWatchdog = null;
        }
        this.#send(
          'task.ack.exec',
          {
            task_key,
            lease_epoch,
            worker_pid: this.workerProc?.pid,
          },
          pokeFrameId
        );
      }
    });

    this.workerProc.on('exit', (code, sig) => {
      console.log(`[Veynod-Nerve] Worker exited for task=${task_key} (code=${code}, sig=${sig})`);
      this.#cleanupWorkerState();
    });
  }

  #killWorkerTree() {
    if (this.orientationWatchdog) {
      clearTimeout(this.orientationWatchdog);
      this.orientationWatchdog = null;
    }
    if (this.workerProc && this.workerProc.pid) {
      const pid = this.workerProc.pid;
      try {
        if (process.platform !== 'win32') {
          process.kill(-pid, 'SIGKILL'); // 负号：连同整个进程组一并强杀
        } else {
          this.workerProc.kill('SIGKILL');
        }
      } catch {}
    }
    this.#cleanupWorkerState();
  }

  #cleanupWorkerState() {
    if (this.orientationWatchdog) {
      clearTimeout(this.orientationWatchdog);
      this.orientationWatchdog = null;
    }
    this.workerProc = null;
    this.activeTask = null;
  }

  #startLoops() {
    this.#stopLoops();
    // 1. 每 5 秒发送应用层双向心跳 + 当前租约续期
    this.pingTimer = setInterval(() => {
      this.#send('sys.ping', {
        worker_busy: this.workerProc !== null,
        active_task_key: this.activeTask?.task_key || null,
      });
      if (this.activeTask) {
        this.#send('lease.heartbeat', {
          task_key: this.activeTask.task_key,
          lease_epoch: this.activeTask.lease_epoch,
        });
      }
    }, 5000);

    // 2. 每 15 分钟在通道内无感热续签子令牌
    this.tokenRefreshTimer = setInterval(() => {
      this.#send('auth.token_refresh.req', { agent_id: this.agentId });
    }, 15 * 60 * 1000);
  }

  #stopLoops() {
    if (this.pingTimer) clearInterval(this.pingTimer);
    if (this.tokenRefreshTimer) clearInterval(this.tokenRefreshTimer);
    this.pingTimer = null;
    this.tokenRefreshTimer = null;
  }

  #scheduleReconnect() {
    const delay = Math.min(3000, 100 * Math.pow(2, this.reconnectAttempts++)) + Math.floor(Math.random() * 150);
    setTimeout(() => this.#connect(), delay);
  }

  #send(type, payload, frameId = crypto.randomUUID()) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ frame_id: frameId, type, ts_ms: Date.now(), payload }));
    }
  }
}
```

---

## 7. 打通十万/百万行级工程闭环：Diagram ↔ Veynod Kernel 深度融合（解决 §9.1 & §9.2）

你在对话中提到：
> “我希望更进一步的将状态固化，例如一百万行级别的工程，通过图来和人交互，接着把需求固化成图，进一步拆分小块，分发任务或者认领任务，避免大型工程下的状态漂移，以及越写越屎山，还有就是大型工程 agent 上下文无法兼顾全局的问题。”

而在你现有的仓库中，**你其实已经把解决这个问题的核心武器造出来了，只是还没接上线！**
看看 `PROJECT_ARCHITECTURE_AND_PATHS.md` §2.3 和 §5.2：
* 你在 `veynod_kernel` 中已经拥有 **Rust 编写的 AST 与写集锁核心 (`veynod_core`)**、**符号边图 (`symbol_edges`)**、以及 **5 阶段确定性门禁 (`verification_gate.py`)**：
  1. `WRITE_SET_BOUNDARY`（严禁 Agent 修改未授权路径的文件，防止越界制造屎山）
  2. `AST_SCOPE_DIFF`（从真实 AST 推导符号修改范围，不信任 Agent 自报的 `touched_symbols`）
  3. `CONTRACT_HASH_GUARD`（冻结接口契约哈希，防止破坏上下游模块的接口约定）
  4. `STATIC_TYPECHECK`（`pyright` 静态类型检查）
  5. `UNIT_TEST_SUITE`（`pytest` 单元测试）

### 7.1 为什么现在还会状态漂移？
因为如 §9.1 和 §9.2 所述，`main` 分支上的 **Path A（`diagrams.py` 图拆解链路）完全绕过了 `veynod_kernel`**！Diagram 派发出来的 Chunk 只是在 `diagrams.py` 里做简单的 JSONB 状态翻写，没有进入 Kernel 的 DAG 写集锁与 5 阶段门禁。

### 7.2 砍掉冗余路径，将 `diagrams.py` 接入 `veynod_kernel` 的三步改造

#### 第一步：为 `diagram_chunks` 增加 Kernel 强绑定列（迁移 `v3_80`）
```sql
-- 新增文件路径: AgentGraph/sql/migrations/v3_80_bind_diagram_to_kernel.sql
ALTER TABLE diagram_chunks
  ADD COLUMN IF NOT EXISTS kernel_workspace_id UUID,
  ADD COLUMN IF NOT EXISTS kernel_task_id UUID,
  ADD COLUMN IF NOT EXISTS scope_contract_hash VARCHAR(64),
  ADD COLUMN IF NOT EXISTS merged_commit_sha VARCHAR(64);
```

#### 第二步：在 `diagrams.py` 的 `freeze` / `dispatch` 阶段，直接编译为 Kernel DAG 节点
修改 `AgentGraph/app/services/diagrams.py`：
1. 当人类在控制台对图点击 **`freeze`（冻结需求图）** 时，调用 `AgentGraph/app/services/code_engine.py`，将每个 `diagram_chunk` 编译为 `veynod_kernel` 中的 `dag_tasks` 节点，并将 Chunk 之间的依赖边写入 Kernel 的 `task_dependencies` 表。
2. 将 `diagram_chunk.allowed_write_paths` 直接映射为 Kernel `dag_tasks` 的 **`write_set`（写集锁）**，并计算上下游接口文件的 `CONTRACT_HASH_GUARD` 哈希钉入 `task_snapshots`（`v4_013`）。
3. **砍掉旧的 Diagram 人工 review/merge 伪操作**：所有 Chunk 对应的 `collab_tasks`（`DG-<diagram>-<chunk>`）统一通过第 5 节的 **WebSocket 双勾神经** 毫秒级派发给 Agent，或者触发 `task_lifecycle.start_run`（Path C）。
4. Agent 提交代码必须且只能调用 Kernel 的 `POST /v2/syscalls/submit_patch`（202 异步门禁）。只有当 `kernel-gate` 跑完 5 阶段确定性门禁并由 `merge_coordinator.py` 完成 Git `update-ref` CAS 合并、生成 `delivery_records` 证据后，通过 `event_outbox` -> `event_inbox` 自动将对应的 `diagram_chunk` 推进为 `merged` 并写入 `merged_commit_sha`，进而自动解锁下游依赖的图节点！

---

## 8. 剥离 `AgentGraph/app/main.py` 后台循环，消除 503 与调度抖动

针对 §2.1 中指出的“`main.py` 的 lifespan 在单 API 进程内起十几个后台循环”导致阻塞 API 与派发的问题：

1. **拆分进程角色**：在 `AgentGraph/app/` 下新建 `scheduler_main.py`，将租约 reaper、webhook 投递、索引构建、访问日志聚合、情况评估、`event_dispatch`、`runtime_projection`、`code_projection` 等后台循环从 `main.py` 的 Web 进程中剥离出来。
2. **环境变量开关**：在 `docker-compose.yml` 中，`api:8000` 容器设置 `ENABLE_INLINE_BACKGROUND_LOOPS=0`，只保留：
   * FastAPI HTTP 路由
   * `/v1/ws/nerve` WebSocket 全双工网关
   * PostgreSQL `LISTEN veynod_nerve_dispatch` 协程（内存级零轮询唤醒）
3. 其余重度后台循环交由独立的 `agentgraph-worker` 容器运行，彻底释放 `api:8000` 的事件循环与数据库连接池。

---

## 9. 分步落地作战与验收清单 (Execution Runbook)

不要同时改动所有模块。按以下 **4 个原子阶段** 依次推进，每完成一步即可获得确定性的正反馈：

### Step 1：打通 `/v1/ws/nerve` 双勾毫秒级派发闭环（预计 1 天）
- [ ] 执行 `AgentGraph/sql/migrations/v3_79_im_nerve_and_fencing.sql`。
- [ ] 落盘 `contracts/nerve_protocol.py`、`AgentGraph/app/services/nerve_hub.py` 与 `AgentGraph/app/api/ws_nerve_routes.py`。
- [ ] 落盘 `AgentGraph/cli/bin/veynod-messenger.mjs`，替换原来的 SSE 监听入口。
- [ ] **验收标准**：在控制台派发任务，观察日志中 `net_rtt_ms < 50ms`（第一勾）与 `exec_startup_ms < 2000ms`（第二勾）；故意拔掉客户端网络或 `kill -STOP` 挂起客户端进程，验证 Hub 是否在 **$200\text{ ms}$ 内** 准确报错 `QOS_TIMEOUT_NET_ACK_200MS`、将 `agent_presence` 置为 `offline` 并将任务秒级转派给其他 Agent。

### Step 2：收口 `collab_tasks` 状态写入权威（解决 R-006，预计 1 天）
- [ ] 审计 `AgentGraph/app/services/` 下除 `task_lifecycle.py` 和 `nerve_hub.py` 以外直接 `UPDATE collab_tasks SET status = ...` 的代码点，统一收敛为调用 `task_lifecycle.transition_task_fenced(db, task_key, expected_epoch, new_status)`。
- [ ] 运行 `cd AgentGraph && pytest tests -q` 确保全部存量测试通过。

### Step 3：剥离 `main.py` 后台循环，消除 API 503（预计 0.5 天）
- [x]（2026-11-04 反向决定：调度与派发需要 nerve 套接字，所有循环留在 API 进程，开关与 `app/worker.py` 已删除）将 `main.py` lifespan 中的非实时轮询任务受控于 `ENABLE_INLINE_BACKGROUND_LOOPS` 开关，保证 `api:8000` 专职服务 HTTP 与 `/v1/ws/nerve`。

### Step 4：融合 Diagram 与 Veynod Kernel，跑通十万行级全自动 Loop（预计 2 天）
- [ ] 执行 `v3_80_bind_diagram_to_kernel.sql`。
- [ ] 在 `diagrams.py` 的 `freeze` 动作中调用 `code_engine.py`，将每个 `diagram_chunk` 注册为 Kernel 的 `dag_tasks`（绑定 `allowed_write_paths` -> `write_set`）。
- [x] 废弃 `CODE_ENGINE_LEGACY_DIRECT=1` 旧路径（2026-11-04 已删除），统一由 Kernel 5 阶段 Gate + Merge Coordinator CAS 产出 `delivery_records` 后驱动 Diagram 图节点变绿并自动派发下一批就绪 Chunk。
- [ ] 运行 `python scripts/chaos_suite.py` 与 `python scripts/e2e_unified.py` 完成全链路混沌与端到端验收。