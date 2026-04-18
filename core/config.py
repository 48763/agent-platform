import os
from pathlib import Path
import yaml


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_agent_config(agent_dir: str) -> dict:
    config_path = Path(agent_dir) / "agent.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    # Merge env vars into settings (env vars override yaml defaults)
    yaml_settings = config.get("settings", {})
    env_settings = {}
    for key, default in yaml_settings.items():
        env_key = key.upper()
        env_val = os.environ.get(env_key)
        if env_val is not None:
            # Type conversion based on yaml default
            if isinstance(default, int):
                env_settings[key] = int(env_val)
            elif isinstance(default, bool):
                env_settings[key] = env_val.lower() in ("true", "1", "yes")
            else:
                env_settings[key] = env_val
        else:
            env_settings[key] = default

    # Also check for settings not in yaml but in env (with SETTING_ prefix)
    for env_key, env_val in os.environ.items():
        lower_key = env_key.lower()
        if lower_key not in env_settings and lower_key not in (
            "hub_url", "agent_host", "agent_port", "data_dir",
            "path", "home", "user", "shell", "term",
        ):
            # Skip known non-setting env vars
            pass

    config["settings"] = env_settings
    return config
