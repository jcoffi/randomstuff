---
name: norgate-database-connect
description: Use when connecting to the local Norgate Data Updater from Linux or Wine, especially when localhost requests fail, Norgate Python calls return HTTP 500, or price, symbol-list, watchlist, or index-constituent queries need the repo-safe connection pattern.
---

# Norgate Database Connect

## Overview

On this machine, Norgate access works only if the NDU/Wine web API URL is patched from `localhost` to `[::1]` before importing `norgatedata`. Core principle: fix transport first, then run a tiny smoke test, then use repo-safe defaults (`NONE` adjustments, fail-closed identity handling).

## When to Use

- Connecting to local Norgate from Linux/Wine
- Seeing HTTP 500 or connection failures against `localhost`
- Fetching `database_symbols`, `price_timeseries`, `watchlist_symbols`, or `index_constituent_timeseries`
- Validating Norgate access before wiring ingest or membership logic

Precondition: Norgate Data Updater (NDU) must already be running under Wine on this machine.

Do not use this skill for canonical index-ID mapping rules; use repo-specific schema/identity guidance for that.

## Quick Reference

| Task | Correct pattern |
|---|---|
| Import order | Patch `norgate_web_api_base_url` before `import norgatedata` |
| Linux/Wine loopback | Replace `localhost` with `[::1]` |
| Local service precondition | NDU must be running in Wine |
| Universe smoke test | `database_symbols("US Equities")` |
| Raw price ingest | `StockPriceAdjustmentType.NONE`, `PaddingType.NONE` |
| Membership pandas output | Look for exact column `Index Constituent` |

## Implementation

```python
import norgatedata.norgatehelper as _nh

_nh.norgate_web_api_base_url = _nh.norgate_web_api_base_url.replace(
    "localhost", "[::1]"
)

import norgatedata

# NDU/Wine must already be running, or the smoke test will fail.
symbols = norgatedata.database_symbols("US Equities")

prices = norgatedata.price_timeseries(
    "AAPL",
    stock_price_adjustment_setting=norgatedata.StockPriceAdjustmentType.NONE,
    padding_setting=norgatedata.PaddingType.NONE,
    timeseriesformat="pandas-dataframe",
)
```

For index membership:

```python
membership = norgatedata.index_constituent_timeseries(
    "AAPL",
    "S&P 500",
    padding_setting=norgatedata.PaddingType.NONE,
    pandas_dataframe=prices.copy(),
    timeseriesformat="pandas-dataframe",
)

if "Index Constituent" not in membership.columns:
    raise RuntimeError("Missing Norgate membership column")
```

## Common Mistakes

- Importing `norgatedata` before patching the URL
- Using `localhost`/IPv4 instead of `[::1]`
- Using adjusted prices for raw ingest paths
- Treating provider/index symbols as canonical repo identities
- Assuming any membership column name other than exact `Index Constituent`
- Forgetting that NDU itself must be running under Wine first

## Rationalizations

| Excuse | Reality |
|---|---|
| "`localhost` should be fine" | On this machine it can return HTTP 500; `[::1]` is the known-good path. |
| "I only need price history" | The import-order/IPv6 fix still applies first. |
| "Total return is close enough" | Repo ingest expects raw/unadjusted OHLCV unless a path explicitly says otherwise. |
| "I can hash the provider symbol directly" | Connection is separate from identity; canonical IDs must come from repo rules, not vendor aliases. |

## Red Flags

- You imported `norgatedata` before patching the helper URL
- You are debugging data content before proving connectivity with `database_symbols(...)`
- You are using adjusted price settings in a raw ingest path
- You are turning provider symbols like `$SPX` into stored identities

All of these mean: stop, re-establish the `[::1]` patch/import order, then retry with a minimal smoke test.
