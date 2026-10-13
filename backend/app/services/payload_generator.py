# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Registry-driven payload generation (CERT-5) — the assertion engine run FORWARD.

`cert_assertions` reads the six constraint columns backward: given a value,
judge it. This module reads the SAME columns forward: given the constraints,
produce a value that satisfies them. One table drives both directions, which
buys the property the certification loop leans on: a payload generated here
from a coherent row set MUST grade clean through `evaluate_specs` over the
same rows. That round-trip is pinned by tests; a disagreement between the two
directions is a registry defect (or a bug here), never a partner's problem.

NOT a schema library. Generation walks `ApiField` rows — duck-typed, attribute
access only (`xpath`, `is_attribute`, `occurrence`, `mandatory`, `datatype`,
`length_rule`, `enum_values`, `pattern_rule`, `condition_text`), in document
order (the `ApiField.position` order the ORM relationship already yields) —
not the source `.xsd`. The registry is the curated truth: code-harvest
evidence and human edits (`updated_by` locks) live there and nowhere else, so
generating from the raw schema would reintroduce exactly the two-readings
drift the registry exists to end. What the flattened rows cannot express
(xs:choice branches, sibling order beyond row order) is out of scope and
belongs to the caller's xmlschema gate.

VALUE RESOLUTION, per leaf/attribute, first hit wins:

1. ``values[path]`` — the test case's explicit intent (full registry paths).
2. ``profile[path]`` — per-network configured defaults (org ids, versions),
   written once and reused across messages: a key is tried verbatim, then
   message-relative (``Head/@orgId`` matches ``ReqTransfer/Head/@orgId``).
   An entry is a literal string, or ``{"provider": name}`` for values that
   must be COMPUTED at send time (msgId, timestamps): a stored msgId would
   collide on the second send, so uniqueness lives in functions, not config.
3. Constraint-derived: first enum member, else a string synthesized from
   ``pattern_rule``, else a ``datatype``/``length_rule``-conforming dummy.

Every resolved value — explicit ones included — is then judged by the
assertion functions themselves; a value that fails its own row's constraints
is still emitted (the caller may be probing on purpose) but lands in
``report["gaps"]``. Business validity is out of scope, the same line
`cert_variants` draws: a schema-valid account number is not an existing
account. Business-valid seeds arrive through layers 1-2.

INCLUSION: required fields (mandatory ``Y``, or occurrence minimum ≥ 1) plus
the ancestors of anything included; optional fields only when explicitly
valued or ``include="all"``. ``mandatory="C"`` is EXCLUDED unless explicitly
valued, and reported under ``conditional_excluded``: the condition is prose
the machine cannot evaluate (see `assert_mandatory`), and emitting a
conditional field whose condition may not hold would be guessing. Honest gaps
over silent guesses, here as everywhere in this pipeline.

FAULTS: a negative payload differs from its positive twin by EXACTLY ONE
named violation (``fault={"path": ..., "kind": ...}``). The valid payload is
built first and then the one thing is broken, so a nack test proves the
validator catches THAT fault — not that the payload was broken twelve ways.
A fault that cannot be constructed for the row (an enum fault on an enum-less
field) raises `FaultNotApplicable` instead of shipping a payload that tests
nothing. The report names the assertion kinds the fault must trip; sibling
assertions on the same field may honestly co-fail (an off-enum value can also
be non-numeric) and the report's ``expected_failure`` is the contract.
"""
from __future__ import annotations

import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field as dc_field
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

# The judge's own column parsers and guards. The generator must read
# `occurrence`/`length_rule` EXACTLY as the judge will, and must refuse the
# same ReDoS-prone patterns the judge refuses (a pattern the judge SKIPs is
# not worth synthesizing for) — importing them beats keeping copies to drift.
from app.services.cert_assertions import (
    FAIL,
    _EXACT_LEN, _MAX_INC, _MAX_LEN, _MIN_INC, _MIN_LEN, _NUMERIC, _OCCURRENCE,
    _MAX_PATTERN_CHARS, _NESTED_QUANTIFIER,
    assert_datatype, assert_enum, assert_length, assert_pattern,
)

__all__ = ["FaultNotApplicable", "GeneratedPayload", "generate_payload",
           "payload_for_message", "FAULT_KINDS"]


class FaultNotApplicable(ValueError):
    """The requested fault cannot be constructed for the target row —
    the caller must know its negative case tests nothing, not receive a
    payload that happens to be valid."""


@dataclass(frozen=True)
class GeneratedPayload:
    xml: str
    report: dict


# ── built-in computed providers ──────────────────────────────────────────────

def _default_providers() -> dict[str, Callable[[], str]]:
    """Send-time values. Tests inject replacements for determinism; production
    callers get uniqueness and current time, which is the point."""
    return {
        "uuid": lambda: uuid.uuid4().hex,
        "timestamp": lambda: datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "date": lambda: datetime.now(timezone.utc).date().isoformat(),
    }


# ── the working tree (nodes carry their registry row) ────────────────────────

@dataclass
class _Attr:
    name: str
    path: str
    row: Any
    include: bool = False
    value: str | None = None
    provider: str | None = None    # provider name when the value is computed


@dataclass
class _Node:
    tag: str
    path: str
    row: Any | None = None
    parent: "_Node | None" = None
    attrs: list[_Attr] = dc_field(default_factory=list)
    children: list["_Node"] = dc_field(default_factory=list)
    include: bool = False
    text: str | None = None
    provider: str | None = None


def _build_tree(api_name: str, rows: Sequence[Any],
                gaps: list[dict]) -> tuple[_Node, dict[str, _Node], dict[str, _Attr]]:
    """Row list (document order) → element tree + path indexes.

    ``@xmlns`` rows are structural, not value slots: the binding comes from the
    message's ``namespace`` and ElementTree consumes declarations at parse time
    (see `XmlCodec.set_value`), so they are skipped here — the serialized root
    carries the declaration and the codec's Clark-tag recovery reads it back.
    """
    root = _Node(tag=api_name, path=api_name)
    elems: dict[str, _Node] = {api_name: root}
    attrs: dict[str, _Attr] = {}

    def ensure(path: str) -> _Node:
        node = elems.get(path)
        if node is not None:
            return node
        parent_path, tag = path.rsplit("/", 1)
        parent = ensure(parent_path)
        node = _Node(tag=tag, path=path, parent=parent)
        parent.children.append(node)
        elems[path] = node
        return node

    for r in rows:
        path = str(getattr(r, "xpath", "") or "")
        if not path or "@" in path.rsplit("/", 1)[0]:
            # a row nested UNDER an attribute is malformed; skip loudly
            gaps.append({"path": path, "reason": "row path is not buildable"})
            continue
        if path == api_name:
            root.row = r
            continue
        if not path.startswith(api_name + "/"):
            gaps.append({"path": path,
                         "reason": f"row does not belong to {api_name} (registry defect)"})
            continue
        parent_path, leaf = path.rsplit("/", 1)
        if leaf.startswith("@"):
            name = leaf[1:]
            if name == "xmlns":
                continue
            attr = _Attr(name=name, path=path, row=r)
            ensure(parent_path).attrs.append(attr)
            attrs[path] = attr
        else:
            node = ensure(path)
            node.row = r
    return root, elems, attrs


# ── inclusion ────────────────────────────────────────────────────────────────

def _min_occurs(row: Any) -> int | None:
    m = _OCCURRENCE.match(str(getattr(row, "occurrence", None) or ""))
    return int(m.group(1)) if m else None


def _max_occurs(row: Any) -> int | None:
    m = _OCCURRENCE.match(str(getattr(row, "occurrence", None) or ""))
    if not m:
        return None
    hi = m.group(2).lower()
    return None if hi in ("n", "*") else int(hi)


def _mandatory_flag(row: Any) -> str:
    return str(getattr(row, "mandatory", None) or "").strip().upper()


def _required(row: Any) -> bool:
    if row is None:
        return False
    flag = _mandatory_flag(row)
    if flag == "Y":
        return True
    if flag == "C":
        # conditional is never "required by default" — inclusion is decided
        # by whether the caller supplied a value (see module docstring).
        return False
    lo = _min_occurs(row)
    return lo is not None and lo >= 1


def _decide_inclusion(root: _Node, *, marked: set[str], include_all: bool,
                      conditional_excluded: list[dict],
                      excluded_optional: list[str]) -> None:
    """Two passes, because requiredness is RELATIVE. A mandatory attribute on
    an optional element is mandatory only when that element appears — the same
    relativity `_ancestor_absent` honors on the judging side — so being
    required never pulls an ancestor chain in. Only an EXPLICIT value does
    (pass 1); pass 2 then applies required/optional/conditional within each
    included parent, and reports exclusions only at the included frontier
    (an excluded subtree's interior would be noise)."""
    pulled: set[str] = set()

    def pull(node: _Node) -> bool:
        hit = node.path in marked or any(a.path in marked for a in node.attrs)
        for c in node.children:
            hit = pull(c) or hit
        if hit:
            pulled.add(node.path)
        return hit

    pull(root)

    def keep(row: Any, path: str) -> bool:
        if path in marked:
            return True
        if row is not None and _mandatory_flag(row) == "C":
            condition = str(getattr(row, "condition_text", None) or "").strip()
            conditional_excluded.append(
                {"path": path, "condition": condition or "(no condition text recorded)"})
            return False
        if _required(row) or include_all:
            return True
        excluded_optional.append(path)
        return False

    def mark(node: _Node, parent_in: bool) -> None:
        if node.parent is None:
            node.include = True
        elif not parent_in:
            node.include = False
        else:
            node.include = node.path in pulled or keep(node.row, node.path)
        for a in node.attrs:
            a.include = node.include and keep(a.row, a.path)
        for c in node.children:
            mark(c, node.include)

    mark(root, True)


# ── constraint-derived values (the forward direction) ────────────────────────

def _char_from_class(cls: str) -> str | None:
    try:
        rx = re.compile(cls)
    except re.error:
        return None
    for cand in "Aa01zZ9 _-./:+":
        if rx.fullmatch(cand):
            return cand
    return None


def _synth_pattern(src: str) -> str | None:
    """A minimal string for a regex — literals, escapes, classes, groups with
    first-alternative choice, and counted quantifiers. Anything fancier
    (lookarounds, backrefs) returns None; the caller verifies the result with
    `re.fullmatch` anyway, so a wrong guess degrades to the datatype dummy
    rather than to an invalid payload."""
    pos, n = 0, len(src)

    def skip_rest_of_alternation() -> None:
        nonlocal pos
        depth = 0
        while pos < n:
            c = src[pos]
            if c == "\\":
                pos += 2
                continue
            if c == "[":
                while pos < n and src[pos] != "]":
                    pos += 2 if src[pos] == "\\" else 1
            elif c == "(":
                depth += 1
            elif c == ")":
                if depth == 0:
                    return
                depth -= 1
            pos += 1

    def parse_quant() -> int | None:
        nonlocal pos
        if pos >= n:
            return 1
        ch = src[pos]
        if ch == "+":
            pos += 1
        elif ch in "*?":
            pos += 1
            ch = "*"
        elif ch == "{":
            m = re.match(r"\{(\d+)(?:,\d*)?\}", src[pos:])
            if not m:
                return None
            pos += m.end()
            if pos < n and src[pos] == "?":
                pos += 1
            return int(m.group(1))
        else:
            return 1
        if pos < n and src[pos] == "?":     # lazy marker changes nothing here
            pos += 1
        return 1 if ch == "+" else 0

    def parse_atom() -> str | None:
        nonlocal pos
        ch = src[pos]
        if ch in "^$":
            pos += 1
            return ""
        if ch == "\\":
            if pos + 1 >= n:
                return None
            esc = src[pos + 1]
            pos += 2
            return {"d": "1", "D": "x", "w": "a", "W": "-", "s": " ", "S": "x"}.get(esc, esc)
        if ch == ".":
            pos += 1
            return "a"
        if ch == "(":
            pos += 1
            if src.startswith("?:", pos):
                pos += 2
            elif pos < n and src[pos] == "?":
                return None            # lookaround/named/conditional group
            inner = parse_seq()
            if inner is None or pos >= n or src[pos] != ")":
                return None
            pos += 1
            return inner
        if ch == "[":
            j = pos + 1
            if j < n and src[j] == "^":
                j += 1
            start = j
            while j < n and (src[j] != "]" or j == start):
                j += 2 if src[j] == "\\" else 1
            if j >= n:
                return None
            cls = src[pos:j + 1]
            pos = j + 1
            return _char_from_class(cls)
        if ch in "+*?{)":
            return None
        pos += 1
        return ch

    def parse_seq() -> str | None:
        nonlocal pos
        out: list[str] = []
        while pos < n and src[pos] != ")":
            if src[pos] == "|":
                pos += 1
                skip_rest_of_alternation()
                break
            atom = parse_atom()
            if atom is None:
                return None
            rep = parse_quant()
            if rep is None:
                return None
            out.append(atom * rep)
        return "".join(out)

    try:
        result = parse_seq()
    except RecursionError:
        return None
    if result is None or pos < n and src[pos] == ")":
        return None
    return result


def _value_for_pattern(pattern: str) -> str | None:
    """Same admission rules as `assert_pattern`: a pattern the judge would
    refuse (over-long, nested-quantifier, non-compiling) is not synthesized —
    the judge SKIPs it, so any dummy satisfies the round-trip anyway."""
    if len(pattern) > _MAX_PATTERN_CHARS or _NESTED_QUANTIFIER.search(pattern):
        return None
    try:
        compiled = re.compile(pattern)
    except re.error:
        return None
    candidate = _synth_pattern(pattern)
    if candidate is not None and compiled.fullmatch(candidate):
        return candidate
    return None


def _datatype_value(row: Any, providers: Mapping[str, Callable[[], str]]) -> str:
    """A `datatype`/`length_rule`-conforming dummy — the value floor when no
    enum, pattern, or configured value narrows the choice."""
    label = str(getattr(row, "datatype", None) or "").strip().lower()
    rule = str(getattr(row, "length_rule", None) or "")
    exact = _EXACT_LEN.match(rule)
    min_len = _MIN_LEN.search(rule)
    max_len = _MAX_LEN.search(rule)
    min_inc = _MIN_INC.search(rule)
    max_inc = _MAX_INC.search(rule)

    if "date" in label and "time" in label:
        return providers["timestamp"]()
    if label == "date":
        return providers["date"]()
    if label in ("boolean", "bool"):
        return "true"

    if label in ("numeric", "number", "decimal") or min_inc or max_inc:
        if min_inc:
            v = min_inc.group(1)
        elif exact:
            v = "1" + "0" * (int(exact.group(1)) - 1)
        elif min_len:
            v = "1" + "0" * (int(min_len.group(1)) - 1)
        else:
            v = "1"
        if max_inc and float(v) > float(max_inc.group(1)):
            v = max_inc.group(1)
        return v

    length = int(exact.group(1)) if exact \
        else int(min_len.group(1)) if min_len else 1
    if max_len:
        length = min(length, int(max_len.group(1)))
    return "A" * max(length, 0)


def _auto_value(row: Any, path: str, providers: Mapping[str, Callable[[], str]],
                gaps: list[dict]) -> tuple[str, str]:
    """(value, source-label) from the row's own constraints."""
    enum = getattr(row, "enum_values", None) or []
    if enum:
        return str(enum[0]), "enum"
    pattern = str(getattr(row, "pattern_rule", None) or "")
    if pattern:
        v = _value_for_pattern(pattern)
        if v is not None:
            return v, "pattern"
        gaps.append({"path": path,
                     "reason": f"pattern {pattern!r} not synthesizable — datatype dummy used"})
    return _datatype_value(row, providers), "datatype"


def _judge_value(row: Any, path: str, value: str, gaps: list[dict]) -> None:
    """The forward direction judging its own output with the judge's own
    functions — a produced (or configured) value that FAILs its row's
    constraints is emitted anyway (the caller may probe on purpose) but is
    never emitted SILENTLY."""
    checks = [
        assert_datatype({"datatype": getattr(row, "datatype", None)}, [value]),
        assert_length({"length_rule": getattr(row, "length_rule", None)}, [value]),
        assert_enum({"enum_values": getattr(row, "enum_values", None)}, [value]),
    ]
    if str(getattr(row, "pattern_rule", None) or ""):
        checks.append(assert_pattern(
            {"pattern_rule": getattr(row, "pattern_rule", None)}, [value]))
    for outcome in checks:
        if outcome.status == FAIL:
            gaps.append({"path": path, "reason": outcome.reason})


# ── explicit layers ──────────────────────────────────────────────────────────

def _explicit_map(api_name: str, paths: set[str],
                  values: Mapping[str, str],
                  profile: Mapping[str, Any]) -> dict[str, tuple[str, Any]]:
    """path → (source, entry). Case values are full paths only — a tail match
    there could silently hit the wrong branch of the message. Profile keys may
    be message-relative because one network profile serves every message."""
    out: dict[str, tuple[str, Any]] = {}
    for path in paths:
        tail = path.split("/", 1)[1] if "/" in path else path
        if path in values:
            out[path] = ("case", values[path])
        elif path in profile:
            out[path] = ("profile", profile[path])
        elif tail in profile:
            out[path] = ("profile", profile[tail])
    return out


def _resolve_entry(entry: Any, providers: Mapping[str, Callable[[], str]],
                   path: str, gaps: list[dict]) -> tuple[str | None, str | None]:
    """(value, provider-name) for an explicit layer entry."""
    if isinstance(entry, Mapping):
        name = str(entry.get("provider") or "")
        fn = providers.get(name)
        if fn is None:
            gaps.append({"path": path, "reason": f"unknown provider {name!r}"})
            return None, None
        return fn(), name
    return str(entry), None


# ── repeats and clones ───────────────────────────────────────────────────────

def _clone_node(node: _Node, parent: _Node | None) -> _Node:
    copy = _Node(tag=node.tag, path=node.path, row=node.row, parent=parent,
                 include=node.include, text=node.text, provider=node.provider)
    copy.attrs = [_Attr(name=a.name, path=a.path, row=a.row, include=a.include,
                        value=a.value, provider=a.provider) for a in node.attrs]
    copy.children = [_clone_node(c, copy) for c in node.children]
    return copy


def _refresh_providers(node: _Node, providers: Mapping[str, Callable[[], str]]) -> None:
    """Re-run computed values inside a cloned subtree — two copies of a
    repeated element must not share one msgId."""
    if node.provider:
        fn = providers.get(node.provider)
        if fn:
            node.text = fn()
    for a in node.attrs:
        if a.provider:
            fn = providers.get(a.provider)
            if fn:
                a.value = fn()
    for c in node.children:
        _refresh_providers(c, providers)


def _insert_clones(node: _Node, extra: int,
                   providers: Mapping[str, Callable[[], str]]) -> None:
    parent = node.parent
    if parent is None or extra <= 0:
        return
    at = parent.children.index(node)
    for i in range(extra):
        copy = _clone_node(node, parent)
        _refresh_providers(copy, providers)
        parent.children.insert(at + 1 + i, copy)


# ── fault injection ──────────────────────────────────────────────────────────

FAULT_KINDS = ("missing", "enum", "pattern", "length_over", "length_under",
               "datatype", "occurrence_extra")


def _current_value(target: _Node | _Attr) -> str:
    return (target.value if isinstance(target, _Attr) else target.text) or ""


def _set_value(target: _Node | _Attr, value: str) -> None:
    if isinstance(target, _Attr):
        target.value, target.provider = value, None
    else:
        target.text, target.provider = value, None


def _apply_fault(fault: Mapping[str, str], elems: dict[str, _Node],
                 attrs: dict[str, _Attr],
                 providers: Mapping[str, Callable[[], str]]) -> dict:
    path = str(fault.get("path") or "")
    kind = str(fault.get("kind") or "")
    if kind not in FAULT_KINDS:
        raise FaultNotApplicable(
            f"unknown fault kind {kind!r} — one of {', '.join(FAULT_KINDS)}")
    target: _Node | _Attr | None = attrs.get(path) or elems.get(path)
    if target is None:
        raise FaultNotApplicable(f"no registry field at {path!r}")
    row = target.row
    if row is None:
        raise FaultNotApplicable(f"{path!r} carries no registry row to violate")

    def entry(expected: tuple[str, ...]) -> dict:
        return {"path": path, "kind": kind, "expected_failure": list(expected)}

    if kind == "missing":
        expected = tuple(k for k, hit in (
            ("mandatory", _mandatory_flag(row) == "Y"),
            ("occurrence", (_min_occurs(row) or 0) >= 1)) if hit)
        if not expected:
            raise FaultNotApplicable(
                f"{path!r} is not required — removing it violates nothing")
        if isinstance(target, _Attr):
            target.include = False
        else:
            if target.parent is None:
                raise FaultNotApplicable("cannot remove the message root")
            target.parent.children.remove(target)
        return entry(expected)

    if kind == "occurrence_extra":
        if isinstance(target, _Attr):
            raise FaultNotApplicable("occurrence_extra applies to elements only")
        hi = _max_occurs(row)
        if hi is None:
            raise FaultNotApplicable(
                f"{path!r} has no finite occurrence maximum to exceed")
        _insert_clones(target, hi, providers)   # 1 present + hi clones = hi+1 total
        return entry(("occurrence",))

    valid = _current_value(target)
    if kind == "enum":
        allowed = {str(v) for v in (getattr(row, "enum_values", None) or [])}
        if not allowed:
            raise FaultNotApplicable(f"{path!r} declares no enum to violate")
        # same length as a real member so the length assertion stays out of it
        broken = next((valid[:-1] + c for c in "XZQ#"
                       if valid[:-1] + c not in allowed), None)
        if broken is None:
            raise FaultNotApplicable(f"could not construct a value outside enum "
                                     f"{sorted(allowed)}")
        _set_value(target, broken)
        return entry(("enum",))
    if kind == "pattern":
        raw = str(getattr(row, "pattern_rule", None) or "")
        compiled = re.compile(raw) if raw else None
        if compiled is None:
            raise FaultNotApplicable(f"{path!r} declares no pattern to violate")
        broken = next((v for v in (valid + "#", "#" + valid, "#")
                       if not compiled.fullmatch(v)), None)
        if broken is None:
            raise FaultNotApplicable(f"pattern {raw!r} matches every probe value")
        _set_value(target, broken)
        return entry(("pattern",))
    if kind == "datatype":
        label = str(getattr(row, "datatype", None) or "").strip().lower()
        if label not in ("numeric", "number", "decimal"):
            raise FaultNotApplicable(
                f"datatype {label!r} is not machine-checked (the judge SKIPs it) "
                "— a datatype fault would go undetected")
        _set_value(target, "X1")
        return entry(("datatype",))

    rule = str(getattr(row, "length_rule", None) or "")
    exact = _EXACT_LEN.match(rule)
    min_len = _MIN_LEN.search(rule)
    max_len = _MAX_LEN.search(rule)
    min_inc = _MIN_INC.search(rule)
    max_inc = _MAX_INC.search(rule)
    pad = (valid[:1] if valid else "A")
    if pad not in "0123456789" and str(getattr(row, "datatype", "") or "").strip().lower() \
            in ("numeric", "number", "decimal"):
        pad = "9"
    if kind == "length_over":
        if max_len or exact:
            bound = int((max_len or exact).group(1))
            _set_value(target, valid.ljust(bound, pad)[:bound] + pad)
        elif max_inc:
            _set_value(target, str(int(float(max_inc.group(1))) + 1))
        else:
            raise FaultNotApplicable(f"{path!r} has no upper length/value bound")
        return entry(("length",))
    if kind == "length_under":
        if (min_len or exact) and int((min_len or exact).group(1)) >= 1:
            bound = int((min_len or exact).group(1))
            base = valid if len(valid) >= bound else pad * bound
            _set_value(target, base[:bound - 1])
        elif min_inc:
            _set_value(target, str(int(float(min_inc.group(1))) - 1))
        else:
            raise FaultNotApplicable(f"{path!r} has no lower length/value bound")
        return entry(("length",))
    raise FaultNotApplicable(f"unhandled fault kind {kind!r}")   # unreachable


# ── serialization ────────────────────────────────────────────────────────────

def _to_xml(root: _Node, namespace: str | None) -> str:
    def build(node: _Node, parent_el: ET.Element | None) -> ET.Element:
        el = ET.Element(node.tag) if parent_el is None \
            else ET.SubElement(parent_el, node.tag)
        for a in node.attrs:
            if a.include:
                el.set(a.name, a.value if a.value is not None else "")
        kept = [c for c in node.children if c.include]
        if kept:
            for c in kept:
                build(c, el)
        elif node.text is not None:
            el.text = node.text
        return el

    root_el = build(root, None)
    if namespace:
        # a plain `xmlns` attribute serializes as the declaration; on re-parse
        # every descendant lands in the namespace (Clark tags), which is what
        # the codec's `@xmlns` recovery and the xmlschema gate both read.
        root_el.set("xmlns", namespace)
    ET.indent(root_el)
    return ET.tostring(root_el, encoding="unicode")


# ── entry points ─────────────────────────────────────────────────────────────

def generate_payload(
    *,
    api_name: str,
    namespace: str | None = None,
    fields: Sequence[Any],
    values: Mapping[str, str] | None = None,
    profile: Mapping[str, Any] | None = None,
    providers: Mapping[str, Callable[[], str]] | None = None,
    repeats: Mapping[str, int] | None = None,
    fault: Mapping[str, str] | None = None,
    include: str = "mandatory",
) -> GeneratedPayload:
    """One wire payload from registry rows plus the layered values above.

    Pure — no DB, no LLM; nondeterminism enters only through `providers`
    (injectable). Raises `FaultNotApplicable` for an unconstructible fault;
    every other shortfall is a `report` entry, never a silent omission."""
    values = dict(values or {})
    profile = dict(profile or {})
    prov = {**_default_providers(), **(providers or {})}
    repeats = dict(repeats or {})
    if include not in ("mandatory", "all"):
        raise ValueError(f"include must be 'mandatory' or 'all', not {include!r}")

    gaps: list[dict] = []
    conditional_excluded: list[dict] = []
    excluded_optional: list[str] = []

    active = [f for f in fields
              if str(getattr(f, "status", "active") or "active") == "active"]
    root, elems, attrs = _build_tree(api_name, active, gaps)

    known = set(elems) | set(attrs)
    explicit = _explicit_map(api_name, known, values, profile)
    unresolved = sorted(p for p in values if p not in known)
    for p in unresolved:
        gaps.append({"path": p, "reason": "case value names no registry field"})

    marked = set(explicit)
    if fault:
        marked.add(str(fault.get("path") or ""))  # a fault target must exist to be broken
    _decide_inclusion(root, marked=marked, include_all=(include == "all"),
                      conditional_excluded=conditional_excluded,
                      excluded_optional=excluded_optional)

    value_sources: dict[str, str] = {}
    provider_values: dict[str, str] = {}

    def resolve(target: _Node | _Attr) -> None:
        row, path = target.row, target.path
        hit = explicit.get(path)
        if hit is not None:
            source, entry = hit
            value, provider_name = _resolve_entry(entry, prov, path, gaps)
            if value is None:                      # unknown provider → fall through
                value, source = _auto_value(row, path, prov, gaps) if row \
                    else ("", "none")
                provider_name = None
            elif provider_name:
                source = f"provider:{provider_name}"
                provider_values[path] = value
        elif row is not None:
            value, source = _auto_value(row, path, prov, gaps)
            provider_name = None
        else:
            value, source, provider_name = "", "none", None
        if row is not None:
            _judge_value(row, path, value, gaps)
        _set_value(target, value)
        target.provider = provider_name
        value_sources[path] = source

    def fill(node: _Node) -> None:
        if not node.include:
            return
        for a in node.attrs:
            if a.include:
                resolve(a)
        kept = [c for c in node.children if c.include]
        if kept:
            for c in kept:
                fill(c)
        elif node.row is not None or node.path in explicit:
            resolve(node)

    fill(root)

    for path, total in repeats.items():
        node = elems.get(path)
        if node is None or not node.include:
            gaps.append({"path": path, "reason": "repeat target absent from payload"})
            continue
        hi = _max_occurs(node.row) if node.row is not None else None
        if hi is not None and total > hi:
            gaps.append({"path": path,
                         "reason": f"{total} repeats exceed occurrence maximum {hi}"})
        _insert_clones(node, total - 1, prov)

    fault_report = None
    if fault:
        fault_report = _apply_fault(fault, elems, attrs, prov)

    xml = _to_xml(root, namespace)
    report = {
        "api_name": api_name,
        "mode": include,
        "included_elements": sum(1 for e in elems.values() if e.include),
        "included_attributes": sum(1 for a in attrs.values() if a.include),
        "excluded_optional": sorted(excluded_optional),
        "conditional_excluded": conditional_excluded,
        "value_sources": value_sources,
        "provider_values": provider_values,
        "unresolved_values": unresolved,
        "gaps": gaps,
        "fault": fault_report,
    }
    return GeneratedPayload(xml=xml, report=report)


def payload_for_message(msg: Any, **kwargs: Any) -> GeneratedPayload:
    """`generate_payload` over an `ApiMessage` ORM row — active fields, in the
    relationship's `position` order."""
    return generate_payload(
        api_name=msg.api_name,
        namespace=msg.namespace,
        fields=[f for f in msg.fields
                if str(getattr(f, "status", "active") or "active") == "active"],
        **kwargs,
    )
