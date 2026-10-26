# Repository Agent Instructions

## New Platform Integrations

When a task asks to add, enable, or extend a social platform integration—such as competitor-account fetching, account analytics, login, publishing, or platform-specific access handling—read `docs/platform-adapter-guide.md` before planning or editing code. Treat it as the repository's required implementation and review checklist.

Also read `docs/platform-access.md` when the task changes queues, per-account concurrency, request pacing, locks, circuit breakers, or access-status reporting. Keep platform-specific behavior inside the platform adapter; shared application code should consume the documented common structures and interfaces.
