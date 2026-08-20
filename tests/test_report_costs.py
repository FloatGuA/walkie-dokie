from datetime import datetime

from scripts.report_costs import (
    PURPOSE_COLORS,
    PURPOSES,
    aggregate,
    estimate_cost_usd,
    render_html,
)

NOW = datetime(2026, 8, 20, 12, 0, 0)


def record(
    *,
    day="2026-08-20",
    provider="deepseek",
    model="deepseek-chat",
    purpose="decide",
    platform="feishu",
    user_id="u1",
    prompt_tokens=1000,
    completion_tokens=100,
    duration_ms=900,
):
    return {
        "timestamp": f"{day}T09:30:00.000000",
        "provider": provider,
        "model": model,
        "purpose": purpose,
        "platform": platform,
        "user_id": user_id,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "duration_ms": duration_ms,
    }


def test_aggregate_sums_tokens_per_day_and_purpose():
    agg = aggregate(
        [
            record(day="2026-08-19", purpose="decide"),
            record(day="2026-08-19", purpose="decide", prompt_tokens=500),
            record(day="2026-08-20", purpose="finalize", completion_tokens=7),
        ],
        days=7,
        now=NOW,
    )

    assert agg["days"] == ["2026-08-19", "2026-08-20"]
    decide = agg["by_day"]["2026-08-19"]["decide"]
    assert decide["calls"] == 2
    assert decide["prompt_tokens"] == 1500
    assert decide["completion_tokens"] == 200
    assert decide["tokens"] == 1700
    assert "decide" not in agg["by_day"]["2026-08-20"]
    assert agg["by_day"]["2026-08-20"]["finalize"]["completion_tokens"] == 7
    assert agg["totals"]["calls"] == 3


def test_records_outside_the_window_are_dropped():
    agg = aggregate(
        [
            record(day="2026-08-20"),
            record(day="2026-08-14"),  # 窗口首日：--days 7 = 今天 + 前 6 天
            record(day="2026-08-13"),  # 窗口外
        ],
        days=7,
        now=NOW,
    )

    assert agg["days"] == ["2026-08-14", "2026-08-20"]
    assert agg["totals"]["calls"] == 2


def test_missing_usage_counts_the_call_but_not_fake_tokens():
    """拿不到 usage 的调用照样算一次调用，token 不许瞎补，还要单独报数。"""
    agg = aggregate(
        [
            record(prompt_tokens=None, completion_tokens=None),
            record(prompt_tokens=200, completion_tokens=20),
        ],
        days=7,
        now=NOW,
    )

    assert agg["totals"]["calls"] == 2
    assert agg["totals"]["prompt_tokens"] == 200
    assert agg["totals"]["completion_tokens"] == 20
    assert agg["unknown_token_calls"] == 1


def test_cost_is_estimated_from_deepseek_tokens_only():
    """claude-cli 走订阅制不按 token 计费，绝不能拿 DeepSeek 的价目去套它。"""
    agg = aggregate(
        [
            record(provider="deepseek", prompt_tokens=1_000_000, completion_tokens=0),
            record(
                provider="claude-cli",
                model="haiku",
                purpose="summarize",
                prompt_tokens=5_000_000,
                completion_tokens=5_000_000,
            ),
        ],
        days=7,
        now=NOW,
    )

    assert agg["totals"]["cost_usd"] == estimate_cost_usd("deepseek", 1_000_000, 0)
    assert agg["totals"]["cost_usd"] > 0
    assert estimate_cost_usd("claude-cli", 5_000_000, 5_000_000) == 0.0


def test_by_user_rows_are_grouped_and_sorted_by_tokens():
    agg = aggregate(
        [
            record(user_id="u1", prompt_tokens=100, completion_tokens=0),
            record(user_id="u1", prompt_tokens=100, completion_tokens=0),
            record(user_id="u2", prompt_tokens=900, completion_tokens=0),
            record(platform=None, user_id=None, prompt_tokens=1, completion_tokens=0),
        ],
        days=7,
        now=NOW,
    )

    owners = [row["owner"] for row in agg["by_user"]]
    assert owners[0] == "feishu:u2"
    assert owners[1] == "feishu:u1"
    assert owners[-1] == "unknown"
    assert agg["by_user"][1]["calls"] == 2
    assert agg["by_user"][1]["tokens"] == 200


def test_unknown_purpose_does_not_crash_aggregation():
    """purpose 是模块常量，但日志是历史数据；出现没见过的值不该让报表挂掉。"""
    agg = aggregate([record(purpose="brand_new_purpose")], days=7, now=NOW)

    assert agg["totals"]["calls"] == 1
    assert "brand_new_purpose" in agg["purposes"]


def test_purposes_keep_the_fixed_color_order():
    agg = aggregate(
        [record(purpose="merge"), record(purpose="decide")], days=7, now=NOW
    )

    # 固定色序：不许按数据里出现的先后循环分配颜色。
    assert agg["purposes"][: len(PURPOSES)] == list(PURPOSES)
    assert set(PURPOSES) <= set(PURPOSE_COLORS)


def test_empty_input_produces_an_empty_but_valid_aggregate():
    agg = aggregate([], days=7, now=NOW)

    assert agg["days"] == []
    assert agg["by_user"] == []
    assert agg["totals"] == {
        "calls": 0,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "tokens": 0,
        "cost_usd": 0.0,
    }


def test_render_html_on_empty_data_says_so_instead_of_crashing():
    html = render_html(aggregate([], days=7, now=NOW), days=7)

    assert "无数据" in html
    assert "<svg" not in html


def test_render_html_draws_one_stacked_segment_per_day_purpose_pair():
    agg = aggregate(
        [
            record(day="2026-08-19", purpose="decide"),
            record(day="2026-08-19", purpose="finalize"),
            record(day="2026-08-20", purpose="decide"),
        ],
        days=7,
        now=NOW,
    )
    html = render_html(agg, days=7)

    assert "<svg" in html
    assert html.count('class="seg"') == 3
    # 图例 5 个系列 + 每根柱顶的总量直接标签（对比度 WARN 的解除手段）。
    assert html.count('class="legend-item"') == len(PURPOSES)
    assert html.count('class="bar-total"') == 2
    for color in PURPOSE_COLORS.values():
        assert color in html
    # 单主题浅色页，背景必须显式给出。
    assert "#fcfcfb" in html
    assert "2026-08-19" in html and "2026-08-20" in html


def test_render_html_escapes_user_ids():
    """user_id 来自外部平台，直接拼进 HTML 会破坏页面结构。"""
    agg = aggregate([record(user_id="<script>x</script>")], days=7, now=NOW)
    html = render_html(agg, days=7)

    assert "<script>x</script>" not in html
    assert "&lt;script&gt;" in html
