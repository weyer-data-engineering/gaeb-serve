"""Reads GAEB 90 bills of quantities (``.d81``, ``.d83``) by turning them into GAEB DA XML.

GAEB 90 is the fixed-width predecessor of DA XML: 80-character lines whose first two
characters name the record type and whose last six hold a running line number. pyGAEB has
no reader for it, so this module writes an equivalent DA XML 3.3 tender (X83) that the rest
of the service reads like any other upload.

Records read: 00 header (phase, OZ mask), 01-03 names, 08 currency, 11/12/31 BoQ levels,
20 remark, 21 item, 25 short text, 26 long text, 99 end. Other records are skipped and
reported.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from decimal import Decimal

from lxml import etree

READABLE_PHASES = ("81", "83")
_NS = "http://www.gaeb.de/GAEB_DA_XML/DA83/3.3"
_RECORD = re.compile(r"^\d\d")
_TEXT_WIDTH = 72  # columns 3 to 74


class Gaeb90Error(Exception):
    """The file looks like GAEB 90 but cannot be converted."""


def looks_like_gaeb90(data: bytes) -> bool:
    """True for a file that starts with a GAEB 90 header record (``00`` with line number 1)."""
    first = data.split(b"\n", 1)[0].rstrip(b"\r")
    return first.startswith(b"00") and len(first) >= 78 and first[-6:].strip(b" ").isdigit()


@dataclass
class _Item:
    oz: list[str]
    index: str
    kind: str  # N normal, G base position, A alternative
    provis: str  # N normal, E Bedarfsposition ohne GB, M Bedarfsposition mit GB
    qty: Decimal | None
    unit: str
    alternative: tuple[int, int] | None = None  # (group, serial number; 0 is the base position)
    short: list[str] = field(default_factory=list)
    long: list[str] = field(default_factory=list)


@dataclass
class _Remark:
    lines: list[str] = field(default_factory=list)


@dataclass
class _Level:
    rno: str
    label: list[str] = field(default_factory=list)
    children: list = field(default_factory=list)


@dataclass
class Converted:
    xml: bytes
    phase: str
    findings: list[str]


def to_da_xml(data: bytes) -> Converted:
    """Converts a GAEB 90 bill of quantities into a DA XML 3.3 tender (X83).

    Raises:
        Gaeb90Error: for phases other than 81 and 83, or a broken OZ mask.
    """
    lines = [_text_field(line) for line in _decode(data).splitlines() if _RECORD.match(line)]
    header = lines[0] if lines and lines[0].startswith("00") else ""
    phase = header[10:12]
    if phase not in READABLE_PHASES:
        raise Gaeb90Error(f"GAEB 90 phase D{phase or '??'} is not supported; only D81 and D83 can be read.")
    levels, item_digits, index_used = _mask(header[62:71])

    info: dict[str, str] = {}
    root = _Level("")
    stack = [root]
    current: _Item | _Remark | None = None
    skipped: dict[str, int] = {}
    alternative_group, alternative_serial = 0, 0
    expected_items = None

    for line in lines[1:]:
        record, text = line[:2], line[2:74]
        if record in ("01", "02", "03"):
            info.setdefault(record, text.strip())
        elif record == "08":
            info["currency"], info["currency_label"] = text[:4].strip(), text[6:].strip()
        elif record == "11":
            parts = _split(text[:9], levels)
            depth = len([p for p in parts if p.strip()])
            if not depth:
                continue
            del stack[depth:]
            level = _Level(parts[depth - 1].strip())
            stack[-1].children.append(level)
            stack.append(level)
            current = level
        elif record == "12" and isinstance(current, _Level):
            current.label.append(text.strip())
        elif record == "31":
            parts = _split(text[:9], levels)
            del stack[max(1, len([p for p in parts if p.strip()])):]
            current = None
        elif record == "20":
            current = _Remark()
            stack[-1].children.append(current)
        elif record == "21":
            oz = text[:9]
            kind, provis = text[9:10] or "N", text[10:11] or "N"
            alternative = None
            if kind == "G":
                alternative_group, alternative_serial = alternative_group + 1, 0
                alternative = (alternative_group, 0)
            elif kind == "A" and alternative_group:
                alternative_serial += 1
                alternative = (alternative_group, alternative_serial)
            current = _Item(
                oz=[p.strip() for p in _split(oz, levels)] + [oz[sum(levels):sum(levels) + item_digits].strip()],
                index=oz[sum(levels) + item_digits:sum(levels) + item_digits + 1].strip() if index_used else "",
                kind=kind, provis=provis,
                qty=_qty(text[21:32]), unit=text[32:36].strip(), alternative=alternative,
            )
            stack[-1].children.append(current)
        elif record == "25" and isinstance(current, _Item):
            current.short.append(text)
        elif record == "26" and isinstance(current, (_Item, _Remark)):
            (current.long if isinstance(current, _Item) else current.lines).append(text.rstrip())
        elif record == "99":
            count = text[67:72].strip()
            expected_items = int(count) if count.isdigit() else None
        else:
            skipped[record] = skipped.get(record, 0) + 1

    findings = [f"warning: GAEB 90 record {r} skipped ({n}x)" for r, n in sorted(skipped.items())]
    counted = _count_items(root)
    if expected_items is not None and expected_items != counted:
        findings.append(f"warning: GAEB 90 end record announces {expected_items} positions, {counted} were read")
    xml = _write(root, info, levels, item_digits, phase)
    return Converted(xml=xml, phase=f"D{phase}", findings=findings)


def _decode(data: bytes) -> str:
    # GAEB 90 is specified for the DOS character set; some programs write Windows-1252 instead.
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        pass
    dos_umlauts = sum(data.count(b) for b in (b"\x81", b"\x84", b"\x94", b"\x8e", b"\x99", b"\x9a", b"\xe1"))
    windows_umlauts = sum(data.count(b) for b in (b"\xfc", b"\xe4", b"\xf6", b"\xc4", b"\xd6", b"\xdc", b"\xdf"))
    return data.decode("cp850" if dos_umlauts >= windows_umlauts else "cp1252", errors="replace")


def _text_field(line: str) -> str:
    return line.rstrip("\r\n").ljust(80)


def _mask(mask: str) -> tuple[list[int], int, bool]:
    """Reads the OZ mask, e.g. ``1122PPPPI``: two levels of two digits, four item digits, an index."""
    levels = [mask.count(str(n)) for n in range(1, 5) if str(n) in mask]
    item_digits = mask.count("P")
    if not item_digits:
        raise Gaeb90Error(f"The GAEB 90 header has no valid OZ mask ({mask.strip() or 'empty'}).")
    return levels, item_digits, "I" in mask


def _split(oz: str, levels: list[int]) -> list[str]:
    parts, start = [], 0
    for length in levels:
        parts.append(oz[start:start + length])
        start += length
    return parts


def _qty(field_text: str) -> Decimal | None:
    digits = field_text.strip()
    if not digits.isdigit():
        return None
    return Decimal(digits).scaleb(-3)


def _count_items(level: _Level) -> int:
    return sum(_count_items(c) if isinstance(c, _Level) else isinstance(c, _Item) for c in level.children)


def _join_short(parts: list[str]) -> str:
    text, previous = "", ""
    for part in parts:
        # A line filled to the last column was cut by the exporting program, maybe inside a word.
        glue = "" if len(previous.rstrip()) >= _TEXT_WIDTH else " "
        text = f"{text}{glue}{part.strip()}" if text else part.strip()
        previous = part
    return text


def _paragraphs(lines: list[str]) -> list[str]:
    """Blank lines separate paragraphs; within one, the source line breaks are kept.

    The exporting program wraps at varying widths and lists one property per line, so
    joining lines would merge list entries and splitting them would break sentences.
    """
    paragraphs: list[list[str]] = [[]]
    for line in lines:
        if line.strip():
            paragraphs[-1].append(" ".join(line.split()))
        elif paragraphs[-1]:
            paragraphs.append([])
    return ["\n".join(p) for p in paragraphs if p]


def _el(parent, name: str, text: str | None = None, **attrs):
    element = etree.SubElement(parent, f"{{{_NS}}}{name}", {k: v for k, v in attrs.items() if v})
    if text is not None:
        element.text = text
    return element


def _text(parent, paragraphs: list[str]) -> None:
    text = _el(parent, "Text")
    for paragraph in paragraphs:
        p = _el(text, "p")
        for number, line in enumerate(paragraph.split("\n")):
            if number:
                _el(p, "br")
            _el(p, "span", line)


def _write(root: _Level, info: dict[str, str], levels: list[int], item_digits: int, phase: str) -> bytes:
    gaeb = etree.Element(f"{{{_NS}}}GAEB", nsmap={None: _NS})
    gaeb_info = _el(gaeb, "GAEBInfo")
    _el(gaeb_info, "Version", "3.3")
    _el(gaeb_info, "VersDate", "2021-05")
    _el(gaeb_info, "ProgSystem", f"GAEB 90 D{phase}")
    prj = _el(gaeb, "PrjInfo")
    _el(prj, "NamePrj", (info.get("02") or info.get("01") or "LV")[:20])
    _el(prj, "LblPrj", info.get("02") or info.get("01") or "LV")
    currency = info.get("currency") or "EUR"
    _el(prj, "Cur", currency)
    _el(prj, "CurLbl", info.get("currency_label") or currency)
    award = _el(gaeb, "Award")
    _el(award, "DP", "83")
    award_info = _el(award, "AwardInfo")
    _el(award_info, "Cur", currency)
    _el(award_info, "CurLbl", info.get("currency_label") or currency)
    if info.get("03"):
        _el(_el(_el(award, "OWN"), "Address"), "Name1", info["03"])
    boq = _el(award, "BoQ", ID="BOQ1")
    boq_info = _el(boq, "BoQInfo")
    _el(boq_info, "Name", (info.get("01") or "LV")[:20])
    _el(boq_info, "LblBoQ", info.get("01") or "LV")
    _el(boq_info, "OutlCompl", "AllTxt")
    for length in levels:
        bkdn = _el(boq_info, "BoQBkdn")
        _el(bkdn, "Type", "BoQLevel")
        _el(bkdn, "Length", str(length))
        _el(bkdn, "Num", "Yes")
    bkdn = _el(boq_info, "BoQBkdn")
    _el(bkdn, "Type", "Item")
    _el(bkdn, "Length", str(item_digits))
    _el(bkdn, "Num", "Yes")
    ids = iter(range(1, 1_000_000))
    _write_body(_el(boq, "BoQBody"), root, ids)
    return etree.tostring(gaeb, xml_declaration=True, encoding="utf-8")


def _write_body(body, level: _Level, ids) -> None:
    itemlist = None
    for child in level.children:
        if isinstance(child, _Level):
            itemlist = None
            ctgy = _el(body, "BoQCtgy", ID=f"C{next(ids)}", RNoPart=child.rno)
            _el(_el(ctgy, "LblTx"), "span", " ".join(child.label) or child.rno)
            _write_body(_el(ctgy, "BoQBody"), child, ids)
            continue
        if itemlist is None:
            itemlist = _el(body, "Itemlist")
        if isinstance(child, _Remark):
            paragraphs = _paragraphs(child.lines)
            if paragraphs:
                remark = _el(itemlist, "Remark", ID=f"R{next(ids)}")
                _text(_el(_el(_el(remark, "Description"), "CompleteText"), "DetailTxt"), paragraphs)
        else:
            _write_item(itemlist, child, ids)


def _write_item(itemlist, item: _Item, ids) -> None:
    element = _el(itemlist, "Item", ID=f"I{next(ids)}", RNoPart=item.oz[-1], RNoIndex=item.index or None)
    if item.provis == "M":
        _el(element, "Provis", "WithTotal")
    elif item.provis == "E":
        _el(element, "Provis", "WithoutTotal")
    if item.alternative:
        _el(element, "ALNGroupNo", str(item.alternative[0]))
        _el(element, "ALNSerNo", str(item.alternative[1]))
    if item.qty is None:
        _el(element, "QtyTBD", "Yes")
    else:
        _el(element, "Qty", format(item.qty.normalize(), "f"))
    if item.unit:
        _el(element, "QU", item.unit)
    complete = _el(_el(element, "Description"), "CompleteText")
    paragraphs = _paragraphs(item.long)
    if paragraphs:
        _text(_el(complete, "DetailTxt"), paragraphs)
    _el(_el(_el(complete, "OutlineText"), "OutlTxt"), "TextOutlTxt").append(_span(_join_short(item.short)))


def _span(text: str):
    span = etree.Element(f"{{{_NS}}}span")
    span.text = text
    return span
