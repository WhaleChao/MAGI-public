"""Tests for Phase 0-1: Registry foundation modules.

Covers service_registry, model_registry, node_registry, datastore_registry.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

import pytest

# Ensure project root on path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _reset_registries():
    """Reset all registry singletons between tests."""
    from api.routing import service_registry, model_registry, node_registry, datastore_registry
    for mod in (service_registry, model_registry, node_registry, datastore_registry):
        mod._loaded = False
        if hasattr(mod, "_registry"):
            mod._registry = {}
        if hasattr(mod, "_roles"):
            mod._roles = {}
        if hasattr(mod, "_aliases"):
            mod._aliases = set()
        if hasattr(mod, "_nodes"):
            mod._nodes = {}
        if hasattr(mod, "_stores"):
            mod._stores = {}
    yield


@pytest.fixture
def json_dir(tmp_path):
    """Provide a temp json dir and patch get_json_dir to return it."""
    with mock.patch("api.routing.service_registry.get_json_dir", return_value=tmp_path), \
         mock.patch("api.routing.model_registry.get_json_dir", return_value=tmp_path), \
         mock.patch("api.routing.node_registry.get_json_dir", return_value=tmp_path), \
         mock.patch("api.routing.datastore_registry.get_json_dir", return_value=tmp_path):
        yield tmp_path


# ===========================================================================
# Service Registry
# ===========================================================================

class TestServiceRegistry:

    def test_load_from_json(self, json_dir):
        from api.routing.service_registry import get_service, get_service_url, list_services
        (json_dir / "services.json").write_text(json.dumps({
            "services": {
                "test_svc": {
                    "description": "Test service",
                    "host": "10.0.0.1",
                    "port": 9090,
                    "protocol": "http",
                    "env_override": {}
                }
            }
        }))
        svc = get_service("test_svc")
        assert svc is not None
        assert svc.host == "10.0.0.1"
        assert svc.port == 9090
        assert svc.base_url == "http://10.0.0.1:9090"
        assert get_service_url("test_svc") == "http://10.0.0.1:9090"
        assert get_service_url("test_svc", path="/v1/chat") == "http://10.0.0.1:9090/v1/chat"
        assert len(list_services()) == 1

    def test_env_override(self, json_dir):
        from api.routing.service_registry import get_service_host_port, reload
        (json_dir / "services.json").write_text(json.dumps({
            "services": {
                "my_svc": {
                    "host": "default.host",
                    "port": 1234,
                    "env_override": {"host": "MY_HOST", "port": "MY_PORT"}
                }
            }
        }))
        with mock.patch.dict(os.environ, {"MY_HOST": "override.host", "MY_PORT": "5678"}):
            reload()
            host, port = get_service_host_port("my_svc")
            assert host == "override.host"
            assert port == 5678

    def test_missing_service_raises(self, json_dir):
        from api.routing.service_registry import get_service_url
        (json_dir / "services.json").write_text(json.dumps({"services": {}}))
        with pytest.raises(KeyError, match="Unknown service"):
            get_service_url("nonexistent")

    def test_missing_file_returns_empty(self, json_dir):
        from api.routing.service_registry import list_services
        # No services.json written
        assert list_services() == []

    def test_reload(self, json_dir):
        from api.routing.service_registry import get_service, reload
        (json_dir / "services.json").write_text(json.dumps({
            "services": {"a": {"host": "h1", "port": 1}}
        }))
        assert get_service("a").host == "h1"
        (json_dir / "services.json").write_text(json.dumps({
            "services": {"a": {"host": "h2", "port": 2}}
        }))
        reload()
        assert get_service("a").host == "h2"


# ===========================================================================
# Model Registry
# ===========================================================================

class TestModelRegistry:

    def test_load_roles(self, json_dir):
        from api.routing.model_registry import get_role_model, list_roles
        (json_dir / "models.json").write_text(json.dumps({
            "roles": {
                "text_primary": {"model": "my-model-v1", "description": "Primary"},
                "vision": {"model": "vision-v1", "description": "Vision"},
            },
            "aliases": {"names": ["old-name"]}
        }))
        assert get_role_model("text_primary") == "my-model-v1"
        assert get_role_model("vision") == "vision-v1"
        assert len(list_roles()) == 2

    def test_fallback_chain(self, json_dir):
        from api.routing.model_registry import get_role_model
        (json_dir / "models.json").write_text(json.dumps({
            "roles": {
                "text_primary": {"model": "base-model", "description": "Primary"},
                "summary": {"model": None, "fallback_role": "text_primary", "description": "Summary"},
                "code": {"model": None, "fallback_role": "summary", "description": "Code"},
            },
            "aliases": {"names": []}
        }))
        assert get_role_model("summary") == "base-model"
        assert get_role_model("code") == "base-model"

    def test_env_override(self, json_dir):
        from api.routing.model_registry import get_role_model, reload
        (json_dir / "models.json").write_text(json.dumps({
            "roles": {
                "text_primary": {
                    "model": "default-model",
                    "env_override": "MY_MODEL_ENV",
                    "description": "Primary"
                }
            },
            "aliases": {"names": []}
        }))
        with mock.patch.dict(os.environ, {"MY_MODEL_ENV": "env-model"}):
            reload()
            assert get_role_model("text_primary") == "env-model"

    def test_aliases(self, json_dir):
        from api.routing.model_registry import is_alias, resolve_model
        (json_dir / "models.json").write_text(json.dumps({
            "roles": {"text_primary": {"model": "gemma-4-26b", "description": "Primary"}},
            "aliases": {"names": ["taide", "gemma4", ""]}
        }))
        assert is_alias("taide") is True
        assert is_alias("gemma4") is True
        assert is_alias("") is True
        assert is_alias("unknown-model") is False
        assert resolve_model("taide") == "gemma-4-26b"

    def test_resolve_with_available(self, json_dir):
        from api.routing.model_registry import resolve_model
        (json_dir / "models.json").write_text(json.dumps({
            "roles": {"text_primary": {"model": "gemma-4-26b-a4b-it-4bit", "description": "P"}},
            "aliases": {"names": ["taide"]}
        }))
        available = ["gemma-4-26b-a4b-it-4bit", "other-model"]
        assert resolve_model("taide", available=available) == "gemma-4-26b-a4b-it-4bit"

    def test_unknown_role_falls_back(self, json_dir):
        from api.routing.model_registry import get_role_model
        (json_dir / "models.json").write_text(json.dumps({
            "roles": {"text_primary": {"model": "fallback-model", "description": "P"}},
            "aliases": {"names": []}
        }))
        assert get_role_model("nonexistent_role") == "fallback-model"


# ===========================================================================
# Node Registry
# ===========================================================================

class TestNodeRegistry:

    def test_load_nodes(self, json_dir):
        from api.routing.node_registry import get_node, get_node_ip, list_nodes
        (json_dir / "nodes.json").write_text(json.dumps({
            "nodes": {
                "melchior": {
                    "description": "Remote node",
                    "role": "inference",
                    "tailscale_ip": "100.1.2.3",
                    "services": {
                        "inference": {"port": 5002, "protocol": "http"}
                    },
                    "env_override": {}
                }
            }
        }))
        node = get_node("melchior")
        assert node is not None
        assert node.tailscale_ip == "100.1.2.3"
        assert get_node_ip("melchior") == "100.1.2.3"
        assert len(list_nodes()) == 1

    def test_node_url(self, json_dir):
        from api.routing.node_registry import get_node_url
        (json_dir / "nodes.json").write_text(json.dumps({
            "nodes": {
                "test_node": {
                    "role": "inference",
                    "tailscale_ip": "10.0.0.1",
                    "services": {"inference": {"port": 5002}},
                    "env_override": {}
                }
            }
        }))
        assert get_node_url("test_node", service="inference") == "http://10.0.0.1:5002"
        assert get_node_url("test_node", service="inference", path="/v1/chat") == "http://10.0.0.1:5002/v1/chat"

    def test_preferred_ip_tailscale_over_lan(self, json_dir):
        from api.routing.node_registry import get_node
        (json_dir / "nodes.json").write_text(json.dumps({
            "nodes": {
                "nas": {
                    "role": "storage",
                    "tailscale_ip": "100.1.1.1",
                    "lan_ip": "192.168.1.3",
                    "env_override": {}
                }
            }
        }))
        node = get_node("nas")
        assert node.preferred_ip == "100.1.1.1"

    def test_env_override_ip(self, json_dir):
        from api.routing.node_registry import get_node_ip, reload
        (json_dir / "nodes.json").write_text(json.dumps({
            "nodes": {
                "mel": {
                    "role": "inference",
                    "tailscale_ip": "100.0.0.1",
                    "env_override": {"tailscale_ip": "MEL_IP"}
                }
            }
        }))
        with mock.patch.dict(os.environ, {"MEL_IP": "200.0.0.1"}):
            reload()
            assert get_node_ip("mel") == "200.0.0.1"

    def test_missing_node(self, json_dir):
        from api.routing.node_registry import get_node_url
        (json_dir / "nodes.json").write_text(json.dumps({"nodes": {}}))
        with pytest.raises(KeyError, match="Unknown node"):
            get_node_url("nonexistent")

    def test_nodes_by_role(self, json_dir):
        from api.routing.node_registry import get_nodes_by_role
        (json_dir / "nodes.json").write_text(json.dumps({
            "nodes": {
                "a": {"role": "inference", "tailscale_ip": "1.1.1.1", "env_override": {}},
                "b": {"role": "storage", "tailscale_ip": "2.2.2.2", "env_override": {}},
                "c": {"role": "inference", "tailscale_ip": "3.3.3.3", "env_override": {}},
            }
        }))
        infra = get_nodes_by_role("inference")
        assert len(infra) == 2
        assert {n.name for n in infra} == {"a", "c"}


# ===========================================================================
# Datastore Registry
# ===========================================================================

class TestDatastoreRegistry:

    def test_load_datastores(self, json_dir):
        from api.routing.datastore_registry import get_datastore, get_connection_params
        (json_dir / "datastores.json").write_text(json.dumps({
            "datastores": {
                "test_db": {
                    "description": "Test DB",
                    "driver": "mariadb",
                    "host": "db.local",
                    "port": 3306,
                    "database": "testdb",
                    "env_override": {}
                }
            }
        }))
        ds = get_datastore("test_db")
        assert ds is not None
        assert ds.host == "db.local"
        assert ds.port == 3306
        params = get_connection_params("test_db")
        assert params["host"] == "db.local"
        assert params["port"] == 3306
        assert params["database"] == "testdb"

    def test_env_override(self, json_dir):
        from api.routing.datastore_registry import get_connection_params, reload
        (json_dir / "datastores.json").write_text(json.dumps({
            "datastores": {
                "db1": {
                    "driver": "mariadb",
                    "host": "default.host",
                    "port": 3306,
                    "database": "mydb",
                    "env_override": {
                        "host": "DB1_HOST",
                        "port": "DB1_PORT"
                    }
                }
            }
        }))
        with mock.patch.dict(os.environ, {"DB1_HOST": "env.host", "DB1_PORT": "3307"}):
            reload()
            params = get_connection_params("db1")
            assert params["host"] == "env.host"
            assert params["port"] == 3307

    def test_missing_datastore_raises(self, json_dir):
        from api.routing.datastore_registry import get_connection_params
        (json_dir / "datastores.json").write_text(json.dumps({"datastores": {}}))
        with pytest.raises(KeyError, match="Unknown datastore"):
            get_connection_params("nonexistent")

    def test_list_datastores(self, json_dir):
        from api.routing.datastore_registry import list_datastores
        (json_dir / "datastores.json").write_text(json.dumps({
            "datastores": {
                "a": {"driver": "mariadb", "host": "h1", "port": 1, "database": "d1", "env_override": {}},
                "b": {"driver": "mariadb", "host": "h2", "port": 2, "database": "d2", "env_override": {}},
            }
        }))
        assert len(list_datastores()) == 2


# ===========================================================================
# Integration: Real JSON files from json/ directory
# ===========================================================================

class TestRealRegistryFiles:
    """Verify the actual json/*.json files load without error."""

    @pytest.fixture(autouse=True)
    def _patch_json_dir(self):
        """Point registries at this repo's json/ dir (handles worktrees)."""
        repo_json = Path(__file__).resolve().parents[1] / "json"
        from api.routing import service_registry, model_registry, node_registry, datastore_registry
        with mock.patch.object(service_registry, "get_json_dir", return_value=repo_json), \
             mock.patch.object(model_registry, "get_json_dir", return_value=repo_json), \
             mock.patch.object(node_registry, "get_json_dir", return_value=repo_json), \
             mock.patch.object(datastore_registry, "get_json_dir", return_value=repo_json):
            yield

    def test_services_json_loads(self):
        from api.routing.service_registry import list_services, reload
        reload()
        services = list_services()
        assert len(services) >= 1
        names = {s.name for s in services}
        assert "magi_server" in names
        assert "tools_api" in names

    def test_models_json_loads(self):
        from api.routing.model_registry import list_roles, get_role_model, reload
        reload()
        roles = list_roles()
        assert len(roles) >= 1
        model = get_role_model("text_primary")
        assert model  # non-empty

    def test_nodes_json_loads(self):
        from api.routing.node_registry import list_nodes, reload
        reload()
        nodes = list_nodes()
        assert len(nodes) >= 1
        names = {n.name for n in nodes}
        assert "melchior" in names

    def test_datastores_json_loads(self):
        from api.routing.datastore_registry import list_datastores, reload
        reload()
        stores = list_datastores()
        assert len(stores) >= 1
        names = {s.name for s in stores}
        assert "local_mariadb" in names
