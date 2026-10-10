import asyncio
from datetime import datetime

import pytest

from liveops.portal import extract_shift_records, portal_urls, report_source_values, trend_metrics


def _response(path: str, data: dict) -> dict:
    return {"source": "compass", "path_full": path, "data": data}


def test_portal_urls_encode_room_and_qianchuan_account() -> None:
    urls = portal_urls("123 456", "789")
    assert "live_room_id=123+456" in urls["detail"]
    assert "aavid=789" in urls["qianchuan"]
    assert urls["pro"].endswith("#data")
    assert "btm_show_id" not in " ".join(urls.values())
    assert urls["base"] == "https://compass.jinritemai.com/screen/shop/main?live_room_id=123+456"


def test_shift_response_converts_epoch_to_local_timezone() -> None:
    from liveops.portal import APP_TIMEZONE

    start = datetime(2026, 9, 28, 7, 0, tzinfo=APP_TIMEZONE).timestamp()
    end = datetime(2026, 9, 28, 10, 0, tzinfo=APP_TIMEZONE).timestamp()
    rows = extract_shift_records(
        [
            _response(
                "/compass_api/shop/live/live_screen/anchor_shift_list",
                {
                    "data": {
                        "anchor_cnt": 1,
                        "info_list": [
                            {
                                "name": "主播甲",
                                "anchor_id": 8,
                                "live_start_ts": start,
                                "live_end_ts": end,
                            }
                        ],
                    },
                },
            )
        ]
    )
    assert rows[0]["host_name"] == "主播甲"
    assert rows[0]["start_at"].hour == 7
    assert rows[0]["end_at"].hour == 10


def test_trends_filter_by_shift_and_aggregate_selected_series() -> None:
    from liveops.portal import APP_TIMEZONE

    start = datetime(2026, 9, 28, 6, 0, tzinfo=APP_TIMEZONE)
    in_range = datetime(2026, 9, 28, 6, 1, tzinfo=APP_TIMEZONE).timestamp()
    outside = datetime(2026, 9, 28, 5, 59, tzinfo=APP_TIMEZONE).timestamp()
    response = _response(
        "/business_api/shop/live_room/flow/trend_analysis_v2",
        {
            "trend_map": {
                "selected": [
                    {"point_name": "pay_amt", "date_time": in_range, "vertical": 1200},
                    {"point_name": "pay_amt", "date_time": outside, "vertical": 9000},
                    {"point_name": "pay_ucnt", "date_time": in_range, "vertical": 7},
                    {"point_name": "watch_ucnt", "date_time": in_range, "vertical": 350},
                    {"point_name": "incr_fans_cnt", "date_time": in_range, "vertical": 3},
                ]
            }
        },
    )
    result = trend_metrics([response], start, datetime(2026, 9, 28, 7, 0, tzinfo=APP_TIMEZONE))
    assert result["gmv_trend"] == 12
    assert result["paid_users"] == 7
    assert result["viewers"] == 350
    assert result["new_followers"] == 3


def test_cumulative_qianchuan_values_require_start_baseline() -> None:
    from liveops.portal import APP_TIMEZONE

    sources = [
        {
            "source": "qianchuan",
            "totals": {
                "total_pay_order_coupon_amount_realtime_for_roi2": {"Value": 2200},
                "total_ecom_platform_subsidy_amount_realtime_for_roi2": {"Value": 300},
                "stat_cost_for_roi2": {"Value": 9000},
                "live_show_count_for_roi2": {"Value": 40000},
            },
        }
    ]
    kwargs = {
        "responses": sources,
        "schedule_start": datetime(2026, 9, 28, 6, 0, tzinfo=APP_TIMEZONE),
        "schedule_end": datetime(2026, 9, 28, 10, 0, tzinfo=APP_TIMEZONE),
        "host_name": "主播甲",
        "include_daily_exposure": True,
    }
    without_baseline = report_source_values(**kwargs, baseline=None)
    with_baseline = report_source_values(
        **kwargs,
        baseline={
            "qianchuan": {
                "total_pay_order_coupon_amount_realtime_for_roi2": 1000,
                "total_ecom_platform_subsidy_amount_realtime_for_roi2": 100,
                "stat_cost_for_roi2": 4500,
                "live_show_count_for_roi2": 18000,
            }
        },
    )
    assert without_baseline["qianchuan_coupon"] is None
    assert without_baseline["spend"] is None
    assert without_baseline["exposure"] is None
    assert with_baseline["qianchuan_coupon"] == 1400
    assert with_baseline["spend"] == 4500
    assert with_baseline["exposure"] == 22000


def test_anchor_report_unwraps_real_response_and_converts_fen_to_yuan() -> None:
    from liveops.portal import snapshot_values

    responses = [
        _response(
            "/compass_api/shop/live/live_screen/anchor_lidar_detail",
            {
                "data": {
                    "info_list": [
                        {"name": "主播甲", "pay_amt": {"unit": "price", "value": 1_032_870}}
                    ]
                }
            },
        )
    ]
    assert snapshot_values(responses, "主播甲")["host_gmv"] == 10_328.7


def test_report_uses_values_matching_requested_screen_cards() -> None:
    from liveops.portal import APP_TIMEZONE

    responses = [
        _response(
            "/compass_api/shop/live/live_screen/core_data",
            {
                "core_data": [
                    {"index_name": "pay_ucnt", "value": {"unit": "number", "value": 12}},
                    {
                        "index_name": "product_click_pay_ucnt_ratio",
                        "value": {"unit": "ratio", "value": 0.12},
                    },
                    {
                        "index_name": "interact_watch_ucnt_ratio",
                        "value": {"unit": "ratio", "value": 0.08},
                    },
                ],
                "pay_amt_all": {"unit": "price", "value": 125000},
            },
        ),
        _response(
            "/compass_api/shop/live/basic_live_screen/base_info",
            {
                "online_user_ucnt": {"unit": "number", "value": 300},
                "incr_fans_cnt": {"unit": "number", "value": 6},
                "avg_watch_duration": {"unit": "time", "value": 95},
            },
        ),
        _response(
            "/business_api/shop/live_room/flow/gmv_interaction",
            {"gmv_change": [{"index_name": "商品点击人数", "value": 101}]},
        ),
        {
            "source": "qianchuan",
            "totals": {
                "total_pay_order_coupon_amount_realtime_for_roi2": {"Value": 100},
                "total_ecom_platform_subsidy_amount_realtime_for_roi2": {"Value": 20},
                "stat_cost_for_roi2": {"Value": 250},
                "live_show_count_for_roi2": {"Value": 5000},
                "total_live_pay_order_gpm_realtime_for_roi2": {"Value": 2300},
            },
        },
    ]
    result = report_source_values(
        responses=responses,
        schedule_start=datetime(2026, 10, 1, 6, tzinfo=APP_TIMEZONE),
        schedule_end=datetime(2026, 10, 1, 10, tzinfo=APP_TIMEZONE),
        host_name="主播甲",
        baseline={"source": "first_shift_zero", "qianchuan": {}},
        include_daily_exposure=True,
    )
    assert result["gmv"] == 1250
    assert result["qianchuan_coupon"] == 120
    assert result["paid_users"] == 12
    assert result["product_click_users"] == 101
    assert result["product_click_conversion"] == 0.12
    assert result["viewers"] == 300
    assert result["gpm"] == 2300
    assert result["interaction_rate"] == 0.08
    assert result["new_followers"] == 6
    assert result["stay_seconds"] == 95
    assert result["spend"] == 250
    assert result["exposure"] == 5000
    assert result["daily_exposure"] == 5000


def _payment_response(**overrides):
    from liveops.portal import QIANCHUAN_OVERVIEW, QIANCHUAN_REQUIRED_METRICS

    return {
        "source": "qianchuan",
        "status": 200,
        "status_code": 0,
        "data_set_key": QIANCHUAN_OVERVIEW,
        "metric_names": list(QIANCHUAN_REQUIRED_METRICS),
        "totals": {key: {"Value": 0} for key in QIANCHUAN_REQUIRED_METRICS},
        **overrides,
    }


async def test_readiness_waits_for_future_payment_response_after_flush_returns():
    from liveops.portal import ResponseCollector

    collector = ResponseCollector()
    await collector.flush()  # No pending tasks does not mean the metrics have arrived.
    waiter = asyncio.create_task(collector.wait_for_data("qianchuan", timeout=1))
    await asyncio.sleep(0)
    collector.responses.append(_payment_response(totals={"stat_cost_for_roi2": {"Value": 1}}))
    collector.changed.set()
    await asyncio.sleep(0)
    assert not waiter.done()
    collector.responses.append(_payment_response())  # All zeros are valid values.
    collector.changed.set()
    await waiter


async def test_readiness_rejects_wrong_mode_business_error_null_and_previous_snapshot():
    from liveops.portal import ResponseCollector, missing_page_metrics

    wrong_mode = _payment_response(metric_names=["stat_cost_for_overall_roi2"])
    assert "消耗" in missing_page_metrics([wrong_mode], "qianchuan")
    business_error = _payment_response(status_code=11001)
    assert "消耗" in missing_page_metrics([business_error], "qianchuan")
    invalid = _payment_response()
    invalid["totals"]["stat_cost_for_roi2"]["Value"] = None
    assert "消耗" in missing_page_metrics([invalid], "qianchuan")
    invalid["totals"]["stat_cost_for_roi2"]["Value"] = float("nan")
    assert "消耗" in missing_page_metrics([invalid], "qianchuan")
    collector = ResponseCollector()
    collector.responses.append(_payment_response())
    with pytest.raises(RuntimeError, match="消耗"):
        await collector.wait_for_data("qianchuan", since=1, timeout=0.01)


def test_top_card_exposure_is_not_overwritten_by_later_funnel_or_failed_response():
    from liveops.portal import qianchuan_snapshot

    top = _payment_response()
    top["totals"]["live_show_count_for_roi2"]["Value"] = 9000
    funnel = _payment_response(data_set_key="board_roi2_funnel_core")
    funnel["totals"]["live_show_count_for_roi2"]["Value"] = 123
    failed = _payment_response(status_code=11001)
    assert qianchuan_snapshot([top, funnel, failed])["live_show_count_for_roi2"] == 9000


def test_legacy_cost_is_not_used_as_payment_cost_baseline():
    from liveops.portal import APP_TIMEZONE

    current = _payment_response()
    current["totals"]["stat_cost_for_roi2"]["Value"] = 250
    result = report_source_values(
        responses=[current],
        schedule_start=datetime(2026, 10, 8, 6, tzinfo=APP_TIMEZONE),
        schedule_end=datetime(2026, 10, 8, 10, tzinfo=APP_TIMEZONE),
        host_name="主播",
        baseline={"qianchuan": {"stat_cost_for_overall_roi2": 200}},
        include_daily_exposure=False,
    )
    assert result["spend"] is None
    assert "stat_cost_for_roi2" in result["baseline_missing"]
