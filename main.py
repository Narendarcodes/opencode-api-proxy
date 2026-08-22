import os
import httpx

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse


ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_API_KEY = os.getenv("ZEN_API_KEY")

if not ZEN_API_KEY:
    raise RuntimeError("ZEN_API_KEY environment variable is not configured.")

OX_ALPHA_MODEL = "x-preview-f-free"  # ponytail: documented model ID, not enforced

app = FastAPI(title="OpenCode Zen Proxy", version="1.0.0")


@app.get("/")
async def root():
    return {"status": "online", "service": "OpenCode Zen Proxy"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


# ponytail: controlled header set — never forward client headers to Zen
def build_upstream_headers(accept: str = "application/json"):
    return {
        "Authorization": f"Bearer {ZEN_API_KEY}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": "Render-OpenCode-Zen-Proxy/1.0",
    }


def upstream_error(status_code: int, detail: str):
    # ponytail: truncate upstream bodies so keys/secrets can't leak through
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "message": detail[:500],
                "type": "upstream_error",
                "status": status_code,
            }
        },
    )


@app.get("/models")
@app.get("/v1/models")
async def get_models():
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(f"{ZEN_BASE_URL}/models", headers=build_upstream_headers())
    print("Zen /models status:", response.status_code, flush=True)
    if response.status_code != 200:
        return upstream_error(response.status_code, response.text)
    return JSONResponse(status_code=200, content=response.json())


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    print("========== INCOMING REQUEST ==========", flush=True)
    print("Method:", request.method, flush=True)
    print("Path:", request.url.path, flush=True)
    print("User-Agent:", request.headers.get("user-agent"), flush=True)
    print("Authorization:", "PRESENT" if request.headers.get("authorization") else "MISSING", flush=True)
    print("Content-Type:", request.headers.get("content-type"), flush=True)
    print("Accept:", request.headers.get("accept"), flush=True)
    print("Origin:", request.headers.get("origin"), flush=True)
    print("Referer:", request.headers.get("referer"), flush=True)
    print("======================================", flush=True)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON request"}})

    requested_model = body.get("model")
    is_stream = body.get("stream", False)
    print("Model:", requested_model, flush=True)
    print("Stream:", is_stream, flush=True)

    headers = build_upstream_headers(accept="text/event-stream" if is_stream else "application/json")

    if is_stream:
        client = httpx.AsyncClient(timeout=600.0, follow_redirects=True)
        try:
            upstream_request = client.build_request(
                "POST", f"{ZEN_BASE_URL}/chat/completions", headers=headers, json=body
            )
            response = await client.send(upstream_request, stream=True)
        except Exception as exc:
            await client.aclose()
            return JSONResponse(status_code=502, content={"error": {"type": "proxy_error", "message": str(exc)}})

        if response.status_code >= 400:
            error_body = await response.aread()
            await response.aclose()
            await client.aclose()
            return upstream_error(response.status_code, error_body.decode("utf-8", errors="replace"))

        async def stream_generator():
            try:
                async for chunk in response.aiter_bytes():
                    yield chunk
            finally:
                await response.aclose()
                await client.aclose()

        return StreamingResponse(stream_generator(), status_code=response.status_code, media_type="text/event-stream")

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        try:
            response = await client.post(f"{ZEN_BASE_URL}/chat/completions", headers=headers, json=body)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"error": {"type": "proxy_error", "message": str(exc)}})

    print("Zen response status:", response.status_code, flush=True)

    if response.status_code >= 400:
        return upstream_error(response.status_code, response.text)

    try:
        return JSONResponse(status_code=response.status_code, content=response.json())
    except Exception:
        return upstream_error(response.status_code, "Upstream returned non-JSON response")


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def universal_proxy(request: Request, path: str):
    print("CATCH-ALL:", request.method, request.url.path, "UA:", request.headers.get("user-agent"), flush=True)
    clean_path = path.lstrip("/")
    if clean_path.startswith("v1/"):
        clean_path = clean_path[3:]
    target_url = f"{ZEN_BASE_URL}/{clean_path}"
    headers = build_upstream_headers()

    body = None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            body = await request.json()
        except Exception:
            body = await request.body()

    async with httpx.AsyncClient(timeout=600.0, follow_redirects=True) as client:
        try:
            if isinstance(body, dict):
                response = await client.request(request.method, target_url, headers=headers, json=body)
            elif body:
                response = await client.request(request.method, target_url, headers=headers, content=body)
            else:
                response = await client.request(request.method, target_url, headers=headers)
        except Exception as exc:
            return JSONResponse(status_code=502, content={"error": {"type": "proxy_error", "message": str(exc)}})

    print("Zen response status:", response.status_code, flush=True)
    if response.status_code >= 400:
        return upstream_error(response.status_code, response.text)

    try:
        return JSONResponse(status_code=response.status_code, content=response.json())
    except Exception:
        return upstream_error(response.status_code, "Upstream returned non-JSON response")

