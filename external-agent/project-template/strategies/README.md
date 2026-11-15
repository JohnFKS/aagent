# strategies/ — 策略文件目录

- 一个策略一个 `.py` 文件,命名建议:`<策略名>_<标的代码>.py`,如 `ma_cross_000001.py`、`rsi_oversold_600519.py`;
- 文件名里的代码用 6 位 A 股代码即可;策略内部使用 order_book_id(如 `000001.XSHE`、`600519.XSHG`);
- 只放 RQAlpha 策略源码(`init` / `handle_bar` 骨架,日线);写法约束清单见平台仓库 `external-agent/SKILL.md` 第 2 步;
- 由 agent 的文件工具直接增改;每次修改后必须重新调用 `platform_validate_code` 校验到 0 error,再进入回测。
