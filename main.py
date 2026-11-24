# -*- coding: utf-8 -*-
"""
同花顺做T半自动辅助工具（THS T-Trade Assistant）
=================================================

功能概览
--------
1. 实时行情：新浪行情接口（hq.sinajs.cn，需 Referer 头，GB18030 编码），秒级轮询，
   解析现价/今开/昨收/最高/最低/成交量/成交额/五档买卖盘/行情时间，
   计算分时均价线（成交额÷成交量）、日内振幅、当日涨跌幅、相对持仓成本盈亏比例。
2. 信号引擎：全部阈值来自 config.json。
   正T（先买后卖）：现价明显低于分时均价线且未跌破当日支撑。
   反T（先卖后买）：现价明显高于分时均价线且高于持仓成本上浮阈值。
   信号带唯一编号、触发时刻、数值快照；同标的同类型信号冷却期内不重复触发。
3. 窗口监控：检测同花顺远航版进程与主窗口是否存在、是否前台，缺失时仅提示不中断行情。
4. 交互：信号触发时弹出置顶确认窗口（信号详情 + 建议价量 + 倒计时），
   用户点击"确认下单"后才用 pyautogui 模拟点击下单界面；超时/忽略一律不下单。
5. 审计：所有信号与下单动作写入带时间戳的 CSV 日志。

运行方式
--------
    python main.py                      # 正常监控（默认 dry_run）
    python main.py --once               # 抓取一次行情并做信号诊断后退出（验证用）
    python main.py --selftest           # 自检：注入一条信号，走完弹窗→倒计时→日志链路
    python main.py --selftest --auto-confirm   # 自检并自动点"确认"，验证下单执行链路
    python main.py --detect             # 自动识别下单界面 8 个操作点坐标（买入侧4 + 卖出侧4，读控件矩形，不需按键）
    python main.py --detect --write     # 识别完整时写入 config.json
    python main.py --pick               # 先自动识别；失败时改为鼠标悬停 1.5 秒记录（ESC 中止）
    python main.py --config other.json  # 指定配置文件

免责声明
--------
本工具仅做行情提示与界面操作辅助，不构成任何投资建议。
dry_run 为默认值，切换真实下单前请务必先阅读 README.md 的风险提示与操作步骤。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# 打包成 exe 后 __file__ 指向 PyInstaller 解包目录（onefile 为临时目录），
# 统一以 exe 所在目录作为工作根，保证 config.json / data / logs 与 exe 同级、可直接编辑。
if getattr(sys, "frozen", False):
    BASE_DIR = Path(sys.executable).resolve().parent
else:
    BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG_PATH = BASE_DIR / "config.json"

# 数据根目录：日志 / 审计 CSV / 下单截图 / 状态台账统一放到用户级本地目录，
# 这样 exe 放在只读介质、Program Files、由快捷方式或计划任务拉起时都能正常写入，
# 也不会因为 PyInstaller 单文件解包目录（_MEIxxxx）退出即被清理而丢掉当日闭环锁与批次台账。
# 默认 %LOCALAPPDATA%\ths_t_trader，可用环境变量 THS_T_DATA_DIR 覆盖。
def _default_data_root() -> Path:
    override = os.environ.get("THS_T_DATA_DIR", "").strip()
    if override:
        return Path(os.path.expandvars(override)).expanduser()
    local_appdata = os.environ.get("LOCALAPPDATA", "").strip()
    if local_appdata:
        return Path(local_appdata) / "ths_t_trader"
    return BASE_DIR / "ths_t_trader"


DATA_ROOT = _default_data_root()


def _child_env() -> Dict[str, str]:
    """为「再拉起一个本程序实例」准备干净的环境变量。

    PyInstaller onefile 的 bootloader 会读取 _PYI_APPLICATION_HOME_DIR 与
    _PYI_PARENT_PROCESS_LEVEL：这两个变量一旦存在，新进程会认为自己是父进程
    拉起的下级进程，直接复用父进程的 _MEIxxxx 解包目录而不再独立解包。
    父进程随后退出并清理该目录，于是子进程出现 certifi/cacert.pem 丢失
    （状态获取失败 / TLS CA bundle invalid path），父进程退出时也会报
    "Failed to remove temporary directory"。

    这里剔除相关变量，强制子进程解包到它自己的 _MEI 目录，两个症状一起消失。
    """
    env = os.environ.copy()
    for key in ("_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL",
                "_PYI_ARCHIVE_FILE", "_PYI_SPLASH_IPC", "_MEIPASS2"):
        env.pop(key, None)
    return env


def resolve_log_dir(cfg: Optional[Dict[str, Any]] = None) -> Path:
    """日志 / 审计 / 截图目录：留空默认 <DATA_ROOT>/logs，绝对路径原样使用，相对路径以 <DATA_ROOT> 为基准。"""
    raw = ""
    if cfg:
        raw = str((cfg.get("logging") or {}).get("log_dir") or "").strip()
    if not raw:
        return DATA_ROOT / "logs"
    path = Path(os.path.expandvars(raw)).expanduser()
    return path if path.is_absolute() else DATA_ROOT / path


def ensure_writable_dir(path: Path, probe: str = "") -> Path:
    """确保目录存在且可写；不可写时依次回退到 exe 目录 logs、系统临时目录，保证主流程不因日志失败中断。

    probe 传真实日志文件名时用「追加打开」验证真实可写；留空则只校验目录可写（截图等由写入方创建文件）。
    """
    candidates = [
        Path(path),
        BASE_DIR / "logs",
        Path(os.environ.get("TEMP") or os.environ.get("TMP") or ".") / "ths_t_trader_logs",
    ]
    for cand in candidates:
        try:
            cand.mkdir(parents=True, exist_ok=True)
            if probe:
                with open(cand / probe, "a", encoding="utf-8"):
                    pass
            elif not os.access(cand, os.W_OK):
                raise PermissionError(f"目录不可写: {cand}")
            if cand != Path(path):
                print(f"（警告：{path} 不可写，日志改写到 {cand}）")
            return cand
        except Exception:
            continue
    return Path(path)


APP_NAME = "THS-T-Assistant"
VERSION = "1.0.0"

# --------------------------------------------------------------------------------------
# 默认配置（config.json 中缺省项自动补齐，便于升级不破坏旧配置）
# --------------------------------------------------------------------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "market": {
        "poll_interval_sec": 3,
        "offhours_poll_interval_sec": 10,
        "timeout_sec": 5,
        "codes": ["600519"],
        "sina_url": "https://hq.sinajs.cn/list={}",
        "referer": "https://finance.sina.com.cn",
    },
    "positions": {
        "600519": {"name": "贵州茅台", "cost": 1450.0, "sellable_qty": 200},
    },
    "price_decimals": {
        "_help": "价格小数位覆盖入口：键为 6 位证券代码，值为小数位（0-4）。留空(default=null)时自动判断："
                 "场内基金/ETF（15/16/51/56/58 开头）或现价<10元 → 3 位（最小报价单位 0.001），其余 → 2 位（0.01）。",
        "default": None,
    },
    "trade": {
        "buy_qty": 100,
        "sell_qty": 100,
        "sessions": [["09:15", "11:30"], ["13:00", "15:05"]],
        "cooldown_sec": 300,
        "max_signals_per_day": 6,
        "allow_offhours_signal": False,
    },
    "signals": {
        "buy_first": {
            "enabled": True,
            "below_avg_pct": 0.30,
            "support_price": 0,
            "support_break_pct": 1.50,
            "max_drop_pct": 3.00,
            "check_cost": False,
            "cost_tolerance_pct": 1.00,
        },
        "sell_first": {
            "enabled": True,
            "above_avg_pct": 0.30,
            "min_profit_over_cost_pct": 0.50,
        },
    },
    "window": {
        "process_name": "happ.exe",
        "process_exe": r"I:\app\THS_win_1\同花顺远航版\bin\happ.exe",
        "title_keywords": ["同花顺", "远航版", "THS"],
        "require_foreground": False,
    },
    "order_ui": {
        "dry_run": True,
        "bring_to_front": True,
        "dpi_aware": True,
        "action_delay_ms": 350,
        "type_interval_sec": 0.03,
        "qty_unit": "shares",
        "price_mode": "signal",
        "price_offset_ticks": 0,
        "screenshot": True,
        "coords": {
            "code_box": [0, 0],
            "price_box": [0, 0],
            "qty_box": [0, 0],
            "buy_button": [0, 0],
            "code_box_sell": [0, 0],
            "price_box_sell": [0, 0],
            "qty_box_sell": [0, 0],
            "sell_button": [0, 0],
        },
        "_help": "下单窗口为买入/卖出双面板：左侧买入（code_box/price_box/qty_box/buy_button），右侧卖出（*_sell/sell_button）。坐标可用 python main.py --detect --write 自动识别，或 --pick 手动悬停拾取；[0,0] 表示未配置，真实下单会拒绝执行。",
    },
    "ui": {
        "popup_timeout_sec": 10,
        "sound": True,
        "always_on_top": True,
        "show_dashboard": True,
    },
    "logging": {
        "_help": "log_dir 留空或填相对路径（如 logs）时，统一落到 %LOCALAPPDATA%\\ths_t_trader\\logs；填绝对路径则原样使用。运行日志、审计 CSV、下单截图、状态台账都写在该目录。",
        "log_dir": "logs",
        "csv_encoding": "utf-8-sig",
        "audit_filename": "audit_trade.csv",
        "runtime_filename": "runtime.log",
    },
}


# --------------------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------------------
def deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """递归合并配置，override 优先。"""
    out = dict(base)
    for key, val in (override or {}).items():
        if isinstance(val, dict) and isinstance(out.get(key), dict):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def load_config(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise SystemExit(f"配置文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    cfg = deep_merge(DEFAULT_CONFIG, raw)
    # 标的与持仓标准化
    cfg["market"]["codes"] = [normalize_code(c) for c in cfg["market"]["codes"]]
    pos = {}
    for code, item in (cfg.get("positions") or {}).items():
        if str(code).startswith("_"):
            continue
        pos[normalize_code(code)[-6:]] = item
    cfg["positions"] = pos
    return cfg


def normalize_code(code: str) -> str:
    """把 600519 / sh600519 / SH600519 统一成新浪格式 sh600519。"""
    code = str(code).strip().lower()
    if re.fullmatch(r"(sh|sz|bj)\d{6}", code):
        return code
    digits = re.sub(r"\D", "", code)
    if len(digits) != 6:
        raise ValueError(f"无法识别的证券代码: {code}")
    if digits.startswith(("60", "68", "sv", "9", "5", "11", "13")):
        prefix = "sh"
    elif digits.startswith(("4", "8")):
        prefix = "bj"
    else:
        prefix = "sz"
    return prefix + digits


def short_code(code: str) -> str:
    return re.sub(r"\D", "", code)[-6:]


def parse_hhmm(text: str) -> dtime:
    hh, mm = str(text).split(":")
    return dtime(int(hh), int(mm))


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def fmt_money(val: float) -> str:
    if val >= 1e8:
        return f"{val / 1e8:.2f}亿"
    if val >= 1e4:
        return f"{val / 1e4:.2f}万"
    return f"{val:.0f}"


# --------------------------------------------------------------------------------------
# 价格精度：按标的自适应小数位与最小报价单位（tick）
# --------------------------------------------------------------------------------------
FUND_CODE_PREFIXES = ("15", "16", "51", "56", "58")   # 场内基金 / ETF 代码段
MAX_PRICE_DECIMALS = 4


def is_fund_like(code: str) -> bool:
    """6 位代码落在场内基金 / ETF 段（15 16 51 56 58 开头）即视为基金类标的。"""
    digits = short_code(code)
    return digits.startswith(FUND_CODE_PREFIXES)


def _coerce_decimals(val: Any) -> Optional[int]:
    """把配置里的值转成合法小数位；无法识别（含空串 / null）返回 None。"""
    if val is None or isinstance(val, bool):
        return None
    text = str(val).strip()
    if not re.fullmatch(r"[0-9]{1,2}", text):
        return None
    return max(0, min(MAX_PRICE_DECIMALS, int(text)))


def resolve_price_decimals(cfg: Optional[Dict[str, Any]], code: str, price: float = 0.0) -> int:
    """决定某标的的价格小数位。

    优先级：
    1. config.price_decimals 中按 6 位代码（或 sh/sz 前缀代码）给出的覆盖值；
    2. config.price_decimals.default 的全局覆盖值；
    3. 自动判断：场内基金 / ETF 段（15 16 51 56 58）或现价 < 10 元 → 3 位小数；
    4. 其余 → 2 位小数。
    """
    overrides = (cfg or {}).get("price_decimals") or {}
    digits = short_code(code)

    if isinstance(overrides, dict):
        for key, val in overrides.items():
            if str(key).startswith("_"):
                continue
            key_text = str(key).strip().lower()
            if key_text in ("default", "auto"):
                continue
            if short_code(key_text) == digits and digits:
                explicit = _coerce_decimals(val)
                if explicit is not None:
                    return explicit
        default_val = _coerce_decimals(overrides.get("default"))
        if default_val is not None:
            return default_val
    else:
        explicit = _coerce_decimals(overrides)
        if explicit is not None:
            return explicit

    if is_fund_like(digits):
        return 3
    try:
        if 0 < float(price) < 10:
            return 3
    except (TypeError, ValueError):
        pass
    return 2


def price_tick(decimals: int) -> float:
    """最小报价单位：3 位小数 → 0.001，2 位小数 → 0.01。"""
    return round(10.0 ** (-max(0, int(decimals))), 6)


def round_price(value: float, decimals: int) -> float:
    """按标的精度四舍五入到最小报价单位（避开二进制浮点导致 0.795 变 0.79）。"""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return 0.0
    scale = 10.0 ** max(0, int(decimals))
    if val >= 0:
        return math.floor(val * scale + 0.5) / scale
    return -math.floor(-val * scale + 0.5) / scale


def fmt_price(value: float, decimals: int) -> str:
    """按小数位输出价格文本；无法解析返回 '-'。"""
    try:
        val = float(value)
    except (TypeError, ValueError):
        return "-"
    return f"{val:.{max(0, int(decimals))}f}"


def fmt_price_of(cfg: Optional[Dict[str, Any]], code: str, value: float, price: float = 0.0) -> str:
    """便捷函数：先按标的判断小数位再格式化。"""
    return fmt_price(value, resolve_price_decimals(cfg, code, price))


def setup_console_encoding() -> None:
    # 无控制台（windowed / 双击 exe）模式下 sys.stdout 与 sys.stderr 为 None，
    # 直接 print 会抛 AttributeError。这里先尝试接管父进程控制台（从 cmd 运行时仍能看到输出），
    # 接管失败就把输出落到 logs\console_<日期>.log，保证任何路径下 print 都不会崩。
    if sys.stdout is None or sys.stderr is None:
        # 带命令行参数运行（--once / --detect / --pick / --selftest 等）时必须有可见输出，
        # 先附着父控制台，失败就自己开一个；双击无参数启动则不动控制台，输出只落日志文件。
        _cli_args = list(sys.argv[1:])
        _want_console = bool(_cli_args) and _cli_args[0] != "--ui"
        attached = False
        if os.name == "nt":
            try:
                import ctypes

                _k32 = ctypes.windll.kernel32
                if _k32.AttachConsole(-1):  # ATTACH_PARENT_PROCESS
                    attached = True
                elif _want_console and _k32.AllocConsole():
                    attached = True
                    try:
                        _k32.SetConsoleTitleW(f"{APP_NAME} v{VERSION} - 命令行模式")
                    except Exception:
                        pass
            except Exception:
                attached = False
        if attached:
            try:
                if sys.stdout is None:
                    sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)  # type: ignore[assignment]
                if sys.stderr is None:
                    sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)  # type: ignore[assignment]
            except Exception:
                attached = False
        if not attached:
            try:
                _console_dir = ensure_writable_dir(DATA_ROOT / "logs")
                _console_dir.mkdir(parents=True, exist_ok=True)
                _console_fh = open(
                    _console_dir / f"console_{datetime.now().strftime('%Y%m%d')}.log",
                    "a", encoding="utf-8", errors="replace",
                )
                if sys.stdout is None:
                    sys.stdout = _console_fh  # type: ignore[assignment]
                if sys.stderr is None:
                    sys.stderr = _console_fh  # type: ignore[assignment]
            except Exception:
                pass
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
    except Exception:
        pass


# --------------------------------------------------------------------------------------
# 行情：新浪接口
# --------------------------------------------------------------------------------------
try:
    import requests
except ImportError:  # pragma: no cover
    requests = None  # type: ignore


@dataclass
class Quote:
    """单只标的的实时行情快照。"""

    code: str
    name: str = ""
    open: float = 0.0
    prev_close: float = 0.0
    price: float = 0.0
    high: float = 0.0
    low: float = 0.0
    volume: float = 0.0          # 成交量（股）
    amount: float = 0.0          # 成交额（元）
    bid: List[Tuple[float, float]] = field(default_factory=list)  # [(价, 量)] 买一~买五
    ask: List[Tuple[float, float]] = field(default_factory=list)  # [(价, 量)] 卖一~卖五
    quote_date: str = ""
    quote_time: str = ""
    fetched_at: str = ""
    decimals: int = 2           # 价格小数位：股票 2 位，ETF/基金或现价<10元 3 位

    # ---- 派生指标 ----
    @property
    def avg_price(self) -> float:
        """分时均价线 = 成交额 / 成交量（每股均价）。"""
        if self.volume > 0 and self.amount > 0:
            return self.amount / self.volume
        return self.price

    @property
    def amplitude_pct(self) -> float:
        """日内振幅 %。"""
        if self.prev_close <= 0:
            return 0.0
        return (self.high - self.low) / self.prev_close * 100.0

    @property
    def change_pct(self) -> float:
        """当日涨跌幅 %。"""
        if self.prev_close <= 0:
            return 0.0
        return (self.price - self.prev_close) / self.prev_close * 100.0

    def deviation_from_avg_pct(self) -> float:
        """现价相对分时均价线的偏离 %（负=低于均价）。"""
        avg = self.avg_price
        if avg <= 0:
            return 0.0
        return (self.price - avg) / avg * 100.0

    def pnl_pct(self, cost: float) -> float:
        """相对持仓成本的盈亏 %。"""
        if cost <= 0:
            return 0.0
        return (self.price - cost) / cost * 100.0

    def bid_ask_text(self, depth: int = 5) -> str:
        parts = []
        d = self.decimals
        for i in range(min(depth, len(self.bid))):
            p, v = self.bid[i]
            parts.append(f"买{i + 1} {fmt_price(p, d)}x{int(v)}")
        for i in range(min(depth, len(self.ask))):
            p, v = self.ask[i]
            parts.append(f"卖{i + 1} {fmt_price(p, d)}x{int(v)}")
        return " | ".join(parts)

    def summary(self) -> str:
        d = self.decimals
        return (
            f"[{self.code}] {self.name} 现价 {fmt_price(self.price, d)} "
            f"({self.change_pct:+.2f}%) 均价 {fmt_price(self.avg_price, d)} "
            f"偏离 {self.deviation_from_avg_pct():+.2f}% "
            f"开 {fmt_price(self.open, d)} 高 {fmt_price(self.high, d)} 低 {fmt_price(self.low, d)} "
            f"量 {fmt_money(self.volume)} 额 {fmt_money(self.amount)} "
            f"振幅 {self.amplitude_pct:.2f}% 行情时间 {self.quote_time}"
        )


class SinaFetcher:
    """新浪行情批量抓取。"""

    CHUNK = 40

    def __init__(self, cfg: Dict[str, Any]):
        if requests is None:
            raise SystemExit("缺少依赖 requests，请先执行: pip install requests")
        self.cfg = cfg
        self.url_tpl = cfg["market"]["sina_url"]
        self.timeout = float(cfg["market"]["timeout_sec"])
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Referer": cfg["market"]["referer"],
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
                ),
                "Accept": "*/*",
            }
        )
        self._line_re = re.compile(r'var hq_str_([a-z]{2}\d{6})="([^"]*)";')

    def fetch(self, codes: List[str]) -> Dict[str, Quote]:
        result: Dict[str, Quote] = {}
        for i in range(0, len(codes), self.CHUNK):
            chunk = codes[i : i + self.CHUNK]
            url = self.url_tpl.format(",".join(chunk))
            resp = self.session.get(url, timeout=self.timeout)
            resp.raise_for_status()
            resp.encoding = "gb18030"
            text = resp.text
            for m in self._line_re.finditer(text):
                quote = self._parse(m.group(1), m.group(2))
                if quote is not None:
                    result[quote.code] = quote
        return result

    def _parse(self, code: str, payload: str) -> Optional[Quote]:
        if not payload.strip():
            return None
        f = payload.split(",")
        if len(f) < 32:
            return None

        def num(idx: int) -> float:
            try:
                return float(f[idx])
            except (ValueError, IndexError):
                return 0.0

        bid: List[Tuple[float, float]] = []
        ask: List[Tuple[float, float]] = []
        for i in range(5):
            bv, bp = num(10 + i * 2), num(11 + i * 2)
            av, ap = num(20 + i * 2), num(21 + i * 2)
            if bp > 0:
                bid.append((bp, bv))
            if ap > 0:
                ask.append((ap, av))

        q = Quote(
            code=code,
            name=f[0],
            open=num(1),
            prev_close=num(2),
            price=num(3),
            high=num(4),
            low=num(5),
            volume=num(8),
            amount=num(9),
            bid=bid,
            ask=ask,
            quote_date=f[30],
            quote_time=f[31],
            fetched_at=now_str(),
            decimals=resolve_price_decimals(self.cfg, code, num(3)),
        )
        if q.price <= 0:
            return None
        return q


# --------------------------------------------------------------------------------------
# 信号引擎
# --------------------------------------------------------------------------------------
@dataclass
class Check:
    name: str
    ok: bool
    detail: str

    def render(self) -> str:
        return f"{'√' if self.ok else '×'} {self.name}: {self.detail}"


@dataclass
class Signal:
    signal_id: str
    code: str
    name: str
    # BUY_FIRST=正T(先买后卖) / SELL_FIRST=反T(先卖后买)
    # REBUY=买回(反T闭环，补回此前高卖的底仓，不登记新批次) / TIME_STOP=时间止损强平
    stype: str
    triggered_at: str
    price: float
    avg_price: float
    prev_close: float
    day_open: float
    high: float
    low: float
    volume: float
    amount: float
    amplitude_pct: float
    change_pct: float
    cost: float
    pnl_pct: float
    deviation_pct: float
    support_price: float
    quote_time: str
    bid_ask: str
    reasons: List[str]
    suggested_price: float
    suggested_qty: float
    window_state: str = ""
    decimals: int = 2           # 该标的的价格小数位（股票 2 / ETF 类 3）
    benchmark_price: float = 0.0     # 卖出基准价（当日批次成本 / 分时均价 / 历史成本）
    benchmark_desc: str = ""         # 基准来源说明，直接写进触发依据
    batch_qty: float = 0.0           # 本次动作对应的待平批次数量（0 表示无批次可配）
    hold_minutes: float = 0.0        # 最早未平批次已持有分钟数

    @property
    def stype_label(self) -> str:
        if self.stype == "BUY_FIRST":
            return "正T·先买后卖"
        if self.stype == "REBUY":
            return "买回·反T闭环"
        if self.stype == "TIME_STOP":
            return "时间止损·强制平仓"
        return "反T·先卖后买"

    @property
    def side(self) -> str:
        return "buy" if self.stype in ("BUY_FIRST", "REBUY") else "sell"

    def price_text(self, value: float) -> str:
        """按本标的精度输出价格文本（ETF 0.795 不会被写成 0.80）。"""
        return fmt_price(value, self.decimals)

    def render_detail(self) -> str:
        d = self.decimals
        lines = [
            f"标的：{self.name}（{self.code}）",
            f"现价：{fmt_price(self.price, d)}    分时均价：{fmt_price(self.avg_price, d)}    偏离：{self.deviation_pct:+.2f}%",
            f"昨收：{fmt_price(self.prev_close, d)}   今开：{fmt_price(self.day_open, d)}   "
            f"最高：{fmt_price(self.high, d)}   最低：{fmt_price(self.low, d)}",
            f"涨跌幅：{self.change_pct:+.2f}%   日内振幅：{self.amplitude_pct:.2f}%",
            f"持仓成本：{fmt_price(self.cost, d)}   盈亏：{self.pnl_pct:+.2f}%   "
            f"支撑：{fmt_price(self.support_price, d)}",
            f"五档：{self.bid_ask}",
            "触发依据：",
        ]
        lines += [f"  · {r}" for r in self.reasons]
        lines.append(
            f"建议：{'买入' if self.side == 'buy' else '卖出'} {int(self.suggested_qty)} 股 "
            f"@ {fmt_price(self.suggested_price, d)}"
        )
        if self.benchmark_price > 0:
            lines.append(
                f"操作基准：{fmt_price(self.benchmark_price, d)}（{self.benchmark_desc or '未标注'}）"
            )
        if self.batch_qty > 0:
            lines.append(
                f"待平批次：{int(self.batch_qty)} 股    已持有 {self.hold_minutes:.0f} 分钟"
            )
        lines.append(f"行情时间：{self.quote_time}    信号编号：{self.signal_id}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# 当日买入批次台账：平T必须对标当日真实买入批次，而不是历史持仓成本
# --------------------------------------------------------------------------------------
@dataclass
class Batch:
    code: str
    price: float                # 买入价
    qty: float                  # 剩余未平数量
    opened_at: float            # 建仓时间戳
    signal_id: str = ""
    fee: float = 0.0            # 该批次买入佣金（按剩余量等比分摊）

    @property
    def cost_price(self) -> float:
        """含买入佣金后的每股实际成本。"""
        if self.qty <= 0:
            return self.price
        return self.price + self.fee / max(1.0, self.qty)


class BatchBook:
    """记录当日已买未卖的批次，卖出时 FIFO 配对。

    用途：① 卖出基准价 = 批次成本 + 目标跳数 + 单边佣金，而不是历史成本；
          ② 时间止损依据批次持有时长；
          ③ 每天开盘自动清空（_roll），跨日不会拿昨天的批次当今天基准。

    注意：台账在内存中，脚本重启即丢失。丢失后卖出基准会自动退回
    config.signals.sell_first.basis 指定的备选基准，并在触发依据里写明来源，
    不会把「无批次」当成「有批次」。
    """

    def __init__(self) -> None:
        self._batches: Dict[str, List[Batch]] = {}
        self._day: str = ""

    def _roll(self) -> None:
        today = date.today().isoformat()
        if self._day != today:
            self._day = today
            self._batches.clear()

    def reset(self) -> None:
        self._batches.clear()

    def add(self, code: str, price: float, qty: float, fee: float = 0.0,
            signal_id: str = "") -> Batch:
        self._roll()
        b = Batch(code=short_code(code), price=float(price), qty=float(qty),
                  opened_at=time.time(), signal_id=signal_id, fee=float(fee))
        self._batches.setdefault(b.code, []).append(b)
        return b

    def pending(self, code: str) -> List[Batch]:
        self._roll()
        return [b for b in self._batches.get(short_code(code), []) if b.qty > 0]

    def pending_qty(self, code: str) -> float:
        return sum(b.qty for b in self.pending(code))

    def weighted_cost(self, code: str) -> float:
        """待平批次的加权每股成本（含买入佣金）。"""
        bs = self.pending(code)
        total = sum(b.qty for b in bs)
        if total <= 0:
            return 0.0
        return sum(b.cost_price * b.qty for b in bs) / total

    def oldest(self, code: str) -> Optional[Batch]:
        bs = self.pending(code)
        return bs[0] if bs else None

    def hold_minutes(self, code: str) -> float:
        b = self.oldest(code)
        return 0.0 if b is None else max(0.0, (time.time() - b.opened_at) / 60.0)

    def consume(self, code: str, qty: float) -> List[Batch]:
        """FIFO 扣减批次，返回被本次卖出平掉的份额。"""
        self._roll()
        left = float(qty)
        used: List[Batch] = []
        for b in self._batches.get(short_code(code), []):
            if left <= 0:
                break
            if b.qty <= 0:
                continue
            take = min(b.qty, left)
            b.qty -= take
            left -= take
            if b.qty <= 0:
                b.fee = 0.0
            else:
                b.fee = b.fee * (b.qty / (b.qty + take))
            used.append(Batch(code=b.code, price=b.price, qty=take,
                              opened_at=b.opened_at, signal_id=b.signal_id))
        return used

    def snapshot(self, code: str = "") -> str:
        codes = [short_code(code)] if code else list(self._batches.keys())
        parts = []
        for c in codes:
            bs = self.pending(c)
            if not bs:
                continue
            total = sum(b.qty for b in bs)
            decimals = resolve_price_decimals(None, c)
            parts.append(f"{c} 待平{int(total)}股@{fmt_price(self.weighted_cost(c), decimals)}"
                         f"（{len(bs)}批，最早持有{self.hold_minutes(c):.0f}分钟）")
        return "；".join(parts) if parts else "无待平批次"


def fee_conf(cfg: Dict[str, Any], code: str = "") -> Dict[str, float]:
    """归一化费用配置：全局默认 → 按品种（fund/stock）→ 按具体代码，三级覆盖。

    ETF（场内基金）与股票的收费结构不同：ETF 无印花税、多数券商无最低佣金门槛；
    股票卖出收印花税、且有最低佣金（常见 5 元）。分三级后同一份配置即可同时容纳
    两类标的：ETF 走 fund 分支按真实成本计算，将来加入股票自动落到 stock 分支，
    不需要改代码。
    """
    trade = cfg.get("trade", {}) if isinstance(cfg, dict) else {}
    base = trade.get("fees") if isinstance(trade.get("fees"), dict) else {}
    out: Dict[str, float] = {
        "commission_rate_pct": float(base.get("commission_rate_pct", 0.0) or 0.0),
        "min_commission": float(base.get("min_commission", 0.0) or 0.0),
        "stamp_tax_pct": float(base.get("stamp_tax_pct", 0.0) or 0.0),
        "transfer_fee_pct": float(base.get("transfer_fee_pct", 0.0) or 0.0),
    }
    kind = "fund" if is_fund_like(code) else "stock"
    layers: List[Dict[str, Any]] = []
    by_kind = base.get("by_kind") if isinstance(base.get("by_kind"), dict) else {}
    layers.append(by_kind.get(kind) or {})
    by_code = base.get("by_code") if isinstance(base.get("by_code"), dict) else {}
    if code:
        layers.append(by_code.get(short_code(code)) or {})
    for layer in layers:
        if not isinstance(layer, dict):
            continue
        for key in out:
            val = layer.get(key)
            if val is None:
                continue
            try:
                out[key] = float(val)
            except (TypeError, ValueError):
                continue
    return out


def fee_of(cfg: Dict[str, Any], code: Any = "", amount: Optional[float] = None,
           side: str = "sell") -> float:
    """估算单笔费用（元）。

    结构：佣金双向收取（比例与最低佣金取大者）；印花税、过户费仅卖出收取，
    ETF 把这两项配 0 后即自动退化为「只有佣金」的标准结构。
    兼容旧签名 fee_of(cfg, amount)：amount 省略时把 code 当成交额。
    """
    if amount is None:
        amount = float(code or 0.0)
        code = ""
    amount = float(amount or 0.0)
    if amount <= 0:
        return 0.0
    conf = fee_conf(cfg, str(code) if code else "")
    commission = max(amount * abs(conf["commission_rate_pct"]) / 100.0,
                     abs(conf["min_commission"]))
    extra = 0.0
    if str(side).strip().lower() == "sell":
        extra = amount * (abs(conf["stamp_tax_pct"]) + abs(conf["transfer_fee_pct"])) / 100.0
    return round(commission + extra, 4)


def ceil_to_tick(value: float, tick: float) -> float:
    """向上取整到最小报价单位：卖出地板价必须真正高于基准才算平T。"""
    if tick <= 0:
        return value
    return round(math.ceil(value / tick - 1e-9) * tick, 6)


class SignalEngine:
    """按配置阈值评估行情，产生信号（带冷却与每日次数限制）。"""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        self._last_trigger: Dict[Tuple[str, str], float] = {}
        self._count: Dict[Tuple[str, str], int] = {}
        self._day: str = ""
        self.book = BatchBook()                      # 当日买入待平批次台账
        self._time_stop_fired: Dict[str, str] = {}   # 标的 -> 已发时间止损的批次指纹
        self._rebuy: Dict[str, Dict[str, Any]] = {}  # 标的 -> 反T已卖待回买批次（闭环锁）
        self._last_sell: Dict[str, Dict[str, Any]] = {}  # 标的 -> 上一笔反T卖出水位（price/at/closed_price）
        self._sold_today: Dict[str, float] = {}      # 标的 -> 当日已卖出股数（底仓余量递减，回买时释放）
        self._net_pos: Dict[str, float] = {}         # 标的 -> 当日净持仓变化（买+卖-，用于暴露底仓缺口）
        self._vol: Dict[str, List[Tuple[float, float]]] = {}  # 标的 -> [(时间戳, 累计成交量)] 量能采样
        self._avg: Dict[str, List[Tuple[float, float]]] = {}  # 标的 -> [(时间戳, 分时均价)] 趋势采样
        self._px: Dict[str, List[Tuple[float, float]]] = {}   # 标的 -> [(时间戳, 现价)] 波动率采样
        self._stop_lock: Dict[str, Dict[str, Any]] = {}  # 标的 -> 止损后冷静期锁（at/price/qty/stype）
        self._realized: Dict[str, float] = {}        # 标的 -> 当日已实现盈亏（卖出时结算，含买入成本）
        self.load_state()                            # 重启后恢复闭环锁与批次台账

    # ---- 量能/趋势采样：用轮询快照估算短期成交量速率与均价线斜率 ----
    def _push_volume(self, q: Quote) -> None:
        """每次轮询记录一笔（时间戳, 累计成交量, 分时均价），供量能确认与趋势过滤使用。"""
        now = time.time()
        code = short_code(q.code)
        buf = self._vol.setdefault(code, [])
        buf.append((now, float(q.volume or 0.0)))
        abuf = self._avg.setdefault(code, [])
        abuf.append((now, float(q.avg_price or 0.0)))
        pxbuf = self._px.setdefault(code, [])
        pxbuf.append((now, float(q.price or 0.0)))
        cutoff = now - 1200.0   # 最多保留 20 分钟样本
        while len(buf) > 2 and buf[0][0] < cutoff:
            buf.pop(0)
        while len(abuf) > 2 and abuf[0][0] < cutoff:
            abuf.pop(0)
        while len(pxbuf) > 2 and pxbuf[0][0] < cutoff:
            pxbuf.pop(0)

    def _volume_ratio(self, code: str, window_sec: float, baseline_sec: float) -> Optional[float]:
        """近 window_sec 的每秒成交量 ÷ 其前 baseline_sec 的每秒均量。

        返回 None 表示样本不足以计算（刚启动或刚重启），调用方应按「跳过确认」处理，
        避免开盘/重启后长时间无法产生任何信号。
        """
        buf = self._vol.get(short_code(code)) or []
        if len(buf) < 3:
            return None
        t_now, v_now = buf[-1]

        def first_after(target: float) -> Optional[Tuple[float, float]]:
            for t, v in buf:
                if t >= target:
                    return t, v
            return None

        w = first_after(t_now - window_sec)
        b = first_after(t_now - window_sec - baseline_sec)
        if w is None or b is None:
            return None
        dt_w = t_now - w[0]
        dt_b = w[0] - b[0]
        if dt_w <= 0 or dt_b <= 0:
            return None
        recent = (v_now - w[1]) / dt_w
        base = (w[1] - b[1]) / dt_b
        if base <= 0:
            return None
        return recent / base

    # ---- 趋势采样：分时均价线（VWAP）的抬升速度，用于识别单边上行、避免高卖卖飞 ----
    def _avg_slope_ticks(self, code: str, window_sec: float, tick: float) -> Optional[float]:
        """近 window_sec 内分时均价线的变化量（单位：跳）。None 表示样本不足。

        均价线是当日累计成交额/成交量的比值（VWAP），比现价平滑得多：
        它在持续抬升说明买盘一直占优，此时反T高卖极易卖飞。
        样本不足（刚开盘或刚重启）返回 None，调用方应按「跳过」处理，
        避免启动初期长时间无法产生卖出信号。
        """
        buf = self._avg.get(short_code(code)) or []
        if len(buf) < 3 or tick <= 0:
            return None
        t_now, a_now = buf[-1]
        if a_now <= 0:
            return None
        for t, a in buf:
            if t >= t_now - window_sec:
                if a <= 0:
                    return None
                return (a_now - a) / tick
        return None

    # ---- 状态持久化：避免重启后闭环锁丢失导致重复下单 ----
    # ---- 波动率自适应门槛与按标的配置分层 ----
    def _trade_conf(self, code: str = "") -> Dict[str, Any]:
        """取 trade 配置并按标的做两层深合并（position_overrides.<code>.trade）。

        同一套 cooldown / 次数上限套在波动完全不同的两个标的上必然一边过松一边过紧，
        因此允许按代码覆盖 trade 层的任意字段（含 time_stop.price_layer.loss_pct）。
        """
        base: Dict[str, Any] = dict(self.cfg.get("trade") or {})
        ovs = self.cfg.get("position_overrides")
        if not isinstance(ovs, dict) or not code:
            return base
        ov = ovs.get(short_code(code)) or ovs.get(code) or {}
        if not isinstance(ov, dict):
            return base
        layer = ov.get("trade")
        if not isinstance(layer, dict):
            return base
        merged = dict(base)
        for key, val in layer.items():
            if isinstance(val, dict) and isinstance(merged.get(key), dict):
                sub = dict(merged[key])
                sub.update(val)
                merged[key] = sub
            else:
                merged[key] = val
        return merged

    def _signal_conf(self, keywords: str, code: str) -> Dict[str, Any]:
        """取 signals.<keywords> 并按标的覆盖（position_overrides.<code>.signals）。"""
        base: Dict[str, Any] = dict(self.cfg["signals"].get(keywords) or {})
        ovs = self.cfg.get("position_overrides")
        if isinstance(ovs, dict) and code:
            ov = ovs.get(short_code(code)) or ovs.get(code) or {}
            if isinstance(ov, dict) and isinstance(ov.get("signals"), dict):
                layer = ov["signals"].get(keywords)
                if isinstance(layer, dict):
                    base.update(layer)
        return base

    def _atr_pct(self, code: str, window_sec: float = 1800.0,
                 hold_sec: float = 1200.0) -> Optional[float]:
        """持仓周期上的可达振幅（百分比中位数），用于近似分时 ATR。

        口径很关键：如果按 1 分钟切段算振幅，得到的只是噪声尺度，远小于「持仓 20 分钟
        内能漂多少」，会让动态门槛恒撞下限、退化成固定跳数。因此这里直接按持仓周期
        （hold_sec）滑窗取「段内最高-最低 ÷ 段均价」，再取中位数，衡量的是
        「过去半小时里，每 20 分钟最坏能走出多大空间」——这才是 T 能不能平掉的真实依据。

        ETF 单跳绝对值极小（159866 一跳仅 0.071%），跨标的比较必须折算成百分比。
        返回 None 表示样本不足（刚开盘或刚重启），调用方应退回静态门槛。
        """
        buf = self._px.get(short_code(code)) or []
        if len(buf) < 8:
            return None
        now = time.time()
        start = now - abs(float(window_sec or 1800.0))
        pts = [(t, p) for t, p in buf if t >= start and p > 0]
        if len(pts) < 8:
            pts = [(t, p) for t, p in buf[-60:] if p > 0]
        if len(pts) < 8:
            return None
        t_from, t_to = pts[0][0], pts[-1][0]
        span = max(t_to - t_from, 1e-6)
        hold = float(hold_sec or 0.0) or span
        hold = min(max(hold, span * 0.2), span)
        step = max(hold / 2.0, 1.0)
        amps: List[float] = []
        st = t_from
        while st + hold <= t_to + 1e-9:
            end_t = st + hold
            seg = [p for t, p in pts if st <= t <= end_t]
            if len(seg) >= 3:
                mid = sum(seg) / len(seg)
                if mid > 0:
                    amps.append((max(seg) - min(seg)) / mid * 100.0)
            st += step
        if not amps:
            vals = [p for _, p in pts]
            mid = sum(vals) / len(vals)
            if mid <= 0:
                return None
            return (max(vals) - min(vals)) / mid * 100.0
        amps.sort()
        n = len(amps)
        return amps[n // 2] if n % 2 else (amps[n // 2 - 1] + amps[n // 2]) / 2.0

    def _breakeven_ticks(self, q: Quote, tick: float) -> int:
        """保本跳数：一买一卖双边费用折算成跳数（向上取整）。

        低于这个跳数的平T在扣费后是亏的，因此动态门槛的下限不能低于保本跳数 + 1。
        """
        if q.price <= 0 or tick <= 0:
            return 1
        qty = float(self.cfg["trade"].get("sell_qty", 0) or 0) or 100.0
        qty = max(100.0, qty)
        amount = q.price * qty
        if amount <= 0:
            return 1
        fee = fee_of(self.cfg, q.code, amount, "buy") + fee_of(self.cfg, q.code, amount, "sell")
        fee_pct = fee / amount * 100.0
        tick_pct = tick / q.price * 100.0
        if tick_pct <= 0:
            return 1
        return max(1, int(math.ceil(fee_pct / tick_pct - 1e-9)))

    def _dyn_conf(self, q: Quote, conf: Dict[str, Any]) -> Dict[str, Any]:
        """把 dev_ticks / target_ticks 换成「百分比主导 + 跳数兜底」的有效跳数。

        原理：eff_ticks = clamp(round(k × ATR% ÷ 单跳%), 保本跳数+1, max_ticks)。
        静态跳数在低波标的（159866 一跳到 0.071%，4 跳仅 0.28%）上偏松、
        在高波标的（159325 一跳到 0.132%，4 跳 0.53%）上偏紧，自适应后同一条规则
        在两类标的上都落在「可达但覆盖成本」的区间。样本不足时沿用静态跳数。
        """
        out: Dict[str, Any] = dict(conf)
        trade = self.cfg.get("trade") or {}
        dyn = trade.get("dynamic_ticks") if isinstance(trade.get("dynamic_ticks"), dict) else {}
        base_dev = int(conf.get("dev_ticks", 0) or 0)
        base_tgt = int(conf.get("target_ticks", 0) or 0)
        out["dev_ticks_base"] = base_dev
        out["target_ticks_base"] = base_tgt
        if not dyn or not bool(dyn.get("enabled", False)):
            return out
        tick = price_tick(q.decimals)
        if tick <= 0 or q.price <= 0:
            return out
        tick_pct = tick / q.price * 100.0
        atr = self._atr_pct(
            q.code,
            float(dyn.get("window_sec", 1800) or 1800),
            float(dyn.get("hold_window_sec", 1200) or 1200),
        )
        if atr is None or tick_pct <= 0:
            out["dyn_note"] = "ATR 样本不足，沿用静态跳数"
            return out
        k = abs(float(dyn.get("k", 0.3) or 0.3))
        be = self._breakeven_ticks(q, tick)
        raw = k * atr / tick_pct

        def clamp_ticks(base: int) -> int:
            lo = int(dyn.get("min_ticks", 0) or 0)
            hi = int(dyn.get("max_ticks", 0) or 0)
            lo = lo if lo > 0 else be + 1
            hi = hi if hi > 0 else max(base * 2, lo + 2)
            hi = max(hi, lo)
            return int(min(max(int(round(raw)), lo), hi))

        if base_dev > 0 or bool(dyn.get("always", False)):
            out["dev_ticks"] = clamp_ticks(max(base_dev, 1))
        if base_tgt > 0 or bool(dyn.get("always", False)):
            out["target_ticks"] = clamp_ticks(max(base_tgt, 1))
        out["atr_pct"] = atr
        out["breakeven_ticks"] = be
        out["dyn_note"] = (
            f"ATR {atr:.2f}% ÷ 单跳 {tick_pct:.3f}% × k={k:g} → "
            f"偏离 {out.get('dev_ticks', base_dev)} 跳 / 目标 {out.get('target_ticks', base_tgt)} 跳"
            f"（保本 {be} 跳，上限 {int(dyn.get('max_ticks', 0) or 0) or 'auto'}）"
        )
        return out

    def _state_file(self) -> str:
        # 台账固定落到用户级数据目录：打包后 __file__ 指向 _MEIxxxx 临时解包目录，
        # 退出即被清理，用它会令当日闭环锁、批次台账、冷却计数每次启动归零。
        data_dir = ensure_writable_dir(DATA_ROOT / "data", "signal_state.json")
        return str(data_dir / "signal_state.json")

    def save_state(self) -> None:
        """把当日闭环锁、批次台账、冷却与计数落盘。"""
        try:
            book: Dict[str, List[Dict[str, Any]]] = {}
            for code, bs in self.book._batches.items():
                items = [{"price": b.price, "qty": b.qty, "opened_at": b.opened_at,
                          "signal_id": b.signal_id, "fee": b.fee}
                         for b in bs if b.qty > 0]
                if items:
                    book[short_code(code)] = items
            data = {
                "day": date.today().isoformat(),
                "book": book,
                "rebuy": self._rebuy,
                "last_sell": self._last_sell,
                "sold_today": self._sold_today,
                "net_pos": self._net_pos,
                "stop_lock": self._stop_lock,
                "realized": self._realized,
                "last_trigger": {f"{k[0]}|{k[1]}": v for k, v in self._last_trigger.items()},
                "count": {f"{k[0]}|{k[1]}": v for k, v in self._count.items()},
            }
            path = self._state_file()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp, path)
        except Exception as exc:  # 状态落盘失败不影响主流程
            logging.getLogger("t_trader").warning("信号状态保存失败：%s", exc)

    def load_state(self) -> None:
        """仅恢复当日状态；跨日一律丢弃，防止拿昨天的锁当今天基准。"""
        path = self._state_file()
        if not os.path.exists(path):
            return
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:
            return
        today = date.today().isoformat()
        if str(data.get("day", "")) != today:
            return
        self._day = today
        self.book._day = today
        for code, items in (data.get("book") or {}).items():
            for it in items:
                try:
                    qty = float(it.get("qty") or 0)
                    if qty <= 0:
                        continue
                    b = Batch(code=short_code(code), price=float(it["price"]), qty=qty,
                              opened_at=float(it.get("opened_at") or time.time()),
                              signal_id=str(it.get("signal_id") or ""),
                              fee=float(it.get("fee") or 0.0))
                except Exception:
                    continue
                self.book._batches.setdefault(b.code, []).append(b)
        self._rebuy = {k: dict(v) for k, v in (data.get("rebuy") or {}).items()}
        self._last_sell = {k: dict(v) for k, v in (data.get("last_sell") or {}).items()}
        self._sold_today = {k: float(v) for k, v in (data.get("sold_today") or {}).items()}
        self._net_pos = {k: float(v) for k, v in (data.get("net_pos") or {}).items()}
        self._stop_lock = {k: dict(v) for k, v in (data.get("stop_lock") or {}).items()}
        self._realized = {k: float(v) for k, v in (data.get("realized") or {}).items()}
        for k, v in (data.get("last_trigger") or {}).items():
            parts = str(k).split("|")
            if len(parts) == 2:
                self._last_trigger[(parts[0], parts[1])] = float(v)
        for k, v in (data.get("count") or {}).items():
            parts = str(k).split("|")
            if len(parts) == 2:
                self._count[(parts[0], parts[1])] = int(v)
        logging.getLogger("t_trader").warning(
            "已恢复当日信号状态：待平 %s，待回买 %s，已卖 %s，净持仓 %s，冷却 %s 项，计数 %s 项",
            {c: int(sum(b.qty for b in v)) for c, v in self.book._batches.items() if v},
            {k: int(v.get("qty", 0)) for k, v in self._rebuy.items()},
            {k: int(v) for k, v in self._sold_today.items()},
            {k: int(v) for k, v in self._net_pos.items()},
            len(self._last_trigger), len(self._count))

    def _roll_day(self) -> None:
        today = date.today().isoformat()
        if self._day != today:
            # 不留隔夜：跨日仍有未平批次说明昨日收盘前没平掉，属于异常，必须告警后再按隔夜持仓清理，
            # 否则昨日批次会被当成今日待平批次，用今天的新底仓去核算昨天的价格。
            leak = {c: int(sum(b.qty for b in v)) for c, v in self.book._batches.items()
                    if v and sum(b.qty for b in v) > 0}
            if leak:
                logging.getLogger("t_trader").warning(
                    "跨日残留未平批次 %s：上一个交易日收盘前未平仓，已按隔夜持仓处理并清空T台账"
                    "（该仓位已转为底仓，不计入今日做T）", leak)
            self._day = today
            self.book.reset()
            self._last_trigger.clear()
            self._count.clear()
            self._time_stop_fired.clear()
            self._rebuy.clear()
            self._last_sell.clear()   # 卖出水位按日重置，昨天的高点不能当今天的卖出线
            self._stop_lock.clear()   # 止损冷静期锁只对当日有效
            self._sold_today.clear()
            self._net_pos.clear()  # 净持仓变化以当日为基准，跨日必须归零
            self._realized.clear()  # 已实现盈亏按日结算，用于日内亏损熔断
            self._vol.clear()      # 量能/均价采样属当日分时数据，跨日必须丢
            self._avg.clear()

    def position(self, code: str) -> Dict[str, Any]:
        pos = self.cfg["positions"].get(short_code(code))
        if pos is None:
            return {"name": "", "cost": 0.0, "sellable_qty": 0}
        t0 = pos.get("t0")
        return {
            "name": pos.get("name", ""),
            "cost": float(pos.get("cost", 0) or 0),
            "sellable_qty": float(pos.get("sellable_qty", 0) or 0),
            # t0=True 当日买入当日可卖（跨境/债券/黄金/货币ETF）；t0=False 为T+1（股票、股票ETF）；
            # 未配置(None)时退回按代码前缀推断，兼容旧配置
            "t0": t0 if isinstance(t0, bool) else None,
        }

    def _is_t0(self, code: str, pos: Dict[str, Any]) -> bool:
        """标的当日买入是否可卖：配置 t0 优先，未配置时按代码前缀推断。"""
        flag = pos.get("t0")
        if isinstance(flag, bool):
            return flag
        return is_fund_like(code)

    # ---- 日内亏损熔断：当日已实现亏损触及上限后，停止一切新开仓（平仓不受限）----
    def _circuit_conf(self) -> Dict[str, Any]:
        conf = self.cfg["trade"].get("daily_loss_circuit") or {}
        return conf if isinstance(conf, dict) else {}

    def _realized_total(self) -> float:
        return float(sum(v for v in self._realized.values()))

    def _circuit_broken(self) -> Optional[str]:
        """返回熔断原因文本；未熔断返回 None。已实现亏损为正表示盈利。"""
        conf = self._circuit_conf()
        if not conf.get("enabled", False):
            return None
        loss = self._realized_total()
        if loss >= 0:
            return None
        drawdown = -loss
        cap_amt = abs(float(conf.get("max_loss_amount", 0) or 0))
        if cap_amt > 0 and drawdown >= cap_amt:
            return f"日内已实现亏损 {drawdown:.2f} 元 ≥ 上限 {cap_amt:.2f} 元"
        cap_pct = abs(float(conf.get("max_loss_pct", 0) or 0))
        if cap_pct > 0:
            base = 0.0
            for code, p in (self.cfg.get("positions") or {}).items():
                try:
                    base += float(p.get("cost", 0) or 0) * float(p.get("sellable_qty", 0) or 0)
                except Exception:
                    continue
            if base > 0 and drawdown / base * 100.0 >= cap_pct:
                return (f"日内已实现亏损 {drawdown / base * 100.0:.2f}% ≥ 上限 {cap_pct:.2f}%"
                        f"（底仓市值基数 {base:.0f} 元）")
        return None

    def in_session(self) -> bool:
        now = datetime.now().time()
        for start, end in self.cfg["trade"]["sessions"]:
            if parse_hhmm(start) <= now <= parse_hhmm(end):
                return True
        return False

    def evaluate(self, q: Quote) -> Tuple[List[Signal], Dict[str, List[Check]]]:
        """返回 (触发的信号列表, 各类型检查明细)。"""
        self._roll_day()
        self._push_volume(q)          # 每轮采样一次累计成交量，供量能确认使用
        pos = self.position(q.code)
        out: Dict[str, List[Check]] = {}
        signals: List[Signal] = []

        # 日内亏损熔断：当日已实现亏损触及上限后，正T/反T一律停手，只允许时间止损平仓
        circuit = self._circuit_broken()
        # 买入通道分流：存在反T闭环锁时，本轮买入只可能是「买回」，走独立类型，
        # 不再借用 BUY_FIRST，避免日志/弹窗/记录里把回买误显示成正T
        buy_stype = "REBUY" if self._rebuy.get(short_code(q.code)) else "BUY_FIRST"
        for stype, keywords in ((buy_stype, "buy_first"), ("SELL_FIRST", "sell_first")):
            conf = self._signal_conf(keywords, q.code)
            checks = self._checks(q, pos, conf, stype)
            if circuit:
                checks = checks + [Check(
                    "日内亏损熔断", False,
                    f"{circuit}；当日停止一切新开仓（已持仓仍由时间止损平仓，不受熔断影响）")]
            out[stype] = checks
            if all(c.ok for c in checks):
                signals.append(self._build(q, pos, conf, stype, checks))

        # 时间止损与价格阈值并行判定：批次到点或超期未平，无视盈亏强制平仓
        # 互斥保护：若本轮已产生价格平T卖出信号，则不再追加时间止损卖出，避免同一笔批次被卖两次
        ts_checks = self._time_stop_checks(q)
        if ts_checks is not None:
            already_selling = any(s.side == "sell" for s in signals)
            if already_selling:
                ts_checks = ts_checks + [Check(
                    "与价格信号互斥", False,
                    "本轮已有平T卖出信号，时间止损本次不重复下单（若用户放弃该信号，下一轮由时间止损接管）")]
            out["TIME_STOP"] = ts_checks
            if all(c.ok for c in ts_checks):
                signals.append(self._build_time_stop(q, pos))

        # ---- 同轮方向互斥：一个标的一轮只能出一个方向 ----
        # 旧逻辑买入通道与卖出通道各自独立判定，同一时点能同时产出「低吸买入」与「高抛卖出」，
        # 日志上就表现为「正T、反T都触发」——既想在 0.754 买、又想在 0.755 卖，逻辑上自相矛盾，
        # 实盘还会被券商判成自相对倒。卖出（减仓/风控）优先，被压掉的一侧如实写进检查明细。
        sell_side = [s for s in signals if s.side == "sell"]
        buy_side = [s for s in signals if s.side == "buy"]
        if sell_side and buy_side:
            kept, dropped = sell_side[0], buy_side[0]
            signals = [s for s in signals if s is not dropped]
            out.setdefault(dropped.stype, []).append(Check(
                "与同轮卖出互斥", False,
                f"同一轮已产生「{kept.stype_label}」卖出信号，本标的本轮不再同时买入"
                "（否则同一时点既低吸又高抛，等于自己和自己对倒）"))

        return signals, out

    def _checks(
        self, q: Quote, pos: Dict[str, Any], conf: Dict[str, Any], stype: str
    ) -> List[Check]:
        cfg = self.cfg
        trade = self._trade_conf(q.code)
        conf = self._dyn_conf(q, conf)
        cost = pos["cost"]
        # 底仓可卖余量必须扣掉当日已卖出的股数，否则同一份底仓会被反复卖出（超卖）
        sold_today = float(self._sold_today.get(short_code(q.code), 0.0) or 0.0)
        sellable = max(0.0, pos["sellable_qty"] - sold_today)
        rebuy = self._rebuy.get(short_code(q.code)) or None
        avg = q.avg_price
        dev = q.deviation_from_avg_pct()
        d = q.decimals
        tick = price_tick(d)
        checks: List[Check] = []

        checks.append(Check("信号开关", bool(conf.get("enabled", True)),
                            "已启用" if conf.get("enabled", True) else "配置中已关闭"))

        allow_off = bool(trade.get("allow_offhours_signal", False))
        in_sess = self.in_session()
        checks.append(
            Check("交易时段", in_sess or allow_off,
                  "当前处于交易时段" if in_sess else ("非交易时段但已允许" if allow_off else "非交易时段，不产生信号"))
        )

        checks.append(Check("行情有效", q.prev_close > 0 and avg > 0,
                            f"昨收 {fmt_price(q.prev_close, d)} 均价 {fmt_price(avg, d)}"
                            if q.prev_close > 0 else "昨收/均价异常"))

        # ---- 振幅过滤：当日波动空间不足以覆盖双边成本时，任何T都无利可图 ----
        min_amp = abs(float(trade.get("min_amplitude_pct", 0.0) or 0.0))
        amp = q.amplitude_pct
        amp_ok = (min_amp <= 0.0) or (amp >= min_amp)
        checks.append(Check(
            "当日振幅足够", amp_ok,
            f"振幅 {amp:.2f}%" + (f"，门槛 ≥ {min_amp:.2f}%" if min_amp > 0 else "（未设门槛）")
            + ("" if amp_ok else "，空间不足以覆盖成本，等波动放大再做")
        ))

        if stype == "REBUY":
            # ---- 买回（反T闭环）：补回此前高卖的底仓，不登记新待平批次 ----
            # 与正T分开判定：不看均价线、不看支撑、不受 T+0/批次上限/可卖额度/收盘保护区约束，
            # 唯一门槛是「现价回到卖出价下方 N 跳」，保证这一圈闭环有价差空间。
            checks.append(Check(
                "存在待回买批次", bool(rebuy),
                (f"待回买 {int(rebuy['qty'])} 股，卖出价 {fmt_price(float(rebuy['price']), d)}"
                 f"（{rebuy.get('at', '')} {rebuy.get('sig_id', '')}）") if rebuy
                else "闭环锁已释放（可能已被其它信号回补），本轮不产生买回信号"))
            if rebuy:
                rb_ticks = int(conf.get("rebuy_ticks", 0) or 0) \
                    or int(self.cfg["signals"]["sell_first"].get("target_ticks", 0) or 0) or 1
                rb_line = round_price(max(tick, float(rebuy["price"]) - rb_ticks * tick), d)
                checks.append(Check(
                    "回买价达标（反T闭环）", q.price <= rb_line,
                    f"现价 {fmt_price(q.price, d)} vs 回买线 {fmt_price(rb_line, d)}"
                    f"（卖出价 {fmt_price(float(rebuy['price']), d)} − {rb_ticks} 跳，"
                    f"待回买 {int(rebuy['qty'])} 股）"))
            max_drop = -abs(float(conf.get("max_drop_pct", 3.0)))
            checks.append(Check(
                "跌幅未失控", q.change_pct >= max_drop,
                f"当日 {q.change_pct:+.2f}%，阈值 ≥ {max_drop:.2f}%"))
            # 止损后冷静期同样约束买回：刚止损就原价接回等于白止损
            self._stop_lock_checks(checks, q, d)
        elif stype == "BUY_FIRST":
            # ---- 标的类型：正T依赖「当日买入当日可卖」，股票 T+1 不可做 ----
            allow_t1 = bool(trade.get("allow_stock_t_plus_1", False))
            # 按标的配置判定，不能只看代码前缀：股票ETF（如 159325 半导体ETF）代码同为 15 开头，
            # 但实行 T+1，当日买入当日不可卖，正T无法闭环
            t0 = self._is_t0(q.code, pos)
            # 反T回买不是新开T：回买只是补回此前卖出的底仓，register_fill 不会为它登记待平批次，
            # 不产生「当日买入必须当日卖出」的约束，所以 T+1 标的也允许当日买回。
            rebuy_pass = bool(rebuy)
            checks.append(Check(
                "标的支持T+0", t0 or allow_t1 or rebuy_pass,
                ("反T回买放行：回买补回已卖底仓，不登记新T批次，T+1 标的亦可当日买回"
                 if rebuy_pass and not t0 else
                 "配置为当日买入当日可卖（跨境/债券/黄金/货币ETF），可做正T" if t0
                 else ("标的为T+1（股票或股票ETF），当日买入不可卖；配置已强制放行，正T实际无法闭环，慎用"
                       if allow_t1 else "标的为T+1（股票或股票ETF），当日买入不可卖，正T不成立（改用反T）"))
            ))

            # 阈值同时按百分比与跳数判定，取更严的一方：低价ETF 0.3% 不足 3 跳时以跳数为准
            thr_pct = -abs(float(conf.get("below_avg_pct", 0.3)))
            dev_ticks = int(conf.get("dev_ticks", 0) or 0)
            thr_tick = -(dev_ticks * tick / q.price * 100.0) if (dev_ticks > 0 and q.price > 0) else 0.0
            threshold = min(thr_pct, thr_tick) if thr_tick else thr_pct
            support = self._support_price(q, conf)
            if rebuy:
                # 反T闭环：已高卖待回买时，买入判定改为「回到卖价下方 N 跳」，不再看均价与支撑，
                # 否则高卖之后同一条均价线条件会与回买窗口互斥，回买永远不成立。
                rb_ticks = int(conf.get("rebuy_ticks", 0) or 0) \
                    or int(self.cfg["signals"]["sell_first"].get("target_ticks", 0) or 0) or 1
                rb_line = round_price(max(tick, float(rebuy["price"]) - rb_ticks * tick), d)
                checks.append(
                    Check("回买价达标（反T闭环）", q.price <= rb_line,
                          f"现价 {fmt_price(q.price, d)} vs 回买线 {fmt_price(rb_line, d)}"
                          f"（卖出价 {fmt_price(float(rebuy['price']), d)} − {rb_ticks} 跳，"
                          f"待回买 {int(rebuy['qty'])} 股）")
                )
            else:
                # 偏离分级：温和偏离只是「便宜」，需量能确认；明显超跌才是「错杀」，直接放行
                s_pct = -abs(float(conf.get("strong_dev_pct", 0.0) or 0.0))
                s_ticks = int(conf.get("strong_dev_ticks", 0) or 0)
                s_tick = -(s_ticks * tick / q.price * 100.0) if (s_ticks > 0 and q.price > 0) else 0.0
                # 强档两侧门槛取「更易达到」的一方（绝对值更小），与弱档取更严的方向相反：
                # 弱档要给足空间，强档只要确认「跌得足够狠」，二者任一满足即成立
                s_cands = [x for x in (s_pct, s_tick) if x < 0]
                strong_thr = max(s_cands) if s_cands else 0.0
                is_strong = (strong_thr < 0) and (dev <= strong_thr)
                dev_ok = dev <= threshold
                checks.append(
                    Check("低于分时均价线", dev_ok,
                          f"偏离 {dev:+.2f}%，阈值 ≤ {threshold:.2f}%"
                          + (f"（{dev_ticks} 跳 ≈ {abs(thr_tick):.2f}%，1 跳 = {fmt_price(tick, d)}）"
                             if thr_tick else "（仅按百分比）")
                          + (f"｜已到强档 ≤ {strong_thr:.2f}%，免量能确认" if is_strong
                             else (f"｜弱档，需量能确认（强档线 {strong_thr:.2f}%）"
                                   if strong_thr < 0 else "")))
                )
                # ---- 量能确认：温和偏离必须放量承接，避免无量阴跌一路抄底 ----
                vmode = str(conf.get("volume_confirm", "off") or "off").strip().lower()
                need_vol = (vmode == "always") or (vmode == "weak" and dev_ok and not is_strong)
                if need_vol:
                    vwin = float(conf.get("volume_window_sec", 60) or 60)
                    vbase = float(conf.get("volume_baseline_sec", 300) or 300)
                    vneed = float(conf.get("volume_ratio", 1.2) or 1.2)
                    vdir = str(conf.get("volume_dir", "surge") or "surge").strip().lower()
                    vr = self._volume_ratio(q.code, vwin, vbase)
                    if vr is None:
                        checks.append(Check(
                            "量能确认", True,
                            f"量能样本不足（需 ≥ {int(vwin + vbase)} 秒，刚开盘或刚重启），"
                            "本次跳过量能确认"))
                    elif vdir == "shrink":
                        vline = (1.0 / vneed) if vneed > 0 else 1.0
                        checks.append(Check(
                            "量能确认（缩量）", vr <= vline,
                            f"近 {int(vwin)} 秒量能 {vr:.2f}× 前 {int(vbase)} 秒均速，"
                            f"要求 ≤ {vline:.2f}×（卖压衰竭才低吸）"))
                    else:
                        checks.append(Check(
                            "量能确认（放量）", vr >= vneed,
                            f"近 {int(vwin)} 秒量能 {vr:.2f}× 前 {int(vbase)} 秒均速，"
                            f"要求 ≥ {vneed:.2f}×（有承接盘才低吸）"))
                # 支撑线与买入线上限冲突时以阈值为准：否则「跌破均价 N 跳」与「不破支撑」
                # 可能构成空集，买入信号将永远无法产生。
                buy_line = q.avg_price * (1 + threshold / 100.0) if q.avg_price > 0 else 0.0
                support_conflict = support > 0 and buy_line > 0 and support > buy_line
                if support_conflict:
                    support_desc = (f"现价 {fmt_price(q.price, d)} vs 支撑 {fmt_price(support, d)}"
                                    f" vs 买入线 {fmt_price(buy_line, d)}"
                                    f"（支撑高于买入线，判定冲突，以阈值为准）")
                else:
                    support_desc = (f"现价 {fmt_price(q.price, d)} vs 支撑 {fmt_price(support, d)}"
                                    + ("" if q.price >= support else "（已跌破，放弃低吸）"))
                checks.append(
                    Check("未破当日支撑", (q.price >= support) or support_conflict, support_desc)
                )
            max_drop = -abs(float(conf.get("max_drop_pct", 3.0)))
            checks.append(
                Check("跌幅未失控", q.change_pct >= max_drop,
                      f"当日 {q.change_pct:+.2f}%，阈值 ≥ {max_drop:.2f}%")
            )
            if conf.get("check_cost"):
                tol = abs(float(conf.get("cost_tolerance_pct", 1.0)))
                limit = cost * (1 + tol / 100.0) if cost > 0 else 0
                checks.append(
                    Check("买入价不高于成本容忍", (cost <= 0) or (q.price <= limit),
                          f"现价 {fmt_price(q.price, d)} vs 上限 {fmt_price(limit, d)}"
                          if cost > 0 else "未配置成本，跳过")
                )
            # 正T只允许单批次滚动，未平仓前不再叠加买入，避免越跌越买变成加仓
            max_batches = int(trade.get("max_pending_batches", 1) or 0)
            pending_n = len(self.book.pending(q.code))
            batch_ok = (max_batches <= 0) or (pending_n < max_batches)
            checks.append(
                Check("未平批次数量", batch_ok or bool(rebuy),
                      f"当前待平 {pending_n} 批"
                      + ("（不限批数）" if max_batches <= 0 else f"，上限 {max_batches} 批")
                      + ("，反T回买放行" if rebuy else ("" if batch_ok else "，先平掉再买")))
            )
            need = self._plan_qty(q, "buy")
            # 可卖数量只约束「新开正T」：买入后必须还能卖回去。
            # 反T回买是补回已卖出的底仓，后续不需要再卖出，刚卖掉底仓时可卖余额已被占用，
            # 若同样受此约束，反T回买将永远不成立，底仓缺口只能留到明天。
            checks.append(
                Check("底仓可卖数量足够", bool(rebuy) or (sellable >= need),
                      ("反T回买放行：回买补回已卖底仓，不依赖当日剩余可卖数量"
                       if rebuy else
                       f"可卖 {int(sellable)} 股，正T买入 {int(need)} 股后需能卖回"
                       f"（{self._qty_desc(q, 'buy')}）"))
            )
            # ---- 不留隔夜：收盘前 close_guard_minutes 分钟内不再开新正T ----
            # 正T闭环要求当日买入当日卖回；离开收盘不足 guard 分钟时新买必然留到明天，直接锁死。
            # 反T回买豁免：回买是补回已卖出的底仓，不产生待平批次，拦住它反而会让底仓缺口留过夜。
            if bool(trade.get("no_overnight", True)) and not rebuy:
                guard = int(trade.get("close_guard_minutes", 10) or 0)
                cut = None
                for _s, _e in (trade.get("sessions") or []):
                    try:
                        _end = parse_hhmm(str(_e))
                    except Exception:
                        continue
                    if cut is None or _end > cut:
                        cut = _end
                if guard > 0 and cut is not None:
                    _now = datetime.now().time()
                    left_min = (datetime.combine(date.today(), cut)
                                - datetime.combine(date.today(), _now)).total_seconds() / 60.0
                    left_min = max(0.0, left_min)
                    checks.append(Check(
                        "收盘前不留新T", left_min > guard,
                        f"距收盘 {left_min:.0f} 分钟，门槛 > {guard} 分钟"
                        + ("" if left_min > guard
                           else "：已进入收盘保护区，此时买入来不及卖回，不再开新正T")))
            # ---- 止损后冷静期 + 接回门槛：刚止损就原价抄回，等于白止损并放大日内亏损 ----
            self._stop_lock_checks(checks, q, d)
        else:
            # ---- 反T闭环锁：已高卖未回买前，禁止对同一份底仓重复卖出 ----
            checks.append(
                Check("无待回买批次", rebuy is None,
                      "无待回买，可正常高卖" if rebuy is None else
                      f"已于 {rebuy.get('at', '')} 卖出 {int(rebuy['qty'])} 股@"
                      f"{fmt_price(float(rebuy['price']), d)}，未回买前不再重复卖出")
            )
            # ---- 卖出水位线：反T不许「越卖越低」----
            # 上一圈闭环（0.758 卖 → 0.754 买回）完成后，下一次卖出至少要覆盖回买价 + N 跳；
            # 没有这条，就会退化成「卖 0.755 → 买 0.752 → 卖 0.753」的连续下移倒手，
            # 表面每笔都赚，实际是把底仓越卖越便宜。
            last_sell = self._last_sell.get(short_code(q.code)) or {}
            ref_price = float(last_sell.get("closed_price", 0) or 0) \
                or float(last_sell.get("price", 0) or 0)
            if ref_price > 0:
                step_ticks = max(1, int(conf.get("target_ticks", 0) or 0))
                hold_line = round_price(ref_price + step_ticks * tick, d)
                ref_desc = ("上一圈回买价" if last_sell.get("closed_price")
                            else "上一笔卖出价")
                checks.append(Check(
                    "卖出价高于上一圈水位", q.price >= hold_line,
                    f"现价 {fmt_price(q.price, d)} vs 水位线 {fmt_price(hold_line, d)}"
                    f"（{ref_desc} {fmt_price(ref_price, d)} + {step_ticks} 跳）"
                    + ("" if q.price >= hold_line else
                       "，比上一圈还低，卖出去再接回来没有价差，等于白折腾手续费")))
            # ---- 卖出基准：默认对标当日买入批次，而非历史持仓成本 ----
            basis_price, basis_desc = self._sell_benchmark(q, conf, pos)
            floor_price = self._sell_floor(q, conf, basis_price)
            thr_pct = abs(float(conf.get("above_avg_pct", 0.3)))
            dev_ticks = int(conf.get("dev_ticks", 0) or 0)
            thr_tick = (dev_ticks * tick / q.price * 100.0) if (dev_ticks > 0 and q.price > 0) else 0.0
            threshold = max(thr_pct, thr_tick) if thr_tick else thr_pct
            checks.append(
                Check("高于分时均价线", dev >= threshold,
                      f"偏离 {dev:+.2f}%，阈值 ≥ {threshold:.2f}%"
                      + (f"（{dev_ticks} 跳 ≈ {thr_tick:.2f}%）" if thr_tick else "（仅按百分比）"))
            )
            # ---- 趋势过滤：反T卖出最怕「卖在上行趋势里」，卖完继续涨就成卖飞 ----
            # 两道判定都只做「封锁」，不产生信号：① 现价离均价线过远（加速拉升段）；
            # ② 均价线（VWAP）自身在持续抬升（买盘一直占优）。命中任一即放弃本次高卖。
            # 样本不足时一律跳过，避免重启初期长时间无法卖出。
            trend_guard = str(conf.get("trend_guard", "both") or "off").strip().lower()
            if trend_guard in ("cap", "both"):
                cap_ticks = int(conf.get("trend_cap_ticks", 0) or 0)
                if cap_ticks > 0 and tick > 0 and avg > 0:
                    cur_ticks = (q.price - avg) / tick
                    checks.append(Check(
                        "偏离上限（未加速拉升）", cur_ticks <= cap_ticks,
                        f"现价较均价线高 {cur_ticks:+.1f} 跳，上限 {cap_ticks} 跳"
                        + ("" if cur_ticks <= cap_ticks else
                           "，已进入加速拉升段，此时高卖大概率卖飞，等回落再卖")))
            if trend_guard in ("slope", "both"):
                slope_ticks = float(conf.get("trend_slope_ticks", 0) or 0)
                slope_win = float(conf.get("trend_window_sec", 600) or 600)
                if slope_ticks > 0:
                    slope = self._avg_slope_ticks(q.code, slope_win, tick)
                    if slope is None:
                        checks.append(Check(
                            "均价线未持续抬升", True,
                            f"均价线采样不足（需 ≥ {int(slope_win)} 秒，刚开盘或刚重启），"
                            "本次跳过趋势过滤"))
                    else:
                        checks.append(Check(
                            "均价线未持续抬升", slope <= slope_ticks,
                            f"近 {int(slope_win)} 秒均价线 {slope:+.1f} 跳，"
                            f"上限 {slope_ticks:g} 跳"
                            + ("" if slope <= slope_ticks else
                               "，分时趋势持续走强，此时高卖容易卖飞，暂不卖出")))
            # ---- 量能确认：无量拉高的高卖最容易买不回来 ----
            # 与买入侧对称实现，但方向含义相反：卖出看重「有没有真实换手/承接」。
            # 缩量虚涨（推高但无成交）往往一根大单就砸回去、也可能原地横住，
            # 卖出后既不回落也不续涨，就成了空卖一次，最后还得追高补回。
            # volume_dir=surge（默认）：要求量能放大到 vneed 倍才允许卖；
            # volume_dir=shrink：反向口径，要求缩量才卖（认为放量强趋势易卖飞）。
            # 样本不足一律跳过，避免重启初期长时间无法卖出。
            vmode = str(conf.get("volume_confirm", "off") or "off").strip().lower()
            if vmode in ("always", "on", "both"):
                vwin = float(conf.get("volume_window_sec", 300) or 300)
                vbase = float(conf.get("volume_baseline_sec", 300) or 300)
                vneed = float(conf.get("volume_ratio", 0.8) or 0.8)
                vdir = str(conf.get("volume_dir", "surge") or "surge").strip().lower()
                vr = self._volume_ratio(q.code, vwin, vbase)
                if vr is None:
                    checks.append(Check(
                        "量能确认", True,
                        f"量能样本不足（需 ≥ {int(vwin + vbase)} 秒，刚开盘或刚重启），"
                        "本次跳过量能确认"))
                elif vdir == "shrink":
                    vline = (1.0 / vneed) if vneed > 0 else 1.0
                    checks.append(Check(
                        "量能确认（缩量）", vr <= vline,
                        f"近 {int(vwin)} 秒量能 {vr:.2f}× 前 {int(vbase)} 秒均速，"
                        f"要求 ≤ {vline:.2f}×（缩量虚涨才高卖）"))
                else:
                    checks.append(Check(
                        "量能确认（放量）", vr >= vneed,
                        f"近 {int(vwin)} 秒量能 {vr:.2f}× 前 {int(vbase)} 秒均速，"
                        f"要求 ≥ {vneed:.2f}×（有换手承接才高卖）"))
            checks.append(
                Check("现价高于平T地板价", basis_price > 0 and q.price >= floor_price,
                      f"现价 {fmt_price(q.price, d)} vs 地板价 {fmt_price(floor_price, d)}"
                      f"（基准 {fmt_price(basis_price, d)}：{basis_desc}"
                      f"，+{int(conf.get('target_ticks', 0) or 0)} 跳 + 双边佣金）"
                      if basis_price > 0 else "无法确定卖出基准（既无批次也无成本）")
            )
            if conf.get("require_cost_profit"):
                need_profit = abs(float(conf.get("min_profit_over_cost_pct", 0.5)))
                floor_cost = cost * (1 + need_profit / 100.0) if cost > 0 else 0
                checks.append(
                    Check("高于历史成本上浮", cost > 0 and q.price >= floor_cost,
                          f"现价 {fmt_price(q.price, d)} vs 成本上浮线 {fmt_price(floor_cost, d)}"
                          f"（成本 {fmt_price(cost, d)} 上浮 {need_profit:.2f}%）"
                          if cost > 0 else "未配置持仓成本，跳过历史成本校验")
                )
            need = self._plan_qty(q, "sell")
            checks.append(
                Check("可卖底仓足够", sellable >= need,
                      f"可卖 {int(sellable)} 股"
                      + (f"（底仓 {int(pos['sellable_qty'])} 已卖 {int(sold_today)}）" if sold_today > 0 else "")
                      + f"，需卖 {int(need)} 股（{self._qty_desc(q, 'sell')}）")
            )

            # ---- P3 卖出可行性：目标利润必须落在该标的今日可达波动之内 ----
            # 卖飞是反T最大的隐性成本：卖点达标但标的当日波动不足以回落到回买线，
            # 就只能追高补回或者留缺口过夜。这里用实时 ATR 做一次可行性闸门。
            if bool(conf.get("require_atr_feasible", False)):
                dyn_cfg = trade.get("dynamic_ticks") if isinstance(trade.get("dynamic_ticks"), dict) else {}
                atr = self._atr_pct(
                    q.code,
                    float(dyn_cfg.get("window_sec", 1800) or 1800),
                    float(dyn_cfg.get("hold_window_sec", 1200) or 1200),
                )
                # 关键：ATR 是「持仓周期内的极差」，只与「卖出后要回落多少才能回买」
                # 同一量纲。不能把卖出跳数也加进去——卖点由实际价格触发，不需要
                # 波动去"挣"，需要波动去"回落"。早期把 target+rebuy 相加再乘 1.5，
                # 会要求 ATR 达 1.4% 以上，等于把反T全部锁死。
                tgt_ticks = int(conf.get("target_ticks", 2) or 2)
                rb_ticks = int(conf.get("rebuy_ticks", tgt_ticks) or tgt_ticks)
                need_pct = (rb_ticks * tick / q.price * 100.0) if q.price > 0 else 0.0
                ratio = abs(float(conf.get("atr_feasible_ratio", 1.5) or 1.5))
                if atr is None:
                    checks.append(Check("卖出可行性（ATR）", True,
                                        "ATR 样本不足（刚开盘或刚重启），本次跳过可行性校验"))
                else:
                    ok_atr = atr >= need_pct * ratio
                    checks.append(Check(
                        "卖出可行性（ATR）", ok_atr,
                        f"当日 ATR {atr:.2f}%，卖出 {tgt_ticks} 跳后需回落 {rb_ticks} 跳"
                        f"（{need_pct:.2f}%）才够回买，要求 ATR ≥ {need_pct * ratio:.2f}%"
                        + ("" if ok_atr else "；波动不足，卖出后大概率回不到回买线，本次丢弃该信号")))

            # ---- P4 反T卖出独立日上限：与全体信号上限解耦 ----
            # max_signals_per_day 是买+卖合计额度，卖出单独失控时会先把额度吃光，
            # 再叠加重启/清空状态就会重复高卖同一份底仓。这里单独设闸。
            sell_cap = int(conf.get("max_sell_per_day", trade.get("max_sell_per_day", 0)) or 0)
            if sell_cap > 0:
                used_sell = int(self._count.get((short_code(q.code), "SELL_FIRST"), 0) or 0)
                checks.append(Check(
                    "反T卖出次数上限", used_sell < sell_cap,
                    f"今日已卖 {used_sell} 次，上限 {sell_cap} 次"
                    + ("" if used_sell < sell_cap else "，今日不再高卖，等明日重置")))

        cooldown = int(trade["cooldown_sec"])
        last = self._last_trigger.get((short_code(q.code), stype))
        if last is None:
            checks.append(Check("信号冷却", True, f"无历史触发（冷却 {cooldown} 秒）"))
        else:
            gap = time.time() - last
            checks.append(
                Check("信号冷却", gap >= cooldown,
                      f"距上次触发 {int(gap)} 秒" + ("" if gap >= cooldown else f"，冷却中（还需 {int(cooldown - gap)} 秒）"))
            )

        limit = int(trade["max_signals_per_day"])
        used = self._count.get((short_code(q.code), stype), 0)
        checks.append(Check("当日次数上限", used < limit, f"已触发 {used} 次，上限 {limit} 次"))
        return checks

    def _stop_lock_checks(self, checks: List[Check], q: Quote, d: int) -> None:
        """止损后冷静期与接回门槛：正T与买回两条买入通道共用。"""
        lock = self._stop_lock.get(short_code(q.code))
        if not lock:
            return
        ts_conf = self._time_stop_conf(q.code)
        cd = int(ts_conf.get("cooldown_after_stop_sec", 0) or 0)
        gap_pct = abs(float(ts_conf.get("reentry_gap_pct", 0) or 0))
        elapsed = time.time() - float(lock.get("at", 0) or 0)
        stopped = float(lock.get("price", 0) or 0)
        cd_ok = elapsed >= cd
        checks.append(Check(
            "止损后冷静期已过", cd_ok,
            f"上次止损于 {lock.get('at_text', '')}@{fmt_price(stopped, d)}"
            f"（{lock.get('stype', '')}），已过 {int(elapsed)} 秒，冷静期 {cd} 秒"
            + ("" if cd_ok else f"，还需 {int(cd - elapsed)} 秒才允许接回")))
        if gap_pct > 0 and stopped > 0:
            reentry_line = round_price(stopped * (1 - gap_pct / 100.0), d)
            checks.append(Check(
                "止损后接回价达标", q.price <= reentry_line,
                f"现价 {fmt_price(q.price, d)} vs 接回线 {fmt_price(reentry_line, d)}"
                f"（止损价 {fmt_price(stopped, d)} 下方 {gap_pct:.2f}%）"
                + ("" if q.price <= reentry_line else "，跌幅不足，接回没有价差空间")))

    # ---- 数量规划：低价ETF固定100份时，跳差收益盖不住最低佣金 ----
    def _plan_qty(self, q: Quote, side: str) -> float:
        """按 trade.qty_mode 规划本次下单数量（100 的整数倍）。

        fixed  ：直接用 buy_qty / sell_qty；
        notional：按 min_notional（最低成交额）反推手数，且不低于 buy_qty/sell_qty，
                  并受 max_notional（单笔成交额上限，0=不限）约束。
        """
        trade = self.cfg["trade"]
        base = float(trade.get("buy_qty" if side == "buy" else "sell_qty", 100) or 100)
        base = max(100.0, round(base / 100.0) * 100.0)
        mode = str(trade.get("qty_mode", "fixed") or "fixed").lower()
        if mode not in ("notional", "amount"):
            return base
        price = float(q.price or 0)
        min_notional = float(trade.get("min_notional", 0) or 0)
        max_notional = float(trade.get("max_notional", 0) or 0)
        if price <= 0 or min_notional <= 0:
            return base
        lots = int(math.ceil(min_notional / (price * 100.0)))
        qty = max(base, lots * 100.0)
        if max_notional > 0:
            cap_lots = int(math.floor(max_notional / (price * 100.0)))
            if cap_lots >= 1:
                qty = min(qty, cap_lots * 100.0)
        return float(int(qty))

    def _qty_desc(self, q: Quote, side: str) -> str:
        trade = self.cfg["trade"]
        if str(trade.get("qty_mode", "fixed") or "fixed").lower() in ("notional", "amount"):
            return (f"按最低成交额 {float(trade.get('min_notional', 0) or 0):.0f} 元定手数，"
                    f"约 {float(self._plan_qty(q, side)) * q.price:.0f} 元")
        return "固定股数"

    # ---- 卖出基准：批次成本优先，避免拿历史成本当平T目标 ----
    def _sell_benchmark(
        self, q: Quote, conf: Dict[str, Any], pos: Dict[str, Any]
    ) -> Tuple[float, str]:
        """返回 (卖出基准价, 基准来源说明)。

        basis=batch（默认）：优先用当日买入批次加权成本；
        basis=day_avg：用当日分时均价；basis=cost：用历史持仓成本。
        批次不存在时逐级退回，并在说明里写明，便于在信号弹窗里核对依据。
        """
        basis = str(conf.get("basis", "batch") or "batch").lower()
        pending = self.book.pending_qty(q.code)
        if basis == "batch" and pending > 0:
            return self.book.weighted_cost(q.code), f"当日买入批次加权成本·待平{int(pending)}股"
        if basis == "day_avg" and q.avg_price > 0:
            return float(q.avg_price), "当日分时均价"
        # basis=batch 但当天还没有买入批次，说明这一轮是「无底仓平T目标」的反T（高卖底仓、低买回），
        # 此时不能用历史成本当基准：深套标的成本远高于现价，用成本当基准会让卖出侧永远不触发、反T彻底失效。
        # 反T的盈亏只取决于「卖价 > 回买价」，因此基准取当日分时均价。
        if basis == "batch" and q.avg_price > 0:
            return float(q.avg_price), "无待平批次·反T用当日分时均价作高卖基准"
        if pos["cost"] > 0:
            note = "历史持仓成本" if basis == "cost" else "无待平批次，退回历史成本"
            return float(pos["cost"]), note
        if q.avg_price > 0:
            return float(q.avg_price), "无成本配置，退回当日分时均价"
        return float(q.price), "无可用基准，退回现价"

    def _sell_floor(self, q: Quote, conf: Dict[str, Any], basis_price: float) -> float:
        """平T地板价 = 基准 + N 跳 + 双边佣金摊薄，向上取整到 tick。

        取「跳数口径」与「百分比口径」的较大者：低价 ETF 用跳数才有意义，
        高价标的上限由百分比兜底。
        """
        if basis_price <= 0:
            return 0.0
        d = q.decimals
        tick = price_tick(d)
        ticks = int(conf.get("target_ticks", 2) or 0)
        pct = abs(float(conf.get("target_pct", 0.0) or 0.0))
        qty = max(100.0, self._plan_qty(q, "sell"))
        amount = basis_price * qty
        buy_fee = fee_of(self.cfg, q.code, amount, "buy")
        sell_fee = fee_of(self.cfg, q.code, amount, "sell")
        fee_per_share = (buy_fee + sell_fee) / qty if qty > 0 else 0.0
        floor_ticks = basis_price + ticks * tick + fee_per_share
        floor_pct = basis_price * (1 + pct / 100.0)
        return round_price(ceil_to_tick(max(floor_ticks, floor_pct), tick), d)

    # ---- 时间止损：拆成「时间层」与「价格层」两层，各自独立开关 ----
    def _time_stop_conf(self, code: str = "") -> Dict[str, Any]:
        """归一化时间止损配置。

        时间层（time_layer）：到 deadline 或持有超 max_hold_minutes 强制平仓，管的是「不留隔夜」；
        价格层（price_layer）：浮亏达 loss_pct 提前止损，管的是「单笔亏损封顶」。
        这两件事逻辑不同，混在一起会导致「刚买 27 秒就因 -0.4% 被扫」与「到点强平」互相干扰。
        旧配置（deadline/max_hold_minutes/loss_pct 平铺）自动映射到对应层，保证向后兼容。
        """
        conf = self._trade_conf(code).get("time_stop") or {}
        if not isinstance(conf, dict):
            conf = {}
        tl = conf.get("time_layer") if isinstance(conf.get("time_layer"), dict) else {}
        pl = conf.get("price_layer") if isinstance(conf.get("price_layer"), dict) else {}
        out = dict(conf)
        out["time_layer"] = {
            "enabled": bool(tl.get("enabled", True)),
            "deadline": str(tl.get("deadline", conf.get("deadline", "14:50")) or "14:50"),
            "max_hold_minutes": float(
                tl.get("max_hold_minutes", conf.get("max_hold_minutes", 0) or 0) or 0),
            "trigger_on_deadline": bool(
                tl.get("trigger_on_deadline", conf.get("trigger_on_deadline", True))),
        }
        out["price_layer"] = {
            "enabled": bool(pl.get("enabled", True)),
            "loss_pct": abs(float(pl.get("loss_pct", conf.get("loss_pct", 0) or 0) or 0)),
        }
        out["min_stop_distance"] = bool(conf.get("min_stop_distance", True))
        out["cooldown_after_stop_sec"] = int(conf.get("cooldown_after_stop_sec", 600) or 0)
        out["reentry_gap_pct"] = abs(float(conf.get("reentry_gap_pct", 0.5) or 0))
        return out

    def _time_stop_checks(self, q: Quote) -> Optional[List[Check]]:
        """无待平批次返回 None（该标的当天没做T，不参与判定）。"""
        conf = self._time_stop_conf(q.code)
        if not conf.get("enabled", False):
            return None
        bs = self.book.pending(q.code)
        if not bs:
            return None
        d = q.decimals
        tick = price_tick(d)
        short = short_code(q.code)
        pos = self.position(q.code)
        tl = conf.get("time_layer") or {}
        pl = conf.get("price_layer") or {}
        now_t = datetime.now().time()
        deadline_text = str(tl.get("deadline", "14:50"))
        deadline = parse_hhmm(deadline_text)
        max_hold = float(tl.get("max_hold_minutes", 0) or 0)
        hold = self.book.hold_minutes(q.code)
        cost_price = self.book.weighted_cost(q.code)
        pnl_pct = (q.price / cost_price - 1) * 100.0 if cost_price > 0 else 0.0

        # ---- 止损距离必须不小于目标距离 ----
        # 目标地板价 = 基准 + max(target_ticks*tick, target_pct)；若止损比目标还近，
        # 等于「止盈没走到、止损先到」，胜率要求会被抬到 70% 以上，数学上必亏。
        sell_conf = self.cfg["signals"].get("sell_first") or {}
        target_ticks = int(sell_conf.get("target_ticks", 0) or 0)
        target_pct = abs(float(sell_conf.get("target_pct", 0) or 0))
        dist_by_tick = ((target_ticks * tick / cost_price) * 100.0
                        if (target_ticks > 0 and cost_price > 0) else 0.0)
        target_dist = max(dist_by_tick, target_pct)
        raw_loss = abs(float(pl.get("loss_pct", 0) or 0))
        loss_pct_eff = max(raw_loss, target_dist) if conf.get("min_stop_distance", True) else raw_loss
        loss_limit = -loss_pct_eff

        reasons: List[str] = []
        if tl.get("enabled", True):
            if bool(tl.get("trigger_on_deadline", True)) and now_t >= deadline:
                reasons.append(f"〔时间层〕已到强制平仓时点 {deadline_text}")
            if max_hold > 0 and hold >= max_hold:
                reasons.append(f"〔时间层〕持有 {hold:.0f} 分钟 ≥ 上限 {max_hold:.0f} 分钟")
        if pl.get("enabled", True) and loss_limit < 0 and pnl_pct <= loss_limit:
            reasons.append(f"〔价格层〕浮亏 {pnl_pct:+.2f}% 触及 {loss_limit:.2f}%")

        # ---- 可卖余额截断：T+1 标的早盘买进的份额当日不可卖，止损单发出去只会是废单 ----
        sold_today = float(self._sold_today.get(short, 0.0) or 0.0)
        sellable = max(0.0, float(pos.get("sellable_qty", 0) or 0) - sold_today)
        pending_ord = float(self.book.pending_qty(q.code))
        avail = min(pending_ord, sellable)
        avail = float(int(avail // 100) * 100) if avail >= 100 else 0.0
        checks = [
            Check("时间止损开关", True,
                  f"时间层{'开' if tl.get('enabled', True) else '关'}"
                  f"（{deadline_text}，持有上限 {max_hold:.0f} 分钟），"
                  f"价格层{'开' if pl.get('enabled', True) else '关'}"
                  f"（浮亏 {loss_pct_eff:.2f}%）"),
            Check("存在待平批次", True, self.book.snapshot(q.code)),
            Check("可卖余额足够", avail >= 100,
                  f"待平 {int(pending_ord)} 股，可卖 {int(sellable)} 股"
                  f"（底仓 {int(pos.get('sellable_qty', 0) or 0)} 已卖 {int(sold_today)}），"
                  f"本次可平 {int(avail)} 股"
                  + ("" if avail >= 100 else "：当日买入份额不可卖，发单必成废单，本次不平仓")),
            Check("止损距离不小于目标距离", loss_pct_eff >= target_dist - 1e-9,
                  f"止损 {loss_pct_eff:.2f}% vs 目标距离 {target_dist:.2f}%"
                  f"（{target_ticks} 跳 ≈ {dist_by_tick:.2f}%，{target_pct:.2f}%）"
                  + ("" if loss_pct_eff >= target_dist - 1e-9
                     else "：止损比止盈还近，长期必亏，已按目标距离抬高")),
            Check("触发条件", bool(reasons),
                  "；".join(reasons) if reasons
                  else f"未触发（持有 {hold:.0f} 分钟，浮动 {pnl_pct:+.2f}%）"),
        ]
        if not reasons:
            return checks

        fingerprint = f"{bs[0].opened_at:.0f}-{sum(b.qty for b in bs):.0f}"
        fired = self._time_stop_fired.get(short_code(q.code))
        checks.append(
            Check("重复提示抑制", fired != fingerprint,
                  "本次为该批次首次提示" if fired != fingerprint
                  else "该批次止损提示已发过，等待成交或撤单后再判定")
        )
        if fired != fingerprint:
            self._time_stop_fired[short_code(q.code)] = fingerprint
        return checks

    def _build_time_stop(self, q: Quote, pos: Dict[str, Any]) -> Signal:
        conf = dict(self.cfg["signals"]["sell_first"])
        checks = [Check("时间止损", True, self.book.snapshot(q.code))]
        return self._build(q, pos, conf, "TIME_STOP", checks)

    # ---- 成交回报：登记/核销批次台账 ----
    def register_fill(self, sig: Signal) -> str:
        """下单（含演练）后更新批次台账，返回可写入日志的台账变更说明。

        记账原则：同一笔买入不能既当「反T回买」又当「待平批次」。
        买入数量优先回补 _rebuy 闭环锁，回补部分只解除锁、不再登记待平批次；
        超出闭环锁的余额才登记为待平批次，佣金按股数比例分摊到两部分。
        买卖双向都累计当日净持仓变化 _net_pos，让「台账已空但底仓实际已变」在状态与日志中可见。
        """
        short = short_code(sig.code)
        if sig.side == "buy":
            qty = float(sig.suggested_qty)
            price = float(sig.suggested_price)
            fee = fee_of(self.cfg, sig.code, price * qty, "buy")
            rebuy = self._rebuy.get(short)
            parts: List[str] = []
            filled = 0.0
            if rebuy and qty > 0:
                lock_qty = float(rebuy.get("qty", 0.0) or 0.0)
                filled = max(0.0, min(lock_qty, qty))
                if filled > 0:
                    rest = lock_qty - filled
                    sold_at = fmt_price(float(rebuy.get("price", 0.0) or 0.0), sig.decimals)
                    if rest > 1e-9:
                        rebuy["qty"] = rest
                        parts.append(f"反T部分回买 {int(filled)} 股（卖出价 {sold_at}），"
                                     f"闭环锁剩余 {int(rest)} 股待回买")
                    else:
                        self._rebuy.pop(short, None)
                        parts.append(f"反T闭环完成，回买 {int(filled)} 股（卖出价 {sold_at}）")
                    # 回买补回的份额解除底仓占用，当日可再次卖出
                    self._sold_today[short] = max(
                        0.0, float(self._sold_today.get(short, 0.0) or 0.0) - filled)
                    # 闭环完成即记录回买价：下一笔反T卖出必须高于「回买价 + N 跳」，
                    # 否则同一份底仓会在下跌里一路下移倒手，越卖越便宜
                    if rest <= 1e-9:
                        self._last_sell[short] = {
                            **dict(self._last_sell.get(short) or {}),
                            "closed_price": price,
                            "closed_at": now_str(),
                            "closed_sig_id": sig.signal_id,
                        }
                    # 回买成交即重置反T冷却：否则刚买回 1 秒就又能高卖，形成高频倒手
                    self._last_trigger[(short, "SELL_FIRST")] = time.time()
            open_qty = max(0.0, qty - filled)
            if open_qty > 1e-9:
                open_fee = fee * (open_qty / qty) if qty > 0 else 0.0
                self.book.add(sig.code, price, open_qty, fee=open_fee, signal_id=sig.signal_id)
                parts.append(f"登记待平批次 {int(open_qty)}股@{fmt_price(price, sig.decimals)}"
                             f"（佣金约 {open_fee:.2f} 元）")
            elif filled > 0:
                parts.append(f"该笔全部用于回补闭环锁，不登记待平批次"
                             f"（佣金约 {fee:.2f} 元计入成本）")
            self._net_pos[short] = float(self._net_pos.get(short, 0.0) or 0.0) + qty
            self.save_state()
            return (f"{sig.code} " + "；".join(parts or [f"买入 {int(qty)} 股未产生台账变更"])
                    + f"；待平 {int(self.book.pending_qty(sig.code))} 股"
                    + self._net_pos_tag(short))
        # 底仓余量递减：卖出即视为占用，避免同一份底仓当日被反复卖出（回买成交时在买入分支释放）
        self._sold_today[short] = float(self._sold_today.get(short, 0.0)) + float(sig.suggested_qty)
        # 净持仓同步递减：时间止损等平仓不登记待回买，底仓缺口只能靠这里暴露
        self._net_pos[short] = float(self._net_pos.get(short, 0.0) or 0.0) - float(sig.suggested_qty)
        sold_price = float(sig.suggested_price or sig.price)
        if sig.stype == "SELL_FIRST":
            # 反T闭环：高卖后登记待回买批次，未回买前不再对同一标的出卖出信号
            self._rebuy[short] = {
                "qty": float(sig.suggested_qty),
                "price": sold_price,
                "at": sig.triggered_at,
                "sig_id": sig.signal_id,
            }
            # 卖出水位：记录本笔成交价，闭环完成后再补 closed_price，
            # 用于拦住「越卖越低」的连续倒手（只在反T卖出时更新，时间止损不参与）
            self._last_sell[short] = {
                "price": sold_price,
                "at": sig.triggered_at,
                "sig_id": sig.signal_id,
            }
        used = self.book.consume(sig.code, sig.suggested_qty)
        tail = (f"已登记待回买 {int(sig.suggested_qty)} 股@{fmt_price(sold_price, sig.decimals)}，"
                f"未回买前不再重复卖出"
                if sig.stype == "SELL_FIRST" else "未登记待回买（时间止损平仓不参与反T闭环）")
        if not used:
            self.save_state()
            return (f"卖出已执行但无买入批次可核销（{sig.code}），台账保持为空；{tail}"
                    f"{self._net_pos_tag(short)}")
        qty = sum(b.qty for b in used)
        avg = sum(b.price * b.qty for b in used) / max(1.0, qty)
        # ---- 当日已实现盈亏结算（日内亏损熔断的唯一数据源）----
        # 毛利 =（卖出价 - 批次成本）× 股数，再扣买入侧已分摊佣金与本次卖出佣金，避免熔断被手续费掩盖
        buy_fee = sum(float(getattr(b, "fee", 0.0) or 0.0) for b in used)
        sell_fee = fee_of(self.cfg, sig.code, sold_price * qty, "sell")
        realized = (sold_price - avg) * qty - buy_fee - sell_fee
        self._realized[short] = float(self._realized.get(short, 0.0) or 0.0) + realized
        if sig.stype == "TIME_STOP":
            self._time_stop_fired.pop(short, None)
            # 止损后冷静期锁：冷静期内禁止新正T；冷静期满后仍需低于止损价 reentry_gap_pct 才允许接回
            self._stop_lock[short] = {
                "at": time.time(),
                "at_text": datetime.now().strftime("%H:%M:%S"),
                "price": float(sold_price),
                "qty": float(qty),
                "stype": sig.stype,
            }
        self.save_state()
        return (f"核销买入批次 {sig.code} {int(qty)}股@"
                f"{fmt_price(avg, sig.decimals)}，剩余待平 "
                f"{int(self.book.pending_qty(sig.code))} 股；{tail}"
                f"{self._realized_tag(short, realized)}"
                f"{self._net_pos_tag(short)}")

    def _net_pos_tag(self, short: str) -> str:
        """当日净持仓变化提示：台账已清空但底仓实际已变动时，必须让缺口可见。

        缺口来源：时间止损/正T平仓卖出不登记待回买，卖出后 book 与 _rebuy 均可能为空，
        此时「待平 0 股、待回买 0 股」会让人误判底仓未动，实际持仓已比期初少。
        """
        net = float(self._net_pos.get(short, 0.0) or 0.0)
        pending = self.book.pending_qty(short)
        lock = float((self._rebuy.get(short) or {}).get("qty", 0.0) or 0.0)
        if abs(net) < 1e-9:
            return ""
        if net > 0:
            return f"；当日净持仓 +{int(net)} 股（底仓较期初增加）"
        miss = -net
        covered = lock + pending
        head = (f"；当日净持仓 {int(net)} 股（底仓较期初净减 {int(miss)} 股），"
                f"待平 {int(pending)} 股、待回买 {int(lock)} 股")
        if miss - covered > 1e-9:
            head += f"，其中 {int(miss - covered)} 股无台账覆盖（平仓卖出所致），需人工核对实际持仓"
        return head

    def _realized_tag(self, short: str, delta: float) -> str:
        """当日已实现盈亏与熔断状态提示：每笔平仓都要让用户看到累计亏损到哪了。"""
        total = self._realized_total()
        tag = f"；本笔已实现 {delta:+.2f} 元，当日累计 {total:+.2f} 元"
        reason = self._circuit_broken()
        if reason:
            tag += f"；日内亏损熔断已触发（{reason}），当日不再开新仓，仅保留平仓"
        return tag

    @staticmethod
    def _support_price(q: Quote, conf: Dict[str, Any]) -> float:
        manual = float(conf.get("support_price", 0) or 0)
        if manual > 0:
            return manual
        return q.prev_close * (1 - abs(float(conf.get("support_break_pct", 1.5))) / 100.0)

    def _build(
        self, q: Quote, pos: Dict[str, Any], conf: Dict[str, Any], stype: str, checks: List[Check]
    ) -> Signal:
        short = short_code(q.code)
        key = (short, stype)
        self._count[key] = self._count.get(key, 0) + 1
        self._last_trigger[key] = time.time()
        seq = self._count[key]
        self.save_state()   # 冷却与当日计数同步落盘，重启后仍受限
        # 信号编号前缀区分类型：B=正T买入 / R=买回 / S=反T卖出 / T=时间止损
        pfx = {"BUY_FIRST": "B", "REBUY": "R", "TIME_STOP": "T"}.get(stype, "S")
        sig_id = f"T{datetime.now():%Y%m%d}-{short}-{pfx}{seq:02d}-{uuid.uuid4().hex[:4].upper()}"

        cost = pos["cost"]
        # 价格精度：按标的自适应小数位与 tick（股票 0.01，ETF/基金 0.001）
        decimals = resolve_price_decimals(self.cfg, q.code, q.price)
        tick = price_tick(decimals)
        offset = int(self.cfg["order_ui"].get("price_offset_ticks", 0) or 0)
        base = q.price
        pending_qty = self.book.pending_qty(q.code)
        hold_minutes = self.book.hold_minutes(q.code)
        benchmark = 0.0
        benchmark_desc = ""

        if stype == "BUY_FIRST":
            qty = self._plan_qty(q, "buy")
            price = round_price(base - offset * tick, decimals)
            benchmark = float(q.avg_price)
            benchmark_desc = "分时均价线（低吸参考）"
        elif stype == "REBUY":
            # 回买数量严格等于闭环锁里的卖出数量（向下取整到 100 股）：
            # 多买会把底仓越做越多，少买则闭环锁核销不干净，留下残锁挡住后续反T
            rb = self._rebuy.get(short) or {}
            lock_qty = float(rb.get("qty", 0.0) or 0.0)
            qty = float(int(lock_qty // 100) * 100) if lock_qty >= 100 else 0.0
            price = round_price(base - offset * tick, decimals)
            benchmark = float(rb.get("price", 0.0) or 0.0)
            benchmark_desc = "反T闭环基准（此前卖出价）"
        elif stype == "TIME_STOP":
            # 平仓数量 = min(当日待平批次, 可卖余额)，并向下取整到 100 股：
            # T+1 标的当日买入的份额当日不可卖，只按 pending_qty 下单会直接变成废单
            left_sellable = max(0.0, pos["sellable_qty"]
                                - float(self._sold_today.get(short, 0.0) or 0.0))
            raw_qty = min(pending_qty, left_sellable)
            qty = float(int(raw_qty // 100) * 100) if raw_qty >= 100 else 0.0
            # 强制平仓要让价：卖出下压至少 1 跳，确保对手盘能吃到
            price = round_price(max(tick, base - max(1, offset + 1) * tick), decimals)
            benchmark = self.book.weighted_cost(q.code)
            benchmark_desc = "待平批次加权成本（时间止损平仓）"
        else:
            benchmark, benchmark_desc = self._sell_benchmark(q, conf, pos)
            left = max(0.0, pos["sellable_qty"]
                       - float(self._sold_today.get(short, 0.0) or 0.0))
            qty = min(self._plan_qty(q, "sell"), left)
            # 按批次平仓时卖出量不得超过当日待平数量，否则会把底仓当T仓卖飞
            if str(conf.get("basis", "batch") or "batch").lower() == "batch" and pending_qty > 0:
                qty = min(qty, pending_qty)
            price = round_price(base + offset * tick, decimals)

        return Signal(
            signal_id=sig_id,
            code=short,
            name=q.name or pos["name"],
            stype=stype,
            triggered_at=now_str(),
            price=q.price,
            avg_price=round(q.avg_price, 3),
            prev_close=q.prev_close,
            day_open=q.open,
            high=q.high,
            low=q.low,
            volume=q.volume,
            amount=q.amount,
            amplitude_pct=round(q.amplitude_pct, 3),
            change_pct=round(q.change_pct, 3),
            cost=cost,
            pnl_pct=round(q.pnl_pct(cost), 3),
            deviation_pct=round(q.deviation_from_avg_pct(), 3),
            support_price=round_price(self._support_price(q, conf), decimals),
            quote_time=f"{q.quote_date} {q.quote_time}",
            bid_ask=q.bid_ask_text(),
            reasons=[c.render() for c in checks if c.ok] + (
                [f"{'回买' if stype == 'REBUY' else '卖出'}基准："
                 f"{fmt_price(benchmark, decimals)}（{benchmark_desc}）"]
                if benchmark_desc else []
            ) + [
                f"阈值配置：{json.dumps(conf, ensure_ascii=False)}"
            ],
            suggested_price=price,
            suggested_qty=qty,
            decimals=decimals,
            benchmark_price=round_price(benchmark, decimals) if benchmark > 0 else 0.0,
            benchmark_desc=benchmark_desc,
            batch_qty=pending_qty,
            hold_minutes=round(hold_minutes, 1),
        )

    def make_test_signal(self, q: Quote, stype: str = "BUY_FIRST") -> Signal:
        """自检用：绕过阈值强行构造一条信号。"""
        conf = self.cfg["signals"]["buy_first" if stype == "BUY_FIRST" else "sell_first"]
        pos = self.position(q.code)
        checks = [
            Check("自检模式", True, "SELFTEST 注入，未做真实阈值判定"),
            Check("行情来源", True, q.summary()),
        ]
        return self._build(q, pos, conf, stype, checks)


# --------------------------------------------------------------------------------------
# 同花顺窗口监控
# --------------------------------------------------------------------------------------
try:
    import win32api
    import win32con
    import win32gui
    import win32process
except ImportError:  # pragma: no cover
    win32api = win32con = win32gui = win32process = None  # type: ignore

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None  # type: ignore


@dataclass
class WindowState:
    process_running: bool = False
    pids: List[int] = field(default_factory=list)
    exe_ok: bool = False
    window_found: bool = False
    hwnd: int = 0
    title: str = ""
    foreground: bool = False
    detail: str = ""
    checked_at: str = ""

    def summary(self) -> str:
        proc = f"进程{'在' if self.process_running else '缺失'}"
        win = f"窗口{'已找到' if self.window_found else '未找到'}"
        front = "前台" if self.foreground else "非前台"
        state = "正常" if (self.process_running and self.window_found) else "异常"
        return f"[{state}] {proc} | {win} | {front} | {self.detail}"


class WindowMonitor:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg["window"]
        self.process_name = self.cfg.get("process_name", "happ.exe").lower()
        self.process_exe = str(self.cfg.get("process_exe", "")).lower()
        self.keywords = [str(k) for k in self.cfg.get("title_keywords", ["同花顺", "远航版"])]
        # 排除本程序自身的窗口，避免自己的界面标题被误判为同花顺主窗口
        self.exclude_pids = {os.getpid()}
        self.last_state: Optional[WindowState] = None

    # ---- 进程 ----
    def _find_processes(self) -> Tuple[List[int], bool, str]:
        if psutil is None:
            return [], False, "缺少 psutil，无法检测进程（pip install psutil）"
        pids: List[int] = []
        exe_ok = False
        note = ""
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() != self.process_name:
                    continue
                pids.append(proc.info["pid"])
                if self.process_exe:
                    try:
                        real = (proc.exe() or "").lower()
                        if real and real == self.process_exe:
                            exe_ok = True
                        elif real:
                            note = f"进程路径为 {real}"
                    except Exception:
                        pass
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if pids and self.process_exe and not exe_ok and not note:
            note = "进程存在但未能读取路径（可能权限不足）"
        return pids, exe_ok, note

    # ---- 窗口 ----
    def _enum_windows(self) -> List[Tuple[int, int, str, int]]:
        items: List[Tuple[int, int, str, int]] = []
        if win32gui is None:
            return items

        def callback(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd)
                if not title:
                    return True
                ex = win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)
                if ex & win32con.WS_EX_TOOLWINDOW:
                    return True
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid in self.exclude_pids:
                    return True
                left, top, right, bottom = win32gui.GetWindowRect(hwnd)
                area = max(0, right - left) * max(0, bottom - top)
                items.append((hwnd, pid, title, area))
            except Exception:
                pass
            return True

        win32gui.EnumWindows(callback, None)
        return items

    def check(self) -> WindowState:
        state = WindowState(checked_at=now_str())
        if win32gui is None:
            state.detail = "缺少 pywin32，无法检测窗口（pip install pywin32）"
            self.last_state = state
            return state

        pids, exe_ok, note = self._find_processes()
        state.process_running = bool(pids)
        state.pids = pids
        state.exe_ok = exe_ok

        windows = self._enum_windows()
        candidates = []
        for hwnd, pid, title, area in windows:
            title_hit = any(k.lower() in title.lower() for k in self.keywords)
            pid_hit = pid in pids
            if pid_hit or title_hit:
                score = (2 if pid_hit else 0) + (2 if title_hit else 0)
                candidates.append((score, area, hwnd, title, pid))
        candidates.sort(reverse=True)

        if candidates:
            _, _, hwnd, title, pid = candidates[0]
            state.window_found = True
            state.hwnd = hwnd
            state.title = title
            try:
                state.foreground = win32gui.GetForegroundWindow() == hwnd
            except Exception:
                state.foreground = False
            state.detail = f"句柄 {hwnd} 标题「{title}」{note}".strip()
        else:
            state.detail = (
                f"未找到主窗口（进程{'在运行' if pids else '未运行'}）{note}"
                if pids else "同花顺未启动，行情监控继续"
            )

        if state.process_running and not exe_ok and self.process_exe:
            state.detail += "；注意：进程路径与配置不一致，请确认是远航版"
        self.last_state = state
        return state

    def activate(self) -> bool:
        """把同花顺主窗口提到前台。"""
        if win32gui is None:
            return False
        state = self.last_state or self.check()
        if not state.window_found:
            return False
        try:
            if win32gui.IsIconic(state.hwnd):
                win32gui.ShowWindow(state.hwnd, win32con.SW_RESTORE)
                time.sleep(0.3)
            win32gui.SetForegroundWindow(state.hwnd)
            time.sleep(0.2)
            return win32gui.GetForegroundWindow() == state.hwnd
        except Exception:
            return False


# --------------------------------------------------------------------------------------
# 审计日志
# --------------------------------------------------------------------------------------
AUDIT_FIELDS = [
    "event", "timestamp", "signal_id", "code", "name", "signal_type", "triggered_at",
    "quote_time", "price", "avg_price", "deviation_pct", "prev_close", "day_open", "high",
    "low", "amplitude_pct", "change_pct", "cost", "pnl_pct", "support_price", "bid_ask",
    "suggested_price", "suggested_qty", "user_choice", "countdown_left", "dry_run",
    "exec_status", "exec_detail", "coords", "window_state", "remark",
]


class Auditor:
    """CSV 审计日志：信号触发 / 用户决策 / 下单结果 三类事件。"""

    def __init__(self, cfg: Dict[str, Any]):
        log_cfg = cfg["logging"]
        log_dir = ensure_writable_dir(resolve_log_dir(cfg), log_cfg.get("audit_filename", "audit_trade.csv"))
        self.dir = log_dir
        self.path = log_dir / log_cfg.get("audit_filename", "audit_trade.csv")
        self.encoding = log_cfg.get("csv_encoding", "utf-8-sig")
        self._lock = threading.Lock()
        self._ensure_header()

    def _ensure_header(self) -> None:
        if not self.path.exists() or self.path.stat().st_size == 0:
            with open(self.path, "w", newline="", encoding=self.encoding) as fh:
                csv.DictWriter(fh, fieldnames=AUDIT_FIELDS).writeheader()

    def write(self, row: Dict[str, Any]) -> None:
        full = {k: "" for k in AUDIT_FIELDS}
        full.update({k: v for k, v in row.items() if k in AUDIT_FIELDS})
        with self._lock:
            with open(self.path, "a", newline="", encoding=self.encoding) as fh:
                csv.DictWriter(fh, fieldnames=AUDIT_FIELDS).writerow(full)

    @staticmethod
    def signal_row(signal: Signal, event: str, **extra: Any) -> Dict[str, Any]:
        d = int(signal.decimals or 2)
        row = {
            "event": event,
            "timestamp": now_str(),
            "signal_id": signal.signal_id,
            "code": signal.code,
            "name": signal.name,
            "signal_type": signal.stype_label,
            "triggered_at": signal.triggered_at,
            "quote_time": signal.quote_time,
            "price": fmt_price(signal.price, d),
            "avg_price": fmt_price(signal.avg_price, max(d, 3)),
            "deviation_pct": f"{signal.deviation_pct:+.3f}",
            "prev_close": fmt_price(signal.prev_close, d),
            "day_open": fmt_price(signal.day_open, d),
            "high": fmt_price(signal.high, d),
            "low": fmt_price(signal.low, d),
            "amplitude_pct": f"{signal.amplitude_pct:.3f}",
            "change_pct": f"{signal.change_pct:+.3f}",
            "cost": fmt_price(signal.cost, d),
            "pnl_pct": f"{signal.pnl_pct:+.3f}",
            "support_price": fmt_price(signal.support_price, d),
            "bid_ask": signal.bid_ask,
            "suggested_price": fmt_price(signal.suggested_price, d),
            "suggested_qty": int(signal.suggested_qty),
            "window_state": signal.window_state,
        }
        row.update(extra)
        return row


def setup_runtime_logger(cfg: Dict[str, Any]) -> logging.Logger:
    log_cfg = cfg["logging"]
    log_dir = ensure_writable_dir(resolve_log_dir(cfg), log_cfg.get("runtime_filename", "runtime.log"))
    logger = logging.getLogger(APP_NAME)
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.FileHandler(log_dir / log_cfg.get("runtime_filename", "runtime.log"), encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)          # 文件记录完整行情轨迹
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    sh.setLevel(logging.INFO)           # 控制台只显示 INFO 及以上
    logger.addHandler(fh)
    logger.addHandler(sh)
    logger.propagate = False
    return logger


# --------------------------------------------------------------------------------------
# 下单执行器（pyautogui 模拟点击）
# --------------------------------------------------------------------------------------
class OrderExecutor:
    def __init__(self, cfg: Dict[str, Any], monitor: WindowMonitor, auditor: Auditor,
                 logger: logging.Logger):
        self.cfg = cfg
        self.ui_cfg = cfg["order_ui"]
        self.monitor = monitor
        self.auditor = auditor
        self.logger = logger
        self.dry_run = bool(self.ui_cfg.get("dry_run", True))
        self.shot_dir = auditor.dir / "shots"
        self.shot_dir.mkdir(parents=True, exist_ok=True)

    # ---- 坐标与数量规划 ----
    def coords(self) -> Dict[str, List[int]]:
        return {k: list(v) for k, v in (self.ui_cfg.get("coords") or {}).items()}

    def qty_text(self, signal: Signal) -> str:
        qty = float(signal.suggested_qty)
        unit = str(self.ui_cfg.get("qty_unit", "shares")).lower()
        if unit in ("lot", "lots", "手"):
            return str(int(qty // 100))
        return str(int(qty))

    def plan(self, signal: Signal, coords: Optional[Dict[str, List[int]]] = None) -> List[Dict[str, Any]]:
        """按买卖方向选用对应面板的一套坐标。

        下单窗口是买入/卖出双面板：左侧买入（code_box/price_box/qty_box/buy_button），
        右侧卖出（code_box_sell/price_box_sell/qty_box_sell/sell_button）。卖出信号必须
        点右侧输入框，否则会把价格数量填进买入侧再点卖出按钮，导致空单提交。
        因此这里不做「卖出回落买入坐标」的兼容——缺失即中止，避免误下单。
        """
        c = dict(self.coords()) if coords is None else dict(coords)
        is_buy = signal.side == "buy"
        side_cn = "买入" if is_buy else "卖出"
        sfx = "" if is_buy else "_sell"
        side_btn = "buy_button" if is_buy else "sell_button"
        steps = [
            {"name": "激活同花顺窗口", "type": "activate"},
            {"name": f"点击{side_cn}侧证券代码框", "type": "click_type",
             "coord": c.get(f"code_box{sfx}"), "text": signal.code},
            {"name": f"点击{side_cn}侧价格框", "type": "click_type",
             "coord": c.get(f"price_box{sfx}"), "text": signal.price_text(signal.suggested_price)},
            {"name": f"点击{side_cn}侧数量框", "type": "click_type",
             "coord": c.get(f"qty_box{sfx}"), "text": self.qty_text(signal)},
            {"name": f"点击{side_cn}按钮", "type": "click", "coord": c.get(side_btn)},
        ]
        return steps

    def _live_coords(self) -> Dict[str, List[int]]:
        """运行时按控件实时定位一次；失败返回空字典（调用方回退静态坐标）。"""
        try:
            live, _missing, detail = detect_order_coords()
        except Exception as exc:  # pragma: no cover
            self.logger.warning("实时定位异常，改用静态坐标: %s", exc)
            return {}
        if not live:
            self.logger.warning("实时定位失败，改用静态坐标: %s", detail)
            return {}
        return {k: v for k, v in live.items() if v}

    def _merge_coords(self, live: Dict[str, List[int]]) -> Dict[str, List[int]]:
        merged = dict(self.coords())
        merged.update(live)
        return merged

    def _missing_coords(self, steps: List[Dict[str, Any]]) -> List[str]:
        missing = []
        for st in steps:
            if st["type"] in ("click", "click_type"):
                xy = st.get("coord") or [0, 0]
                if not xy or int(xy[0]) <= 0 or int(xy[1]) <= 0:
                    missing.append(st["name"])
        return missing

    # ---- 主执行入口 ----
    def execute(self, signal: Signal) -> Dict[str, Any]:
        steps = self.plan(signal)
        coords_text = json.dumps(
            {st["name"]: st.get("coord") for st in steps if st.get("coord")}, ensure_ascii=False
        )
        missing = self._missing_coords(steps)

        if self.dry_run:
            if self.ui_cfg.get("locate_at_runtime", True):
                live = self._live_coords()
                if live:
                    steps = self.plan(signal, coords=self._merge_coords(live))
                    coords_text = json.dumps(
                        {st["name"]: st.get("coord") for st in steps if st.get("coord")},
                        ensure_ascii=False,
                    )
            detail = "DRY_RUN：仅规划动作，不操作鼠标。动作序列=" + json.dumps(
                [{"step": st["name"], "coord": st.get("coord"), "text": st.get("text")} for st in steps],
                ensure_ascii=False,
            )
            self.logger.info("[DRY_RUN] %s %s", signal.signal_id, detail)
            return {"status": "DRY_RUN", "detail": detail, "coords": coords_text, "clicked": []}

        state = self.monitor.check()
        if not state.window_found:
            detail = f"真实下单已中止：未找到同花顺窗口（{state.detail}）"
            self.logger.error(detail)
            return {"status": "ABORTED_NO_WINDOW", "detail": detail, "coords": coords_text, "clicked": []}

        try:
            import pyautogui
        except ImportError:
            detail = "真实下单已中止：缺少 pyautogui（pip install pyautogui）"
            self.logger.error(detail)
            return {"status": "ABORTED_NO_PYAUTOGUI", "detail": detail, "coords": coords_text, "clicked": []}

        if self.ui_cfg.get("dpi_aware", True):
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                pass

        pyautogui.FAILSAFE = True
        pyautogui.PAUSE = 0.05
        delay = max(0.05, float(self.ui_cfg.get("action_delay_ms", 350)) / 1000.0)
        interval = float(self.ui_cfg.get("type_interval_sec", 0.03))
        clicked: List[Dict[str, Any]] = []

        try:
            if self.ui_cfg.get("bring_to_front", True):
                ok = self.monitor.activate()
                clicked.append({"step": "activate", "ok": ok})
                time.sleep(0.5)

            # 运行时实时定位：窗口被拖动、换分辨率或客户端换布局后，config 里的静态坐标
            # 会全部偏移（实测窗口移动 275px 后旧坐标就会点到别处）。这里在真正点击前
            # 按控件重新枚举一次，成功则覆盖静态坐标；失败才回退 config。
            if self.ui_cfg.get("locate_at_runtime", True):
                live = self._live_coords()
                if live:
                    steps = self.plan(signal, coords=self._merge_coords(live))
                    coords_text = json.dumps(
                        {st["name"]: st.get("coord") for st in steps if st.get("coord")},
                        ensure_ascii=False,
                    )
                    missing = self._missing_coords(steps)

            if missing:
                detail = ("真实下单已中止：以下坐标未配置 -> " + "、".join(missing)
                          + "；请用 python main.py --detect --write（或 --pick --manual）补齐买卖两侧坐标")
                self.logger.error(detail)
                return {"status": "ABORTED_NO_COORD", "detail": detail,
                        "coords": coords_text, "clicked": clicked}

            # 遮挡校验：坐标取自控件矩形中心，本身正确；但若有别的窗口浮在同花顺之上
            # （如安卓模拟器、聊天窗），鼠标会点到那个窗口，导致误操作到无关程序。
            blocked = check_overlay_blocking(steps, _find_order_window())
            if blocked:
                detail = ("真实下单已中止：以下操作点被其它窗口遮挡 -> " + "、".join(blocked)
                          + "；请把同花顺下单窗口切到最前，或移开/最小化遮挡窗口后重试")
                self.logger.error(detail)
                return {"status": "ABORTED_OCCLUDED", "detail": detail,
                        "coords": coords_text, "clicked": clicked}

            for st in steps:
                if st["type"] == "activate":
                    continue
                x, y = int(st["coord"][0]), int(st["coord"][1])
                pyautogui.click(x, y)
                time.sleep(delay)
                if st["type"] == "click_type":
                    # 同花顺价格框会自动带出参考价，ctrl+a 未必全选生效，
                    # 追加 delete + 多次 backspace 兜底，避免出现旧值残留拼接（如 0.7960.795）
                    pyautogui.hotkey("ctrl", "a")
                    time.sleep(0.05)
                    pyautogui.press("delete")
                    time.sleep(0.02)
                    for _ in range(8):
                        pyautogui.press("backspace")
                        time.sleep(0.015)
                    pyautogui.write(str(st["text"]), interval=interval)
                    time.sleep(delay)
                    clicked.append({"step": st["name"], "coord": [x, y], "text": st["text"]})
                else:
                    clicked.append({"step": st["name"], "coord": [x, y]})
                self.logger.info("执行动作: %s @ (%s,%s) %s", st["name"], x, y, st.get("text", ""))

            # 点完下单按钮后客户端会弹「委托确认」，此处回读回显参数并自动确认，拿到合同编号
            dl: Optional[DialogResult] = None
            if steps and str(steps[-1].get("name", "")).endswith("按钮"):
                typed = [st.get("text") for st in steps if st.get("type") == "click_type"]
                expect: Dict[str, Any] = {}
                if len(typed) >= 3:
                    expect = {"code": typed[0], "price": typed[1], "qty": typed[2]}
                dl = handle_order_dialogs(
                    _find_order_window() or 0, expect, self.logger,
                    auto_confirm=bool(self.ui_cfg.get("auto_confirm_order", True)),
                    timeout=float(self.ui_cfg.get("dialog_timeout_sec", 5.0)),
                )

            shot = ""
            if self.ui_cfg.get("screenshot", True):
                try:
                    shot_path = self.shot_dir / f"{signal.signal_id}_{datetime.now():%H%M%S}.png"
                    pyautogui.screenshot(str(shot_path))
                    shot = str(shot_path)
                except Exception as exc:
                    self.logger.warning("截图失败: %s", exc)

            detail = "已按配置坐标完成模拟点击：" + json.dumps(clicked, ensure_ascii=False)
            if shot:
                detail += f"；截图 {shot}"
            if dl is not None:
                detail += f"；下单弹窗 {dl.status}"
                if dl.echoed:
                    detail += " 回显" + json.dumps(dl.echoed, ensure_ascii=False)
                if dl.contract_no:
                    detail += f" 合同编号 {dl.contract_no}"
                if dl.detail:
                    detail += f"（{dl.detail}）"
                status = {
                    "SUBMITTED": "ORDER_SUBMITTED",
                    "REJECTED": "ORDER_REJECTED",
                    "CONFIRM_MISMATCH": "ORDER_ABORTED_MISMATCH",
                    "DECLINED": "ORDER_DECLINED",
                }.get(dl.status, "SIMULATED_OK")
            else:
                detail += "；请在同花顺界面人工核对委托是否提交成功"
                status = "SIMULATED_OK"
            return {"status": status, "detail": detail, "coords": coords_text, "clicked": clicked}
        except Exception as exc:  # pragma: no cover
            detail = f"模拟点击异常: {exc}"
            self.logger.exception(detail)
            return {"status": "ERROR", "detail": detail, "coords": coords_text, "clicked": clicked}


# --------------------------------------------------------------------------------------
# 弹窗与主程序（Tk）
# --------------------------------------------------------------------------------------
class SignalPopup:
    """置顶确认弹窗，带倒计时。"""

    def __init__(self, master, signal: Signal, timeout: int, sound: bool,
                 on_done, auto_confirm_after: Optional[int] = None):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.signal = signal
        self.timeout = int(timeout)
        self.remaining = self.timeout
        self.choice: Optional[str] = None
        self.on_done = on_done
        self.auto_confirm_after = auto_confirm_after
        self._elapsed = 0
        self._closed = False

        self.win = tk.Toplevel(master)
        self.win.title(f"【T信号】{signal.name} {signal.code} {signal.stype_label}")
        self.win.attributes("-topmost", True)
        self.win.resizable(False, False)
        self.win.configure(bg="#ffffff")
        self.win.protocol("WM_DELETE_WINDOW", lambda: self._finish("ignore"))

        color = "#c0392b" if signal.side == "buy" else "#1e8449"
        head = tk.Frame(self.win, bg=color)
        head.pack(fill="x")
        tk.Label(
            head,
            text=f"{signal.stype_label}    {signal.name} {signal.code}",
            font=("Microsoft YaHei", 14, "bold"), fg="white", bg=color, padx=14, pady=10,
        ).pack(side="left")

        body = tk.Frame(self.win, bg="#ffffff", padx=16, pady=12)
        body.pack(fill="both", expand=True)

        detail = tk.Label(
            body, text=signal.render_detail(), justify="left", anchor="nw",
            font=("Consolas", 10), bg="#ffffff", fg="#222222", wraplength=720,
        )
        detail.pack(fill="both", expand=True)

        self.timer_label = tk.Label(
            body, text="", font=("Microsoft YaHei", 13, "bold"), fg=color, bg="#ffffff"
        )
        self.timer_label.pack(pady=(10, 4))

        btns = tk.Frame(body, bg="#ffffff")
        btns.pack(pady=(6, 4))

        mode = "（DRY_RUN 演练，不会真的操作鼠标）" if signal_stub_dry(master) else "（真实下单模式）"
        tk.Label(btns, text=mode, font=("Microsoft YaHei", 9), fg="#888888", bg="#ffffff").pack(side="left", padx=8)

        self.btn_ok = tk.Button(
            btns, text="确认下单 (Enter)", command=lambda: self._finish("confirm"),
            font=("Microsoft YaHei", 11, "bold"), bg=color, fg="white",
            activebackground=color, width=16, height=1, relief="flat",
        )
        self.btn_ok.pack(side="left", padx=10)

        self.btn_no = tk.Button(
            btns, text="忽略 (Esc)", command=lambda: self._finish("ignore"),
            font=("Microsoft YaHei", 11), width=12, relief="flat", bg="#eeeeee",
        )
        self.btn_no.pack(side="left", padx=6)

        self.win.bind("<Return>", lambda e: self._finish("confirm"))
        self.win.bind("<Escape>", lambda e: self._finish("ignore"))

        self._center()
        if sound:
            play_signal_sound()
        self.win.focus_force()
        self.btn_ok.focus_set()
        self._tick()

    def _center(self) -> None:
        self.win.update_idletasks()
        w = self.win.winfo_width()
        h = self.win.winfo_height()
        sw = self.win.winfo_screenwidth()
        self.win.geometry(f"+{max(20, (sw - w) // 2)}+{max(20, int(self.win.winfo_screenheight() * 0.18))}")

    def _tick(self) -> None:
        if self._closed:
            return
        if self.auto_confirm_after is not None and self._elapsed >= self.auto_confirm_after:
            self._finish("confirm")
            return
        if self.remaining <= 0:
            self._finish("timeout")
            return
        self.timer_label.config(text=f"请在 {self.remaining} 秒内选择，超时自动放弃下单（不下单）")
        self.remaining -= 1
        self._elapsed += 1
        self.win.after(1000, self._tick)

    def _finish(self, choice: str) -> None:
        if self._closed:
            return
        self._closed = True
        self.choice = choice
        left = max(0, self.remaining)
        try:
            self.win.destroy()
        except Exception:
            pass
        self.on_done(self.signal, choice, left)


def signal_stub_dry(master) -> bool:
    try:
        return bool(getattr(master, "app_dry_run", True))
    except Exception:
        return True


def play_signal_sound() -> None:
    """播放提示音（不依赖额外音频文件）。"""
    try:
        import winsound
        for _ in range(2):
            winsound.Beep(880, 160)
            winsound.Beep(1320, 160)
        winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
    except Exception:
        try:
            print("\a", end="", flush=True)
        except Exception:
            pass


class TraderApp:
    def __init__(self, cfg: Dict[str, Any], logger: logging.Logger,
                 config_path: Optional[Path] = None,
                 argv: Optional[List[str]] = None):
        self.cfg = cfg
        self.logger = logger
        # 重启与配置编辑需要知道「当前用的是哪份配置」和「原来是怎么启动的」
        self.config_path = Path(config_path).resolve() if config_path else DEFAULT_CONFIG_PATH
        self.argv = list(argv) if argv is not None else list(sys.argv[1:])
        self.cfg_ui_proc: Optional[Any] = None
        self.restarting = False
        self.fetcher = SinaFetcher(cfg)
        self.engine = SignalEngine(cfg)
        self.monitor = WindowMonitor(cfg)
        self.auditor = Auditor(cfg)
        self.executor = OrderExecutor(cfg, self.monitor, self.auditor, logger)
        self.codes = list(cfg["market"]["codes"])
        self.quotes: Dict[str, Quote] = {}
        self.checks: Dict[str, Dict[str, List[Check]]] = {}
        self.signal_queue: "queue.Queue[Signal]" = queue.Queue()
        self.popup: Optional[SignalPopup] = None
        self.running = True
        self.window_state = self.monitor.check()
        self.last_signal: Optional[Signal] = None
        self.poll_count = 0
        self.error_count = 0
        self.started_at = now_str()
        # 下单记录（按 signal_id 归并：待确认 → 已确认 → 下单结果 → 台账变更）
        self.records: Dict[str, Dict[str, Any]] = {}
        self._rec_lock = threading.Lock()
        self._rec_dirty = True
        self._rec_tab: Optional[Any] = None

        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.title(f"做T辅助 · 半自动 v{VERSION}   确认后才下单")
        self.root.geometry("1040x640")
        self.root.app_dry_run = self.executor.dry_run  # type: ignore[attr-defined]
        self.root.protocol("WM_DELETE_WINDOW", self.shutdown)
        self._build_ui()
        self._poller = threading.Thread(target=self._poll_loop, daemon=True)
        self._poller.start()
        self.root.after(300, self._pump)
        self.root.after(500, self._refresh_dashboard)

    # ---- UI ----
    def _build_ui(self) -> None:
        tk = self.tk
        top = tk.Frame(self.root, bg="#2c3e50")
        top.pack(fill="x")
        mode = "DRY_RUN 演练模式（不操作鼠标）" if self.executor.dry_run else "真实下单模式（确认后模拟点击）"
        color = "#f39c12" if self.executor.dry_run else "#e74c3c"
        tk.Label(top, text=f"  {APP_NAME} v{VERSION}", font=("Microsoft YaHei", 12, "bold"),
                 fg="white", bg="#2c3e50", pady=8).pack(side="left")
        tk.Label(top, text=f"{mode}  ", font=("Microsoft YaHei", 10, "bold"),
                 fg="white", bg=color, padx=8, pady=8).pack(side="right")

        info = tk.Frame(self.root, padx=10, pady=8)
        info.pack(fill="both", expand=True)

        from tkinter import ttk
        self.notebook = ttk.Notebook(info)
        self.notebook.pack(fill="both", expand=True)

        tab_status = tk.Frame(self.notebook)
        self.notebook.add(tab_status, text="  实时状态  ")
        tk.Label(tab_status, text="实时状态", font=("Microsoft YaHei", 10, "bold"), anchor="w").pack(fill="x", pady=(6, 0))
        self.status_text = tk.Text(tab_status, height=16, font=("Consolas", 10), bg="#fbfbfb",
                                   relief="solid", borderwidth=1, wrap="none")
        self.status_text.pack(fill="both", expand=True, pady=(4, 8))
        self.status_text.configure(state="disabled")

        tab_rec = tk.Frame(self.notebook)
        self.notebook.add(tab_rec, text="  下单记录  ")
        self._rec_tab = tab_rec
        self._build_record_tab(tab_rec)

        bar = tk.Frame(self.root, padx=10, pady=6)
        bar.pack(fill="x")
        tk.Button(bar, text="打开日志目录", command=self._open_log_dir,
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="立即检查窗口", command=self._manual_check,
                  font=("Microsoft YaHei", 9)).pack(side="left", padx=6)
        tk.Button(bar, text="设置参数", command=self._open_config_ui,
                  font=("Microsoft YaHei", 9)).pack(side="left")
        tk.Button(bar, text="重启程序", command=self._restart,
                  bg="#2980b9", fg="white",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="left", padx=6)
        tk.Button(bar, text="退出程序", command=self.shutdown, bg="#c0392b", fg="white",
                  font=("Microsoft YaHei", 9, "bold")).pack(side="right")

        self.logger.info("程序启动 | dry_run=%s | 标的=%s | 配置轮询=%ss",
                         self.executor.dry_run, ",".join(self.codes),
                         self.cfg["market"]["poll_interval_sec"])

    def _open_log_dir(self) -> None:
        try:
            os.startfile(str(self.auditor.dir))  # type: ignore[attr-defined]
        except Exception as exc:
            self.logger.warning("打开日志目录失败: %s", exc)

    def _manual_check(self) -> None:
        self.window_state = self.monitor.check()
        self.logger.info("手动窗口检查: %s", self.window_state.summary())

    # ---- 下单记录面板 ----
    _REC_EVENTS = ("SIGNAL_TRIGGER", "USER_DECISION", "ORDER_RESULT", "BATCH_UPDATE")
    _REC_COLUMNS = (
        ("time", "时间", 130, False),
        ("code", "标的", 130, False),
        ("stype", "类型", 100, False),
        ("side", "方向", 50, False),
        ("price", "委托价", 68, False),
        ("qty", "数量", 56, False),
        ("status", "状态", 96, False),
        ("detail", "说明", 330, True),
    )
    _REC_STATUS = {
        "DRY_RUN": ("演练完成", "dry"),
        "SIMULATED_OK": ("已模拟下单", "ok"),
        "ORDER_SUBMITTED": ("已提交委托", "ok"),
        "ABORTED_NO_COORD": ("中止·坐标未配", "fail"),
        "ABORTED_NO_WINDOW": ("中止·无窗口", "fail"),
        "ABORTED_NO_PYAUTOGUI": ("中止·缺pyautogui", "fail"),
        "ORDER_FAILED": ("下单失败", "fail"),
    }
    _REC_CHOICE = {
        "confirm": ("已确认·执行中", "wait"),
        "ignore": ("已忽略", "skip"),
        "timeout": ("超时放弃", "skip"),
    }

    def _build_record_tab(self, parent) -> None:
        tk = self.tk
        from tkinter import ttk
        bar = tk.Frame(parent)
        bar.pack(fill="x", pady=(6, 4))
        self.rec_count = tk.Label(bar, text="今日 0 条记录", anchor="w",
                                  font=("Microsoft YaHei", 9))
        self.rec_count.pack(side="left")
        tk.Button(bar, text="刷新", command=self._reload_records,
                  font=("Microsoft YaHei", 9)).pack(side="right")
        tk.Button(bar, text="打开审计CSV", command=self._open_audit_csv,
                  font=("Microsoft YaHei", 9)).pack(side="right", padx=6)
        tk.Button(bar, text="复制选中行", command=self._copy_selected_record,
                  font=("Microsoft YaHei", 9)).pack(side="right")

        wrap = tk.Frame(parent)
        wrap.pack(fill="both", expand=True, pady=(0, 6))
        wrap.rowconfigure(0, weight=1)
        wrap.columnconfigure(0, weight=1)
        cols = [c[0] for c in self._REC_COLUMNS]
        self.rec_tree = ttk.Treeview(wrap, columns=cols, show="headings", height=12)
        for key, title, width, stretch in self._REC_COLUMNS:
            self.rec_tree.heading(key, text=title)
            anchor = "w" if key in ("code", "status", "detail") else "center"
            self.rec_tree.column(key, width=width, anchor=anchor, stretch=stretch)
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.rec_tree.yview)
        hsb = ttk.Scrollbar(wrap, orient="horizontal", command=self.rec_tree.xview)
        self.rec_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self.rec_tree.grid(row=0, column=0, sticky="nsew")
        vsb.grid(row=0, column=1, sticky="ns")
        hsb.grid(row=1, column=0, sticky="ew")
        for tag, color in (("ok", "#1e8449"), ("dry", "#b9770e"), ("fail", "#c0392b"),
                           ("wait", "#2471a3"), ("skip", "#7f8c8d")):
            self.rec_tree.tag_configure(tag, foreground=color)
        self.rec_tree.bind("<Double-1>", lambda _e: self._show_record_detail())
        self.rec_tree.bind("<Return>", lambda _e: self._show_record_detail())
        self._reload_records()

    @staticmethod
    def _rec_side(stype_label: str, side: str = "") -> str:
        if side == "buy":
            return "买入"
        if side == "sell":
            return "卖出"
        label = stype_label or ""
        # 买回是「买入」动作，但标签里带「反T」二字，必须先于反T判定拦下
        if "买回" in label:
            return "买入"
        if "先买" in label:
            return "买入"
        if "先卖" in label or "止损" in label:
            return "卖出"
        return ""

    @classmethod
    def _rec_status_of(cls, status: str) -> Tuple[str, str]:
        key = (status or "").strip()
        if not key:
            return "", "fail"
        return cls._REC_STATUS.get(key, (f"异常·{key}", "fail"))

    def _rec_ensure(self, sid: str, **fields: Any) -> Dict[str, Any]:
        """按 signal_id 归并记录：不存在则建，存在则用非空字段覆盖。"""
        with self._rec_lock:
            rec = self.records.get(sid)
            if rec is None:
                rec = {"signal_id": sid, "time": now_str(), "code": "", "stype": "",
                       "side": "", "price": "", "qty": "", "status": "待确认",
                       "tag": "wait", "detail": "", "sig": None}
                self.records[sid] = rec
            for key, val in fields.items():
                if val not in (None, ""):
                    rec[key] = val
            self._rec_dirty = True
            return rec

    def _record_signal(self, sig: Signal) -> None:
        """信号触发：建一条待确认记录（跑在行情线程里，只改内存数据）。"""
        d = int(sig.decimals or 2)
        self._rec_ensure(
            sig.signal_id,
            time=now_str(),
            code=f"{short_code(sig.code)} {sig.name}".strip(),
            stype=sig.stype_label,
            side=self._rec_side(sig.stype_label, getattr(sig, "side", "")),
            price=fmt_price(sig.suggested_price or sig.price, d),
            qty=str(int(sig.suggested_qty or 0)),
            status="待确认", tag="wait",
            detail=f"弹窗倒计时 {self.cfg['ui']['popup_timeout_sec']}s 内需人工确认",
            sig=sig,
        )

    def _record_update(self, sig: Signal, status: str = "", tag: str = "",
                       detail: str = "", append: bool = False) -> None:
        sid = getattr(sig, "signal_id", "")
        if not sid:
            return
        if status:
            self._rec_ensure(sid, status=status, tag=tag)
        if detail:
            with self._rec_lock:
                rec = self.records.get(sid)
                if rec is not None:
                    old = rec.get("detail") or ""
                    rec["detail"] = (old + " | " + detail) if (append and old) else detail
                    self._rec_dirty = True

    def _reload_records(self) -> None:
        """从 audit_trade.csv 补入当日历史记录（内存里已有的一律以内存为准）。"""
        day = datetime.now().strftime("%Y-%m-%d")
        loaded = self._load_records_from_csv(day)
        added = 0
        with self._rec_lock:
            for sid, rec in loaded.items():
                if sid not in self.records:
                    self.records[sid] = rec
                    added += 1
            self._rec_dirty = True
        if added:
            self.logger.info("下单记录：自审计日志补入 %s 条历史（%s）", added, day)
        self._flush_records()

    def _load_records_from_csv(self, day: str) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        path = self.auditor.path
        if not path.exists():
            return out
        try:
            with open(path, "r", newline="", encoding=self.auditor.encoding) as fh:
                for row in csv.DictReader(fh):
                    if not (row.get("timestamp") or "").startswith(day):
                        continue
                    event = (row.get("event") or "").strip()
                    sid = (row.get("signal_id") or "").strip()
                    if not sid or event not in self._REC_EVENTS:
                        continue
                    label = (row.get("signal_type") or "").strip()
                    rec = out.get(sid)
                    if rec is None:
                        rec = {
                            "signal_id": sid,
                            "time": (row.get("timestamp") or "").strip(),
                            "code": ((row.get("code") or "").strip() + " "
                                     + (row.get("name") or "").strip()).strip(),
                            "stype": label,
                            "side": self._rec_side(label),
                            "price": (row.get("suggested_price") or row.get("price") or "").strip(),
                            "qty": (row.get("suggested_qty") or "").strip(),
                            "status": "待确认", "tag": "wait", "detail": "", "sig": None,
                        }
                        out[sid] = rec
                    if event == "USER_DECISION":
                        choice = (row.get("user_choice") or "").strip()
                        hit = self._REC_CHOICE.get(choice)
                        if hit:
                            rec["status"], rec["tag"] = hit
                    elif event == "ORDER_RESULT":
                        rec["status"], rec["tag"] = self._rec_status_of(row.get("exec_status") or "")
                        detail = (row.get("exec_detail") or "").strip()
                        if detail:
                            rec["detail"] = detail[:240]
                    elif event == "BATCH_UPDATE":
                        note = (row.get("exec_detail") or "").strip()
                        if note:
                            rec["detail"] = ((rec["detail"] + " | 台账 " + note[:160])
                                             if rec["detail"] else ("台账 " + note[:160]))
        except Exception as exc:
            self.logger.warning("读取审计日志失败: %s", exc)
        return out

    def _flush_records(self) -> None:
        """把记录刷到表格（只在 UI 线程调用）。"""
        tree = getattr(self, "rec_tree", None)
        if tree is None or not self.running:
            return
        with self._rec_lock:
            rows = sorted(self.records.values(),
                          key=lambda r: (r.get("time") or "", r.get("signal_id") or ""))
            snapshot = [dict(r) for r in rows]
            self._rec_dirty = False
        tree.delete(*tree.get_children())
        for rec in snapshot:
            detail = rec.get("detail", "") or ""
            if len(detail) > 80:
                detail = detail[:80] + "…"
            tree.insert("", "end", iid=rec["signal_id"],
                        values=(rec.get("time", ""), rec.get("code", ""), rec.get("stype", ""),
                                rec.get("side", ""), rec.get("price", ""), rec.get("qty", ""),
                                rec.get("status", ""), detail),
                        tags=(rec.get("tag") or "wait",))
        if self._rec_tab is not None:
            try:
                self.notebook.tab(self._rec_tab, text=f"  下单记录 ({len(snapshot)})  ")
            except Exception:
                pass
        if getattr(self, "rec_count", None) is not None:
            done = sum(1 for r in snapshot if r.get("tag") in ("ok", "dry"))
            self.rec_count.configure(
                text=f"今日共 {len(snapshot)} 条信号记录，已执行 {done} 笔（双击行看详情）")

    def _show_record_detail(self) -> None:
        sel = self.rec_tree.selection()
        if not sel:
            return
        with self._rec_lock:
            rec = dict(self.records.get(sel[0]) or {})
        if not rec:
            return
        tk = self.tk
        win = tk.Toplevel(self.root)
        win.title(f"下单记录详情 · {rec.get('signal_id')}")
        win.geometry("780x470")
        txt = tk.Text(win, font=("Consolas", 10), wrap="word", bg="#fbfbfb")
        txt.pack(fill="both", expand=True, padx=8, pady=8)
        lines = [
            f"信号ID：{rec.get('signal_id')}",
            f"记录时间：{rec.get('time')}",
            f"标的：{rec.get('code')}",
            f"类型：{rec.get('stype')}",
            f"方向：{rec.get('side')}",
            f"委托价：{rec.get('price')}    数量：{rec.get('qty')}",
            f"状态：{rec.get('status')}",
            f"说明：{rec.get('detail')}",
        ]
        sig = rec.get("sig")
        if sig is not None:
            try:
                lines += ["", "── 信号明细 ──", sig.render_detail()]
            except Exception:
                pass
        txt.insert("1.0", "\n".join(lines))
        txt.configure(state="disabled")
        tk.Button(win, text="关闭", command=win.destroy).pack(pady=(0, 8))

    def _copy_selected_record(self) -> None:
        sel = self.rec_tree.selection()
        if not sel:
            return
        lines: List[str] = []
        for iid in sel:
            vals = self.rec_tree.item(iid, "values")
            lines.append("\t".join(str(v) for v in vals))
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append("\n".join(lines))
            self.logger.info("已复制 %s 条下单记录到剪贴板", len(lines))
        except Exception as exc:
            self.logger.warning("复制下单记录失败: %s", exc)

    def _open_audit_csv(self) -> None:
        try:
            os.startfile(str(self.auditor.path))  # type: ignore[attr-defined]
        except Exception as exc:
            self.logger.warning("打开审计CSV失败: %s", exc)

    # ---- 设置参数（config.json）与重启 ----
    def _open_config_ui(self) -> None:
        """打开 config.json 图形化编辑器（config_ui.py，独立进程，改坏不影响主程序）。"""
        import tkinter.messagebox as mb
        script = BASE_DIR / "config_ui.py"
        if self.cfg_ui_proc is not None and self.cfg_ui_proc.poll() is None:
            mb.showinfo("提示", "配置窗口已经打开了，请在那个窗口里操作。")
            return
        frozen = getattr(sys, "frozen", False)
        if not frozen and not script.exists():
            if mb.askyesno("未找到配置编辑器",
                           f"未找到 {script.name}。\n\n是否用记事本直接打开配置文件？\n{self.config_path}"):
                try:
                    os.startfile(str(self.config_path))  # type: ignore[attr-defined]
                except Exception as exc:
                    mb.showerror("打开失败", str(exc))
            return
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self._cfg_snapshot = self._config_snapshot()   # 记录打开前的磁盘状态，退出后比对
        try:
            ui_cmd = ([sys.executable, "--ui"] if frozen else [sys.executable, str(script)])
            self.cfg_ui_proc = subprocess.Popen(
                ui_cmd + ["--config", str(self.config_path)],
                cwd=str(BASE_DIR), close_fds=True, creationflags=flags,
                env=_child_env())
        except Exception as exc:
            mb.showerror("启动配置编辑器失败", str(exc))
            return
        self.logger.info("打开配置编辑器 | pid=%s | 配置=%s", self.cfg_ui_proc.pid, self.config_path)
        self.root.after(1000, self._poll_config_ui)

    def _config_snapshot(self) -> Optional[Tuple[int, int]]:
        """取 config.json 的 (mtime_ns, size) 指纹，用于判断编辑器是否真的落盘。"""
        try:
            st = self.config_path.stat()
            return (st.st_mtime_ns, st.st_size)
        except OSError:
            return None

    def _poll_config_ui(self) -> None:
        """盯住配置编辑器：关闭后按文件指纹判断是否真的保存，再决定是否提示重启。"""
        import tkinter.messagebox as mb
        proc = self.cfg_ui_proc
        if proc is None or not self.running:
            return
        if proc.poll() is None:
            self.root.after(1000, self._poll_config_ui)
            return
        self.cfg_ui_proc = None
        before = getattr(self, "_cfg_snapshot", None)
        after = self._config_snapshot()
        saved = after is not None and after != before
        if proc.returncode != 0:
            self.logger.info("配置编辑器非正常结束 | returncode=%s | 配置已变更=%s",
                             proc.returncode, saved)
        if not saved:
            self.logger.info("配置未变更 | returncode=%s | 未触发重启", proc.returncode)
            mb.showinfo("配置未变更",
                        "config.json 没有发生变化，本次未保存任何修改，程序也不会重启。\n\n"
                        "如需修改：重新打开配置编辑器 → 改完点【保存】按钮 → "
                        "看到“保存成功”提示后再关闭窗口。")
            return
        self.logger.info("配置已保存 | 询问是否重启 | 配置=%s", self.config_path)
        if mb.askyesno("配置已保存",
                       "config.json 已保存。\n\n是否立即重启监控进程让新参数生效？\n"
                       "（重启会取消当前未确认的信号弹窗；当日信号台账会先落盘、重启后自动恢复）"):
            self._do_restart()

    def _restart_args(self) -> List[str]:
        """复用原启动参数，剔除一次性子命令与由重启函数自行补齐的 --config。"""
        drop = {"--config", "--once", "--selftest", "--auto-confirm", "--test-order",
                "--submit", "--detect", "--write", "--pick", "--manual"}
        out: List[str] = []
        skip_next = False
        for item in self.argv:
            if skip_next:
                skip_next = False
                continue
            key = item.split("=", 1)[0]
            if key in drop:
                if "=" not in item and key == "--config":
                    skip_next = True
                continue
            out.append(item)
        return out

    def _restart(self) -> None:
        import tkinter.messagebox as mb
        tip = "确定重启监控程序？\n\n"
        if self.popup is not None:
            tip += "⚠ 当前有未确认的信号弹窗，重启会取消它。\n\n"
        tip += ("重启前会自动保存当日信号台账，重启后自动恢复。\n"
                f"配置：{self.config_path.name}")
        if mb.askyesno("重启程序", tip):
            self._do_restart()

    def _do_restart(self) -> None:
        import tkinter.messagebox as mb
        if self.restarting:
            return
        self.restarting = True

        # 1) 先校验新配置：配置写坏时宁可留在当前实例，也不要重启后起不来
        try:
            load_config(self.config_path)
        except Exception as exc:
            self.restarting = False
            mb.showerror("配置有误，已取消重启",
                         f"{self.config_path}\n\n{exc}\n\n请先修正配置再重启。")
            return

        # 2) 停轮询并落盘当日台账，避免重启丢批次/闭环锁
        self.running = False
        try:
            self.engine.save_state()
            self.logger.info("重启前已保存信号状态")
        except Exception as exc:
            self.logger.warning("重启前保存状态失败: %s", exc)

        # 3) 拉起新实例（沿用原启动参数，配置路径显式带上）
        cmd = ([sys.executable] if getattr(sys, "frozen", False)
               else [sys.executable, str(Path(__file__).resolve())]) \
            + self._restart_args() + ["--config", str(self.config_path)]
        try:
            log_dir = Path(self.auditor.dir)
        except Exception:
            log_dir = resolve_log_dir(None)
        log_path = log_dir / f"restart_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"
        fh: Any = subprocess.DEVNULL
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            fh = open(log_path, "a", encoding="utf-8", errors="replace")
        except Exception:
            fh = subprocess.DEVNULL
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        try:
            proc = subprocess.Popen(cmd, cwd=str(BASE_DIR), close_fds=True,
                                    creationflags=flags, env=_child_env(),
                                    stdout=fh, stderr=subprocess.STDOUT)
        except Exception as exc:
            self.running = True
            self.restarting = False
            if fh is not subprocess.DEVNULL:
                try:
                    fh.close()
                except Exception:
                    pass
            mb.showerror("重启失败", f"无法启动新实例：{exc}")
            return

        self.logger.info("重启：新实例 pid=%s 已拉起，本实例退出 | cmd=%s",
                         proc.pid, " ".join(cmd))
        time.sleep(0.6)   # 让新实例先起好，再把旧窗口收掉
        try:
            self.root.destroy()
        except Exception:
            pass
        os._exit(0)

    # ---- 行情轮询线程 ----
    def _poll_loop(self) -> None:
        while self.running:
            try:
                quotes = self.fetcher.fetch(self.codes)
                self.poll_count += 1
                self.error_count = 0
                for code, q in quotes.items():
                    self.quotes[short_code(code)] = q
                    self.logger.debug("行情 %s", q.summary())
                self.window_state = self.monitor.check()
                self.logger.debug("窗口 %s", self.window_state.summary())

                for q in quotes.values():
                    signals, checks = self.engine.evaluate(q)
                    self.checks[short_code(q.code)] = checks
                    for sig in signals:
                        sig.window_state = self.window_state.summary()
                        self._on_signal(sig)
            except Exception as exc:
                self.error_count += 1
                self.logger.warning("行情获取失败(%s 次): %s", self.error_count, exc)
            interval = float(
                self.cfg["market"]["poll_interval_sec"]
                if self.engine.in_session() or self.cfg["trade"].get("allow_offhours_signal")
                else self.cfg["market"]["offhours_poll_interval_sec"]
            )
            slept = 0.0
            while slept < interval and self.running:
                time.sleep(0.2)
                slept += 0.2

    def _on_signal(self, sig: Signal) -> None:
        self.last_signal = sig
        self._record_signal(sig)
        self.auditor.write(Auditor.signal_row(sig, "SIGNAL_TRIGGER",
                                              user_choice="", dry_run=self.executor.dry_run))

        self.logger.warning("信号触发 %s %s %s @ %s 建议%s %d 股",
                            sig.signal_id, sig.name, sig.stype_label,
                            fmt_price(sig.price, sig.decimals),
                            "买入" if sig.side == "buy" else "卖出", int(sig.suggested_qty))
        self.signal_queue.put(sig)

    # ---- UI 泵 ----
    def _pump(self) -> None:
        if not self.running:
            return
        if self.popup is None:
            try:
                sig = self.signal_queue.get_nowait()
            except queue.Empty:
                sig = None
            if sig is not None:
                self._show_popup(sig)
        self.root.after(250, self._pump)

    def _show_popup(self, sig: Signal) -> None:
        auto_confirm = getattr(self.root, "auto_confirm_after", None)
        self.popup = SignalPopup(
            self.root, sig,
            timeout=int(self.cfg["ui"]["popup_timeout_sec"]),
            sound=bool(self.cfg["ui"]["sound"]),
            on_done=self._on_popup_done,
            auto_confirm_after=auto_confirm,
        )

    def _on_popup_done(self, sig: Signal, choice: str, remaining: int) -> None:
        self.popup = None
        self.auditor.write(Auditor.signal_row(
            sig, "USER_DECISION", user_choice=choice, countdown_left=remaining,
            dry_run=self.executor.dry_run,
            remark={"confirm": "用户确认下单", "ignore": "用户点忽略", "timeout": "倒计时结束自动放弃"}.get(choice, ""),
        ))
        self.logger.info("用户选择 %s（%s，剩余 %ss）", choice, sig.signal_id, remaining)
        hit = self._REC_CHOICE.get(choice)
        if hit:
            self._record_update(sig, status=hit[0], tag=hit[1],
                                detail={
                                    "confirm": "用户确认，正在执行下单",
                                    "ignore": "用户点忽略，未下单",
                                    "timeout": f"倒计时结束自动放弃（剩余 {remaining}s）",
                                }.get(choice, ""))
        if choice != "confirm":
            return
        threading.Thread(target=self._run_order, args=(sig,), daemon=True).start()

    def _run_order(self, sig: Signal) -> None:
        result = self.executor.execute(sig)
        self.auditor.write(Auditor.signal_row(
            sig, "ORDER_RESULT", user_choice="confirm", dry_run=self.executor.dry_run,
            exec_status=result["status"], exec_detail=result["detail"],
            coords=result["coords"],
        ))
        self.logger.warning("下单结果 %s -> %s", result["status"], result["detail"])
        status_label, status_tag = self._rec_status_of(result["status"])
        self._record_update(sig, status=status_label, tag=status_tag,
                            detail=str(result["detail"] or "")[:240])

        # 批次台账推进：真实提交必须推进；演练默认也推进，否则空跑时永远等不到平仓信号
        advance = result["status"] in ("ORDER_SUBMITTED", "SIMULATED_OK") or (
            result["status"] == "DRY_RUN"
            and bool(self.cfg["trade"].get("book_on_dry_run", True))
        )
        if advance:
            try:
                note = self.engine.register_fill(sig)
                self.logger.warning("批次台账 %s", note)
                self._record_update(sig, detail=f"台账 {note}"[:240], append=True)
                self.auditor.write(Auditor.signal_row(
                    sig, "BATCH_UPDATE", user_choice="confirm",
                    dry_run=self.executor.dry_run, exec_status=result["status"],
                    exec_detail=note, coords=result["coords"],
                ))
            except Exception as exc:
                self.logger.warning("批次台账更新失败（不影响已发生的委托）：%s", exc)

        if result["status"] in ("DRY_RUN", "SIMULATED_OK"):
            try:
                self.root.after(0, lambda: self._toast(result))
            except Exception:
                pass

    def _toast(self, result: Dict[str, Any]) -> None:
        if getattr(self, "suppress_toast", False):
            print(f"[toast] {result['status']}: {result['detail'][:300]}")
            return
        mapping = {
            "DRY_RUN": "演练完成（未操作鼠标）",
            "SIMULATED_OK": "已模拟点击，请立即到同花顺人工核对委托",
            "ABORTED_NO_COORD": "已中止：下单界面坐标未配置",
            "ABORTED_NO_WINDOW": "已中止：未找到同花顺主窗口",
            "ABORTED_NO_PYAUTOGUI": "已中止：缺少 pyautogui",
        }
        state = mapping.get(result["status"], "执行异常，请人工核对")
        import tkinter.messagebox as mb
        mb.showinfo("下单结果", f"{state}\n\n{result['detail'][:600]}", parent=self.root)

    def _refresh_dashboard(self) -> None:
        if not self.running:
            return
        lines = []
        lines.append(f"启动时间：{self.started_at}    轮询次数：{self.poll_count}    "
                     f"失败次数：{self.error_count}    当前时间：{now_str()}")
        lines.append(f"交易时段：{'是' if self.engine.in_session() else '否'}"
                     f"（非交易时段仅采集行情，不产生信号，可在 config.trade.allow_offhours_signal 调整）")
        lines.append("")
        lines.append("【同花顺窗口】" + self.window_state.summary())
        lines.append("")
        lines.append("【行情】")
        for code in self.codes:
            q = self.quotes.get(short_code(code))
            if q is None:
                lines.append(f"  {code}: 等待行情…")
                continue
            pos = self.engine.position(code)
            lines.append("  " + q.summary())
            support = self.engine._support_price(q, self.cfg['signals']['buy_first'])
            lines.append(f"      持仓成本 {fmt_price(pos['cost'], q.decimals)} 盈亏 {q.pnl_pct(pos['cost']):+.2f}%  "
                         f"可卖 {int(pos['sellable_qty'])} 股  支撑 {fmt_price(support, q.decimals)}")

            chk = self.checks.get(short_code(code), {})
            for stype, label in (("BUY_FIRST", "正T"), ("SELL_FIRST", "反T")):
                items = chk.get(stype) or []
                if not items:
                    continue
                passed = sum(1 for c in items if c.ok)
                state = "全部满足" if passed == len(items) else "待满足"
                lines.append(f"      {label}: {passed}/{len(items)} 项 {state}")
        if self.last_signal:
            lines.append("")
            lines.append(f"【最近信号】{self.last_signal.signal_id} {self.last_signal.name} "
                         f"{self.last_signal.stype_label} @ {fmt_price(self.last_signal.price, self.last_signal.decimals)} "
                         f"（{self.last_signal.triggered_at}）")
        lines.append("")
        lines.append(f"审计日志：{self.auditor.path}")
        lines.append(f"运行日志：{self.auditor.dir / 'runtime.log'}")
        if self.executor.dry_run:
            lines.append("")
            lines.append(">> 当前 dry_run=True：确认下单只走流程并记录日志，不会操作鼠标。")
        else:
            lines.append("")
            lines.append(">> 当前 dry_run=False：点击确认后将按配置坐标模拟真实下单，请谨慎。")

        self.status_text.configure(state="normal")
        self.status_text.delete("1.0", "end")
        self.status_text.insert("1.0", "\n".join(lines))
        self.status_text.configure(state="disabled")
        if self._rec_dirty:
            self._flush_records()
        self.root.after(700, self._refresh_dashboard)

    def run(self) -> None:
        self.root.mainloop()

    def shutdown(self) -> None:
        if not self.running:
            return
        self.running = False
        try:
            self.engine.save_state()
        except Exception as exc:
            self.logger.warning("退出前保存状态失败: %s", exc)
        self.logger.info("程序退出")
        try:
            self.root.destroy()
        except Exception:
            pass


# --------------------------------------------------------------------------------------
# 命令行子命令
# --------------------------------------------------------------------------------------
def cmd_once(cfg: Dict[str, Any], logger: logging.Logger) -> int:
    fetcher = SinaFetcher(cfg)
    engine = SignalEngine(cfg)
    monitor = WindowMonitor(cfg)
    auditor = Auditor(cfg)
    codes = cfg["market"]["codes"]
    print(f"\n=== 行情抓取自检 {now_str()} ===")
    print(f"标的：{', '.join(codes)}")
    quotes = fetcher.fetch(codes)
    if not quotes:
        print("✗ 未获取到任何行情，请检查网络或代码是否正确")
        return 2

    state = monitor.check()
    print(f"窗口监控：{state.summary()}\n")

    for q in quotes.values():
        pos = engine.position(q.code)
        d = q.decimals
        print(q.summary())
        print(f"  成交额/成交量 => 分时均价 {fmt_price(q.avg_price, max(d, 3))}")
        print(f"  涨跌幅 {q.change_pct:+.2f}%  振幅 {q.amplitude_pct:.2f}%  "
              f"偏离均价 {q.deviation_from_avg_pct():+.2f}%")
        print(f"  持仓成本 {fmt_price(pos['cost'], d)}  盈亏 {q.pnl_pct(pos['cost']):+.2f}%  "
              f"可卖 {int(pos['sellable_qty'])} 股")
        print(f"  价格精度 {d} 位小数（最小报价单位 {fmt_price(price_tick(d), d)}）")
        print(f"  五档：{q.bid_ask_text()}")
        signals, checks = engine.evaluate(q)
        for stype, label in (("BUY_FIRST", "正T·先买后卖"), ("SELL_FIRST", "反T·先卖后买")):
            print(f"  --- {label} 判定 ---")
            for c in checks[stype]:
                print(f"    {c.render()}")
            verdict = "触发信号" if any(s.stype == stype for s in signals) else "不触发"
            print(f"    => {verdict}")
        for sig in signals:
            print(f"  ★ 生成信号 {sig.signal_id}")
            auditor.write(Auditor.signal_row(
                sig, "SIGNAL_TRIGGER", user_choice="", dry_run=cfg["order_ui"]["dry_run"],
                remark="--once 行情自检（未进入人工确认流程）",
            ))
        probe = _probe_signal(q, engine)
        auditor.write(Auditor.signal_row(
            probe, "QUOTE_PROBE", user_choice="", dry_run=cfg["order_ui"]["dry_run"],
            remark="--once 行情自检快照",
        ))
        # dry_run 下单动作预览：确认价格框实际键入文本（ETF 应为 0.795 而非 0.80）
        probe.suggested_qty = float(cfg["trade"]["buy_qty"])
        preview = OrderExecutor(cfg, monitor, auditor, logger).plan(probe)
        print("  下单动作预览（dry_run=" + str(cfg["order_ui"]["dry_run"]) + "）：")
        for idx, step in enumerate(preview, 1):
            text = step.get("text")
            suffix = f" 键入 '{text}'" if text is not None else ""
            print(f"    {idx}. {step['name']}{suffix}")
        print()
    print(f"审计日志已写入：{auditor.path}")
    return 0


def _probe_signal(q: Quote, engine: SignalEngine) -> Signal:
    """把当前行情打包成信号结构，仅用于日志留痕（不代表触发信号）。"""
    conf = engine.cfg["signals"]["buy_first"]
    pos = engine.position(q.code)
    decimals = resolve_price_decimals(engine.cfg, q.code, q.price)
    return Signal(
        signal_id=f"PROBE-{short_code(q.code)}-{datetime.now():%H%M%S}",
        code=short_code(q.code), name=q.name, stype="BUY_FIRST", triggered_at=now_str(),
        price=q.price, avg_price=q.avg_price, prev_close=q.prev_close, day_open=q.open,
        high=q.high, low=q.low, volume=q.volume, amount=q.amount,
        amplitude_pct=q.amplitude_pct, change_pct=q.change_pct, cost=pos["cost"],
        pnl_pct=q.pnl_pct(pos["cost"]), deviation_pct=q.deviation_from_avg_pct(),
        support_price=round_price(SignalEngine._support_price(q, conf), decimals),
        quote_time=f"{q.quote_date} {q.quote_time}",
        bid_ask=q.bid_ask_text(), reasons=["行情自检记录"], suggested_price=q.price,
        suggested_qty=0, decimals=decimals,
    )


def cmd_selftest(cfg: Dict[str, Any], logger: logging.Logger, auto_confirm: bool,
                 timeout: int) -> int:
    print(f"\n=== 自检模式 {now_str()} ===")
    fetcher = SinaFetcher(cfg)
    engine = SignalEngine(cfg)
    monitor = WindowMonitor(cfg)
    auditor = Auditor(cfg)
    codes = cfg["market"]["codes"]
    quotes = fetcher.fetch(codes)
    if not quotes:
        print("✗ 行情获取失败，自检终止")
        return 2
    q = list(quotes.values())[0]
    print(f"真实行情：{q.summary()}")

    sig = engine.make_test_signal(q, "BUY_FIRST")
    sig.window_state = monitor.check().summary()
    auditor.write(Auditor.signal_row(sig, "SIGNAL_TRIGGER", user_choice="SELFTEST",
                                     dry_run=cfg["order_ui"]["dry_run"],
                                     remark="selftest 注入信号"))
    print(f"已注入测试信号：{sig.signal_id}")
    print(sig.render_detail())
    print(f"\n弹窗将显示 {timeout} 秒，{'自动点击确认' if auto_confirm else '超时后自动放弃（不点按钮即可）'}…")

    app = TraderApp(cfg, logger)
    app.suppress_toast = True
    app.root.auto_confirm_after = 3 if auto_confirm else None  # type: ignore[attr-defined]
    app.cfg["ui"]["popup_timeout_sec"] = timeout
    result_holder: Dict[str, Any] = {}

    original = app._on_popup_done

    def wrapped(signal, choice, remaining):
        result_holder["choice"] = choice
        result_holder["remaining"] = remaining
        original(signal, choice, remaining)
        app.root.after(1200 if choice == "confirm" else 300, app.shutdown)

    app._on_popup_done = wrapped  # type: ignore[method-assign]
    app.signal_queue.put(sig)
    app.run()

    print(f"\n自检结束：用户选择 = {result_holder.get('choice')}")
    print(f"审计日志：{auditor.path}")
    show_tail(auditor.path, 3)
    return 0


def show_tail(path: Path, limit: int) -> None:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            rows = list(csv.reader(fh))
        print(f"\n--- CSV 末尾 {min(limit, len(rows) - 1)} 行 ---")
        head = rows[0]
        for r in rows[-limit:]:
            pairs = [f"{head[i]}={r[i]}" for i in range(min(len(head), len(r)))
                     if head[i] in ("event", "timestamp", "signal_id", "code", "signal_type",
                                    "price", "user_choice", "exec_status", "dry_run")]
            print("  " + " | ".join(pairs))
    except Exception as exc:
        print(f"读取日志失败: {exc}")


PICK_VK_MBUTTON = 0x04  # 鼠标中键（滚轮按下）
PICK_VK_DEFAULT = 0x7B  # F12
PICK_VK_ESCAPE = 0x1B


def _pick_console_guard() -> None:
    """关闭控制台快速编辑模式。

    误点控制台窗口进入「标记」状态会让进程挂起，此时任何按键都不会被处理，
    表现为「按了键但程序毫无反应」。拾取工具必须禁用该模式。
    """
    try:
        import ctypes

        k32 = ctypes.windll.kernel32
        handle = k32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            mode.value &= ~0x0040  # 去掉 ENABLE_QUICK_EDIT_MODE
            mode.value |= 0x0080   # 补 ENABLE_EXTENDED_FLAGS，否则上一步不生效
            k32.SetConsoleMode(handle, mode)
    except Exception:
        pass


def _set_console_title(text: str) -> None:
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(text)
    except Exception:
        pass


def _pick_beep(ok: bool) -> None:
    try:
        import winsound

        winsound.Beep(1400 if ok else 500, 80 if ok else 250)
    except Exception:
        pass


def _pick_pressed(extra_vk: Optional[int]) -> bool:
    """鼠标中键 / F12 / 自定义键，任一按下即视为记录动作。"""
    keys = [PICK_VK_MBUTTON, PICK_VK_DEFAULT]
    if extra_vk:
        keys.append(int(extra_vk))
    for vk in keys:
        try:
            if win32api.GetAsyncKeyState(vk) & 0x8000:
                return True
        except Exception:
            continue
    return False


# --------------------------------------------------------------------------------------
# 下单界面坐标自动识别（读控件矩形，完全不需要按键，与 F1/F2/F8 等客户端热键零冲突）
# --------------------------------------------------------------------------------------
def _find_order_window() -> Optional[int]:
    """查找同花顺下单窗口（标题含「网上股票交易系统」）。"""
    if win32gui is None:
        return None
    cands: List[int] = []

    def _cb(h: int, _p: Any) -> bool:
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            t = win32gui.GetWindowText(h) or ""
            c = win32gui.GetClassName(h) or ""
        except Exception:
            return True
        if ("网上股票交易系统" in t) or (("交易系统" in t) and c.startswith("Afx")):
            cands.append(h)
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return None
    return cands[0] if cands else None


def _enum_child_items(hwnd: int) -> List[Dict[str, Any]]:
    """枚举窗口内所有子控件及其屏幕矩形。"""
    items: List[Dict[str, Any]] = []

    def _cb(h: int, _p: Any) -> bool:
        try:
            cls = win32gui.GetClassName(h) or ""
            txt = (win32gui.GetWindowText(h) or "").strip()
            l, t, r, b = win32gui.GetWindowRect(h)
        except Exception:
            return True
        if r - l <= 0 or b - t <= 0:
            return True
        items.append({
            "cls": cls, "text": txt, "rect": (l, t, r, b),
            "cx": (l + r) // 2, "cy": (t + b) // 2, "w": r - l, "h": b - t,
        })
        return True

    try:
        win32gui.EnumChildWindows(hwnd, _cb, None)
    except Exception:
        return items
    return items


def _root_window(hwnd: int) -> Optional[int]:
    """取顶层祖先窗口（GA_ROOT=2）。"""
    if win32gui is None or not hwnd:
        return None
    try:
        return win32gui.GetAncestor(int(hwnd), 2) or int(hwnd)
    except Exception:
        return int(hwnd)


# --------------------------------------------------------------------------------------
# 下单确认弹窗处理（点「买入/卖出」按钮后客户端会弹「委托确认」，再弹「提示」）
# --------------------------------------------------------------------------------------
@dataclass
class DialogResult:
    """下单弹窗处理结果。"""
    status: str = "NO_DIALOG"          # SUBMITTED / REJECTED / CONFIRM_MISMATCH / NO_DIALOG / DECLINED
    contract_no: str = ""
    detail: str = ""
    echoed: Dict[str, str] = field(default_factory=dict)


def _dialog_pid(hwnd: int) -> int:
    if win32process is None or not hwnd:
        return 0
    try:
        _t, pid = win32process.GetWindowThreadProcessId(int(hwnd))
        return int(pid or 0)
    except Exception:
        return 0


def _process_dialogs(pid: int, min_h: int = 140, min_w: int = 160) -> List[int]:
    """列出指定进程可见的 #32770 弹窗（过滤状态条等矮窗）。"""
    if win32gui is None or not pid:
        return []
    out: List[int] = []

    def _cb(h: int, _p: Any) -> bool:
        try:
            if not win32gui.IsWindowVisible(h):
                return True
            if (win32gui.GetClassName(h) or "") != "#32770":
                return True
            _t, p = win32process.GetWindowThreadProcessId(h)
            if int(p or 0) != pid:
                return True
            l, t, r, b = win32gui.GetWindowRect(h)
            if (b - t) < min_h or (r - l) < min_w:
                return True
            out.append(h)
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(_cb, None)
    except Exception:
        return out
    return out


def _dialog_texts(hwnd: int) -> List[str]:
    """读标准对话框内子控件的文本（Static / Button 等）。"""
    return [it["text"] for it in _enum_child_items(hwnd) if it["text"]]


def _click_dialog_button(hwnd: int, labels: Tuple[str, ...]) -> Optional[str]:
    """点击对话框内含指定文案的按钮，返回被点击按钮的文本。"""
    import pyautogui as _pg
    for it in _enum_child_items(hwnd):
        if not (it["cls"] or "").lower().startswith("button"):
            continue
        txt = it["text"]
        if any(lbl in txt for lbl in labels):
            _pg.click(it["cx"], it["cy"])
            return txt
    return None


def parse_confirm_dialog(texts: List[str]) -> Dict[str, str]:
    """从「委托确认」框文本解析客户端回显的下单参数（这是最可靠的下单结果证据）。"""
    blob = re.sub(r"<[^>]+>", "", "\n".join(texts))
    out: Dict[str, str] = {}
    for key, pat in (
        ("code", r"证券代码[:：]\s*(\d+)"),
        ("strategy", r"委托策略[:：]\s*(\S+)"),
        ("price", r"委托价格[:：]\s*([\d.]+)"),
        ("qty", r"(?:买入|卖出)数量[:：]\s*(\d+)"),
    ):
        m = re.search(pat, blob)
        if m:
            out[key] = m.group(1)
    return out


def _wait_confirm_dialog(pid: int, timeout: float) -> Optional[int]:
    """等待「委托确认」框出现（含「是否确定」文案的 #32770）。"""
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        for h in _process_dialogs(pid):
            blob = "\n".join(_dialog_texts(h))
            if "是否确定" in blob or "确定以上" in blob:
                return h
        time.sleep(0.15)
    return None


def _wait_result_dialog(pid: int, exclude: int, timeout: float) -> Optional[int]:
    """等待「提示」框出现（含合同编号 / 成功 / 失败文案）。"""
    deadline = time.time() + max(0.5, timeout)
    while time.time() < deadline:
        for h in _process_dialogs(pid):
            if h == exclude:
                continue
            blob = "\n".join(_dialog_texts(h))
            if ("合同编号" in blob) or ("已成功" in blob) or ("失败" in blob) or ("废单" in blob):
                return h
        time.sleep(0.15)
    return None


def handle_order_dialogs(hwnd: int, expect: Dict[str, Any], logger: logging.Logger,
                         auto_confirm: bool = True, timeout: float = 4.0) -> DialogResult:
    """处理点按钮后的「委托确认」与「提示」弹窗。

    expect: {"code": "159325", "price": 0.795, "qty": 100}
    auto_confirm=False 时即便参数一致也点「否」，仅用于演练核对，不产生委托。
    """
    res = DialogResult()
    pid = _dialog_pid(hwnd)
    if not pid:
        res.status, res.detail = "NO_DIALOG", "取不到同花顺进程号，无法监听弹窗"
        return res

    dlg = _wait_confirm_dialog(pid, timeout)
    if dlg is None:
        res.status, res.detail = "NO_DIALOG", f"{timeout:.1f}s 内未出现委托确认框"
        logger.warning("下单弹窗：%s", res.detail)
        return res

    texts = _dialog_texts(dlg)
    echoed = parse_confirm_dialog(texts)
    res.echoed = echoed
    logger.info("委托确认框回显：%s", echoed)

    def _norm_code(v: str) -> str:
        return re.sub(r"\D", "", v or "")

    mismatch: List[str] = []
    if expect.get("code") and _norm_code(str(expect["code"])) and \
            _norm_code(str(expect["code"])) != _norm_code(echoed.get("code", "")):
        mismatch.append(f"代码 {echoed.get('code')}≠{expect['code']}")
    if expect.get("price") is not None:
        try:
            if abs(float(echoed.get("price", "nan")) - float(expect["price"])) > 1e-9:
                mismatch.append(f"价格 {echoed.get('price')}≠{expect['price']}")
        except Exception:
            mismatch.append(f"价格无法比对（回显 {echoed.get('price')}）")
    if expect.get("qty") is not None:
        try:
            if int(float(echoed.get("qty", "nan"))) != int(float(expect["qty"])):
                mismatch.append(f"数量 {echoed.get('qty')}≠{expect['qty']}")
        except Exception:
            mismatch.append(f"数量无法比对（回显 {echoed.get('qty')}）")

    if mismatch:
        _click_dialog_button(dlg, ("否", "取消"))
        res.status = "CONFIRM_MISMATCH"
        res.detail = "回显与期望不一致，已点「否」拦截：" + "；".join(mismatch)
        logger.error("下单弹窗：%s", res.detail)
        return res

    if not auto_confirm:
        _click_dialog_button(dlg, ("否", "取消"))
        res.status = "DECLINED"
        res.detail = "回显一致，但 auto_confirm=false，已点「否」不提交"
        logger.info("下单弹窗：%s", res.detail)
        return res

    clicked = _click_dialog_button(dlg, ("是", "确定"))
    if clicked is None:
        res.status = "NO_DIALOG"
        res.detail = "委托确认框内找不到「是」按钮"
        logger.error("下单弹窗：%s", res.detail)
        return res
    logger.info("委托确认框已点「%s」", clicked)

    tip = _wait_result_dialog(pid, dlg, timeout)
    if tip is None:
        res.status, res.detail = "SUBMITTED", "已确认提交，但未捕获结果提示框"
        return res

    tip_texts = _dialog_texts(tip)
    blob = re.sub(r"<[^>]+>", "", "\n".join(tip_texts))
    res.detail = blob.replace("\n", " ").strip()
    m = re.search(r"合同编号[:：]?\s*(\d+)", blob)
    if m:
        res.contract_no = m.group(1)
    bad = any(k in blob for k in ("失败", "废单", "拒绝", "不允许", "不在交易时间", "非交易时间"))
    res.status = "REJECTED" if bad else "SUBMITTED"
    _click_dialog_button(tip, ("确定", "关闭"))
    if res.status == "REJECTED":
        logger.error("委托结果：%s", res.detail)
    else:
        logger.info("委托结果：%s 合同编号=%s", res.detail, res.contract_no or "-")
    return res


def check_overlay_blocking(steps: List[Dict[str, Any]], target_hwnd: Optional[int]) -> List[str]:
    """检查各点击点是否被其它窗口遮挡。

    坐标取自控件矩形中心，本身是正确的；但若有窗口（安卓模拟器、聊天窗、悬浮球等）
    浮在同花顺之上，鼠标会点到那个窗口，造成误操作。返回被遮挡的步骤名。
    """
    if win32gui is None or not target_hwnd:
        return []
    root_target = _root_window(target_hwnd)
    blocked: List[str] = []
    for st in steps:
        if st.get("type") not in ("click", "click_type"):
            continue
        xy = st.get("coord") or [0, 0]
        if int(xy[0]) <= 0 or int(xy[1]) <= 0:
            continue
        try:
            h = win32gui.WindowFromPoint((int(xy[0]), int(xy[1])))
        except Exception:
            continue
        if not h:
            continue
        if _root_window(h) != root_target:
            blocked.append(st["name"])
    return blocked


# 下单窗口为买入/卖出双面板，两侧各有独立的代码/价格/数量输入框，共 8 个操作点。
ORDER_COORD_KEYS: List[str] = [
    "code_box", "price_box", "qty_box", "buy_button",
    "code_box_sell", "price_box_sell", "qty_box_sell", "sell_button",
]
ORDER_COORD_LABELS: Dict[str, str] = {
    "code_box": "买入-代码框",
    "price_box": "买入-价格框",
    "qty_box": "买入-数量框",
    "buy_button": "买入按钮",
    "code_box_sell": "卖出-代码框",
    "price_box_sell": "卖出-价格框",
    "qty_box_sell": "卖出-数量框",
    "sell_button": "卖出按钮",
}


def detect_order_coords() -> Tuple[Dict[str, List[int]], List[str], str]:
    """从下单窗口控件布局推导 8 个操作点（买入侧 4 + 卖出侧 4）。

    返回 (coords, missing, detail)。coords 只含成功识别的键；missing 为缺失键说明。
    """
    if win32gui is None:
        return {}, [], "缺少 pywin32，无法自动识别（pip install pywin32）"
    hwnd = _find_order_window()
    if not hwnd:
        return {}, [], "未找到同花顺下单窗口：请先打开「网上股票交易系统」下单界面并保持可见"
    title = win32gui.GetWindowText(hwnd)
    items = _enum_child_items(hwnd)
    if not items:
        return {}, [], f"下单窗口「{title}」内未枚举到控件（可能被最小化）"

    edits = [i for i in items if i["cls"] == "Edit" and i["w"] >= 20 and i["h"] >= 8]
    by_text: Dict[str, Dict[str, Any]] = {}
    for it in items:
        if it["text"] and it["text"] not in by_text:
            by_text[it["text"]] = it

    def _edit_right_of(label_text: str) -> Optional[Dict[str, Any]]:
        lb = by_text.get(label_text)
        if not lb:
            return None
        cand = [
            e for e in edits
            if abs(e["cy"] - lb["cy"]) <= 14 and e["rect"][0] >= lb["rect"][2] - 10
        ]
        if not cand:
            return None
        # 优先与标签同一水平线（cy 最接近），其次取最靠左的，避免误选到下方其它编辑框
        cand.sort(key=lambda e: (abs(e["cy"] - lb["cy"]), e["rect"][0]))
        return cand[0]

    coords: Dict[str, List[int]] = {}
    missing: List[str] = []

    def _find_code_edit(title_kw: str, price_e: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """定位「XX股票[F1/F2]」标题下方的证券代码输入框。"""
        code_lb = None
        for t, it in sorted(by_text.items(), key=lambda kv: kv[1]["rect"][0]):
            if title_kw in t:
                code_lb = it
                break
        if code_lb:
            below = [
                e for e in edits
                if 0 <= (e["rect"][1] - code_lb["rect"][3]) < 45
                and e["rect"][0] < code_lb["rect"][2] + 80
                and e["rect"][2] > code_lb["rect"][0]
            ]
            if below:
                return min(below, key=lambda e: e["rect"][1])
        if price_e is not None:
            ups = [
                e for e in edits
                if e["rect"][3] <= price_e["rect"][1]
                and abs(e["rect"][0] - price_e["rect"][0]) < 25
                and e["rect"][1] > price_e["rect"][1] - 120
            ]
            if ups:
                return max(ups, key=lambda e: e["rect"][3])
        return None

    def _side(prefix: str, side_cn: str, title_kw: str, btn_kw: str,
              btn_key: str, btn_label: str) -> None:
        """识别单侧（买入/卖出）的代码框、价格框、数量框与下单按钮。"""
        price_e = _edit_right_of(f"{side_cn}价格")
        qty_e = _edit_right_of(f"{side_cn}数量")
        code_e = _find_code_edit(title_kw, price_e)

        for key, ent, label in (
            (f"code_box{prefix}", code_e, "证券代码输入框"),
            (f"price_box{prefix}", price_e, f"{side_cn}价格输入框"),
            (f"qty_box{prefix}", qty_e, f"{side_cn}数量输入框"),
        ):
            if ent:
                coords[key] = [ent["cx"], ent["cy"]]
            else:
                missing.append(f"{key}({label})")

        btns = [i for i in items if i["text"] == btn_kw and i["w"] >= 100]
        if prefix:
            # 卖出侧取最靠右的按钮，避免误选到左侧「买入」
            btn = max(btns, key=lambda i: i["cx"]) if btns else None
        else:
            # 买入侧取最靠左/最靠上的按钮
            btn = min(btns, key=lambda i: (i["rect"][1], i["rect"][0])) if btns else None
        if btn:
            coords[btn_key] = [btn["cx"], btn["cy"]]
        else:
            missing.append(f"{btn_key}({btn_label})")

    _side("", "买入", "买入股票", "买入", "buy_button", "买入按钮")
    _side("_sell", "卖出", "卖出股票", "卖出", "sell_button", "卖出按钮")

    detail = (
        f"下单窗口「{title}」hwnd={hwnd}，控件 {len(items)} 个，编辑框 {len(edits)} 个"
        f"（买入侧 + 卖出侧双面板）"
    )
    return coords, missing, detail


def save_coords(config_path: Path, coords: Dict[str, List[int]]) -> bool:
    """把识别/拾取到的坐标合并写回 config.json 的 order_ui.coords。"""
    try:
        with open(config_path, "r", encoding="utf-8-sig") as f:
            raw = json.load(f)
    except Exception as e:
        print(f"✗ 读取配置失败：{e}")
        return False
    oui = raw.setdefault("order_ui", {})
    cur = oui.setdefault("coords", {})
    for k, v in coords.items():
        cur[k] = [int(v[0]), int(v[1])]
    try:
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"✗ 写入配置失败：{e}")
        return False
    return True


def cmd_detect(cfg: Dict[str, Any], config_path: Path, write: bool = False) -> int:
    """自动识别下单界面 8 个操作点（买入侧 4 + 卖出侧 4）；write=True 时写入配置。"""
    coords, missing, detail = detect_order_coords()
    print("\n=== 下单界面坐标自动识别 ===")
    print(f"  {detail}")
    if not coords:
        print("✗ 未能识别任何坐标。")
        return 2
    order = ORDER_COORD_KEYS
    label = ORDER_COORD_LABELS
    for k in order:
        if k in coords:
            print(f"  ✔ {label[k]:<12} {k:<12} = [{coords[k][0]}, {coords[k][1]}]")
    if missing:
        print("  ✗ 未识别：" + "、".join(missing))
        print("    可改用 `python main.py --pick --manual` 手动悬停记录这些点。")
    probe = [{"name": label[k], "type": "click", "coord": coords[k]}
             for k in order if k in coords]
    blocked = check_overlay_blocking(probe, _find_order_window())
    if blocked:
        print("  ⚠ 遮挡警告：以下位置当前被其它窗口盖住，真实点击会落到那个窗口上 -> "
              + "、".join(blocked))
        print("    请先把同花顺下单窗口切到最前，或移开/最小化遮挡窗口（如安卓模拟器、聊天窗）。")
    if not write:
        print("\n（仅展示，未写入配置；加 --detect --write 写入）")
        return 0
    if missing:
        print("\n存在缺失项，为安全起见不写入不完整坐标。")
        return 2
    if save_coords(config_path, coords):
        print(f"\n✔ 已写入 {config_path.name} 的 order_ui.coords")
        return 0
    return 2


def cmd_pick(cfg: Dict[str, Any], config_path: Path, extra_vk: Optional[int] = None,
             dwell_sec: float = 1.5) -> int:
    """坐标拾取：鼠标移到控件上保持静止约 1.5 秒即自动记录（也可按 F12/中键），ESC 中止。

    之所以不用 F 键：同花顺下单界面把 F1/F2/F8 等键注册为功能热键（控件标题可见
    「买入股票[F1]」「卖出股票[F2]」），按 F8 会同时触发客户端动作且拾取拿不到按键。
    改为「悬停静止自动记录」后完全不需要按键，与交易界面零冲突。
    """
    if win32api is None:
        print("✗ 缺少 pywin32，无法拾取坐标（pip install pywin32）")
        return 2
    _pick_console_guard()
    names = [
        ("code_box", "左侧【买入】面板-证券代码输入框"),
        ("price_box", "左侧【买入】面板-买入价格输入框"),
        ("qty_box", "左侧【买入】面板-买入数量输入框"),
        ("buy_button", "左侧【买入】按钮"),
        ("code_box_sell", "右侧【卖出】面板-证券代码输入框"),
        ("price_box_sell", "右侧【卖出】面板-卖出价格输入框"),
        ("qty_box_sell", "右侧【卖出】面板-卖出数量输入框"),
        ("sell_button", "右侧【卖出】按钮"),
    ]
    still_need = max(0.3, float(dwell_sec))
    print("\n=== 坐标拾取（悬停自动记录）===")
    print(f"用法：把鼠标移到目标控件上，保持静止约 {still_need:g} 秒，听到「嘀」一声即记录成功。")
    print("备用：按 F12 或鼠标中键也可立即记录（若某些环境下按键无效，用悬停方式即可）。")
    print("中止：按 ESC（或直接关闭本窗口），中止不会修改配置。")
    print("提示：不要用 F1/F2/F8 等 F 键，同花顺已把它们绑定为买入/卖出等功能热键。")
    print("注意：同花顺窗口需处于最大化且显示比例与平时一致（建议系统缩放 100%）。\n")
    recorded: Dict[str, List[int]] = {}
    idx = 0
    while idx < len(names):
        code, label = names[idx]
        print(f"  请把鼠标移到【{label}】，停住不动 {still_need:g} 秒"
              f"（{idx + 1}/{len(names)}，ESC 中止）")
        _set_console_title(f"[坐标拾取 {idx + 1}/{len(names)}] {label} —— 悬停静止即记录")
        lx, ly = win32api.GetCursorPos()
        still_since = None
        armed = True
        prev_key = _pick_pressed(extra_vk)
        while True:
            try:
                if win32api.GetAsyncKeyState(PICK_VK_ESCAPE) & 0x8000:
                    _set_console_title("坐标拾取已中止")
                    _pick_beep(False)
                    print("\n已中止，配置未修改。")
                    return 1
            except Exception:
                pass

            cur_key = _pick_pressed(extra_vk)
            x, y = win32api.GetCursorPos()
            x, y = int(x), int(y)
            moved = abs(x - lx) > 2 or abs(y - ly) > 2
            if moved:
                lx, ly = x, y
                still_since = None
                armed = True
            elif still_since is None:
                still_since = time.time()

            key_hit = bool(cur_key and not prev_key)
            prev_key = cur_key
            dwell_hit = bool(
                armed and still_since is not None and (time.time() - still_since) >= still_need
            )

            if key_hit or dwell_hit:
                if x <= 2 and y <= 2:
                    print("    ✗ 坐标为屏幕左上角 (0,0)，疑似未对准控件，请重新悬停。")
                    _pick_beep(False)
                    still_since = time.time()
                    armed = False
                    prev_key = _pick_pressed(extra_vk)
                    time.sleep(0.2)
                    continue
                recorded[code] = [x, y]
                _pick_beep(True)
                print(f"    ✔ {code} = [{x}, {y}]  （{idx + 1}/{len(names)}）")
                idx += 1
                time.sleep(0.25)
                break
            time.sleep(0.05)

    with open(config_path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    raw.setdefault("order_ui", {}).setdefault("coords", {}).update(recorded)
    with open(config_path, "w", encoding="utf-8") as fh:
        json.dump(raw, fh, ensure_ascii=False, indent=2)
    print(f"\n已写入 {config_path}")
    print(json.dumps(recorded, ensure_ascii=False, indent=2))
    print("提示：确认这些坐标在真实下单窗口中可用；切换分辨率/缩放后需要重新拾取。")
    return 0


# --------------------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------------------
def cmd_test_order(cfg: Dict[str, Any], logger: logging.Logger, side: str = "buy",
                   code: str = "", price: Optional[float] = None, qty: Optional[int] = None,
                   submit: bool = False) -> int:
    """下单界面演练：真实点击输入框并填写代码/价格/数量，默认【不点下单按钮】。

    submit=False 时不会产生任何委托，只验证「坐标是否对准控件、输入是否落到正确面板」；
    submit=True 才会点击买入/卖出按钮，属于真实下单，必须在交易时段且由用户显式授权。
    """
    ui = cfg.get("order_ui", {})
    if not code:
        codes = cfg.get("market", {}).get("codes") or []
        code = str(codes[0]) if codes else ""
    code = str(code).strip()
    digits = "".join(ch for ch in code if ch.isdigit()) or code

    hwnd = _find_order_window()
    if not hwnd:
        print("✘ 未找到「网上股票交易系统5.0」下单窗口，请先在同花顺里打开下单界面")
        return 1
    title = win32gui.GetWindowText(hwnd)
    print(f"下单窗口：「{title}」hwnd={hwnd}")

    try:
        import pyautogui
    except ImportError:
        print("✘ 缺少 pyautogui：pip install pyautogui")
        return 1
    if ui.get("dpi_aware", True):
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            pass
    pyautogui.FAILSAFE = True
    pyautogui.PAUSE = 0.05

    if ui.get("bring_to_front", True):
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            win32gui.SetForegroundWindow(hwnd)
        except Exception:
            try:
                win32gui.SetWindowPos(hwnd, win32con.HWND_TOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
                win32gui.SetWindowPos(hwnd, win32con.HWND_NOTOPMOST, 0, 0, 0, 0,
                                      win32con.SWP_NOMOVE | win32con.SWP_NOSIZE)
            except Exception:
                pass
        time.sleep(0.6)

    live, missing, detail = detect_order_coords()
    if missing:
        print("✘ 坐标识别不完整：" + "、".join(missing) + (f"（{detail}）" if detail else ""))
        return 1
    coords = dict(ui.get("coords") or {})
    coords.update({k: v for k, v in live.items() if v})
    print("实时定位坐标：" + json.dumps(coords, ensure_ascii=False))

    is_buy = side == "buy"
    sfx = "" if is_buy else "_sell"
    side_cn = "买入" if is_buy else "卖出"
    steps = [
        {"name": f"点击{side_cn}侧证券代码框", "coord": coords.get("code_box" + sfx), "text": digits},
        {"name": f"点击{side_cn}侧价格框", "coord": coords.get("price_box" + sfx), "text": None},
        {"name": f"点击{side_cn}侧数量框", "coord": coords.get("qty_box" + sfx), "text": None},
    ]

    if price is None:
        price = 0.0
        try:
            quotes = SinaFetcher(cfg).fetch([code])
            q = quotes.get(code)
            if q is None and quotes:
                q = next(iter(quotes.values()))
            if q is not None:
                price = float(q.price or 0.0)
                print(f"现价取自行情接口：{q.code} {price}")
        except Exception as exc:
            print(f"（行情接口取价失败：{exc}）")
        if not price:
            print(f"✘ 取不到 {code} 现价，请用 --price 指定")
            return 1
    decimals = resolve_price_decimals(cfg, code, float(price))
    price_text = fmt_price(float(price), decimals)
    if qty is None:
        qty = int((cfg.get("trade", {}) or {}).get("buy_qty" if is_buy else "sell_qty", 100) or 100)
    qty_text = str(int(qty))
    steps[1]["text"] = price_text
    steps[2]["text"] = qty_text

    blocked = check_overlay_blocking(steps, hwnd)
    if blocked:
        print("✘ 以下点被其它窗口遮挡，已中止（请先把同花顺下单窗口切到最前）：" + "、".join(blocked))
        return 2

    interval = float(ui.get("type_interval_sec", 0.03))
    delay = max(0.05, float(ui.get("action_delay_ms", 350)) / 1000.0)
    print(f"\n=== 开始演练：{side_cn} {code} 价格 {price_text} 数量 {qty_text} ===")
    for st in steps:
        x, y = st["coord"]
        print(f"  → {st['name']} [{x},{y}] 输入 {st['text']}")
        pyautogui.click(x, y)
        time.sleep(delay)
        pyautogui.hotkey("ctrl", "a")
        time.sleep(0.05)
        pyautogui.press("delete")
        time.sleep(0.02)
        for _ in range(8):
            pyautogui.press("backspace")
            time.sleep(0.015)
        pyautogui.typewrite(str(st["text"]), interval=interval)
        time.sleep(delay)

    dialog_res: Optional[DialogResult] = None
    if submit:
        btn_key = "buy_button" if is_buy else "sell_button"
        bx, by = coords[btn_key]
        print(f"  → 点击{side_cn}按钮 [{bx},{by}]（真实提交）")
        pyautogui.click(bx, by)
        time.sleep(0.3)
        dialog_res = handle_order_dialogs(
            hwnd,
            {"code": code, "price": float(price), "qty": int(qty)},
            logger,
            auto_confirm=bool(ui.get("auto_confirm_order", True)),
            timeout=float(ui.get("dialog_timeout_sec", 5.0)),
        )
        mark = {"SUBMITTED": "✔", "REJECTED": "✘"}.get(dialog_res.status, "…")
        print(f"  {mark} 弹窗处理：{dialog_res.status}")
        if dialog_res.detail:
            print(f"    {dialog_res.detail}")
        if dialog_res.echoed:
            print(f"    回显核对：{dialog_res.echoed}")
        if dialog_res.contract_no:
            print(f"  📄 合同编号：{dialog_res.contract_no}")
    else:
        print(f"  ⊘ 已跳过点击{side_cn}按钮 —— 本次不产生任何委托")

    shot_path = ""
    if ui.get("screenshot", True):
        try:
            shot_dir = ensure_writable_dir(resolve_log_dir(cfg) / "shots")
            ts = time.strftime("%Y%m%d_%H%M%S")
            shot_path = str(shot_dir / f"test_order_{side}_{ts}.png")
            pyautogui.screenshot(shot_path)
            print(f"  📷 截图：{shot_path}")
        except Exception as exc:
            print(f"  （截图失败：{exc}）")

    logger.info("ORDER_TEST side=%s code=%s price=%s qty=%s submit=%s shot=%s",
                side, code, price_text, qty_text, submit, shot_path)
    if dialog_res is not None:
        logger.info("ORDER_DIALOG status=%s contract_no=%s echoed=%s detail=%s",
                    dialog_res.status, dialog_res.contract_no or "-",
                    json.dumps(dialog_res.echoed, ensure_ascii=False), dialog_res.detail)
    print("\n演练结束：输入框已填写，" + ("已点击下单按钮（真实提交）。" if submit else "未点击下单按钮，无委托产生。"))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    setup_console_encoding()
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    if raw_argv and raw_argv[0] == "--ui":
        # 打包模式：同一个 exe 以 --ui 拉起图形化配置编辑器（源码模式仍直接运行 config_ui.py）
        from config_ui import main as config_ui_main
        return int(config_ui_main(raw_argv[1:]) or 0)
    parser = argparse.ArgumentParser(
        description=f"{APP_NAME} v{VERSION} —— 同花顺做T半自动辅助工具（信号 + 人工确认下单）"
    )
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="配置文件路径")
    parser.add_argument("--once", action="store_true", help="抓取一次行情并做信号诊断后退出")
    parser.add_argument("--selftest", action="store_true", help="注入测试信号，验证弹窗/日志链路")
    parser.add_argument("--auto-confirm", action="store_true", help="自检时 3 秒后自动点击确认")
    parser.add_argument("--timeout", type=int, default=10, help="自检弹窗倒计时秒数（默认 10）")
    parser.add_argument("--detect", action="store_true",
                        help="自动识别下单界面 8 个操作点坐标（买入侧4 + 卖出侧4，读控件矩形，不需按键）")
    parser.add_argument("--write", action="store_true",
                        help="配合 --detect：识别完整时写入 config.json")
    parser.add_argument("--manual", action="store_true",
                        help="配合 --pick：跳过自动识别，强制手动悬停记录")
    parser.add_argument("--pick", action="store_true",
                        help="坐标拾取：先自动识别，失败或 --manual 时改为鼠标悬停记录")
    parser.add_argument("--pick-key", type=int, default=None,
                        help="额外指定一个记录键的虚拟键码（如 0x7B 为 F12）；鼠标中键与 F12 始终可用")
    parser.add_argument("--dwell", type=float, default=1.5,
                        help="拾取时鼠标需静止的秒数（默认 1.5，越小越容易触发）")
    parser.add_argument("--test-order", action="store_true",
                        help="下单演练：真实点击输入框填写代码/价格/数量，默认不点下单按钮（无委托产生）")
    parser.add_argument("--side", choices=["buy", "sell"], default="buy", help="配合 --test-order：买入侧或卖出侧")
    parser.add_argument("--code", default="", help="配合 --test-order：证券代码，默认取 config 第一个标的")
    parser.add_argument("--price", type=float, default=None, help="配合 --test-order：价格，默认取实时行情现价")
    parser.add_argument("--qty", type=int, default=None, help="配合 --test-order：数量，默认取 config 的 buy_qty/sell_qty")
    parser.add_argument("--submit", action="store_true",
                        help="配合 --test-order：填写后点击买入/卖出按钮（真实提交，默认关闭）")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    cfg = load_config(config_path)
    logger = setup_runtime_logger(cfg)

    print(f"{APP_NAME} v{VERSION}")
    print(f"配置文件：{config_path}")
    print(f"标的：{', '.join(cfg['market']['codes'])}    "
          f"dry_run：{cfg['order_ui']['dry_run']}    "
          f"轮询：{cfg['market']['poll_interval_sec']}s")

    if args.detect:
        return cmd_detect(cfg, config_path, write=args.write)
    if args.test_order:
        if args.submit:
            print("\n⚠ --submit 会点击买入/卖出按钮，属于真实委托提交。")
            print("  请在交易时段、并确认价格数量无误后再使用。")
        return cmd_test_order(cfg, logger, side=args.side, code=args.code,
                              price=args.price, qty=args.qty, submit=args.submit)
    if args.pick:
        if not args.manual:
            coords, missing, detail = detect_order_coords()
            if coords and not missing:
                print("\n=== 下单界面坐标自动识别成功 ===")
                print(f"  {detail}")
                for k in ORDER_COORD_KEYS:
                    print(f"  ✔ {ORDER_COORD_LABELS[k]:<12} = {coords[k]}")
                if save_coords(config_path, coords):
                    print(f"\n✔ 已写入 {config_path.name} 的 order_ui.coords")
                    print("  如需手动修正，执行：python main.py --pick --manual")
                    return 0
                return 2
            print("\n自动识别未完成，转为手动悬停记录。")
            if missing:
                print("  未识别：" + "、".join(missing))
            if detail:
                print("  " + detail)
        return cmd_pick(cfg, config_path, args.pick_key, args.dwell)
    if args.once:
        return cmd_once(cfg, logger)
    if args.selftest:
        return cmd_selftest(cfg, logger, args.auto_confirm, args.timeout)

    app = TraderApp(cfg, logger, config_path=config_path,
                    argv=argv if argv is not None else sys.argv[1:])
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
