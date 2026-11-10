"""Reading GAEB files and writing bids, on top of pyGAEB.

Everything engine-specific lives in this module, so the HTTP API in ``app.py`` stays
the same if the engine is ever replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

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
TENDER_PHASES = {ExchangePhase.X83, ExchangePhase.X81, ExchangePhase.X82}


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
    positions = [_position(item) for item in doc.award.boq.iter_items()] if doc.is_procurement else []
    return ParsedDocument(
        version=doc.source_version.value,
        phase=doc.exchange_phase.value,
        kind=doc.document_kind.value,
        project=doc.award.project_name,
        client=doc.award.client,
        currency=doc.award.currency,
        markdown=to_markdown(doc),
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
    if not tender.is_procurement or tender.exchange_phase not in TENDER_PHASES:
        raise GaebError(
            f"A bid can only be made from a tender (X83), not from a {tender.exchange_phase.value} file."
        )

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
        # A bid answers in the tender's version; older 2.x tenders are answered in 3.3.
        target_version=tender.source_version if tender.source_version in WRITABLE_VERSIONS
        else SourceVersion.DA_XML_33,
        prog_system=settings.prog_system,
        prog_name=settings.prog_name,
    )
    return Bid(
        x84=xml,
        file_name=_bid_file_name(file_name),
        total=doc.grand_total,
        unpriced=unpriced,
        unknown=sorted(set(prices) - known),
        unfilled_fields=unfilled,
        # X84 leaves out the tender's prose by design; only real losses are worth reporting.
        warnings=[w for w in warnings if "not part of X84" not in w],
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
