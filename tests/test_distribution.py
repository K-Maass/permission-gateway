from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]


def test_compose_exposes_only_the_operator_app():
    manifest = yaml.safe_load((ROOT / "compose.yaml").read_text())
    assert manifest["networks"]["core"]["internal"]
    exposed = {name: service["ports"] for name, service in manifest["services"].items()
               if service.get("ports")}
    assert set(exposed) == {"api"}
    assert all(str(port).startswith("127.0.0.1:") for port in exposed["api"])
    assert manifest["networks"]["agents"]["internal"]
    assert manifest["services"]["db"]["networks"] == ["core"]
    assert manifest["services"]["gateway"]["networks"] == ["core", "agents"]
    for name in ("worker", "worker-public"):
        assert manifest["services"][name]["networks"] == ["agents"]
    worker = manifest["services"]["worker"]
    assert "PAOS_OPERATOR_TOKEN" not in worker.get("environment", {})
    assert "PAOS_DATABASE_URL" not in worker.get("environment", {})
    assert "docker.sock" not in (ROOT / "compose.yaml").read_text()
