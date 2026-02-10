# Py Fast Fetch API (FastAPI + curl_cffi)

A simple **reverse-proxy / fetch API** built with **FastAPI** and **curl_cffi**.

This service forwards HTTP requests to a target URL while supporting:
- Browser TLS impersonation
- Optional hardcoded proxy usage
- Multiple HTTP methods
- Response size limiting

Designed for **private / internal use** and deployed on your own VPS.

---

## Features

- `/api/fetch` endpoint
- Supports HTTP methods:
  - GET
  - POST
  - PUT
  - PATCH
  - DELETE
- Uses `curl_cffi` for real browser-like TLS fingerprints
- Hardcoded proxy options:
  - `proxy=default`
  - `proxy=none`
- Browser impersonation (allowlist)
- Maximum response size limit (default: 10 MB)
- Simple, minimal, production-friendly

---

## Requirements

- Python **3.9+**

---

## Installation

Clone or copy the project files, then install dependencies:

```bash
pip install -r requirements.txt
````

---

## Project Structure

```text
.
├── main.py           # FastAPI application
├── requirements.txt  # Python dependencies
└── README.md
```

---

## Running the API (Development)

Run with Uvicorn:

```bash
uvicorn main:app --reload
```

The API will be available at:

```
http://127.0.0.1:8000
```

---

## Running on VPS (Production)

Recommended command:

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --workers 2
```

For production usage, it is recommended to run the app using **systemd** (see systemd service configuration).

---

## API Usage

### Endpoint

```
/api/fetch
```

### Query Parameters

| Parameter     | Required | Description                                  |
| ------------- | -------- | -------------------------------------------- |
| `url`         | ✅ Yes    | Target URL (`http` or `https`)               |
| `proxy`       | ❌ No     | `default` or `none` (default: `default`)     |
| `impersonate` | ❌ No     | Browser impersonation (default: `chrome142`) |

---

### Example: GET request (no proxy)

```bash
curl "http://127.0.0.1:8000/api/fetch?url=https://example.com&proxy=none"
```

---

### Example: POST request with JSON

```bash
curl -X POST \
  "http://127.0.0.1:8000/api/fetch?url=https://httpbin.org/post&proxy=default" \
  -H "Content-Type: application/json" \
  -d '{"hello":"world"}'
```

---

### Example: Using browser impersonation

```bash
curl "http://127.0.0.1:8000/api/fetch?url=https://tls.browserleaks.com/json&impersonate=chrome142"
```

---

## Proxy Configuration

Proxies are **hardcoded in the server** for safety.

Example in `main.py`:

```python
PROXIES = {
    "default": {"https": "http://localhost:3128"},
    "none": None
}
```

Only these values are accepted:

* `proxy=default`
* `proxy=none`

---

## Response Size Limit

* Default maximum response size: **10 MB**
* If exceeded, the API returns:

  ```
  HTTP 413 Payload Too Large
  ```

You can override the limit using an environment variable:

```bash
export MAX_RESPONSE_BYTES=20971520  # 20 MB
```

---

## Error Handling

| Status Code | Meaning                                   |
| ----------- | ----------------------------------------- |
| 400         | Invalid input (URL, proxy, impersonation) |
| 502         | Upstream request failed                   |
| 413         | Response too large                        |

---

## Notes

* This API is intended for **private/internal use**
* No SSRF protection is enforced by default
* Do not expose publicly without additional security

---