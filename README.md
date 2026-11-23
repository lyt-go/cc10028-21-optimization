# 文档解析与知识抽取平台

面向多格式文档批量接入、版式解析、分块切分、实体关系抽取、向量化嵌入与抽取质量校验入库的非结构化知识抽取平台。

这是一个前后端分离的管理平台：前端 Vue 3 + Vite + TypeScript，后端 FastAPI（Python）。
两边各自独立启动，前端 dev server 已关掉自动打开页面，启动后按终端打印的地址手工打开。

## 目录结构

```text
.
├── frontend/                 Vue 3 + Vite + TypeScript 前端
│   ├── src/views/WorkbenchView.vue|tsx  全部模块共享工作台
│   ├── src/data/modules.ts              18个业务模块配置
│   ├── src/api/              统一请求封装
│   └── vite.config.ts        dev server 配置（open: false）
├── backend/                  FastAPI（Python） 后端
│   ├── app/routers/resource.py  通用查询/导出/登记/动作接口
│   ├── app/services/<key>.py    每个模块独立领域逻辑
│   ├── app/services/base.py     共享服务骨架
│   └── app/store.py          内存数据仓库与示例数据
├── .gitignore
└── docker-compose.yml
```

## 启动

### 后端

```bash
cd backend
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
./run.sh
```

健康检查：`curl http://127.0.0.1:8000/api/health`

### 前端

```bash
cd frontend
npm ci
npm run dev
```

前端默认监听 `http://127.0.0.1:5173/`，dev server 不会自动打开浏览器，
需要自己访问。`/api` 由 vite 代理到后端 `http://127.0.0.1:8000`。

## 业务模块

| 模块 | 目录 | 业务对象 | 主要字段 |
| --- | --- | --- | --- |
| 文件接入 | `file_intake` | 接入批次 | 批次编号、来源系统、文件清单 |
| 格式解析 | `format_parse` | 解析任务 | 任务编号、文档编号、解析器类型 |
| 版式还原 | `layout_restore` | 版式结构 | 文档编号、段落结构、标题层级 |
| 表格抽取 | `table_extract` | 表格区域 | 表格编号、文档编号、页码位置 |
| OCR识别 | `ocr_engine` | 识别任务 | 任务编号、图片区域、识别语言 |
| 分块切分 | `chunk_split` | 文本分块 | 分块编号、文档编号、分块长度 |
| 实体识别 | `entity_recognize` | 命名实体 | 实体编号、分块编号、实体类型 |
| 关系抽取 | `relation_extract` | 实体关系 | 关系编号、头实体、尾实体 |
| 术语归一 | `term_normalize` | 术语映射 | 映射编号、源术语、标准术语 |
| 敏感词过滤 | `sensitive_filter` | 过滤规则 | 规则编号、匹配模式、命中词条 |
| 向量嵌入 | `vector_embed` | 向量化任务 | 任务编号、分块编号、嵌入模型 |
| 知识图谱 | `kg_build` | 图谱三元组 | 三元组编号、头实体、谓词关系 |
| 抽取质量 | `extract_quality` | 质检报告 | 报告编号、批次编号、抽样比例 |
| 人工复核 | `annot_review` | 复核任务 | 复核编号、分块编号、复核对象 |
| 文档版本 | `doc_version` | 版本快照 | 快照编号、文档编号、版本号 |
| 抽取血缘 | `extract_lineage` | 血缘链路 | 链路编号、源文件、解析节点 |
| 结果交付 | `result_deliver` | 交付任务 | 交付编号、快照编号、交付格式 |
| 失败重采 | `parse_retry` | 重采任务 | 重采编号、失败环节、原始批次 |

## 约定

- 全部模块共享`frontend/src/views/WorkbenchView.vue|tsx`和一套资源路由。
- 每个模块仍保留独立`backend/app/services/<模块>.py`；纯前端保留
  `frontend/src/domain/<模块>.ts`，用于承载真实业务差异并维持36题位容量。
- 列表接口统一返回 `{ items, total, page, size }`，动作接口统一返回 `{ ok, message }`。
- 状态流转只允许在 `app/services` 里改，路由层不做业务判断。

## 版本差异与增量交付

文档版本模块除了数量统计，还会把实体、关系的记录级身份快照写入
`data/versions/<文档>_v<版本>.records.json`（下称记录附件）。因此当
两个版本的实体数、关系数完全相同时，仍能定位具体被删除的记录、
换了端点的关系，并形成版本差异选交清单。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/doc_version/compare?doc_id=&version_a=&version_b=&receiver_id=&receiver_has_baseline=&baseline_verified=` | 记录级差异 + 选交清单 |
| GET | `/api/doc_version/selection?...` | 仅返回选交清单 |
| GET | `/api/doc_version/versions?doc_id=` | 版本列表与记录附件是否可用 |
| POST | `/api/result_deliver/incremental` | 按选交清单生成增量包并出回执 |
| POST | `/api/result_deliver/resume` | 按 request_key 续传同一次请求 |
| GET | `/api/result_deliver/verify?request_key=` | 导入前端点依赖校验 |
| GET | `/api/result_deliver/receipt?request_key=` | 读取回执 |
| GET | `/api/result_deliver/receipts` | 全部回执 |

### 定位规则

- 实体身份键：`实体类型|实体文本|分块编号`；关系身份键：`头实体|关系类型|尾实体`。
- `recognized_at` / `extracted_at` 等每次抽取都会变的字段不参与内容比较。
- 变动条目带 `before` / `after`（版本号 + record_id + 原始记录），
  记录编号被复用时用 `replaces` / `replaced_by` 互指。

### 接收方基准三态

| 模式 | 条件 | 包内数据 |
| --- | --- | --- |
| `incremental` | 持有基准且可核实 | 只含变更实体/关系 + 删除墓碑；未变更端点按基准已存在 |
| `closure` | 持有基准但无法核实 | 变更记录 + 墓碑 + 关系端点实体随包补齐；不把依赖视为已存在 |
| `full` | 首次下载 | 目标版本完整实体与关系，无墓碑 |

### 交付与回执

一次交付请求由 `(文档, 基准版本, 目标版本, 接收方, 模式)` 唯一确定 `request_key`。
打包写到 `data/deliveries/REQ-*/`，中断后同一请求续传不会重开新交付；
已成功的同一请求再次提交会返回原回执（`duplicate_suppressed`）。
回执列出依赖对象、角色与所属版本，便于目标导入时判断端点能否补齐。

## 领域：数据工程
子方向：数据采集、爬虫与RPA
