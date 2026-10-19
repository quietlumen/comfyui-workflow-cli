# cwf 数据模型与命令速查（技能用）

数据来源永远是 `cwf ... --json` 的 stdout。下面是各命令返回的关键字段，供写参数时对照。

## 关键命令与返回字段

### `cwf status`
```json
{
  "ok": true,
  "online": true,
  "comfyui_version": "0.39.2",
  "queue_running": 0, "queue_pending": 0,
  "instance_workflow_files": 45,
  "ledger_entries": 45,
  "origins": {"instance": 45, "api": 0},
  "states": {"ready": 5, "captured": 40, "registered": 0, "stale": 0, "missing": 0},
  "params_total": 37,
  "browser_ready": true,
  "warnings": []
}
```
- 实例离线时 `online: false`，但台账命令照常可用。

### `cwf list [--type F] [--status S] [--json]`
```json
{
  "ok": true, "count": 45, "source": "ledger",
  "items": [
    {"id": "wf-0035", "name": "z_image_turbo_文生图", "origin": "instance",
     "state": "ready", "nodes": 22, "params": 4,
     "function": "text_to_image", "function_source": "auto",
     "warnings": 0, "last_run": {"run_id": "...", "status": "success", "at": "..."}}
  ]
}
```
- `state: ready` = 可调用的工作流（有 api.json 且有绑定参数）。
- `function` 取值：`text_to_image` / `image_edit` / `upscale` / `video` / `other`。
- 用法：文生图候选 = `list --type text_to_image --status ready`；视频 =
  `list --type video --status ready`（图生视频/文生视频都是 video）。

### `cwf show <id>`
```json
{
  "ok": true, "id": "wf-0035", "name": "z_image_turbo_文生图",
  "origin": "instance", "function": "text_to_image",
  "state": "ready", "state_reasons": [...],
  "verified": true, "nodes": {"total": 22, "bypassed": 3, "muted": 0},
  "params": [
    {"name": "prompt", "type": "string", "category": "basic",
     "default": "...", "key": "PrimitiveStringMultiline#value#0",
     "node": "37", "input": "value", "orphan": false},
    {"name": "megapixels", "type": "number", "category": "advanced",
     "default": 1, "key": "ResolutionSelector#megapixels#0", ...},
    {"name": "seed", "type": "integer", "category": "basic", ...},
    {"name": "upscale", "type": "number", "category": "advanced", ...}
  ],
  "presets": {"quick": {"params": {"megapixels": 1, "upscale": 1}}},
  "warnings": [], "runs": [{"run_id": "...", "status": "success", ...}]
}
```

### `cwf run <id> --dry-run --json`（组包预览，提交前给用户看）
```json
{
  "ok": true, "id": "wf-0035", "dry_run": true,
  "params": {"prompt": "...", "megapixels": 0.5, "seed": 42, "upscale": 1.5},
  "param_sources": {"prompt": "cli", "megapixels": "cli", ...},
  "orphans": [], "uploads": [{"param": "image", "local_path": "x.png", "uploaded": false}],
  "payload": {...}
}
```
- `param_sources` 是 `default` / `preset:<名>` / `cli` 三档，帮用户看到"值从哪来"。
- 图片参数给本地路径 → `uploads` 里 `uploaded: false`（dry-run 不真传），真跑时自动上传。

### `cwf run <id> --param ... --yes --json`
```json
{
  "ok": true, "run_id": "20261020-001139_5d1ba8f2", "prompt_id": "...",
  "status": "success", "duration_seconds": 16.8,
  "artifacts": [{"type": "image", "instance_path": "z_00001_.png",
                 "local_path": "D:\\comfyui_workflow\\data\\outputs\\wf-0035\\...\\z_00001_.png",
                 "bytes": 1322866, "width": 832, "height": 1216}],
  "params_applied": {...}, "mode": {...}, "uploads": [...]
}
```
- 产物 `local_path` 直接用于 present。

### `cwf history [id]` / `cwf log <run_id>`
- `history`：`{"count": N, "items": [{run_id, workflow_id, status, at, duration_seconds, artifacts}]}`。
- `log`：完整记录（含 `node_errors`、`error`、`artifacts`）；`--no-wait` 留下的
  `status: running` 记录会就地补全。

### `cwf inspect <id> --suggestion --json`
```json
{
  "ok": true, "id": "wf-0035", "state": "ready",
  "summary": {"nodes": 25, "links": 35, "bypassed": 3, "params": 4,
              "candidates": 47, "node_drift": 0, "type_drift": 0},
  "suggestion": {"params": [
    {"key": "KSampler#seed#0", "node": "58", "input": "seed",
     "class_type": "easy seed", "type": "integer", "default": 1,
     "recommended": true, "suggested_name": "seed", "already_bound": false,
     "reason": "内置语义表：seed"}
  ], "nodes": [...]}
}
```
- 绑定依据就是 `key`：`cwf param add <id> <名字> --key "<key>"`。

### `cwf param list <id> --json`（读时自检）
```json
{
  "ok": true, "id": "wf-0035", "count": 4,
  "params": [...], 
  "type_drift": [{"name": "megapixels", "stored": "integer", "declared": "number",
                  "raw": "FLOAT", "key": "ResolutionSelector#megapixels#0"}]
}
```
- `type_drift` 非空时用 `cwf param set <id> <名> --type <declared>` 修正。

### `cwf inspect <id> --warnings --json`（转换警告 + 漂移）
```json
{
  "ok": true, "id": "wf-0035", "state": "ready",
  "summary": {"node_drift": 0, "type_drift": 0, "full": true},
  "warnings": [...],
  "drift": {"node": [], "type": []}
}
```

## 语义键与参数类型

- 语义键 `Class#input#0`：节点类型 + 输入名 + 该类型在图上出现的序号（稳定，不随节点 ID 变）。
- 参数 `type`（cwf 语义类型）：`string` / `integer` / `number` / `boolean` / `combo` / `image` / `file`。
  - `combo` 有 `options`（可选值），喂错值 ComfyUI 可能"秒完不出图"，提交前必须校验；
  - `integer`/`number` 有 `min`/`max`，越界直接报错；
  - `image`/`file`：给本地路径自动上传，或给实例 input 里的文件名。
- 参数 `category`：`basic` / `advanced` / `expert`（展示分层用）。

## 预设（preset）

- 存：`cwf run <id> --save-preset <名> --param k=v`（值取 `cli` 来源的合并结果）。
- 用：`cwf run <id> --preset <名>`（与 `--param` 叠加，`cli` 优先级最高）。
- 读：`cwf show <id>` 的 `presets` 键。

## 参数合并优先级

`default`（条目默认值） < `--preset`（按序叠加） < `--param k=v`（cli，最高）。
来源标记在 `run --dry-run` 的 `param_sources` 里。

## 旁路节点与图片参数约定

- `show`/`inspect` 里 `bypassed` 节点默认不执行；绑在旁路节点上的参数**显式给值**
  会自动启用该节点；图片参数**没给值**默认按"没有这张图"旁路。
- 图生图/图生视频流："传几张图"常常就是"给几个图片参数"。
- `run --enable <节点ID>` 强制打开旁路节点；`run --skip <节点ID>` 本次额外旁路。

## 队列

- `queue add <id> --param k=v [--preset N] [--tag T]` → `job_id`。
- `queue run [--max N] [--until-empty]` 前台消费；Ctrl+C 优雅停。
- `queue list [--state S]` / `queue show <job_id>` / `queue stats` / `queue retry --failed` /
  `queue cancel --all` / `queue rm <job_id>` / `queue pause|resume`。
- `batch <id> --queue` 把批量行投进队列（不立即跑）。

## 批量

- 驱动器三选一：`--matrix "k=a,b; k2=x,y"` / `--csv 文件` / `--jsonl 文件`。
- `--dry-run` 先看行；`--resume` 跳过已有成功记录的行；`--on-error continue|stop`。
- 汇总 JSON：`{batch_id, workflow_id, driver, total, submitted, succeeded, failed,
  skipped, items, results, failures, skipped_items}`；落地文件 `batch/<batch_id>.json`。

## 退出码（供判断）

| 码 | 含义 |
|---|---|
| 0 | 成功 |
| 1 | 一般错误 / 部分失败（BATCH_PARTIAL / QUEUE_PARTIAL） |
| 2 | 用法/参数/依赖缺失/未初始化（USAGE / NOT_FOUND / NOT_INITIALIZED） |
| 3 | 实例离线（OFFLINE） |
| 5 | 执行失败（EXEC_FAILED / TIMEOUT） |
| 7 | 需要确认但非交互（NEED_CONFIRM，加 `--yes` 或真交互） |
