# main.py
"""
Simple private FastAPI reverse-proxy / fetch endpoint using curl_cffi.

Features:
- Endpoint: /api/fetch
- Query params:
    - url (required)         -> target URL (http/https only)
    - proxy (optional)       -> 'default' or 'none' (hardcoded only)
    - impersonate (optional) -> browser impersonation, allowlist
- Forwards client HTTP method: GET, POST, PUT, PATCH, DELETE
- Forwards request headers (with hop-by-hop removal) and raw body
- Uses curl_cffi.requests for outgoing requests
- Enforces a maximum response size (default: 10 MB). If exceeded -> 413
- Simple error handling suitable for a private service
"""

from typing import Optional, Dict, Iterable
from urllib.parse import urlparse
import os

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import Response, StreamingResponse
from starlette.status import HTTP_400_BAD_REQUEST, HTTP_502_BAD_GATEWAY, HTTP_413_REQUEST_ENTITY_TOO_LARGE
import curl_cffi.requests as curl_requests

app = FastAPI(title="Py Fast Fetch Proxy")

# === Config / constants ===
# change values to your real proxy addresses if needed.
PROXIES: Dict[str, Optional[Dict[str, str]]] = {
    "default": {"https": "http://localhost:3128"},
    "none": None,
}

# Allowlist of impersonations (curl_cffi supports several Chrome/Firefox 'profiles')
ALLOWED_IMPERSONATIONS = {"chrome120", "chrome142", "realworld"}  # adjust as you like
DEFAULT_IMPERSONATION = "chrome142"

# Maximum allowed response size (bytes). Default: 10 MB
MAX_RESPONSE_BYTES = int(os.getenv("MAX_RESPONSE_BYTES", 10 * 1024 * 1024))

# Hop-by-hop headers to remove when forwarding and when returning response
HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",  # we'll let Response set correct content-length if needed
}


# === Helpers ===
def validate_url(target_url: str) -> None:
    """Basic URL validation: must be http or https and non-empty."""
    if not target_url:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Missing 'url' query parameter.")
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="URL must use http or https scheme.")


def filter_headers_for_forwarding(in_headers: Dict[str, str]) -> Dict[str, str]:
    """Return headers to forward to the target, removing hop-by-hop and certain unsafe headers."""
    out = {}
    for k, v in in_headers.items():
        k_l = k.lower()
        if k_l in HOP_BY_HOP_HEADERS:
            continue
        # Optionally disallow X-Forwarded-* or other headers if you'd like (not done here)
        out[k] = v
    return out


def filter_response_headers(in_headers: Dict[str, str]) -> Dict[str, str]:
    """Filter headers coming from target before returning to client."""
    out = {}
    for k, v in in_headers.items():
        if k.lower() in HOP_BY_HOP_HEADERS:
            continue
        out[k] = v
    return out


def build_proxies(proxy_key: Optional[str]) -> Optional[Dict[str, str]]:
    """Return proxies dict for curl_cffi based on chosen proxy key ('default'/'none')."""
    if not proxy_key:
        proxy_key = "default"
    if proxy_key not in PROXIES:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail=f"Invalid proxy value: {proxy_key}")
    return PROXIES[proxy_key]


# === Core endpoint (supports multiple HTTP verbs) ===
@app.api_route("/api/fetch", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def fetch_endpoint(request: Request) -> Response:
    """
    Reverse-proxy endpoint. Query params:
      - url (required)
      - proxy (optional) -> 'default' or 'none'
      - impersonate (optional) -> e.g. 'chrome142'
    """
    # Extract query params
    target_url = request.query_params.get("url")
    proxy_param = request.query_params.get("proxy", "default")
    impersonate_param = request.query_params.get("impersonate", DEFAULT_IMPERSONATION)

    # Validate URL and proxy name
    validate_url(target_url)
    proxies_for_curl = build_proxies(proxy_param)

    # Validate impersonation
    if impersonate_param not in ALLOWED_IMPERSONATIONS:
        raise HTTPException(
            status_code=HTTP_400_BAD_REQUEST,
            detail=f"Invalid impersonate value. Allowed: {sorted(ALLOWED_IMPERSONATIONS)}"
        )

    # Prepare headers (forward most client headers, but strip hop-by-hop)
    incoming_headers = {k: v for k, v in request.headers.items()}
    outgoing_headers = filter_headers_for_forwarding(incoming_headers)

    # Read body (may be empty)
    body = await request.body()
    data = body if body else None

    # Determine method and prepare call
    method = request.method.upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise HTTPException(status_code=HTTP_400_BAD_REQUEST, detail="Unsupported HTTP method.")

    # Use curl_cffi to perform the request
    try:
        # If there's a content-length on the response we can pre-check it later.
        # We do a direct call and then inspect headers + content.
        # Using .request allows dynamic method selection.
        curl_func = getattr(curl_requests, method.lower())

        # Perform request. We use a modest timeout; tune if needed.
        # Note: curl_cffi's API is similar to requests; adapt if your version differs.
        resp = curl_func(
            target_url,
            headers=outgoing_headers,
            data=data,
            impersonate=impersonate_param,
            proxies=proxies_for_curl,
            timeout=30.0,
            allow_redirects=False,  # keep behavior simple; change if you want redirects followed
        )

    except Exception as e:
        # Network/transport error
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=f"Upstream request failed: {e}")

    # At this point, resp is expected to have .status_code, .headers, .content
    status = getattr(resp, "status_code", None)
    resp_headers = dict(getattr(resp, "headers", {}) or {})

    # If target provided Content-Length and it exceeds our limit, abort early
    content_length_header = resp_headers.get("Content-Length") or resp_headers.get("content-length")
    if content_length_header:
        try:
            content_length_val = int(content_length_header)
            if content_length_val > MAX_RESPONSE_BYTES:
                raise HTTPException(
                    status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail=f"Upstream response too large ({content_length_val} bytes) - limit is {MAX_RESPONSE_BYTES} bytes."
                )
        except ValueError:
            # ignore malformed header and proceed to read content (we will check actual length)
            pass

    # Read full content (simple approach — acceptable for private service with size cap)
    try:
        content_bytes = getattr(resp, "content", None)
        if content_bytes is None:
            # Fallback: try .raw.read() if content is not present (library differences)
            raw = getattr(resp, "raw", None)
            if raw is not None and hasattr(raw, "read"):
                content_bytes = raw.read()
            else:
                # As last resort, coerce to bytes(str(...))
                content_bytes = bytes(str(resp), "utf-8")
    except Exception as e:
        raise HTTPException(status_code=HTTP_502_BAD_GATEWAY, detail=f"Failed reading upstream response body: {e}")

    # Enforce size limit based on actual received bytes
    if content_bytes is not None and len(content_bytes) > MAX_RESPONSE_BYTES:
        raise HTTPException(
            status_code=HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"Upstream response too large ({len(content_bytes)} bytes) - limit is {MAX_RESPONSE_BYTES} bytes."
        )

    # Filter response headers before returning
    filtered_resp_headers = filter_response_headers(resp_headers)

    # Prepare and return response (preserve upstream status code)
    return Response(
        content=content_bytes,
        status_code=(status or 200),
        headers=filtered_resp_headers,
        media_type=filtered_resp_headers.get("content-type")  # let FastAPI set Content-Type if present
    )


# === Optional: simple root to check service ===
@app.get("/")
async def root():
    return {"message": "Hello World!"}


# === Run with: uvicorn main:app --reload ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=8000, reload=True)
