# IndigoVat-01 · 染缸还原台

FastAPI + PostgreSQL + Jinja2：主界面是横向**缸位条**（Alpine 反应式），不是工坊/染缸/批次三表导航。Session Cookie 登录；规则在 `app/services/vat_rules.py`。

## 技术栈

- FastAPI、SQLAlchemy 2、PostgreSQL
- 启动时 `create_all` + 幂等种子（蓝靛湾一号坊 / 清水江二号坊）
- Session Cookie 认证（Starlette SessionMiddleware）
- Jinja2 + Alpine.js + Pico（叠靛蓝水墨自定义样式）
- Docker Compose：`web` + `db`

## 端口与数据库

| 服务 | 端口 |
|------|------|
| Web  | **4720** |
| Postgres | **6120**（容器内 5432） |

数据库账号：`indigovat` / `indigovat` / 库名 `indigovat`

## 快速启动

```bash
cd IndigoVat/IndigoVat-01
docker compose up --build -d
```

浏览器打开：http://localhost:4720

演示账号（登录页已预填）：

- `admin` / `123456`
- `worker` / `123456`

## 交互（信息架构）

1. **染缸还原台**（顶栏「还原台」）：横滑缸位条，每缸显示状态、最近电位与 redox sparkline；卡片右上角带「已签碱剂滴定条数」角标
2. **碱剂滴定本**（顶栏「碱剂滴定」）：独立专页，按缸 chip 筛选滴定列表，并在本页新建滴定登记
3. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
4. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；展开区**不**放滴定建账表单（滴定只在专页写）

**业务规则（改状态入口 `validate_vat_status_change`，两套门槛并行）**：

状态改为 `ready`（可染色）时，必须同时满足：

- **氧化还原电位门槛（原有）**：最新浸染批次 `redoxMv` 已填且 ≤ **-500 mV**；
- **碱剂滴定门槛**：该缸滴定序号自 1 **连续**且**不少于 3 条**，相邻碱度差绝对值 ≤ **0.4**，且**最新一条滴定采集时间晚于该缸最近一笔浸染**。

任意一条不满足都不能标可染色（空账、条数不足、序号断号/重复、碱度波动过大、采集早于浸染、电位未达标都会被拦）。

**滴定建账权限**：只有 `reducing`（还原中）染缸可写；`idle`（闲置缸）与 `ready`（可染色缸）禁止建账。同缸内滴定序号不得重复，碱度必须为正数。

## 本地开发（可选）

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
set POSTGRES_HOST=localhost
set POSTGRES_PORT=6120
uvicorn app.main:app --host 0.0.0.0 --port 4720 --reload
```

## 业务模型

1. **Workshop**：`name`、`region`、`notes`（UI 上仅为筛选片）
2. **Vat**：归属工坊、`code`、`dyeType`、`volumeL`、状态 `idle|reducing|ready`
3. **DipLot**：归属染缸、`dippedAt`、`clothMeters`、`redoxMv`（可空）
4. **AlkaliTitration（碱剂滴定本）**：归属染缸、`seq`（同缸唯一，从 1 起）、`alkalinity`（正数）、`collectedAt`、`operator`（当班人）

## 目录结构

```
IndigoVat-01/
  Dockerfile
  entrypoint.sh
  docker-compose.yml
  requirements.txt
  app/
    main.py
    db.py
    models.py
    schemas.py
    auth.py
    seed.py
    routers/
    services/vat_rules.py
    templates/   # base / bay / login
```
