"""平台运行日志 mod(聚宽式回测日志):注入每次平台回测,产出逐日结构化日志。

对标聚宽/米筐回测日志面板:【初始化】【开盘前】【成交】【撤单】【收盘结算】
分段输出,带交易时间戳与 INFO/WARNING 级别;**不改策略契约** —— 由平台注入,
对所有策略(Agent 生成 / 手写 / MCP)通用。

机制:
- 标准 rqalpha Mod(`load_mod()` 入口,经 config.mod 的 ``lib`` 键加载:
  ``{"platform_log": {"enabled": True, "lib": "aquant.runner.platform_log_mod",
   "log_file": <路径>}}``);
- 订阅事件总线:POST_SYSTEM_INIT / BEFORE_TRADING / TRADE / 撤单与拒单 /
  AFTER_TRADING(每日持仓收益结算日志),逐条追加到内存缓冲;
- 每日收盘后**增量写盘**(log_file),worker 收尾读取并随 stdout 哨兵行回传
  主进程(run_backtest 解析后进 BacktestResult.run_log);
- 时间戳用**回测交易日**(env.trading_dt),一眼对齐 K 线;级别 INFO/WARNING,
  与聚宽面板同构。
"""

from __future__ import annotations

from datetime import datetime

from rqalpha.const import SIDE
from rqalpha.core.events import EVENT
from rqalpha.interface import AbstractMod


def _ts(env) -> str:
    """回测交易时间戳 'YYYY-MM-DD HH:MM'。"""
    try:
        return env.trading_dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return datetime.now().strftime("%Y-%m-%d %H:%M")


def _money(v: float) -> str:
    return f"{v:,.2f}"


class PlatformLogMod(AbstractMod):
    """聚合回测运行日志(初始化/开盘前/成交/撤单/收盘结算)。"""

    def __init__(self) -> None:
        self._lines: list[str] = []
        self._env = None
        self._log_file = ""
        self._prev_total: float | None = None
        self._init_done = False

    # ---- 生命周期 --------------------------------------------------------
    def start_up(self, env, mod_config) -> None:
        self._env = env
        self._log_file = getattr(mod_config, "log_file", "")
        bus = env.event_bus
        for event_type, handler in (
            (EVENT.POST_SYSTEM_INIT, self._on_post_system_init),
            (EVENT.POST_USER_INIT, self._on_post_user_init),
            (EVENT.BEFORE_TRADING, self._on_before_trading),
            (EVENT.TRADE, self._on_trade),
            (EVENT.ORDER_CREATION_REJECT, self._on_order_reject),
            (EVENT.ORDER_CANCELLATION_PASS, self._on_order_cancel),
            (EVENT.AFTER_TRADING, self._on_after_trading),
        ):
            bus.add_listener(event_type, lambda ev, h=handler: self._safe(h, ev))

    def tear_down(self, code, exception=None) -> None:
        """回测收尾:输出汇总并落盘(异常中断时也保证已写盘内容可读)。"""
        if exception is not None:
            self._info("⚠️ 回测异常中断,以下为已发生的日志")
        self._info("回测结束")
        self._flush()

    # ---- 工具 ------------------------------------------------------------
    def _info(self, message: str) -> None:
        self._lines.append(f"{_ts(self._env)} - INFO  - {message}")

    def _warn(self, message: str) -> None:
        self._lines.append(f"{_ts(self._env)} - WARNING - {message}")

    def _divider(self) -> None:
        self._lines.append(f"{_ts(self._env)} - INFO  - " + "=" * 60)

    def _flush(self) -> None:
        if not self._log_file:
            return
        try:
            with open(self._log_file, "w", encoding="utf-8") as f:
                f.write("\n".join(self._lines))
        except OSError:
            pass  # 日志落盘失败不阻塞回测(哨兵行仍带内存缓冲由 worker 兜底)

    # ---- 持仓快照 --------------------------------------------------------
    def _positions_snapshot(self) -> tuple[list[str], float]:
        """持仓明细行 + 市值合计。"""
        env = self._env
        portfolio = env.portfolio
        rows: list[str] = []
        market_value = 0.0
        try:
            for oid, pos in dict(portfolio.positions).items():
                if pos.quantity == 0:
                    continue
                market_value += float(pos.market_value)
                inst = self._env.get_instrument(oid)
                name = inst.symbol if inst else oid
                pnl_pct = (pos.last_price / pos.avg_price - 1) * 100 if pos.avg_price else 0.0
                rows.append(
                    f"  {oid}({name}) {pos.quantity:g}股 成本 {pos.avg_price:,.3f} "
                    f"现价 {pos.last_price:,.3f} 市值 {_money(pos.market_value)} "
                    f"浮盈 {pnl_pct:+.2f}%"
                )
        except Exception as exc:  # noqa: BLE001 - 持仓快照异常不影响回测
            rows.append(f"  (持仓快照异常:{type(exc).__name__}: {exc})")
        return rows, market_value

    # ---- 事件处理(每个 handler 套安全壳:日志层异常绝不影响回测)----------
    def _safe(self, handler, event) -> None:
        try:
            handler(event)
        except Exception as exc:  # noqa: BLE001 - 日志是旁路,失败只降级不传染
            try:
                self._warn(f"(日志快照异常:{type(exc).__name__}: {exc})")
            except Exception:
                pass

    def _on_post_system_init(self, event) -> None:
        env = self._env
        self._divider()
        cash = 0.0
        try:
            cash = float(env.portfolio.cash)
        except Exception:
            pass
        self._info(f"【初始化完成】初始资金 {_money(cash)} | 交易日历与数据 bundle 就绪")
        self._info(f"【成本口径】0.2% 滑点 · 万八佣金 · 千一印花税 · T+1 · 涨跌停/成交量限制已强制启用")

    def _on_post_user_init(self, event) -> None:
        self._info("【策略初始化完成】init(context) 执行成功")

    def _on_before_trading(self, event) -> None:
        env = self._env
        portfolio = env.portfolio
        self._divider()
        rows, market_value = self._positions_snapshot()
        if rows:
            self._info(f"【开盘前】{env.trading_dt:%Y-%m-%d} 持仓检查(持仓 {len(rows)} 只)")
            self._lines.extend(rows)
        else:
            self._info(f"【开盘前】{env.trading_dt:%Y-%m-%d} 持仓:空仓(现金 {_money(portfolio.cash)})")
        self._info(f"【开盘前】总资产 {_money(portfolio.total_value)} = 现金 {_money(portfolio.cash)} + 市值 {_money(market_value)}")

    def _on_trade(self, event) -> None:
        trade = event.trade
        name = trade.instrument.symbol if trade.instrument else trade.order_book_id
        side = "买入" if trade.side == SIDE.BUY else ("卖出" if trade.side == SIDE.SELL else str(trade.side))
        amount = float(trade.last_price) * int(trade.last_quantity)
        cost = float(trade.commission) + float(trade.tax)
        self._info(
            f"【成交】{trade.order_book_id}({name}) {side} {int(trade.last_quantity)}股 "
            f"@ {float(trade.last_price):,.3f} 金额 {_money(amount)} 费用 {cost:,.2f}"
        )

    def _on_order_reject(self, event) -> None:
        # 兼容两种发布形态:订单拒绝(带 order 对象)/环境拒单(order_book_id + reason)
        order = getattr(event, "order", None)
        if order is not None:
            name = order.instrument.symbol if getattr(order, "instrument", None) else order.order_book_id
            self._warn(f"【拒单】{order.order_book_id}({name}) {order.quantity}股 未成交:"
                       f"{getattr(order, 'reject_reason', '') or '资金/涨跌停/停牌等约束'}")
        else:
            reason = getattr(event, "reason", "") or "资金/涨跌停/停牌等约束"
            self._warn(f"【拒单】{getattr(event, 'order_book_id', '?')} 未成交:{reason}")

    def _on_order_cancel(self, event) -> None:
        order = getattr(event, "order", None)
        if order is not None:
            self._warn(f"【撤单】{order.order_book_id} 剩余未成交部分已撤销")
        else:
            self._warn(f"【撤单】{getattr(event, 'order_book_id', '?')} 已撤销:"
                       f"{getattr(event, 'reason', '')}")

    def _on_after_trading(self, event) -> None:
        env = self._env
        portfolio = env.portfolio
        total = float(portfolio.total_value)
        cash = float(portfolio.cash)
        daily_ret = float(portfolio.daily_returns) * 100
        total_ret = float(portfolio.total_returns) * 100
        rows, market_value = self._positions_snapshot()
        self._info(
            f"【收盘结算】{env.trading_dt:%Y-%m-%d} 总资产 {_money(total)} "
            f"(现金 {_money(cash)} + 市值 {_money(market_value)}) "
            f"| 日收益 {daily_ret:+.2f}% | 累计 {total_ret:+.2f}%"
        )
        if rows:
            self._lines.extend(rows)
        self._prev_total = total
        self._flush()  # 每日增量落盘,中断也不丢


def load_mod():  # noqa: D103 - rqalpha ModHandler 约定入口
    return PlatformLogMod()
