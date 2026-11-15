"""Layer 2:板块涨跌停价规则(四舍五入到分,模拟交易所规则)。

规则口径(与 docs/design.md 1.3 节一致):
- 主板(60xxxx.XSHG / 000xxx/001xxx/002xxx/003xxx.XSHE):±10%;
- 创业板(300xxx/301xxx.XSHE):±20%(2020-08-24 注册制改革起),此前 ±10%;
- 科创板(688xxx.XSHG):±20%;
- 北交所(以 .BJ 结尾,或 8xxxxx / 92xxxx):±30%;
- ST:主板 ±5%;创业板/科创板 ST 仍 ±20%;北交所不适用 ±5% 规则(仍 ±30%)。
- 上市首日,以及创业板/科创板注册制新股上市后前 5 个交易日,无涨跌幅限制 → 返回 None,
  调用方(bundle 构建)据此不设有效涨跌停(bundle 中以超宽占位值写入,见 bundle.py 注释)。

价格四舍五入到分(Decimal ROUND_HALF_UP),与交易所"四舍五入至 0.01 元"的规则一致。
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Iterable, Sequence

__all__ = [
    "PRICE_LIMIT_RATIO_CHANGE_DATE",
    "board_of",
    "limit_ratio",
    "price_limits",
    "round_to_cent",
]

# 创业板注册制改革日:此后创业板新股/存量股涨跌幅为 ±20%
PRICE_LIMIT_RATIO_CHANGE_DATE = date(2020, 8, 24)

# 注册制新股上市后无涨跌幅限制的交易日数(含上市首日)
NEW_BOARD_NO_LIMIT_TRADING_DAYS = 5

_CENT = Decimal("0.01")


def round_to_cent(value: float) -> float:
    """按交易所规则四舍五入到分(Decimal ROUND_HALF_UP,避免二进制浮点误差)。"""
    return float(Decimal(str(value)).quantize(_CENT, rounding=ROUND_HALF_UP))


def _code_of(order_book_id: str) -> str:
    """取六位数字代码部分('600519.XSHG' → '600519','.BJ' 结尾同样适用)。"""
    return order_book_id.split(".")[0]


def board_of(order_book_id: str) -> str:
    """识别板块:'main'(主板) / 'gem'(创业板) / 'star'(科创板) / 'bj'(北交所)。

    无法识别时保守按主板处理(±10%)。
    """
    oid = order_book_id.upper()
    code = _code_of(oid)
    if oid.endswith(".BJ") or code.startswith("8") or code.startswith("92") or code.startswith("4"):
        return "bj"
    if code.startswith("688"):
        return "star"
    if code.startswith("300") or code.startswith("301"):
        return "gem"
    return "main"


def limit_ratio(
    order_book_id: str,
    *,
    date: datetime | date,
    is_st: bool = False,
) -> float:
    """返回该合约在给定日期的涨跌幅限制比例(如 0.1 / 0.2 / 0.3 / 0.05)。

    创业板在 2020-08-24 之前为 ±10%,此后 ±20%;ST 只对主板减半(±5%)。
    """
    d = date.date() if isinstance(date, datetime) else date
    board = board_of(order_book_id)
    if board == "bj":
        return 0.30
    if board == "star":
        return 0.20
    if board == "gem":
        # 创业板注册制改革(2020-08-24)前 ±10%
        return 0.20 if d >= PRICE_LIMIT_RATIO_CHANGE_DATE else 0.10
    # 主板
    return 0.05 if is_st else 0.10


def _days_since_listed(
    d: datetime | date,
    listed_date: datetime | date,
    trading_dates: Sequence[datetime | date] | None,
) -> int | None:
    """d 为上市后第几个交易日(0-based)。依赖升序交易日历或首日相等判断。"""
    dd = d.date() if isinstance(d, datetime) else d
    ld = listed_date.date() if isinstance(listed_date, datetime) else listed_date
    if dd < ld:
        return 0
    if trading_dates is None:
        return 0 if dd == ld else None
    # 上市日之后的交易日子序列中,d 的下标即交易日序号(0-based)
    idx = None
    count = -1
    for td in trading_dates:
        td = td.date() if isinstance(td, datetime) else td
        if td < ld:
            continue
        count += 1
        if td == dd:
            idx = count
            break
        if td > dd:
            break
    return idx


def _in_no_limit_window(order_book_id: str, day_number: int | None) -> bool:
    """是否处于"无涨跌幅"窗口:上市首日;创业板/科创板前 5 个交易日。"""
    if day_number is None:
        return False
    if day_number == 0:
        return True
    board = board_of(order_book_id)
    if board in ("gem", "star"):
        return day_number < NEW_BOARD_NO_LIMIT_TRADING_DAYS
    return False


def price_limits(
    order_book_id: str,
    prev_close: float,
    *,
    date: datetime | date,
    is_st: bool = False,
    listed_date: datetime | date | None = None,
    trading_dates: Sequence[datetime | date] | Iterable[datetime | date] | None = None,
) -> tuple[float, float] | None:
    """计算某合约给定交易日的 (涨停价, 跌停价)。

    :param order_book_id: 合约代码,如 '600519.XSHG' / '000001.XSHE' / '430047.BJ'
    :param prev_close: 前一交易日收盘价(真实价口径)
    :param date: 当前交易日
    :param is_st: 是否 ST(主板 ±5%,创业板/科创板不受影响)
    :param listed_date: 上市日;提供后启用"上市首日/新股前 5 交易日无涨跌幅"判定
    :param trading_dates: 升序交易日历(用于精确计算上市后第几个交易日);
        未提供时仅能识别"date == listed_date"的首日情形

    :returns: (limit_up, limit_down),已四舍五入到分;处于无涨跌幅窗口时返回 None,
        调用方据此不设置有效涨跌停(bundle 中以超宽占位值代替)。
    """
    if not prev_close or prev_close <= 0:
        raise ValueError(f"prev_close 必须为正数,收到 {prev_close!r}(order_book_id={order_book_id})")

    if listed_date is not None:
        days = _days_since_listed(
            date, listed_date, list(trading_dates) if trading_dates is not None else None
        )
        if _in_no_limit_window(order_book_id, days):
            return None

    ratio = limit_ratio(order_book_id, date=date, is_st=is_st)
    # 用 Decimal 完成乘法,避免二进制浮点在半分位上的误差(如 25.55*1.3=33.215)
    base = Decimal(str(prev_close))
    ratio_d = Decimal(str(ratio))
    up = float((base * (1 + ratio_d)).quantize(_CENT, rounding=ROUND_HALF_UP))
    down = float((base * (1 - ratio_d)).quantize(_CENT, rounding=ROUND_HALF_UP))
    return up, down
