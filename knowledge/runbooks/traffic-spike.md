# Runbook: Traffic spike and CPU saturation

## What it looks like
- Request rate is far above its baseline (a campaign, a flash sale, a retry storm).
- CPU is close to 100% on every replica, latency and timeouts go up together.
- Logs show saturated worker pools or queue depth warnings, not new exceptions.
- There was no recent deploy, and memory is normal.

## How to investigate
1. Compare request_rate and cpu_percent with their baselines in the metrics.
2. Search WARN and ERROR logs for saturation, queue depth or upstream timeouts.
3. Check the deploy list to rule out a bad release.

## How to fix
- Scale out (scale_service). A good first target is current replicas x request_rate ratio, capped by MAX_REPLICAS.
- A restart does NOT help: the load is real and comes back at once.
- Follow up: add autoscaling for the service and load-test before the next campaign.
