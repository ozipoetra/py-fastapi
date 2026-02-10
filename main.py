import logging
from typing import Optional, Dict
from urllib.parse import urlparse

from fastapi import FastAPI, Request, HTTPException, Response
from curl_cffi import requests as curl_requests

# === Configuration & Constants ===

# Hardcoded Proxy Map
# Ensure the proxy URL format is correct for curl_cffi (usually http://user:pass@host:port)
PROXIES = {
    "default": {"https": "http://localhost:3128", "http": "http://localhost:3128"},
    "none": None,
}

# Browser Impersonation Allowlist
ALLOWED_IMPERSONATIONS = {"chrome131_android", "safari260_ios", "firefox144", "chrome142", "safari260", "realworld"}
DEFAULT_IMPERSONATION = "chrome142"

# Max Response Size (10 MB)
MAX_RESPONSE_BYTES = 10 * 1024 * 1024 

# Headers to strictly remove to prevent conflicts or protocol errors
HOP_BY_HOP_HEADERS = {
    "host",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "content-length", # We let the systems recalculate this
    "content-encoding", # We let curl decode, so we shouldn't pass this back unless we re-encode
    "accept-encoding",  # We let curl handle compression negotiation
}

# Setup basic logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("proxy")

app = FastAPI(title="Curl_CFFI Secure Proxy")


# === Helper Functions ===

def normalize_headers(headers: dict) -> dict:
    """
    Strips hop-by-hop headers and low-level transport headers
    that usually cause 400 Bad Request or 502 errors if forwarded blindly.
    """
    clean = {}
    for k, v in headers.items():
        if k.lower() not in HOP_BY_HOP_HEADERS:
            clean[k] = v
    return clean

def validate_request(url: str, proxy_key: str, impersonate: str):
    """Checks input validity, raises HTTPException on failure."""
    # 1. Validate URL
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid or missing 'url'. Must start with http/https.")
    
    # 2. Validate Proxy
    if proxy_key not in PROXIES:
        raise HTTPException(status_code=400, detail=f"Invalid 'proxy'. Allowed: {list(PROXIES.keys())}")
    
    # 3. Validate Impersonate
    if impersonate not in ALLOWED_IMPERSONATIONS:
        raise HTTPException(status_code=400, detail=f"Invalid 'impersonate'. Allowed: {list(ALLOWED_IMPERSONATIONS)}")


# === Main Endpoint ===

@app.api_route("/api/fetch", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def fetch_endpoint(request: Request):
    # --- 1. Extract & Validate Parameters ---
    target_url = request.query_params.get("url")
    proxy_key = request.query_params.get("proxy", "none")
    impersonate = request.query_params.get("impersonate", DEFAULT_IMPERSONATION)

    validate_request(target_url, proxy_key, impersonate)
    
    # --- 2. Prepare Request Data ---
    # Get client headers and strip dangerous ones
    client_headers = normalize_headers(dict(request.headers))
    
    # Get body (if any)
    client_body = await request.body()
    
    # Select Proxy
    target_proxy = PROXIES[proxy_key]

    # --- 3. Execute Request (Streaming Mode) ---
    try:
        # We use a Session context for safety, though a direct call works too.
        # stream=True is ESSENTIAL for the size limit check.
        with curl_requests.Session() as s:
            response = s.request(
                method=request.method,
                url=target_url,
                headers=client_headers,
                data=client_body,
                proxies=target_proxy,
                impersonate=impersonate,
                timeout=30,  # 30s timeout
                stream=True, # Start reading headers immediately, don't wait for body
                allow_redirects=True
            )

            # --- 4. Size Limit Check (Early Abort) ---
            # Check Content-Length header first if available
            cl_header = response.headers.get("content-length")
            if cl_header:
                try:
                    if int(cl_header) > MAX_RESPONSE_BYTES:
                        raise HTTPException(status_code=413, detail="Response too large (Header)")
                except ValueError:
                    pass # Ignore bad header, check actual stream

            # Read content in chunks to enforce limit on streams/unknown lengths
            content_accumulator = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                content_accumulator.extend(chunk)
                if len(content_accumulator) > MAX_RESPONSE_BYTES:
                    # Abort connection immediately
                    raise HTTPException(status_code=413, detail="Response too large (Stream limit exceeded)")

            # Final body content
            final_content = bytes(content_accumulator)
            
            # --- 5. Prepare Response ---
            # Clean up upstream headers (remove Transfer-Encoding, etc.)
            upstream_headers = normalize_headers(dict(response.headers))

            return Response(
                content=final_content,
                status_code=response.status_code,
                headers=upstream_headers,
                media_type=response.headers.get("content-type")
            )

    except HTTPException:
        raise # Re-raise known HTTP exceptions (like 413)

    except Exception as e:
        # Catch network/curl errors
        logger.error(f"Upstream error for {target_url}: {e}")
        raise HTTPException(status_code=502, detail=f"Upstream request failed: {str(e)}")

# === Runner (for debugging) ===
if __name__ == "__main__":
    import uvicorn
    # Run: python main.py
    uvicorn.run(app, host="127.0.0.1", port=8000)