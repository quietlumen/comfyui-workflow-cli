---
name: cwf
description: 通过 cwf 命令行工具把本地 ComfyUI 变成可参数化调用的服务。初始化实例配置、同步/导入/转换工作流、为工作流绑定参数（得到可通过 API 调用的工作流+参数规范），再按功能类型（文生图/图像编辑/文生视频/图生视频/放大）挑选工作流并运行、回收产物。用户提出"初始化/配置/连接 ComfyUI""导入/同步/转换工作流""绑定参数/有哪些参数""用 z_image_turbo、qwen image、minimax、wan 等模型或工作流出图""跑文生图/图生图/文生视频/图生视频""查 ComfyUI 状态、运行记录"时使用。
---

# cwf —— ComfyUI 工作流命令行调用器

本技能**自带 cwf 内核源码**（`src/cwf/`），安装后即可独立工作，不依赖外部项目。
所有数据都通过 `cwf` 命令获取（**不直接读文件、不自己解析工作流 JSON**）——命令的
`--json` 输出是唯一事实来源。脚本目录只是薄封装：用 skill 自己的解释器跑内核、统一
补 `--json`，其余全交给 cwf。

## 安装后的第一步：agent 自己发现需要配置

安装本 skill 后，**agent 首次使用前必须先自查并引导用户配置**，流程固定如下：

1. **自查**：检查 skill 目录里 `skill_env.json` 是否存在、`.venv/Scripts/python.exe` 是否可用。
   - 两者都在 → 已配置，跳过下面步骤，直接进入"调用约定"。
   - 缺任一 → **未配置**，禁止直接调任何命令（`cwf_cli.py` 的门禁也会拦）。
2. **发现需要 setup 后，用 `ask_user_question` 弹窗向用户要两样东西**（不要静默选目录）：
   - `--workspace`：**必问**——工作区目录（日志/运行记录/产物/队列/台账放这里，用户给哪个用哪个）；
   - `--instance-dir`：**建议问**——ComfyUI 安装目录（不知道就让用户先 `cwf status` 或提供路径；不给也能后续补）。
3. **把用户回答拼进 setup 命令再执行**：
   ```
   <python> "<skill_dir>/scripts/setup.py" --workspace <用户给的目录> --instance-dir <用户给的 ComfyUI 目录>
   ```
   （终端下用户也可以直接跑 setup.py 交互输入；agent 场景一定用 `ask_user_question` 先收集，再把值当参数传。）
4. setup 完成（生成了 `skill_env.json`）后再进入正常调用。setup 失败就把 stdout/stderr 如实汇报，让用户修。

## 安装（一次性）

在 skill 目录跑配置脚本——它会在 skill 里建 `.venv`、**在线安装依赖**，并初始化工作区：

```
<python> "<skill_dir>/scripts/setup.py" --workspace D:\xxx --instance-dir D:\ComfyUI\ComfyUI [--host H --port P]
```

- **`--workspace`（必填）**：由**用户提供**的工作区目录。**日志、运行记录、产物、
  队列、台账全放这里**（skill 本身只放源码和 `.venv`）。用户给哪个就用哪个。
- **`--instance-dir`**：ComfyUI 安装目录（`cwf init` 会用；不给也能后续 `cwf init`）。
- 依赖用 pip 在线安装（`typer` / `rich` / `httpx` / `pydantic` / `playwright`），
  需要目标机器能访问 PyPI。
- 幂等：重复跑会复用已有的 `.venv` 与配置。
- 配置结果写在 skill 根的 `skill_env.json`（`.venv` 的 python + 用户提供的工作区路径）。

## 调用约定

- `scripts/cwf_cli.py` 每次读 `skill_env.json`：用 skill 的 `.venv` python 跑
  `python -m cwf`，`PYTHONPATH` 指向 skill 的 `src/`。未配置（没跑 setup）时明确提示。
- `--workspace` 缺省用配置里的值；显式给了就覆盖（可临时指到别的目录）。
- 追加 `--json` 使 stdout 是单个 JSON（`ok` 字段标记成败）。**解析 stdout JSON，
  不要正则刮文本**；日志/错误走 stderr。
- 需要确认的关口（首次运行、高成本任务、非交互环境）用 `--yes` 跳过，**但只在你已
  向用户展示过"将要提交什么"之后**。

```
<python> "<skill_dir>/scripts/cwf_cli.py" [--workspace <ws>] [--json] <command> [args]
```

> **布局**：`src/cwf/` = 内核源码；`.venv/` = 本 skill 自己的解释器（依赖在线安装）；
> `skill_env.json` = 命令配置（python 入口 + 工作区）。工作区里的 `config.json` 记录
> ComfyUI 实例与 `capture.browser`（默认 chromium，本机装了系统 Chrome 可改成
> `chrome` 免下载浏览器）。

## 能力分两层

### A 层：把工作流变成可调用的服务（一次性准备）

| 命令 | 作用 |
|---|---|
| `cwf init --instance-dir <dir> [--host H --port P]` | 初始化工作区与实例配置（离线也能成功） |
| `cwf status` | 实例在线/版本/显存/队列、台账条数/状态分布、已绑参数、浏览器就绪 |
| `cwf sync [--convert] [--adopt NEW=OLD]` | 扫实例目录收进台账；源文件变更标 `stale` |
| `cwf convert <id> [--force]` / `cwf convert --all` | UI→API 转换（写 `api.json` + `api.full.json`） |
| `cwf inspect <id> --suggestion` | 看候选输入与推荐参数名（绑定前的依据） |
| `cwf param add <id> <name> --key "Class#input#0"` | 把候选输入绑成用户接口参数 |
| `cwf param list <id>` / `cwf param set <id> <name> ...` | 参数清单；改默认值/类型/分类 |
| `cwf set <id> --function <类型>` | 人工纠正功能分类（`list` 的分组依据） |

准备完成的标准：`cwf list` 里出现 `ready` 状态、有 `params` 的条目——它就是"可以
通过 API 调用"的工作流。

### B 层：挑选并调用（日常）

| 命令 | 作用 |
|---|---|
| `cwf list [--type text_to_image\|image_edit\|video\|upscale] [--status ready]` | 按功能/状态列可用工作流 |
| `cwf show <id>` | 条目详情：状态、参数、预设、最近运行（含 `presets` 键） |
| `cwf run <id> --param k=v ... [--preset N] [--dry-run] [--out 目录] [--yes]` | 组包→确认→提交→等完→下载产物→写运行记录 |
| `cwf run <id> --param 图片参数=本地路径` | 图片参数传本地路径会自动上传（`--dry-run` 里会显示"待上传"） |
| `cwf batch <id> --matrix "k=a,b"` / `--csv` / `--jsonl` | 批量跑一个流，失败隔离，`--resume` 续跑 |
| `cwf queue add <id> --param ...` + `cwf queue run [--max N]` | 排队消费（前台进程） |
| `cwf history [id]` / `cwf log <run_id>` | 运行记录 / 单次完整记录（含产物路径、node_errors） |
| `cwf upload <图>` | 上传图片到实例 input，拿到可直接当参数值的名字 |
| `cwf param list <id>`（读时自检） | `type_drift`：参数类型与节点声明不一致的清单 |
| `cwf inspect <id> --warnings` | 转换警告 + 读时漂移（`node_drift`/`type_drift`） |

## 会话流程

### 首次准备（用户说"初始化/导入/转换/绑定"时）

1. `cwf init --instance-dir <用户给的 ComfyUI 安装目录>`（不知道目录就先 `cwf status`
   或问用户；离线初始化也能成功）。
2. `cwf sync` 把实例目录里的工作流收进台账；`cwf status` 汇报结果。
3. 逐条或批量 `cwf convert <id>`（或 `cwf convert --all`）转成 API 版本。
4. 对要用的工作流：`cwf inspect <id> --suggestion` 看推荐参数 → `cwf param add <id> <名>
   --key "<语义键>"` 把常用输入绑成参数（提示词/种子/宽高比/像素/图片输入等）。
5. 人工纠错：`cwf set <id> --function <类型>` 修正 `list` 分组；`cwf param set --type`
   修参数类型漂移（`param list` 会提示）。

### 日常调用（用户说"跑个文生图/视频"时）

1. **按用户要的类型列候选**：
   - 文生图 → `cwf list --type text_to_image --status ready`
   - 图像编辑/图生图 → `cwf list --type image_edit --status ready`
   - 文生视频/图生视频 → `cwf list --type video --status ready`
   - 放大 → `cwf list --type upscale --status ready`
2. **把候选列给用户选**（编号 + 名字 + 参数数 + 最近运行），别擅自挑一个就开跑；
   用户点名某模型（"用 z_image_turbo"）时直接匹配名字。
3. 选定后 `cwf show <id>` 拿参数规范，把用户意图映射到参数：
   - 提示词 → 该流的 `prompt`/`positive_prompt` 参数；分辨率/比例 → `width`/`height`/
     `megapixels`/`aspect_ratio`（比例要换算，注意模型的整除/范围约束，参数带
     min/max/options 时先校验）。
   - 图生图/图生视频的输入图 → 先把本地图 `cwf upload`（或直接把本地路径给图片参数，
     `run` 会自动上传），把返回名字填给图片参数。
   - 种子/步数/CFG → 对应参数；想随机种子用 `--random-seed`。
4. **提交前展示"将要应用的参数清单"**（必要时先 `cwf run <id> --dry-run` 给用户看
   payload 与参数来源）；视频、高分辨率、大 batch 等必须先得到用户确认。
5. `cwf run <id> --param ... --yes` 提交；等完下载产物到 `outputs/<id>/<run_id>/`。
   用 present/图片语法把产物给用户看；视频产物同样展示文件路径。
6. 失败时如实报告 JSON 里的 `error`/`node_errors`，给出可操作建议（缺模型/节点 → 提示
   补装后 `cwf convert <id> --force`；参数类型/范围错 → 看 `param list` 的漂移提示）。
   不要静默重试烧显存。

### 批量与队列（用户说"跑一批/排队"时）

- 小批量：`cwf batch <id> --matrix "prompt=a,b" --yes`（或 `--csv`/`--jsonl`），
  失败隔离，跑完给 `results`/`failures` 明细；`--resume` 续跑跳过已成功行。
- 想排队不占前台：`cwf batch <id> ... --queue --yes` 投递 → `cwf queue run --max N`
  消费（前台进程）；`cwf queue stats` 看状态。

## 规则

- **数据一律走命令**：`list`/`show`/`inspect`/`param list` 的 `--json` 是事实来源；
  本技能不直接读 `data/workflows/*/meta.json` 之类的文件。
- 工作区默认 `D:\comfyui_workflow\data`；每次调用都显式传 `--workspace`，不靠环境变量。
- 只做用户点名的事：不自动转换全部、不自动改参数、不自动清理产物/运行记录。
- 需要确认的关口：首次运行一个未验证的转换结果、高成本任务（视频/大 batch/高分辨率）、
  批量超过阈值。展示确认内容后再用 `--yes`。
- 非交互环境（skill 里）一律 `--yes`，但前提是**你先把将提交的内容展示给用户**。
- 离线时 `list`/`show`/`inspect` 照常可用（只读台账）；`run`/`convert`/`upload` 需要
  实例在线，失败信息会明说。

## 底层源码：只读，绝不自动修改

- `src/cwf/**` 是内核实现，**只允许读取**：遇到源码疑似 bug、命令行为不符、或想
  加功能时，**不要直接改代码**。
- 发现问题：把现象记录到工作区的 `logs/`（例如 `cwf log` / `--debug` 的堆栈），
  并在给用户的汇报里说明"问题记录在 <路径>"，等用户明确指示后再动源码。
- 配置层（`skill_env.json` / 工作区 `config.json` / 参数绑定 / 预设）是**使用配置**，
  不在"只读"之列，可以通过命令正常改（`cwf init` / `param set` / `run --save-preset`…）。
- 只有两个例外可以动源码：
  1. 用户直接要求修 bug / 加功能；
  2. 运行环境初始化（`scripts/setup.py` 建 `.venv`、装依赖、写 `skill_env.json`）——
     这只动 `.venv/` 与 `skill_env.json`，不碰 `src/cwf/**`。

## 结果怎么判断是否正确

每次 `run` 的 `--json` 直接给 `status` / `artifacts` / `error` / `node_errors`；
`log <run_id>` 有完整记录（含这次真正发出去的 `submitted_prompt`、参数来源
`param_sources`、产物路径）。**判定规则**：

| 迹象 | 结论 |
|---|---|
| `status=success` 且 `artifacts` 非空、文件存在 | **成功**——产物路径直接 present |
| `status=failed` | **失败**——`error` + `node_errors`（哪个节点、什么错）如实汇报 |
| `status=timeout` | **超时**——`error` 说明等了多久 |
| `status=success` 但 `artifacts` 为空 | **疑似空跑**——ComfyUI 也会对"没执行的图"回 success；检查 SaveImage 是否被旁路、节点是否真的跑（`execution_cached` 全节点 = 完全缓存没干活） |
| 参数被 `--set` 改坏 / 模型名不存在 | 提交阶段多数会 400 + `node_errors`（`value_not_in_list` 等），`run --json` 直接给出；执行期失败走 `status=failed` |

- **产物落地核对**：`artifacts[].local_path` 存在、非空、PNG 能读宽高。把产物用
  present/图片语法给用户看，是"结果正确"最直接的证据。
- 判定辅助代码见 `src/cwf/judge.py`（`judge_run_result` / `check_artifact`）。

## 变更与记录

- 源文件变了（`sync` 标 `stale` / 列表带 `!`）→ 提醒用户，经确认再
  `cwf convert <id> --force` 重建；绑定参数保留，`verified` 重置需重新确认。
- 每个 `run` 都在 `workflows/<id>/runs/` 留完整记录，`history`/`log` 可查。
- 参数语义键（`Class#input#0`）在节点 ID 变化时由 cwf 读时自检报告
  （`inspect --warnings` 的 `drift` 块），`cwf convert <id> --force` 会把定位写回去。

参数/数据模型细节见 [references/data-and-commands.md](references/data-and-commands.md)。
