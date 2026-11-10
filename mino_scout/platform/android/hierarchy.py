# !/usr/bin/env python
# -*-coding:utf-8 -*-
"""UI 层级采集与锚点解析（S0b）。

为什么需要
----------
现引擎让决策 VLM 每步直出绝对坐标，实测同一个按钮会给出七种坐标
（`455,2094 → 450,2081 → 462,2094 → 456,2086 …`，见 docs/plan-review-and-industry-comparison.md §3.5）。
后果：① 震荡检测要求 `str(params)` 全等，永久失效；② 动作轨迹无法复用。

改法：模型给**语义锚点**（resource_id / text / content_desc），执行侧用 UI 层级把它
解析成精确坐标，模型的坐标只作兜底。同一个按钮 → 同一个锚点 → 动作签名稳定。

成本与来源（实测，结论改过一次）
--------------------------------
起初用 `adb shell uiautomator dump`，实测 **2.2s**，且在**动画页面上直接失败**
（`ERROR: could not get idle state.` —— 它死等 idle，而造物相机社区页卡片一直在动，
三次全失败）。改用 uiautomator2 后：首次连接 745ms，**连接复用后单次约 35ms**，
且动画页面能正常 dump。

所以来源优先级是 u2 → adb exec-out → adb 落盘，且 35ms 的开销让"每步都取层级"
重新变得可行（是否注入 prompt 仍由调用方决定）。
"""
from __future__ import annotations

import re
import subprocess
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any, Optional

from mino_scout.tools.keep_alive.log import SLog

TAG = "Hierarchy"

_BOUNDS_RE = re.compile(r"\[(-?\d+),(-?\d+)\]\[(-?\d+),(-?\d+)\]")

# dump 结果缓存：serial -> (ts, UiDump)。同一屏内多次锚点解析共用一次 dump。
_CACHE: dict[str, tuple[float, "UiDump"]] = {}
_CACHE_TTL_SEC = 1.5

# uiautomator2 连接复用：首次连接约 745ms，复用后单次 dump 约 35ms
_U2_CONNS: dict[str, Any] = {}


@dataclass
class UiNode:
    index: int
    resource_id: str = ""
    text: str = ""
    content_desc: str = ""
    cls: str = ""
    package: str = ""
    clickable: bool = False
    enabled: bool = True
    focused: bool = False
    bounds: tuple[int, int, int, int] = (0, 0, 0, 0)
    hint: str = ""
    checkable: bool = False
    checked: bool = False
    checked_known: bool = False
    editable: bool = False
    sensitive: bool = False
    visible: bool = True
    parent: Optional["UiNode"] = field(default=None, repr=False)

    @property
    def center(self) -> tuple[int, int]:
        l, t, r, b = self.bounds
        return (l + r) // 2, (t + b) // 2

    @property
    def area(self) -> int:
        l, t, r, b = self.bounds
        return max(0, r - l) * max(0, b - t)

    @property
    def rid_short(self) -> str:
        """去掉包名前缀的 resource-id（模型常只给后半段）。"""
        return self.resource_id.split("/", 1)[-1] if self.resource_id else ""

    def label(self) -> str:
        return self.text or self.content_desc or self.rid_short or self.cls.split(".")[-1]

    def to_brief(self) -> dict[str, Any]:
        cx, cy = self.center
        return {
            "resource_id": self.resource_id,
            "text": "" if self.sensitive else self.text,
            "content_desc": self.content_desc,
            "class": self.cls,
            "clickable": self.clickable,
            "enabled": self.enabled,
            "focused": self.focused,
            "checkable": self.checkable,
            "checked": self.checked,
            "checked_known": self.checked_known,
            "editable": self.editable,
            "sensitive": self.sensitive,
            "visible": self.visible,
            "bounds": list(self.bounds),
            "center": [cx, cy],
        }


@dataclass
class UiDump:
    ok: bool = False
    nodes: list[UiNode] = field(default_factory=list)
    error: str = ""
    elapsed_ms: int = 0
    source: str = "adb"
    raw_text: str = ""

    def __len__(self) -> int:
        return len(self.nodes)


# ---------- 采集 ----------


def _run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                           errors="replace")
        return p.returncode, p.stdout or "", p.stderr or ""
    except subprocess.TimeoutExpired:
        return -1, "", f"timeout {timeout}s"
    except FileNotFoundError:
        return -2, "", "adb not in PATH"


def _u2_dump(serial: str) -> tuple[str, str]:
    """用 uiautomator2 抓层级（主路径）。返回 (xml, error)。

    为什么优先用它而不是 `adb shell uiautomator dump`：
      - **动画页面上 `uiautomator dump` 直接失败**（`ERROR: could not get idle state.`），
        它会死等 idle；而造物相机社区页卡片一直在动，实测三次全失败；
      - 快得多：首次连接约 745ms，**连接复用后约 35ms**（对比 exec-out 约 2.2s）。
    依赖已在 requirements（uiautomator2==3.5.0），设备侧 agent 也已安装
    （`driver/tentacle/engine/mobile/mAdb.py` 早就用它注入触控）。
    """
    try:
        import uiautomator2 as u2
    except ImportError as exc:
        return "", f"uiautomator2 不可用: {exc}"
    try:
        dev = _U2_CONNS.get(serial)
        if dev is None:
            dev = u2.connect(serial)
            _U2_CONNS[serial] = dev
        return dev.dump_hierarchy(compressed=False) or "", ""
    except Exception as exc:  # pragma: no cover - 设备侧 agent 异常
        _U2_CONNS.pop(serial, None)   # 连接可能已坏，下次重连
        return "", f"u2 dump 失败: {type(exc).__name__}: {exc}"


def dump_ui_nodes(
    serial: str,
    *,
    timeout_sec: float = 12.0,
    force_fresh: bool = False,
) -> UiDump:
    """抓一次 UI 层级并解析成 UiNode 列表（带 TTL 缓存）。

    来源优先级：uiautomator2 → adb exec-out → adb 落盘再读。
    """
    if not serial:
        return UiDump(ok=False, error="missing serial")
    if not force_fresh:
        hit = _CACHE.get(serial)
        if hit and (time.time() - hit[0]) < _CACHE_TTL_SEC:
            cached = hit[1]
            SLog.i(TAG, f"hierarchy cache hit sn={serial} nodes={len(cached)}")
            return cached

    t0 = time.time()
    errors: list[str] = []
    source = ""

    xml_text, err = _u2_dump(serial)
    if xml_text:
        source = "u2"
    else:
        errors.append(err or "u2 无输出")
        # 兜底 1：exec-out 直出（动画页面会失败，仅作后备）
        rc, out, err2 = _run(
            ["adb", "-s", serial, "exec-out", "uiautomator", "dump", "/dev/tty"], timeout_sec
        )
        xml_text = _extract_xml(out)
        if xml_text:
            source = "adb_exec_out"
        else:
            errors.append((err2 or out or f"rc={rc}").strip()[:120])
            # 兜底 2：落盘再读（老设备 /dev/tty 不通）
            rc2, _, err3 = _run(
                ["adb", "-s", serial, "shell", "uiautomator", "dump", "/sdcard/window_dump.xml"],
                timeout_sec,
            )
            if rc2 == 0:
                _, out2, _ = _run(
                    ["adb", "-s", serial, "shell", "cat", "/sdcard/window_dump.xml"], timeout_sec
                )
                xml_text = _extract_xml(out2)
                if xml_text:
                    source = "adb_file"
            if not xml_text:
                errors.append((err3 or f"rc={rc2}").strip()[:120])

    if not xml_text:
        dump = UiDump(ok=False, error=" | ".join(e for e in errors if e)[:240],
                      elapsed_ms=int((time.time() - t0) * 1000))
        SLog.w(TAG, f"hierarchy dump failed sn={serial}: {dump.error}")
        return dump

    dump = _parse_xml(xml_text)
    dump.source = source
    dump.elapsed_ms = int((time.time() - t0) * 1000)
    SLog.i(TAG, f"hierarchy dump sn={serial} src={source} nodes={len(dump)} "
                f"ok={dump.ok} elapsed={dump.elapsed_ms}ms")
    if dump.ok:
        _CACHE[serial] = (time.time(), dump)
    return dump


def invalidate_cache(serial: str = "") -> None:
    """动作执行后屏幕会变，调用方应主动失效缓存。"""
    if serial:
        _CACHE.pop(serial, None)
    else:
        _CACHE.clear()


def _extract_xml(text: str) -> str:
    """从 uiautomator 输出里截出 XML（它会混入 'UI hierchary dumped to: ...' 之类文案）。"""
    if not text:
        return ""
    i = text.find("<hierarchy")
    if i < 0:
        i = text.find("<?xml")
        if i < 0:
            return ""
    j = text.rfind("</hierarchy>")
    return text[i:j + len("</hierarchy>")] if j > i else text[i:]


def _parse_bounds(raw: str) -> tuple[int, int, int, int]:
    m = _BOUNDS_RE.search(raw or "")
    if not m:
        return (0, 0, 0, 0)
    l, t, r, b = (int(g) for g in m.groups())
    return (l, t, r, b)


def _parse_xml(xml_text: str) -> UiDump:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        return UiDump(ok=False, error=f"xml parse: {exc}")

    nodes: list[UiNode] = []

    def walk(el: ET.Element, parent: Optional[UiNode]) -> None:
        node: Optional[UiNode] = None
        if el.tag == "node":
            a = el.attrib
            node = UiNode(
                index=len(nodes),
                resource_id=(a.get("resource-id") or "").strip(),
                text=(a.get("text") or "").strip(),
                content_desc=(a.get("content-desc") or "").strip(),
                cls=(a.get("class") or "").strip(),
                package=(a.get("package") or "").strip(),
                clickable=(a.get("clickable") == "true"),
                enabled=(a.get("enabled") != "false"),
                focused=(a.get("focused") == "true"),
                bounds=_parse_bounds(a.get("bounds") or ""),
                hint=(a.get("hint") or "").strip(),
                checkable=a.get("checkable") == "true",
                checked=a.get("checked") == "true",
                checked_known="checked" in a,
                editable=a.get("editable") == "true" or "edittext" in str(a.get("class") or "").lower(),
                sensitive=a.get("password") == "true",
                visible=a.get("visible-to-user") != "false",
                parent=parent,
            )
            nodes.append(node)
        for child in el:
            walk(child, node or parent)

    walk(root, None)
    return UiDump(ok=True, nodes=nodes)


# ---------- 锚点解析 ----------

_MATCH_ORDER = (
    "resource_id",       # 最稳
    "resource_id_short",
    "text",
    "content_desc",
    "text_contains",
    "content_desc_contains",
    "class",             # 内容型控件/无文字按钮：只按控件类型命中
)


@dataclass
class AnchorMatch:
    node: UiNode
    matched_by: str
    candidates: int = 1
    clickable_candidates: int = 0

    def to_brief(self) -> dict[str, Any]:
        out = self.node.to_brief()
        out["matched_by"] = self.matched_by
        out["candidates"] = self.candidates
        out["clickable_candidates"] = self.clickable_candidates
        return out


def _norm(s: Any) -> str:
    return str(s or "").strip().lower()


def _clickable_target(node: UiNode, max_up: int = 4) -> Optional[UiNode]:
    """返回该节点本身或最近的可点击祖先；都不可点击则 None。

    文本节点常包在可点击父容器里（点父容器才有效），所以要往上找。
    """
    cur: Optional[UiNode] = node
    hops = 0
    while cur is not None and hops <= max_up:
        if cur.clickable and cur.area > 0:
            return cur
        cur = cur.parent
        hops += 1
    return None


def resolve_target(
    nodes: list[UiNode],
    target: dict[str, Any],
    *,
    prefer_clickable: bool = True,
    index: Optional[int] = None,
) -> Optional[AnchorMatch]:
    """按优先级把语义锚点解析成节点。

    target 支持键：resource_id / text / content_desc（可同时给，按优先级逐个尝试）。
    index 给了就按屏幕顺序取第 N 个命中（0 起），默认 None 走面积/可点击规则。

    同一优先级命中多个时的挑选规则（顺序很重要）：
      1. **先按可点击性分层** —— 只在"自身或祖先可点击"的候选里挑。
         实测踩过的坑：`content_desc="社区"` 同时命中顶部标题(121,220, 不可点击)
         与底栏 Tab(233,2494)；若先按面积挑会选中标题，点下去毫无效果
         —— 这正是"点击成功但界面无变化"那类假动作的来源。
      2. 同层内取**面积最小**的（最具体的那个）。
    """
    if not nodes or not isinstance(target, dict):
        return None

    rid = _norm(target.get("resource_id") or target.get("resource-id") or target.get("id"))
    text = _norm(target.get("text"))
    desc = _norm(target.get("content_desc") or target.get("content-desc") or target.get("desc"))
    cls = _norm(target.get("class"))

    usable = [n for n in nodes if n.enabled and n.area > 0]
    if cls:
        # 按控件类型收窄：内容型控件（列表卡片、贴图按钮）只靠类型就能圈定。
        same = [n for n in usable if cls == _norm(n.cls) or cls == _norm(n.cls.split(".")[-1])]
        if same:
            usable = same
    def ordered(cands: list[UiNode]) -> list[UiNode]:
        return sorted(cands, key=lambda n: (n.bounds[1], n.bounds[0]))

    def pick(cands: list[UiNode], how: str) -> Optional[AnchorMatch]:
        if not cands:
            return None
        total = len(cands)
        if index is not None:
            # 调用方要第几个就取第几个：列表卡片、同名重复项都按屏幕顺序排。
            seq = ordered(cands)
            pos = int(index) if int(index) >= 0 else total + int(index)
            if pos < 0 or pos >= len(seq):
                return None
            chosen = _clickable_target(seq[pos]) or seq[pos]
            return AnchorMatch(node=chosen, matched_by=f"{how}#{index}", candidates=total,
                               clickable_candidates=total)
        if prefer_clickable:
            # 映射到"实际可点的那个节点"，只保留能点的候选
            actionable = [(c, _clickable_target(c)) for c in cands]
            actionable = [(c, t) for c, t in actionable if t is not None]
            if actionable:
                _, chosen = min(actionable, key=lambda pair: pair[1].area)
                return AnchorMatch(node=chosen, matched_by=how, candidates=total,
                                   clickable_candidates=len(actionable))
            # 一个都不可点击：仍返回最小者，但标记出来供审计
            chosen = min(cands, key=lambda n: n.area)
            return AnchorMatch(node=chosen, matched_by=how, candidates=total,
                              clickable_candidates=0)
        return AnchorMatch(node=min(cands, key=lambda n: n.area), matched_by=how,
                           candidates=total, clickable_candidates=0)

    for how in _MATCH_ORDER:
        if how == "resource_id" and rid:
            got = pick([n for n in usable if _norm(n.resource_id) == rid], how)
        elif how == "resource_id_short" and rid:
            got = pick([n for n in usable if _norm(n.rid_short) == rid], how)
        elif how == "text" and text:
            got = pick([n for n in usable if _norm(n.text) == text], how)
        elif how == "content_desc" and desc:
            got = pick([n for n in usable if _norm(n.content_desc) == desc], how)
        elif how == "text_contains" and text:
            got = pick([n for n in usable if text in _norm(n.text)], how)
        elif how == "content_desc_contains" and desc:
            got = pick([n for n in usable if desc in _norm(n.content_desc)], how)
        elif how == "class" and cls:
            # 只给控件类型时（列表卡片、无文字贴图按钮）按类型命中。
            got = pick([n for n in usable if cls == _norm(n.cls) or cls == _norm(n.cls.split(".")[-1])], how)
        else:
            got = None
        if got is not None:
            return got
    return None


def _bottom_tab_nodes(nodes: list[UiNode], *, floor_ratio: float = 0.72) -> list[UiNode]:
    usable = [n for n in nodes if n.enabled and n.area > 0]
    if not usable:
        return []
    max_y = max(n.bounds[3] for n in usable)
    floor = max_y * floor_ratio if max_y > 0 else 0
    return [n for n in usable if n.bounds[1] >= floor]


def resolve_bottom_tab_text(nodes: list[UiNode], label: str) -> Optional[AnchorMatch]:
    """底栏区域：精确未命中时用 contains 匹配 Tab 文案（Nexus 已映射真实 Tab 名）。"""
    text = _norm(label)
    if not text or not nodes:
        return None
    pool = _bottom_tab_nodes(nodes)
    if not pool:
        return None

    def pick(cands: list[UiNode], how: str) -> Optional[AnchorMatch]:
        actionable = [(c, _clickable_target(c)) for c in cands]
        actionable = [(c, t) for c, t in actionable if t is not None]
        if not actionable:
            return None
        _, chosen = min(actionable, key=lambda pair: pair[1].area)
        return AnchorMatch(
            node=chosen,
            matched_by=how,
            candidates=len(cands),
            clickable_candidates=len(actionable),
        )

    exact = [n for n in pool if _norm(n.text) == text or _norm(n.content_desc) == text]
    got = pick(exact, "bottom_tab_text")
    if got is not None:
        return got
    contains = [
        n
        for n in pool
        if text in _norm(n.text) or text in _norm(n.content_desc)
    ]
    return pick(contains, "bottom_tab_text_contains")


def resolve_anchor_between(
    nodes: list[UiNode],
    left: str,
    right: str,
) -> Optional[AnchorMatch]:
    """双锚点之间的槽位（无文案图标 Tab）。left/right 可为空表示贴边。"""
    if not nodes:
        return None
    left_n = str(left or "").strip()
    right_n = str(right or "").strip()
    pool = _bottom_tab_nodes(nodes)
    if len(pool) < 2:
        return None
    ordered = sorted(pool, key=lambda n: n.bounds[0])

    def fold(s: str) -> str:
        return _norm(s)

    def find_idx(label: str) -> int | None:
        if not label:
            return None
        f = fold(label)
        for i, n in enumerate(ordered):
            if f and (f == fold(n.text) or f == fold(n.content_desc)):
                return i
        return None

    li, ri = find_idx(left_n), find_idx(right_n)
    if li is None and left_n:
        return None
    if ri is None and right_n:
        return None
    if li is not None and ri is not None:
        if ri <= li + 1:
            mid_idx = li + 1 if li + 1 < len(ordered) else li
        else:
            mid_idx = li + 1
    elif li is not None:
        mid_idx = min(li + 1, len(ordered) - 1)
    elif ri is not None:
        mid_idx = max(ri - 1, 0)
    else:
        return None
    if mid_idx < 0 or mid_idx >= len(ordered):
        return None
    node = ordered[mid_idx]
    target = _clickable_target(node) or node
    return AnchorMatch(node=target, matched_by="anchor_between", candidates=len(ordered))


def has_target(params: dict[str, Any]) -> bool:
    """params 里是否带了可用的语义锚点。"""
    t = (params or {}).get("target")
    if not isinstance(t, dict):
        return False
    return any(_norm(t.get(k)) for k in
               ("resource_id", "resource-id", "id", "text", "content_desc", "content-desc", "desc", "class"))


def lift_text_anchor(params: dict[str, Any]) -> dict[str, Any]:
    """把 selector_text / description / 字符串 target 提升为 params.target。"""
    p = dict(params or {})
    raw_target = p.get("target")
    if isinstance(raw_target, str) and raw_target.strip():
        label = raw_target.strip().strip("「」\"'")
        p["target"] = {"text": label, "content_desc": label}
        return p
    if has_target(p):
        return p
    target = dict(p.get("target") or {}) if isinstance(p.get("target"), dict) else {}
    label = ""
    for key in ("selector_text", "description", "label", "text"):
        raw = p.get(key)
        if raw is None and key != "selector_text":
            raw = target.get(key)
        s = str(raw or "").strip().strip("「」\"'")
        if s:
            label = s
            break
    if not label:
        return p
    target.setdefault("text", label)
    target.setdefault("content_desc", label)
    p["target"] = target
    return p


def snap_point(
    nodes: list[UiNode],
    x: int,
    y: int,
    *,
    slop: int = 96,
) -> Optional[tuple[int, int, dict[str, Any]]]:
    """把落点吸到真正可点的控件中心。

    模型给的底栏坐标经常偏下（落到手势条），`input tap` 仍返回 0。
    点在控件内部 → 取面积最小的那个；否则在正下方 slop 像素内找同 x 列的 Tab。
    """
    if not nodes:
        return None
    inside: list[UiNode] = []
    near: list[UiNode] = []
    for n in nodes:
        if not (n.clickable and n.enabled and n.area > 0):
            continue
        l, t, r, b = n.bounds
        if l <= x <= r and t <= y <= b:
            inside.append(n)
            continue
        if l <= x <= r and t <= y <= (b + slop):
            near.append(n)
    pool = inside or near
    if not pool:
        return None
    chosen = min(pool, key=lambda n: n.area)
    cx, cy = chosen.center
    how = "inside" if chosen in inside else "near"
    return cx, cy, {"snap": {**chosen.to_brief(), "how": how, "from": [x, y]}}


def click_xy(serial: str, x: int, y: int) -> tuple[bool, str]:
    """用已复用的 uiautomator2 连接点坐标。

    Android 14+ / 部分 OEM 上 `adb shell input tap` 返回 0 但事件到不了 App。
    u2 走 AccessibilityService，这是 MiniOrange mAdb 的主路径。
    """
    if not serial:
        return False, "missing serial"
    try:
        import uiautomator2 as u2
    except ImportError as exc:
        return False, f"uiautomator2 不可用: {exc}"
    try:
        dev = _U2_CONNS.get(serial)
        if dev is None:
            dev = u2.connect(serial)
            _U2_CONNS[serial] = dev
        dev.click(int(x), int(y))
        return True, "u2"
    except Exception as exc:
        SLog.w(TAG, f"u2 click ({x},{y}) failed: {type(exc).__name__}: {exc}")
        return False, f"{type(exc).__name__}: {exc}"


# ---------- 供 prompt / 调试使用的紧凑视图 ----------


def to_prompt_text(dump: UiDump, *, limit: int = 40, max_chars: int = 3000) -> str:
    """把层级压成紧凑可读列表（仅可点击或带文案的节点）。

    默认不在每步注入（dump 要 ~2.2s）；排障或 dry-run 时用。
    """
    if dump.raw_text:
        return str(dump.raw_text)[:max_chars]
    if not dump.ok:
        return ""
    lines: list[str] = []
    seen: set[tuple] = set()
    for n in dump.nodes:
        if not (n.clickable or n.text or n.content_desc):
            continue
        if n.area <= 0:
            continue
        key = (n.rid_short, n.text, n.content_desc)
        if key in seen:
            continue
        seen.add(key)
        cx, cy = n.center
        bits = []
        if n.text:
            bits.append(f'text="{n.text[:24]}"')
        if n.content_desc:
            bits.append(f'desc="{n.content_desc[:24]}"')
        if n.rid_short:
            bits.append(f"id={n.rid_short[:28]}")
        flag = "点" if n.clickable else " "
        lines.append(f"[{flag}] {' '.join(bits)} @({cx},{cy})")
        if len(lines) >= limit:
            break
    text = "\n".join(lines)
    return text[:max_chars]
