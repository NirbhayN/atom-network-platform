# Copyright 2026 The ATOM Authors
# SPDX-License-Identifier: MIT

"""Shared XSD packaging.

The XSD agent (`app/agents/xsd.py`) stores its output as markdown in
`XSD.content` — it may wrap 0, 1, or many ```xml schema blocks. Pulling the
individual `.xsd` files back out is needed in two places that MUST agree:

  * The Authority operator download (`api.change_requests`) — serves a single .xsd
    or a .zip of all schemas, and
  * the partner kit shipment (`services.change_dispatch`) — bundles the same
    schemas so the partner downloads every file, not just the first block.

Keeping the extraction here (one source of truth) stops the two surfaces from
drifting on filename detection.
"""
import re

_XML_FENCE_RE    = re.compile(r"```\s*xml\s*\n(?P<body>.*?)```", re.DOTALL | re.IGNORECASE)
_XSD_FILENAME_RE = re.compile(r"`([A-Za-z0-9._\-]+\.xsd)`")


def extract_xsd_blocks(markdown: str) -> list[tuple[str, str]]:
    """Pull out every ```xml block in `markdown`. Returns a list of
    `(filename, xml_body)` tuples in document order.

    Schema filenames appear in a heading like
        ### `network-annotations.xsd`
    so we take the last `<name>.xsd` token BETWEEN the previous block's end and this
    block's start (still capped at ~600 chars) — scoping to the inter-block region
    stops a filename mentioned in the PREVIOUS section's prose ("regenerate X.xsd
    consumers") from claiming the next block. Positional fallback (`xsd_part_<n>.xsd`)
    when the agent didn't name it. A filename emitted twice (e.g. a corrected
    re-emission in the same document) keeps its position with the LAST body winning —
    shipping two bodies under one name made the zip an accident of iteration order.
    """
    md = markdown or ""
    out: list[tuple[str, str]] = []
    index_of: dict[str, int] = {}
    prev_end = 0
    for i, m in enumerate(_XML_FENCE_RE.finditer(md), start=1):
        body = m.group("body").strip()
        prefix = md[max(prev_end, m.start() - 600): m.start()]
        prev_end = m.end()
        names = _XSD_FILENAME_RE.findall(prefix)
        fname = names[-1] if names else f"xsd_part_{i}.xsd"
        if fname in index_of:
            out[index_of[fname]] = (fname, body)      # last emission wins
        else:
            index_of[fname] = len(out)
            out.append((fname, body))
    return out
