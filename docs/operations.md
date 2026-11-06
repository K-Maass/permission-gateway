# Integration and operations

## Register a local A2A worker

The declared demo capability is `summarize.document` for a task with the synthetic `personal`
classification. Registration lives in the routing module and deployment manifest. Declare the
worker's capabilities, data classifications, and fixed endpoint there. For the sample run, update
`PAOS_WORKER_URL` in the API and gateway service environments to that peer's base URL on the
internal Compose network. The peer must advertise
a usable A2A card, support streaming and reattachment using its task handle, and produce the
two UTF-8 report revisions shown in the scripted peer. Keep the lower-classification worker in
the candidate list to exercise exclusion.

The gateway can resume a persisted task handle after a gateway restart. If the peer loses the
handle, inspect the task in the console before creating a replacement. The scripted peer keeps
its run state in memory, so restarting the peer itself during a run loses that state.

## Operator API

The console signs in with `POST /api/session` and uses the returned 12-hour session token as an
Authorization bearer. Keep the token on the app's origin. `POST /api/tasks` with an empty JSON
object starts the sample. Read task state, download revisions, and approve or deny through the
task-specific routes. The task stream carries the same snapshot in an AG-UI event envelope.
Pass `Last-Event-ID` when reconnecting. `GET /api/memory` returns the synthetic retrieval view.

## Write adapter contract

The included adapter records a report in a local Postgres register. Review the proposed revision
before approving. The server associates the approved action with its immutable digest and
records the decision and expiry using the database clock.

To add an action, define its typed proposal and operator-facing preview, supply a trusted
dispatch mapping, and implement a stable idempotency key and a verify-before-retry operation.
An adapter must be able to prove whether the requested result already exists and whether its
target and digest match. If it cannot establish the outcome, surface it for reconciliation.
The sample's one-write guarantee applies to its unique database key and its postcondition
check. Treat new adapters as a separate integration that needs the crash and replay tests.

## Back up and restore

For a recovery checkpoint, let open reviews settle before taking the backup. The dump contains
application data, including the artifact bytes. Store it as you would the underlying data.

```sh
./scripts/backup checkpoint.dump
```

From a fresh clone with its own generated credentials, pick a new Compose project and start
only its database. Restore a trusted dump created by this release.

```sh
./scripts/setup
export COMPOSE_PROJECT_NAME=paos-recovered
export PAOS_PORT=8766
docker compose up --wait -d db
./scripts/restore /path/to/checkpoint.dump
docker compose up --build --wait -d
```

The restore script requires an empty database and discards restored login sessions before the
application starts. Use the token from the recovered installation.

## Wider PAOS boundaries

**Runtime admission** binds dispatch to the selected endpoint and verifies runtime identity and
capability before handing work to an agent. The release slice exercises classification-based
selection and pins the A2A card to its declared endpoint. Runtime provisioning belongs to the
wider architecture.

**Tool mediation** turns a requested external action into an effect with policy, decision,
execution, and verification stages. In this slice the gateway holds the database credential and
the worker returns report bytes. The approval decision applies to the report digest and the
gateway verifies the result it wrote to the register.

## Verify

```sh
uv sync --locked --python 3.13
uv run --locked python -m playwright install chromium
./scripts/verify
```

On Linux, use `playwright install --with-deps chromium` if browser libraries are missing.
The test script starts and removes uniquely named synthetic stacks. It verifies the browser flow,
A2A reattachment, worker exclusion, artifact integrity, authorization, memory, and recovery.
