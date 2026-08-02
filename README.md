# StockMonitor

抓取 A 股公开讨论并生成供 LLM 判读的情绪语料。默认数据源策略是：

1. 每个标的先请求东方财富（移动 JSON API，失败后再尝试东财 PC 页面）。
2. 东财成功后停止，不请求百度和新浪；明确返回当日 0 帖也视为成功。
3. 只有东财失败时才同时抓取百度和新浪，并生成同一时段的 `merged_*` 文件。

## 使用

```bash
python3 -m pip install -r requirements.txt
./run.sh --slot 早盘
```

日常入口默认使用 `--source auto`。诊断单一来源时可以强制指定：

```bash
./run.sh --source eastmoney
./run.sh --source baidu
./run.sh --source sina
```

抓取结果和 manifest 写入 `data/`。判读时优先读取 manifest 中的：

- `file`：东财成功或强制单源时的语料。
- `merged_file`：东财失败后，百度和新浪备用语料的合并结果。

manifest 同时包含轻量 `market` 快照：收盘价、当日/5日涨跌、振幅、5日量比、换手率和成交额。它们只用于给情绪提供量价背景；均线、指标、信号、板块、资金和策略分析继续由本地 `light_stock2` 负责。StockMonitor 不导入 `light_stock2` 内部模块，也不直接依赖其 SQLite 表结构。

同日多次运行请传 `--slot`。手动重新合并备用源时：

```bash
python3 merge_sources.py --date 2026-08-01 --slot 早盘
```

## 验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q .
bash -n run.sh
```

当所有启用标的都没有任何成功响应时，`daily_monitor.py` 返回非零退出码；部分成功时返回 0，并在 manifest 中保留失败详情。

## 结构化判读和历史库

让 LLM 按 [SENTIMENT_RESULT_TEMPLATE.json](SENTIMENT_RESULT_TEMPLATE.json) 生成结构化结果，随后执行：

```bash
python3 process_sentiment.py data/sentiment_result_2026-08-01_尾盘.json
```

该命令会依次校验评分范围和自然映射、写入 `data/sentiment_history.db`，并生成适合飞书的 `data/llm_report_*.md`。同一交易日和时段默认禁止覆盖；确认重跑时显式增加 `--replace`。

查询单个标的最近记录：

```bash
python3 sentiment_store.py history --code 688981 --limit 30
```
