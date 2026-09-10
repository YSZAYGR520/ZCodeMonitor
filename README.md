# ZCode Monitor — ZCode / GLM Coding Plan 用量监控悬浮窗

[![Platform](https://img.shields.io/badge/platform-Windows-blue)]()
[![Python](https://img.shields.io/badge/python-3.8%2B-3776ab)]()
[![Dependencies](https://img.shields.io/badge/dependencies-0-success)]()
[![License](https://img.shields.io/badge/license-MIT-green)]()

> A lightweight desktop usage monitor for ZCode / GLM Coding Plan — single-file EXE with
> zero dependencies, shows 5h/weekly quota, token distribution, 24h trends & model speed
> in a compact always-on-top panel. Built with pure tkinter, CLI tool included.

读取**智谱 ZCode / Z.ai / GLM coding runtime** 本机用量数据库(`~/.zcode/cli/db/db.sqlite`)，
输出统计的桌面监控面板。数据 100% 本地读取 + 官方额度接口(只读)，不修改 ZCode 数据。

## 界面形态
<img height="980" alt="2026-09-10_21-26-27" src="https://github.com/user-attachments/assets/30613647-d85d-4c6f-9396-0cc4c835ab48" />

| 形态 | 入口 | 说明 |
| --- | --- | --- |
| **① tkinter 悬浮窗(推荐)** | **`ZCodeMonitor.exe`**(12MB 单文件,零依赖) / `启动悬浮窗.bat` / `python zcode_monitor_tk.py` | **tkinter Canvas 自绘深色卡片界面**——不依赖 Python/WebView2/浏览器任何运行时,双击 EXE 即用;数据在后台线程读取,UI 永不卡死 |
| **② 命令行统计** | `一键运行.bat` / `python zcode_usage_cli.py` | 总览/按模型/按项目/费用估算，可输出 JSON |

> tkinter 版要点: 无边框置顶悬浮窗 · 顶栏拖动 · 时间窗切换(今日/7天/30天/全部) ·
> 最小化到任务栏(—按钮) · 渐变额度条(超阈值变琥珀/红) · 5h积分与速率同行 ·
> 深浅主题 · 设置(刷新间隔/预警阈值/主题/模块开关/窗口高度→注册表持久化) ·
> 窗口高度: 默认自适应内容(上限=屏幕工作区, 自动防盖任务栏), 也可在设置窗指定固定高度 ·
> **面板整体不滚动**, 模块完整展开; 仅「模型速度」卡内容超出时在卡内滚动(带细滚动条)。
> SQLite 用普通连接+`PRAGMA query_only` 读 WAL 库(`mode=ro` 会与写入方死锁,勿回退)。

## 快速开始

> 本仓库已附带打包好的 `ZCodeMonitor.exe`（12MB，Windows x64，无需任何环境），
> 克隆或下载本仓库后双击即可运行；无法运行时（如被安全策略拦截）再用源码方式。

```bash
# 方式一: 双击 ZCodeMonitor.exe 直接运行(仓库内附带, 无需任何环境)
# 方式二: 源码运行(需 Python 3.8+, 无需安装任何第三方库)
python zcode_monitor_tk.py

# 方式三: 自行打包独立 EXE(12MB, 目标机无需任何环境)
pip install pyinstaller
pyinstaller --onefile --windowed --name ZCodeMonitor --icon logo.ico zcode_monitor_tk.py

# 命令行统计
python zcode_usage_cli.py --window 7d --by-model
```

## 注意事项与已知限制

1. **数据来源**: 读取 `~/.zcode/cli/db/db.sqlite`(只读) + 官方额度接口
   (`open.bigmodel.cn`, Key 自动取自 `~/.zcode/cli/config.json`)。不上传任何数据。
2. **Windows 专用**: 悬浮窗的置顶/无边框/最小化用了 Win32 API, macOS/Linux 需自行适配。
3. **Smart App Control**: Win11 开启「智能应用控制」时可能拦截自打包的未签名 EXE,
   此时用 `python`/bat 方式运行即可; 正式分发需代码签名证书。
4. **上下文/MCP 数值**: 「上下文」为本地估算(当前会话输入token vs 模型上限,
   上限在 monitor.json 的 context_limits 配置); 官方接口不提供精确值, 仅供参考。
5. **外推口径**: 5 小时块内按当前速率线性外推, 假设速率不变, 属趋势参考。
6. **勿用 `mode=ro` 读库**: ZCode 的库是 WAL 模式, 只读 URI 连接在 Windows 上
   会与写入方死锁(本项目早期真实踩坑), 必须用普通连接 + `PRAGMA query_only`。
7. **配置持久化位置**: 注册表 `HKCU\Software\ZCodeMonitor`(设置面板写入);
   `monitor.json` 为默认值/单价/上下文上限, 与 exe 同目录或脚本同目录。
8. **单实例**: 重复启动会激活已有窗口而非叠新窗口。

## 开源说明

- 许可: MIT(可自由使用/修改/分发, 保留版权声明即可, 见 `LICENSE`)
- 欢迎 PR/issue; 改 UI 布局时请注意 tk 字体实际渲染框比字号大约 40%,
  行距建议 ≥ 字号×1.9, 或参考源码中的 bbox 检测思路自行验证。

## 悬浮窗功能

- **顶栏**：状态点(绿=库可读) · 时间窗切换(今日/7天/30天/全部) · 主题切换 ◐ · 设置 ⚙ · 最小化 — · 退出 ✕ · 按住空白处拖动
- **KPI 卡**：有效请求 / 输入 tok / 输出 tok(含思考) / 活跃会话
- **官方额度卡**：5h 积分+余量+百分比(与速率/外推同行) · 渐变进度条(超阈值变琥珀/红) ·
  重置倒计时 · 本周进度 · 上下文占比 · MCP 月度(若有)；峰谷徽章(高峰全额/非高峰5折)
- **算力分布**：按模型 token 占比彩色条形 + 主/子智能体来源
- **24h 趋势**：官方/本地(回退)折线图 + 峰值/总量 + MCP 本月/24h 行
- **模型速度**：每模型 加权速度(红<30/黄30-80/绿>80) · 缓存命中率(⚡%, tok/s 右侧) ·
  首token · 耗时 · 输入/输出(左右分栏, ↓输入青/↑输出绿) · 缓存(命中均/未命中均)；
  **卡内独立滚动区**（像素级丝滑滚动，滚轮只滚卡片内容、不影响其他模块；右侧细滚动条可拖动）；
  模型名截断时**悬停显示全名**；
  点右上角「请求日志 ↗」弹出**独立请求日志悬浮窗**（8 列: 时间/模型/状态/耗时/输入/输出/命中率/命中-未命中，
  **50 条记录 + 表头固定 + 数据区滚动**；可拖动、可关闭，不关则随主程序一直显示）
- **额度预警**：越过警告/危急阈值时，顶栏标题旁显示**红点**（危急=红 / 警告=琥珀，
  恢复正常自动消失；常驻状态提示，不打扰使用）
- **设置窗**：刷新间隔 / 预警阈值 / 深浅主题 / 模块开关 / **窗口高度**（0=自动, 或指定像素上限；
  过小会自动补足到可行高度）→ 保存注册表, 重启保留
- **窗口**：无边框置顶 · 高度自适应内容(上限=工作区, 防盖任务栏; 可自定义) ·
  面板整体不滚动(模块全展开)、模型速度卡内容超出时卡内滚动 · 最小化到任务栏
  （最小化期间暂停刷新省资源，还原立即刷新） · **窗口位置记忆**(重启回到上次位置) · 单实例

## 它读的是什么

ZCode 把会话/任务用量写在本机 SQLite `~/.zcode/cli/db/db.sqlite`（桌面 App 的
`~/Library/Application Support/ZCode` 只存 Electron 状态；CLI 的 `*.jsonl` 会脱敏 token，都不含用量）。
有效表：

| 表 | 含义 | 关键列 |
| --- | --- | --- |
| `model_usage` | 每次 LLM 请求(含子 agent / thinking) | 各类 token、`duration_ms`、`time_to_first_token_ms`、时间戳、`status` |
| `tool_usage` | 每次工具(命令 / extension)执行 | `duration_ms`、`status` |
| `turn_usage` | 每一「轮」的聚合 | token、耗时、`model_request_count`、`tool_call_count` |
| `session` | 会话；`interactive` 顶层任务、`subagent_child` 子智能体(`parent_id`) | `id/parent_id/directory/title` |

口径：**只计入 `status='completed'`**；「一次顶层任务」= 一个 `interactive` 会话 + 其全部后代
子智能体会话，用量递归求和（与社区 zcode-dashbord 做法一致）。

## 指标口径（第三方近似，非官方内置页）

| 输出项 | 计算方式 |
| --- | --- |
| 轮 rounds | `turn_usage` 行数 |
| 步 steps | `tool_usage` 行数（每次工具执行算一步） |
| LLM 时长 | `SUM(model_usage.duration_ms)` |
| 工具调用时长 | `SUM(tool_usage.duration_ms)` |
| 首 token 平均 | `AVG(time_to_first_token_ms)`（仅 completed） |
| tok/s | `(Σ输入+Σ输出) / LLM秒` |
| 缓存命中 | `Σcache_read / Σinput_tokens`（input 已含缓存读） |
| 输入/输出 tok | `SUM(input_tokens)` / `SUM(output_tokens)` |
| 估算费用 | fresh输入×input价 + 缓存读×cache_read价 + 缓存写×cache_write价 + 输出×output价 |

### 监控面板口径(对齐社区 [zcode-monitor](https://github.com/yiyanwannian/zcode-monitor))

把每次 `model_usage`（completed）作为一个样本，按模型给出分布：

| 面板指标 | 口径 |
| --- | --- |
| 生成速度 tok/s | `(输出+思考token) / 总耗时`——思考计入产出，分母含首 token 等待 |
| 加权平均 | `Σ(输出+思考) / Σ耗时`（token 加权，与逐请求平均不同） |
| 速度三档着色 | <30 红 / 30-80 黄 / >80 绿 |
| 首次token(秒) | `time_to_first_token_ms` 的 平均/中位/P90 |
| 请求耗时(秒) | `duration_ms` 的 平均/中位/P90/最长 |
| 单次输出 token | `输出+思考token` 的 平均/最大 |
| 单次输入 token | `input_tokens`（含缓存读）的平均/最大 |
| 算力分布 | 按模型 `Σcomputed_total_tokens` 占比条形图；主任务/子智能体按 `query_source` 计数 |
| KPI 总览 | 有效请求 / 输入输出 tok 合计 / 活跃会话数（DISTINCT session_id）/ 错误数 |
| 5h 区块 | 相邻请求间隔 <5h 归同一区块，窗口=首请求起 5 小时（ccusage 口径）；倒计时=窗口结束-当前 |
| 消耗速率/外推 | 速率=区块token/已耗时；外推=按当前速率折算整段 5h 的积分占比 |
| 每周/上下文 | 每周=自然周(周一00:00)聚合；上下文=最近一次请求 input_tokens ÷ monitor.json 里该模型上限 |

## monitor.json（额度监控配置）

与 exe/脚本同目录，可直接用文本编辑器修改；改完下一轮刷新(10s)生效：

```json
{
  "plan": "pro",
  "credits_per_request": 7,
  "week_start": "monday",
  "refresh_seconds": 10,
  "warn_pct": 85,
  "crit_pct": 95,
  "theme": "dark",
  "context_limits": { "glm": 1000000, "deepseek": 128000, "kimi": 256000 }
}
```

- `plan`：`lite` / `pro` / `max` / `none`——决定 5 小时/每周积分上限
  （Lite 2000/1万 · Pro 1.2万/6万 · Max 2.8万/14万，参照智谱官网套餐）
- `credits_per_request`：积分估算系数，**已按官方页校准为 7**（一次模型调用≈7积分；
  官方"600 prompts=12000积分"里的 prompt 是用户消息，一次会触发多次模型调用）
- `week_start`：每周窗口锚点，`monday`（自然周一，默认）或 `rolling7d`（滚动 7 天），
  按官方周重置日对不上时切换试试
- `refresh_seconds`：**悬浮窗自动刷新间隔（秒），2–3600，默认 10**——
  也可在悬浮窗 ⚙ 设置窗调整（保存到注册表，优先于本文件）
- `warn_pct` / `crit_pct`：额度预警 / 危急阈值百分比（默认 85 / 95），
  进度条与数值越过阈值时变琥珀/红
- `theme`：`dark`（深色）/ `light`（浅色），默认 dark——悬浮窗 ◐ 按钮切换并写注册表
- `modules`：**悬浮窗显示的板块**（数组，顺序即从上到下）——
  `kpi`(KPI总览) / `quota`(官方额度) / `dist`(算力分布) / `trend`(24h趋势) / `models`(模型速度)；
  默认全开。⚙ 设置窗勾选开关（保存到注册表，优先于本文件）
- `context_limits`：模型名关键词 → 上下文 token 上限，用于估算上下文进度
- 也可直接加 `"block_credits": 12000, "week_credits": 60000` 覆盖档位数值
- **改完自动生效**（每轮刷新重读配置）
- 若百分比与官方页出现偏差，重校公式：`系数 = 官方百分比 × 档位积分上限 ÷ 窗口内请求数`
  （悬浮窗上可以看到区块/本周的请求数）

> ZCode 官方「使用统计」是核心功能、不对第三方开放精确口径，故部分指标（步数、tok/s、LLM 时长）
> 可能与官方显示有差异；首 token、缓存命中、工具时长、输入/输出 token 等与官方高度一致。

## 命令行统计

```
python zcode_usage_cli.py                     # 累计 + 最近任务摘要
python zcode_usage_cli.py --window 7d         # today | 7d | 30d | all
python zcode_usage_cli.py --session <会话id>   # 只看某会话(含其子会话)
python zcode_usage_cli.py --json              # 机器可读 JSON
python zcode_usage_cli.py --by-model          # 按模型明细
python zcode_usage_cli.py --by-project        # 按项目(会话目录)明细
python zcode_usage_cli.py --cost              # 估算费用(需 pricing.json)
python zcode_usage_cli.py --pricing <路径>     # 指定单价表
python zcode_usage_cli.py --help
```

## 费用估算与 pricing.json

单价表默认取 **exe/脚本同目录下 `pricing.json`**，可用 `--pricing` 覆盖。格式：

```json
{
  "currency": "USD",
  "models": {
    "deepseek-v4-pro-0813": { "input": 2.0, "cache_read": 0.5, "cache_write": 2.0, "output": 8.0 }
  }
}
```

- 字段单位 `$ / 1M tokens`：`input`=未命中缓存的新输入；`cache_read`=命中缓存读；`cache_write`=缓存写入；`output`=输出。
- GLM Coding Plan 套餐内调用通常含在订阅(边际≈0)，可不配或填 0。
- 未配置单价的模型费用按 0 计，并在输出标「未配置单价」。内置单价均为估算/占位，请按实际改。

## 目录结构

```
├─ zcode_monitor_tk.py     # 悬浮窗主程序(tkinter, 入口)
├─ zcode_usage.py          # 核心库:读库/分组/费用/分布统计/官方接口/额度
├─ zcode_usage_cli.py      # 命令行入口
├─ ZCodeMonitor.exe        # 打包好的单文件 EXE(12MB, 可直接双击)
├─ logo.ico                # 程序图标(窗口/任务栏/EXE)
├─ 启动悬浮窗.bat          # 悬浮窗启动(优先 EXE, 回退 pythonw)
├─ 一键运行.bat            # CLI 启动入口
├─ monitor.json            # 监控配置(额度档位/单价系数/上下文上限)
├─ pricing.json            # CLI 费用单价表(可改)
├─ LICENSE                 # MIT 许可证
├─ TECH_STACK.md           # 技术栈决策 + 数据源权威清单(面向二次开发)
├─ RELEASE_NOTES.md        # 发布说明
└─ README.md
```

## 性能说明

库可高达数百 MB，工具以 SQLite **普通连接 + `PRAGMA query_only`** 直连读取
（不用 `mode=ro`——WAL 库下会与写入方死锁），不做整库拷贝；
本地统计单次几十毫秒；官方额度/趋势两个网络请求并行执行且有 60s 缓存，
首屏约 1~2 秒出数据，不轰炸官方服务器。

## 重新打包 exe

需要 Python 3.8+（仅标准库，无需 pip 装任何运行依赖；打包工具本身除外）：

```bash
pip install pyinstaller
pyinstaller --noconfirm --onefile --windowed --name ZCodeMonitor --icon logo.ico zcode_monitor_tk.py
# 产物 dist/ZCodeMonitor.exe ≈12MB; 把 monitor.json / pricing.json 放在 exe 同目录
```

> ⚠️ 若本机 Device Guard 策略拦截新 exe，请直接双击 `启动悬浮窗.bat`（走 python）运行悬浮窗。
> 打包后记得把 `pricing.json` 放在 exe 同目录，`--cost` 才能自动读到。

## GitHub 发布信息（仓库元数据, 建仓库时直接复制）

**仓库描述（About 栏）：**

> ZCode / GLM Coding Plan 桌面用量监控悬浮窗——零依赖单文件 EXE，本地读取用量数据 + 官方额度接口，实时展示 5 小时/周额度、算力分布、24h 趋势与模型速度。tkinter 实现，附带命令行统计工具。

英文版：

> A lightweight desktop usage monitor for ZCode / GLM Coding Plan — single-file EXE with zero dependencies, shows 5h/weekly quota, token distribution, 24h trends & model speed in a compact always-on-top panel. Built with pure tkinter, CLI tool included.

**Topics 标签：**

```
zcode  glm  zhipu-ai  bigmodel  usage-monitor  dashboard  tkinter  python  desktop-widget  floating-window  quota-tracker  sqlite
```

**Release 发布说明：** 见 `RELEASE_NOTES.md`。
