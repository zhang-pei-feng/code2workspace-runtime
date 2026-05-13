# EpiETL API Reference

Base URL: `https://epietl.com`

## Endpoints

| Method | Path | Auth | Purpose |
|---|---|---:|---|
| `GET` | `/api/reports` | Yes | Search global surveillance reports. |
| `GET` | `/api/risk/events` | No | Query AI-extracted pathogen risk events. |
| `GET` | `/api/channels` | No | List data source channels. |
| `GET` | `/api/health` | No | Health check. |

## Authentication

For authenticated endpoints:

```http
Authorization: Bearer <YOUR_API_KEY>
```

Use the `EPIETL_API_KEY` environment variable with the bundled script. Do not store the key in this reference file.

## Limits and Pagination

- Rate limit: 200 requests per minute per key.
- Each request returns up to 200 items.
- Use `limit` and `offset` for pagination.

## Examples

Search reports by country:

```bash
curl -H "Authorization: Bearer $EPIETL_API_KEY" \
  "https://epietl.com/api/reports?country=China&limit=5"
```

Search critical risk events for cholera:

```bash
curl "https://epietl.com/api/risk/events?severity=critical&pathogen=cholera&limit=10"
```

List public channels:

```bash
curl "https://epietl.com/api/channels?limit=20"
```

## Suggested Citation

Data sourced from EpiETL (`https://epietl.com`), developed and maintained by Greater Bay Area Center for Bioinformation (GBACB). Original surveillance data published by respective national and international public health agencies; see individual report source URLs for details.
