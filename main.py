import asyncio
import json
import logging
import os
import uuid
from typing import Any, Dict, Optional

import httpx

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger("opencode_proxy")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

ZEN_BASE_URL = "https://opencode.ai/zen/v1"
ZEN_API_KEY = os.getenv("ZEN_API_KEY")


app = FastAPI(title="OpenCode Zen Proxy", version="1.0.0")


def request_id_header() -> str:
    return uuid.uuid4().hex[:12]


def build_upstream_headers(accept: str = "application/json") -> Dict[str, str]:
    if not ZEN_API_KEY:
        raise RuntimeError("ZEN_API_KEY environment variable is not configured.")
    return {
        "Authorization": f"Bearer {ZEN_API_KEY}",
        "Content-Type": "application/json",
        "Accept": accept,
        "User-Agent": "OpenCode-Zen-Proxy/1.1",
    }


def extract_message_from_body(raw_body: Any) -> Optional[str]:
    if raw_body is None:
        return None
    if isinstance(raw_body, str):
        return raw_body.strip() or None
    if isinstance(raw_body, dict):
        if "message" in raw_body and isinstance(raw_body["message"], str):
            return raw_body["message"]
        if "error" in raw_body and isinstance(raw_body["error"], dict):
            return extract_message_from_body(raw_body["error"])
    if isinstance(raw_body, list):
        for item in raw_body:
            message = extract_message_from_body(item)
            if message:
                return message
    return None


def classify_upstream_error(status_code: int, raw_detail: Any) -> Dict[str, str]:
    detail = raw_detail.decode("utf-8", errors="replace") if isinstance(raw_detail, bytes) else str(raw_detail)
    message = detail
    if detail.strip().startswith("{"):
        try:
            message = extract_message_from_body(json.loads(detail)) or detail
        except (TypeError, ValueError):
            message = detail
    if not message or message == "None":
        message = "Upstream request failed"

    lowered = message.lower()
    if status_code in (401, 403) or "invalid api key" in lowered or "unauthorized" in lowered or "authentication" in lowered:
        return {"message": message, "type": "authentication_error", "code": "authentication_error"}
    if "model is unavailable" in lowered or "model not available" in lowered or ("model" in lowered and "unavailable" in lowered):
        return {"message": message, "type": "upstream_error", "code": "model_unavailable"}
    if "endpoint is unavailable" in lowered or ("endpoint" in lowered and "unavailable" in lowered):
        return {"message": message, "type": "upstream_error", "code": "endpoint_unavailable"}
    if "no payment method" in lowered or "credits" in lowered or "payment" in lowered and "method" in lowered:
        return {"message": message, "type": "payment_required", "code": "payment_required"}
    if status_code == 429:
        return {"message": message, "type": "rate_limit_error", "code": "rate_limit_exceeded"}
    if status_code >= 500:
        return {"message": message, "type": "upstream_error", "code": "upstream_error"}
    return {"message": message, "type": "upstream_error", "code": "upstream_error"}


async def send_chat_completion_with_retry(client: httpx.AsyncClient, url: str, headers: Dict[str, str], body: Dict[str, Any], is_stream: bool, request_id: str):
    max_attempts = 2
    last_response = None
    for attempt in range(1, max_attempts + 1):
        try:
            if is_stream:
                upstream_request = client.build_request("POST", url, headers=headers, json=body)
                response = await client.send(upstream_request, stream=True)
            else:
                response = await client.post(url, headers=headers, json=body)
        except Exception:
            raise

        if response.status_code == 429 and attempt < max_attempts:
            retry_after = response.headers.get("retry-after")
            sleep_seconds = 1.0
            try:
                if retry_after:
                    sleep_seconds = max(1.0, float(retry_after))
            except ValueError:
                pass
            logger.warning("request_id=%s upstream_rate_limited retrying in %.1f seconds attempt=%s/%s", request_id, sleep_seconds, attempt + 1, max_attempts)
            await asyncio.sleep(sleep_seconds)
            last_response = response
            continue

        last_response = response
        return response

    return last_response


def upstream_error(status_code: int, detail: Any, request_id: str) -> JSONResponse:
    payload = classify_upstream_error(status_code, detail)
    return JSONResponse(
        status_code=status_code,
        content={"error": {"message": payload["message"][:500], "type": payload["type"], "code": payload["code"]}},
        headers={"X-Request-Id": request_id},
    )


def validation_error(message: str, request_id: str, code: str = "validation_error") -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": {"message": message, "type": "validation_error", "code": code}},
        headers={"X-Request-Id": request_id},
    )


def log_request_context(request: Request, payload: Optional[dict], request_id: str) -> None:
    logger.info(
        "request_id=%s method=%s path=%s model=%s stream=%s content_type=%s accept=%s user_agent=%s",
        request_id,
        request.method,
        request.url.path,
        payload.get("model") if isinstance(payload, dict) else None,
        bool(payload.get("stream", False)) if isinstance(payload, dict) else None,
        request.headers.get("content-type"),
        request.headers.get("accept"),
        request.headers.get("user-agent"),
    )


@app.get("/")
async def root():
    return {"status": "online", "service": "OpenCode Zen Proxy"}


@app.get("/health")
async def health():
    return {"status": "healthy"}


@app.get("/models")
@app.get("/v1/models")
async def get_models(request: Request):
    request_id = request_id_header()
    log_request_context(request, None, request_id)
    try:
        headers = build_upstream_headers()
    except RuntimeError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(exc), "type": "authentication_error", "code": "missing_zen_api_key"}},
            headers={"X-Request-Id": request_id},
        )

    url = f"{ZEN_BASE_URL}/models"
    logger.info("request_id=%s upstream_url=%s upstream_method=GET", request_id, url)
    try:
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url, headers=headers)
    except httpx.TimeoutException as exc:
        logger.exception("request_id=%s upstream_timeout url=%s", request_id, url)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream timeout while retrieving model catalog", "type": "upstream_error", "code": "upstream_timeout"}},
            headers={"X-Request-Id": request_id},
        )
    except Exception as exc:
        logger.exception("request_id=%s upstream_error url=%s", request_id, url)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "proxy_error", "code": "upstream_connection_error"}},
            headers={"X-Request-Id": request_id},
        )

    logger.info(
        "request_id=%s upstream_status=%s upstream_content_type=%s upstream_elapsed_ms=%s",
        request_id,
        response.status_code,
        response.headers.get("content-type"),
        getattr(response, "elapsed", None).total_seconds() * 1000 if getattr(response, "elapsed", None) else None,
    )

    if response.status_code >= 400:
        return upstream_error(response.status_code, response.text, request_id)

    try:
        payload = response.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream returned a non-JSON model catalog", "type": "upstream_error", "code": "invalid_upstream_response"}},
            headers={"X-Request-Id": request_id},
        )

    return JSONResponse(status_code=200, content=payload, headers={"X-Request-Id": request_id})


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    request_id = request_id_header()
    try:
        body = await request.json()
    except Exception:
        return validation_error("Invalid JSON request", request_id, "invalid_json")

    if not isinstance(body, dict):
        return validation_error("Request body must be a JSON object", request_id, "invalid_json")

    requested_model = body.get("model")
    if not requested_model:
        return validation_error("Missing required field: model", request_id, "missing_model")

    messages = body.get("messages")
    if not isinstance(messages, list) or not messages:
        return validation_error("Missing required field: messages", request_id, "missing_messages")

    is_stream = body.get("stream", False)
    if not isinstance(is_stream, bool):
        return validation_error("Field 'stream' must be a boolean", request_id, "invalid_stream")

    log_request_context(request, body, request_id)

    try:
        headers = build_upstream_headers(accept="text/event-stream" if is_stream else "application/json")
    except RuntimeError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(exc), "type": "authentication_error", "code": "missing_zen_api_key"}},
            headers={"X-Request-Id": request_id},
        )

    upstream_url = f"{ZEN_BASE_URL}/chat/completions"
    logger.info("request_id=%s upstream_url=%s upstream_method=POST stream=%s model=%s", request_id, upstream_url, is_stream, requested_model)

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            stream_response = await send_chat_completion_with_retry(client, upstream_url, headers, body, is_stream, request_id)
    except httpx.TimeoutException as exc:
        logger.exception("request_id=%s upstream_timeout url=%s", request_id, upstream_url)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream timeout while generating completion", "type": "upstream_error", "code": "upstream_timeout"}},
            headers={"X-Request-Id": request_id},
        )
    except Exception as exc:
        logger.exception("request_id=%s upstream_connection_error url=%s", request_id, upstream_url)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "proxy_error", "code": "upstream_connection_error"}},
            headers={"X-Request-Id": request_id},
        )

    logger.info(
        "request_id=%s upstream_status=%s upstream_content_type=%s upstream_elapsed_ms=%s",
        request_id,
        stream_response.status_code,
        stream_response.headers.get("content-type"),
        getattr(stream_response, "elapsed", None).total_seconds() * 1000 if getattr(stream_response, "elapsed", None) else None,
    )

    if stream_response.status_code >= 400:
        error_text = await stream_response.aread() if hasattr(stream_response, "aread") else stream_response.text
        return upstream_error(stream_response.status_code, error_text, request_id)

    if is_stream:
        async def stream_generator():
            try:
                async for chunk in stream_response.aiter_bytes():
                    if chunk:
                        yield chunk
            finally:
                try:
                    await stream_response.aclose()
                except Exception:
                    pass

        return StreamingResponse(
            stream_generator(),
            status_code=200,
            media_type="text/event-stream",
            headers={"X-Request-Id": request_id, "Cache-Control": "no-cache, no-transform"},
        )

    try:
        payload = stream_response.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream returned a non-JSON completion response", "type": "upstream_error", "code": "invalid_upstream_response"}},
            headers={"X-Request-Id": request_id},
        )

    return JSONResponse(status_code=200, content=payload, headers={"X-Request-Id": request_id})


@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def universal_proxy(request: Request, path: str):
    request_id = request_id_header()
    clean_path = path.lstrip("/")
    if clean_path.startswith("v1/"):
        clean_path = clean_path[3:]
    target_url = f"{ZEN_BASE_URL}/{clean_path}"
    logger.info("request_id=%s method=%s path=%s upstream_url=%s", request_id, request.method, request.url.path, target_url)

    try:
        headers = build_upstream_headers()
    except RuntimeError as exc:
        return JSONResponse(
            status_code=503,
            content={"error": {"message": str(exc), "type": "authentication_error", "code": "missing_zen_api_key"}},
            headers={"X-Request-Id": request_id},
        )

    body = None
    if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
        try:
            body = await request.json()
        except Exception:
            body = await request.body()

    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            if isinstance(body, dict):
                response = await client.request(request.method, target_url, headers=headers, json=body)
            elif body:
                response = await client.request(request.method, target_url, headers=headers, content=body)
            else:
                response = await client.request(request.method, target_url, headers=headers)
    except httpx.TimeoutException as exc:
        logger.exception("request_id=%s upstream_timeout url=%s", request_id, target_url)
        return JSONResponse(
            status_code=504,
            content={"error": {"message": "Upstream timeout for forwarded request", "type": "upstream_error", "code": "upstream_timeout"}},
            headers={"X-Request-Id": request_id},
        )
    except Exception as exc:
        logger.exception("request_id=%s upstream_connection_error url=%s", request_id, target_url)
        return JSONResponse(
            status_code=502,
            content={"error": {"message": str(exc), "type": "proxy_error", "code": "upstream_connection_error"}},
            headers={"X-Request-Id": request_id},
        )

    logger.info(
        "request_id=%s upstream_status=%s upstream_content_type=%s upstream_elapsed_ms=%s",
        request_id,
        response.status_code,
        response.headers.get("content-type"),
        getattr(response, "elapsed", None).total_seconds() * 1000 if getattr(response, "elapsed", None) else None,
    )

    if response.status_code >= 400:
        return upstream_error(response.status_code, response.text, request_id)

    try:
        payload = response.json()
    except ValueError:
        return JSONResponse(
            status_code=502,
            content={"error": {"message": "Upstream returned a non-JSON response", "type": "upstream_error", "code": "invalid_upstream_response"}},
            headers={"X-Request-Id": request_id},
        )

    return JSONResponse(status_code=response.status_code, content=payload, headers={"X-Request-Id": request_id})

