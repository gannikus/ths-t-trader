---
AIGC:
    Label: "1"
    ContentProducer: 001191440300708461136T1XGW3
    ProduceID: 2cf0af362bda02c9ebee3360cad8ea10_7042f493e70e11f1a9b8525400f8a581
    ReservedCode1: o0U0TKmP1IwX5VY1RFGWndkejI3j2CzXYL9BlfKx3EzO44euSsCNhXotQxFt9BQKOMBT2PJXNBwkEYH37HnIntd9n5F32w6xjuEF+ZQJgyxPXBISNLrjDxzetywvpYCebNlGr0AIN/y5KrV9aVcsYfPD0rqoZuMMs5ZP5YQvQpeP6op5izHogAnANRg=
    ContentPropagator: 001191440300708461136T1XGW3
    PropagateID: 2cf0af362bda02c9ebee3360cad8ea10_7042f493e70e11f1a9b8525400f8a581
    ReservedCode2: o0U0TKmP1IwX5VY1RFGWndkejI3j2CzXYL9BlfKx3EzO44euSsCNhXotQxFt9BQKOMBT2PJXNBwkEYH37HnIntd9n5F32w6xjuEF+ZQJgyxPXBISNLrjDxzetywvpYCebNlGr0AIN/y5KrV9aVcsYfPD0rqoZuMMs5ZP5YQvQpeP6op5izHogAnANRg=
---

# 做T脚本 P0–P4 优化落地说明

- 日期：2026-11-23
- 目标文件：`output\ths_t_trader\main.py`（170,768 → 209,7xx 字节）、`output\ths_t_trader\config.json`
- 编译校验：py_compile COMPILE_OK
- 离线验收：19 项断言全部通过（费用 / 分层 / 地板价 / 动态跳数 / 卖出上限）

---

## P0 费用模型标准化

**设计原则**：代码只认标准结构，ETF 用「最低佣金 0、印花税 0、过户费 0」退化为纯佣金；后续加股票无需改代码。

**三级覆盖链**（`fee_conf()`）：

```
trade.fees（全局默认）
  └─ trade.fees.by_kind.fund / .stock      ← 按品种
       └─ trade.fees.by_code.<6位代码>       ← 按单只标的（特例手改）
```

**当前配置**

| 层级 | commission_rate_pct | min_commission | stamp_tax_pct | transfer_fee_pct |
|---|---|---|---|---|
| 全局默认 | 0.005（万0.5，双向） | 0 | 0 | 0 |
| by_kind.fund | 0.005 | 0 | 0 | 0 |
| by_kind.stock | 0.005 | 5.0 元 | 0.05（卖出） | 0.001（双向） |

**收取规则**（`fee_of(cfg, code, amount, side)`）
- 佣金：双向收取，取 `max(成交额×费率, 最低佣金)`
- 印花税：仅卖出
- 过户费：仅卖出（按你券商实际口径调整该字段即可）
- 品种判定：`is_fund_like()` — 6 位代码以 15/16/51/56/58 开头视为基金

**实测**

| 标的 | 1520 元买入 | 1520 元卖出 |
|---|---|---|
| 159325 ETF | 0.076 元（无最低 5 元） | 0.076 元（无印花税/过户费） |
| 600519 股票 | 5.0000 元（触发最低） | 5.7752 元（最低 5 + 印花 0.76 + 过户 0.0152） |

**连带修正**：`_sell_floor()` 原按 `fee×2` 估算双边，现改为 `buy_fee + sell_fee` 真实双边费用；买入/卖出登记也改为带 `code` 与 `side` 调用，股票的最低佣金与卖出税费会自动计入成本。

> 若券商对 ETF 有 0.1 元最低佣金，只需把 `by_kind.fund.min_commission` 改为 `0.1`。

---

## P1 波动率自适应跳数（百分比主导、跳数兜底）

**公式**（`_dyn_conf()`）

```
eff_ticks = clamp( round(k × ATR% ÷ 单跳%), max(保本跳数 + 1, min_ticks), max_ticks )
```

**ATR 口径**（`_atr_pct()`）：持仓窗口（默认 1200 秒）内的 `(最高 − 最低) ÷ 均价`，在 30 分钟窗口内滑动后取**中位数**。

> 早期版本按 60 秒切段求平均振幅，量纲与「持仓 20 分钟能漂多少」不一致，会让动态门槛恒撞下限、退化成固定跳数。已改为与持仓周期对齐的口径。

**k 值标定**：用 5 个交易日 1 分钟 K 线（11-17 ~ 11-23）实测

| 标的 | ATR20m 中位 | 单跳 | 反推 k（目标 4 跳） | 取值 |
|---|---|---|---|---|
| 159325 | 1.17% | 0.130% | 0.44 | k=0.45 |
| 159866 | 0.35% | 0.070% | 0.81（按 2 跳则 0.40） | k=0.45 |

**回落概率（卖出后 20 分钟内）**

| 跳数 | 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8 |
|---|---|---|---|---|---|---|---|---|
| 159325 | 94% | 84% | 71% | 58% | 46% | 38% | 30% | 21% |
| 159866 | 91% | 70% | 51% | 32% | 17% | 9% | 5% | 3% |

→ 159325 取 4 跳（58% 可回买）；159866 取 2 跳（70%），原 4 跳只有 32% 概率能平掉，属于系统性卡单。

**自适应效果**（真实日内 ATR 回放）

| 环境 | 159325 ATR | 跳数 | 159866 ATR | 跳数 |
|---|---|---|---|---|
| 低波 | 0.51% | 4→2 | 0.20% | 2→2 |
| 常态 | 0.90~1.03% | 4→4 | 0.33~0.35% | 2→2 |
| 高波 | 1.50% | 4→5 | 0.40% | 2→3 |
| 爆波 | 3.15% | 4→8（撞上限） | — | — |

样本不足（刚开盘/刚重启）时自动回落静态跳数，不会因缺数据误判。

---

## P2 按标的参数分层

配置路径 `position_overrides.<6位代码>.trade` 与 `.signals`，深度合并覆盖全局；未列出的标的自动走全局。

| 参数 | 159325 半导体ETF | 159866 日经ETF | 说明 |
|---|---|---|---|
| sell_first.target_ticks | 4 | 2 | 按各自 ATR 定 |
| sell_first.target_pct | 0.3% | 0.15% | 与 target_ticks 同量级，避免百分比抬高门槛 |
| sell_first.rebuy_ticks | 3 | 2 | |
| sell_first.above_avg_pct | 0.3% | 0.15% | |
| buy_first.below_avg_pct / dev_ticks | 0.3% / 4 | 0.15% / 2 | |
| cooldown_sec | 180 | 1800 | 日经波动小，降低频率 |
| max_sell_per_day | 3 | 1 | 反T卖出独立额度 |
| price_layer.loss_pct | 0.8% | 0.4% | 止损按各自波动收紧 |
| require_atr_feasible | true | true | P3 闸门 |

> 159866 的卖出通道未硬关闭，而是由 P3 的 ATR 闸门柔性放行——极低波日自动不出信号，波动够时仍可做。

---

## P3 卖出前可行性校验

**判据**：`ATR(20m) ≥ 回买跳数 × 单跳% × atr_feasible_ratio(1.5)`

**为什么只算回买**：ATR 是「持仓周期内的极差」，只与「卖出后要回落多少才够回买」同一量纲。卖点由实际价格触发，不需要波动去"挣"。早期把 `target+rebuy` 相加再乘 1.5，会要求 ATR ≥ 1.4%，等于把反T全部锁死——已修正。

**真实 5 日回放**

| 日期 | 159325 ATR | 结果 | 159866 ATR | 结果 |
|---|---|---|---|---|
| 11-17 | 1.03% | 放行 | 0.20% | 拦截 |
| 11-18 | 0.51% | 拦截（波动不足） | 0.33% | 放行 |
| 11-19 | 1.50% | 放行 | 0.40% | 放行 |
| 11-20 | 0.90% | 放行 | 0.35% | 放行 |
| 11-23 | 0.65% | 放行 | 0.14% | 拦截 |

---

## P4 反T卖出日上限与计数持久化

- `max_sell_per_day` 按标的生效（159325=3 / 159866=1），与 `max_signals_per_day` 的买卖合计额度解耦
- 计数落 `data\signal_state.json` 的 `count` 字段，随每次成交/信号即时 `save_state()`，**跨重启保留**、跨日由 `_roll_day()` 重置
- 直接针对上次「重启后重复高卖同一份底仓」的问题加闸：重启不再让卖出额度归零

---

## 验证与回滚

**验证**
- `py_compile` 编译通过
- 19 项断言：配置分层生效、未覆盖标的回落全局、ETF 无最低佣金、股票触发 5 元最低、地板价覆盖保本跳、四档波动下跳数自适应、无采样兜底、卖出日上限精确拦截
- 真实 1 分钟 K 线 5 日回放：动态跳数与 P3 闸门行为符合预期

**回滚**
```
temp\main.py.bak_p0p4_20261123_151251
temp\config.json.bak_p0p4_20261123_151251
```

**生效方式**：当前运行中的监控进程（PID 262560，15:01 启动）加载的是旧代码与旧配置，需重启后才生效。

---

## 运行数据目录迁移（2026-11-23，方案 B）

**问题**：日志 / 审计 / 截图 / 台账原先相对 `BASE_DIR` 写入，打包成单文件 exe 后出现两类故障：
1. `_state_file()` 用 `os.path.dirname(abspath(__file__))` 拼 `data/signal_state.json`，frozen 下 `__file__` 指向 PyInstaller 解包目录 `_MEIxxxx`，退出即被清理 → 当日闭环锁、批次台账、冷却计数每次启动归零；
2. 截图用 `Path("logs/shots")` 相对当前工作目录，从快捷方式 / 计划任务启动时散落到别处；exe 放在只读目录（如 Program Files）时日志写不进去。

**改法**：新增用户级数据根 `DATA_ROOT`，默认 `%LOCALAPPDATA%\ths_t_trader`，可用环境变量 `THS_T_DATA_DIR` 整体覆盖。

| 落点 | 迁移后位置 |
| --- | --- |
| 审计 CSV | `%LOCALAPPDATA%\ths_t_trader\logs\audit_trade.csv` |
| 运行日志 | `%LOCALAPPDATA%\ths_t_trader\logs\runtime.log` |
| 无控制台 print 兜底 | `%LOCALAPPDATA%\ths_t_trader\logs\console_<日期>.log` |
| 下单截图 | `%LOCALAPPDATA%\ths_t_trader\logs\shots\` |
| 信号台账 | `%LOCALAPPDATA%\ths_t_trader\data\signal_state.json` |

**新增/修改函数**
- `_default_data_root()`：frozen 与源码模式都返回 LOCALAPPDATA 下目录，避免两套行为。
- `resolve_log_dir(cfg)`：`logging.log_dir` 留空或相对路径 → `DATA_ROOT/logs`；绝对路径原样使用。
- `ensure_writable_dir(path, probe="")`：目录不可写时依次回退 exe 目录 `logs`、`TEMP/ths_t_trader_logs`；`probe` 传真实文件名时用追加打开验证真实可写，留空只校验目录可写（不再留 `.write_test` 垃圾文件）。
- `_console_dir`、`Auditor.dir`、`setup_runtime_logger`、`_state_file`、`--test-order` 截图、`restart_*.log` 共 9 处路径统一走上述入口。

**校验**：`py_compile` 通过；源码与打包 exe 各跑 `--once`，全部产物落在 LOCALAPPDATA，exe 目录只剩 `exe + config.json`；双击模式仅出现 `TkTopLevel` 主窗口、无 ConsoleWindowClass 黑框，日志照常写入。

**回滚**：`main.py.bak_localappdata_<时间戳>`、`config_ui.py.bak_localappdata_<时间戳>`。

---

## 重启/配置编辑器子进程证书失效修复（2026-11-24）

**现象**：打包 exe 里点「重启」或「配置」后，新实例的状态获取全部失败，日志报
`Could not find a suitable TLS CA certificate bundle, invalid path: C:\...\Temp\_MEI000446dc2\certifi\cacert.pem`；
父实例退出时还弹框 `Failed to remove temporary directory`。

**根因**：PyInstaller 单文件 exe 的 bootloader 会读 `_PYI_APPLICATION_HOME_DIR` 与 `_PYI_PARENT_PROCESS_LEVEL`。
`subprocess.Popen` 默认继承父进程环境变量，于是新实例认为自己是被拉起的下级进程，
不再独立解包，直接复用父实例的 `_MEIxxxx` 目录。父实例随后退出并清理该目录，
子进程手里的 `certifi/cacert.pem` 路径就失效了 —— TLS 校验失败、行情全部拿不到；
同时因为子进程还占着目录，父进程删不掉，于是弹出 `Failed to remove temporary directory`。

**改法**：新增 `_child_env()`，在启动子进程前剔除 `_PYI_APPLICATION_HOME_DIR`、
`_PYI_PARENT_PROCESS_LEVEL`、`_PYI_ARCHIVE_FILE`、`_PYI_SPLASH_IPC`、`_MEIPASS2`，
强制子进程解包到自己的 `_MEI` 目录。接入两处 `subprocess.Popen`：

| 位置 | 场景 |
| --- | --- |
| `_open_config_ui()` | 主界面点「配置」拉起 config_ui |
| 重启逻辑 | 「重启」拉起新实例 |

**校验**：`py_compile` 通过；`_child_env()` 单元校验确认 PYI 变量全部剔除、`THS_T_DATA_DIR`/`PATH` 等业务变量保留、父进程环境不受影响；
重新打包后 exe 连续启动两轮均正常抓取两只标的行情，退出后 `%TEMP%` 下无新增 `_MEI` 残留。
另清理了历史遗留的 64 个 `_MEI` 目录（其中 13 个各 63.5 MB，合计回收约 843 MB）。

**回滚**：`main.py.bak_childenv_20261124_121915`、`ths_t_trader.exe.bak_childenv_<时间戳>`

---

## 待观察项

1. 159866 一天仅 1 次反T + 冷却 1800 秒，若觉得过少，调 `position_overrides.159866.signals.sell_first.max_sell_per_day` 与 `trade.cooldown_sec`
2. `target_pct` 与 `target_ticks` 取大者作为卖出门槛，改动其一注意保持同量级
3. 过户费双向或仅卖出各券商口径不同，当前按仅卖出计，可按实际情况改 `transfer_fee_pct` 的收取方向
4. 全部验证基于 dry_run=true 与历史 K 线回放，未做实盘撮合验证

---

## 配置编辑器：持仓 ⇄ 行情监控联动（2026-11-24）

**问题**：持仓页填的标的不会出现在「行情监控」里，要先手工在行情页再录一次代码，两边不一致时监控列表还会漏掉实际持仓标的。

**改法**（`config_ui.py`，ConfigUI / PositionsEditor 各自新增接口）：

| 组件 | 新增方法 | 作用 |
| --- | --- | --- |
| ConfigUI | `_market_codes_row()` / `market_codes()` | 读取行情监控代码输入框 |
| ConfigUI | `position_editor()` / `position_codes()` | 读取持仓页当前代码集合 |
| ConfigUI | `sync_market_codes(added, removed)` | 增量并入/移出行情监控 |
| ConfigUI | `_initial_market_sync()` | 打开即对齐：持仓有、行情无的代码自动补入 |
| ConfigUI | `_merge_positions_into_market(data)` | 保存前兜底，只增不删 |
| PositionsEditor | `_on_code_edit(widgets)` | 代码填满 6 位即时并入行情监控 |
| PositionsEditor | `_sync_removed(code)` | 删除持仓行时，代码不再被任何持仓引用则移出行情 |

**联动规则**
1. 持仓页代码填满 6 位 → 立即并入行情监控列表；
2. 删除持仓行 → 仅当该代码不再被其它持仓引用时才从行情移除（避免误删）；
3. 保存时再兜底合并一次，只增不删，不会覆盖用户手工加的监控标的。

**校验**：无头自检 9 项全 PASS；`--selftest` 仅剩历史遗留的未映射键提示；`--gui-smoke` 报告「与磁盘差异 1 处」为持仓 600036 自动补入行情的预期行为，判定 PASSED；重新打包后 exe 直接跑 `--ui --gui-smoke`、`--ui --check` 退出码均为 0。

**同时修复**：配置编辑器窗口偏小时「保存 / 保存并退出」按钮被挤出可视区，改为固定在底部、快捷键保存与退出绑定（`_on_shortcut_save` / `_on_shortcut_exit`）。

---

*（内容由AI生成，仅供参考）*
