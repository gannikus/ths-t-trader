#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""config_ui.py —— THS-T-Assistant 图形化配置编辑器（tkinter）

独立工具，不依赖也不修改 main.py；读写脚本同目录的 config.json。

设计要点
--------
* 分组分页（Notebook）：账户与标的 / 行情 / 信号 / 交易 / 窗口 / 下单 UI / 坐标点 / 弹窗与日志 / 保留键。
* 每个字段：中文标签 + 单位 + 原始 _help 说明（分组级 _help 显示在页首，字段级说明显示在字段下方）。
* 输入校验：百分比、金额、股数（100 整数倍）、时刻 HH:MM、证券代码、屏幕坐标、URL 等；
  校验不通过不写盘，错误清单可点击定位到对应页签。
* 保存安全：先把原文件复制到 backups/config_时间戳.json，再原子写入（临时文件 + os.replace）。
* 结构保持：以磁盘原文件的深拷贝为基底，只覆盖界面上展示的字段，
  因此键名、键顺序、以及任何未在界面展示的键（含 _help / _example / 未来新增项）原样保留。
* 保存前给出改动差异确认；另提供"其他保留键"页签只读预览未被界面映射的内容。

命令行
------
    python config_ui.py                 # 打开图形界面
    python config_ui.py --config x.json # 编辑指定的配置文件
    python config_ui.py --check         # 只做校验，不打开窗口
    python config_ui.py --selftest      # 开发者自检：覆盖检查 + 校验器用例 + 结构保持验证
    python config_ui.py --gui-smoke     # 构建全部界面后立即销毁（冒烟测试，不进入事件循环）
"""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# 与 main.py 保持一致：打包后以 exe 所在目录为工作根
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"
BACKUP_DIR_NAME = "backups"
BACKUP_KEEP = 30
APP_TITLE = "THS-T-Assistant 配置编辑器 v1.0"

_MISSING = object()


# ======================================================================================
# 值/路径工具
# ======================================================================================
def path_get(tree: Any, path: Sequence[str], default: Any = _MISSING) -> Any:
    node = tree
    for key in path:
        if not isinstance(node, dict) or key not in node:
            if default is _MISSING:
                raise KeyError("配置缺少键：" + ".".join(path))
            return default
        node = node[key]
    return node


def path_set(tree: Dict[str, Any], path: Sequence[str], value: Any) -> None:
    node = tree
    for key in path[:-1]:
        nxt = node.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            node[key] = nxt
        node = nxt
    node[path[-1]] = value


def path_has(tree: Any, path: Sequence[str]) -> bool:
    return path_get(tree, path, _MISSING) is not _MISSING


def coerce_like(orig: Any, new: Any) -> Any:
    """尽量保持原值类型：int 不因输入 100.0 变成 float，float 不变成 int。"""
    if isinstance(orig, bool):
        return bool(new)
    if isinstance(new, bool):
        return new
    if isinstance(orig, int) and not isinstance(orig, bool):
        if isinstance(new, float) and float(new).is_integer():
            return int(new)
        if isinstance(new, str) and new.strip().lstrip("+-").isdigit():
            return int(new)
        return new
    if isinstance(orig, float) and isinstance(new, int):
        return float(new)
    return new


def fmt_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        text = repr(value)
        return text
    return str(value)


def now_stamp() -> str:
    t = datetime.now()
    return f"{t.year:04d}{t.month:02d}{t.day:02d}_{t.hour:02d}{t.minute:02d}{t.second:02d}"


# ======================================================================================
# 校验
# ======================================================================================
class FieldError(Exception):
    pass


HHMM_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CODE_RE = re.compile(r"^\d{6}$")


def norm_hhmm(text: str) -> str:
    m = HHMM_RE.match(text.strip())
    if not m:
        raise FieldError("时刻格式必须是 HH:MM（24 小时制，例如 09:15、14:50）")
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_num(text: str, kind: str) -> float:
    raw = (text or "").strip().replace("，", "").replace(",", "")
    if raw == "":
        raise FieldError("不能为空")
    try:
        value = float(raw)
    except ValueError:
        raise FieldError(f"必须是数字（当前输入：{text}）")
    if kind == "int" and not float(value).is_integer():
        raise FieldError(f"必须是整数（当前输入：{text}）")
    if value != value or value in (float("inf"), float("-inf")):
        raise FieldError("数值非法")
    return value


def make_validator(kind: str, lo: Optional[float], hi: Optional[float], label: str,
                   step: Optional[float] = None) -> Callable[[Any], Any]:
    def _validate(value: Any) -> Any:
        if value is None:
            raise FieldError(f"「{label}」不能为空")
        if kind in ("int", "float", "percent", "qty", "money"):
            num = parse_num(str(value), "int" if kind in ("int", "qty") else "float")
            if lo is not None and num < lo:
                raise FieldError(f"「{label}」不能小于 {lo:g}（当前 {num:g}）")
            if hi is not None and num > hi:
                raise FieldError(f"「{label}」不能大于 {hi:g}（当前 {num:g}）")
            if step:
                if abs(num / step - round(num / step)) > 1e-9:
                    raise FieldError(f"「{label}」必须是 {step:g} 的整数倍（当前 {num:g}）")
            return int(num) if kind in ("int", "qty") else float(num)
        if kind == "bool":
            if isinstance(value, str):
                return value.strip().lower() in ("1", "true", "yes", "on", "是", "y")
            return bool(value)
        if kind == "time":
            return norm_hhmm(str(value))
        if kind == "code":
            text = str(value).strip()
            if not CODE_RE.match(text):
                raise FieldError(f"「{label}」必须是 6 位数字证券代码（当前 {text}）")
            return text
        if kind == "text":
            text = str(value).strip()
            if text == "":
                raise FieldError(f"「{label}」不能为空")
            return text
        return value

    return _validate


def split_list(text: str, seps: str = ",，;； ") -> List[str]:
    parts: List[str] = []
    buf = ""
    for ch in (text or ""):
        if ch in seps:
            if buf.strip():
                parts.append(buf.strip())
            buf = ""
        else:
            buf += ch
    if buf.strip():
        parts.append(buf.strip())
    return parts


# ======================================================================================
# Schema
# ======================================================================================
KIND_INT = "int"
KIND_FLOAT = "float"
KIND_PERCENT = "percent"
KIND_QTY = "qty"
KIND_MONEY = "money"
KIND_BOOL = "bool"
KIND_TEXT = "text"
KIND_CHOICE = "choice"
KIND_STRLIST = "strlist"
KIND_CODELIST = "codelist"
KIND_TIME = "time"


@dataclass
class Field:
    path: Tuple[str, ...]
    label: str
    kind: str
    help: str = ""
    unit: str = ""
    choices: Tuple[str, ...] = ()
    lo: Optional[float] = None
    hi: Optional[float] = None
    step: Optional[float] = None

    @property
    def key(self) -> str:
        return ".".join(self.path)

    def validate(self, value: Any) -> Any:
        if self.kind == KIND_BOOL:
            return bool(value)
        if self.kind == KIND_STRLIST:
            items = value if isinstance(value, list) else split_list(str(value))
            items = [str(x).strip() for x in items if str(x).strip()]
            if not items:
                raise FieldError(f"「{self.label}」至少要有一个值")
            return items
        if self.kind == KIND_CODELIST:
            items = value if isinstance(value, list) else split_list(str(value))
            out: List[str] = []
            for item in items:
                code = str(item).strip()
                if not code:
                    continue
                if not CODE_RE.match(code):
                    raise FieldError(f"「{self.label}」中的 {code} 不是 6 位数字证券代码")
                out.append(code)
            if not out:
                raise FieldError(f"「{self.label}」至少要有一个证券代码")
            return out
        if self.kind == KIND_CHOICE:
            text = str(value).strip()
            if text not in self.choices:
                raise FieldError(f"「{self.label}」只能是 {'/'.join(self.choices)}（当前 {text}）")
            return text
        validator = make_validator(self.kind, self.lo, self.hi, self.label, self.step)
        return validator(value)


@dataclass
class TabSpec:
    title: str
    help_path: Optional[Tuple[str, ...]] = None
    fields: List[Field] = dc_field(default_factory=list)
    builders: List[str] = dc_field(default_factory=list)
    note: str = ""


def F(path: str, label: str, kind: str, help: str = "", unit: str = "",
      choices: Sequence[str] = (), lo: Optional[float] = None,
      hi: Optional[float] = None, step: Optional[float] = None) -> Field:
    return Field(tuple(path.split(".")), label, kind, help, unit, tuple(choices), lo, hi, step)


SCHEMA: List[TabSpec] = [
    TabSpec(
        title="账户与标的",
        help_path=("positions", "_help"),
        builders=["positions", "decimals"],
        note="持仓成本与可卖数量决定做T的盈亏基准和卖出上限；价格精度一般留空自动判断（ETF 3 位、股票 2 位）。",
    ),
    TabSpec(
        title="行情",
        help_path=None,
        fields=[
            F("market.poll_interval_sec", "盘中轮询间隔", KIND_INT, "交易时段内两次抓取新浪行情的间隔秒数，3 秒足够分时级别判断。", "秒", lo=1, hi=600),
            F("market.offhours_poll_interval_sec", "非交易时段轮询间隔", KIND_INT, "非交易时段行情不变化，拉长间隔减少无谓请求。", "秒", lo=1, hi=3600),
            F("market.timeout_sec", "HTTP 超时", KIND_INT, "单次请求新浪接口的超时时间，超时只记 WARNING 并在下一轮重试。", "秒", lo=1, hi=120),
            F("market.codes", "监控标的代码", KIND_CODELIST,
              "要监控的 6 位证券代码，多个用逗号分隔，例如 159325,159866。"
              "与「账户与标的」页持仓联动：持仓里的代码会自动并入这里，无需手动添加；删除持仓行也会同步移除。"
              "只想监控、不持仓的代码，直接在此添加即可（不会被持仓表覆盖）。"),
            F("market.sina_url", "新浪行情接口地址", KIND_TEXT, "必须保留 {} 占位符，脚本会把逗号分隔的代码填进去。", ""),
            F("market.referer", "请求 Referer", KIND_TEXT, "新浪接口要求带 Referer，缺失会返回空数据。", ""),
        ],
        note="接口字段固定为 https://hq.sinajs.cn/list={} 与 https://finance.sina.com.cn，一般不需要改动。",
    ),
    TabSpec(
        title="信号",
        help_path=("signals", "_help"),
        fields=[
            F("signals.buy_first.enabled", "启用正T（先买后卖）", KIND_BOOL, "关闭后不再产生低吸买入信号。"),
            F("signals.buy_first.below_avg_pct", "正T：低于均价线比例", KIND_PERCENT, "现价低于分时均价线的百分比门槛。", "%", lo=0, hi=100),
            F("signals.buy_first.dev_ticks", "正T：低于均价线跳数", KIND_INT, "按最小报价单位计的跳数门槛，0=不看跳数；实际门槛取百分比与跳数中更严的一方。", "跳", lo=0, hi=200),
            F("signals.buy_first.support_price", "正T：日内支撑价", KIND_FLOAT, "填 0 表示自动用 昨收×(1-支撑跌破比例) 作为支撑价。", "元", lo=0),
            F("signals.buy_first.support_break_pct", "正T：跌破支撑幅度", KIND_PERCENT, "跌破支撑线后不再低吸，防止越跌越买。", "%", lo=0, hi=100),
            F("signals.buy_first.max_drop_pct", "正T：当日最大跌幅", KIND_PERCENT, "当日跌幅超过该值则不再低吸（防单边下跌接刀）。", "%", lo=0, hi=100),
            F("signals.buy_first.check_cost", "正T：同时校验持仓成本", KIND_BOOL, "勾选后买入价还须不高于 成本×(1+成本容忍上浮)。正T低吸时通常关闭。"),
            F("signals.buy_first.cost_tolerance_pct", "正T：成本容忍上浮", KIND_PERCENT, "配合上一项使用，允许买入价高出成本的比例。", "%", lo=0, hi=100),
            F("signals.buy_first.strong_dev_pct", "正T：强档偏离比例", KIND_PERCENT, "偏离均价达到该比例即视为明显超跌，可跳过量能确认直接低吸；填 0 表示不启用强档分级。", "%", lo=0, hi=100),
            F("signals.buy_first.strong_dev_ticks", "正T：强档偏离跳数", KIND_INT, "按最小报价单位计的强档跳数，与强档比例取更易达到的一方。填 0 表示不看跳数。", "跳", lo=0, hi=200),
            F("signals.buy_first.volume_confirm", "正T：量能确认方式", KIND_CHOICE, "off=不看量能；weak=仅弱档偏离需要量能确认（推荐）；always=所有买入都要量能确认。", "", choices=("off", "weak", "always")),
            F("signals.buy_first.volume_dir", "正T：量能方向", KIND_CHOICE, "surge=要求放量，有承接盘才低吸；shrink=要求缩量，抛压衰竭才低吸。", "", choices=("surge", "shrink")),
            F("signals.buy_first.volume_window_sec", "正T：量能观察窗口", KIND_INT, "用近多少秒的成交量速率代表当前量能。", "秒", lo=10, hi=600),
            F("signals.buy_first.volume_baseline_sec", "正T：量能基准窗口", KIND_INT, "以观察窗口之前这么多秒的均速作为基准，两者之比即量能倍数。", "秒", lo=30, hi=3600),
            F("signals.buy_first.volume_ratio", "正T：量能倍数门槛", KIND_FLOAT, "放量要求不低于该倍数；缩量要求不高于其倒数。样本不足 6 分钟时自动跳过确认。", "倍", lo=0.1, hi=10),
            F("signals.sell_first.enabled", "启用反T（先卖后买）", KIND_BOOL, "关闭后不再产生高位卖出信号。"),
            F("signals.sell_first.basis", "反T：卖出基准", KIND_CHOICE, "batch=当日未平买入批次加权成本（真正的平T，推荐）；day_avg=当日分时均价；cost=历史持仓成本。", "", choices=("batch", "day_avg", "cost")),
            F("signals.sell_first.above_avg_pct", "反T：高于均价线比例", KIND_PERCENT, "现价高于分时均价线的百分比门槛。", "%", lo=0, hi=100),
            F("signals.sell_first.dev_ticks", "反T：高于均价线跳数", KIND_INT, "按最小报价单位计的跳数门槛，0=不看跳数。", "跳", lo=0, hi=200),
            F("signals.sell_first.target_ticks", "反T：目标价差跳数", KIND_INT, "相对基准至少要走出去的跳数，是卖出地板价的底线之一。", "跳", lo=0, hi=200),
            F("signals.sell_first.target_pct", "反T：目标价差比例", KIND_PERCENT, "相对基准至少要走出去的百分比，与跳数取更严者，再叠加双边佣金摊薄。", "%", lo=0, hi=100),
            F("signals.sell_first.require_cost_profit", "反T：额外要求高于成本", KIND_BOOL, "勾选后还要求现价高于 成本×(1+最低成本上浮)。深套标的请保持关闭，否则永不触发。"),
            F("signals.sell_first.min_profit_over_cost_pct", "反T：最低成本上浮", KIND_PERCENT, "配合上一项使用。", "%", lo=0, hi=100),
            F("signals.sell_first.trend_guard", "反T：趋势过滤", KIND_CHOICE, "off=关闭；cap=只在偏离均价线上限内才卖；slope=只在均价线未持续抬升时卖；both=两者都要满足（推荐，防卖飞）。", "", choices=("both", "cap", "slope", "off")),
            F("signals.sell_first.trend_cap_ticks", "反T：偏离均价线上限", KIND_INT, "现价高于分时均价线的跳数上限，超过视为加速拉升段，此时高卖大概率卖飞。0=不限制。", "跳", lo=0, hi=200),
            F("signals.sell_first.trend_slope_ticks", "反T：均价线抬升上限", KIND_INT, "观测窗口内分时均价线（VWAP）抬升的跳数上限，超过说明买盘持续占优。0=不限制。", "跳", lo=0, hi=200),
            F("signals.sell_first.trend_window_sec", "反T：均价线观测窗口", KIND_INT, "计算均价线抬升速度的时间窗口。样本不足时自动跳过趋势过滤。", "秒", lo=60, hi=1800),
        ],
        note="阈值单位均为百分比；低价 ETF 的百分比常常不足一跳，靠 dev_ticks / target_ticks 补足。",
    ),
    TabSpec(
        title="交易",
        help_path=("trade", "_help"),
        fields=[
            F("trade.qty_mode", "下单数量模式", KIND_CHOICE, "fixed=直接用固定股数；notional=按最低成交额反推手数（不低于固定股数）。低价 ETF 按 100 份下单时一跳价差盖不住最低佣金，建议 notional。", "", choices=("notional", "fixed")),
            F("trade.buy_qty", "每次买入股数", KIND_QTY, "固定股数，必须是 100 的整数倍；notional 模式下作为下限。", "股", lo=100, hi=10000000, step=100),
            F("trade.sell_qty", "每次卖出股数", KIND_QTY, "固定股数，必须是 100 的整数倍；正T卖出不得超过可卖底仓。", "股", lo=100, hi=10000000, step=100),
            F("trade.min_notional", "单笔最低成交额", KIND_MONEY, "notional 模式下按该金额反推手数，例如 5000 元。填 0 表示不启用。", "元", lo=0),
            F("trade.max_notional", "单笔成交额上限", KIND_MONEY, "notional 模式下限制单笔最大金额，0=不限。", "元", lo=0),
            F("trade.min_amplitude_pct", "当日最小振幅", KIND_PERCENT, "当日振幅低于该值时不出任何T信号，避免在空间盖不住成本时反复交易。填 0 表示不过滤。", "%", lo=0, hi=100),
            F("trade.max_pending_batches", "最大未平批次数", KIND_INT, "单标的允许同时存在的未平买入批次数，达到后不再触发正T买入，防止越跌越买变成加仓。", "批", lo=0, hi=20),
            F("trade.allow_stock_t_plus_1", "允许股票做正T", KIND_BOOL, "A股 T+1，当日买入不可卖；关闭时股票（非ETF/基金）不产生正T信号。"),
            F("trade.book_on_dry_run", "演练也记批次台账", KIND_BOOL, "勾选后 dry_run 也会推进批次台账，便于空跑观察完整买卖循环。"),
            F("trade.cooldown_sec", "信号冷却时间", KIND_INT, "同标的同类型信号的冷却秒数，冷却期内不重复触发。", "秒", lo=0, hi=7200),
            F("trade.max_signals_per_day", "每日信号上限", KIND_INT, "当日单标的单类型信号的最大次数，按自然日重置。", "次", lo=1, hi=200),
            F("trade.allow_offhours_signal", "非交易时段也出信号", KIND_BOOL, "默认关闭；盘后调试阈值时可临时开启。"),
            F("trade.fees.commission_rate_pct", "佣金费率", KIND_PERCENT, "按成交额比例计，用于计算平T地板价里的双边佣金摊薄，请按券商实际费率填写。", "%", lo=0, hi=10),
            F("trade.fees.min_commission", "最低佣金", KIND_MONEY, "不足该金额时按最低佣金计。", "元", lo=0),
            F("trade.fees.stamp_tax_pct", "印花税率", KIND_PERCENT, "ETF/基金为 0；股票卖出按实际费率填写。", "%", lo=0, hi=10),
            F("trade.no_overnight", "收盘前不留新T", KIND_BOOL, "勾选后临近收盘不再开新正T，避免当日买入却来不及卖出而被留成隔夜仓位。"),
            F("trade.close_guard_minutes", "收盘前禁止开仓分钟数", KIND_INT, "距当日最后一个交易时段结束不足该分钟数时，不再产生正T买入信号。", "分钟", lo=0, hi=120),
            F("trade.time_stop.enabled", "启用时间止损", KIND_BOOL, "存在未平买入批次时，按时间层或价格层规则强制平仓，避免T单隔夜套牢。"),
            F("trade.time_stop.time_layer.enabled", "启用时间层", KIND_BOOL, "时间层只管「不留隔夜」：到点或超期强制平仓，与浮亏多少无关。"),
            F("trade.time_stop.time_layer.deadline", "强制平仓时点", KIND_TIME, "到达该时刻仍有未平批次则强制平仓，格式 HH:MM。", ""),
            F("trade.time_stop.time_layer.max_hold_minutes", "最长持有时间", KIND_INT, "单个批次持有超过该分钟数则强制平仓，0=不限制。", "分钟", lo=0, hi=600),
            F("trade.time_stop.time_layer.trigger_on_deadline", "到点强制触发", KIND_BOOL, "关闭后到点不平仓，只按超时与浮亏判定（不建议关闭，会留隔夜）。"),
            F("trade.time_stop.price_layer.enabled", "启用价格层", KIND_BOOL, "价格层只管「单笔亏损封顶」：浮亏达阈值提前止损，与到点无关。"),
            F("trade.time_stop.price_layer.loss_pct", "浮亏止损比例", KIND_PERCENT, "账面浮亏达到该比例即触发止损；会被自动抬高到不小于目标价差距离，避免止盈没到止损先到。", "%", lo=0, hi=100),
            F("trade.time_stop.min_stop_distance", "止损距离不小于目标距离", KIND_BOOL, "勾选后止损比例的下限自动取 目标跳数×一跳 与 目标比例 中更严的一方，保证盈亏比不小于 1:1。"),
            F("trade.time_stop.cooldown_after_stop_sec", "止损后冷静期", KIND_INT, "止损成交后该标的禁止再开新T的秒数，防止止损后立刻追回。", "秒", lo=0, hi=7200),
            F("trade.time_stop.reentry_gap_pct", "止损后接回门槛", KIND_PERCENT, "冷静期结束后，现价须比止损价再低该比例才允许重新开正T。", "%", lo=0, hi=20),
            F("trade.daily_loss_circuit.enabled", "启用日内亏损熔断", KIND_BOOL, "当日已实现亏损触及上限后，正T/反T全部停手，仅保留时间止损平仓。"),
            F("trade.daily_loss_circuit.max_loss_amount", "熔断亏损金额", KIND_MONEY, "当日已实现亏损达到该金额即熔断，0=不看金额。", "元", lo=0),
            F("trade.daily_loss_circuit.max_loss_pct", "熔断亏损比例", KIND_PERCENT, "当日已实现亏损占持仓成本的比例达到该值即熔断，0=不看比例。", "%", lo=0, hi=100),
        ],
        builders=["sessions"],
        note="交易时段、股数、佣金与时间止损都影响下单决策，改动后建议先 dry_run 观察一轮。",
    ),
    TabSpec(
        title="窗口",
        help_path=("window", "_help"),
        fields=[
            F("window.process_name", "同花顺进程名", KIND_TEXT, "用于判断客户端是否运行，例如 happ.exe。"),
            F("window.process_exe", "同花顺可执行文件路径", KIND_TEXT, "用于按路径精确定位进程，可留空。"),
            F("window.title_keywords", "主窗口标题关键词", KIND_STRLIST, "多个关键词用逗号分隔，命中任一即认为找到主窗口。"),
            F("window.require_foreground", "要求窗口在前台", KIND_BOOL, "勾选后只有同花顺在前台才算窗口正常（更严格）。"),
        ],
        note="窗口缺失只提示，不影响行情监控与信号判定。",
    ),
    TabSpec(
        title="下单 UI",
        help_path=("order_ui", "_help"),
        fields=[
            F("order_ui.dry_run", "演练模式（不真实下单）", KIND_BOOL, "强烈建议先保持勾选。取消勾选后脚本会真实移动鼠标并点击同花顺下单界面。"),
            F("order_ui.bring_to_front", "下单前激活窗口", KIND_BOOL, "真实下单前把同花顺窗口置前。"),
            F("order_ui.dpi_aware", "启用 DPI 感知", KIND_BOOL, "高分屏下保持坐标准确，建议开启。"),
            F("order_ui.action_delay_ms", "每步动作间隔", KIND_INT, "点击/输入之间的等待毫秒数，界面卡顿时可调大。", "ms", lo=0, hi=5000),
            F("order_ui.type_interval_sec", "逐字输入间隔", KIND_FLOAT, "模拟键盘输入每个字符之间的间隔，过快可能丢字。", "秒", lo=0, hi=2),
            F("order_ui.qty_unit", "数量输入单位", KIND_CHOICE, "shares=按股数输入；lots=按手数输入。", "", choices=("shares", "lots")),
            F("order_ui.price_mode", "价格取值方式", KIND_CHOICE, "signal=信号价；bid=买一价；ask=卖一价；last=现价。", "", choices=("signal", "bid", "ask", "last")),
            F("order_ui.price_offset_ticks", "价格偏移跳数", KIND_INT, "负值更保守（买更低/卖更高前请核对手工习惯）；1 跳=ETF 0.001，股票 0.01。", "跳", lo=-50, hi=50),
            F("order_ui.screenshot", "下单过程截图", KIND_BOOL, "每次真实下单可截图存数据目录 logs\\shots\\ 便于复盘（数据目录默认 %LOCALAPPDATA%\\ths_t_trader）。"),
            F("order_ui.locate_at_runtime", "运行时实时定位控件", KIND_BOOL, "真实下单前按控件重新定位并覆盖静态坐标，窗口被拖动或换分辨率后无需重新拾取。"),
            F("order_ui.auto_confirm_order", "自动确认委托框", KIND_BOOL, "勾选后脚本读取确认框回显的代码/价格/数量，与预期一致才点「是」，不一致点「否」并中止。关闭则一律点「否」，只核对不提交。"),
            F("order_ui.dialog_timeout_sec", "等待委托框超时", KIND_FLOAT, "等待同花顺委托确认框出现的秒数。", "秒", lo=0.5, hi=60),
        ],
        builders=["coords"],
        note="coords 坐标点在「坐标点」页签单独编辑；取消 dry_run 前请确认坐标已全部配置（[0,0] 会被拒绝执行）。",
    ),
    TabSpec(
        title="坐标点",
        builders=["coords_page"],
        note="坐标是屏幕绝对像素。点「拾取」后请在 3 秒内把鼠标移到同花顺对应控件上，到时自动记录当前鼠标位置；[0,0] 表示未配置。",
    ),
    TabSpec(
        title="弹窗与日志",
        help_path=("ui", "_help"),
        fields=[
            F("ui.popup_timeout_sec", "确认弹窗倒计时", KIND_INT, "信号确认窗的倒计时秒数，归零即放弃，绝不会自动下单。", "秒", lo=1, hi=300),
            F("ui.sound", "信号提示音", KIND_BOOL, "有新信号时播放系统提示音。"),
            F("ui.always_on_top", "确认窗置顶", KIND_BOOL, "确认窗保持在最前，避免被同花顺挡住。"),
            F("ui.show_dashboard", "显示状态看板", KIND_BOOL, "常驻运行时显示实时状态看板窗口。"),
            F("logging.log_dir", "日志目录", KIND_TEXT, "留空或填相对路径（如 logs）时落到 %LOCALAPPDATA%\\ths_t_trader\\logs；填绝对路径则原样使用。审计 CSV、运行日志、下单截图都写这里。"),
            F("logging.csv_encoding", "CSV 编码", KIND_CHOICE, "utf-8-sig 便于 Excel 双击直接打开。", "", choices=("utf-8-sig", "utf-8", "gbk")),
            F("logging.audit_filename", "审计日志文件名", KIND_TEXT, "记录信号与下单动作的 CSV 文件名。"),
            F("logging.runtime_filename", "运行日志文件名", KIND_TEXT, "记录程序运行轨迹的文本日志文件名。"),
        ],
        note="ui 与 logging 分组位于 config.json 的两个独立段落，此处合并在同一页签展示。",
    ),
]

# 由专用编辑器整体接管的路径前缀（其中的子键不逐个登记 Field）
COVERED_PREFIXES: Tuple[Tuple[str, ...], ...] = (
    ("positions",),
    ("price_decimals",),
    ("trade", "sessions"),
    ("order_ui", "coords"),
)

ALL_FIELDS: List[Field] = [f for tab in SCHEMA for f in tab.fields]
FIELD_BY_KEY: Dict[str, Field] = {f.key: f for f in ALL_FIELDS}


# ======================================================================================
# 配置读写 / 备份
# ======================================================================================
def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"找不到配置文件：{path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("配置文件根节点必须是 JSON 对象")
    return data


def dump_json_text(data: Dict[str, Any]) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2) + "\n"


def write_json_atomic(path: Path, data: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(dump_json_text(data), encoding="utf-8")
    os.replace(tmp, path)


def backup_config(path: Path, keep: int = BACKUP_KEEP) -> Path:
    bdir = path.parent / BACKUP_DIR_NAME
    bdir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()
    dst = bdir / f"config_{stamp}.json"
    seq = 1
    while dst.exists():
        dst = bdir / f"config_{stamp}_{seq}.json"
        seq += 1
    shutil.copy2(path, dst)
    olds = sorted(bdir.glob("config_*.json"))
    if len(olds) > keep:
        for old in olds[: len(olds) - keep]:
            try:
                old.unlink()
            except OSError:
                pass
    return dst


def diff_tree(old: Any, new: Any, prefix: Tuple[str, ...] = ()) -> List[Tuple[str, Any, Any]]:
    out: List[Tuple[str, Any, Any]] = []
    if isinstance(old, dict) and isinstance(new, dict):
        keys = list(old.keys()) + [k for k in new.keys() if k not in old]
        for key in keys:
            out.extend(diff_tree(old.get(key, _MISSING), new.get(key, _MISSING), prefix + (key,)))
        return out
    if old is _MISSING and new is _MISSING:
        return out
    if old != new:
        out.append((".".join(prefix), None if old is _MISSING else old, None if new is _MISSING else new))
    return out


def leaf_paths(node: Any, prefix: Tuple[str, ...] = ()) -> List[Tuple[Tuple[str, ...], Any]]:
    out: List[Tuple[Tuple[str, ...], Any]] = []
    if isinstance(node, dict):
        for key, val in node.items():
            out.extend(leaf_paths(val, prefix + (key,)))
    else:
        out.append((prefix, node))
    return out


def validate_config(cfg: Dict[str, Any]) -> List[str]:
    """按 schema 校验一个完整配置字典，返回问题列表（空表示通过）。"""
    problems: List[str] = []
    for fld in ALL_FIELDS:
        try:
            value = path_get(cfg, fld.path)
        except KeyError:
            problems.append(f"缺少配置项 {fld.key}")
            continue
        try:
            fld.validate(value)
        except FieldError as exc:
            problems.append(f"{fld.key}: {exc}")
    # 由专用编辑器接管的段落做轻量校验
    try:
        sessions = path_get(cfg, ("trade", "sessions"))
        if not isinstance(sessions, list) or not sessions:
            problems.append("trade.sessions: 至少要有一个交易时段")
        else:
            for idx, item in enumerate(sessions, 1):
                if not isinstance(item, (list, tuple)) or len(item) != 2:
                    problems.append(f"trade.sessions 第 {idx} 段格式必须是 [开始, 结束]")
                    continue
                try:
                    start, end = norm_hhmm(str(item[0])), norm_hhmm(str(item[1]))
                except FieldError as exc:
                    problems.append(f"trade.sessions 第 {idx} 段：{exc}")
                    continue
                if start >= end:
                    problems.append(f"trade.sessions 第 {idx} 段开始时间必须早于结束时间（{start} → {end}）")
    except KeyError:
        problems.append("缺少 trade.sessions")
    positions = cfg.get("positions")
    if not isinstance(positions, dict):
        problems.append("positions 必须是对象")
    else:
        items = {k: v for k, v in positions.items() if not str(k).startswith("_")}
        if not items:
            problems.append("positions 至少要配置一只标的")
        for code, item in items.items():
            if not CODE_RE.match(str(code)):
                problems.append(f"positions 的键 {code} 必须是 6 位数字代码")
            if not isinstance(item, dict):
                problems.append(f"positions.{code} 必须是对象")
                continue
            try:
                make_validator(KIND_FLOAT, 0, None, "持仓成本")(item.get("cost"))
                make_validator(KIND_QTY, 0, None, "可卖数量", step=100)(item.get("sellable_qty"))
            except FieldError as exc:
                problems.append(f"positions.{code}: {exc}")
    coords = cfg.get("order_ui", {}).get("coords", {})
    if isinstance(coords, dict):
        for name, val in coords.items():
            if not isinstance(val, (list, tuple)) or len(val) != 2:
                problems.append(f"order_ui.coords.{name} 必须是 [x, y]")
                continue
            for axis in val:
                try:
                    num = make_validator(KIND_INT, 0, 20000, f"坐标 {name}")(axis)
                    _ = num
                except FieldError as exc:
                    problems.append(str(exc))
    if isinstance(cfg.get("price_decimals"), dict):
        for key, val in cfg["price_decimals"].items():
            if str(key).startswith("_"):
                continue
            if val is None:
                continue
            if key == "default":
                try:
                    make_validator(KIND_INT, 0, 4, "默认小数位")(val)
                except FieldError as exc:
                    problems.append(f"price_decimals.default: {exc}")
                continue
            if not CODE_RE.match(str(key)):
                problems.append(f"price_decimals 的键 {key} 必须是 6 位数字代码")
            try:
                make_validator(KIND_INT, 0, 4, f"{key} 小数位")(val)
            except FieldError as exc:
                problems.append(f"price_decimals.{key}: {exc}")
    return problems


def coverage_report(cfg: Dict[str, Any]) -> Dict[str, List[str]]:
    """返回 {missing_field_paths, unmapped_paths}：前者=界面声明但配置里缺的键，后者=配置里有但界面未映射的键。"""
    missing = [f.key for f in ALL_FIELDS if not path_has(cfg, f.path)]
    unmapped: List[str] = []
    mapped_keys = {f.path for f in ALL_FIELDS}
    for path, _value in leaf_paths(cfg):
        if not path:
            continue
        if path in mapped_keys:
            continue
        if any(path[: len(pre)] == pre for pre in COVERED_PREFIXES):
            continue
        # 以 _ 开头的键是注释/示例（_help、_example），已在页面顶部作为说明展示，保存时原样保留
        if path[-1].startswith("_"):
            continue
        unmapped.append(".".join(path))
    return {"missing_field_paths": missing, "unmapped_paths": unmapped}


# ======================================================================================
# tkinter 界面
# ======================================================================================
def _enable_dpi() -> None:
    try:
        import ctypes

        try:
            # 与 main.py 的 ensure_dpi_aware() 保持同一级别（per-monitor v2）：
            # 编辑器若用 (1) system-aware，多屏不同缩放下 winfo_pointerxy 读到的
            # 坐标与执行端 ClientToScreen 换算口径不一致，会出现整体偏移。
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


KIND_LABELS = {
    KIND_INT: "整数",
    KIND_FLOAT: "小数",
    KIND_PERCENT: "百分比",
    KIND_QTY: "股数",
    KIND_MONEY: "金额",
    KIND_BOOL: "开关",
    KIND_TEXT: "文本",
    KIND_CHOICE: "选项",
    KIND_STRLIST: "列表",
    KIND_CODELIST: "代码列表",
    KIND_TIME: "时刻",
}


class ConfigUI:
    """图形化配置编辑器主窗体。"""

    def __init__(self, config_path: Path) -> None:
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.path = Path(config_path)
        self.original: Dict[str, Any] = load_config(self.path)
        self.fields: Dict[Tuple[str, ...], "FieldRow"] = {}
        self.editors: List[Any] = []
        self.dirty = False

        self.root = tk.Tk()
        self.root.title(f"{APP_TITLE} —— {self.path.name}")
        self.root.geometry("1120x780")
        self.root.minsize(960, 620)
        self._init_style()
        self._build()
        try:
            self.root.protocol("WM_DELETE_WINDOW", self.on_exit)
        except Exception:
            pass
        self.status(f"已加载 {self.path.name}（Ctrl+S 保存）")
        self._initial_market_sync()

    # ---------------------------------------------------------------- 样式与骨架
    def _init_style(self) -> None:
        ttk, tk = self.ttk, self.tk
        style = ttk.Style(self.root)
        try:
            style.theme_use("vista")
        except Exception:
            pass
        base = ("Microsoft YaHei UI", 10)
        small = ("Microsoft YaHei UI", 9)
        style.configure(".", font=base)
        style.configure("TNotebook.Tab", font=base, padding=(14, 6))
        style.configure("FieldLabel.TLabel", font=base)
        style.configure("Hint.TLabel", font=small, foreground="#5a6472")
        style.configure("Help.TLabel", font=small, foreground="#2f4b7c")
        style.configure("Section.TLabel", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Status.TLabel", font=small, foreground="#3a4a5f")
        style.configure("Warn.TLabel", font=small, foreground="#a5471b")

    def _build(self) -> None:
        ttk, tk = self.ttk, self.tk

        header = ttk.Frame(self.root, padding=(12, 10, 12, 4))
        header.pack(fill="x")
        ttk.Label(header, text="配置文件", style="Section.TLabel").pack(side="left")
        ttk.Label(header, text=str(self.path), style="Hint.TLabel").pack(side="left", padx=(8, 0))

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=10, pady=(4, 0))
        self._build_tabs()

        footer = ttk.Frame(self.root, padding=(12, 8, 12, 10))
        footer.pack(fill="x")
        # 状态栏限宽 + 按钮区独立成 column：避免长状态文本挤占空间导致按钮不可见
        footer.columnconfigure(0, weight=1)
        self.status_var = tk.StringVar(value="")
        ttk.Label(footer, textvariable=self.status_var, style="Status.TLabel",
                  anchor="w", width=42).grid(row=0, column=0, sticky="w")
        btns = ttk.Frame(footer)
        btns.grid(row=0, column=1, sticky="e", padx=(8, 0))
        ttk.Button(btns, text="打开备份目录", width=14,
                   command=self.on_open_backup).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="放弃修改并重载", width=16,
                   command=self.on_reload).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="校验", width=8,
                   command=self.on_check).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="退出", width=8,
                   command=self.on_exit).pack(side="left", padx=(0, 8))
        ttk.Button(btns, text="保存到配置文件", width=18,
                   command=self.on_save).pack(side="left")
        # 快捷键兜底：即使界面缩放异常也能保存/退出
        for seq in ("<Control-s>", "<Control-S>"):
            self.root.bind(seq, self._on_shortcut_save)
        for seq in ("<Control-q>", "<Control-Q>"):
            self.root.bind(seq, self._on_shortcut_exit)

    def _on_shortcut_save(self, _event: Any = None) -> str:
        self.on_save()
        return "break"

    def _on_shortcut_exit(self, _event: Any = None) -> str:
        self.on_exit()
        return "break"

    def _build_tabs(self) -> None:
        for tab in SCHEMA:
            page = ScrollPage(self.nb)
            self.nb.add(page, text=tab.title)
            self._fill_page(page.inner, tab)
        page = ScrollPage(self.nb)
        self.nb.add(page, text="保留键（只读）")
        self._fill_unmapped(page.inner)

    STATUS_MAX = 42

    def status(self, text: str, warn: bool = False) -> None:
        text = str(text).replace("\n", " ")
        if len(text) > self.STATUS_MAX:
            text = "…" + text[-(self.STATUS_MAX - 1):]
        self.status_var.set(text)

    # ---------------------------------------------------------------- 持仓 ⇄ 行情 联动
    def _market_codes_row(self) -> Optional["FieldRow"]:
        return self.fields.get(("market", "codes"))

    def market_codes(self) -> List[str]:
        """当前界面上「监控标的代码」的代码列表。"""
        row = self._market_codes_row()
        if row is None:
            return []
        return [c for c in split_list(str(row.var.get())) if c]

    def position_editor(self) -> Optional["PositionsEditor"]:
        for editor in self.editors:
            if isinstance(editor, PositionsEditor):
                return editor
        return None

    def position_codes(self) -> List[str]:
        editor = self.position_editor()
        return editor.codes() if editor is not None else []

    def sync_market_codes(self, added: Sequence[str] = (), removed: Sequence[str] = ()) -> List[str]:
        """把持仓变化同步到「监控标的代码」输入框，返回本次实际变动的代码。"""
        row = self._market_codes_row()
        if row is None:
            return []
        codes = self.market_codes()
        touched: List[str] = []
        drop = {str(c).strip() for c in removed if str(c).strip()}
        if drop:
            kept = [c for c in codes if c not in drop]
            if kept != codes:
                touched.extend(c for c in codes if c in drop)
                codes = kept
        for code in added:
            code = str(code).strip()
            if not code or not CODE_RE.match(code):
                continue
            if code not in codes:
                codes.append(code)
                touched.append(code)
        if touched:
            row.var.set(", ".join(codes))
            self.dirty = True
        return touched

    def _initial_market_sync(self) -> None:
        """打开编辑器/重载配置时，把持仓里已有的标的补进行情监控列表。"""
        positions = self.original.get("positions") or {}
        pos_codes = [str(k) for k in positions if not str(k).startswith("_")]
        current = self.market_codes()
        added = [c for c in pos_codes if c not in current]
        if not added:
            return
        touched = self.sync_market_codes(added=added)
        if touched:
            self.status(f"已并入 {len(touched)} 个持仓标的到行情，待保存")

    def _merge_positions_into_market(self, data: Dict[str, Any]) -> List[str]:
        """保存前的兜底：持仓表里的代码一定并入 market.codes（只增不删）。"""
        positions = data.get("positions") or {}
        pos_codes = [str(k) for k in positions if not str(k).startswith("_")]
        if not pos_codes:
            return []
        try:
            current = path_get(data, ("market", "codes"))
        except KeyError:
            current = []
        codes = [str(c).strip() for c in (current or []) if str(c).strip()]
        for code in pos_codes:
            if code not in codes:
                codes.append(code)
        if codes:
            path_set(data, ("market", "codes"), codes)
        return []

    # ---------------------------------------------------------------- 页面构建
    def _fill_page(self, parent: Any, tab: TabSpec) -> None:
        ttk = self.ttk
        if tab.note:
            ttk.Label(parent, text=tab.note, style="Hint.TLabel", wraplength=1000,
                      justify="left").pack(fill="x", padx=14, pady=(10, 2))
        if tab.help_path and path_has(self.original, tab.help_path):
            help_text = str(path_get(self.original, tab.help_path))
            if help_text:
                box = ttk.LabelFrame(parent, text="原配置说明", padding=(10, 6))
                box.pack(fill="x", padx=14, pady=(6, 8))
                ttk.Label(box, text=help_text, style="Help.TLabel", wraplength=980,
                          justify="left").pack(fill="x")
        for builder in tab.builders:
            if builder == "positions":
                self.editors.append(PositionsEditor(self, parent))
            elif builder == "decimals":
                self.editors.append(DecimalsEditor(self, parent))
            elif builder == "sessions":
                self.editors.append(SessionsEditor(self, parent))
            elif builder == "coords":
                continue  # 坐标在独立页签编辑
            elif builder == "coords_page":
                self.editors.append(CoordsEditor(self, parent))
        if tab.fields:
            section = ttk.Frame(parent)
            section.pack(fill="x", padx=2, pady=(4, 14))
            for fld in tab.fields:
                try:
                    value = path_get(self.original, fld.path)
                except KeyError:
                    value = None
                row = FieldRow(self, section, fld, value)
                row.pack(fill="x", padx=12, pady=(4, 6))
                self.fields[fld.path] = row

    def _fill_unmapped(self, parent: Any) -> None:
        ttk, tk = self.ttk, self.tk
        report = coverage_report(self.original)
        ttk.Label(parent, text="以下键未在界面中单独映射，保存时会原样保留（不会被删除或改写成默认值）。",
                  style="Hint.TLabel", wraplength=1000, justify="left").pack(fill="x", padx=14, pady=(10, 4))
        if report["missing_field_paths"]:
            ttk.Label(parent, text="界面声明但配置中缺少的键：" + "、".join(report["missing_field_paths"]),
                      style="Warn.TLabel", wraplength=1000, justify="left").pack(fill="x", padx=14, pady=(2, 6))
        frame = ttk.LabelFrame(parent, text=f"未映射键（{len(report['unmapped_paths'])} 项）", padding=(10, 6))
        frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))
        text = tk.Text(frame, height=22, wrap="none", font=("Consolas", 9))
        text.pack(side="left", fill="both", expand=True)
        bar = ttk.Scrollbar(frame, orient="vertical", command=text.yview)
        bar.pack(side="right", fill="y")
        text.configure(yscrollcommand=bar.set)
        lines = []
        for key in report["unmapped_paths"]:
            try:
                val = path_get(self.original, tuple(key.split(".")))
            except KeyError:
                val = ""
            lines.append(f"{key} = {json.dumps(val, ensure_ascii=False)}")
        if not lines:
            lines = ["（无：当前配置的全部键均已在界面中映射）"]
        text.insert("1.0", "\n".join(lines))
        text.configure(state="disabled")

    # ---------------------------------------------------------------- 收集与校验
    def collect(self) -> Tuple[Dict[str, Any], List[str]]:
        data = copy.deepcopy(self.original)
        errors: List[str] = []
        for path_key, row in self.fields.items():
            try:
                value = row.get_value()
            except FieldError as exc:
                errors.append(f"{row.fld.key}: {exc}")
                continue
            try:
                orig = path_get(self.original, path_key)
            except KeyError:
                orig = _MISSING
            path_set(data, path_key, coerce_like(orig if orig is not _MISSING else value, value))
        for editor in self.editors:
            try:
                editor.apply(data)
            except FieldError as exc:
                errors.append(str(exc))
        if not errors:
            errors.extend(self._merge_positions_into_market(data))
        if not errors:
            errors.extend(validate_config(data))
        return data, errors

    def _jump_to_error(self, message: str) -> None:
        key = message.split(":", 1)[0].strip()
        for idx, tab in enumerate(SCHEMA):
            if any(f.key == key for f in tab.fields):
                self.nb.select(idx)
                return
            if key.startswith("positions") and "positions" in tab.builders:
                self.nb.select(idx)
                return
            if key.startswith("price_decimals") and "decimals" in tab.builders:
                self.nb.select(idx)
                return
            if key.startswith("trade.sessions") and "sessions" in tab.builders:
                self.nb.select(idx)
                return
            if key.startswith("order_ui.coords") and "coords_page" in tab.builders:
                self.nb.select(idx)
                return

    # ---------------------------------------------------------------- 动作
    def on_check(self) -> None:
        from tkinter import messagebox

        data, errors = self.collect()
        if errors:
            messagebox.showerror("校验未通过", "发现 %d 处问题：\n\n%s" % (len(errors), "\n".join(errors[:20])))
            self._jump_to_error(errors[0])
            self.status(f"校验失败：{len(errors)} 处问题")
            return
        diffs = diff_tree(self.original, data)
        messagebox.showinfo("校验通过", f"全部 {len(ALL_FIELDS)} 个字段校验通过；相对磁盘文件有 {len(diffs)} 处改动。")
        self.status(f"校验通过，待保存改动 {len(diffs)} 处")

    def on_save(self) -> None:
        from tkinter import messagebox

        data, errors = self.collect()
        if errors:
            messagebox.showerror("无法保存", "发现 %d 处问题，请先修正：\n\n%s" % (len(errors), "\n".join(errors[:20])))
            self._jump_to_error(errors[0])
            self.status(f"保存中止：{len(errors)} 处校验问题")
            return
        diffs = diff_tree(self.original, data)
        if not diffs:
            messagebox.showinfo("无需保存", "界面内容与磁盘配置完全一致。")
            self.status("无改动")
            return
        lines = []
        for key, old, new in diffs[:40]:
            old_text = "（不存在）" if old is None else json.dumps(old, ensure_ascii=False)
            new_text = "（删除）" if new is None else json.dumps(new, ensure_ascii=False)
            lines.append(f"{key}\n    {old_text}  →  {new_text}")
        if len(diffs) > 40:
            lines.append(f"... 还有 {len(diffs) - 40} 处改动")
        if not messagebox.askokcancel("确认保存", f"共 {len(diffs)} 处改动：\n\n" + "\n".join(lines) + "\n\n保存前会自动备份原配置。"):
            self.status("已取消保存")
            return
        try:
            backup = backup_config(self.path)
            write_json_atomic(self.path, data)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("保存失败", f"{exc}")
            self.status(f"保存失败：{exc}")
            return
        self.original = data
        messagebox.showinfo("保存成功", f"已写入：\n{self.path}\n\n原配置备份：\n{backup}")
        self.status(f"已保存（{len(diffs)} 处改动），备份：{backup.name}")
        self.nb.select(len(SCHEMA))

    def on_reload(self) -> None:
        from tkinter import messagebox

        if not messagebox.askokcancel("放弃修改", "将丢弃界面上的所有修改，重新读取磁盘上的配置文件。继续？"):
            return
        try:
            self.original = load_config(self.path)
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("读取失败", str(exc))
            return
        for child in list(self.nb.children.values()):
            pass
        for idx in range(len(self.nb.tabs())):
            self.nb.forget(0)
        self.fields.clear()
        self.editors.clear()
        self._build_tabs()
        self.status(f"已重新加载 {self.path}")
        self._initial_market_sync()

    def on_open_backup(self) -> None:
        import subprocess

        bdir = self.path.parent / BACKUP_DIR_NAME
        bdir.mkdir(parents=True, exist_ok=True)
        try:
            subprocess.Popen(["explorer", str(bdir)])
            self.status(f"已打开备份目录：{bdir}")
        except Exception as exc:  # noqa: BLE001
            self.status(f"打开目录失败：{exc}")

    def on_exit(self) -> None:
        from tkinter import messagebox

        data, errors = self.collect()
        if not errors and diff_tree(self.original, data):
            ans = messagebox.askyesnocancel(
                "退出",
                "存在未保存的修改。\n\n是：保存后退出\n否：不保存直接退出\n取消：返回继续编辑",
            )
            if ans is None:
                return
            if ans:
                self.on_save()
                data2, errors2 = self.collect()
                if errors2 or diff_tree(self.original, data2):
                    # 保存被取消或失败，保持窗口打开
                    return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()


# ----------------------------------------------------------------------------------
# 基础控件
# ----------------------------------------------------------------------------------
try:  # GUI 依赖：导入失败时仅命令行模式仍可使用
    import tkinter as _tk
    from tkinter import ttk as _ttk
    GUI_IMPORT_ERROR: Optional[str] = None
except Exception as _exc:  # pragma: no cover - 无图形环境
    _tk = None
    _ttk = None
    GUI_IMPORT_ERROR = str(_exc)

if _ttk is not None:

    class ScrollPage(_ttk.Frame):
        """可滚动页签容器（本身即 Notebook 页）。"""

        def __init__(self, parent: Any) -> None:
            super().__init__(parent)
            self.canvas = _tk.Canvas(self, borderwidth=0, highlightthickness=0)
            self.vsb = _ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
            self.canvas.configure(yscrollcommand=self.vsb.set)
            self.vsb.pack(side="right", fill="y")
            self.canvas.pack(side="left", fill="both", expand=True)
            self.inner = _ttk.Frame(self.canvas)
            self._win = self.canvas.create_window((0, 0), window=self.inner, anchor="nw")
            self.inner.bind("<Configure>", self._on_inner)
            self.canvas.bind("<Configure>", self._on_canvas)
            self.canvas.bind("<Enter>", self._on_enter)
            self.canvas.bind("<Leave>", self._on_leave)

        def _on_inner(self, _event: Any = None) -> None:
            self.canvas.configure(scrollregion=self.canvas.bbox("all"))

        def _on_canvas(self, event: Any) -> None:
            self.canvas.itemconfigure(self._win, width=event.width)

        def _on_enter(self, _event: Any = None) -> None:
            self.canvas.bind_all("<MouseWheel>", self._on_wheel)

        def _on_leave(self, _event: Any = None) -> None:
            self.canvas.unbind_all("<MouseWheel>")

        def _on_wheel(self, event: Any) -> None:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

else:  # pragma: no cover - 无图形环境占位

    class ScrollPage:  # type: ignore[no-redef]
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("当前环境不可用 tkinter：" + str(GUI_IMPORT_ERROR))


class FieldRow:
    """单字段行：中文标签 + 控件 + 单位 + 说明。"""

    def __init__(self, ui: ConfigUI, parent: Any, fld: Field, value: Any) -> None:
        tk, ttk = ui.tk, ui.ttk
        self.ui = ui
        self.fld = fld
        self.frame = ttk.Frame(parent)
        opts = dict(font=("Microsoft YaHei UI", 9))
        top = ttk.Frame(self.frame)
        top.pack(fill="x")
        ttk.Label(top, text=fld.label, style="FieldLabel.TLabel", width=24,
                  anchor="w").pack(side="left")
        self.var: Any
        self.widget: Any
        if fld.kind == KIND_BOOL:
            self.var = tk.BooleanVar(value=bool(value))
            self.widget = ttk.Checkbutton(top, variable=self.var, text="开启")
            self.widget.pack(side="left")
        elif fld.kind == KIND_CHOICE:
            self.var = tk.StringVar(value="" if value is None else str(value))
            self.widget = ttk.Combobox(top, textvariable=self.var, values=list(fld.choices),
                                       state="readonly", width=14)
            self.widget.pack(side="left")
        else:
            if fld.kind in (KIND_STRLIST, KIND_CODELIST):
                text = ", ".join(str(x) for x in (value or [])) if isinstance(value, list) else ("" if value is None else str(value))
                width = 62
            elif fld.kind == KIND_TEXT:
                text = "" if value is None else str(value)
                width = 62
            elif fld.kind == KIND_TIME:
                text = "" if value is None else str(value)
                width = 10
            else:
                text = "" if value is None else fmt_cell(value)
                width = 16
            self.var = tk.StringVar(value=text)
            self.widget = ttk.Entry(top, textvariable=self.var, width=width, **opts)
            self.widget.pack(side="left")
        if fld.unit:
            ttk.Label(top, text=fld.unit, style="Hint.TLabel").pack(side="left", padx=(6, 0))
        ttk.Label(top, text=f"[{KIND_LABELS.get(fld.kind, fld.kind)}]", style="Hint.TLabel").pack(side="left", padx=(10, 0))
        if fld.help:
            ttk.Label(self.frame, text=fld.help, style="Hint.TLabel", wraplength=980,
                      justify="left").pack(fill="x", pady=(1, 0))

    def pack(self, **kwargs: Any) -> None:
        self.frame.pack(**kwargs)

    def get_value(self) -> Any:
        fld = self.fld
        if fld.kind == KIND_BOOL:
            return bool(self.var.get())
        raw = str(self.var.get())
        if fld.kind in (KIND_STRLIST, KIND_CODELIST):
            return split_list(raw)
        return fld.validate(raw)


class BaseEditor:
    path: Tuple[str, ...] = ()
    title = ""

    def __init__(self, ui: ConfigUI, parent: Any) -> None:
        tk, ttk = ui.tk, ui.ttk
        self.ui = ui
        self.frame = ttk.LabelFrame(parent, text=self.title, padding=(10, 6))
        self.frame.pack(fill="x", padx=14, pady=(6, 10))
        self.body = ttk.Frame(self.frame)
        self.body.pack(fill="x")

    def apply(self, data: Dict[str, Any]) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError


class PositionsEditor(BaseEditor):
    path = ("positions",)
    title = "持仓标的（代码 / 名称 / 成本 / 可卖数量 / T+0）"

    def __init__(self, ui: ConfigUI, parent: Any) -> None:
        super().__init__(ui, parent)
        self.rows: List[Dict[str, Any]] = []
        for code, item in ui.original.get("positions", {}).items():
            if str(code).startswith("_"):
                continue
            if not isinstance(item, dict):
                continue
            self._add_row(code, item.get("name", ""), item.get("cost", 0),
                          item.get("sellable_qty", 0), item.get("t0"))
        ttk = ui.ttk
        ttk.Button(self.body, text="＋ 新增标的",
                   command=lambda: self._add_row("", "", 0, 0, None)).pack(anchor="w", pady=(4, 0))
        ttk.Label(self.body,
                  text="联动说明：这里新增标的（代码填满 6 位）会自动加入【行情】页的「监控标的代码」；"
                       "删除某行也会把它从行情监控里移除。只想监控、不持仓的代码请在【行情】页单独添加。",
                  style="Hint.TLabel", wraplength=980, justify="left").pack(anchor="w", pady=(2, 0))
        ttk.Label(self.body,
                  text="T+0 勾选说明：只有跨境/债券/黄金/货币 ETF 才能当日买入当日卖出；"
                       "股票与股票 ETF（如 159325 半导体）为 T+1，勾错会让脚本发出当天无法成交的卖出单。",
                  style="Hint.TLabel", wraplength=980, justify="left").pack(anchor="w", pady=(4, 0))

    def codes(self) -> List[str]:
        """当前界面上持仓表里的代码（按行顺序）。"""
        out: List[str] = []
        for row in self.rows:
            code = str(row["code"].get()).strip()
            if code:
                out.append(code)
        return out

    def _on_code_edit(self, widgets: Dict[str, Any]) -> None:
        """代码填满 6 位时立即并入行情监控，避免保存后再去行情页补一遍。"""
        code = str(widgets["code"].get()).strip()
        if not CODE_RE.match(code):
            return
        prev = str(widgets.get("_synced") or "")
        if prev == code:
            return
        if prev and prev not in self.codes():
            self.ui.sync_market_codes(removed=[prev])
        widgets["_synced"] = code
        touched = self.ui.sync_market_codes(added=[code])
        if touched:
            self.ui.status(f"{code} 已同步加入行情监控")

    def _sync_removed(self, code: str) -> None:
        """删除持仓行时同步从行情监控移除（同一代码还有别的行时保留）。"""
        if not CODE_RE.match(code):
            return
        if code in self.codes():
            return
        touched = self.ui.sync_market_codes(removed=[code])
        if touched:
            self.ui.status(f"{code} 已从行情监控中移除")

    def _add_row(self, code: Any, name: Any, cost: Any, qty: Any, t0: Any = None) -> None:
        tk, ttk = self.ui.tk, self.ui.ttk
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=2)
        widgets: Dict[str, Any] = {}
        specs = [("code", "代码", 10), ("name", "名称", 16), ("cost", "成本", 12), ("qty", "可卖股数", 12)]
        values = {"code": code, "name": name, "cost": cost, "qty": qty}
        for key, label, width in specs:
            ttk.Label(row, text=label, style="Hint.TLabel").pack(side="left", padx=(0, 4))
            var = tk.StringVar(value=fmt_cell(values[key]))
            ttk.Entry(row, textvariable=var, width=width).pack(side="left", padx=(0, 10))
            widgets[key] = var
        t0_var = tk.BooleanVar(value=bool(t0))
        ttk.Checkbutton(row, text="T+0", variable=t0_var).pack(side="left", padx=(0, 10))
        widgets["t0"] = t0_var
        widgets["frame"] = row
        ttk.Button(row, text="删除", width=6,
                   command=lambda r=widgets: self._remove(r)).pack(side="left")
        widgets["code"].trace_add("write", lambda *_a, r=widgets: self._on_code_edit(r))
        widgets["_synced"] = str(code).strip() if CODE_RE.match(str(code).strip()) else ""
        self.rows.append(widgets)

    def _remove(self, widgets: Dict[str, Any]) -> None:
        code = str(widgets["code"].get()).strip()
        if widgets in self.rows:
            self.rows.remove(widgets)
        widgets["frame"].destroy()
        self._sync_removed(code)

    def apply(self, data: Dict[str, Any]) -> None:
        orig = self.ui.original.get("positions", {})
        result: Dict[str, Any] = {}
        for key, val in orig.items():
            if str(key).startswith("_"):
                result[key] = val
        seen: Dict[str, str] = {}
        for row in self.rows:
            code = str(row["code"].get()).strip()
            if not code:
                raise FieldError("持仓表中存在空的证券代码，请填写或删除该行")
            if not CODE_RE.match(code):
                raise FieldError(f"持仓表代码 {code} 必须是 6 位数字")
            if code in seen:
                raise FieldError(f"持仓表代码 {code} 重复")
            seen[code] = code
            cost = make_validator(KIND_FLOAT, 0, None, f"{code} 持仓成本")(row["cost"].get())
            qty = make_validator(KIND_QTY, 0, None, f"{code} 可卖股数", step=100)(row["qty"].get())
            base = orig.get(code)
            item = dict(base) if isinstance(base, dict) else {}
            name = str(row["name"].get()).strip()
            if name:
                item["name"] = name
            else:
                item.pop("name", None)
            item["cost"] = cost
            item["sellable_qty"] = qty
            item["t0"] = bool(row["t0"].get())
            result[code] = item
        if not [k for k in result if not str(k).startswith("_")]:
            raise FieldError("持仓表至少要保留一只标的")
        data["positions"] = result


class DecimalsEditor(BaseEditor):
    path = ("price_decimals",)
    title = "价格小数位（按代码覆盖，留空自动判断）"

    def __init__(self, ui: ConfigUI, parent: Any) -> None:
        super().__init__(ui, parent)
        tk, ttk = ui.tk, ui.ttk
        self.rows: List[Dict[str, Any]] = []
        conf = ui.original.get("price_decimals", {})
        top = ttk.Frame(self.body)
        top.pack(fill="x", pady=(0, 6))
        ttk.Label(top, text="default（全局默认）", style="Hint.TLabel").pack(side="left", padx=(0, 6))
        default_val = conf.get("default")
        self.default_var = tk.StringVar(value="" if default_val is None else str(default_val))
        ttk.Entry(top, textvariable=self.default_var, width=10).pack(side="left")
        ttk.Label(top, text="留空=全部自动判断；填 0-4 表示全局默认小数位", style="Hint.TLabel").pack(side="left", padx=(8, 0))
        for key, val in conf.items():
            if str(key).startswith("_") or key == "default":
                continue
            self._add_row(key, val)
        ttk.Button(self.body, text="＋ 新增代码覆盖", command=lambda: self._add_row("", 3)).pack(anchor="w", pady=(4, 0))
        if conf.get("_help"):
            ttk.Label(self.body, text=str(conf["_help"]), style="Help.TLabel", wraplength=980,
                      justify="left").pack(fill="x", pady=(6, 0))

    def _add_row(self, code: Any, decimals: Any) -> None:
        tk, ttk = self.ui.tk, self.ui.ttk
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="代码", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        code_var = tk.StringVar(value=fmt_cell(code))
        ttk.Entry(row, textvariable=code_var, width=12).pack(side="left", padx=(0, 10))
        ttk.Label(row, text="小数位", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        dec_var = tk.StringVar(value=fmt_cell(decimals))
        ttk.Combobox(row, textvariable=dec_var, values=["2", "3", "4", "0", "1"],
                     state="readonly", width=6).pack(side="left", padx=(0, 10))
        widgets = {"code": code_var, "dec": dec_var, "frame": row}
        ttk.Button(row, text="删除", width=6, command=lambda w=widgets: self._remove(w)).pack(side="left")
        self.rows.append(widgets)

    def _remove(self, widgets: Dict[str, Any]) -> None:
        if widgets in self.rows:
            self.rows.remove(widgets)
        widgets["frame"].destroy()

    def apply(self, data: Dict[str, Any]) -> None:
        orig = self.ui.original.get("price_decimals", {})
        result: Dict[str, Any] = {}
        for key, val in orig.items():
            if str(key).startswith("_"):
                result[key] = val
        raw_default = str(self.default_var.get()).strip()
        if raw_default == "":
            result["default"] = None
        else:
            result["default"] = make_validator(KIND_INT, 0, 4, "默认小数位")(raw_default)
        for row in self.rows:
            code = str(row["code"].get()).strip()
            if not code:
                raise FieldError("价格精度表存在空代码，请填写或删除该行")
            if not CODE_RE.match(code):
                raise FieldError(f"价格精度表代码 {code} 必须是 6 位数字")
            if code in result:
                raise FieldError(f"价格精度表代码 {code} 重复")
            result[code] = make_validator(KIND_INT, 0, 4, f"{code} 小数位")(row["dec"].get())
        data["price_decimals"] = result


class SessionsEditor(BaseEditor):
    path = ("trade", "sessions")
    title = "交易时段（开始 / 结束，HH:MM）"

    def __init__(self, ui: ConfigUI, parent: Any) -> None:
        super().__init__(ui, parent)
        tk, ttk = ui.tk, ui.ttk
        self.rows: List[Dict[str, Any]] = []
        for item in ui.original.get("trade", {}).get("sessions", []) or []:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                self._add_row(item[0], item[1])
        ttk.Button(self.body, text="＋ 新增时段", command=lambda: self._add_row("09:30", "11:30")).pack(anchor="w", pady=(4, 0))

    def _add_row(self, start: Any, end: Any) -> None:
        tk, ttk = self.ui.tk, self.ui.ttk
        row = ttk.Frame(self.body)
        row.pack(fill="x", pady=2)
        ttk.Label(row, text="开始", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        start_var = tk.StringVar(value=str(start))
        ttk.Entry(row, textvariable=start_var, width=10).pack(side="left", padx=(0, 10))
        ttk.Label(row, text="结束", style="Hint.TLabel").pack(side="left", padx=(0, 4))
        end_var = tk.StringVar(value=str(end))
        ttk.Entry(row, textvariable=end_var, width=10).pack(side="left", padx=(0, 10))
        widgets = {"start": start_var, "end": end_var, "frame": row}
        ttk.Button(row, text="删除", width=6, command=lambda w=widgets: self._remove(w)).pack(side="left")
        self.rows.append(widgets)

    def _remove(self, widgets: Dict[str, Any]) -> None:
        if widgets in self.rows:
            self.rows.remove(widgets)
        widgets["frame"].destroy()

    def apply(self, data: Dict[str, Any]) -> None:
        result: List[List[str]] = []
        for idx, row in enumerate(self.rows, 1):
            try:
                start = norm_hhmm(str(row["start"].get()))
                end = norm_hhmm(str(row["end"].get()))
            except FieldError as exc:
                raise FieldError(f"交易时段第 {idx} 段：{exc}")
            if start >= end:
                raise FieldError(f"交易时段第 {idx} 段开始时间必须早于结束时间（{start} → {end}）")
            result.append([start, end])
        if not result:
            raise FieldError("交易时段至少要保留一段")
        data.setdefault("trade", {})["sessions"] = result


COORD_FIELDS: List[Tuple[str, str]] = [
    ("code_box", "买入侧 · 代码框"),
    ("price_box", "买入侧 · 价格框"),
    ("qty_box", "买入侧 · 数量框"),
    ("buy_button", "买入按钮"),
    ("code_box_sell", "卖出侧 · 代码框"),
    ("price_box_sell", "卖出侧 · 价格框"),
    ("qty_box_sell", "卖出侧 · 数量框"),
    ("sell_button", "卖出按钮"),
]


def _find_order_hwnd() -> Optional[int]:
    """查找同花顺下单窗口（标题/类名口径与 main.py 的 _find_order_window 一致）。"""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception:  # noqa: BLE001
        return None
    user32 = ctypes.windll.user32
    found: List[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd: int, _lparam: int) -> bool:  # noqa: ANN001
        try:
            # 与 main.py 保持一致：必须可见，否则会匹配到同花顺遗留的隐藏 Afx 窗口
            if not user32.IsWindowVisible(hwnd):
                return True
            length = user32.GetWindowTextLengthW(hwnd)
            if not length:
                return True
            buf = ctypes.create_unicode_buffer(length + 1)
            user32.GetWindowTextW(hwnd, buf, length + 1)
            title = buf.value
            cls = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cls, 256)
            if "网上股票交易系统" in title or ("交易系统" in title and cls.value.startswith("Afx")):
                found.append(int(hwnd))
                return False
        except Exception:  # noqa: BLE001
            return True
        return True

    try:
        user32.EnumWindows(_cb, 0)
    except Exception:  # noqa: BLE001
        return None
    return found[0] if found else None


def _client_origin(hwnd: Optional[int]) -> Optional[Tuple[int, int]]:
    """下单窗口客户区左上角的屏幕坐标。"""
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        pt = wintypes.POINT(0, 0)
        if not ctypes.windll.user32.ClientToScreen(wintypes.HWND(hwnd), ctypes.byref(pt)):
            return None
        return int(pt.x), int(pt.y)
    except Exception:  # noqa: BLE001
        return None


def _client_size(hwnd: Optional[int]) -> Optional[Tuple[int, int]]:
    """下单窗口客户区尺寸。"""
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        rect = wintypes.RECT()
        if not ctypes.windll.user32.GetClientRect(wintypes.HWND(hwnd), ctypes.byref(rect)):
            return None
        return int(rect.right - rect.left), int(rect.bottom - rect.top)
    except Exception:  # noqa: BLE001
        return None


# ---------- 坐标测试：置前 / 遮挡判定 / 鼠标键盘模拟（仅 Windows） ----------

COORD_TEST_VALUES: Dict[str, str] = {
    "code_box": "000001",
    "code_box_sell": "000001",
    "price_box": "9.99",
    "price_box_sell": "9.99",
    "qty_box": "100",
    "qty_box_sell": "100",
}


def _win_root_at(x: int, y: int) -> Optional[int]:
    """屏幕坐标点上最顶层窗口的根句柄；取不到返回 None（调用方按无法判定处理）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return None
        root = user32.GetAncestor(wintypes.HWND(hwnd), 2)  # GA_ROOT
        return int(root) if root else int(hwnd)
    except Exception:  # noqa: BLE001
        return None


def _foreground_hwnd() -> Optional[int]:
    try:
        import ctypes

        value = int(ctypes.windll.user32.GetForegroundWindow() or 0)
        return value or None
    except Exception:  # noqa: BLE001
        return None


def _activate_window(hwnd: Optional[int]) -> bool:
    """把下单窗口切到最前面，并等它真正拿到焦点。"""
    if not hwnd:
        return False
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.ShowWindow(wintypes.HWND(hwnd), 9)  # SW_RESTORE
        user32.SetForegroundWindow(wintypes.HWND(hwnd))
        for _ in range(12):
            if _foreground_hwnd() == int(hwnd):
                return True
            time.sleep(0.05)
        return False
    except Exception:  # noqa: BLE001
        return False


def _move_cursor(x: int, y: int) -> None:
    import ctypes

    ctypes.windll.user32.SetCursorPos(int(x), int(y))


def _click_left(x: int, y: int) -> None:
    """移动鼠标并单击左键（只负责点下去，不判断点到的是什么）。"""
    import ctypes

    user32 = ctypes.windll.user32
    user32.SetCursorPos(int(x), int(y))
    time.sleep(0.08)
    user32.mouse_event(0x0002, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTDOWN
    time.sleep(0.04)
    user32.mouse_event(0x0004, 0, 0, 0, 0)  # MOUSEEVENTF_LEFTUP


def _key(vk: int, up: bool = False) -> None:
    import ctypes

    ctypes.windll.user32.keybd_event(int(vk), 0, 2 if up else 0, 0)


def _press(vk: int) -> None:
    _key(vk, False)
    time.sleep(0.02)
    _key(vk, True)


def _type_text(text: str) -> None:
    """模拟主键盘输入数字与小数点（坐标测试够用）。"""
    for ch in str(text):
        if ch.isdigit():
            _press(0x30 + int(ch))
        elif ch == ".":
            _press(0xBE)  # VK_OEM_PERIOD


def _focus_hwnd(hwnd: Optional[int]) -> Optional[int]:
    """取指定窗口所属线程当前的焦点控件。

    跨进程/跨线程调用 GetFocus() 一律返回空，必须经 GetGUIThreadInfo 按线程查。
    """
    if not hwnd:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32

        class GUITHREADINFO(ctypes.Structure):
            _fields_ = [
                ("cbSize", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("hwndActive", ctypes.c_void_p),
                ("hwndFocus", ctypes.c_void_p),
                ("hwndCapture", ctypes.c_void_p),
                ("hwndMenuOwner", ctypes.c_void_p),
                ("hwndMoveSize", ctypes.c_void_p),
                ("hwndCaret", ctypes.c_void_p),
                ("rcCaret", wintypes.RECT),
            ]

        user32.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        user32.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        user32.GetGUIThreadInfo.restype = ctypes.c_int

        tid = user32.GetWindowThreadProcessId(ctypes.c_void_p(int(hwnd)), None)
        if not tid:
            return None
        info = GUITHREADINFO()
        info.cbSize = ctypes.sizeof(GUITHREADINFO)
        if not user32.GetGUIThreadInfo(wintypes.DWORD(int(tid)), ctypes.byref(info)):
            return None
        return int(info.hwndFocus) if info.hwndFocus else None
    except Exception:  # noqa: BLE001
        return None


def _related_windows(a: Optional[int], b: Optional[int]) -> bool:
    """两个句柄是否为同一控件，或存在父子关系。"""
    if not a or not b:
        return False
    if int(a) == int(b):
        return True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [ctypes.c_void_p]
        user32.GetParent.restype = ctypes.c_void_p
        for start, other in ((int(a), int(b)), (int(b), int(a))):
            cur = start
            for _ in range(8):
                parent = user32.GetParent(ctypes.c_void_p(cur))
                if not parent:
                    break
                cur = int(parent)
                if cur == other:
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False


def _window_class(hwnd: Optional[int]) -> str:
    """取窗口类名，取不到返回空串。"""
    if not hwnd:
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(256)
        n = user32.GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 256)
        return buf.value if n else ""
    except Exception:  # noqa: BLE001
        return ""


def _client_rect(hwnd: Optional[int], root: Optional[int]) -> Optional[Tuple[int, int, int, int]]:
    """把控件矩形换算成下单窗口客户区坐标，返回 (x1, y1, x2, y2)。"""
    if not hwnd or not root:
        return None
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        rc = wintypes.RECT()
        if not user32.GetWindowRect(wintypes.HWND(int(hwnd)), ctypes.byref(rc)):
            return None
        origin = wintypes.POINT(0, 0)
        if not user32.ClientToScreen(wintypes.HWND(int(root)), ctypes.byref(origin)):
            return None
        ox, oy = origin.x, origin.y
        return (rc.left - ox, rc.top - oy, rc.right - ox, rc.bottom - oy)
    except Exception:  # noqa: BLE001
        return None


def _same_side_inputs(target_rect: Optional[Tuple[int, int, int, int]],
                      root: Optional[int], tol: int = 24) -> list:
    """列出与目标控件同列的输入框，返回按 y 升序的 [(y, 宽, hwnd), ...]，供人工对照。"""
    if not target_rect or not root:
        return []
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        found = []
        proto = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)

        def _cb(h, _lp):
            if _window_class(h).lower() not in ("edit", "tedit", "richedit", "richedit20w", "richedit50w"):
                return True
            r = _client_rect(h, root)
            if r and abs(r[0] - target_rect[0]) <= tol:
                found.append((r[1], r[2] - r[0], int(h)))
            return True

        user32.EnumChildWindows(wintypes.HWND(int(root)), proto(_cb), 0)
        found.sort()
        return found
    except Exception:  # noqa: BLE001
        return []


def _clear_field_hard() -> None:
    """清空当前获得焦点的输入框，序列与真实下单完全一致：Ctrl+A → Delete → Backspace×8。

    2026-11-24 实测（同花顺远航版下单窗口 hwnd=68784，买入价格框 hwnd=70358）：
    · Ctrl+A → Delete → Backspace×8 能把价格框清空到与基线零像素差，焦点全程停留在原控件；
    · 绝对不能用 Home / End / Shift+End —— 价格框不消费 End 键，End 被对话框导航逻辑
      当成控件跳转，焦点会直接跑到代码框（hwnd=70348），之后 Delete / Backspace 以及
      输入的数字全部落进代码框，表现为「试填价格填到了代码框里」。
    """
    VK_CONTROL, VK_A = 0x11, 0x41
    VK_DELETE, VK_BACK = 0x2E, 0x08
    _key(VK_CONTROL, False)
    _press(VK_A)
    _key(VK_CONTROL, True)
    time.sleep(0.05)
    _press(VK_DELETE)
    time.sleep(0.02)
    for _ in range(8):
        _press(VK_BACK)
        time.sleep(0.015)


def _class_name(hwnd: Optional[int]) -> str:
    """控件类名（Edit / ComboBox / Button …）。"""
    if not hwnd:
        return ""
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.GetClassNameW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
        user32.GetClassNameW.restype = ctypes.c_int
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(ctypes.c_void_p(int(hwnd)), buf, 256)
        return buf.value
    except Exception:  # noqa: BLE001
        return ""


def _probe_child_at(x: int, y: int) -> Tuple[Optional[int], str]:
    """屏幕坐标点上的子窗口句柄与其类名（只读探测）。"""
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        user32.WindowFromPoint.restype = ctypes.c_void_p
        hwnd = user32.WindowFromPoint(wintypes.POINT(int(x), int(y)))
        if not hwnd:
            return None, ""
        handle = int(hwnd)
        return handle, _class_name(handle)
    except Exception:  # noqa: BLE001
        return None, ""


def _is_text_control(cls: str) -> bool:
    lower = str(cls or "").lower()
    return any(lower.startswith(p) for p in ("edit", "tedit", "richedit", "combobox"))


def _read_window_text(hwnd: Optional[int], length: int = 64) -> Optional[str]:
    """读控件当前文本（只读，用于校验试填值与清空结果）。"""
    if not hwnd:
        return None
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SendMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        user32.SendMessageW.restype = ctypes.c_ssize_t
        buf = ctypes.create_unicode_buffer(int(length))
        user32.SendMessageW(
            ctypes.c_void_p(int(hwnd)),
            0x000D,  # WM_GETTEXT
            ctypes.c_size_t(int(length)),
            ctypes.c_ssize_t(ctypes.addressof(buf)),
        )
        return buf.value
    except Exception:  # noqa: BLE001
        return None


def _set_window_text(hwnd: Optional[int], text: str = "") -> bool:
    """直接给控件置文本，仅作为清空兜底（清空没有副作用）。"""
    if not hwnd:
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        user32.SetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
        user32.SetWindowTextW.restype = ctypes.c_int
        return bool(user32.SetWindowTextW(ctypes.c_void_p(int(hwnd)), str(text)))
    except Exception:  # noqa: BLE001
        return False


def _select_all_and_clear() -> None:  # 兼容旧调用点
    _clear_field_hard()


class CoordsEditor(BaseEditor):
    path = ("order_ui", "coords")
    title = "下单界面坐标（下单窗口客户区相对像素）"

    def __init__(self, ui: ConfigUI, parent: Any) -> None:
        super().__init__(ui, parent)
        tk, ttk = ui.tk, ui.ttk
        self.rows: Dict[str, Tuple[Any, Any]] = {}
        coords = dict(ui.original.get("order_ui", {}).get("coords", {}) or {})
        oui_orig = ui.original.get("order_ui", {}) or {}
        legacy = str(oui_orig.get("_coords_mode", "") or "").strip().lower() != "client"
        notice = ""
        if legacy and coords:
            # 旧版存的是屏幕绝对坐标：先换算成客户区相对坐标再显示，
            # 否则用户点保存会把这些值当成相对坐标打上 client 标记，导致整体偏移。
            origin = _client_origin(_find_order_hwnd())
            if origin:
                ox, oy = origin
                coords = {
                    k: ([int(v[0]) - ox, int(v[1]) - oy]
                        if isinstance(v, (list, tuple)) and len(v) >= 2 else v)
                    for k, v in coords.items()
                }
                notice = ("　【注意】配置里是旧版屏幕绝对坐标，已按当前下单窗口自动换算为客户区相对坐标，"
                          "建议逐项重拾一遍确认。")
            else:
                notice = ("　【注意】配置里是旧版屏幕绝对坐标，且当前未找到下单窗口（标题需含"
                          "「网上股票交易系统」），无法换算；请先打开下单界面再重拾。")
        grid = ttk.Frame(self.body)
        grid.pack(fill="x")
        for idx, (name, label) in enumerate(COORD_FIELDS):
            value = coords.get(name) or [0, 0]
            try:
                x_val, y_val = int(value[0]), int(value[1])
            except Exception:  # noqa: BLE001
                x_val, y_val = 0, 0
            cell = ttk.Frame(grid)
            cell.grid(row=idx // 2, column=idx % 2, sticky="w", padx=(0, 24), pady=4)
            ttk.Label(cell, text=label, style="FieldLabel.TLabel", width=16, anchor="w").pack(side="left")
            x_var = tk.StringVar(value=str(x_val))
            y_var = tk.StringVar(value=str(y_val))
            ttk.Label(cell, text="x", style="Hint.TLabel").pack(side="left")
            ttk.Entry(cell, textvariable=x_var, width=7).pack(side="left", padx=(2, 8))
            ttk.Label(cell, text="y", style="Hint.TLabel").pack(side="left")
            ttk.Entry(cell, textvariable=y_var, width=7).pack(side="left", padx=(2, 8))
            ttk.Button(cell, text="拾取", width=6,
                       command=lambda xv=x_var, yv=y_var, lb=label: self._pick(xv, yv, lb)).pack(side="left")
            ttk.Button(cell, text="测试", width=6,
                       command=lambda nm=name, lb=label: self._test_clicked(nm, lb)).pack(side="left", padx=(4, 0))
            self.rows[name] = (x_var, y_var)
        for name, val in coords.items():
            if name in self.rows:
                continue
            x_var = tk.StringVar(value=str((val or [0, 0])[0]))
            y_var = tk.StringVar(value=str((val or [0, 0])[1]))
            self.rows[name] = (x_var, y_var)
        bar = ttk.Frame(self.body)
        bar.pack(fill="x", pady=(10, 0))
        ttk.Button(bar, text="测试全部输入框", width=16,
                   command=self._test_inputs).pack(side="left")
        self.fill_check = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            bar,
            text="试填校验（默认关闭；勾选后写入一个数字，再用与真实下单相同的 Ctrl+A/Delete 清空）",
            variable=self.fill_check,
        ).pack(side="left", padx=(12, 0))
        ttk.Label(bar, text="依次点击 6 个输入框并核对坐标/焦点（默认不输入字符）；不会点击买入/卖出按钮，也不会提交委托",
                  style="Hint.TLabel").pack(side="left", padx=(8, 0))
        ttk.Label(self.body, text="说明：点「拾取」后 3 秒内把鼠标移到同花顺对应控件上，到时自动读取并换算为"
                                  "「下单窗口客户区相对坐标」（窗口移动/改分辨率后仍然有效）；也可按 main.py --detect 自动定位。"
                                  "全部为 [0,0] 时真实下单会被拒绝（dry_run 不受影响）。" + notice,
                  style="Hint.TLabel", wraplength=980, justify="left").pack(fill="x", pady=(8, 0))
        ttk.Label(self.body, text="说明：点「测试」会用当前填入的坐标真的点一次同花顺——输入框只点击并核对焦点与控件矩形，默认不输入字符（勾选「试填校验」才会写入），"
                                  "按钮只把鼠标移过去不点击；状态栏会给出命中控件的矩形和同列输入框清单，据此判断是否要重拾。",
                  style="Hint.TLabel", wraplength=980, justify="left").pack(fill="x", pady=(4, 0))

    def _pick(self, x_var: Any, y_var: Any, label: str) -> None:
        self.ui.status(f"3 秒后记录「{label}」的鼠标位置，请把鼠标移到目标控件上…")

        def _grab() -> None:
            try:
                x, y = self.ui.root.winfo_pointerxy()
            except Exception as exc:  # noqa: BLE001
                self.ui.status(f"读取鼠标位置失败：{exc}")
                return
            hwnd = _find_order_hwnd()
            origin = _client_origin(hwnd)
            if not origin:
                self.ui.status("未找到同花顺下单窗口（标题需含「网上股票交易系统」），"
                               "无法换算客户区相对坐标；本次未写入，请先打开下单界面后重拾")
                return
            rx, ry = int(x) - origin[0], int(y) - origin[1]
            if rx < 0 or ry < 0:
                self.ui.status(f"鼠标位置 ({x}, {y}) 落在下单窗口客户区之外，请把鼠标对准控件本体后重拾")
                return
            x_var.set(str(rx))
            y_var.set(str(ry))
            self.ui.status(f"已记录「{label}」= 客户区({rx}, {ry})  屏幕({x}, {y})")

        self.ui.root.after(3000, _grab)

    # ---------- 坐标测试：按填入的坐标真的操作一次同花顺 ----------

    def _resolve_point(self, name: str, label: str) -> Optional[Tuple[int, int, int, int, int]]:
        """把该行客户区相对坐标换算成屏幕绝对坐标，返回 (rx, ry, sx, sy, hwnd)。"""
        pair = self.rows.get(name)
        if not pair:
            self.ui.status(f"「{label}」没有对应的坐标行，无法测试", warn=True)
            return None
        try:
            rx = int(str(pair[0].get()).strip() or 0)
            ry = int(str(pair[1].get()).strip() or 0)
        except Exception:  # noqa: BLE001
            self.ui.status(f"「{label}」的 x/y 不是整数，无法测试", warn=True)
            return None
        if rx <= 0 and ry <= 0:
            self.ui.status(f"「{label}」还没填坐标（当前 {rx}, {ry}），请先拾取", warn=True)
            return None
        hwnd = _find_order_hwnd()
        origin = _client_origin(hwnd)
        if not hwnd or not origin:
            self.ui.status("未找到同花顺下单窗口（标题需含「网上股票交易系统」），无法测试；请先打开下单界面", warn=True)
            return None
        size = _client_size(hwnd)
        if size and not (0 <= rx < size[0] and 0 <= ry < size[1]):
            self.ui.status(f"「{label}」客户区坐标 ({rx}, {ry}) 超出客户区 {size[0]}x{size[1]}，该点不会命中控件", warn=True)
            return None
        return rx, ry, origin[0] + rx, origin[1] + ry, int(hwnd)

    def _test_one(self, name: str, label: str) -> bool:
        point = self._resolve_point(name, label)
        if not point:
            return False
        rx, ry, sx, sy, hwnd = point
        if not _activate_window(hwnd):
            self.ui.status("无法把同花顺下单窗口切到最前面（可能被遮挡或权限不足），本次未测试", warn=True)
            return False
        time.sleep(0.2)
        top = _win_root_at(sx, sy)
        if top is not None and top != hwnd:
            self.ui.status(f"「{label}」在屏幕 ({sx}, {sy}) 被其他窗口挡住，已跳过；请让下单界面完整显示后重试", warn=True)
            return False
        if name.endswith("_button"):
            _move_cursor(sx, sy)
            self.ui.status(f"「{label}」客户区({rx}, {ry}) → 屏幕({sx}, {sy})：鼠标已移到该点但未点击（避免误触发委托），请目视确认是否正好压住按钮", warn=True)
            return True
        child, cls = _probe_child_at(sx, sy)
        if child is None:
            self.ui.status(f"「{label}」客户区({rx}, {ry}) → 屏幕({sx}, {sy})：该坐标上没有可识别的控件，本次未点击；请重新拾取坐标", warn=True)
            return False
        focus = None
        for _ in (1, 2):
            _click_left(sx, sy)
            time.sleep(0.18)
            focus = _focus_hwnd(hwnd)
            if _related_windows(focus, child):
                break
        if not _related_windows(focus, child):
            self.ui.status(
                f"「{label}」客户区({rx}, {ry}) → 屏幕({sx}, {sy})：点击后焦点没有落到该坐标上的控件"
                f"（该点控件类 {cls or '未知'}，当前焦点在 {_window_class(focus) or '未知'}）；请重新拾取该坐标后重试",
                warn=True,
            )
            return False
        try_fill = bool(self.fill_check.get()) if getattr(self, "fill_check", None) is not None else False
        if try_fill:
            text = COORD_TEST_VALUES.get(name, "1")
            _clear_field_hard()
            time.sleep(0.05)
            focus = _focus_hwnd(hwnd)
            if not _related_windows(focus, child):
                self.ui.status(
                    f"「{label}」清空按键后焦点已从该控件漂移到 {_window_class(focus) or '未知'}，"
                    f"已中止试填、一个数字都没有输入；请重新拾取坐标后重试", warn=True)
                return False
            _type_text(text)
            time.sleep(0.3)
            focus_written = _focus_hwnd(hwnd)
            _clear_field_hard()
            time.sleep(0.1)
            drift = ""
            if not _related_windows(focus_written, child):
                drift = f"；注意：输入后焦点漂移到 {_window_class(focus_written) or '未知'}，数字可能落在别的框里，请目视核对"
            self.ui.status(
                f"「{label}」客户区({rx}, {ry}) → 屏幕({sx}, {sy})：已按「试填校验」向该控件输入 {text}，"
                f"随后用与真实下单相同的清空序列（Ctrl+A → Delete → Backspace）清空，焦点全程未离开该控件{drift}。"
                f"请目视确认输入时数字确实出现在这个框里", warn=True)
            return True
        rect = _client_rect(child, hwnd)
        where = f"控件类 {cls or '未知'}"
        if rect:
            where += f"，客户区 ({rect[0]},{rect[1]})-({rect[2]},{rect[3]})，宽 {rect[2] - rect[0]} 高 {rect[3] - rect[1]}"
        rows = _same_side_inputs(rect, hwnd)
        hint = ""
        if len(rows) > 1:
            hint = "；同侧输入框自上而下：" + "、".join(
                ("►" if h == int(child) else "") + f"y={y}(宽{w})" for y, w, h in rows
            )
        self.ui.status(
            f"「{label}」客户区({rx}, {ry}) → 屏幕({sx}, {sy})：坐标已压准输入框，焦点已在该控件；{where}{hint}。"
            f"本测试不向真实输入框输入任何字符（该控件不响应 WM_GETTEXT、读不回原值，所以默认只核对矩形），请按上面的矩形核对是否就是要填的那个框；如需验证能否真的输入，可勾选下方「试填校验」（写入后会按真实下单同样的 Ctrl+A → Delete 清空，清空效果已实测有效）"
        )
        return True

    def _confirm(self, title: str, message: str) -> bool:
        from tkinter import messagebox

        return bool(messagebox.askokcancel(title, message))

    def _test_clicked(self, name: str, label: str) -> None:
        tip = ("按钮只把鼠标移过去、不点击（避免误触发下单）。" if name.endswith("_button")
               else f"会点击该点并试填 {COORD_TEST_VALUES.get(name, '1')}，随后立即清空。")
        if not self._confirm("坐标测试", "测试会把同花顺下单窗口切到最前面，再模拟真实鼠标操作：\n\n"
                                        f"「{label}」{tip}\n\n测试期间请不要动鼠标键盘，全程不会提交任何委托。继续？"):
            self.ui.status("已取消坐标测试")
            return
        self._test_one(name, label)

    def _test_inputs(self) -> None:
        names = [(n, lb) for n, lb in COORD_FIELDS if not n.endswith("_button")]
        plan = "\n".join(f"· {lb}" for n, lb in names)
        if not self._confirm("测试全部输入框坐标",
                             "将把同花顺下单窗口切到最前面，按顺序点击下列输入框，逐个核对坐标是否压准、焦点是否到位"
                             "（默认不输入任何字符，不会改动价格/数量/代码）：\n\n"
                             f"{plan}\n\n不会点击买入/卖出按钮，也不会提交任何委托。测试期间请不要动鼠标键盘。继续？"):
            self.ui.status("已取消坐标测试")
            return
        ok = 0
        for name, label in names:
            if not self._test_one(name, label):
                self.ui.status(f"「{label}」测试未通过，已中断批量测试（修好该点后重试）", warn=True)
                return
            ok += 1
            time.sleep(0.2)
        self.ui.status(f"坐标测试完成：{ok}/{len(names)} 个输入框坐标均已压准并拿到焦点（未输入任何字符）")

    def apply(self, data: Dict[str, Any]) -> None:
        orig = self.ui.original.get("order_ui", {}).get("coords", {}) or {}
        result: Dict[str, Any] = {}
        for name, (x_var, y_var) in self.rows.items():
            try:
                x = make_validator(KIND_INT, 0, 20000, f"{name}.x")(x_var.get())
                y = make_validator(KIND_INT, 0, 20000, f"{name}.y")(y_var.get())
            except FieldError as exc:
                raise FieldError(f"坐标 {name}：{exc}")
            result[name] = [x, y]
        for name, val in orig.items():
            if name not in result:
                result[name] = val
        oui = data.setdefault("order_ui", {})
        oui["coords"] = result
        # 版本标记 + 参照尺寸：执行端据此判断坐标空间（client / 旧版 screen）与窗口尺寸是否变化。
        # 关键约束：只有确实按客户区口径确定了这批值（旧格式已在打开时换算，或本次找到下单窗口）
        # 才能打 client 标记；否则保留了旧版屏幕绝对坐标却标记成 client，会导致后续整体偏移。
        size = _client_size(_find_order_hwnd())
        legacy = str((self.ui.original.get("order_ui", {}) or {}).get("_coords_mode", "") or "").strip().lower() != "client"
        prev_ref = (self.ui.original.get("order_ui", {}) or {}).get("_coords_ref")
        if size:
            oui["_coords_mode"] = "client"
            oui["_coords_ref"] = {"client_w": size[0], "client_h": size[1]}
        elif not legacy:
            oui["_coords_mode"] = "client"
            oui["_coords_ref"] = dict(prev_ref) if isinstance(prev_ref, dict) else {}
        # legacy 且当前取不到下单窗口：保持原样（不打标记），执行端会临时换算并提示重拾


# ======================================================================================
# 自检
# ======================================================================================
def run_selftest(config_path: Path) -> int:
    print("== 1. 加载配置 ==")
    cfg = load_config(config_path)
    print(f"  路径：{config_path}")
    print(f"  顶层键：{list(cfg.keys())}")

    print("== 2. schema 覆盖检查 ==")
    report = coverage_report(cfg)
    print(f"  界面字段数：{len(ALL_FIELDS)}")
    if report["missing_field_paths"]:
        print("  FAIL 界面声明但配置中缺少：" + "、".join(report["missing_field_paths"]))
    else:
        print("  PASS 界面声明的全部字段在配置中都存在")
    if report["unmapped_paths"]:
        print("  FAIL 配置中未被界面映射（会被原样保留）：" + "、".join(report["unmapped_paths"]))
    else:
        print("  PASS 配置的全部键均已被界面覆盖或由专用编辑器接管")

    print("== 3. 现有配置值校验 ==")
    problems = validate_config(cfg)
    if problems:
        for item in problems:
            print("  FAIL " + item)
    else:
        print("  PASS 当前配置全部字段合法")

    print("== 4. 结构保持（模拟修改后保存到临时文件） ==")
    tmp = config_path.parent.parent / "temp" / f"config_ui_selftest_{now_stamp()}.json"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    data = copy.deepcopy(cfg)
    path_set(data, ("market", "poll_interval_sec"), 5)
    path_set(data, ("signals", "buy_first", "below_avg_pct"), 0.45)
    path_set(data, ("order_ui", "dry_run"), False)
    data.setdefault("trade", {}).setdefault("sessions", [])
    data["trade"]["sessions"] = [["09:30", "11:30"], ["13:00", "15:00"]]
    data.setdefault("positions", {})
    data["positions"].setdefault("159325", {})
    data["positions"]["159325"]["cost"] = 1.605
    data["order_ui"]["coords"]["code_box"] = [400, 700]
    validate_ok = not validate_config(data)
    write_json_atomic(tmp, data)
    back = load_config(tmp)
    diffs = diff_tree(cfg, back)
    changed_keys = sorted(k for k, _o, _n in diffs)
    expect_changed = sorted([
        "market.poll_interval_sec",
        "signals.buy_first.below_avg_pct",
        "order_ui.dry_run",
        "trade.sessions",
        "positions.159325.cost",
        "order_ui.coords.code_box",
    ])
    print(f"  修改后校验通过：{validate_ok}")
    if changed_keys == expect_changed:
        print(f"  PASS 差异仅限预期字段：{changed_keys}")
    else:
        print(f"  FAIL 差异异常：{changed_keys}（预期 {expect_changed}）")
    if list(back.keys()) == list(cfg.keys()):
        print("  PASS 顶层键顺序与键名完全保持")
    else:
        print(f"  FAIL 顶层键变化：{list(back.keys())}")
    help_keys = [k for k in cfg if isinstance(cfg[k], dict) and "_help" in cfg[k]]
    help_lost = [k for k in help_keys if "_help" not in back.get(k, {})]
    if not help_lost:
        print(f"  PASS {len(help_keys)} 个分组的 _help 说明全部保留")
    else:
        print(f"  FAIL _help 丢失：{help_lost}")
    try:
        tmp.unlink()
    except OSError:
        pass

    print("== 5. 校验器用例 ==")
    cases: List[Tuple[str, Callable[[], Any], bool]] = [
        ("百分比 0.30 合法", lambda: make_validator(KIND_PERCENT, 0, 100, "p")("0.30"), True),
        ("百分比 -1 非法", lambda: make_validator(KIND_PERCENT, 0, 100, "p")("-1"), False),
        ("百分比 150 非法", lambda: make_validator(KIND_PERCENT, 0, 100, "p")("150"), False),
        ("百分比 abc 非法", lambda: make_validator(KIND_PERCENT, 0, 100, "p")("abc"), False),
        ("股数 100 合法", lambda: make_validator(KIND_QTY, 0, None, "q", step=100)("100"), True),
        ("股数 150 非法（非整百）", lambda: make_validator(KIND_QTY, 0, None, "q", step=100)("150"), False),
        ("股数 8000 合法", lambda: make_validator(KIND_QTY, 0, None, "q", step=100)("8000"), True),
        ("整数 3 合法", lambda: make_validator(KIND_INT, 1, 600, "i")("3"), True),
        ("整数 3.5 非法", lambda: make_validator(KIND_INT, 1, 600, "i")("3.5"), False),
        ("时刻 09:15 合法", lambda: norm_hhmm("09:15"), True),
        ("时刻 14:50 合法", lambda: norm_hhmm("14:50"), True),
        ("时刻 24:00 非法", lambda: norm_hhmm("24:00"), False),
        ("时刻 9:5 非法", lambda: norm_hhmm("9:5"), False),
        ("时刻 0925 非法", lambda: norm_hhmm("0925"), False),
        ("代码 159325 合法", lambda: fld_validate("market.codes", ["159325", "159866"]), True),
        ("代码 15932 非法", lambda: fld_validate("market.codes", ["15932"]), False),
        ("代码 15932A 非法", lambda: fld_validate("market.codes", ["15932A"]), False),
        ("坐标 0 合法", lambda: make_validator(KIND_INT, 0, 20000, "c")("0"), True),
        ("坐标 386 合法", lambda: make_validator(KIND_INT, 0, 20000, "c")("386"), True),
        ("坐标 -5 非法", lambda: make_validator(KIND_INT, 0, 20000, "c")("-5"), False),
        ("坐标 abc 非法", lambda: make_validator(KIND_INT, 0, 20000, "c")("abc"), False),
        ("选项 true 非法（qty_unit）", lambda: fld_validate("order_ui.qty_unit", "true"), False),
        ("选项 shares 合法", lambda: fld_validate("order_ui.qty_unit", "shares"), True),
        ("时刻字段 deadline 合法", lambda: fld_validate("trade.time_stop.time_layer.deadline", "14:50"), True),
        ("时刻字段 deadline 非法", lambda: fld_validate("trade.time_stop.time_layer.deadline", "25:00"), False),
    ]
    fails = 0
    for name, fn, expect_ok in cases:
        try:
            fn()
            ok = True
            detail = ""
        except FieldError as exc:
            ok = False
            detail = str(exc)
        except Exception as exc:  # noqa: BLE001
            ok = False
            detail = f"意外异常：{exc}"
        if ok == expect_ok:
            print(f"  PASS {name}")
        else:
            fails += 1
            print(f"  FAIL {name}（实际 {'通过' if ok else '拒绝：' + detail}）")

    print("== 6. 备份与原子写入 ==")
    bsrc = config_path.parent.parent / "temp" / f"backup_probe_{now_stamp()}.json"
    bsrc.parent.mkdir(parents=True, exist_ok=True)
    bsrc.write_text(dump_json_text(cfg), encoding="utf-8")
    bak = backup_config(bsrc)
    ok_backup = bak.exists() and bak.read_text(encoding="utf-8") == bsrc.read_text(encoding="utf-8")
    print(("  PASS" if ok_backup else "  FAIL") + f" 备份内容与原文一致：{bak}")
    write_json_atomic(bsrc, cfg)
    ok_atomic = load_config(bsrc) == cfg
    print(("  PASS" if ok_atomic else "  FAIL") + " 原子写入后内容一致")
    for path in (bsrc, bak):
        try:
            path.unlink()
        except OSError:
            pass
    try:
        (bsrc.parent / BACKUP_DIR_NAME).rmdir()
    except OSError:
        pass

    total_fail = fails
    if report["missing_field_paths"] or report["unmapped_paths"] or problems or not validate_ok or not ok_backup or not ok_atomic or changed_keys != expect_changed:
        total_fail += 1
    print()
    if total_fail:
        print(f"SELFTEST FAILED（{total_fail} 类问题）")
        return 1
    print("SELFTEST PASSED")
    return 0


def fld_validate(key: str, value: Any) -> Any:
    fld = FIELD_BY_KEY.get(key)
    if fld is None:
        raise FieldError(f"未知字段 {key}")
    return fld.validate(value)


def run_gui_smoke(config_path: Path) -> int:
    _enable_dpi()
    ui = ConfigUI(config_path)
    tabs = ui.nb.tabs()
    print(f"页签数量：{len(tabs)} -> {[ui.nb.tab(t, 'text') for t in tabs]}")
    print(f"标量字段控件数：{len(ui.fields)}")
    print(f"专用编辑器数：{len(ui.editors)} -> {[type(e).__name__ for e in ui.editors]}")
    data, errors = ui.collect()
    print(f"collect 结果：错误 {len(errors)} 个")
    for item in errors[:10]:
        print("  " + item)
    print(f"与磁盘差异：{len(diff_tree(ui.original, data))} 处")
    expect = copy.deepcopy(ui.original)
    ui._merge_positions_into_market(expect)
    diffs = diff_tree(expect, data)
    if diffs:
        print("联动兜底之外的意外差异：")
        for path, old, new in diffs[:10]:
            print(f"  {path}: {old!r} -> {new!r}")
    ok = not errors and not diffs
    ui.root.update_idletasks()
    ui.root.destroy()
    if ok:
        print("GUI SMOKE PASSED")
        return 0
    print("GUI SMOKE FAILED")
    return 1


def run_check(config_path: Path) -> int:
    cfg = load_config(config_path)
    problems = validate_config(cfg)
    report = coverage_report(cfg)
    print(f"配置文件：{config_path}")
    print(f"界面字段：{len(ALL_FIELDS)}；未映射键：{len(report['unmapped_paths'])}")
    if report["unmapped_paths"]:
        print("未映射（保存时原样保留）：" + "、".join(report["unmapped_paths"]))
    if problems:
        print(f"校验发现问题 {len(problems)} 处：")
        for item in problems:
            print("  - " + item)
        return 1
    print("校验通过：全部字段合法")
    return 0


# ======================================================================================
# 入口
# ======================================================================================
def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="THS-T-Assistant 图形化配置编辑器")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径（默认脚本同目录 config.json）")
    parser.add_argument("--check", action="store_true", help="只校验配置，不打开窗口")
    parser.add_argument("--selftest", action="store_true", help="开发者自检（覆盖检查 + 校验用例 + 结构保持）")
    parser.add_argument("--gui-smoke", action="store_true", help="构建全部界面后立即销毁，用于冒烟测试")
    args = parser.parse_args(argv)
    path = Path(args.config).expanduser()
    try:
        if args.check:
            return run_check(path)
        if args.selftest:
            return run_selftest(path)
        if args.gui_smoke:
            return run_gui_smoke(path)
        _enable_dpi()
        ConfigUI(path).run()
        return 0
    except FileNotFoundError as exc:
        print(f"[错误] {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[错误] {exc}")
        return 3


if __name__ == "__main__":
    sys.exit(main())
