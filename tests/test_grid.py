"""端到端测试：POST /grid-tracks。"""
import pytest
from fastapi.testclient import TestClient

from app.grid import solve_grid_tracks
from app.main import app
from app.models import GridItemIn, GridTracksRequest

client = TestClient(app)


def solve(columns, items=None, container_width=1000, gap=0):
    req = GridTracksRequest(
        container_width=container_width,
        gap=gap,
        columns=columns,
        items=[GridItemIn(**it) for it in (items or [])],
    )
    return solve_grid_tracks(req)


def post(body):
    return client.post("/grid-tracks", json=body)


# ---------- 固定轨道解析 ----------


def test_fixed_px_percent_tracks():
    data = solve(["200px", "50%"], container_width=1000)
    assert [t.base for t in data.tracks] == [200, 500]
    assert [t.limit for t in data.tracks] == [200, 500]
    assert [t.final for t in data.tracks] == [200, 500]
    assert data.rounds == []
    assert data.overflow == []
    assert data.free_space == 300  # 无 fr 轨道，剩余空间不分配
    assert data.total_tracks_width == 700


def test_gap_does_not_change_percentage_basis():
    # 百分比相对 container_width 解析，间距在剩余空间之外另行扣除
    data = solve(["25%", "25%"], container_width=1000, gap=20)
    assert [t.final for t in data.tracks] == [250, 250]
    assert data.used_gap == 20
    assert data.free_space == pytest.approx(480)


# ---------- fr 分配 ----------


def test_plain_fr_distribution():
    data = solve(["100px", "1fr", "2fr"], container_width=1000)
    assert [t.final for t in data.tracks] == [100, 300, 600]
    assert data.tracks[1].limit is None
    assert data.tracks[2].base == 0
    fr_rounds = [r for r in data.rounds if r.phase == "fr"]
    assert len(fr_rounds) == 1
    assert fr_rounds[0].free_space == 900
    assert fr_rounds[0].share == 300
    assert fr_rounds[0].active == [1, 2]
    assert fr_rounds[0].frozen == []
    assert data.total_tracks_width == 1000


def test_fr_with_gap():
    data = solve(["100px", "1fr", "1fr"], container_width=1000, gap=50)
    # 两道间距共 100，剩余 800 由两个 fr 平分
    assert [t.final for t in data.tracks] == [100, 400, 400]
    assert data.free_space == 800


def test_fr_freeze_when_share_below_base():
    # minmax(350px, 1fr) 基础 350；剩余空间 650 平分只得 325（增长低于 350），
    # 先固定该轨道在 350，650 全给另一轨道（0 + 650）
    data = solve(["minmax(350px, 1fr)", "1fr"], container_width=1000)
    assert [t.final for t in data.tracks] == [350, 650]
    fr_rounds = [r for r in data.rounds if r.phase == "fr"]
    assert len(fr_rounds) == 2
    assert fr_rounds[0].frozen == [0]
    assert fr_rounds[0].share == 325
    assert fr_rounds[1].frozen == []
    assert fr_rounds[1].share == 650
    assert data.total_tracks_width == 1000


def test_fr_freeze_iteration_with_three_tracks():
    # 基础 600 / 200 / 0；剩余 200，因子 1/1/1：
    # 首轮份额 66.67，固定 0、1；次轮 200 全给列 2
    data = solve(
        ["minmax(600px, 1fr)", "minmax(200px, 2fr)", "1fr"],
        container_width=1000,
    )
    finals = [t.final for t in data.tracks]
    assert finals[0] == 600
    assert finals[1] == 200
    assert finals[2] == pytest.approx(200)
    fr_rounds = [r for r in data.rounds if r.phase == "fr"]
    assert fr_rounds[0].frozen == [0, 1]
    assert fr_rounds[-1].frozen == []


def test_zero_fr_takes_no_space():
    data = solve(["minmax(0px, 0fr)", "1fr"], container_width=1000)
    assert [t.final for t in data.tracks] == [0, 1000]


def test_no_positive_free_space_keeps_bases():
    # 固定轨道已占满（超出）容器，fr 轨道保持基础尺寸
    data = solve(["600px", "600px", "1fr"], container_width=1000)
    assert [t.final for t in data.tracks] == [600, 600, 0]
    fr_rounds = [r for r in data.rounds if r.phase == "fr"]
    assert len(fr_rounds) == 1
    assert fr_rounds[0].share == 0
    assert fr_rounds[0].frozen == []


# ---------- min_content 分配 ----------


def test_min_content_equal_split():
    data = solve(
        ["100px", "minmax(100px, 400px)", "minmax(100px, 400px)", "100px"],
        items=[{"start": 1, "span": 4, "min_content": 700}],
        container_width=1000,
    )
    # 初始 100+100+100+100=400，缺口 300，两条可增长轨道各 +150
    assert [t.base for t in data.tracks] == [100, 250, 250, 100]
    mc = [r for r in data.rounds if r.phase == "min_content"]
    assert len(mc) == 1
    assert mc[0].deficit == 300
    assert mc[0].share == 150
    assert mc[0].active == [1, 2]
    assert mc[0].capped == []
    assert data.overflow == []


def test_min_content_cap_transfers_share():
    # 两条可增长轨道：上限 200（room 50）与 400（room 350），缺口 300。
    # 首轮每份额 150：列 1 触顶只拿 50 后退出；次轮剩余 250 全给列 2
    data = solve(
        ["minmax(150px, 200px)", "minmax(50px, 400px)"],
        items=[{"start": 1, "span": 2, "min_content": 500}],
        container_width=1000,
    )
    assert [t.base for t in data.tracks] == [200, 300]
    mc = [r for r in data.rounds if r.phase == "min_content"]
    assert len(mc) == 2
    assert mc[0].share == 150
    assert len(mc[0].capped) == 1
    assert mc[0].capped[0].index == 0
    assert mc[0].capped[0].limit == 200
    assert mc[0].capped[0].grew == 50
    assert mc[0].active == [1]  # 触顶后仍可增长的轨道
    assert mc[0].overflow == 250  # 转给下一轮的缺口
    assert mc[1].share == 250
    assert mc[1].capped == []
    assert mc[1].overflow == 0
    assert data.overflow == []


def test_min_content_unmet_becomes_overflow():
    data = solve(
        ["minmax(0px, 100px)", "100px"],
        items=[{"id": "wide", "start": 1, "span": 2, "min_content": 500}],
        container_width=1000,
    )
    assert data.tracks[0].base == 100
    assert len(data.overflow) == 1
    ov = data.overflow[0]
    assert ov.item_id == "wide"
    assert ov.item_index == 0
    assert ov.available == 200
    assert ov.overflow == 300


def test_items_processed_short_span_first():
    # 两个项目同缺口需求时，跨度 1 的先处理（只长列 1），跨度 2 的随后
    data = solve(
        ["minmax(0px, 500px)", "minmax(0px, 500px)"],
        items=[
            {"start": 1, "span": 2, "min_content": 200},
            {"start": 1, "span": 1, "min_content": 100},
        ],
        container_width=1000,
    )
    # 短跨项目先：列 0 = 100；长跨项目缺口 100，两列各 +50
    assert [t.base for t in data.tracks] == [150, 50]
    assert [r.item_index for r in data.rounds if r.phase == "min_content"] == [1, 0]


def test_same_span_preserves_input_order():
    data = solve(
        ["minmax(0px, 500px)", "minmax(0px, 500px)"],
        items=[
            {"id": "a", "start": 1, "span": 1, "min_content": 100},
            {"id": "b", "start": 2, "span": 1, "min_content": 100},
        ],
        container_width=1000,
    )
    assert [r.item_id for r in data.rounds if r.phase == "min_content"] == ["a", "b"]


def test_min_content_counts_internal_gaps():
    data = solve(
        ["100px", "minmax(0px, 500px)"],
        items=[{"start": 1, "span": 2, "min_content": 250}],
        container_width=1000,
        gap=50,
    )
    # 可用 = 100 + 0 + 内部间距 50 = 150，缺口 100
    assert data.tracks[1].base == 100
    assert data.rounds[0].deficit == 100


def test_grown_base_used_by_fr_phase():
    # min_content 把 minmax 轨道撑到 300，fr 阶段在该基础上分配
    data = solve(
        ["minmax(0px, 1fr)", "1fr"],
        items=[{"start": 1, "span": 1, "min_content": 300}],
        container_width=1000,
    )
    assert data.tracks[0].base == 300
    assert data.free_space == 700
    # 两轨道同因子：在各自基础（300 / 0）之上各 +350
    assert [t.final for t in data.tracks] == [650, 350]


def test_multiple_cap_rounds():
    # 三条上限依次为 120 / 150 / 无限的轨道，缺口 390：
    # 初始基础 0，份额 130 -> 列 0 触顶（+120）；剩 270 / 2 = 135 各拿，
    # 列 1 不触顶（room 150），列 2 拿 135，列 1 拿 135
    data = solve(
        ["minmax(0px, 120px)", "minmax(0px, 150px)", "minmax(0px, 1fr)"],
        items=[{"start": 1, "span": 3, "min_content": 390}],
        container_width=1000,
    )
    bases = [t.base for t in data.tracks]
    assert bases[0] == 120
    assert bases[1] == 135
    assert bases[2] == pytest.approx(135)


def test_already_satisfied_item_produces_no_round():
    data = solve(
        ["200px", "minmax(0px, 400px)"],
        items=[{"start": 1, "span": 1, "min_content": 100}],
        container_width=1000,
    )
    assert data.rounds == []
    assert data.overflow == []


# ---------- minmax 解析细节 ----------


def test_minmax_percentage_bounds():
    data = solve(["minmax(10%, 30%)", "1fr"], container_width=1000)
    t0 = data.tracks[0]
    assert t0.base == 100
    assert t0.limit == 300


def test_minmax_fixed_min_greater_than_max_clamps():
    # CSS 语义：下限大于上限时上限夹到下限
    data = solve(["minmax(300px, 100px)"], container_width=1000)
    assert data.tracks[0].base == 300
    assert data.tracks[0].limit == 300


def test_minmax_fr_upper_has_no_limit():
    data = solve(["minmax(50px, 2fr)"] , container_width=1000)
    t0 = data.tracks[0]
    assert t0.base == 50
    assert t0.limit is None
    assert t0.final == 1000


# ---------- 422 校验（HTTP 层） ----------


def test_negative_width_422():
    resp = post({"container_width": -1, "columns": ["1fr"]})
    assert resp.status_code == 422


def test_negative_gap_422():
    resp = post({"container_width": 100, "gap": -5, "columns": ["1fr"]})
    assert resp.status_code == 422


def test_negative_min_content_422():
    resp = post(
        {
            "container_width": 100,
            "columns": ["1fr"],
            "items": [{"start": 1, "min_content": -1}],
        }
    )
    assert resp.status_code == 422


def test_invalid_span_422():
    body = {
        "container_width": 100,
        "columns": ["1fr", "1fr"],
        "items": [{"start": 1, "span": 0}],
    }
    assert post(body).status_code == 422
    body["items"][0]["span"] = 1.5
    assert post(body).status_code == 422


def test_out_of_range_item_422():
    body = {
        "container_width": 100,
        "columns": ["1fr", "1fr"],
        "items": [{"start": 2, "span": 2}],
    }
    resp = post(body)
    assert resp.status_code == 422
    assert "越界" in resp.json()["detail"]


def test_start_below_one_422():
    body = {
        "container_width": 100,
        "columns": ["1fr"],
        "items": [{"start": 0}],
    }
    assert post(body).status_code == 422


def test_bad_track_syntax_422():
    for bad in ["auto", "1em", "fr", "minmax(1fr, 2fr)", "minmax(1px)",
                "minmax(1px, 2px, 3px)", "100", "-5px", "minmax(-1px, 1fr)"]:
        resp = post({"container_width": 100, "columns": [bad]})
        assert resp.status_code == 422, bad


def test_empty_columns_422():
    assert post({"container_width": 100, "columns": []}).status_code == 422


def test_percentage_negative_422():
    assert post({"container_width": 100, "columns": ["-10%"]}).status_code == 422


def test_non_finite_values_422():
    assert post({"container_width": "nan", "columns": ["1fr"]}).status_code == 422
    assert post({"container_width": 100, "columns": ["nanfr"]}).status_code == 422


# ---------- 综合轨迹 ----------


def test_round_numbering_and_trace_fields():
    data = solve(
        ["minmax(0px, 100px)", "minmax(0px, 1fr)", "1fr"],
        items=[
            {"id": "x", "start": 1, "span": 3, "min_content": 450},
        ],
        container_width=1000,
    )
    mc = [r for r in data.rounds if r.phase == "min_content"]
    # 首轮均分 150：列 0 触顶（+100）退出；次轮剩 350-... -> 列 1、2 平分
    assert mc[0].round == 1
    assert mc[-1].round >= 2
    fr = [r for r in data.rounds if r.phase == "fr"]
    assert fr[0].round == mc[-1].round + 1
    assert fr[0].item_index is None
    for r in data.rounds:
        assert r.detail
