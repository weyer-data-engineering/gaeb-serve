"""Reading GAEB files and writing bids, on top of pyGAEB.

Everything engine-specific lives in this module, so the HTTP API in ``app.py`` stays
the same if the engine is ever replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from lxml import etree
from pygaeb import ExchangePhase, GAEBParser, GAEBWriter, ItemType, PhaseTransition, SourceVersion
from pygaeb import __version__ as ENGINE_VERSION
from pygaeb.convert import to_markdown
from pygaeb.exceptions import PyGAEBError
from pygaeb.models.document import Party
from pygaeb.models.enums import ComplementKind, ValidationSeverity
from pygaeb.models.item import Item
from pygaeb.models.order import Address
from pygaeb.writer.version_registry import WRITABLE_VERSIONS

from gaeb_serve.config import Settings

ENGINE = f"pyGAEB {ENGINE_VERSION}"


class GaebError(Exception):
    """The file is not a GAEB document this service can read, or the request does not fit it."""


@dataclass
class BidderField:
    mark: str
    label: str
    value: str


@dataclass
class Position:
    oz: str
    short_text: str
    qty: Decimal | None
    qty_to_be_determined: bool
    unit: str | None
    item_type: str
    counts_in_total: bool
    unit_price: Decimal | None
    total_price: Decimal | None
    bidder_fields: list[BidderField]


@dataclass
class ParsedDocument:
    version: str
    phase: str
    kind: str
    project: str | None
    client: str | None
    currency: str
    markdown: str
    positions: list[Position]
    findings: list[str]


@dataclass
class BidPosition:
    unit_price: Decimal | None = None
    bidder_fields: dict[str, str] = field(default_factory=dict)


@dataclass
class Bidder:
    name: str
    street: str | None = None
    postal_code: str | None = None
    city: str | None = None


@dataclass
class Bid:
    x84: bytes
    file_name: str
    total: Decimal
    unpriced: list[str]
    unknown: list[str]
    unfilled_fields: list[str]
    warnings: list[str]


def parse(data: bytes, file_name: str, settings: Settings) -> ParsedDocument:
    """Reads a GAEB file into Markdown and a list of positions.

    Raises:
        GaebError: when the file cannot be read as GAEB.
    """
    doc = _parse(data, file_name, settings)
    extras = _read_extras(data)
    alternatives = _mark_alternatives(doc, extras)
    positions = [_position(item) for item in doc.award.boq.iter_items()] if doc.is_procurement else []
    return ParsedDocument(
        version=doc.source_version.value,
        phase=doc.exchange_phase.value,
        kind=doc.document_kind.value,
        project=doc.award.project_name,
        client=doc.award.client,
        currency=doc.award.currency,
        markdown=_with_remarks(_with_alternative_bases(to_markdown(doc), alternatives), extras.remarks),
        positions=positions,
        findings=[
            f"{result.severity.value}: {result.message}"
            for result in doc.validation_results
            if result.severity != ValidationSeverity.INFO
        ],
    )


def bid(
    data: bytes,
    file_name: str,
    prices: dict[str, BidPosition],
    bidder: Bidder | None,
    settings: Settings,
) -> Bid:
    """Turns a tender into a bid (X84) with the given unit prices and bidder fields.

    Positions are addressed by their full OZ (``01.02.0010``). A position without a
    price stays unpriced in the bid; the caller decides whether that is acceptable.

    Raises:
        GaebError: when the file is no tender or cannot be read.
    """
    tender = _parse(data, file_name, settings)
    if not tender.is_procurement or tender.exchange_phase is not ExchangePhase.X83:
        raise GaebError(
            f"A bid can only be made from a tender (X83), not from a {tender.exchange_phase.value} file."
        )

    _mark_alternatives(tender, _read_extras(data))
    doc = PhaseTransition.tender_to_bid(tender)
    if bidder is not None:
        doc.award.contractor = Party(address=Address(
            name=bidder.name, street=bidder.street, pcode=bidder.postal_code, city=bidder.city,
        ))
    known: set[str] = set()
    unpriced: list[str] = []
    unfilled: list[str] = []
    for item in doc.award.boq.iter_items():
        oz = item.full_oz
        known.add(oz)
        wanted = prices.get(oz, BidPosition())
        _apply(item, wanted)
        if _needs_price(item) and item.unit_price is None:
            unpriced.append(oz)
        unfilled.extend(f"{oz} #{c.mark}" for c in _bidder_fields(item) if c.empty or not c.body)

    xml, warnings = GAEBWriter.to_bytes(
        doc,
        phase=ExchangePhase.X84,
        # A bid answers in the tender's own DA XML version where the writer supports it.
        target_version=tender.source_version if tender.source_version in WRITABLE_VERSIONS
        else SourceVersion.DA_XML_33,
        prog_system=settings.prog_system,
        prog_name=settings.prog_name,
    )
    return Bid(
        x84=_without_alternative_markers(xml),
        file_name=_bid_file_name(file_name),
        total=doc.grand_total,
        unpriced=unpriced,
        unknown=sorted(set(prices) - known),
        unfilled_fields=unfilled,
        # X84 leaves out the tender's prose by design; only real losses are worth reporting.
        # Alternatives are fixed up in _without_alternative_markers.
        warnings=[w for w in warnings if "not part of X84" not in w and "Alternative written as" not in w],
    )


def _parse(data: bytes, file_name: str, settings: Settings):
    if not data:
        raise GaebError("The file is empty.")
    try:
        return GAEBParser.parse_bytes(data, filename=file_name, max_file_size=settings.max_file_size_bytes)
    except PyGAEBError as e:
        raise GaebError(str(e)) from e


def _position(item: Item) -> Position:
    return Position(
        oz=item.full_oz,
        short_text=item.short_text,
        qty=item.qty,
        qty_to_be_determined=item.qty_tbd,
        unit=item.unit,
        item_type=item.item_type.value,
        counts_in_total=item.affects_total,
        unit_price=item.unit_price,
        total_price=item.total_price,
        bidder_fields=[BidderField(c.mark, c.label, c.value) for c in _bidder_fields(item)],
    )


def _bidder_fields(item: Item):
    if item.long_text is None:
        return []
    return [c for c in item.long_text.complements if c.kind == ComplementKind.BIDDER]


def _apply(item: Item, wanted: BidPosition) -> None:
    if wanted.unit_price is not None:
        item.unit_price = wanted.unit_price
        item.total_price = item.computed_total
    if item.long_text is not None:
        for mark, text in wanted.bidder_fields.items():
            item.long_text.fill_bidder(mark, text)


def _needs_price(item: Item) -> bool:
    return item.item_type is not ItemType.TEXT_ONLY


def _bid_file_name(file_name: str) -> str:
    stem = file_name.rsplit(".", 1)[0] if "." in file_name else file_name
    return f"{stem or 'Angebot'}.X84"


# pyGAEB 1.21 neither reads alternative positions (ALNGroupNo/ALNSerNo) nor remarks (<Remark>),
# so both are read from the XML here. Remove once the engine supports them.

_SAFE_XML = etree.XMLParser(resolve_entities=False, no_network=True, remove_comments=True)


@dataclass
class _Extras:
    # full OZ -> (group, serial number); serial number "0" is the base position.
    alternatives: dict[str, tuple[str, str]] = field(default_factory=dict)
    # (anchor OZ, before the anchor?, text)
    remarks: list[tuple[str, bool, str]] = field(default_factory=list)


def _local(element) -> str:
    return etree.QName(element).localname if isinstance(element.tag, str) else ""


def _child(element, name: str):
    return next((c for c in element if _local(c) == name), None)


def _read_extras(data: bytes) -> _Extras:
    extras = _Extras()
    try:
        root = etree.fromstring(data, _SAFE_XML)
    except etree.XMLSyntaxError:
        return extras  # not DA XML 3.x; pyGAEB already read what there is
    for boq in root.iter("{*}BoQ"):
        body = _child(boq, "BoQBody")
        if body is not None:
            _walk(body, [], extras)
    return extras


def _walk(body, prefix: list[str], extras: _Extras) -> None:
    for node in body:
        if _local(node) == "BoQCtgy":
            inner = _child(node, "BoQBody")
            if inner is not None:
                _walk(inner, [*prefix, node.get("RNoPart", "")], extras)
        elif _local(node) == "Itemlist":
            _walk_itemlist(node, prefix, extras)


def _walk_itemlist(itemlist, prefix: list[str], extras: _Extras) -> None:
    pending: list[str] = []
    last_oz = None
    for node in itemlist:
        if _local(node) == "Item":
            last_oz = ".".join([*prefix, node.get("RNoPart", "")])
            group, serial = _child(node, "ALNGroupNo"), _child(node, "ALNSerNo")
            if group is not None and serial is not None:
                extras.alternatives[last_oz] = ((group.text or "").strip(), (serial.text or "").strip())
            extras.remarks.extend((last_oz, True, text) for text in pending)
            pending = []
        elif _local(node) == "Remark":
            text = "\n\n".join(
                " ".join("".join(p.itertext()).split()) for p in node.iter("{*}p") if "".join(p.itertext()).strip()
            )
            if text:
                pending.append(text)
    if pending and last_oz:
        extras.remarks.extend((last_oz, False, text) for text in pending)


def _mark_alternatives(doc, extras: _Extras) -> dict[str, str]:
    """Marks alternative positions so they stay out of the sums; returns alternative OZ -> base OZ."""
    if not extras.alternatives or not doc.is_procurement:
        return {}
    bases = {group: oz for oz, (group, serial) in extras.alternatives.items() if serial == "0"}
    result = {}
    for item in doc.award.boq.iter_items():
        group, serial = extras.alternatives.get(item.full_oz, (None, "0"))
        if serial != "0" and item.item_type is ItemType.NORMAL:
            item.item_type = ItemType.ALTERNATIVE
            result[item.full_oz] = bases.get(group, "")
    return result


def _with_alternative_bases(markdown: str, alternatives: dict[str, str]) -> str:
    lines = markdown.split("\n")
    for oz, base in alternatives.items():
        start = _heading_index(lines, oz)
        if start is None or not base:
            continue
        for i in range(start + 1, len(lines)):
            if lines[i].startswith(("**", "#")):
                break
            if "*Alternativposition*" in lines[i]:
                lines[i] = lines[i].replace("*Alternativposition*", f"*Alternativposition zu {base}*", 1)
                break
    return "\n".join(lines)


def _with_remarks(markdown: str, remarks: list[tuple[str, bool, str]]) -> str:
    lines = markdown.split("\n")
    for oz, before, text in reversed(remarks):
        index = _heading_index(lines, oz)
        if index is None:
            continue
        if not before:
            index += 1
            while index < len(lines) and not lines[index].startswith(("**", "#")):
                index += 1
        lines[index:index] = [f"*Hinweis:* {text}", ""]
    return "\n".join(lines)


def _heading_index(lines: list[str], oz: str) -> int | None:
    return next((i for i, line in enumerate(lines) if line.startswith(f"**{oz} ")), None)


def _without_alternative_markers(xml: bytes) -> bytes:
    """Drops pyGAEB's own <AlternativeItem> marker, which other GAEB software does not read.

    The receiving software knows the alternatives from its own tender; the bid only carries prices.
    """
    if b"AlternativeItem" not in xml:
        return xml
    root = etree.fromstring(xml, _SAFE_XML)
    for marker in list(root.iter("{*}AlternativeItem")):
        marker.getparent().remove(marker)
    return etree.tostring(root, xml_declaration=True, encoding="utf-8")
