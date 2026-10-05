# ADR-0004: a layered architecture, enforced as it is built

## Status

Accepted. Supersedes three contracts in the original `.importlinter`.

## Context

The first `.importlinter` described a ports-and-adapters design that the
application was never built to: services were to reach the database only
through ports, the web layer was to call services only, and SQLAlchemy was to
stay inside the database package. The code went the other way from the start.
Application services own their unit of work and query through repositories and
SQLAlchemy directly; the server-rendered read models in `web/view_models` query
the database for the screens they build. Three of the six contracts therefore
failed on every run, with 565 reported import chains, and the gate that should
have caught a new violation was red for good — so it caught nothing.

A fourth contract ("audit writes go through `audit.record`") failed only on a
definition error: forbidden contracts follow indirect imports, and
`audit.record`, the sanctioned writer, itself imports `audit.store`.

Measuring the real package dependencies showed a clean layered shape with two
genuine upward imports:

- the curated demo seed lived in `db.seed` but drove `services.engine` and
  `services.registry`, so the database layer depended on the application layer;
- `web.application` and `web/__init__` imported the application factory from
  `covenant_radar.asgi`, the entrypoint above them.

## Decision

The architecture is the one the code has, written down and enforced:

| Layer | Packages | May import |
|---|---|---|
| Entrypoints | `asgi`, `cli` (independent of each other) | everything below |
| Presentation | `web`, `api` | application and below |
| Application | `services`, `demo`, `lifecycle` | adapters and below |
| Adapters | `ai`, `audit`, `db`, `documents`, `ingestion`, `ml`, `notifications`, `reporting`, `scheduler` | domain and below |
| Domain | `domain` | foundation |
| Foundation | `config`, `i18n`, `observability`, `ports`, `security` | `core` |
| Core | `core` | nothing in the package |

Packages in one layer may import each other. Nothing imports upward.

`.importlinter` enforces this with a `layers` contract, and keeps the rules
that held all along: domain purity (no frameworks, adapters or services in
`domain`), only the AI client imports model providers, and audit writes go
through `audit.record` (now checking direct imports, which is what the rule
means). A new contract keeps `core`, `domain`, `i18n` and `ports` free of
FastAPI, Starlette and SQLAlchemy.

The two upward imports were fixed in code:

- the demo seed moved to `covenant_radar.demo.curated`, beside the rest of the
  demo application code;
- the application factory moved to `covenant_radar.web.app`; `asgi` is now a
  thin entrypoint that builds the default app and re-exports the factory.

## Consequences

The gate is green and fails on the next upward import, so it protects the
design again. It no longer claims that services and screens avoid SQLAlchemy:
they do not, and a contract that says otherwise only teaches people to ignore
the gate. Introducing repository ports later remains possible; if that work
is done, the session contract can return for the packages it covers.
