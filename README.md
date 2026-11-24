---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 2cf0af362bda02c9ebee3360cad8ea10_07b1e80ee73c11f1a9b8525400f8a581
    ReservedCode1: VOBeHRJfFsj7iChzRvNwERAOQoFQJwLXyL/Wb6/aAcGTMJ/h9X7rU4zsGn0d+F6m/NBCKVv3Xd1uThgoGLTBCb3zkNBYwwBHKIzV56faurlEaSSPSGVU35Ki2oSaUgxtvtRMl2B4zDecLMl6mfS68NpqPJ/LwJ5FuVpI68T/5IXpJ+8O8pEU9ujSlkw=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 2cf0af362bda02c9ebee3360cad8ea10_07b1e80ee73c11f1a9b8525400f8a581
    ReservedCode2: VOBeHRJfFsj7iChzRvNwERAOQoFQJwLXyL/Wb6/aAcGTMJ/h9X7rU4zsGn0d+F6m/NBCKVv3Xd1uThgoGLTBCb3zkNBYwwBHKIzV56faurlEaSSPSGVU35Ki2oSaUgxtvtRMl2B4zDecLMl6mfS68NpqPJ/LwJ5FuVpI68T/5IXpJ+8O8pEU9ujSlkw=
---


# 同花顺做T半自动辅助工具（THS-T-Assistant v1.0.0）

监控同花顺远航版窗口 + 新浪实时行情，按配置阈值给出**正T（先买后卖）/ 反T（先卖后买）**信号；信号触发时弹置顶确认窗（10 秒倒计时），**只有你在弹窗上点了“确认下单”，脚本才会用 pyautogui 模拟点击同花顺下单界面**。默认 `dry_run=true`，只走流程不动鼠标。

> 风险声明：本工具为**半自动辅助**，不构成投资建议。真实下单模式会操控鼠标键盘，可能造成真实成交与亏损。首次实盘请务必用最小数量（100 股）验证，并全程盯屏。任何信号/成交结果以券商与同花顺客户端为准。

---

## 1. 文件清单

| 文件 | 作用 |
| --- | --- |
| `main.py` | 主程序（行情抓取 / 信号引擎 / 窗口监控 / 弹窗确认 / 模拟下单 / 审计日志 / 坐标拾取） |
| `config.json` | 全部参数与阈值（标的、持仓、阈值、轮询、冷却、坐标、dry_run 等） |
| `start.bat` | 双击启动，自动 `cd` 到脚本目录、缺依赖时自动 pip 安装 |
| `README.md` | 本文档 |
| `%LOCALAPPDATA%\ths_t_trader\logs\audit_trade.csv` | 审计日志：信号快照 / 用户选择 / 模拟点击坐标与结果（Excel 可直接打开） |
| `%LOCALAPPDATA%\ths_t_trader\logs\runtime.log` | 运行日志：启动、每轮行情快照（DEBUG）、窗口状态、下单过程 |
| `%LOCALAPPDATA%\ths_t_trader\data\signal_state.json` | 当日信号台账（闭环锁 / 批次 / 冷却计数），重启后自动恢复 |

> 运行数据（日志、审计 CSV、截图、台账）统一放在用户级数据目录，**不写在 exe 同目录**：exe 放在桌面、U 盘、
> Program Files 或由快捷方式 / 计划任务拉起时都能正常写入；单文件 exe 的解包临时目录（`_MEIxxxx`）退出即被清理，
> 台账放那里会让闭环锁与批次每次启动归零。数据根目录可用环境变量 `THS_T_DATA_DIR` 覆盖，
> 日志目录可在 `logging.log_dir` 填绝对路径单独指定；相对路径以数据根目录为基准。

依赖（`start.bat` 会自动安装）：`requests`、`pyautogui`、`pillow`、`psutil`、`pywin32`。Python 3.9+（实测 3.11.8）。

---

## 2. 快速开始

```bat
:: 方式一：双击 start.bat（推荐，等价于 python main.py）
:: 方式二：命令行
cd /d <本目录>
pip install requests pyautogui pillow psutil pywin32
python main.py                 :: 正式运行：常驻监控 GUI
```

其他命令行用法：

```bat
python main.py --once                    :: 抓一次真实行情 + 打印每条判定依据后退出（不弹窗、不下单）
python main.py --selftest                :: 自检：注入一条信号，走完 弹窗→倒计时→忽略 链路
python main.py --selftest --auto-confirm :: 自检：3 秒后自动点“确认”，验证下单执行链路（dry_run 下仅打印动作）
python main.py --detect --write            :: 自动识别下单界面买卖两侧共 8 个点击坐标并写入 config.json（不需按键）
python main.py --config other.json       :: 使用别的配置文件
```

**建议使用顺序**：`--once`（确认行情通）→ `--selftest`（确认弹窗/日志通）→ `--detect --write`（自动识别坐标）→ 保持 `dry_run=true` 盘中实跑几天 → 才考虑切真实下单。

---

## 3. 实时行情（新浪接口）

- 地址：`https://hq.sinajs.cn/list=sh600519,sz000001`（`market.sina_url` 可改），必须带请求头 `Referer: https://finance.sina.com.cn`，返回 **GB18030** 编码，脚本已固定处理。
- 解析字段：名称、现价、今开、昨收、最高、最低、成交量（股）、成交额（元）、买一~买五价量、卖一~卖五价量、行情日期时间、停牌/状态标记。
- 派生指标：
  - **分时均价线** = 成交额 ÷ 成交量（VWAP，实测 `4316023208 / 2984695 = 1446.052`）
  - **日内振幅** = (最高 − 最低) ÷ 昨收
  - **当日涨跌幅** = (现价 − 昨收) ÷ 昨收
  - **相对持仓成本盈亏** = (现价 − 成本) ÷ 成本
  - **偏离均价** = (现价 − 均价) ÷ 均价（信号判定核心指标）
- 轮询：盘中 `market.poll_interval_sec`（默认 3 秒）；非交易时段自动降频为 `offhours_poll_interval_sec`（默认 10 秒）。
- 实测样例（2026-11-18 收盘后）：`sh600519 贵州茅台 现价 1442.66 (-1.03%) 均价 1446.05 偏离 -0.23% 高 1456.00 低 1431.70 量 298.47万 额 43.16亿 振幅 1.67% 行情时间 15:34:59`。

> 注：收盘后新浪返回的是当日收盘静态快照，价格不再变化，此时看不出信号跳动属正常。

---

## 4. 信号引擎

两类信号，阈值全部在 `config.json` 的 `signals` 段：

**正T·先买后卖（BUY_FIRST）** — 低吸摊低成本，判定条件（全部满足才触发）：

| 条件 | 参数 | 默认值 | 含义 |
| --- | --- | --- | --- |
| 开关 | `buy_first.enabled` | true | — |
| 交易时段 | `trade.sessions` | 09:15-11:30 / 13:00-15:05 | 时段外不产生信号 |
| 标的支持 T+0 | `trade.allow_stock_t_plus_1` | false | 代码 15/16/51/56/58 开头视为场内基金可 T+0；股票默认不放行（当日买入不可卖，正T无法闭环，底仓套利请走反T） |
| 低于均价线 | `below_avg_pct` / `dev_ticks` | 0.30 / 5 | 百分比与跳数两套阈值取更严的一方。0.795 的 ETF 一跳 0.001≈0.13%，0.30% 不足 3 跳，故以 5 跳（≈0.63%）为准 |
| 未破支撑 | `support_price` / `support_break_pct` | 0 / 1.50 | `support_price=0` 时自动取 `昨收×(1−1.50%)`；现价跌破则放弃低吸 |
| 跌幅可控 | `max_drop_pct` | 3.00 | 当日跌幅 > 3% 视为单边下跌，不做正T |
| 成本约束（可选） | `check_cost` / `cost_tolerance_pct` | false / 1.00 | 开启后要求买价 ≤ `成本×(1+1.00%)`；正T低吸时通常关闭，否则跌下来就买不了 |
| 未平批次上限 | `trade.max_pending_batches` | 1 | 上一批买入还没平掉就不再叠加买入，避免越跌越买变成加仓摊薄 |
| 可卖底仓 | `positions.*.sellable_qty` | — | 正T买入后必须能卖回，底仓不足不下信号 |
| 冷却 | `trade.cooldown_sec` | 300 | 同标的同类型信号冷却期内不重复 |
| 当日上限 | `trade.max_signals_per_day` | 6 | 单标的单类型当日最多几次 |

**反T·先卖后买（SELL_FIRST）** — 高位减仓、低位接回：

| 条件 | 参数 | 默认值 | 含义 |
| --- | --- | --- | --- |
| 开关 | `sell_first.enabled` | true | — |
| 卖出基准 | `sell_first.basis` | batch | `batch`：优先用当日买入批次加权成本（真正的平T）；无待平批次时退回当日分时均价（高卖底仓的反T）。`day_avg`：当日分时均价。`cost`：历史持仓成本（深套标的别用，会永远不触发） |
| 高于均价线 | `above_avg_pct` / `dev_ticks` | 0.30 / 3 | 两套阈值取更严的一方 |
| 平T地板价 | `target_ticks` / `target_pct` | 3 / 0.20 | 地板价 = max(基准 + N 跳 + 双边佣金摊薄, 基准×(1+pct%))，向上取整到 tick；现价须 ≥ 地板价才卖 |
| 成本利润（可选） | `require_cost_profit` / `min_profit_over_cost_pct` | false / 0.50 | 开启后额外要求现价 ≥ `成本×(1+0.50%)`，默认关闭（深套标的会被永久拦住） |
| 可卖底仓 | `positions.*.sellable_qty` | — | 卖出量不得超过可卖底仓；`basis=batch` 时还不得超过当日待平数量 |
| 冷却 / 上限 | 同上 | 300 / 6 | — |

**时间止损（TIME_STOP）** — 存在当日未平买入批次时的强制平仓兜底，与价格信号**互斥**（本轮已产生卖出信号则不再重复下卖单）：

| 条件 | 参数 | 默认值 | 含义 |
| --- | --- | --- | --- |
| 开关 | `trade.time_stop.enabled` | true | 关闭则只靠价格信号平T |
| 到点强平 | `deadline` / `trigger_on_deadline` | 14:50 / true | 到该时点仍未平仓则产生平仓卖出信号，禁止T单变隔夜 |
| 持有超期 | `max_hold_minutes` | 120 | 单批次持有时长超过该值仍未平仓，同样强平 |
| 浮亏止损 | `loss_pct` | 0.3 | 相对批次成本浮亏达到该比例即强平 |
| 重复提示 | — | 同批次一次 | 同一批次指纹只提示一次，成交或撤单后才重新判定 |

**数量与费用口径**（`trade` 段）：

| 参数 | 默认值 | 含义 |
| --- | --- | --- |
| `qty_mode` | notional | `notional`：按成交额反推手数（推荐，避免 100 份×0.8 元这种低效小单）；`fixed`：用 `buy_qty`/`sell_qty` 原值 |
| `min_notional` / `max_notional` | 5000 / 0 | 单笔最低/最高成交额，`0` 表示不限制；反推手数不低于 `buy_qty` |
| `fees.commission_rate_pct` | 0.025 | 佣金率（%），参与地板价的双边佣金摊薄 |
| `fees.min_commission` | 5.0 | 最低佣金（元），小额单实际按此计，会显著抬高保本价 |
| `fees.stamp_tax_pct` | 0.0 | ETF/基金印花税为 0 |
| `book_on_dry_run` | true | 空跑时也登记批次台账，可完整观察"买入→待平→时间止损"整条链路 |

> 阈值填负数会被按绝对值处理（判定窗口取正值百分比）。

**信号内容**：唯一编号（如 `T20261118-600519-B01-9FB7`：日期-代码-B正T/S反T-序号-随机尾）、触发时刻、以及触发依据的**数值快照**（现价/均价/偏离/昨收/最高最低/涨跌幅/振幅/成本/盈亏/支撑线/五档盘口/建议价量）。弹窗与 CSV 均包含这份快照。

`--once` 会把每条判定逐条打印，便于核对是否阈值写错。实测输出片段：

```
--- 正T·先买后卖 判定 ---
  √ 交易时段: 当前处于交易时段
  √ 低于分时均价线: 偏离 -0.23%，阈值 ≤ -0.10%
  √ 未破当日支撑: 现价 1442.66 vs 支撑 1435.83
  √ 跌幅未失控: 当日 -1.03%，阈值 ≥ -3.00%
  => 触发信号
```

---

## 5. 窗口监控

- 进程：`window.process_name`（`happ.exe`）+ 完整路径 `I:\app\THS_win_1\同花顺远航版\bin\happ.exe`（仅作提示，不做强制杀进程）。
- 主窗口：`win32gui` 枚举可见窗口，按 `window.title_keywords`（默认 `["同花顺","远航版"]`）匹配标题；已自动排除**本程序自身进程**的窗口。
- 前台判断：`GetForegroundWindow` 与主窗口句柄比较，`require_foreground` 仅用于提示，不作为下单前置条件。
- 窗口缺失时：主界面顶部状态行变红提示“同花顺未启动”，**行情监控与信号判定照常进行**；dry_run 演练不受影响，真实下单会先尝试激活窗口，失败则记录 `ABORTED_NO_WINDOW` 并中止。

---

## 6. 交互与下单流程

1. 信号触发 → 播放提示音（`ui.sound`）→ 弹出**置顶**确认窗（`ui.always_on_top`）。
2. 弹窗显示：信号编号、类型、现价/均价/偏离、涨跌幅/振幅、成本与盈亏、支撑线、五档盘口、建议价量、触发依据，底部 **10 秒倒计时**（`ui.popup_timeout_sec`）。
3. 三个出口：**确认下单** → 执行下单；**忽略** → 记录 `ignore`，不下单；**倒计时归零** → 记录 `timeout`，自动关闭且**绝对不下单**。
4. 确认后执行（`OrderExecutor`）：
   - `dry_run=true`：在日志与终端打印动作序列（激活窗口 → 点代码框 → 输入代码 → 点价格框 → 输入价格 → 点数量框 → 输入数量 → 点买/卖按钮），**不移动鼠标**，状态记为 `DRY_RUN`。
   - `dry_run=false`：先激活同花顺窗口，再按控件实时定位一次 8 个坐标（locate_at_runtime，失败回退 config.json 的客户区相对坐标）；回退到静态坐标时会校验客户区尺寸是否与 `_coords_ref` 一致（不一致 → `ABORTED_LAYOUT_CHANGED`，旧版屏幕坐标 → 同样中止），校验全部已配置（任一 `[0,0]` → `ABORTED_NO_COORD` 拒绝执行），最后用 `ClientToScreen` 把相对坐标换算成屏幕绝对坐标再逐项点击并输入、点击下单按钮；每次点击记录坐标与执行结果，可选截图（`order_ui.screenshot`，存数据目录 `logs\shots\`）。
5. 全程写入 `%LOCALAPPDATA%\ths_t_trader\logs\audit_trade.csv`（`SIGNAL_TRIGGER` → `USER_DECISION` → `ORDER_RESULT` 三行一组）与同目录 `runtime.log`。

输入方式可配置：`order_ui.qty_unit`（`shares`=按股数直接输入；`lots`=输入手数）、`order_ui.price_mode`（`signal`=用信号价/`bid`=用买一价/`ask`=用卖一价/`last`=现价）、`order_ui.price_offset_ticks`（价格偏移跳数，负值更保守，按标的 tick 计算：ETF/基金 1 跳=0.001，股票 1 跳=0.01）、`order_ui.type_interval_sec`（输入速度）、`order_ui.action_delay_ms`（每步之间间隔）。

### 6.1 价格精度（ETF 必读）

不同标的的最小报价单位不同，统一按两位小数处理会把 ETF 价格截断（例如 0.795 → 0.80，真实下单会被废单或错误成交）。脚本按标的自动判定，**不需要手工设置**：

| 判定条件 | 小数位 | tick |
|---|---|---|
| 场内基金/ETF：6 位代码以 `15`/`16`/`51`/`56`/`58` 开头 | 3 | 0.001 |
| 现价 < 10 元的标的 | 3 | 0.001 |
| 其余（普通股票，如 600519、300750） | 2 | 0.01 |

精度贯穿全部输出链路：控制台行情行、`--once` 判定依据、信号弹窗详情、CSV 审计字段（`price`/`avg_price`/`cost`/`support_price`/`suggested_price`）、下单价格框键入文本，以及 `price_offset_ticks` 的偏移量（ETF 每跳 0.001，不会退化成 0.01）。

**手动覆盖入口**：`config.json` 的 `price_decimals`，键为 6 位代码、值为 0~4 的整数：

```json
"price_decimals": {
  "default": null,
  "600519": 2,
  "159325": 3
}
```

`default` 留 `null`（或空串）表示全部走自动规则；填整数则作为全局默认，代码级键优先级最高。取整使用「四舍五入到 tick」，避免二进制浮点把 0.795 舍成 0.79。

---

## 7. 参数调优

调优前先用 `--once` 观察真实的“偏离均价”范围，再决定阈值——不同标的波动差异极大（银行股日内偏离常在 ±0.2%，白酒股可达 ±0.5%）。

1. **正T `below_avg_pct`**：偏保守就调大（0.5~0.8），信号更少但更安全；追求频率就调小（0.15~0.25）。
2. **反T `above_avg_pct` + `min_profit_over_cost_pct`**：两者同时决定“卖得够不够高”。成本较低时可把 `min_profit_over_cost_pct` 调大（1.0~1.5），避免在成本线附近乱卖。
3. **`support_break_pct`（正T保护）**：跌破 `昨收×(1−x%)` 就不再低吸，防止越跌越买。震荡市 1.5~2.0，单边下跌市建议 3.0 以上或关闭正T。
4. **`max_drop_pct`**：当日跌幅超过该值不做正T，防接刀。建议不低于 3.0。
5. **`cooldown_sec`**：一只票同时只应有一笔在做。做T周期通常在 5~30 分钟，冷却建议 300~900 秒，避免同一波下跌反复提示。
6. **`poll_interval_sec`**：3 秒足够分时级别判断；调 1 秒意义不大（新浪接口非逐笔，且易被限频）。
7. **`buy_qty` / `sell_qty`**：必须为 100 的整数倍；正T买入数量不得超过该标的 `sellable_qty`（脚本会校验）。
8. **`sessions`**：默认避开 9:30 前的集合竞价，收盘对齐到 15:05；如需 9:25 竞价后参与可改为 `["09:25","11:30"]`。
9. **`trade.allow_offhours_signal`**：默认 `false`，非交易时段只监控不出信号；盘后调试想验证阈值可临时设 `true`。
10. **`max_signals_per_day`**：控制每日信号总量，防止频繁交易。当日计数按自然日重置。

---

## 8. 坐标拾取（真实下单必做）

**方法 A：自动识别（首选，不需要任何按键）**

```bat
python main.py --detect            :: 只读取并显示 8 个坐标，不写入
python main.py --detect --write    :: 识别完整时写入 config.json
```

脚本直接枚举「网上股票交易系统5.0」窗口的子控件矩形，按标签位置推导出坐标，全程只读控件、
不发送任何键盘事件，与同花顺自带热键零冲突。

> **坐标口径（本次改造后）**：`order_ui.coords` 存的是**下单窗口客户区的相对像素**（相对客户区
> 左上角），不再存屏幕绝对坐标。点击前才用 `ClientToScreen(hwnd, 0, 0)` 取客户区原点换算成屏幕
> 坐标，因此窗口被拖动、最大化、换分辨率、换显示器都不会让坐标失效。
>
> 配置里附带两个辅助键：`_coords_mode`（取值 `"client"`，标记坐标口径版本）与
> `_coords_ref`（形如 `{"client_w": 1110, "client_h": 607}`，记录保存坐标时的客户区尺寸）。
> 真实下单前如果当前客户区尺寸与 `_coords_ref` 的偏差超过 `order_ui._coords_tolerance_px`
> （默认 8px），说明客户端布局或系统缩放变了，静态坐标不再可信，直接中止
> （`ABORTED_LAYOUT_CHANGED`）并提示重拾，而不是冒险点偏。
>
> 旧版配置（没有 `_coords_mode`，坐标仍是屏幕绝对坐标）在首次使用时会按当前窗口原点临时换算，
> 同时打一条 WARN 日志；请尽快执行 `python main.py --detect --write` 写入新口径坐标。
>
> DPI 口径也已统一：主程序在启动时即声明 DPI 感知（`ensure_dpi_aware()`），配置编辑器同样使用
> `SetProcessDpiAwareness(2)`，避免 125%/150% 缩放下「拾取端」与「执行端」坐标系不一致造成整体偏移。

**下单窗口是买入/卖出双面板**：左侧「买入股票[F1]」一套输入框 + 买入按钮，右侧「卖出股票[F2]」
另一套输入框 + 卖出按钮，两侧输入框互不相通。因此需要 **8 个坐标点**，缺一不可：

| 项目 | 配置键 |
| --- | --- |
| 左侧-证券代码框 | `code_box` |
| 左侧-买入价格框 | `price_box` |
| 左侧-买入数量框 | `qty_box` |
| 买入按钮 | `buy_button` |
| 右侧-证券代码框 | `code_box_sell` |
| 右侧-卖出价格框 | `price_box_sell` |
| 右侧-卖出数量框 | `qty_box_sell` |
| 卖出按钮 | `sell_button` |

买入信号只点左侧四个点，卖出信号只点右侧四个点，**不做跨侧回落**：某侧坐标缺失时真实下单直接
中止（`ABORTED_NO_COORD`），避免把价格数量填进买入侧却点卖出按钮这类空单风险。

**运行时自动纠偏（`order_ui.locate_at_runtime`，默认 `true`）**：真实下单在激活窗口之后、点击之前，
会再按控件实时定位一次这 8 个点并直接用于本次点击（`config.json` 里的静态坐标仅作回退）。
好处是窗口被拖动、换分辨率或客户端换布局后，坐标自动跟随，不必重新拾取；定位失败或控件识别不完整时
才回退到 `config.json` 的静态坐标，并记录一条 WARN 日志。把该项设为 `false` 则恢复为纯静态坐标。
实测把下单窗口平移 (180,110) 后，8 个坐标同步偏移 (180,110)，回原位后与配置完全一致。

**遮挡校验**：`--detect` 会顺带检查这些点当前是否被其它窗口（安卓模拟器、聊天窗、悬浮球）盖住，
被盖住时打印「遮挡警告」；真实下单前也会再查一次，发现遮挡则中止（`ABORTED_OCCLUDED`），
防止鼠标点到其它程序上。

**方法 B：`--pick`（先自动识别，失败则手动悬停记录）**

```bat
python main.py --pick              :: 先自动识别，不完整时转手动
python main.py --pick --manual     :: 强制手动悬停记录
```

手动模式：按提示依次把鼠标移到对应控件上，**停住不动约 1.5 秒**，「嘀」一声即记录成功
（也可按 F12 或鼠标中键立即记录），顺序为
`左侧代码框` → `左侧价格框` → `左侧数量框` → `买入按钮` → `右侧代码框` → `右侧价格框` → `右侧数量框` → `卖出按钮`；
中途按 **ESC** 中止且不修改配置。

> ⚠️ **不要用 F1 / F2 / F8 等 F 键记录坐标**：同花顺下单界面已把它们注册为功能热键
> （控件标题可见「买入股票[F1]」「卖出股票[F2]」），按键会同时触发客户端动作，且拾取程序
> 拿不到该按键，表现为「按了 F8 但什么都没记录」。这也是早期版本拾取失败的根因，
> 现已改为悬停记录，彻底绕开按键冲突。

**方法 C：手动取坐标**

配置编辑器的「坐标点」页签已按新口径工作：点「拾取」后 3 秒内把鼠标悬停在目标控件上，程序读到的
屏幕坐标会自动减去下单窗口客户区原点，落盘为客户区相对坐标，并顺带写入 `_coords_mode` /
`_coords_ref`；若下单窗口未打开，会提示先打开再拾取。

在另一个 Python 窗口手工换算（先取客户区原点，再把屏幕坐标减掉）：

```python
import ctypes
from ctypes import wintypes

user32 = ctypes.windll.user32
hwnd = 0  # 用 main._find_order_window() 拿到的下单窗口句柄
pt = wintypes.POINT(0, 0)
user32.ClientToScreen(hwnd, ctypes.byref(pt))
ox, oy = pt.x, pt.y          # 客户区左上角的屏幕坐标

import pyautogui
x, y = pyautogui.position()  # 鼠标位置的屏幕坐标
print(x - ox, y - oy)        # 应填入 config.json 的客户区相对坐标
```

**注意事项**

- 坐标为**下单窗口客户区相对像素**，格式 `[x, y]`；两轴都为 0（`[0,0]`）视为未配置，真实下单会拒绝执行
  （相对坐标下 x 或 y 单侧为 0 是合法的，控件可能正好贴边）。
- 抓坐标时**不需要**让窗口停在固定位置——正因为是相对坐标，拖动窗口、最大化都不会失效；
  但客户端布局（是否展开高级面板、列宽、委托面板位置）必须与抓取时保持一致，否则控件位置会变。
- 客户区尺寸变化超过 `_coords_tolerance_px`（默认 8px）会被守卫拦下（`ABORTED_LAYOUT_CHANGED`），
  重新执行 `--detect --write` 或在配置编辑器里重拾即可；系统缩放（DPI）变更、换显示器同理。
- 建议先在同花顺里放一笔“模拟委托”界面做靶子，用 `dry_run=false` + 单只票 + 100 股跑一次，确认点击位置正确。
- 若下单界面需要先切换“买入/卖出”标签页或输入密码，请在 `order_ui` 之外先自行准备好界面状态——本脚本只按配置坐标点击一次流程，不做复杂导航。

---

## 9. 从 dry_run 切到真实下单

1. **先完成坐标识别**：`python main.py --detect --write`，确认 `config.json` 中 8 个坐标（买卖两侧各 4 个）都不是 `[0,0]`，且 `order_ui._coords_mode` 已写成 `"client"`。旧版配置（坐标为屏幕绝对坐标、无该键）必须先重跑一次，否则真实下单会在回退静态坐标时以 `ABORTED_LAYOUT_CHANGED` 中止。
2. **保持 dry_run 再演练一次**：`python main.py --selftest --auto-confirm`，看日志里 `DRY_RUN` 的动作序列与你期望的界面操作是否一致。
3. **修改配置**：`"dry_run": true` → `false`（`order_ui` 段）。
4. **缩小风险**：`trade.buy_qty` / `sell_qty` 先设 100，`max_signals_per_day` 设 1~2，`cooldown_sec` 设 600+。
5. **人工在场**：真实模式下每次下单都必须由你点“确认下单”，倒计时结束即放弃；离开电脑前请直接关闭脚本（Ctrl+C 或关主窗口）。
6. **核对结果**：成交后立刻在同花顺中人工核对委托是否成功、价格数量是否正确；脚本不会替你判断成交回报。
7. **出问题立即回滚**：把 `dry_run` 改回 `true` 并重启脚本。pyautogui 的**鼠标移到屏幕左上角**会触发 FailSafe 异常，可作紧急刹车。

`ABORTED_*` 常见状态：

| exec_status | 含义 |
| --- | --- |
| `DRY_RUN` | 演练：仅打印动作序列，未操作鼠标 |
| `SIMULATED_OK` | 已按坐标完成模拟点击（**不代表成交**，需到同花顺人工核对委托） |
| `ABORTED_NO_COORD` | 坐标未配置或存在 `[0,0]`，拒绝真实下单 |
| `ABORTED_LAYOUT_CHANGED` | 静态坐标不可信：客户区尺寸与 `_coords_ref` 偏差超容差，或仍是旧版屏幕绝对坐标，拒绝真实下单（重跑 `--detect --write`） |
| `ABORTED_OCCLUDED` | 目标点被其它窗口遮挡，拒绝真实下单（先把同花顺切到最前） |
| `ABORTED_NO_WINDOW` | 找不到同花顺主窗口，拒绝真实下单 |
| `ABORTED_NO_PYAUTOGUI` | 缺少 pyautogui，无法模拟点击 |
| `ERROR` | 执行过程异常（鼠标被占用、控件错位等），详见 `exec_detail` |

---

## 10. 审计日志字段（audit_trade.csv）

`event`（事件类型：`QUOTE_PROBE` / `SIGNAL_TRIGGER` / `USER_DECISION` / `ORDER_RESULT`）、`timestamp`、`signal_id`、`code`、`name`、`signal_type`、`triggered_at`、`quote_time`、`price`、`avg_price`、`deviation_pct`、`prev_close`、`day_open`、`high`、`low`、`amplitude_pct`、`change_pct`、`cost`、`pnl_pct`、`support_price`、`bid_ask`、`suggested_price`、`suggested_qty`、`user_choice`（`confirm`/`ignore`/`timeout`/`SELFTEST`）、`countdown_left`、`dry_run`、`exec_status`、`exec_detail`、`coords`（点击坐标明细）、`window_state`、`remark`。

编码 `utf-8-sig`，Excel 双击不乱码。

---

## 11. 实测验证记录（2026-11-18，本机）

| 验证项 | 结果 |
| --- | --- |
| 依赖安装（requests / pyautogui / pillow / psutil / pywin32） | 通过，Python 3.11.8 |
| 新浪行情抓取（带 Referer，GB18030） | 通过，`sh600519` / `sz000001` 秒级取到真实价量 |
| 派生指标（均价/振幅/涨跌幅/盈亏） | 通过，均价 1446.052，振幅 1.67%，涨跌 -1.03% |
| 正T信号触发（放宽阈值验证） | 通过，生成 `T20261118-600519-B01-9FB7` |
| 反T信号触发（放宽阈值验证） | 通过，生成 `T20261118-000001-S01-B931` |
| 窗口监控 | 通过，正确识别“happ.exe 进程缺失 / 窗口未找到”，且不中断行情 |
| 自检弹窗 + 倒计时 + 自动确认 + dry_run 执行 | 通过，CSV 依次写入 `SIGNAL_TRIGGER`→`USER_DECISION`→`ORDER_RESULT(DRY_RUN)` |
| 真实模式坐标缺失保护 | 通过，`exec_status=ABORTED_NO_COORD`，未移动鼠标 |
| 常驻运行与日志 | 通过，`runtime.log` 持续记录每轮行情与窗口状态 |
| ETF 价格精度（159325 半导体NF / 159866 日经ETF） | 通过，`--once` 输出 现价 0.795 / 均价 0.788 / 成本 1.601，159866 现价 1.502，均为 3 位小数 |
| 下单价格框文本（dry_run 动作预览） | 通过，键入 `0.795`（旧版会写成 `0.80`）、`1.502`；数量框键入 `100` |
| 价格偏移 tick 实算 | 通过，`price_offset_ticks=2` 时 ETF 0.795→0.793（每跳 0.001）、600519 1442.664→1442.64（每跳 0.01） |
| CSV 审计精度 | 通过，`price=0.795 / avg_price=0.788 / cost=1.601 / support_price=0.773` |
| 离线精度回归脚本 | 通过，31 项断言全绿（小数位判定 / tick / 取整 / 格式化 / 覆盖入口 / 弹窗文本） |

---

## 12. 常见问题

- **弹窗没声音**：Windows 提示音被静音或 `ui.sound=false`；不影响信号。
- **提示“同花顺未启动”但仍出信号**：设计如此，窗口缺失只影响真实下单，不影响行情与信号。
- **按 F8 没反应 / 反而触发了下单界面动作**：F8 属同花顺客户端占用的功能热键（F1 买入页、F2 卖出页），拾取程序无法截获，现已被弃用。坐标获取改用 `--detect` 自动识别，无需任何按键；若必须手动，用 `--pick --manual` 的"悬停静止 1.5 秒"方式记录。
- **坐标点了但没填进去**：同花顺控件位置变动或 DPI 不一致；重新拾取，并确认 `dpi_aware=true`。
- **点击落到了别的程序上**：下单窗口被模拟器/聊天窗/悬浮窗盖住。`--detect` 会打印遮挡警告，真实下单会以
  `ABORTED_OCCLUDED` 中止。把同花顺下单窗口切到最前再执行即可。
- **卖出信号把价格填进了买入侧**：配置里缺少右侧卖出侧 4 个坐标（老配置或识别不全所致），并用 --detect 确认 8 个点齐全。重新执行 `python main.py --detect --write`
  补齐 `code_box_sell / price_box_sell / qty_box_sell`，卖出侧缺坐标时程序会直接中止而不是跨侧复用。
- **CSV 中文乱码**：用 Excel 直接打开即可（`utf-8-sig`）；WPS/文本编辑器请选 UTF-8。
- **非交易时段一直没信号**：这是设计；调试可临时设 `allow_offhours_signal=true` 或放宽阈值。
- **新浪接口返回空**：偶发限频或网络抖动，脚本会记录 WARNING 并在下一轮重试；连续失败时检查本机网络/代理。
- **可以加自动止盈止损吗**：本版本只做“信号 + 人工确认”，不自动平仓。任何自动交易都需要你自行评估风险后扩展。

---

## 13. 目录结构

```
程序目录（exe / 源码所在处，只读也能跑）
ths_t_trader/
├── main.py
├── config_ui.py
├── config.json
├── start.bat
└── README.md

数据目录（默认 %LOCALAPPDATA%\ths_t_trader，可用 THS_T_DATA_DIR 覆盖）
ths_t_trader/
├── logs/
│   ├── audit_trade.csv          审计 CSV
│   ├── runtime.log              运行日志
│   ├── console_<日期>.log       无控制台时的 print 兜底输出
│   ├── restart_<时间戳>.log     重启时子进程输出
│   └── shots/                   截图（开启 order_ui.screenshot 后生成）
└── data/
    └── signal_state.json        当日信号台账
```
*（内容由AI生成，仅供参考）*

## 自动发布（Release）

推送到 `main` 分支会自动触发 `.github/workflows/release.yml`，在 Windows 环境中完成：

1. 打包单文件 `THS-T-Assistant.exe`（`--ui` 参数复用为图形化配置编辑器）；
2. 生成**便携版** `THS-T-Assistant-<版本>-portable.zip`（exe + 干净出厂配置 + 使用说明 + 文档）；
3. 用 Inno Setup 生成**安装包** `THS-T-Assistant-<版本>-setup.exe`（装到当前用户目录，无需管理员权限，带开始菜单/配置编辑器快捷方式）；
4. 创建 GitHub Release，上传上述两个文件，版本号形如 `v1.0.0-20261124-<short-sha>`。

**出厂配置安全设计**：包内 `config.json` 以字段完整配置为结构基线，但彻底清洗了隐私数据——
持仓、可卖数量、成本、下单坐标全部复位，仅保留一个公开指数 ETF（510300）作为演示标的
（校验器要求至少一只标的）；帮助文本中写死的证券代码也会泛化。用户须在「新手指引」页
替换为自己的标的与坐标。安装包采用 `onlyifdoesntexist`，升级时不会覆盖用户已有配置。

本地手动构建（需 Windows + 已安装依赖）：

```bash
pip install -r packaging/requirements-build.txt
python packaging/build_release.py                 # 产物在 dist/release/
```

构建产物如需编安装包（需 Inno Setup 6）：

```bash
ISCC.exe /DAppVersion=<版本> /DSourceDir=<portable目录> /DOutDir=dist/release packaging\installer.iss
```

CI 会先运行 `exe --ui --check` 做只读校验，校验不过不会发布。

## 下单演练（--test-order）

用于在不产生委托的前提下，验证下单窗口坐标、窗口激活与输入是否正常。

```bat
python main.py --test-order                       :: 买入侧演练，代码取 config 第一个标的，价格取实时现价
python main.py --test-order --side sell            :: 卖出侧演练
python main.py --test-order --price 0.795 --qty 100
python main.py --test-order --submit               :: 填完后真实点击买入按钮（会提交委托，谨慎）
```

行为说明：
- 会激活「网上股票交易系统5.0」窗口，点击对应侧的代码/价格/数量三个输入框并填入内容。
- 默认**不点击**买入/卖出按钮，因此不产生任何委托；加 `--submit` 才会点击按钮。
- 填值前统一执行 `ctrl+a` → `delete` → 8 次 `backspace` 清空。原因：同花顺价格框会自动带出参考价，
  仅 `ctrl+a` 未必生效，会出现旧值与新值拼接（例如 `0.796` + `0.795` 变成 `0.7960.795`）。
- 截图保存到 `%LOCALAPPDATA%\ths_t_trader\logs\shots\test_order_{side}_{时间戳}.png`，可在审计日志中查到 `ORDER_TEST` 行。

输入框内容校验提示：该客户端输入框是自绘控件，`GetWindowText` / `WM_GETTEXT` / UIA `ValuePattern`
都取不到值，也不支持 `ctrl+a`+`ctrl+c` 复制。校验只能靠截图后放大读字。

---

## 图形化配置编辑器（config_ui.py）

不想手改 `config.json` 时，用这个独立的 tkinter 工具可视化编辑全部配置项。它只读写 `config.json`，**不依赖也不修改 `main.py`**。

```bat
python config_ui.py                 :: 打开界面（默认读脚本同目录 config.json）
python config_ui.py --config x.json  :: 编辑指定的配置文件
python config_ui.py --check          :: 只做校验，不打开窗口，适合改完配置后自检
```

**界面结构**（10 个页签）：新手指引 / 账户与标的 / 行情 / 信号 / 交易 / 窗口 / 下单 UI / 坐标点 / 弹窗与日志 / 保留键（只读）。
每个字段都有中文标签、单位，以及原 `config.json` 里 `_help` 的完整说明；分组级 `_help` 显示在页首「原配置说明」框内。

### 新手指引（首屏页签）

面向首次使用，三步完成最关键的初始配置，结果写入既有页签，最后统一保存到 `config.json`：

1. **添加标的**：输入 6 位证券代码（多个用逗号分隔），点「查询行情并补全」联网从新浪获取名称与现价并导入【账户与标的】；网络不可用时点「跳过查询，直接按代码添加」。
   - 自动同步进【行情】页监控列表；重复代码去重。
   - 成本先填现价作占位，可卖股数默认 0，**务必到【账户与标的】页按真实持仓修改**，否则盈亏基准和可卖额度不准。
   - T+0 按代码段推断（15/16/51/56/58 开头视为场内基金/ETF），仅为初判，**股票 ETF 实际多为 T+1，请自行复核勾选**。
2. **定位坐标**：先打开同花顺「网上股票交易系统」下单界面，点「自动识别 8 个坐标」，识别结果（客户区相对坐标 + 屏幕坐标）写入【坐标点】页。识别只读控件矩形，不点击、不输入；缺失的点需到【坐标点】页手动「拾取」。
   - 识别后即使关闭下单窗口再保存，也会保留当时的 `_coords_mode=client` 与参照尺寸，不会被误判为旧版绝对坐标。
3. **检查并保存**：一键检查标的数量、坐标完整性（8/8）、`dry_run` 状态与改动清单；通过后点「立即保存到配置文件」，再按 README 的顺序做 `python main.py --once` → `--selftest` 验证。

**校验规则**（不通过则拒绝保存，并自动跳到出错的页签）：

| 类别 | 规则 |
| --- | --- |
| 百分比 | 0~100 的数值，如 0.30、1.50 |
| 股数 | 100 的整数倍 |
| 时刻 | `HH:MM` 24 小时制，交易时段要求开始早于结束 |
| 证券代码 | 6 位数字 |
| 坐标 | 0~20000 的整数像素 |
| 选项类 | 只能取下拉框中的合法值（qty_mode / basis / qty_unit / price_mode / csv_encoding） |

**安全机制**：

- 保存前先把原文件复制到 `backups/config_YYYYMMDD_HHMMSS.json`（最多保留最近 30 份），再以「临时文件 + 原子替换」写回，中途异常不会留下半截文件。
- 保存前弹窗列出全部改动（旧值 → 新值），取消即不写盘。
- 以磁盘原文件的深拷贝为基底，只覆盖界面上展示的字段，因此**键名、键顺序、`_help` / `_example` 以及任何未展示的键都会原样保留**；「保留键（只读）」页签可以查看哪些键未被界面映射。
- 修改 `order_ui.dry_run` 之外的下单参数（尤其坐标）后，建议先 `python main.py --test-order` 演练一次，确认无误再考虑关闭演练模式。

**坐标点页签**：点某一项的「拾取」后，3 秒内把鼠标移到同花顺对应输入框/按钮上，程序会自动读取当前鼠标位置；`[0,0]` 表示未配置，真实下单时会被拒绝。

**开发者自检**（不启动窗口）：

```bat
python config_ui.py --selftest    :: 字段覆盖检查 + 校验器用例 + 结构保持/备份/原子写入验证
```

## 14. TODO List（按优先级）

排序原则：先修复缺陷并保证下单安全，再完善和落地新功能，数据与风控能力紧随其后，UI 优化放在最后。所有新增能力都不得绕过人工确认、`dry_run` 默认以及坐标/遮挡校验。

### P0 · 缺陷与正确性

- [ ] **功能 bug 修复**：修复已发现或运行中暴露的缺陷，重点覆盖行情抓取、信号判定、坐标拾取、真实下单中止逻辑与状态台账恢复。
- [ ] **配置与状态可恢复**：提供 `config.json` 模板/生成命令，给配置加版本号与迁移逻辑；状态文件损坏或跨日时自动降级重建并告警，而不是静默丢失闭环锁。
- [ ] **回归样本集**：把 `--once` 的判定输出、`audit_trade.csv` 与 `CHANGELOG_P0P4.md` 中的历史回放场景固化为可重复执行的回归脚本，先锁住现有行为再加新功能。

### P1 · 核心功能完善

- [ ] **加入大模型决策（辅助层）**：对行情快照、触发依据、批次状态和历史信号给出解释与辅助建议；第一阶段只输出文字提示，不替代人工确认，不改变规则引擎的最终触发结果。
- [ ] **委托生命周期跟踪**：在点击下单按钮后继续跟踪委托确认、部分成交、撤单和废单，把状态回写到审计 CSV 与批次台账，避免“点了按钮就假设成交”。
- [ ] **持仓/成交对账**：支持导入同花顺导出的成交记录或手工录入实际成交，与 `signal_state.json` 的待平批次、当日已卖数量核对，发现偏差时给出修复建议。
- [ ] **一键停手与全局风控**：增加全局急停开关/热键，触发后暂停新信号、关闭待确认弹窗、禁止真实下单；同时把当日亏损熔断、最大持仓、单标的上限做成可配置的总闸。
- [ ] **模拟撮合/训练模式**：在 dry_run 下按行情快照模拟成交、滑点与手续费，完整走完“买入→待平→回买/止损”，用于练手和验证策略而不产生真实委托。

### P2 · 数据与策略增强

- [ ] **多数据源校验与行情新鲜度检测**：评估接入备用免费行情源，主源异常时切换；对停牌、快照过期、成交量异常跳变打标，避免用脏数据触发信号。
- [ ] **回放/回测与参数评估**：复用已有的 1 分钟 K 线与轮询快照做历史回放，输出信号次数、胜率、盈亏、最大回撤和分标的统计，为阈值调整提供依据。
- [ ] **分标的策略模板与参数推荐**：把 `position_overrides` 的常见组合沉淀为可复用模板；结合回测结果给出阈值建议，但只生成建议值，不自动改配置。
- [ ] **盘前/盘后 AI 简报与信号解释归档**：自动生成当日计划、风险提示和收盘复盘，并把每次信号的规则依据与大模型解释一起写入可读的复盘记录。

### P3 · 体验层（优先级最低）

- [ ] **UI 优化**：在功能和决策链路稳定后，优化常驻主界面、确认弹窗与 `config_ui.py` 配置编辑器的可读性、状态展示和错误定位；不改动现有配置键含义。

### 待确认（先不排期）

- 大模型采用本地模型还是云端 API，是否允许发送行情快照与审计字段。
- 是否允许通过界面自动化读取同花顺委托/成交列表做对账，还是只接受手工导入。
- 备用行情源的具体服务商与调用频率限制。

*（内容由AI生成，仅供参考）*
