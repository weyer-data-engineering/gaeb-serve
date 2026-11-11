"""HTTP API: read GAEB files, write bids."""

from __future__ import annotations

import base64
import hmac
import logging
import os
import time
from decimal import Decimal
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Security, UploadFile
from fastapi.security import APIKeyHeader
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
    description="Reads GAEB tenders (DA XML X83 and related, GAEB 90 D81/D83) and writes priced X84 bids.",
)


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(api_key: Annotated[str | None, Security(_api_key_header)]) -> None:
    """Accepts the request only with one of the configured keys; compared in constant time."""
    if not api_key or not any(hmac.compare_digest(api_key.encode(), key.encode()) for key in settings.api_keys):
        raise HTTPException(status_code=401, detail="Missing or invalid API key.",
                            headers={"WWW-Authenticate": "APIKey"})


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
    markdown: str
    total: Decimal
    unpriced: list[str]
    unknown_positions: list[str]
    unfilled_fields: list[str]
    warnings: list[str]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "version": __version__, "engine": gaeb.ENGINE}


@app.post("/v1/parse", response_model=ParseResponse, response_model_by_alias=True,
          dependencies=[Depends(require_api_key)])
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


@app.post("/v1/bid", response_model=BidResponse, response_model_by_alias=True,
          dependencies=[Depends(require_api_key)])
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
        markdown=result.markdown,
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
