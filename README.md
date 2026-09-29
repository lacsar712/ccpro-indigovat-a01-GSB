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

1. **顶栏两个入口**：**还原台**（`/`）与**碱剂滴定**（`/titrations`），均可点
2. **染缸还原台**：横滑缸位条，每缸显示状态、最近电位、redox sparkline 与「碱 N」已签条数角标
3. **工坊 chip**：仅作缸位筛选，无独立工坊 CRUD 页
4. **点缸展开**：同页内登记浸染批次、改状态、看近几笔；展开区内**不放**滴定建账表单
5. **碱剂滴定专页**：按缸筛选滴定本列表 + 新建表单（染缸 / 滴定序号 / 碱度值 / 采集时间 / 当班人）

**建账规则**：只有 `reducing`（还原中）染缸能写碱剂滴定本；闲置缸与可染色缸禁止建账。同缸内滴定序号从 1 起且不得重复，碱度必须为正。

**可染色双门槛（并行，缺一不可，共用同一判定函数 `validate_vat_status_change`）**：

1. **氧化还原电位门槛（原有）**：最新浸染批次 `redoxMv` 存在且 ≤ **-500 mV**；
2. **碱度门槛**：该缸滴定序号从 1 起**连续且不少于 3 条**，**相邻碱度差绝对值 ≤ 0.4**，且**最新采集时间晚于该缸最近一笔浸染**。

空账（含只有 2 条滴定的种子缸 V-01）一律不得改为可染色。规则见 `app/services/vat_rules.py`。

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
4. **AlkalinityTitration（碱剂滴定本）**：归属染缸、`seq`（同缸唯一，从 1 起）、`alkalinity`（正数）、`collectedAt`、`operator`；仅还原中可建账

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
    templates/   # base / bay / titrations / login
```
