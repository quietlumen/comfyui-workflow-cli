# cwf —— ComfyUI 工作流命令行调用器（agent skill 版）

把本地 ComfyUI 实例的工作流变成"可确认、可参数化调用"的服务。本目录是**源码与外壳一体的
可分发包**：`src/cwf/` 是内核实现，`scripts/` 是命令外壳，安装后即可独立工作。

## 目录结构

```
cwf/
├── SKILL.md                  # agent 使用说明（能力、流程、只读约束）
├── README.md                 # 本文件（安装/配置/门禁/常见问题）
├── src/cwf/                  # 内核源码（只读，agent 不得修改）
├── scripts/
│   ├── setup.py              # 一次性配置：建 .venv + 在线装依赖 + cwf init + 浏览器配置
│   └── cwf_cli.py            # 命令入口：门禁检查 → 调内核 → stdout 单 JSON
└── references/
    └── data-and-commands.md  # 各命令 --json 返回字段速查
```

运行时产物（不进版本库，由 setup 生成）：`.venv/`、`skill_env.json`。

## 安装 / 配置（一次性）

```
python scripts/setup.py --workspace D:\xxx --instance-dir D:\ComfyUI\ComfyUI
```

- `--workspace`（**必填，用户提供**）：工作区目录。**日志、运行记录、产物、队列、台账
  全放这里**（skill 本身只放源码和 `.venv`）。
- `--instance-dir`：ComfyUI 安装目录；不给也能后续用 `cwf init` 补。
- 依赖用 pip 在线安装（typer / rich / httpx / pydantic / playwright），需能访问 PyPI。
- 幂等：重复跑复用已有 `.venv` 与配置。
- 完成后生成 `skill_env.json`（命令入口 python + 用户提供的工作区路径）。

## 使用

```
python scripts/cwf_cli.py <command> [args]
```

- `--workspace` 缺省用配置值；显式给出则覆盖。
- stdout 恒为单个 JSON（`ok` 标记成败），日志/错误走 stderr。
- 常用命令：`status` / `sync` / `convert` / `list` / `show` / `inspect` / `param` /
  `run` / `batch` / `queue` / `history` / `log` / `upload`。
- 完整用法见 `SKILL.md`；返回字段见 `references/data-and-commands.md`。

## 门禁（没有配置/初始化就拦）

| 状态 | 行为 |
|---|---|
| 未配置（缺 `skill_env.json` 或 `.venv`） | 除 `--help`/`--version` 外一律拦截，返回 `NOT_CONFIGURED`，提示先跑 `setup.py` |
| 已配置但工作区未初始化（缺 `config.json`） | 除 `init` / `--help` / `--version` 外一律拦截，返回 `NOT_INITIALIZED`，提示先 `setup.py` 或 `cwf init` |
| 已配置 + 已初始化 | 正常执行 |

即：**拿到本 skill 后，第一件事永远是 `setup.py`**；初始化完成前，`list`/`show`/
`sync` 等工作流相关命令都不可用。

## 底层源码：只读

- `src/cwf/**` 只允许读取；发现问题**不要直接改**，记录到工作区 `logs/` 并汇报，
  等用户明确指示后再动。
- 使用配置（`skill_env.json` / 工作区 `config.json` / 参数绑定 / 预设）可通过命令正常改。
- 例外：用户要求修 bug / 加功能；`setup.py` 环境初始化（只动 `.venv/` 与 `skill_env.json`）。

## 工作区布局（日志/记录/产物）

```
<workspace>/
├── config.json        # 实例与捕获配置
├── workflows/         # 台账：meta / source / api / params / presets / runs
├── outputs/           # 下载回工作区的图片/视频产物（<id>/<run_id>/）
├── queue/             # 任务队列事件流
├── batch/             # 批量汇总报告
├── cache/             # object_info 缓存
├── rules/             # 参数规则
└── logs/              # --debug 日志
```

## 常见问题

- **在线装依赖失败**：确认能访问 PyPI；`setup.py` 会给出 pip 报错。
- **转换失败提到浏览器**：默认 `capture.browser=chromium` 需下载；setup 会自动探测系统
  Chrome/Edge 并切换成 `chrome`。手动：改工作区 `config.json` 的 `capture.browser`。
- **没初始化就报 NOT_INITIALIZED**：先 `setup.py` 或 `cwf init --workspace <目录>`。
