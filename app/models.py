"""请求 / 响应的数据模型（Pydantic）。"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

Origin = Literal["ua", "user", "author"]


class ElementInfo(BaseModel):
    """DOM 中一个元素的快照。祖先链按 根 -> ... -> 父 的顺序给出。"""

    tag: str = ""
    id: Optional[str] = None
    classes: list[str] = Field(default_factory=list)
    attributes: dict[str, str] = Field(default_factory=dict)


class Declaration(BaseModel):
    property: str
    value: str
    important: bool = False


class Rule(BaseModel):
    """一条结构化样式规则。layer 为 null 表示未分层声明。"""

    selector: str
    declarations: list[Declaration]
    origin: Origin = "author"
    layer: Optional[str] = None
    order: Optional[int] = None  # 源码次序，缺省取规则在数组中的下标


class ComputeRequest(BaseModel):
    element: ElementInfo
    ancestors: list[ElementInfo] = Field(default_factory=list)
    layers: list[str] = Field(default_factory=list)  # 层声明顺序：早 -> 晚
    rules: list[Rule] = Field(default_factory=list)


# ---------- 响应 ----------


class CandidateOut(BaseModel):
    """参与某属性层叠竞争的一条候选声明。"""

    selector: str
    origin: str
    layer: Optional[str]
    important: bool
    specificity: list[int]
    source_order: int
    property: str
    value: str


class EliminatedOut(BaseModel):
    """一条被淘汰的候选及其被淘汰的阶段与原因。"""

    candidate: CandidateOut
    stage: str  # importance / origin / layer / specificity / source_order
    detail: str


class PropertyEvidenceOut(BaseModel):
    """普通属性的层叠证据。"""

    property: str
    status: str  # computed | invalid
    computed_value: Optional[str]
    winner: CandidateOut
    var_steps: list[str] = Field(default_factory=list)
    eliminated: list[EliminatedOut] = Field(default_factory=list)


class CustomPropTraceOut(BaseModel):
    """自定义属性沿祖先链逐元素的解析轨迹。"""

    element: str
    property: str
    status: str  # ok | invalid | inherited
    declared_value: Optional[str] = None  # 本元素层叠胜出的原始值（未替换 var）
    resolved: Optional[str] = None
    note: str = ""
    winner: Optional[CandidateOut] = None
    eliminated: list[EliminatedOut] = Field(default_factory=list)


class InvalidDeclOut(BaseModel):
    property: str
    value: str
    reason: str


class ComputeResponse(BaseModel):
    computed: dict[str, Optional[str]]
    custom_properties: dict[str, str]
    invalid: list[InvalidDeclOut]
    evidence: list[PropertyEvidenceOut]
    custom_property_trace: list[CustomPropTraceOut]


# ---------- POST /selector-overlap ----------


class OverlapRequest(BaseModel):
    selector_a: str
    selector_b: str


class OverlapConflictOut(BaseModel):
    """不可满足时的冲突证据。

    kind:
      - compound：两段被对齐到同一节点，但其复合约束（tag / id / 属性）互斥；
        segment_b 为 null 时表示单侧某段内部自相矛盾。
      - child：子组合符（>）的刚性把两段强制压到同一节点（可经最近祖先吸附
        使弹性后代段占位后逐层传播），无后代间隙可规避，而两段约束互斥。
    段下标按选择器书写顺序（0 起，最右一段即目标段）。
    """

    kind: Literal["compound", "child"]
    segment_a: Optional[int] = None
    segment_b: Optional[int] = None
    field: Optional[str] = None  # tag / id / attr:<name>；纯结构性结论时为 null
    value_a: Optional[str] = None
    value_b: Optional[str] = None
    reason: str


class OverlapAlignmentRow(BaseModel):
    """逐深度对齐的一行（响应中按 根 -> 目标 顺序给出）。

    depth 为距目标的层数（0 即目标）；segment_a / segment_b 为该深度上
    两侧各自落位的段下标（原序），未落位为 null。combinator_a / combinator_b
    为该段朝目标方向到下一段的组合符（">" 或 " "，末段为 null）。
    """

    depth: int
    segment_a: Optional[int] = None
    segment_b: Optional[int] = None
    combinator_a: Optional[str] = None
    combinator_b: Optional[str] = None
    element: dict  # 见证节点，只包含选择器要求的 tag/id/classes/attributes 字段


class OverlapResponse(BaseModel):
    satisfiable: bool
    selector_a: str
    selector_b: str
    element: Optional[dict] = None
    ancestors: list[dict] = Field(default_factory=list)  # 根 -> 父
    alignment: list[OverlapAlignmentRow] = Field(default_factory=list)
    conflict: Optional[OverlapConflictOut] = None


# ---------- POST /keyframe-sample ----------


class KeyframeIn(BaseModel):
    """一个数值关键帧：offset ∈ [0,1]，value 为「数字 + 单位」（如 12px / 50%）。"""

    offset: float
    value: str


Direction = Literal["normal", "reverse", "alternate", "alternate-reverse"]
FillMode = Literal["none", "forwards", "backwards", "both"]


class KeyframeSampleRequest(BaseModel):
    """CSS 数值属性关键帧采样请求。

    keyframes 的偏移必须严格递增并覆盖 0、1；所有取值单位一致。
    easing 仅支持 "linear" 或 "cubic-bezier(x1,y1,x2,y2)"。
    """

    keyframes: list[KeyframeIn]
    duration: float
    delay: float = 0.0
    iteration_count: int = 1
    direction: Direction = "normal"
    fill_mode: FillMode = "none"
    easing: str = "linear"
    time: float

    @field_validator("iteration_count", mode="before")
    @classmethod
    def _positive_integer(cls, v):
        # 在 Pydantic 默认的 bool->int 归并发生之前显式拒绝布尔值；
        # 非整数值交由 int 字段校验拒绝，正整数由引擎复查
        if isinstance(v, bool):
            raise ValueError("iteration_count 必须为正整数，不能是布尔值")
        return v


class KeyframeSampleResponse(BaseModel):
    """采样结果。

    phase：before / active / after；iteration 为 0 起轮次；segment 为
    命中的帧段（其起点关键帧下标）；local_progress 为段内插值进度，
    eased_progress 为经缓动反解后的进度；value 为插值结果字符串。
    无填充的 before / after 阶段除 phase 外均为 null。
    """

    phase: str
    iteration: Optional[int]
    segment: Optional[int]
    local_progress: Optional[float]
    eased_progress: Optional[float]
    value: Optional[str]


# ---------- POST /counter-render ----------


class CounterOperation(BaseModel):
    """节点上的一条计数器操作（counter-reset / counter-set / counter-increment）。

    value 缺省时 reset / set 取 0、increment 取 1。
    """

    op: Literal["reset", "set", "increment"]
    name: str
    value: Optional[int] = None

    @field_validator("value", mode="before")
    @classmethod
    def _reject_non_int(cls, v):
        # 与 iteration_count 同理：在 Pydantic 的 bool -> int 归并前显式拒绝布尔；
        # 1.0 等浮点也拒绝，计数器操作值必须是整数
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError("计数器操作值必须为整数")
        return v


class ContentFragment(BaseModel):
    """content 的一个片段：字面文本、counter(name, style) 或
    counters(name, separator, style)。style 缺省为 decimal。"""

    kind: Literal["text", "counter", "counters"]
    value: str = ""  # text：字面文本
    name: Optional[str] = None  # counter / counters：计数器名
    separator: Optional[str] = None  # counters：连接分隔符
    style: Optional[str] = None  # decimal / decimal-leading-zero /
    # lower-alpha(lower-latin) / upper-alpha(upper-latin)

    @model_validator(mode="before")
    @classmethod
    def _string_is_text(cls, data):
        # content 数组中的裸字符串视作 text 片段
        if isinstance(data, str):
            return {"kind": "text", "value": data}
        return data


class CounterElement(BaseModel):
    """计数器渲染元素树的一个节点。

    counter_reset / counter_set / counter_increment 为计数器操作列表；
    content 为片段数组，元素可为裸字符串（字面文本）或片段对象。
    """

    id: object
    counter_reset: list[CounterOperation] = Field(default_factory=list)
    counter_set: list[CounterOperation] = Field(default_factory=list)
    counter_increment: list[CounterOperation] = Field(default_factory=list)
    content: list[ContentFragment] = Field(default_factory=list)
    children: list["CounterElement"] = Field(default_factory=list)

    @field_validator("id", mode="before")
    @classmethod
    def _id_type(cls, v):
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("节点 id 必须为非空字符串或整数")
        if isinstance(v, str) and not v.strip():
            raise ValueError("节点 id 不能为空字符串")
        return v

    @field_validator(
        "counter_reset", "counter_set", "counter_increment", mode="before"
    )
    @classmethod
    def _wrap_single_op(cls, v):
        # 允许只写一条操作时给对象而非数组
        if isinstance(v, dict):
            return [v]
        return v


class CounterRenderRequest(BaseModel):
    root: CounterElement


class CounterScopeEventOut(BaseModel):
    """一条计数器作用域变更轨迹。

    phase：
      - enter：节点处理（reset → set → increment）阶段发生的变更，
        type 为 reset / set / increment / create；
      - exit：节点子树处理完毕、作用域退出时发生的变更，
        type 为 restore（恢复被遮蔽的外层实例）/ destroy（实例销毁）。

    type 含义：
      - reset：reset 新建实例并压栈，shadow 为被遮蔽的外层值（无外层为 null）；
      - create：set / increment 引用了不存在的计数器，先以 0 建立实例，
        其后紧接同一条操作的 set / increment 事件；
      - set / increment：写入或累加值，value 为变更后的值；
      - restore：退出时弹出 reset 实例，外层实例恢复为 value；
      - destroy：退出时弹出（reset 或惰性建立的）实例，栈中再无该计数器。
    """

    phase: Literal["enter", "exit"]
    type: Literal["reset", "create", "set", "increment", "restore", "destroy"]
    name: str
    value: Optional[int] = None
    shadow: Optional[int] = None


class CounterNodeOut(BaseModel):
    """先序遍历中一个节点的渲染结果。"""

    id: object
    text: str
    enter: list[CounterScopeEventOut] = Field(default_factory=list)
    exit: list[CounterScopeEventOut] = Field(default_factory=list)


class CounterRenderResponse(BaseModel):
    nodes: list[CounterNodeOut]


# ---------- POST /math-evaluate ----------


class MathContext(BaseModel):
    """CSS 数学表达式的换算上下文（均可选；表达式用到对应单位时必填，
    缺失即 422）。提供的值必须为非负有限数值。

    - font_size：1em 对应的 px；
    - root_font_size：1rem 对应的 px；
    - viewport_width / viewport_height：100vw / 100vh 对应的 px；
    - percentage_basis：100% 对应的 px。
    """

    font_size: Optional[float] = None
    root_font_size: Optional[float] = None
    viewport_width: Optional[float] = None
    viewport_height: Optional[float] = None
    percentage_basis: Optional[float] = None


class MathEvaluateRequest(BaseModel):
    expression: str
    context: MathContext = Field(default_factory=MathContext)


class MathAstNodeOut(BaseModel):
    """表达式 AST 节点。

    number 节点带 value / unit（原始单位，小写，无单位为 ""）；
    unary 带 op 与 operand；binary 带 op 与 left / right；
    function 带 name 与 args。id 按先序从 1 编号，与 trace 的 node_id 对应。
    """

    id: int
    type: Literal["number", "unary", "binary", "function"]
    value: Optional[float] = None
    unit: Optional[str] = None
    op: Optional[str] = None
    operand: Optional["MathAstNodeOut"] = None
    left: Optional["MathAstNodeOut"] = None
    right: Optional["MathAstNodeOut"] = None
    name: Optional[str] = None
    args: list["MathAstNodeOut"] = Field(default_factory=list)


class MathTraceStepOut(BaseModel):
    """逐节点求值轨迹的一步（后序，最后一步即根节点结果）。

    kind：number / positive / negate / add / subtract / multiply /
    divide / calc / min / max / clamp。value / unit 为该节点规范化后的
    结果（长度一律为 px，无单位时 unit 为 ""）。
    """

    node_id: int
    kind: str
    detail: str
    value: float
    unit: str


class MathEvaluateResponse(BaseModel):
    expression: str
    value: float
    unit: str  # "px" 或 ""（无单位）
    ast: MathAstNodeOut
    trace: list[MathTraceStepOut]


# ---------- POST /grid-tracks ----------


class GridItemIn(BaseModel):
    """单行网格中的一个项目。

    start：起始列线（1 起，含）；span：跨列数（正整数）；
    min_content：项目占用轨道至少要容纳的内容宽度（px）。
    id 可选，仅用于在 overflow 中回指项目。
    """

    start: int
    span: int = 1
    min_content: float = 0.0
    id: Optional[object] = None

    @field_validator("start", "span", mode="before")
    @classmethod
    def _strict_int(cls, v):
        # 与计数器操作值同理：拒绝布尔与浮点，列线 / 跨度必须为整数
        if isinstance(v, bool) or isinstance(v, float):
            raise ValueError("start / span 必须为整数")
        return v

    @field_validator("min_content", mode="before")
    @classmethod
    def _reject_bool_min(cls, v):
        if isinstance(v, bool):
            raise ValueError("min_content 必须为非负数值，不能是布尔值")
        return v

    @field_validator("id", mode="before")
    @classmethod
    def _id_type(cls, v):
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, (str, int)):
            raise ValueError("项目 id 必须为字符串或整数")
        if isinstance(v, str) and not v.strip():
            raise ValueError("项目 id 不能为空字符串")
        return v


class GridTracksRequest(BaseModel):
    """单行 CSS Grid 列宽求解请求。

    columns 每项为一条轨道语法：Npx、N%、Nfr 或 minmax(下限, 上限)，
    下限可为 Npx / N%，上限可为 Npx / N% / Nfr。
    """

    container_width: float
    gap: float = 0.0
    columns: list[str]
    items: list[GridItemIn] = Field(default_factory=list)

    @field_validator("container_width", "gap", mode="before")
    @classmethod
    def _reject_bool(cls, v):
        if isinstance(v, bool):
            raise ValueError("container_width / gap 必须为数值，不能是布尔值")
        return v


class GridTrackOut(BaseModel):
    """一列的求解结果。

    base：min_content 分配后的基础尺寸；limit：上限（fr 上限为 null）；
    final：最终尺寸。
    """

    index: int
    definition: str
    base: float
    limit: Optional[float]
    final: float


class GridRoundOut(BaseModel):
    """求解过程的逐轮轨迹。

    phase：
      - min_content：处理某个项目的最小内容缺口，按轮等额分给可增长轨道，
        触及 minmax 上限的轨道退出，其份额转给其余轨道；
      - fr：剩余空间按 fr 迭代分配；比例份额低于基础尺寸的轨道被固定，
        重算其余轨道的份额。
    item_index：min_content 轮对应的项目下标（输入顺序，0 起）；fr 轮为 null。
    target / deficit / share：本轮处理对象、待分配缺口与每条可增长轨道的份额。
    active：本轮仍可增长的列下标；capped：本轮因触顶退出的列下标及所得宽度。
    frozen：fr 轮中被固定在基础尺寸的列下标。
    free_space：fr 轮中本轮可供分配的剩余空间。
    overflow：min_content 轮结束后仍未满足的缺口（0 表示已满足）。
    """

    phase: Literal["min_content", "fr"]
    round: int
    item_index: Optional[int] = None
    item_id: Optional[object] = None
    target: Optional[list[int]] = None
    deficit: float
    share: Optional[float] = None
    active: list[int] = Field(default_factory=list)
    capped: list["GridCappedOut"] = Field(default_factory=list)
    frozen: list[int] = Field(default_factory=list)
    free_space: Optional[float] = None
    overflow: float = 0.0
    detail: str = ""


class GridCappedOut(BaseModel):
    """某轮中触及上限而退出的一条轨道。"""

    index: int
    limit: float
    grew: float


class GridOverflowOut(BaseModel):
    """无法满足 min_content 的项目及溢出缺口。"""

    item_index: int
    item_id: Optional[object] = None
    start: int
    span: int
    min_content: float
    available: float
    overflow: float


class GridTracksResponse(BaseModel):
    container_width: float
    gap: float
    used_gap: float  # 实际计入的总间距（列数 - 1）× gap
    tracks: list[GridTrackOut]
    rounds: list[GridRoundOut]
    overflow: list[GridOverflowOut]
    free_space: float  # fr 阶段开始时的剩余空间
    total_tracks_width: float  # 各列最终尺寸之和（不含间距）
