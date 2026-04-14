from pathlib import Path
import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_agent_config(agent_dir: str) -> dict:
    config_path = Path(agent_dir) / "agent.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)
