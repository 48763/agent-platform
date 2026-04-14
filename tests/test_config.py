import tempfile
import os
from pathlib import Path


def test_load_config_from_yaml(tmp_path):
    config_file = tmp_path / "config.yaml"
    config_file.write_text("hub:\n  host: '0.0.0.0'\n  port: 9000\n")

    from core.config import load_config
    cfg = load_config(str(config_file))
    assert cfg["hub"]["host"] == "0.0.0.0"
    assert cfg["hub"]["port"] == 9000


def test_load_agent_config(tmp_path):
    agent_dir = tmp_path / "myagent"
    agent_dir.mkdir()
    (agent_dir / "agent.yaml").write_text(
        "name: test-agent\ndescription: test\nroute_patterns:\n  - 'test'\n"
    )

    from core.config import load_agent_config
    cfg = load_agent_config(str(agent_dir))
    assert cfg["name"] == "test-agent"
    assert cfg["route_patterns"] == ["test"]
