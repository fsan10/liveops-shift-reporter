from datetime import date, time

from liveops.formulas import build_report_metrics


def test_requested_report_calculations() -> None:
    metrics, missing = build_report_metrics(
        work_date=date(2026, 9, 1),
        start_time=time(6),
        end_time=time(10),
        host_name="测试主播B",
        promoter="测试投手C",
        source={
            "gmv": 11348,
            "qianchuan_coupon": 2956,
            "paid_users": 70,
            "product_click_users": 749,
            "viewers": 2947,
            "watch_count": 6240,
            "product_click_conversion": None,
            "watch_conversion": None,
            "gpm": 2293,
            "interaction_rate": 10.45,
            "new_followers": 19,
            "stay_seconds": 122,
            "spend": 6079,
            "exposure": 46659,
            "daily_exposure": 226157,
        },
        include_daily_exposure=True,
    )

    assert metrics["work_date"] == "2026-09-01"
    assert metrics["time_range"] == "06:00-10:00"
    assert metrics["duration_hours"] == 4
    assert metrics["overall_amount"] == 14304
    assert metrics["hourly_gmv"] == 2837
    assert metrics["avg_order_value"] == 162.11
    assert metrics["product_click_conversion"] == 0.0935
    assert metrics["watch_conversion"] == 0.0238
    assert metrics["interaction_rate"] == 0.1045
    assert metrics["overall_roi"] == 2.35
    assert metrics["cpm"] == 130.29
    assert metrics["daily_exposure"] == 226157
    assert "daily_exposure" not in missing


def test_missing_metrics_remain_empty_not_zero() -> None:
    metrics, missing = build_report_metrics(
        work_date=date(2026, 9, 1),
        start_time=time(22),
        end_time=time(2),
        host_name="主播乙",
        promoter="",
        source={"gmv": 100},
        include_daily_exposure=False,
    )
    assert metrics["duration_hours"] == 4
    assert metrics["qianchuan_coupon"] is None
    assert metrics["overall_amount"] is None
    assert "qianchuan_coupon" in missing
    assert metrics["daily_exposure"] is None
