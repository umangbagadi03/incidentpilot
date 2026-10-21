# Runbook: Database connection pool exhaustion

## What it looks like
- Latency goes up on endpoints that use the database.
- Logs show PoolTimeout, "connection refused" or "max connections reached".
- Error rate rises together with latency.

## How to investigate
1. Search ERROR logs for pool or connection timeouts.
2. Check if a traffic spike (request rate) happened at the same time.
3. Check for a slow query or a slow third-party call that keeps connections open.

## How to fix
- Restart the service (restart_service) to release hung connections.
- If traffic is the cause, scale out (scale_service), but only if the database can take more connections.
- Follow up: fix the slow query or increase the pool size in the config.
