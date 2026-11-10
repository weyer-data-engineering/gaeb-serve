"""HTTP API: read GAEB files, write bids."""

from __future__ import annotations

import base64
import logging
import os
import time
from decimal import Decimal
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from gaeb_serve import __version__, gaeb
from gaeb_serve.config import Settings

logging.basicConfig(level=os.getenv("GAEB_LOG_LEVEL", "INFO"), format="%(levelname)s:     %(name)s %(message)s")
log = logging.getLogger("gaeb_serve")
settings = Settings.from_env()

app = FastAPI(
    title="gaeb-serve",
    version=__version__,
    description="Reads GAEB DA XML tenders (X83 and related) and writes priced X84 bids.",
)


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class BidderFieldOut(_Model):
    mark: str
    label: str
    value: str


class PositionOut(_Model):
    oz: str
    short_text: str
    qty: Decimal | None
    qty_to_be_determined: bool
    unit: str | None
    item_type: str
    counts_in_total: bool
    unit_price: Decimal | None
    total_price: Decimal | None
    bidder_fields: list[BidderFieldOut]


class ParseResponse(_Model):
    version: str
    phase: str
    kind: str
    project: str | None
    client: str | None
    currency: str
    markdown: str
    positions: list[PositionOut]
    findings: list[str]


class BidPositionIn(_Model):
    oz: str = Field(min_length=1)
    unit_price: Decimal | None = None
    bidder_fields: dict[str, str] = Field(default_factory=dict)


class BidderIn(_Model):
    name: str = Field(min_length=1)
    street: str | None = None
    postal_code: str | None = None
    city: str | None = None


class BidRequest(_Model):
    positions: list[BidPositionIn]
    bidder: BidderIn | None = None


class BidResponse(_Model):
    file_name: str
    content_base64: str
    total: Decimal
    unpriced: list[str]
    unknown_positions: list[str]
    unfilled_fields: list[str]
    warnings: list[str]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "engine": gaeb.ENGINE}


@app.post("/v1/parse", response_model=ParseResponse, response_model_by_alias=True)
def parse(file: Annotated[UploadFile, File()]) -> ParseResponse:
    """Reads a GAEB file: Markdown for people and models, positions for calculations."""
    data, name = _read(file)
    started = time.monotonic()
    try:
        doc = gaeb.parse(data, name, settings)
    except gaeb.GaebError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    log.info("parsed phase=%s positions=%d bytes=%d ms=%d", doc.phase, len(doc.positions), len(data),
             (time.monotonic() - started) * 1000)
    return ParseResponse.model_validate(doc, from_attributes=True)


@app.post("/v1/bid", response_model=BidResponse, response_model_by_alias=True)
def bid(file: Annotated[UploadFile, File()], prices: Annotated[str, Form()]) -> BidResponse:
    """Turns a tender (X83) into a bid (X84) with the given unit prices and bidder fields."""
    data, name = _read(file)
    try:
        request = BidRequest.model_validate_json(prices)
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=f"prices: {e.errors(include_url=False)}") from e

    wanted = {p.oz: gaeb.BidPosition(p.unit_price, p.bidder_fields) for p in request.positions}
    bidder = gaeb.Bidder(**request.bidder.model_dump()) if request.bidder else None
    try:
        result = gaeb.bid(data, name, wanted, bidder, settings)
    except gaeb.GaebError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    log.info("bid positions=%d unpriced=%d unknown=%d", len(wanted), len(result.unpriced), len(result.unknown))
    return BidResponse(
        file_name=result.file_name,
        content_base64=base64.b64encode(result.x84).decode("ascii"),
        total=result.total,
        unpriced=result.unpriced,
        unknown_positions=result.unknown,
        unfilled_fields=result.unfilled_fields,
        warnings=result.warnings,
    )


def _read(file: UploadFile) -> tuple[bytes, str]:
    data = file.file.read(settings.max_file_size_bytes + 1)
    if len(data) > settings.max_file_size_bytes:
        raise HTTPException(status_code=413, detail="The file is larger than this service accepts.")
    # The extension tells the engine the exchange phase, so the name matters even without a path.
    name = (file.filename or "upload.X83").replace("\\", "/").rsplit("/", 1)[-1]
    return data, name
