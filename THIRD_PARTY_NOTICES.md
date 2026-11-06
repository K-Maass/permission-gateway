# Third-party software

PAOS Core's [MIT license](LICENSE) covers the original code in this repository.
The build obtains dependencies from their upstream distributions using the lockfiles and
image digests. Each upstream dependency retains its own license.

| Dependency | License |
| --- | --- |
| [A2A Python SDK](https://github.com/a2aproject/a2a-python) | Apache-2.0 |
| [AG-UI protocol](https://github.com/ag-ui-protocol/ag-ui) | MIT |
| [FastAPI](https://github.com/fastapi/fastapi) | MIT |
| [HTTPX](https://github.com/encode/httpx) | BSD-3-Clause |
| [Psycopg](https://github.com/psycopg/psycopg) | LGPL-3.0-only |
| [Uvicorn](https://github.com/Kludex/uvicorn) | BSD-3-Clause |
| [React](https://github.com/facebook/react) | MIT |
| [Vite](https://github.com/vitejs/vite) | MIT |

This source release includes a build recipe. If distributing a built image or bundled
dependencies, retain the notices and comply with the terms for that distribution.
