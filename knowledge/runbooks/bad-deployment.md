# Runbook: Bad deployment or release

## What it looks like
- Error rate (5xx) jumps right after a new deploy or release.
- Logs show new exceptions that mention the new version, for example KeyError or NullPointerException.
- Pods are in CrashLoopBackOff or fail readiness checks after a rollout.

## How to investigate
1. List recent deploys and compare the deploy time with the time the alert started.
2. Search ERROR logs and look for exceptions that mention the new release version.
3. Check that CPU, memory and traffic are normal (so the cause is the code, not load).

## How to fix
- Roll back the deploy to the previous known good version (rollback_deploy).
- If the problem is only a missing config or secret, fix the config and restart the service.
