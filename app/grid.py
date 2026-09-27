"""单行 CSS Grid 列宽求解。

简化版轨道尺寸算法：

1. 解析固定轨道（Npx / N% / Nfr / minmax(Npx|%, Npx|%|fr)），得到每条
   轨道的基础尺寸与增长上限；
2. 项目按跨度从短到长、同跨度按输入顺序处理。项目跨度内轨道的当前尺寸
   之和（含内部间距）不足 min_content 时，缺口在可增长轨道间**等额**分配；
   触到 minmax 上限的轨道退出，其份额转给其余轨道，直到缺口补齐或无轨道
   可增长（后者把未满足的差额记入 overflow）；
3. 容器剩余空间按 fr 迭代分配：每条 fr 轨道按比例取份额；若某轨道的份额
   低于其基础尺寸，则把该轨道**固定**在基础尺寸、退出分配，重算其余轨道
   的份额，直至无人被固定。

百分比轨道相对 container_width 解析；minmax 的固定下限大于上限时按 CSS
语义把上限夹到下限。
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .models import (
    GridCappedOut,
    GridItemIn,
    GridOverflowOut,
    GridRoundOut,
    GridTrackOut,
    GridTracksRequest,
    GridTracksResponse,
)

_EPS = 1e-9
# 数字记号（含科学计数法）
_NUMBER = r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?"
_PX_RE = re.compile(rf"^\s*({_NUMBER})px\s*$", re.IGNORECASE)
_PCT_RE = re.compile(rf"^\s*({_NUMBER})%\s*$")
_FR_RE = re.compile(rf"^\s*({_NUMBER})fr\s*$", re.IGNORECASE)
_MINMAX_RE = re.compile(r"^\s*minmax\(\s*(.+?)\s*,\s*(.+?)\s*\)\s*$", re.IGNORECASE | re.DOTALL)


class GridTrackError(ValueError):
    """请求不满足求解前提（映射为 HTTP 422）。"""


@dataclass
class _Track:
    index: int
    definition: str
    base: float
    limit: float | None  # None：fr 上限（可无限增长）
    fr: float = 0.0  # fr 因子（上限为 fr 的 minmax 与裸 fr）
    growable: bool = False  # min_content 阶段可否增长


@dataclass
class _State:
    tracks: list[_Track]
    size: list[float]  # 各轨道当前尺寸
    rounds: list[GridRoundOut] = field(default_factory=list)
    round_no: int = 0


# ------------------------------------------------------------- 解析


def _finite(value: float, what: str) -> float:
    if not math.isfinite(value):
        raise GridTrackError(f"{what} 必须为有限数值，收到 {value!r}")
    return value


def _parse_bound(token: str, what: str, container_width: float) -> tuple[str, float]:
    """解析 minmax 的一个边界，返回 (kind, value)，kind 为 px / pct / fr。"""
    px = _PX_RE.match(token)
    if px:
        value = _finite(float(px.group(1)), what)
        if value < 0:
            raise GridTrackError(f"{what} 不能为负值：{token.strip()!r}")
        return "px", value
    pct = _PCT_RE.match(token)
    if pct:
        value = _finite(float(pct.group(1)), what)
        if value < 0:
            raise GridTrackError(f"{what} 不能为负值：{token.strip()!r}")
        return "pct", value * container_width / 100.0
    fr = _FR_RE.match(token)
    if fr:
        value = _finite(float(fr.group(1)), what)
        if value < 0:
            raise GridTrackError(f"{what} 不能为负值：{token.strip()!r}")
        return "fr", value
    raise GridTrackError(
        f"{what} 只能是 Npx / N% / Nfr，收到 {token.strip()!r}"
    )


def _parse_track(raw: str, index: int, container_width: float) -> _Track:
    if not isinstance(raw, str) or not raw.strip():
        raise GridTrackError(f"第 {index} 列轨道语法为空，必须是 px / % / fr 或 minmax(...)")
    text = raw.strip()

    px = _PX_RE.match(text)
    if px:
        value = _finite(float(px.group(1)), f"第 {index} 列轨道尺寸")
        if value < 0:
            raise GridTrackError(f"第 {index} 列轨道尺寸不能为负值：{text!r}")
        return _Track(index, text, value, value)

    pct = _PCT_RE.match(text)
    if pct:
        pct_value = _finite(float(pct.group(1)), f"第 {index} 列轨道百分比")
        if pct_value < 0:
            raise GridTrackError(f"第 {index} 列轨道尺寸不能为负值：{text!r}")
        value = pct_value * container_width / 100.0
        return _Track(index, text, value, value)

    fr = _FR_RE.match(text)
    if fr:
        fr_value = _finite(float(fr.group(1)), f"第 {index} 列 fr 因子")
        if fr_value < 0:
            raise GridTrackError(f"第 {index} 列 fr 因子不能为负值：{text!r}")
        return _Track(index, text, 0.0, None, fr=fr_value, growable=True)

    mm = _MINMAX_RE.match(text)
    if mm:
        lo_kind, lo = _parse_bound(mm.group(1), f"第 {index} 列 minmax 下限", container_width)
        hi_kind, hi = _parse_bound(mm.group(2), f"第 {index} 列 minmax 上限", container_width)
        if lo_kind == "fr":
            raise GridTrackError(
                f"第 {index} 列 minmax 下限不能是 fr：{text!r}"
            )
        if hi_kind == "fr":
            # minmax(px|%, Nfr)：基础尺寸为下限，fr 参与剩余空间分配，无有限上限
            return _Track(index, text, lo, None, fr=hi, growable=True)
        # 固定 minmax：CSS 规定下限大于上限时上限夹到下限
        if hi < lo:
            hi = lo
        return _Track(index, text, lo, hi, growable=True)

    raise GridTrackError(
        f"第 {index} 列轨道语法 {text!r} 非法：应为 Npx / N% / Nfr 或 "
        "minmax(Npx|%, Npx|%|fr)"
    )


def _validate_items(
    items: list[GridItemIn], column_count: int
) -> list[tuple[int, int, float, object]]:
    """校验项目，返回 (下标, 0 起始列, span, min_content, id) 列表。"""
    checked = []
    for i, item in enumerate(items):
        if item.start < 1:
            raise GridTrackError(
                f"第 {i} 个项目的 start 必须 >= 1（列线 1 起），收到 {item.start}"
            )
        if item.span < 1:
            raise GridTrackError(
                f"第 {i} 个项目的 span 必须为正整数，收到 {item.span}"
            )
        if not math.isfinite(item.min_content):
            raise GridTrackError(
                f"第 {i} 个项目的 min_content 必须为有限数值，收到 {item.min_content!r}"
            )
        if item.min_content < 0:
            raise GridTrackError(
                f"第 {i} 个项目的 min_content 不能为负值，收到 {item.min_content}"
            )
        end_line = item.start + item.span  # 占用终止列线（start+span，n 列的右边界为 n+1）
        last_col = item.start - 1 + item.span - 1  # 占用的最后一列（0 起）
        if last_col >= column_count:
            raise GridTrackError(
                f"第 {i} 个项目越界：start={item.start}、span={item.span} "
                f"终止于列线 {end_line}，但容器只有 {column_count} 列"
            )
        checked.append((i, item.start - 1, item.span, item.min_content, item.id))
    return checked


# ------------------------------------------------------------- 数值工具


def _r(value: float) -> float:
    """输出时夹掉浮点尾巴，保留 6 位小数。"""
    value = round(value, 6)
    return 0.0 if value == 0 else value


# ------------------------------------------------------------- min_content 分配


def _grow_min_content(
    state: _State,
    item_index: int,
    item_id: object,
    col: int,
    span: int,
    min_content: float,
    gap: float,
) -> float:
    """为单个项目分配最小内容缺口，返回未满足的缺口（0 表示已满足）。

    每轮把当前缺口在可增长轨道间等额分配；吃不下自己份额的轨道只长到上限
    并退出，其未满足的份额在下一轮转给其余轨道。
    """
    target = list(range(col, col + span))
    internal_gaps = gap * (span - 1)
    deficit = min_content - (sum(state.size[j] for j in target) + internal_gaps)
    if deficit <= _EPS:
        return 0.0
    deficit = max(deficit, 0.0)

    # 可增长：minmax 下限轨道与裸 fr；被固定轨道（px / %）永不增长
    active = {j for j in target if state.tracks[j].growable}
    while deficit > _EPS and active:
        share = deficit / len(active)
        capped: list[GridCappedOut] = []
        for j in sorted(active):
            track = state.tracks[j]
            room = (track.limit - state.size[j]) if track.limit is not None else math.inf
            if room <= _EPS or share > room + _EPS:
                # 吃不下自己的份额：只长到上限并退出，份额下一轮转给其余轨道
                grew = max(room, 0.0)
                if grew > _EPS:
                    state.size[j] = track.limit  # type: ignore[assignment]
                capped.append(
                    GridCappedOut(index=j, limit=_r(track.limit or 0.0), grew=_r(grew))
                )

        round_deficit = deficit
        if capped:
            for c in capped:
                active.discard(c.index)
            deficit = max(
                min_content - (sum(state.size[j] for j in target) + internal_gaps),
                0.0,
            )
        else:
            # 无人触顶：所有可增长轨道各拿自己的份额，缺口补齐
            for j in active:
                state.size[j] += share
            deficit = 0.0

        state.round_no += 1
        state.rounds.append(
            GridRoundOut(
                phase="min_content",
                round=state.round_no,
                item_index=item_index,
                item_id=item_id,
                target=target,
                deficit=_r(round_deficit),
                share=_r(share),
                active=sorted(active),
                capped=capped,
                overflow=_r(deficit),
                detail=(
                    f"项目[{item_index}] 跨列 {target[0] + 1}-{target[-1] + 1}，"
                    f"缺口 {_r(round_deficit)}px 在 {len(active) + len(capped)} 条可增长轨道间"
                    f"等额分配（每条 {_r(share)}px）"
                    + (
                        f"；列 {[c.index + 1 for c in capped]} 触及上限退出，"
                        f"剩余缺口 {_r(deficit)}px 转给其余轨道"
                        if capped
                        else "，缺口补齐"
                    )
                ),
            )
        )

    return deficit


# ------------------------------------------------------------- fr 分配


def _distribute_fr(state: _State, container_width: float, total_gap: float) -> float:
    """把剩余空间按 fr 迭代分配，返回 fr 阶段开始时的剩余空间。

    各轨道的「基础尺寸」为 min_content 阶段结束后的当前尺寸；fr 轨道在该
    基础之上增长。
    """
    bases = sum(state.size)
    free_space = container_width - total_gap - bases
    fr_indices = [j for j, t in enumerate(state.tracks) if t.fr > 0]
    if not fr_indices:
        return free_space

    frozen: set[int] = set()
    # 已固定轨道保持基础尺寸（增长为 0），剩余空间始终为初始 free_space
    while True:
        active = [j for j in fr_indices if j not in frozen]
        total_fr = sum(state.tracks[j].fr for j in active)
        share_per_fr = free_space / total_fr if total_fr > 0 else 0.0

        newly_frozen: list[int] = []
        if free_space > _EPS:
            for j in active:
                track = state.tracks[j]
                if track.fr * share_per_fr + _EPS < state.size[j]:
                    # 比例份额（的增长）低于基础尺寸：固定该轨道，重算其余份额
                    newly_frozen.append(j)

        state.round_no += 1
        state.rounds.append(
            GridRoundOut(
                phase="fr",
                round=state.round_no,
                deficit=0.0,
                share=_r(share_per_fr) if free_space > _EPS else _r(0.0),
                active=active,
                frozen=sorted(newly_frozen),
                free_space=_r(free_space),
                detail=(
                    (
                        f"剩余空间 {_r(free_space)}px，总 fr={_r(total_fr)}，"
                        f"每单位 fr 取 {_r(share_per_fr)}px"
                        + (
                            f"；列 {[j + 1 for j in newly_frozen]} 的比例份额低于基础尺寸，"
                            "固定并重算"
                            if newly_frozen
                            else "，所有 fr 轨道份额不低于基础尺寸，分配完成"
                        )
                    )
                    if free_space > _EPS
                    else f"剩余空间 {_r(free_space)}px，fr 轨道保持基础尺寸"
                ),
            )
        )

        if not newly_frozen:
            # 终轮：每条 fr 轨道在其（min_content 后的）基础尺寸之上按份额增长
            if free_space > _EPS:
                for j in active:
                    state.size[j] = state.size[j] + state.tracks[j].fr * share_per_fr
            break
        frozen.update(newly_frozen)

    return free_space


# ----------------------------------------------------------------- 总入口


def solve_grid_tracks(req: GridTracksRequest) -> GridTracksResponse:
    container_width = _finite(req.container_width, "container_width")
    if container_width < 0:
        raise GridTrackError(f"container_width 不能为负值，收到 {container_width}")
    gap = _finite(req.gap, "gap")
    if gap < 0:
        raise GridTrackError(f"gap 不能为负值，收到 {gap}")
    if not req.columns:
        raise GridTrackError("columns 至少需要一条轨道定义")

    tracks = [_parse_track(raw, i, container_width) for i, raw in enumerate(req.columns)]
    items = _validate_items(req.items, len(tracks))
    state = _State(tracks=tracks, size=[t.base for t in tracks])

    total_gap = gap * (len(tracks) - 1)

    # 项目按跨度从短到长、同跨度按输入顺序处理
    ordered = sorted(items, key=lambda it: (it[2], it[0]))
    overflow: list[GridOverflowOut] = []
    for item_index, col, span, min_content, item_id in ordered:
        residual = _grow_min_content(
            state, item_index, item_id, col, span, min_content, gap
        )
        if residual > _EPS:
            target = list(range(col, col + span))
            available = sum(state.size[j] for j in target) + gap * (span - 1)
            overflow.append(
                GridOverflowOut(
                    item_index=item_index,
                    item_id=item_id,
                    start=col + 1,
                    span=span,
                    min_content=_r(min_content),
                    available=_r(available),
                    overflow=_r(residual),
                )
            )

    # base：min_content 分配结束、fr 分配开始前的尺寸快照
    bases = list(state.size)
    free_space = _distribute_fr(state, container_width, total_gap)

    tracks_out = [
        GridTrackOut(
            index=t.index,
            definition=t.definition,
            base=_r(bases[t.index]),
            limit=_r(t.limit) if t.limit is not None else None,
            final=_r(state.size[t.index]),
        )
        for t in tracks
    ]
    return GridTracksResponse(
        container_width=_r(container_width),
        gap=_r(gap),
        used_gap=_r(total_gap),
        tracks=tracks_out,
        rounds=state.rounds,
        overflow=overflow,
        free_space=_r(free_space),
        total_tracks_width=_r(sum(state.size)),
    )
