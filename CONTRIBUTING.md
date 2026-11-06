# Contributing

Start with the [quickstart](README.md) and use synthetic fixtures in reports and tests.
For security issues, follow the [private reporting instructions](SECURITY.md).

Run the release checks from the root of your checkout:

```sh
uv sync --locked --python 3.13
uv run --locked ruff check src packages tests scripts
uv run --locked python -m playwright install chromium
./scripts/verify
```

On Linux, add `--with-deps` to the browser install if the system libraries are missing.
The verifier starts disposable Compose stacks. In a pull request, explain the behavior
change and include the commands you used to verify it.
