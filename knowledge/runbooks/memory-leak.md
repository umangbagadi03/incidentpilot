# Runbook: Memory leak and OOM kills

## What it looks like
- Memory usage keeps growing and never goes back down.
- Logs show OutOfMemoryError, high GC (garbage collection) time, or OOMKilled containers.
- Containers restart again and again, and latency rises before each restart.
- There was no recent deploy, so the leak is usually an older bug that finally hit the memory limit.

## How to investigate
1. Check memory in the metrics. Compare the latest value with the baseline.
2. Search ERROR logs for OutOfMemoryError or OOMKilled.
3. Check the deploy list to rule out a bad release.

## How to fix
- Restart the service (restart_service) to free the memory. This is a temporary fix.
- Open a ticket for the team to fix the leak in code, because it will come back.
- Scaling out does not fix a leak. Each replica leaks the same way.
