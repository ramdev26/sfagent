# Supplement Factory (live WooCommerce)

## Merchant agent only (recommended)

```powershell
.\.venv\Scripts\Activate.ps1
python scripts/run_demo.py supplement_factory --merchant
```

- Portal: http://localhost:3104  
- Merchant API: http://localhost:8004/api/merchant/health  

Live catalog, orders, and low-stock alerts from WooCommerce. Price / stock / listing edits are **staged** until you click Approve (then written via REST).

Try: “How are sales this month?”, “Which SKUs are low stock?”, “Raise Kevin Levrone creatine by 5%”.

## Shopping storefront (optional)

```powershell
python scripts/run_demo.py supplement_factory
```

- Storefront: http://localhost:3004  

## Credentials

`examples/supplement_factory/.env` (or repo-root `.env`):

- `WOOCOMMERCE_URL`
- `WOOCOMMERCE_CONSUMER_KEY` / `WOOCOMMERCE_CONSUMER_SECRET`

## Deploy on Vercel (API + portal)

Use **two Vercel projects** from the same GitHub repo ([ramdev26/sfagent](https://github.com/ramdev26/sfagent)). FastAPI runs as one Fluid Compute function (`app.py`); the merchant UI is Next.js.

### Project A — merchant API

1. Vercel → **Add New** → **Project** → import `ramdev26/sfagent`
2. Settings:
   - **Root Directory**: leave empty (repo root)
   - Framework should detect **FastAPI** (`vercel.json` + `app.py`)
   - Install uses `requirements-vercel.txt` (Messages API only; no Agent SDK — keeps under Hobby size limits)
3. Environment variables:

| Key | Value |
|-----|--------|
| `ANTHROPIC_API_KEY` | your Anthropic key |
| `WOOCOMMERCE_URL` | `https://supplementfactory.lk` |
| `WOOCOMMERCE_CONSUMER_KEY` | Woo REST key |
| `WOOCOMMERCE_CONSUMER_SECRET` | Woo REST secret |
| `DEMO_ALLOWED_HOSTS` | your API host, e.g. `sfagent-api.vercel.app` (no `https://`) |

4. Deploy. Check `https://<api-host>/api/merchant/health`

### Project B — merchant portal

1. **Add New** → **Project** → same repo again
2. Settings:
   - **Root Directory**: `examples/supplement_factory/merchant-web`
   - Framework: **Next.js** (uses that folder’s `vercel.json`)
3. Environment variable (Production + Preview):

| Key | Value |
|-----|--------|
| `NEXT_PUBLIC_API_URL` | `https://<api-host>` from Project A (no trailing slash) |

4. Deploy. Open the portal URL and confirm Home loads sales/orders.

**Notes**

- Sessions and memory are **in-process**. A cold start or a different function instance can drop a chat session; refresh and start again.
- Agent turns are capped at **300s** (`maxDuration` in root `vercel.json`). Long tool loops may need a Pro plan / higher limit.
- CORS already allows `*.vercel.app` and localhost. Override with `DEMO_CORS_ORIGIN_REGEX` if you add a custom domain.
