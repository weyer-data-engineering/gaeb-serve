# gaeb-serve

A small HTTP service for [GAEB](https://www.gaeb.de/) files, the exchange format of German construction tenders.

- **Read** a GAEB DA XML file (X81–X89, DA XML 2.0–3.3) and get its bill of quantities as Markdown, plus every position as structured data.
- **Bid**: turn a tender (X83) into a bid (X84) with your unit prices and bidder fields, written the way the X84 phase requires.

It is a thin, stateless wrapper around [pyGAEB](https://github.com/frameiq/pygaeb) and ships as a Docker image. The API does not depend on the engine, so it could be replaced later.

gaeb-serve was built for [AITEZA](https://aiteza.ai), where it reads tenders for the assistant and creates bids in the chat. It has no dependency on AITEZA and works with any client.

GAEB 90 (`.d83`) and GAEB 2000 (`.p83`) are not supported.

## Run

```bash
docker run --rm -p 5002:5002 -e GAEB_API_KEY="$(openssl rand -hex 32)" ghcr.io/weyer-data-engineering/gaeb-serve:latest
```

Clients send the key in the `X-API-Key` header. The service does not start without `GAEB_API_KEY`. `/health` needs no key, so probes keep working.

The key protects against accidental access, not against an attacker on the network: it travels in plain text unless you put TLS in front. Run the service in an internal network next to the application that calls it, and do not expose it publicly.

| Variable | Default | Meaning |
|---|---|---|
| `GAEB_API_KEY` | (required) | Key clients send in `X-API-Key`. Several keys, comma-separated, allow rotating without downtime. |
| `GAEB_MAX_FILE_SIZE_MB` | `100` | Larger uploads are rejected with 413. |
| `GAEB_WORKERS` | `2` | Worker processes. Parsing is CPU-bound, so one per core is a good start. |
| `GAEB_PROG_SYSTEM` | `gaeb-serve <version>` | `ProgSystem` written into bids. |
| `GAEB_PROG_NAME` | `gaeb-serve` | `ProgName` written into bids. |
| `GAEB_LOG_LEVEL` | `INFO` | Log level. File contents are never logged. |

## Kubernetes

A Helm chart is published with every release:

```bash
kubectl create secret generic gaeb-serve-api-key --from-literal=apiKey="$(openssl rand -hex 32)"
helm install gaeb-serve oci://ghcr.io/weyer-data-engineering/charts/gaeb-serve \
  --version 0.1.0 --set apiKey.existingSecret=gaeb-serve-api-key
```

Clients in the cluster reach the service at `http://gaeb-serve.<namespace>.svc.cluster.local:5002`. The chart runs two replicas with a pod disruption budget, a read-only root filesystem and no service account token. There is no ingress on purpose: keep the service internal.

| Value | Default | Meaning |
|---|---|---|
| `apiKey.existingSecret` | | Secret that holds the API key(s). |
| `apiKey.secretKey` | `apiKey` | Key inside that secret. |
| `apiKey.value` | | Without `existingSecret`, the chart creates a secret with this value. |
| `image.tag`, `image.digest` | chart version | Image to run; set `digest` to pin it. |
| `workers` | `1` | `GAEB_WORKERS` per pod. Scale with `replicaCount` instead. |
| `maxFileSizeMb`, `logLevel`, `progSystem`, `progName` | | The variables above. |
| `replicaCount` | `2` | Pods. |
| `resources` | 100m / 256Mi, limit 1Gi | A 5 MB tender with 5,000 positions peaks at about 190 MB. |

All values are listed in [`charts/gaeb-serve/values.yaml`](charts/gaeb-serve/values.yaml).

## API

OpenAPI documentation is served at `/docs`.

### `GET /health`

```json
{"status": "ok", "version": "0.1.0", "engine": "pyGAEB 1.21.0"}
```

### `POST /v1/parse`

Multipart upload with the field `file`. The file name matters: its extension tells the exchange phase.

```bash
curl -H "X-API-Key: $GAEB_API_KEY" -F file=@tender.x83 http://localhost:5002/v1/parse
```

```json
{
  "version": "3.2",
  "phase": "X83",
  "kind": "procurement",
  "project": "Sanierung Turnhalle",
  "client": "Muster GmbH",
  "currency": "EUR",
  "markdown": "# Sanierung Turnhalle\n\n## 01 Abbrucharbeiten\n\n**01.0010 Baustelleneinrichtung** ...",
  "positions": [
    {
      "oz": "02.0010",
      "shortText": "Sportboden flächenelastisch",
      "qty": "420.5",
      "qtyToBeDetermined": false,
      "unit": "m2",
      "itemType": "Normal",
      "countsInTotal": true,
      "unitPrice": null,
      "totalPrice": null,
      "bidderFields": [{"mark": "1", "label": "Angebotenes Fabrikat:", "value": ""}]
    }
  ],
  "findings": []
}
```

Amounts are decimal strings. `positions` is filled for tenders, bids and awards (X81–X89); `findings` lists problems the reader tolerated.

`itemType` is `Normal`, `LumpSum`, `Eventual` (Bedarfsposition) or `Alternative` (Wahlposition). `countsInTotal` says whether the position enters the sum: not for alternatives and not for Bedarfspositionen without total. The Markdown names the base position of each alternative and includes the tender's remarks (Hinweistexte) as `*Hinweis:*` paragraphs.

### `POST /v1/bid`

Multipart upload with the tender as `file` and a JSON field `prices`:

```bash
curl -H "X-API-Key: $GAEB_API_KEY" -F file=@tender.x83 -F 'prices={
  "positions": [
    {"oz": "01.0010", "unitPrice": "1500"},
    {"oz": "02.0010", "unitPrice": "48.90", "bidderFields": {"1": "Example Sportboden S1"}}
  ],
  "bidder": {"name": "Example Bau GmbH", "street": "Hauptstraße 1", "postalCode": "12345", "city": "Musterstadt"}
}' http://localhost:5002/v1/bid
```

```json
{
  "fileName": "tender.X84",
  "contentBase64": "PD94bWwg...",
  "total": "21062.45",
  "unpriced": ["01.0020", "02.0020"],
  "unknownPositions": [],
  "unfilledFields": [],
  "warnings": []
}
```

Positions are addressed by their full OZ as `/v1/parse` returns it. Totals are computed from quantity and unit price. Positions without a price stay unpriced in the bid and are listed in `unpriced`, so the caller can decide whether that is acceptable. The bid is written in the tender's DA XML version.

### Errors

| Status | When |
|---|---|
| 401 | The `X-API-Key` header is missing or wrong. |
| 413 | The file is larger than `GAEB_MAX_FILE_SIZE_MB`. |
| 422 | The file is empty or not readable as GAEB, the prices are invalid, or a bid was requested for something other than a tender. `detail` says why. |

## Develop

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/pytest
helm unittest charts/gaeb-serve   # needs the helm-unittest plugin
GAEB_API_KEY=dev .venv/bin/uvicorn gaeb_serve.app:app --reload --port 5002
```

Test files are synthetic. Please never commit real tenders.

## License

MIT, see [LICENSE](LICENSE). pyGAEB is MIT licensed as well. GAEB is a standard of the Gemeinsamer Ausschuss Elektronik im Bauwesen. This project is not affiliated with it.
