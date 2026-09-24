# 地震灾害科学协同服务

这是一个面向地震台网与应急指挥中心的模块化后端，集中管理地震事件、台站观测、震情计算、灾情协同、用户权限、会话、审计和可恢复后台任务。项目使用 FastAPI 与 SQLite，所有运行数据保存在单个本地数据库文件中，不依赖另行部署的数据库、缓存或消息队列。

## 主要模块

- 震情档案：登记地震事件、震源参数和台站观测，保留计算输入摘要。
- 预警生命周期：带版本号的预警状态机，约束检测→待确认→已发布→升级→解除/作废的转移，检测幂等、过期版本拒绝、操作权限分级。
- 科学计算：提供震级、距离和烈度的确定性计算，以及可恢复后台任务。
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

预警以独立于地震事件档案的状态机管理，状态为：检测（detected）、待确认（pending_confirmation）、已发布（published）、升级（escalated）、解除（released）、作废（voided）。允许的转移：

```text
detected ──request_confirmation──▶ pending_confirmation ──confirm_publish──▶ published
   │                                     │                                      │
   └──────────────void───────────────────┴──────────────────────────────────────┘
                                                                  published/escalated ──escalate──▶ escalated（级别必须提高）
                                                                  published/escalated ──release───▶ released（终态）
                                                                  detected/pending   ──void───────▶ voided（终态）
```

- 解除与作废是终态，不存在任何转出；迟到台站包重复检测同一事件幂等返回，不会把已结束的预警推回生效。
- 每次成功转移使 `version` 加一；转移请求必须携带 `expected_version`，与当前版本不一致则返回 409 并记录拒绝原因。
- 人工动作需要会话和权限：`seismic.alerts.confirm`（提交确认）、`seismic.alerts.publish`（确认发布/升级）、`seismic.alerts.release`（解除）、`seismic.alerts.void`（作废，管理员默认具备全部权限）。
- `GET /api/seismic/alerts/{id}` 返回当前状态、版本、`last_transition`（最后一次合法转移及确认人）和 `last_rejected`（最近一次被拒绝操作的原因）。
- 所有状态与转移（含被拒绝的操作）持久化在 `seismic_alerts` / `seismic_alert_transitions` 表，重启不丢失。

## 测试

```bash
python -m pytest
```

测试覆盖身份初始化、登录、用户与角色维护、权限计算、账号停用后的会话撤销、审计脱敏、事件与台站观测、烈度计算、后台任务去重与领取，以及数据库时间格式。

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
