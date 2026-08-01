#!/usr/bin/env python3
"""把一份 Markdown 文件推送到飞书机器人。

用法:
  export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"
  python3 send_feishu.py data/llm_report_2026-07-31.md
  python3 send_feishu.py data/llm_report_2026-07-31.md --title "股吧情绪日报 07-31"

说明:
- LLM 判读结论由人(读 data/ 里评论)写成 md,本脚本只负责推送,不调用任何大模型。
- webhook 只从环境变量 FEISHU_WEBHOOK 读取,绝不写进文件/仓库。
- 飞书 interactive 卡片发送逻辑内置于本脚本(notify_feishu),自包含,无其他脚本依赖。
"""

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path
from urllib.error import HTTPError, URLError

def notify_feishu(webhook: str, title: str, markdown_text: str) -> tuple[bool, str]:
    """发送到飞书/Lark 自定义机器人(interactive 卡片)。webhook 从环境变量/.env.local 传入。"""
    card = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {"title": {"tag": "plain_text", "content": title},
                       "template": "red"},
            "elements": [{"tag": "markdown", "content": markdown_text}],
        },
    }
    data = json.dumps(card, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        webhook, data=data, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", errors="replace")
        payload = json.loads(body)
        # 飞书成功返回 {"StatusCode":0,...} 或 {"code":0,...}
        if payload.get("StatusCode") == 0 or payload.get("code") == 0:
            return True, body
        return False, body
    except (HTTPError, URLError, TimeoutError, ValueError) as exc:
        return False, str(exc)

def _load_env_local() -> None:
    """轻量加载同目录 .env.local(仅解析 KEY=VALUE),让单独运行也能读到 webhook。
    不覆盖已存在的环境变量(命令行 export 优先)。"""
    env_path = Path(__file__).resolve().parent / ".env.local"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val

def main() -> int:
    p = argparse.ArgumentParser(description="推送 Markdown 到飞书机器人")
    p.add_argument("md_file", help="要推送的 Markdown 文件路径")
    p.add_argument("--title", default="", help="卡片标题(默认取 md 首行标题)")
    args = p.parse_args()

    path = Path(args.md_file)
    if not path.exists():
        print(f"文件不存在: {path}", file=sys.stderr)
        return 2

    text = path.read_text(encoding="utf-8")

    title = args.title
    if not title:
        # 取首个 # 标题行,去掉 # 前缀
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#"):
                title = s.lstrip("#").strip()
                break
        title = title or path.stem

    webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        _load_env_local()
        webhook = os.environ.get("FEISHU_WEBHOOK")
    if not webhook:
        print("未设置环境变量 FEISHU_WEBHOOK。请先:\n"
              '  export FEISHU_WEBHOOK="https://open.feishu.cn/open-apis/bot/v2/hook/xxxx"',
              file=sys.stderr)
        return 1

    ok, info = notify_feishu(webhook, title, text)
    print(f"[飞书] {'成功' if ok else '失败'}: {info}", file=sys.stderr)
    return 0 if ok else 1

if __name__ == "__main__":
    raise SystemExit(main())
