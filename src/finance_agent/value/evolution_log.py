# src/finance_agent/value/evolution_log.py
"""通用 evolution_log 组件：每轮跑完 append 关键指标到长表 CSV。

长表（一行一个指标），表头固定 7 列：
  ts, project, run_id, metric_name, metric_value, param_version, note

约定：同一 run_id 下 append 多行（每个指标一行）；run_id 留空回退为 ts。
首次或空文件自动写表头，自动建父目录，metric_value/note 自动 CSV 转义。
默认日志落 data/evolution.csv（data/ 已 gitignore，不入版本库、不碰业务表）。
"""
from __future__ import annotations

import csv
import io
import os
from datetime import datetime, timezone

HEADER = ["ts", "project", "run_id", "metric_name",
          "metric_value", "param_version", "note"]

DEFAULT_CSV_PATH = "data/evolution.csv"
PROJECT = "finance-agent"


def log_evolution(csv_path, project, metric_name, metric_value,
                  param_version="", run_id="", note=""):
    """Append one metric row to the evolution CSV. Idempotent header, append-safe."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    run_id = run_id or ts
    new = not os.path.exists(csv_path) or os.path.getsize(csv_path) == 0
    d = os.path.dirname(csv_path)
    if d:
        os.makedirs(d, exist_ok=True)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    if new:
        w.writerow(HEADER)
    w.writerow([ts, project, run_id, metric_name,
                metric_value, param_version, note])
    with open(csv_path, "a", encoding="utf-8", newline="") as f:
        f.write(buf.getvalue())
        f.flush()
    return run_id
