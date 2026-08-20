"""模型调用成本报表。手动运行，标准 pytest 不收集聚合以外的部分。

用法（从仓库根运行）：
    python3 -m scripts.report_costs                                   # 近 7 天，终端表格
    python3 -m scripts.report_costs --days 30                         # 换窗口
    python3 -m scripts.report_costs --html var/logs/costs.html        # 附带单文件 HTML

数据源是 ``var/logs/model_calls.jsonl``（walkie_dokie.model_call_log 写入）。
金额只是估算：按下面的价目常量乘 token 数算出来的，不是账单。
"""

from __future__ import annotations

import argparse
import html
import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from walkie_dokie.model_call_log import MODEL_CALL_LOG_PATH

# 报表的固定 purpose 色序。绝不按数据里出现的先后循环分配——同一个 purpose 在
# 不同天的报表里必须永远是同一个颜色，否则两张报表没法对着看。
# 这 5 色已过 CVD/对比度校验（dataviz 的 validate_palette，light + surface
# #fcfcfb：全 PASS，仅"对比度 < 3:1"WARN，由图例 + 柱顶直接标签 + 汇总表解除）。
PURPOSE_COLORS = {
    "decide": "#2a78d6",
    "finalize": "#eb6834",
    "judge_confirmation": "#1baf7a",
    "summarize": "#eda100",
    "merge": "#e87ba4",
}
PURPOSES = tuple(PURPOSE_COLORS)
# 日志是历史数据，可能含今天还不存在的 purpose；给它们一个中性灰，不进固定色序。
_FALLBACK_COLOR = "#8a8880"

# 价目日期 2026-08，变价改这里。单位：人民币元 / 百万 token。
# 注意：这两个数字来自本次需求给定的现价；作者训练知识里 2025-09 调价后
# deepseek-chat 的 output 价是 ¥8/百万，跟这里不一致——真要拿这个金额对账，
# 先去 DeepSeek 定价页核一次。金额只是估算，报表其余部分不依赖它。
DEEPSEEK_INPUT_CNY_PER_MTOK = 2.0
DEEPSEEK_OUTPUT_CNY_PER_MTOK = 3.0

# 文本一律用文本色，不用系列色写字（dataviz 规则）。
_INK = "#1f1f1e"
_INK_MUTED = "#6b6a63"
_SURFACE = "#fcfcfb"


def estimate_cost_cny(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    """按 token 估算金额。

    只有 DeepSeek 是按 token 计费的；claude-cli 走的是订阅制账号，token 数有
    参考价值但没有对应单价，一律记 0，绝不拿 DeepSeek 的价目去套。
    """
    if provider != "deepseek":
        return 0.0
    return (
        prompt_tokens * DEEPSEEK_INPUT_CNY_PER_MTOK
        + completion_tokens * DEEPSEEK_OUTPUT_CNY_PER_MTOK
    ) / 1_000_000


def load_records(path: Path) -> list[dict]:
    """读 JSONL。文件不存在当作零条——第一次跑报表时它本来就还没被创建。"""
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _blank_stats() -> dict:
    return {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "tokens": 0}


def _add(stats: dict, prompt_tokens: int, completion_tokens: int) -> None:
    stats["calls"] += 1
    stats["prompt_tokens"] += prompt_tokens
    stats["completion_tokens"] += completion_tokens
    stats["tokens"] += prompt_tokens + completion_tokens


def aggregate(records, days: int, *, now: datetime | None = None) -> dict:
    """按 日×purpose 和 用户 汇总窗口内的调用。纯函数，不碰文件也不碰时钟以外的东西。

    ``days=7`` 表示今天 + 前 6 天。token 为 None（provider 没回 usage）时按 0 计入
    合计，但单独记进 ``unknown_token_calls``——报表要说得出"这份数字漏了多少"。
    """
    now = now or datetime.now()
    first_day = (now - timedelta(days=days - 1)).date()

    by_day: dict[str, dict[str, dict]] = defaultdict(dict)
    by_owner: dict[str, dict] = {}
    owner_cost: dict[str, float] = defaultdict(float)
    totals = _blank_stats()
    total_cost = 0.0
    unknown_token_calls = 0
    extra_purposes: list[str] = []

    for item in records:
        stamp = datetime.fromisoformat(item["timestamp"])
        if stamp.date() < first_day:
            continue

        prompt_tokens = item.get("prompt_tokens")
        completion_tokens = item.get("completion_tokens")
        if prompt_tokens is None or completion_tokens is None:
            unknown_token_calls += 1
        prompt_tokens = prompt_tokens or 0
        completion_tokens = completion_tokens or 0

        purpose = item["purpose"]
        if purpose not in PURPOSE_COLORS and purpose not in extra_purposes:
            extra_purposes.append(purpose)

        day = stamp.date().isoformat()
        day_bucket = by_day[day]
        if purpose not in day_bucket:
            day_bucket[purpose] = _blank_stats()
        _add(day_bucket[purpose], prompt_tokens, completion_tokens)

        platform = item.get("platform")
        user_id = item.get("user_id")
        owner = f"{platform}:{user_id}" if platform and user_id else "unknown"
        if owner not in by_owner:
            by_owner[owner] = _blank_stats()
        _add(by_owner[owner], prompt_tokens, completion_tokens)

        cost = estimate_cost_cny(item["provider"], prompt_tokens, completion_tokens)
        owner_cost[owner] += cost
        total_cost += cost
        _add(totals, prompt_tokens, completion_tokens)

    user_rows = [
        {"owner": owner, "cost_cny": owner_cost[owner], **stats}
        for owner, stats in by_owner.items()
    ]
    # unknown 永远排最后：它不是某个用户，跟真实用户混在一起排会误导。
    user_rows.sort(key=lambda row: (row["owner"] == "unknown", -row["tokens"]))

    return {
        "days": sorted(by_day),
        "by_day": dict(by_day),
        "by_user": user_rows,
        "purposes": list(PURPOSES) + extra_purposes,
        "totals": {**totals, "cost_cny": total_cost},
        "unknown_token_calls": unknown_token_calls,
    }


def _color(purpose: str) -> str:
    return PURPOSE_COLORS.get(purpose, _FALLBACK_COLOR)


def _pad(text: str, width: int, *, right: bool = False) -> str:
    """按终端显示宽度对齐：中文是双宽字符，str.ljust 会把表头排歪。"""
    fill = " " * max(width - sum(2 if ord(ch) > 0x2E80 else 1 for ch in text), 0)
    return fill + text if right else text + fill


def _fmt_int(value: int) -> str:
    return f"{value:,}"


def _fmt_cny(value: float) -> str:
    return f"¥{value:.4f}" if 0 < value < 0.01 else f"¥{value:.2f}"


# ---------------------------------------------------------------- 终端输出


def print_report(agg: dict, days: int) -> None:
    totals = agg["totals"]
    print(f"\n近 {days} 天模型调用成本（估算）")
    print("=" * 60)
    print(f"  估算金额   {_fmt_cny(totals['cost_cny'])}   （仅 DeepSeek 按 token 计价）")
    print(f"  总调用     {_fmt_int(totals['calls'])}")
    print(
        f"  总 tokens  {_fmt_int(totals['tokens'])}"
        f"   （输入 {_fmt_int(totals['prompt_tokens'])} / "
        f"输出 {_fmt_int(totals['completion_tokens'])}）"
    )
    if agg["unknown_token_calls"]:
        print(f"  其中 {agg['unknown_token_calls']} 次调用没拿到 usage，token 未计入")

    if not agg["days"]:
        print("\n窗口内没有任何模型调用记录。")
        return

    print("\n按日 × purpose")
    print(
        f"  {_pad('日期', 12)}{_pad('purpose', 20)}{_pad('调用', 6, right=True)}"
        f"{_pad('输入', 12, right=True)}{_pad('输出', 10, right=True)}"
    )
    for day in agg["days"]:
        for purpose in agg["purposes"]:
            stats = agg["by_day"][day].get(purpose)
            if not stats:
                continue
            print(
                f"  {day:<12}{purpose:<20}{stats['calls']:>6}"
                f"{_fmt_int(stats['prompt_tokens']):>12}"
                f"{_fmt_int(stats['completion_tokens']):>10}"
            )

    print("\n按用户")
    print(
        f"  {_pad('owner', 28)}{_pad('调用', 6, right=True)}"
        f"{_pad('tokens', 12, right=True)}{_pad('估算金额', 12, right=True)}"
    )
    for row in agg["by_user"]:
        print(
            f"  {row['owner']:<28}{row['calls']:>6}"
            f"{_fmt_int(row['tokens']):>12}{_fmt_cny(row['cost_cny']):>12}"
        )
    print()


# ---------------------------------------------------------------- HTML 报表

_CSS = f"""
* {{ box-sizing: border-box; }}
body {{
  margin: 0; padding: 40px 32px; background: {_SURFACE}; color: {_INK};
  font: 15px/1.55 -apple-system, "PingFang SC", "Microsoft YaHei", system-ui, sans-serif;
}}
.wrap {{ max-width: 960px; margin: 0 auto; }}
h1 {{ font-size: 20px; font-weight: 600; margin: 0 0 4px; letter-spacing: -0.01em; }}
h2 {{ font-size: 14px; font-weight: 600; margin: 40px 0 12px; }}
.sub {{ color: {_INK_MUTED}; font-size: 13px; margin: 0 0 32px; }}
.stats {{ display: flex; flex-wrap: wrap; gap: 48px; margin-bottom: 12px; }}
.stat-label {{ color: {_INK_MUTED}; font-size: 12px; letter-spacing: 0.04em; }}
.stat-value {{ font-size: 22px; font-weight: 600; font-variant-numeric: tabular-nums; }}
.hero .stat-value {{ font-size: 40px; letter-spacing: -0.02em; }}
.note {{ color: {_INK_MUTED}; font-size: 12px; margin: 4px 0 0; }}
.chart {{ position: relative; overflow-x: auto; }}
svg {{ display: block; }}
.legend {{ display: flex; flex-wrap: wrap; gap: 18px; margin: 8px 0 4px; }}
.legend-item {{ display: flex; align-items: center; gap: 6px; font-size: 12px; color: {_INK_MUTED}; }}
.swatch {{ width: 10px; height: 10px; border-radius: 2px; }}
.seg {{ cursor: default; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ text-align: right; padding: 7px 10px; border-bottom: 1px solid #ececea; }}
th:first-child, td:first-child {{ text-align: left; }}
th {{ color: {_INK_MUTED}; font-weight: 500; font-size: 12px; }}
td {{ font-variant-numeric: tabular-nums; }}
.dot {{ display: inline-block; width: 8px; height: 8px; border-radius: 2px; margin-right: 7px; }}
#tip {{
  position: fixed; pointer-events: none; opacity: 0; transition: opacity .09s;
  background: {_INK}; color: {_SURFACE}; font-size: 12px; line-height: 1.45;
  padding: 7px 10px; border-radius: 5px; white-space: nowrap; z-index: 10;
}}
.empty {{ color: {_INK_MUTED}; padding: 48px 0; text-align: center; }}
"""

_JS = """
(function () {
  var tip = document.getElementById('tip');
  document.querySelectorAll('.seg').forEach(function (el) {
    el.addEventListener('mousemove', function (e) {
      tip.textContent = el.dataset.label;
      tip.style.opacity = 1;
      tip.style.left = (e.clientX + 14) + 'px';
      tip.style.top = (e.clientY - 12) + 'px';
    });
    el.addEventListener('mouseleave', function () { tip.style.opacity = 0; });
  });
})();
"""

_PAD_L, _PAD_R, _PAD_T, _PAD_B = 66, 20, 24, 46
_PLOT_H = 260
_SLOT_W = 58
_BAR_W = 20
_GAP = 2  # 堆叠段之间的背景色间隔


def _nice_max(value: int) -> int:
    if value <= 0:
        return 1
    step = 10 ** (len(str(value)) - 1)
    return -(-value // step) * step


def _top_rounded_path(x: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    r = min(r, w / 2, h)
    return (
        f"M{x:.1f},{y + h:.1f} V{y + r:.1f} Q{x:.1f},{y:.1f} {x + r:.1f},{y:.1f} "
        f"H{x + w - r:.1f} Q{x + w:.1f},{y:.1f} {x + w:.1f},{y + r:.1f} "
        f"V{y + h:.1f} Z"
    )


def _render_chart(agg: dict) -> str:
    days = agg["days"]
    day_totals = {
        day: sum(stats["tokens"] for stats in agg["by_day"][day].values())
        for day in days
    }
    y_max = _nice_max(max(day_totals.values(), default=0))
    width = _PAD_L + _SLOT_W * len(days) + _PAD_R
    height = _PAD_T + _PLOT_H + _PAD_B
    base_y = _PAD_T + _PLOT_H

    parts = [f'<svg width="{width}" height="{height}" role="img">']

    # 网格与 y 轴刻度：一根 y 轴，只画 tokens。金额是另一个尺度，只出现在
    # 上面的 stat 和下面的表里，绝不叠成第二根轴。
    for i in range(5):
        value = y_max * i / 4
        y = base_y - _PLOT_H * i / 4
        parts.append(
            f'<line x1="{_PAD_L}" y1="{y:.1f}" x2="{width - _PAD_R}" y2="{y:.1f}" '
            f'stroke="#e8e7e4" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{_PAD_L - 10}" y="{y + 4:.1f}" text-anchor="end" '
            f'font-size="11" fill="{_INK_MUTED}">{_fmt_int(int(value))}</text>'
        )

    for index, day in enumerate(days):
        slot_x = _PAD_L + _SLOT_W * index
        x = slot_x + (_SLOT_W - _BAR_W) / 2
        total = day_totals[day]
        cursor = base_y
        present = [p for p in agg["purposes"] if agg["by_day"][day].get(p, {}).get("tokens")]
        for order, purpose in enumerate(present):
            tokens = agg["by_day"][day][purpose]["tokens"]
            full_h = _PLOT_H * tokens / y_max
            is_top = order == len(present) - 1
            y = cursor - full_h
            # 间隔从段顶部让出，不从底部——每段的底必须贴住下面一段（最底下那段
            # 贴住基线），从底部裁会让整根柱子浮在轴上方。
            draw_y = y if is_top else y + _GAP
            draw_h = max(cursor - draw_y, 0.5)
            share = tokens / total * 100 if total else 0
            label = html.escape(f"{purpose} · {_fmt_int(tokens)} tokens · {share:.0f}%")
            shape = (
                f'<path class="seg" d="{_top_rounded_path(x, draw_y, _BAR_W, draw_h)}"'
                if is_top
                else f'<rect class="seg" x="{x:.1f}" y="{draw_y:.1f}" '
                f'width="{_BAR_W}" height="{draw_h:.1f}"'
            )
            parts.append(f'{shape} fill="{_color(purpose)}" data-label="{label}"/>')
            cursor -= full_h

        parts.append(
            f'<text class="bar-total" x="{slot_x + _SLOT_W / 2:.1f}" '
            f'y="{base_y - _PLOT_H * total / y_max - 8:.1f}" text-anchor="middle" '
            f'font-size="11" fill="{_INK}">{_fmt_int(total)}</text>'
        )
        parts.append(
            f'<text x="{slot_x + _SLOT_W / 2:.1f}" y="{base_y + 18}" '
            f'text-anchor="middle" font-size="11" fill="{_INK_MUTED}">'
            f'{html.escape(day[5:])}</text>'
        )

    parts.append(
        f'<line x1="{_PAD_L}" y1="{base_y}" x2="{width - _PAD_R}" y2="{base_y}" '
        f'stroke="#d5d4d0" stroke-width="1"/>'
    )
    parts.append("</svg>")
    return "".join(parts)


def _render_legend(purposes) -> str:
    items = [
        f'<span class="legend-item"><span class="swatch" '
        f'style="background:{_color(p)}"></span>{html.escape(p)}</span>'
        for p in purposes
    ]
    return f'<div class="legend">{"".join(items)}</div>'


def _render_day_table(agg: dict) -> str:
    rows = []
    for day in agg["days"]:
        for purpose in agg["purposes"]:
            stats = agg["by_day"][day].get(purpose)
            if not stats:
                continue
            rows.append(
                f"<tr><td>{html.escape(day)}</td>"
                f'<td><span class="dot" style="background:{_color(purpose)}"></span>'
                f"{html.escape(purpose)}</td>"
                f"<td>{stats['calls']}</td>"
                f"<td>{_fmt_int(stats['prompt_tokens'])}</td>"
                f"<td>{_fmt_int(stats['completion_tokens'])}</td></tr>"
            )
    return (
        "<table><thead><tr><th>日期</th><th>purpose</th><th>调用</th>"
        "<th>输入 tokens</th><th>输出 tokens</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _render_user_table(agg: dict) -> str:
    rows = [
        f"<tr><td>{html.escape(row['owner'])}</td><td>{row['calls']}</td>"
        f"<td>{_fmt_int(row['prompt_tokens'])}</td>"
        f"<td>{_fmt_int(row['completion_tokens'])}</td>"
        f"<td>{_fmt_cny(row['cost_cny'])}</td></tr>"
        for row in agg["by_user"]
    ]
    return (
        "<table><thead><tr><th>owner</th><th>调用</th><th>输入 tokens</th>"
        "<th>输出 tokens</th><th>估算金额</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_html(agg: dict, days: int) -> str:
    """单文件报表：内联 CSS/JS，无外部依赖。刻意只有浅色单主题（本地开发工具）。"""
    totals = agg["totals"]
    generated = datetime.now().strftime("%Y-%m-%d %H:%M")

    if not agg["days"]:
        body = (
            f'<div class="empty">近 {days} 天没有任何模型调用记录——'
            "无数据可展示。</div>"
        )
    else:
        note = ""
        if agg["unknown_token_calls"]:
            note = (
                f'<p class="note">其中 {agg["unknown_token_calls"]} 次调用没拿到 '
                "usage，token 与金额未计入。</p>"
            )
        body = f"""
<div class="stats">
  <div class="hero"><div class="stat-label">估算金额</div>
    <div class="stat-value">{_fmt_cny(totals['cost_cny'])}</div></div>
  <div><div class="stat-label">总调用</div>
    <div class="stat-value">{_fmt_int(totals['calls'])}</div></div>
  <div><div class="stat-label">总 TOKENS</div>
    <div class="stat-value">{_fmt_int(totals['tokens'])}</div></div>
</div>
<p class="note">金额只估 DeepSeek（按 token 计价）；claude-cli 走订阅制，不计入金额。</p>
{note}
<h2>按日 tokens（按 purpose 堆叠）</h2>
{_render_legend(agg['purposes'])}
<div class="chart">{_render_chart(agg)}</div>
<h2>按日 × purpose</h2>
{_render_day_table(agg)}
<h2>按用户</h2>
{_render_user_table(agg)}
"""

    return f"""<!doctype html>
<html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>模型调用成本报表</title>
<style>{_CSS}</style></head>
<body><div class="wrap">
<h1>模型调用成本报表</h1>
<p class="sub">近 {days} 天 · 生成于 {generated} · 金额为估算，不是账单</p>
{body}
</div><div id="tip"></div>
<script>{_JS}</script>
</body></html>"""


def main() -> int:
    parser = argparse.ArgumentParser(description="模型调用成本报表")
    parser.add_argument("--days", type=int, default=7, help="统计窗口天数（默认 7）")
    parser.add_argument("--html", type=Path, help="额外输出单文件 HTML 报表到该路径")
    args = parser.parse_args()

    agg = aggregate(load_records(MODEL_CALL_LOG_PATH), days=args.days)
    print_report(agg, args.days)

    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(render_html(agg, args.days), encoding="utf-8")
        print(f"HTML 报表已写入 {args.html}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
