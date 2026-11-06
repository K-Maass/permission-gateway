# Reporting a security issue

Use [GitHub private vulnerability reporting](https://github.com/K-Maass/permission-gateway/security/advisories/new)
for suspected security issues. Include the commit or release, a minimal reproduction with
synthetic data, the expected security boundary, and what happened. Redact credentials and
private input from any logs or traces.

The reference deployment serves one operator on a loopback address. It keeps worker and
database traffic on separate internal networks. Treat a worker endpoint as an operator
configuration decision. See [integration and operations](docs/operations.md) for the
deployment and adapter boundaries.
