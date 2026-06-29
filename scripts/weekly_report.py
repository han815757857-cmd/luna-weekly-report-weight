#!/usr/bin/env python3
"""
Luna 减脂周报 - 自动拉飞书多维表格数据，调用 Claude 分析，飞书发送报告
"""

import json
import os
import subprocess
import sys
from datetime import datetime, timedelta, timezone

# ─── 配置 ──────────────────────────────────────────────────────────────────
FEISHU_APP_ID       = os.environ["FEISHU_APP_ID"]
FEISHU_APP_SECRET   = os.environ["FEISHU_APP_SECRET"]
FEISHU_USER_OPEN_ID = os.environ["FEISHU_USER_OPEN_ID"]
ANTHROPIC_API_KEY   = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_BASE_URL  = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")

BITABLE_APP_TOKEN   = "F4gfbrlJQaGETrsQcyzc5g7KnGb"
TABLE_ID            = "tblEORaxUFLwfn93"   # 每日数据记录表

TZ8 = timezone(timedelta(hours=8))


# ─── 工具函数 ───────────────────────────────────────────────────────────────
def curl(method: str, url: str, headers: dict, body: dict | None = None) -> dict:
    """用 curl 发请求，绕过 SSL 代理问题"""
    cmd = ["curl", "-s", "-X", method, url]
    for k, v in headers.items():
        cmd += ["-H", f"{k}: {v}"]
    if body:
        cmd += ["-d", json.dumps(body)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    if result.returncode != 0:
        raise RuntimeError(f"curl 失败: {result.stderr}")
    return json.loads(result.stdout)


def get_token() -> str:
    resp = curl("POST",
        "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
        {"Content-Type": "application/json"},
        {"app_id": FEISHU_APP_ID, "app_secret": FEISHU_APP_SECRET},
    )
    token = resp.get("tenant_access_token", "")
    if not token:
        raise RuntimeError(f"获取 token 失败: {resp}")
    return token


def get_table_records(token: str) -> list[dict]:
    """拉多维表格所有记录（自动翻页）"""
    url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{BITABLE_APP_TOKEN}/tables/{TABLE_ID}/records"
    headers = {"Authorization": f"Bearer {token}"}
    records = []
    page_token = None

    while True:
        params = "?page_size=100"
        if page_token:
            params += f"&page_token={page_token}"
        resp = curl("GET", url + params, headers)
        if resp.get("code") != 0:
            raise RuntimeError(f"读表格失败: {resp}")
        items = resp["data"].get("items", [])
        records.extend(items)
        if not resp["data"].get("has_more"):
            break
        page_token = resp["data"].get("page_token")

    return records


def parse_records(records: list[dict]) -> list[dict]:
    """整理字段，过滤上周数据"""
    now = datetime.now(TZ8)
    # 上周一到上周日
    monday = now - timedelta(days=now.weekday() + 7)
    sunday = monday + timedelta(days=6)
    start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    end   = sunday.replace(hour=23, minute=59, second=59, microsecond=0)

    result = []
    for item in records:
        f = item.get("fields", {})
        date_val = f.get("日期", "")
        if not date_val:
            continue
        # 日期字段可能是毫秒时间戳（int）或 "2026-06-23" 字符串
        try:
            if isinstance(date_val, (int, float)):
                d = datetime.fromtimestamp(date_val / 1000, tz=TZ8)
            else:
                d = datetime.strptime(str(date_val)[:10], "%Y-%m-%d").replace(tzinfo=TZ8)
        except (ValueError, OSError):
            continue
        if not (start <= d <= end):
            continue

        result.append({
            "日期": str(date_val)[:10],
            "晨重": f.get("晨重", ""),
            "晚重": f.get("晚重", ""),
            "当日减重": f.get("当日减重", ""),
            "喝水量": f.get("喝水量", ""),
            "睡眠": f.get("睡眠", ""),
            "饮食场景": f.get("饮食场景", ""),
            "运动": f.get("运动", ""),
            "备注": f.get("备注", ""),
        })

    return sorted(result, key=lambda x: x["日期"])


def build_data_text(rows: list[dict], week_label: str) -> str:
    if not rows:
        return "上周无记录。"
    lines = [f"【{week_label} 每日数据】"]
    for r in rows:
        line = (
            f"{r['日期']}  晨重:{r['晨重']}kg  当日:{r['当日减重']}kg  "
            f"运动:{r['运动']}  饮食:{r['饮食场景']}  "
            f"喝水:{r['喝水量']}  睡眠:{r['睡眠']}h  备注:{r['备注']}"
        )
        lines.append(line)
    return "\n".join(lines)


def call_claude(data_text: str, week_label: str) -> str:
    """调用 Claude API 生成周报分析"""
    prompt = f"""你是 Luna 的减脂数据分析师。以下是她上周（{week_label}）的每日减脂记录：

{data_text}

Luna 当前目标：7月底达到 65kg。今天是 {datetime.now(TZ8).strftime('%Y年%m月%d日')}，当前体重约 {data_text.split('晨重:')[- 1].split('kg')[0].strip() if '晨重:' in data_text else '未知'}kg。

请用以下格式输出周报（中文，简洁有力，不空泛鸡汤）：

## Luna 减脂周报｜{week_label}

**本周体重变化**
- 开始：X kg → 结束：X kg，净变化：X kg
- 最低点：X kg（X月X日）

**本周亮点（做对了什么）**
（1-2条，具体到行为）

**本周风险（什么在拖后腿）**
（1-2条，具体到行为）

**7月目标进度**
（距离65kg还有X kg，按当前速度X周可达，是否需要调整节奏）

**下周 1 个重点行动**
（只给1条，最具体可执行的）
"""

    payload = {
        "model": "claude-opus-4-7",
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }

    cmd = [
        "curl", "-s", "-X", "POST",
        f"{ANTHROPIC_BASE_URL}/v1/messages",
        "-H", f"x-api-key: {ANTHROPIC_API_KEY}",
        "-H", "anthropic-version: 2023-06-01",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    resp = json.loads(result.stdout)

    if "content" not in resp:
        raise RuntimeError(f"Claude 调用失败: {resp}")

    return resp["content"][0]["text"]


def send_feishu_message(token: str, text: str) -> None:
    """飞书发送私信给 Luna"""
    payload = {
        "receive_id": FEISHU_USER_OPEN_ID,
        "msg_type": "text",
        "content": json.dumps({"text": text}),
    }
    cmd = [
        "curl", "-s", "-X", "POST",
        "https://open.feishu.cn/open-apis/im/v1/messages?receive_id_type=open_id",
        "-H", f"Authorization: Bearer {token}",
        "-H", "Content-Type: application/json",
        "-d", json.dumps(payload),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    resp = json.loads(result.stdout)
    if resp.get("code") != 0:
        raise RuntimeError(f"飞书发送失败: {resp}")
    print("✅ 飞书发送成功")


# ─── 主流程 ─────────────────────────────────────────────────────────────────
def main():
    now = datetime.now(TZ8)
    monday = now - timedelta(days=now.weekday() + 7)
    sunday = monday + timedelta(days=6)
    week_label = f"{monday.strftime('%m月%d日')}－{sunday.strftime('%m月%d日')}"

    print(f"📊 开始生成周报：{week_label}")

    print("1. 获取飞书 token...")
    token = get_token()

    print("2. 拉取多维表格数据...")
    raw_records = get_table_records(token)
    week_rows = parse_records(raw_records)
    print(f"   上周记录 {len(week_rows)} 条")

    if not week_rows:
        # 无数据时也发一条提醒
        msg = f"⚠️ Luna 减脂周报｜{week_label}\n\n上周没有记录数据，记得坚持打卡哦～"
        send_feishu_message(token, msg)
        return

    data_text = build_data_text(week_rows, week_label)

    print("3. 调用 Claude 生成分析...")
    report = call_claude(data_text, week_label)

    print("4. 飞书发送周报...")
    send_feishu_message(token, report)

    print("✅ 完成！")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"❌ 出错了: {e}", file=sys.stderr)
        sys.exit(1)
