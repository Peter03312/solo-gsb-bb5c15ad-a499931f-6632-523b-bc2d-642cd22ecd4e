"""合规汇总查询 GET /compliance/summary 端到端测试。

使用独立分区 PS 与 2030 年限值，避免与 test_api.py 中的 P1/P2 数据互相干扰。
"""
from __future__ import annotations

import pytest

PARTITION = "PS"
WINDOW_START = "2030-06-01T00:00:00Z"
WINDOW_END = "2030-07-01T00:00:00Z"


def _summary(client, partition=PARTITION, start=WINDOW_START, end=WINDOW_END):
    return client.get(
        "/compliance/summary", params={"partition": partition, "start": start, "end": end}
    )


def _readings_and_publish(client, sid, values, expected_version):
    for i, (indicator, value) in enumerate(values.items()):
        r = client.put(
            f"/samples/{sid}/readings/{indicator}",
            json={"indicator_code": indicator, "value": value,
                  "expected_version": expected_version + i},
        )
        assert r.status_code == 200, r.text
    version = expected_version + len(values)
    r = client.post(f"/samples/{sid}/publish", json={"expected_version": version})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture(scope="module", autouse=True)
def summary_data(client):
    """构造汇总场景：A 合格、B 双指标不合格、C 先不合格后更正为合格（旧报告保留）、
    D 有读数从未发布（应被排除）。"""
    client.post("/admin/partitions", json={"code": PARTITION, "name": "汇总测试分区"})
    for code in ("turbidity", "ph"):
        client.post(f"/admin/partitions/{PARTITION}/mandatory-indicators",
                    json={"indicator_code": code})
    client.post(
        f"/admin/partitions/{PARTITION}/limits",
        json={"indicator_code": "turbidity", "max_value": 1.0,
              "valid_from": "2030-01-01T00:00:00Z", "valid_to": "2031-01-01T00:00:00Z"},
    )
    client.post(
        f"/admin/partitions/{PARTITION}/limits",
        json={"indicator_code": "ph", "min_value": 6.5, "max_value": 8.5,
              "valid_from": "2030-01-01T00:00:00Z", "valid_to": "2031-01-01T00:00:00Z"},
    )

    # A：合格
    a = client.post("/samples", json={"partition_code": PARTITION,
                                      "sampled_at": "2030-06-01T00:00:00Z"}).json()["id"]
    _readings_and_publish(client, a, {"turbidity": 0.2, "ph": 7.2}, 0)

    # B：浑浊度与 pH 双超限，不合格
    b = client.post("/samples", json={"partition_code": PARTITION,
                                      "sampled_at": "2030-06-02T00:00:00Z"}).json()["id"]
    _readings_and_publish(client, b, {"turbidity": 1.0, "ph": 9.0}, 0)

    # C：先发布不合格（v3），更正浑浊度后再发布合格（v4），汇总只计最新报告
    c = client.post("/samples", json={"partition_code": PARTITION,
                                      "sampled_at": "2030-06-03T00:00:00Z"}).json()["id"]
    first = _readings_and_publish(client, c, {"turbidity": 1.0, "ph": 7.2}, 0)
    assert first["conclusion"] == "unqualified"
    r = client.put(f"/samples/{c}/readings/turbidity",
                   json={"indicator_code": "turbidity", "value": 0.2,
                         "expected_version": 2, "request_no": "SUM-FIX-C-TURB"})
    assert r.status_code == 200
    second = client.post(f"/samples/{c}/publish", json={"expected_version": 3}).json()
    assert second["conclusion"] == "qualified"

    # D：读数齐全但从未发布 -> 不计入
    d = client.post("/samples", json={"partition_code": PARTITION,
                                      "sampled_at": "2030-06-04T00:00:00Z"}).json()["id"]
    for i, (indicator, value) in enumerate({"turbidity": 0.2, "ph": 7.2}.items()):
        client.put(f"/samples/{d}/readings/{indicator}",
                   json={"indicator_code": indicator, "value": value, "expected_version": i})

    # 窗口外样本：2030-05 与 2030-07 各一条合格报告，用于验证范围筛选
    out1 = client.post("/samples", json={"partition_code": PARTITION,
                                         "sampled_at": "2030-05-31T23:59:59Z"}).json()["id"]
    _readings_and_publish(client, out1, {"turbidity": 0.2, "ph": 7.2}, 0)
    out2 = client.post("/samples", json={"partition_code": PARTITION,
                                         "sampled_at": "2030-07-01T00:00:00Z"}).json()["id"]
    _readings_and_publish(client, out2, {"turbidity": 0.2, "ph": 7.2}, 0)

    return {"a": a, "b": b, "c": c, "d": d, "out1": out1, "out2": out2}


# ---------------------------------------------------------------- 正常汇总

def test_summary_window_counts(client, summary_data):
    r = _summary(client)
    assert r.status_code == 200, r.text
    body = r.json()
    # 区间内 4 个样本，仅 D 未发布；C 有两版报告但只计最新（合格）
    assert body["partition_code"] == PARTITION
    assert body["start"] == WINDOW_START
    assert body["end"] == WINDOW_END
    assert body["report_samples_total"] == 3
    assert body["qualified_count"] == 2       # A、C
    assert body["unqualified_count"] == 1     # B
    # 按指标超限次数（B 的浑浊度与 pH 各 1；C 的旧不合格报告不计）
    assert body["indicator_violation_counts"] == {"ph": 1, "turbidity": 1}


def test_summary_only_latest_report_counted(client, summary_data):
    """C 的旧版不合格报告不得重复计数：总数为 3 而非 4。"""
    body = _summary(client).json()
    assert body["report_samples_total"] == 3
    assert body["qualified_count"] + body["unqualified_count"] == body["report_samples_total"]


def test_summary_unpublished_sample_excluded(client, summary_data):
    # 包含 D（06-04）的更宽窗口：D 仍不应计入
    r = _summary(client, start="2030-06-04T00:00:00Z", end="2030-06-05T00:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["report_samples_total"] == 0
    assert body["qualified_count"] == 0
    assert body["unqualified_count"] == 0
    assert body["indicator_violation_counts"] == {"ph": 0, "turbidity": 0}


def test_summary_half_open_boundaries(client, summary_data):
    # 右端点开：[06-01, 06-03) 只含 A、B，C(06-03) 被排除
    r = _summary(client, start="2030-06-01T00:00:00Z", end="2030-06-03T00:00:00Z")
    body = r.json()
    assert body["report_samples_total"] == 2
    assert body["qualified_count"] == 1
    assert body["unqualified_count"] == 1
    # 起点闭：[06-02, 06-04) 含 B、C
    r = _summary(client, start="2030-06-02T00:00:00Z", end="2030-06-04T00:00:00Z")
    body = r.json()
    assert body["report_samples_total"] == 2
    assert body["qualified_count"] == 1
    assert body["unqualified_count"] == 1
    # 端点样本：窗口恰为 C 的采样时刻也能取到 C
    r = _summary(client, start="2030-06-03T00:00:00Z", end="2030-06-03T00:00:01Z")
    body = r.json()
    assert body["report_samples_total"] == 1
    assert body["qualified_count"] == 1


def test_summary_timezone_offset_normalized(client, summary_data):
    # 起点给 -08:00 偏移：2030-05-31T16:00:00-08:00 == 2030-06-01T00:00:00Z（左闭，含 A）
    r = _summary(client, start="2030-05-31T16:00:00-08:00", end=WINDOW_END)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["start"] == WINDOW_START
    assert body["report_samples_total"] == 3
    # +08:00 偏移终点：2030-06-03T08:00:00+08:00 == 2030-06-03T00:00:00Z（右开，排除 C）
    r = _summary(client, start=WINDOW_START, end="2030-06-03T08:00:00+08:00")
    assert r.status_code == 200
    body = r.json()
    assert body["end"] == "2030-06-03T00:00:00Z"
    assert body["report_samples_total"] == 2


def test_summary_empty_window_all_zeros(client, summary_data):
    r = _summary(client, start="2030-12-01T00:00:00Z", end="2030-12-02T00:00:00Z")
    assert r.status_code == 200
    body = r.json()
    assert body["report_samples_total"] == 0
    assert body["qualified_count"] == 0
    assert body["unqualified_count"] == 0
    # 无报告时各指标超限次数为零
    assert body["indicator_violation_counts"] == {"ph": 0, "turbidity": 0}


# ---------------------------------------------------------------- 错误路径

def test_summary_partition_not_found(client):
    r = _summary(client, partition="NOPE")
    assert r.status_code == 404
    err = r.json()["error"]
    assert err["code"] == "partition_not_found"


def test_summary_start_not_before_end(client):
    r = _summary(client, start="2030-06-02T00:00:00Z", end="2030-06-01T00:00:00Z")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_time_range"

    equal = _summary(client, start="2030-06-01T00:00:00Z", end="2030-06-01T00:00:00Z")
    assert equal.status_code == 422
    assert equal.json()["error"]["code"] == "invalid_time_range"


def test_summary_naive_datetime_rejected(client):
    r = _summary(client, start="2030-06-01T00:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"

    r = _summary(client, end="2030-07-01T00:00:00")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "timezone_required"


def test_summary_malformed_datetime_rejected(client):
    r = _summary(client, start="not-a-datetime")
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "invalid_datetime"


def test_summary_missing_params_unified_error(client):
    r = client.get("/compliance/summary", params={"partition": PARTITION})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "validation_error"
