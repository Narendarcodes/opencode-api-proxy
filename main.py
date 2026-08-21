import os
import httpx

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse, JSONResponse


ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_API_KEY = os.getenv("ZEN_API_KEY")

if not ZEN_API_KEY:
    raise RuntimeError("ZEN_API_KEY environment variable is not configured.")

OX_ALPHA_MODEL = "x-preview-f-free"

app = FastAPI(title="OpenCode Zen Proxy", version="1.0.0")


@app.get("/")
async def root():
    return {"status": "online", "service": "OpenCode Zen Proxy"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


def build_headers(request: Request):
    return {
        "Authorization": f"Bearer {ZEN_API_KEY}",
        "Content-Type": request.headers.get("content-type", "application/json"),
        "Accept": request.headers.get("accept", "application/json"),
        "User-Agent": "OpenCode-Zen-Proxy/1.0",
    }


@app.get("/models")
@app.get("/v1/models")
async def get_models(request: Request):
    headers = build_headers(request)
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        response = await client.get(f"{ZEN_BASE_URL}/models", headers=headers)
    if response.status_code != 200:
        return JSONResponse(status_code=response.status_code, content={"error": response.text})
    return JSONResponse(status_code=200, content=response.json())


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    headers = build_headers(request)
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(status_code=400, content={"error": {"message": "Invalid JSON request"}})

    requested_model = body.get("model")
    print(f"Model requested: {requested_model}", flush=True)
    if requested_model == OX_ALPHA_MODEL:
        print("Ox Alpha Free request detected", flush=True)

    is_stream = body.get("stream", False)

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
            return JSONResponse(status_code=response.status_code, content={"error": error_body.decode("utf-8", errors="replace")})

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

    try:
        return JSONResponse(status_code=response.status_code, content=response.json())
    except Exception:
        return JSONResponse(status_code=response.status_code, content={"text": response.text})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def universal_proxy(request: Request, path: str):
    clean_path = path.lstrip("/")
    if clean_path.startswith("v1/"):
        clean_path = clean_path[3:]
    target_url = f"{ZEN_BASE_URL}/{clean_path}"
    headers = build_headers(request)

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

    try:
        return JSONResponse(status_code=response.status_code, content=response.json())
    except Exception:
        return JSONResponse(status_code=response.status_code, content={"text": response.text})
