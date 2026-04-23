import logging
from typing import Optional, Dict
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Response
from curl_cffi import requests as curl_requests

# === Configuration & Constants ===

# Hardcoded Proxy Map
PROXIES = {
    "default": {"https": "http://localhost:40000", "http": "http://localhost:40000"},
    "test": {"http": "http://172.66.45.9:80"},
    "none": None,
}

# Browser Impersonation Allowlist
ALLOWED_IMPERSONATIONS = {"chrome131_android", "safari260_ios", "firefox147", "chrome145", "safari260"}
DEFAULT_IMPERSONATION = "chrome131_android"

# Max Response Size (10 MB)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024

# Headers that are safe to forward even when impersonating.
# These carry session/auth/body data and don't affect the browser fingerprint.
PASSTHROUGH_HEADERS = {
    "cookie",
    "authorization",
    "x-requested-with",
    "origin",
    "referer",
    "content-type",      # Required so the server knows the body format (e.g. application/json)
    "content-length",    # Required for POST/PUT body framing
}

# Headers to strip from upstream responses before returning to client.
HOP_BY_HOP_RESPONSE_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length",    # Let FastAPI/Starlette recalculate
    "content-encoding",  # curl_cffi already decoded the body
}

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI(title="Curl_CFFI Secure Proxy")


# === Helper Functions ===

def get_request_headers(client_headers: dict, impersonate: str) -> dict:
    """
    When impersonation is active, curl_cffi sets its own browser-realistic
    headers (User-Agent, Accept, Accept-Encoding, sec-ch-ua, sec-fetch-*, etc.).
    Forwarding the raw client headers on top of those overrides the fingerprint
    and breaks impersonation.

    Strategy:
    - Impersonation active  → only pass through safe, session-related headers
                              (cookie, authorization, etc.) so the fingerprint stays intact.
    - No impersonation      → forward all client headers minus hop-by-hop ones.
    """
    if impersonate:
        # Only keep headers that carry session/auth data
        return {
            k: v for k, v in client_headers.items()
            if k.lower() in PASSTHROUGH_HEADERS
        }
    else:
        # No impersonation: strip hop-by-hop but forward everything else
        hop_by_hop = {
            "host", "connection", "keep-alive", "proxy-authenticate",
            "proxy-authorization", "te", "trailers", "transfer-encoding",
            "upgrade", "content-length", "content-encoding", "accept-encoding",
        }
        return {k: v for k, v in client_headers.items() if k.lower() not in hop_by_hop}


def normalize_response_headers(headers: dict) -> dict:
    """Strip hop-by-hop headers from the upstream response."""
    return {k: v for k, v in headers.items() if k.lower() not in HOP_BY_HOP_RESPONSE_HEADERS}


def validate_request(url: str, proxy_key: str, impersonate: str):
    """Checks input validity, raises HTTPException on failure."""
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid or missing 'url'. Must start with http/https.")

    if proxy_key not in PROXIES:
        raise HTTPException(status_code=400, detail=f"Invalid 'proxy'. Allowed: {list(PROXIES.keys())}")

    if impersonate not in ALLOWED_IMPERSONATIONS:
        raise HTTPException(status_code=400, detail=f"Invalid 'impersonate'. Allowed: {list(ALLOWED_IMPERSONATIONS)}")


# === Main Endpoint ===

@app.api_route("/api/fetch", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def fetch_endpoint(request: Request):
    # --- 1. Extract & Validate Parameters ---
    target_url = request.query_params.get("url")
    proxy_key = request.query_params.get("proxy", "default")
    impersonate = request.query_params.get("impersonate", DEFAULT_IMPERSONATION)

    validate_request(target_url, proxy_key, impersonate)

    # --- 2. Prepare Request Data ---
    # Only forward headers that won't interfere with the impersonation fingerprint
    outgoing_headers = get_request_headers(dict(request.headers), impersonate)

    # Get body (if any)
    client_body = await request.body()

    # Select Proxy
    target_proxy = PROXIES[proxy_key]

    logger.info(f"Fetching {request.method} {target_url} | impersonate={impersonate} | proxy={proxy_key}")

    # --- 3. Execute Request (Streaming Mode) ---
    try:
        with curl_requests.Session() as s:
            response = s.request(
                method=request.method,
                url=target_url,
                headers=outgoing_headers,  # fingerprint-safe headers only
                data=client_body,
                proxies=target_proxy,
                impersonate=impersonate,
                timeout=30,
                stream=True,
                allow_redirects=True,
            )

            # --- 4. Size Limit Check (Early Abort) ---
            cl_header = response.headers.get("content-length")
            if cl_header:
                try:
                    if int(cl_header) > MAX_RESPONSE_BYTES:
                        raise HTTPException(status_code=413, detail="Response too large (Header)")
                except ValueError:
                    pass  # Bad header value — fall through to stream check

            content_accumulator = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                content_accumulator.extend(chunk)
                if len(content_accumulator) > MAX_RESPONSE_BYTES:
                    raise HTTPException(status_code=413, detail="Response too large (Stream limit exceeded)")

            final_content = bytes(content_accumulator)

            # --- 5. Prepare Response ---
            upstream_headers = normalize_response_headers(dict(response.headers))

            return Response(
                content=final_content,
                status_code=response.status_code,
                headers=upstream_headers,
                media_type=response.headers.get("content-type"),
            )

    except HTTPException:
        raise

    except Exception as e:
        logger.error(f"Upstream error for {target_url}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {str(e)}")

#== New Buzzheavier Endpoint ===

@app.get("/api/buzz")
async def buzzheavier_endpoint(request: Request):
    """
    Specific handler for Buzzheavier to bypass HTMX-based redirects.
    Usage: /api/buzz?url=https://buzzheavier.com/5bxiamjqv78x
    """
    target_url = request.query_params.get("url")
    proxy_key = request.query_params.get("proxy", "default")
    impersonate = request.query_params.get("impersonate", DEFAULT_IMPERSONATION)

    # Basic validation
    if not target_url or "buzzheavier.com" not in target_url:
        raise HTTPException(status_code=400, detail="Invalid Buzzheavier URL")

    # Clean URL and construct the download path
    base_url = target_url.strip().rstrip("/")
    download_endpoint = f"{base_url}/download?alt=true"
    
    # Select Proxy
    target_proxy = PROXIES.get(proxy_key)

    try:
        # We use a Session to maintain the browser fingerprint via curl_cffi
        with curl_requests.Session() as s:
            # Custom headers specifically for Buzzheavier's HTMX setup
            custom_headers = {
                "HX-Request": "true",
                "HX-Current-URL": base_url,
                "Referer": base_url,
                "Accept": "*/*",
            }
            
            # Merge with any incoming auth/session headers if present
            incoming_safe = get_request_headers(dict(request.headers), impersonate)
            custom_headers.update(incoming_safe)

            response = s.get(
                url=download_endpoint,
                headers=custom_headers,
                proxies=target_proxy,
                impersonate=impersonate,
                timeout=15,
                allow_redirects=False  # We want to catch the hx-redirect header
            )

            # Look for the redirect in headers
            # Note: curl_cffi headers are case-insensitive dicts
            hx_redirect = response.headers.get("hx-redirect")

            if hx_redirect:
                logger.info(f"Buzz Redirect Found: {hx_redirect}")
                return Response(
                    status_code=302,
                    headers={"Location": hx_redirect}
                )
            
            # If it's a 204 but no header, or a 404, return the debug info
            return Response(
                content=f"Redirect header not found. Status: {response.status_code}",
                status_code=502,
                media_type="text/plain"
            )

    except Exception as e:
        logger.error(f"Buzzheavier error: {e}")
        raise HTTPException(status_code=502, detail=str(e))

# === Runner (for debugging) ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)
