# Restart the gateway and deny a write

Run the [quickstart](../README.md) with a slower synthetic worker so there is time to
restart the gateway during the run:

```sh
./scripts/setup
export PAOS_FIXTURE_STEP_DELAY=8
docker compose up --build --wait -d
./scripts/operator-token
```

1. Sign in at [the local console](http://127.0.0.1:8765). Select **Start release review**.
2. Once Activity shows **A2A task and context IDs recorded**, restart the gateway in your terminal.

   ```sh
   docker compose restart gateway
   ```

3. Keep the browser open. Activity should show **Reattached using the recorded A2A handle**.
   Wait for both artifact revisions and the operator decision card.
4. Select **Deny**. The task should become **Denied** with no receipt. Both report revisions
   remain available to inspect and download.

The worker stays running through this exercise. The automated browser test also checks the
worker's dispatch count to prove there was only one dispatch. A separate crash test verifies
receipt recovery when the register write already committed.

Stop the stack with `docker compose down`. Compose retains the database volume.
