# NuLixPay Agent

NuLixPay Agent is an AI-powered shopping assistant for the PayPal AI Hackathon. It will understand shopping requests written in natural language, recommend products that fit a user's preferences and budget, compare options, and let the user approve a cart before a simulated PayPal Sandbox payment.

The backend serves a local product catalog, product search, an AI shopping agent, PayPal Sandbox checkout, and optional Channel3 search for products sold by other merchants. The web interface is not wired up yet. The agent recommends products. It does not create or capture payments. Channel3 results cannot be paid through NuLixPay.

## Project layout

```text
nulixpay-agent/
├── backend/
│   ├── main.py            # API entry point and routes
│   ├── catalog.py         # Load, validate, and search products
│   ├── channel3_client.py # Channel3 search for other merchants
│   ├── checkout.py        # Price the cart and capture approved orders
│   ├── shopping_agent.py  # Rank catalog products into a cart
│   ├── agent_llm.py       # Mock parser and optional LLM client
│   ├── agent_schema.py    # Pydantic models for requirements and replies
│   ├── paypal.py          # PayPal Sandbox Orders API client
│   ├── order_store.py     # SQLite record of orders
│   ├── money.py           # Euro amounts as Decimal cents
│   ├── settings.py        # Sandbox settings from the environment
│   └── requirements.txt
├── data/
│   └── products.json      # 15 fictional products
├── tests/
│   ├── test_catalog.py    # Catalog and search tests
│   ├── test_payments.py   # Checkout tests with a mocked PayPal API
│   ├── test_agent.py      # Agent tests with the deterministic mock parser
│   └── test_channel3.py   # Channel3 tests with a fake HTTP client
├── .env.example           # Sandbox variable names, without secrets
├── frontend/              # React + Vite shopping assistant
└── docs/                  # Reserved for project notes
```

`docs/` contains a `.gitkeep` file so Git can track that empty folder. Git does not store a folder that has no files.

## Prerequisites

- Python 3.11 or newer
- PowerShell

Check your Python version:

```powershell
python --version
```

## Run the API locally

From the project root (`nulixpay-agent`):

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r backend\requirements.txt
python -m uvicorn main:app --reload --reload-dir backend --app-dir backend --host 127.0.0.1 --port 8000
```

What each command does:

1. `python -m venv .venv` creates an isolated environment in `.venv` so these packages stay inside the project.
2. `Activate.ps1` switches the current PowerShell session to that environment. Your prompt should start with `(.venv)`.
3. `pip install` installs the packages listed in `backend\requirements.txt`.
4. `uvicorn` starts the server. `--reload` restarts it when you save a Python file. `--app-dir backend` tells Uvicorn to load `main.py` from the `backend` folder. `main:app` means the `app` object inside `main.py`.

If activation is blocked by PowerShell's execution policy, allow scripts for this session only, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

Leave the Uvicorn terminal open while you test. Stop the server with `Ctrl+C`.

## Run the frontend

Install [Node.js LTS](https://nodejs.org/) first. In PowerShell, `node --version` and `npm --version` should both print a version.

Open a second PowerShell window at the project root. The API must stay running on port 8000.

```powershell
cd frontend
npm install
npm run dev
```

If PowerShell says script execution is disabled, allow scripts for this window only, then run the two commands again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

Open http://127.0.0.1:5173. The page calls `http://127.0.0.1:8000`. PayPal, Channel3, and model keys stay in the backend `.env`. The frontend does not contain them.

The API allows browser calls from `http://127.0.0.1:5173` and `http://localhost:5173` only. After PayPal approval, a browser that lands on `/payments/return` is redirected to `http://127.0.0.1:5173/return`. That page reads the order and waits for you to press **Capture payment**. API clients that do not ask for HTML still receive JSON, and the return route still does not capture money.

Stop the frontend with `Ctrl+C`.

## Try the welcome endpoint

With the server running, open:

- http://127.0.0.1:8000/ — raw JSON: a welcome message and `"status": "running"`
- http://127.0.0.1:8000/docs — interactive API docs (Swagger UI)

In `/docs`, open **GET /**, choose **Try it out**, then **Execute**. The response body should be:

```json
{
  "message": "Welcome to NuLixPay Agent",
  "status": "running"
}
```

You can also request it from a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/
```

## Product search

`GET /products` returns the full catalog. `GET /products/search` accepts two optional query parameters:

- `query` — text matched inside the name, category, description, and features. Matching ignores letter case and looks for the text anywhere in those fields. `laptop` matches the category `laptops`.
- `max_price` — a budget in euros. Products priced above it are left out. A product priced exactly at the budget is included.

With neither parameter, search returns every product. The catalog order stays the same.

A missing or invalid `data/products.json` is a server-side problem, so those requests return HTTP 500 with a short `detail` message. A negative or non-numeric `max_price` is a client error and returns HTTP 422.

## Run the tests

From the project root, with the virtual environment active:

```powershell
python -m pip install -r backend\requirements.txt
python -m unittest discover -s tests -v
```

The tests call the API in-process. Payment tests use a fake PayPal transport, so they do not contact PayPal and do not need your Sandbox credentials. Channel3 tests use a fake HTTP client, so they do not spend Channel3 credits. You do not need Uvicorn running.

## Try the catalog in Swagger UI

Start the API, then open http://127.0.0.1:8000/docs.

1. Open **GET /products**, choose **Try it out**, then **Execute**. The response lists all 15 products.
2. Open **GET /products/search**, choose **Try it out**, set `query` to `laptop`, and **Execute**. The three laptops are returned.
3. Set `query` to `usb` and `max_price` to `20`, then **Execute**. Only the Kite Charge 20W charger matches, because the USB-C hub costs 34.50 euros.
4. Set `max_price` to `-1` and **Execute**. Swagger shows a 422 response because a budget cannot be negative.

From a second PowerShell window:

```powershell
Invoke-RestMethod http://127.0.0.1:8000/products
Invoke-RestMethod "http://127.0.0.1:8000/products/search?query=laptop&max_price=700"
```

## External merchant search

`GET /products/external/search` asks Channel3, at `POST https://api.trychannel3.com/v1/search`, for products sold by other merchants. It is separate from `GET /products/search`, which only reads `data/products.json`.

Each result is labeled `External merchant product`. The response includes `checkout_supported: false`. NuLixPay does not sell these products, and a Channel3 product id is not a catalog id. `POST /payments/create-order` still accepts only ids from the local catalog, such as `hp-001`.

The server reads `CHANNEL3_API_KEY` from `.env` and sends it as the `x-api-key` header. The key is not written to the log and is not returned by the API. Leave the variable empty until you want to try a live search. A missing key returns HTTP 503 and does not call Channel3.

The request asks Channel3 for EUR offers. `max_price` is optional and is sent as Channel3's price filter. Each offer keeps the price, currency, availability, merchant domain, and purchase link that Channel3 returned. If Channel3 omits a price or a purchase link, that offer is left out. The API does not invent a price, a stock status, or a merchant name.

`limit` defaults to 5 and cannot be higher than 10, so one Swagger try stays small.

### Configure the key

Add this line to `.env` if it is not there yet. Do not replace the PayPal values already in that file.

```text
CHANNEL3_API_KEY=
```

Put your key after the `=`. Restart Uvicorn after saving `.env`.

### Try a live search in Swagger UI

Start the API, then open http://127.0.0.1:8000/docs.

1. Open **GET /products/external/search**, choose **Try it out**, set `query` to `wool sweater`, leave `max_price` empty, leave `limit` at `5`, and **Execute**.
2. A 200 response has `source: "channel3"`, `checkout_supported: false`, and a `results` list. Each product has `channel3_product_id`, `name`, and `offers` with `price`, `currency`, `availability`, `merchant_domain`, and `purchase_url` when Channel3 supplied them.
3. Open a `purchase_url` in the browser to see the merchant page. That page is not NuLixPay checkout.
4. Copy a `channel3_product_id` into **POST /payments/create-order**. The response is **400** with `Unknown product id`. PayPal is not called.

From a second PowerShell window:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/products/external/search?query=wool%20sweater&limit=5"
```

| What happens | Response | What it means |
| --- | --- | --- |
| `CHANNEL3_API_KEY` is empty | 503 | Add the key to `.env` and restart |
| Channel3 rejects the key | 502 | The key is not included in the response |
| Channel3 rate limit | 429 | Wait, then try a smaller search |
| Channel3 is down or slow | 502 or 504 | Read `detail.message` |
| Channel3 id sent to create-order | 400 | Checkout only sells the local catalog |

The server log line looks like `Channel3 POST /v1/search -> 200`. It does not include the API key or the response body.

## PayPal Sandbox checkout

Checkout uses the PayPal Orders API v2 on `https://api-m.sandbox.paypal.com`. The live PayPal API is not called. Credentials stay in `.env` on the server. The API never returns the client id, the secret, or the access token.

`products.json` is the starting stock. Completed payments and a capture that is still in progress are counted in `data/orders.sqlite`, which is gitignored. The price is calculated on the server with `Decimal`. A browser-supplied amount is rejected.

An order and a payment are different:

1. `POST /payments/create-order` creates an unpaid PayPal order and returns `payment_completed: false` plus an approval URL.
2. The buyer approves that order in the PayPal Sandbox. The browser may land on `/payments/return`. That page does not capture money.
3. `POST /payments/{order_id}/capture` with `{"confirm": true}` captures only when PayPal reports the order as `APPROVED`.
4. `payment_completed: true` and `payment_status: "PAID"` mean the payment is complete.

This demo sets PayPal shipping to `NO_SHIPPING`, so the Sandbox approval screen does not collect a shipping address.

### Configure Sandbox credentials

In the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/applications/sandbox), open your Sandbox REST app and copy the Sandbox client id and secret. From the project root:

```powershell
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
```

If `.env` already exists, add the variables below to it. Do not replace a file that already has your Sandbox credentials. Set:

```text
PAYPAL_MODE=sandbox
PAYPAL_CLIENT_ID=your-sandbox-client-id
PAYPAL_CLIENT_SECRET=your-sandbox-secret
```

Use the Sandbox values only. Leave `PAYPAL_MODE` as `sandbox`. Do not paste `.env` into chat, Swagger, or a screenshot.

You also need a Sandbox personal account, from Dashboard → Sandbox → Accounts. That buyer account approves the payment. It is separate from the REST app credentials.

Restart Uvicorn after changing `.env`.

### Complete a Sandbox payment

Start the API, then open http://127.0.0.1:8000/docs.

1. Open **POST /payments/create-order**, choose **Try it out**, and use:

```json
{
  "items": [
    {"product_id": "hp-001", "quantity": 1}
  ]
}
```

Execute. A 201 response has `payment_completed: false`, `amount: "129.00"`, `currency: "EUR"`, and `approval_url`. Copy the `order_id`.

2. Paste `approval_url` into the browser. Log in with the Sandbox personal account and approve the order. The browser opens `/payments/return`. The JSON says no payment was captured.

3. Open **GET /payments/{order_id}**, enter the order id, and Execute. After approval, `order_status` is `APPROVED` and `payment_status` is still `NOT_PAID`.

4. Open **POST /payments/{order_id}/capture**, enter the same order id, and use:

```json
{"confirm": true}
```

Execute. A completed payment has `payment_completed: true`, `payment_status: "PAID"`, and `order_status: "COMPLETED"`.

5. Execute capture again. The response is **409**. `payment_status` is `PAID`, and the order is not captured a second time.

The same steps in PowerShell, after you replace the order id:

```powershell
$body = @{ items = @(@{ product_id = "hp-001"; quantity = 1 }) } | ConvertTo-Json -Depth 4
$created = Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8000/payments/create-order -ContentType "application/json" -Body $body
$created.approval_url
$created.order_id

$confirm = @{ confirm = $true } | ConvertTo-Json
Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:8000/payments/$($created.order_id)/capture" -ContentType "application/json" -Body $confirm
```

Open `approval_url` and approve the order before the capture command. Run capture only after that approval.

### Safe checks while you test

| What you send | Response | What it means |
| --- | --- | --- |
| `amount` in the create body | 422 | The server will not take a browser price |
| Unknown product, or quantity `0` | 400 or 422 | The cart was rejected before PayPal |
| Dawn Glass Mini `sw-003` | 409 | No stock, PayPal was not called |
| Capture before approval | 409 | The buyer has not approved it |
| Capture without `confirm: true` | 422 | There is no explicit confirmation |
| Declined funding source | 402 | `payment_status` is `DECLINED`. Money was not captured |
| Capture of a paid order | 409 | Duplicate capture was refused |
| Wrong or missing Sandbox credentials | 502 | Credentials were rejected. The secret is not in the response |
| `PAYPAL_MODE` other than `sandbox` | 503 | Live mode is disabled |
| PayPal Sandbox is down or slow | 502 or 504 | Read `detail.message`. A timeout uses 504 |

PayPal errors can also include `detail.issue` and `detail.debug_id`. Those two values are safe to copy into the PayPal Sandbox dashboard when you look up the call. Do not copy `.env`, the `Authorization` header, or a buyer email address.

The server log line for a PayPal call looks like `PayPal sandbox POST /v2/checkout/orders -> 201`. It does not include the access token or the request body. `GET /payments/{order_id}` is the safe way to see whether an order is still unpaid or already paid. `payment_completed: false` means money has not been captured, even when the buyer has approved the order.

## Shopping agent

`POST /agent/chat` reads a natural-language request, then recommends products from `data/products.json`. Prices come from that file. Available stock is the catalog stock minus paid and in-progress quantities in `data/orders.sqlite`. The route does not create a PayPal order and does not capture a payment.

`AGENT_MODE=mock` is the default. It uses a deterministic parser, so Swagger and the automated tests work without a paid model API. To use a model, set these values in `.env` and restart the API:

```text
AGENT_MODE=llm
LLM_API_BASE=https://api.openai.com/v1
LLM_API_KEY=your-key
LLM_MODEL=gpt-4o-mini
```

The key stays in `.env`. Keep `LLM_API_BASE` on `https`. The shopper's message cannot change that URL.

With the server running, open http://127.0.0.1:8000/docs, choose **POST /agent/chat**, then **Try it out**, and send:

```json
{"message": "I need noise cancelling headphones and a charger under 200 euros"}
```

A recommendation includes `items`, `total_eur`, `explanation`, and `payment_created: false`. If the request does not name a product type, `status` is `clarification` and `clarifying_question` asks which category to use. A budget that no catalog product can meet returns `status: "no_match"` and an `unmet_requirements` entry. To pay, copy the returned product ids into `POST /payments/create-order` yourself.
