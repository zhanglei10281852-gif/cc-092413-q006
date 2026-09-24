# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
- 预警生命周期：带版本号的预警状态机，约束检测、待确认、已发布、升级、解除和作废之间的转移，检测幂等、过期版本拒绝、按权限发布/解除。
- 灾情协同：管理灾情报告、公告、部门责任和跨部门办理状态。
- 身份与权限：用户、角色、细粒度权限、会话令牌、账号停用和会话撤销。
- 审计记录：关键身份操作留痕，并对口令和令牌等敏感字段做过滤。
- 后台任务：使用 SQLite 保存待执行任务，支持去重、租约、重试和完成回执。

## 运行环境

- Python 3.11
- SQLite 3，由 Python 标准库提供
- Linux、macOS 或 Windows

## 安装

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[dev]"
```

配置项均以 `TOWNSHIP_` 开头。可以复制 `.env.example` 后按需设置，默认数据库位于 `./data/township.db`。

## 初始化与检查

```bash
python -m app.cli init-db
python -m app.cli check-db
```

## 启动服务

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8432
```

健康检查：

```bash
curl -sS http://127.0.0.1:8432/api/system/health
```

首次部署可创建唯一的初始管理员：

```bash
curl -sS -X POST http://127.0.0.1:8432/api/auth/bootstrap   -H 'Content-Type: application/json'   -d '{"username":"admin","password":"Admin!23456","client_label":"initial-setup"}'
```

之后通过 `/api/auth/login` 获取会话令牌，并在管理接口请求头中使用 `Authorization: Bearer <token>`。

## 预警生命周期

预警与地震事件关联，使用显式状态机管理，避免迟到的台站包把已经解除的警报重新推回生效状态。

```text
detected ──submit_confirm──▶ pending ──publish──▶ published ──escalate──▶ escalated
   │                             │                   │                          │
   └──void──▶ voided             └──void──▶ voided   └─────release──▶ released  │
                   (终态)                 (终态)                                 ◀──release──┘
                                                                  (released 也是终态；escalated 可继续 escalate)
```

允许的转移：`detected→pending`、`pending→published`、`published→escalated`、`escalated→escalated`、`published/escalated→released`、`detected/pending→voided`。`released` 与 `voided` 为终态，任何推回生效状态的操作都会被拒绝。

接口（前缀 `/api/seismic`）：

- `POST /events/{event_id}/alerts/detect`：自动/人工检测，按 `request_key`（缺省按事件）幂等；迟到包只取回当前预警，不改变状态。
- `POST /alerts/{alert_id}/transition`：携带 `action`、`expected_version`、`trigger_source` 执行转移。
- `GET /alerts/{alert_id}`、`GET /events/{event_id}/alerts`：返回当前 `status`、`version`、`severity`、`last_transition`、完整 `history`、`allowed_targets` 和最近一次 `last_rejection`。

关键约束：

- 每次成功转移使 `version` 自增；请求携带的 `expected_version` 与当前不一致时以 `409 stale_version` 拒绝。
- 非法转移返回 `409 illegal_transition`，对终态操作返回 `409 terminal_state`，并写入拒绝记录。
- 转移强制登录，按动作校验权限：确认 `seismic.alert.confirm`、发布/升级 `seismic.alert.publish`、解除/作废 `seismic.alert.release`；无权限返回 `403 missing_permission`。
- 转移记录触发来源（`automatic`/`manual`）与确认/操作人；所有状态落库，重启不丢失。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取、预警生命周期状态机（幂等检测、版本冲突、非法与终态转移、权限拦截、重启持久化），以及数据库时间格式。

## 编译检查

```bash
python -m compileall -q app tests
```

## API 冒烟

```bash
python -m app.cli smoke
```

该命令在进程内启动应用并检查服务根路径与健康接口，适合部署前快速确认路由和数据库初始化是否正常。

## 目录结构

```text
app/
  api/             用户、角色、审计、认证和系统接口
  core/            时钟、安全、异常和分页能力
  repositories/    SQLite 查询与持久化读取
  routers/         灾情、事件、公告、部门和信访业务接口
  seismic/         地震事件、台站观测、科学计算与预警生命周期状态机
  schemas/         管理接口输入模型
  services/        身份、审计和后台任务领域服务
  cli.py           初始化、检查和冒烟入口
  database.py      SQLite 连接、事务、表结构与基础权限
tests/             核心、管理接口和原有业务回归测试
tools/             本地维护脚本
```

## 数据一致性

SQLite 连接默认启用外键、WAL、busy timeout 与同步写入策略。需要跨多张表更新的管理操作在即时事务中执行，失败会整体回滚。会话令牌只保存摘要；用户停用会撤销仍有效的会话。审计事件保存操作者、动作、资源、结果和前后状态，但不会保存明文密码或令牌。
