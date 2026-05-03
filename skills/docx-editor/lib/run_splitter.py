"""
run_splitter.py — text run splitting + rPr 保留

移植自 Mike docxTrackedChanges.ts::flattenParagraph + reconstructParagraph。

核心概念：
- Word 把連續文字切成多個 <w:r> runs（為了字型/格式變化）
- 要 edit 某一段文字，必須：
  1. 平面化段落：把所有 <w:r><w:t> 串接成一個 paraText，並記錄每個字元所在的 run
  2. 在串接後的 paraText 中找 edit 範圍
  3. 依範圍重建段落，把跨 run 的 edit 範圍精確切出來
  4. 在切點插入 <w:del> + <w:ins>，保留原 run 的 <w:rPr>

XML namespace:
  W  = http://schemas.openxmlformats.org/wordprocessingml/2006/main
"""

from copy import deepcopy
from typing import List, Optional, Tuple

from lxml import etree

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def _w(tag: str) -> str:
    return f"{{{W}}}{tag}"


# ---------------------------------------------------------------------------
# Paragraph flattening
# ---------------------------------------------------------------------------

class RunInfo:
    """一個 run 的 flat 檢視（平面化後的資訊）。"""
    __slots__ = ("el", "rpr", "para_start", "para_end", "text")

    def __init__(self, el: etree._Element, rpr: Optional[etree._Element],
                 para_start: int, para_end: int, text: str):
        self.el = el          # 原 <w:r> element
        self.rpr = rpr        # <w:rPr>（若有）
        self.para_start = para_start
        self.para_end = para_end
        self.text = text


class FlatParagraph:
    """段落平面化結果。"""

    def __init__(self, para_text: str, runs: List[RunInfo], char_run: List[int]):
        self.para_text = para_text   # 整段串接純文字
        self.runs = runs             # 各 run 的資訊（保留原始順序）
        self.char_run = char_run     # char_run[i] = para_text[i] 屬於哪個 run（index in runs）


def flatten_paragraph(p_element: etree._Element) -> FlatParagraph:
    """
    平面化段落：把所有 <w:r><w:t> 串接，記錄每個字元所在的 run。

    規則（同 Mike 的 accepted view）：
    - <w:r> → 直接處理
    - <w:ins> → 把內層 <w:r> 視為普通 run（accepted 狀態包含插入文字）
    - <w:del> → 跳過（accepted 狀態不含刪除文字）
    - 其他子元素（bookmarks、sdt...）→ 略過不處理
    """
    runs: List[RunInfo] = []
    para_text = ""
    char_run: List[int] = []

    def process_run(r_el: etree._Element):
        nonlocal para_text
        rpr = r_el.find(_w("rPr"))
        text = ""
        for child in r_el:
            if child.tag == _w("t"):
                text += (child.text or "")
        start = len(para_text)
        end = start + len(text)
        run_idx = len(runs)
        runs.append(RunInfo(r_el, rpr, start, end, text))
        para_text += text
        for _ in range(len(text)):
            char_run.append(run_idx)

    for child in p_element:
        tag = child.tag
        if tag == _w("r"):
            process_run(child)
        elif tag == _w("ins"):
            # Accepted view: include inner runs as if bare
            for inner in child:
                if inner.tag == _w("r"):
                    process_run(inner)
        # w:del: skip entirely

    return FlatParagraph(para_text, runs, char_run)


# ---------------------------------------------------------------------------
# Run building helpers
# ---------------------------------------------------------------------------

def _build_run(rpr: Optional[etree._Element], text: str, tag: str) -> etree._Element:
    """
    建立一個 <w:r> 包含 rpr（若有）及 <w:t> 或 <w:delText>。
    換行符用 <w:br/> 表示（同 Mike 的 buildRun）。
    """
    r_el = etree.Element(_w("r"))
    if rpr is not None:
        r_el.append(deepcopy(rpr))

    segments = text.split("\n")
    for i, seg in enumerate(segments):
        if i > 0:
            # Insert <w:br/>
            br = etree.SubElement(r_el, _w("br"))
        if seg:
            t_el = etree.SubElement(r_el, _w(tag))
            t_el.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            t_el.text = seg

    return r_el


def _rpr_for_pos(flat: FlatParagraph, pos: int) -> Optional[etree._Element]:
    """取得 para_text 位置 pos 對應 run 的 rPr（用於繼承格式）。"""
    if pos < 0:
        pos = 0
    if pos >= len(flat.para_text):
        pos = len(flat.para_text) - 1
    if pos < 0:
        return flat.runs[0].rpr if flat.runs else None
    run_idx = flat.char_run[pos]
    return flat.runs[run_idx].rpr


# ---------------------------------------------------------------------------
# Paragraph reconstruction with tracked changes
# ---------------------------------------------------------------------------

def rebuild_paragraph_with_edits(
    p_element: etree._Element,
    flat: FlatParagraph,
    edits: List[dict],
    author: str,
    date: str,
) -> None:
    """
    在段落 p_element 內就地插入 <w:del> + <w:ins> tracked changes。

    edits: 已排序（按 delete_start）、非重疊的 edit dict 列表，每個 dict：
        {
            "delete_start": int,
            "delete_end": int,
            "deleted_text": str,
            "inserted_text": str,
            "del_wid": str or None,
            "ins_wid": str or None,
        }

    演算法（同 Mike 的 reconstructParagraph）：
    1. 找出 edits 涵蓋的 run 範圍（firstRunIdx ~ lastRunIdx）
    2. 找出 paragraph 中對應的 child element 範圍
    3. 在該範圍內，依序 emit:
       - 非 edit 區段 → 普通 <w:r>
       - edit 區段 → <w:del> + <w:ins>
    4. 把原本那些 runs 替換成新的 elements
    """
    if not edits or not flat.runs:
        return

    # Determine touched run index range
    first_run_idx = len(flat.runs)
    last_run_idx = -1

    for edit in edits:
        ds = edit["delete_start"]
        de = edit["delete_end"]
        for pos in range(ds, de):
            if pos < len(flat.char_run):
                r = flat.char_run[pos]
                first_run_idx = min(first_run_idx, r)
                last_run_idx = max(last_run_idx, r)
        # Also include the run at the insertion point
        if ds == de:
            if ds < len(flat.para_text):
                r = flat.char_run[ds]
                first_run_idx = min(first_run_idx, r)
                last_run_idx = max(last_run_idx, r)
            elif ds > 0:
                r = flat.char_run[ds - 1]
                first_run_idx = min(first_run_idx, r)
                last_run_idx = max(last_run_idx, r)

    if first_run_idx > last_run_idx:
        # No runs touched (edge case)
        return

    # span_start / span_end: character range covered by the touched runs
    first_run = flat.runs[first_run_idx]
    last_run = flat.runs[last_run_idx]
    span_start = first_run.para_start
    span_end = last_run.para_end

    # Build new run group for the span
    new_elements: List[etree._Element] = []

    def emit_normal(a: int, b: int):
        """Emit normal runs for para_text[a:b]."""
        if a >= b:
            return
        # Group consecutive chars by run index to preserve run boundaries
        i = a
        while i < b:
            run_idx = flat.char_run[i]
            j = i + 1
            while j < b and flat.char_run[j] == run_idx:
                j += 1
            rpr = flat.runs[run_idx].rpr
            text_slice = flat.para_text[i:j]
            new_elements.append(_build_run(rpr, text_slice, "t"))
            i = j

    def emit_del(a: int, b: int, wid: str):
        """Emit <w:del> wrapping runs for para_text[a:b]."""
        if a >= b:
            return
        del_el = etree.Element(_w("del"))
        del_el.set(_w("id"), wid)
        del_el.set(_w("author"), author)
        del_el.set(_w("date"), date)

        i = a
        while i < b:
            run_idx = flat.char_run[i]
            j = i + 1
            while j < b and flat.char_run[j] == run_idx:
                j += 1
            rpr = flat.runs[run_idx].rpr
            text_slice = flat.para_text[i:j]
            del_el.append(_build_run(rpr, text_slice, "delText"))
            i = j
        new_elements.append(del_el)

    def emit_ins(pos: int, text: str, wid: str):
        """Emit <w:ins> at position pos with given text."""
        if not text:
            return
        # Inherit rPr from adjacent position
        adj_pos = (pos - 1) if pos == span_end else pos
        rpr = _rpr_for_pos(flat, adj_pos)
        ins_el = etree.Element(_w("ins"))
        ins_el.set(_w("id"), wid)
        ins_el.set(_w("author"), author)
        ins_el.set(_w("date"), date)
        ins_el.append(_build_run(rpr, text, "t"))
        new_elements.append(ins_el)

    cursor = span_start
    for edit in edits:
        ds = edit["delete_start"]
        de = edit["delete_end"]
        emit_normal(cursor, ds)
        # Insertion fires first (before deletion), matching Mike's order
        if edit.get("inserted_text") and edit.get("ins_wid"):
            emit_ins(ds, edit["inserted_text"], edit["ins_wid"])
        if de > ds and edit.get("del_wid"):
            emit_del(ds, de, edit["del_wid"])
        cursor = de

    emit_normal(cursor, span_end)

    # Identify which child elements to replace
    # Find the set of child indices corresponding to run elements in the span
    # We need to locate original children by element identity
    touched_run_elements = set()
    for ri in range(first_run_idx, last_run_idx + 1):
        touched_run_elements.add(id(flat.runs[ri].el))

    # Also collect any w:del elements inside the span (to accept their deletions)
    # following Mike's logic: "Any w:del wrappers that sit inside the span we're
    # rewriting are also dropped"
    children = list(p_element)
    touched_indices = set()
    first_touched_idx = None

    for ci, child in enumerate(children):
        el_id = id(child)
        if el_id in touched_run_elements:
            touched_indices.add(ci)
            if first_touched_idx is None:
                first_touched_idx = ci
        elif child.tag == _w("ins"):
            # Check if any inner runs are touched
            for inner in child:
                if id(inner) in touched_run_elements:
                    touched_indices.add(ci)
                    if first_touched_idx is None:
                        first_touched_idx = ci
                    break
        elif child.tag == _w("del"):
            # Check if this del is within the span by checking its position
            # We drop any w:del within startChildIdx ~ endChildIdx range
            # Since we don't track child indices in RunInfo, check if it's between
            # first and last touched run children
            pass  # handled below

    if first_touched_idx is None:
        return

    # Also drop w:del elements between first and last touched indices
    last_touched_idx = max(touched_indices) if touched_indices else first_touched_idx
    for ci in range(first_touched_idx, last_touched_idx + 1):
        if ci < len(children) and children[ci].tag == _w("del"):
            touched_indices.add(ci)

    # Rebuild paragraph children
    new_children: List[etree._Element] = []
    inserted_new = False

    for ci, child in enumerate(children):
        if ci == first_touched_idx and not inserted_new:
            # Insert all the new elements here
            new_children.extend(new_elements)
            inserted_new = True
        if ci not in touched_indices:
            new_children.append(child)

    # Remove all existing children and add new ones
    for child in list(p_element):
        p_element.remove(child)
    for child in new_children:
        p_element.append(child)


# ---------------------------------------------------------------------------
# Collapse diff (minimize tracked range)
# ---------------------------------------------------------------------------

def collapse_diff(find: str, replace: str) -> Tuple[str, str, int, int]:
    """
    找 find 和 replace 的最小差異範圍。
    移植 Mike 的 collapseDiff。

    Returns:
        (deleted, inserted, leading_eq, trailing_eq)
        deleted: 實際要刪除的子字串（find 的中間部分）
        inserted: 實際要插入的字串（replace 的中間部分）
        leading_eq: 前綴共同字元數
        trailing_eq: 後綴共同字元數
    """
    leading = 0
    min_len = min(len(find), len(replace))
    while leading < min_len and find[leading] == replace[leading]:
        leading += 1

    trailing = 0
    while (trailing < min_len - leading and
           find[len(find) - 1 - trailing] == replace[len(replace) - 1 - trailing]):
        trailing += 1

    deleted = find[leading: len(find) - trailing if trailing else len(find)]
    inserted = replace[leading: len(replace) - trailing if trailing else len(replace)]

    return deleted, inserted, leading, trailing
