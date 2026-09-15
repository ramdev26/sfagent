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
