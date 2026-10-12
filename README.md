# V8 BTC Consensus — Paper Only

This package adds a separate V8 consensus paper-trading service and a small HTTP signal handoff to V5, V6.3, and V7. Existing bot Telegram messages and existing individual CSV/P&L reporting remain in place. No authenticated Kalshi API credentials, order endpoints, or live trading logic are added.

## What V8 tracks
- Only enters when fresh V5, V6.3, and V7 signals all agree on BUY UP or all agree on BUY DOWN for the exact same `KXBTC15M` ticker.
- Any disagreement, NO TRADE, stale/missing signal, invalid price, or duplicate ticker results in no paper entry.
- Uses the highest of the three quoted entry prices as a conservative paper entry.
- Public market result lookup for settlement; binary-contract gross P&L, configurable per-contract fee assumption, net P&L.
- Telegram notifications for entries, settlements, and performance updates.
- CSV ledgers: `v8_signals.csv`, `v8_consensus_log.csv`, `v8_paper_trades.csv`, `v8_performance.csv`.
- Performance metrics: settled count, open count, wins/losses, win rate, gross P&L, fees, net P&L, average win/loss, profit factor, maximum drawdown.

## Deploy layout
Deploy `bot_v8_consensus_paper.py` as a separate Python service with a persistent disk/volume for its CSV files. Set its start command to `python bot_v8_consensus_paper.py`.

Required V8 environment variables:
- `V8_SHARED_SECRET`: a long random secret shared only by V8 and the three signal-producing services.
- `PORT`: platform-provided port (defaults to 8080 locally).

Optional V8 variables:
- `V8_DATA_DIR`: persistent directory for CSV files (default current directory).
- `V8_FRESH_SECONDS`: max signal age (default 90).
- `V8_CONSENSUS_WAIT_SECONDS`: wait before logging missing-signal NO TRADE (default 25).
- `V8_FEE_PER_CONTRACT`: fee assumption per contract (default 0.00; set to a defensible fee estimate to see net P&L).
- `V8_MAX_ENTRY_PRICE` / `V8_MIN_ENTRY_PRICE`: valid entry range (defaults $0.60 / $0.05).
- `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`: optional V8 performance notifications.

In each V5, V6.3, and V7 service, deploy its corresponding patched file in this package and include `v8_bridge.py` in the same working directory. Set:
- `V8_SIGNAL_URL=https://YOUR-V8-SERVICE-DOMAIN/signal`
- `V8_SHARED_SECRET` to exactly the same value as V8.

Keep each bot's existing environment variables unchanged. The bridge is best-effort: if V8 is unreachable, it logs an error but does not stop or change the originating bot. Do not put the secret in source code or share it in screenshots.

## Before starting all services
1. Back up the existing scripts and CSVs.
2. Add persistent storage to V8; ephemeral filesystem resets would lose its history.
3. Set a unique strong secret in V8 and all three signal services.
4. Start V8, check `/health`, then deploy the patched V5/V6.3/V7 scripts.
5. Verify V8 receives signals from all three bot labels and creates NO TRADE rows when they disagree.
6. Keep the system paper-only while collecting a meaningful sample.

## Important notes / limitations
- The package is not deployed for you; environment variables and service URLs must be set in your hosting dashboard.
- V8 consensus uses the bots' current signal decisions and quoted prices. It does not establish that consensus is profitable; evaluate a large sample, including all agreements, before drawing conclusions.
- V8 fee accounting is only as accurate as `V8_FEE_PER_CONTRACT`; default is zero and therefore net P&L equals gross P&L until configured.
- The service records one consensus decision per ticker for its process lifetime. Use persistent storage and avoid multiple V8 replicas, which could duplicate processing.
- Review your hosting setup for the exact V6.3 and V7 versions currently deployed before replacing them with these package copies.
