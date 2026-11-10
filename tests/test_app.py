import base64
import json
from pathlib import Path

from fastapi.testclient import TestClient

from gaeb_serve.app import app

FIXTURE = Path(__file__).parent / "fixtures" / "tender.x83"
client = TestClient(app)


def upload(name="Ausschreibung.x83", data=None):
    return {"file": (name, FIXTURE.read_bytes() if data is None else data, "application/octet-stream")}


def test_health_names_version_and_engine():
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["engine"].startswith("pyGAEB ")


def test_parse_tender_returns_markdown_and_positions():
    response = client.post("/v1/parse", files=upload())

    assert response.status_code == 200
    body = response.json()
    assert body["phase"] == "X83"
    assert body["project"] == "Sanierung Turnhalle"
    assert [p["oz"] for p in body["positions"]] == ["01.0010", "01.0020", "02.0010", "02.0020"]
    floor = body["positions"][1]
    assert floor["shortText"] == "Sportboden abbrechen"
    assert floor["qty"] == "420.5"
    assert floor["unit"] == "m2"
    assert "01.0020" in body["markdown"]
    assert "einschließlich" in body["markdown"]


def test_parse_marks_bidder_fields_and_eventual_positions():
    positions = client.post("/v1/parse", files=upload()).json()["positions"]

    assert positions[2]["bidderFields"] == [{"mark": "1", "label": "Angebotenes Fabrikat:", "value": ""}]
    assert positions[3]["countsInTotal"] is False


def test_parse_keeps_umlauts_of_latin1_files():
    latin1 = FIXTURE.read_text("utf-8").replace('encoding="utf-8"', 'encoding="ISO-8859-1"').encode("latin-1")

    markdown = client.post("/v1/parse", files=upload(data=latin1)).json()["markdown"]

    assert "räumen" in markdown


def test_parse_rejects_files_that_are_not_gaeb():
    response = client.post("/v1/parse", files=upload("notes.x83", b"just some text"))

    assert response.status_code == 422


def test_parse_rejects_empty_files():
    response = client.post("/v1/parse", files=upload(data=b""))

    assert response.status_code == 422


def test_bid_writes_priced_x84():
    prices = {"positions": [
        {"oz": "01.0010", "unitPrice": "1500"},
        {"oz": "01.0020", "unitPrice": 12.5},
        {"oz": "02.0010", "unitPrice": "48.90", "bidderFields": {"1": "Muster Sportboden S1"}},
        {"oz": "02.0020", "unitPrice": "31"},
    ], "bidder": {"name": "Example Bau GmbH", "street": "Hauptstraße 1", "postalCode": "12345", "city": "Musterstadt"}}

    response = client.post("/v1/bid", files=upload(), data={"prices": json.dumps(prices)})

    assert response.status_code == 200
    body = response.json()
    assert body["fileName"] == "Ausschreibung.X84"
    assert body["unpriced"] == []
    assert body["unknownPositions"] == []
    assert body["unfilledFields"] == []
    assert body["warnings"] == []
    # 1500 + 420.5 * 12.5 + 420.5 * 48.90; the Bedarfsposition does not count
    assert body["total"] == "27318.70"
    x84 = base64.b64decode(body["contentBase64"]).decode("utf-8")
    assert "DA84" in x84
    assert "48.9" in x84
    assert "Muster Sportboden S1" in x84
    assert "<Name1>Example Bau GmbH</Name1>" in x84


def test_bid_reports_missing_prices_unknown_positions_and_open_fields():
    prices = {"positions": [{"oz": "01.0010", "unitPrice": "1500"}, {"oz": "09.9999", "unitPrice": "1"}]}

    body = client.post("/v1/bid", files=upload(), data={"prices": json.dumps(prices)}).json()

    assert body["unpriced"] == ["01.0020", "02.0010", "02.0020"]
    assert body["unknownPositions"] == ["09.9999"]
    assert body["unfilledFields"] == ["02.0010 #1"]


def test_bid_rejects_invalid_prices():
    response = client.post("/v1/bid", files=upload(), data={"prices": '{"positions": [{"oz": ""}]}'})

    assert response.status_code == 422


def test_bid_needs_a_tender():
    bid_file = base64.b64decode(
        client.post("/v1/bid", files=upload(), data={"prices": '{"positions": []}'}).json()["contentBase64"]
    )

    response = client.post("/v1/bid", files=upload("Angebot.x84", bid_file), data={"prices": '{"positions": []}'})

    assert response.status_code == 422
    assert "X84" in response.json()["detail"]
