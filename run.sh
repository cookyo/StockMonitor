#!/usr/bin/env bash
# 每日股吧评论抓取一键脚本(纯抓取,不打分)
#
# 流程: 读 monitor_config.json -> 抓每个标的评论(带正文) -> 落 data/{源}_*.json
#       -> 生成抓取清单 data/fetch_manifest_*.md
#
# 用法:
#   ./run.sh                          # 抓今天(东财主源；失败才抓百度+新浪)
#   ./run.sh --date 2026-07-31        # 抓指定日期
#   ./run.sh --source sina            # 用新浪股市汇(东财被限流时的备用源)
#   ./run.sh --source baidu           # 用百度股市通(聚合东财+雪球, 东财软封时首选备用)
#   ./run.sh --date 2026-07-31 --source baidu
#   ./run.sh --slot 早盘               # 同日多次跑各自成组(早/午/尾), 免互相覆盖
#   ./run.sh --slot 午盘 ; ./run.sh --slot 尾盘   # 不传 --slot 时自动用当前钟点 HHMM
#
# 抓完后: 大模型读 data/{源}_*.json,按 LLM_SENTIMENT_RUBRIC.md 的固定绝对尺判读,
#         写成 data/llm_report_{date}.md,再用 send_feishu.py 推送飞书。
#
# 飞书 webhook 只走环境变量/.env.local(已在 .gitignore,不进仓库):
#   export FEISHU_WEBHOOK="https://open.larkoffice.com/open-apis/bot/v2/hook/xxxx"

set -euo pipefail

cd "$(dirname "$0")"

# 自动加载本地机密(.env.local 已在 .gitignore 中)
if [[ -f .env.local ]]; then
  # shellcheck disable=SC1091
  set -a; source .env.local; set +a
fi

# 选择 python(项目要求 3.10;优先 python3.10,回退 python3)
PY="$(command -v python3.10 || command -v python3 || true)"
if [[ -z "${PY}" ]]; then
  echo "找不到 python3,请先安装" >&2
  exit 1
fi

ARGS=()
NEXT_IS_DATE=0
NEXT_IS_SOURCE=0
NEXT_IS_SLOT=0
SOURCE="auto"
SLOT=""
for arg in "$@"; do
  if [[ "${NEXT_IS_DATE}" == "1" ]]; then
    ARGS+=(--date "$arg"); NEXT_IS_DATE=0; continue
  fi
  if [[ "${NEXT_IS_SOURCE}" == "1" ]]; then
    ARGS+=(--source "$arg"); SOURCE="$arg"; NEXT_IS_SOURCE=0; continue
  fi
  if [[ "${NEXT_IS_SLOT}" == "1" ]]; then
    ARGS+=(--slot "$arg"); SLOT="$arg"; NEXT_IS_SLOT=0; continue
  fi
  case "$arg" in
    --date)   NEXT_IS_DATE=1 ;;
    --source) NEXT_IS_SOURCE=1 ;;
    --slot)   NEXT_IS_SLOT=1 ;;
    *)        echo "未知参数: $arg" >&2; exit 2 ;;
  esac
done
if [[ "${NEXT_IS_DATE}" == "1" || "${NEXT_IS_SOURCE}" == "1" || "${NEXT_IS_SLOT}" == "1" ]]; then
  echo "参数缺少值" >&2
  exit 2
fi

echo "==> 抓取评论(带正文) · 数据源: ${SOURCE}${SLOT:+ · 时段: ${SLOT}}"
# 安全展开空数组(兼容 macOS bash 3.2 的 set -u)
"${PY}" daily_monitor.py ${ARGS[@]+"${ARGS[@]}"}

echo "==> 完成。抓取清单见 ./data/fetch_manifest_*.md"
echo "    每个标的的实际数据文件以抓取清单中的 file/merged_file 为准"
echo "    下一步:大模型读这些评论按 LLM_SENTIMENT_RUBRIC.md 判读 -> data/llm_report_*.md -> send_feishu.py 推送。"
