# OpenCode Zen Proxy

A minimal FastAPI relay that forwards OpenCode requests to [OpenCode Zen](https://opencode.ai/zen) over HTTPS. Hosted on Render Free. The proxy holds your Zen API key as an environment variable — it does not run any model itself; inference happens on Zen.

## Architecture

```
OpenCode (Windows) ──HTTPS──> Render Free (FastAPI) ──HTTPS──> OpenCode Zen ──> Ox Alpha Free
```

## Deploy

1. Fork/clone this repo (private is fine — Render accesses it via GitHub OAuth).
2. In the [Render dashboard](https://dashboard.render.com/): **New → Web Service**, connect the repo.
   - Build Command: `pip install -r requirements.txt`
   - Start Command: `uvicorn main:app --host 0.0.0.0 --port $PORT`
   - Instance Type: **Free**
3. Add environment variable: `ZEN_API_KEY` = your Zen API key.
4. Deploy.

## Endpoints

| Path | Method | Purpose |
| --- | --- | --- |
| `/` | GET | Health/identity check |
| `/health` | GET | Health check |
| `/v1/models` | GET | Full Zen model catalog |
| `/v1/chat/completions` | POST | Chat completions (streaming + non-streaming) |
| `/{path:path}` | any | Universal proxy to Zen |

## Configure OpenCode

- Base URL: `https://<your-service>.onrender.com/v1`
- Model: `x-preview-f-free`

## Notes

- Render Free sleeps after ~15 min of no inbound traffic; cold start is ~1 min.
- 750 free instance-hours/month. Watch usage for bandwidth/build limits.
- Never commit `ZEN_API_KEY`. It lives only in Render's environment variables.
