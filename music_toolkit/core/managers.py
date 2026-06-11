# music_toolkit/core/managers.py

import json
import os

class AppManager:
    """Manages global paths and configurations for the toolkit."""
    def __init__(self, base_dir=None):
        # Find the project's root directory (where Music-Toolkit.py is)
        if base_dir:
            self.project_root = base_dir
        else:
            # This navigates up from core -> music_toolkit -> project_root
            self.project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

        self.config_dir = os.path.join(self.project_root, 'config')
        self.temp_dir = os.path.join(self.project_root, 'temp')
        self._ensure_dirs_exist()

    def _ensure_dirs_exist(self):
        """Creates the config and temp directories if they don't exist."""
        os.makedirs(self.config_dir, exist_ok=True)
        os.makedirs(self.temp_dir, exist_ok=True)

    def load_config(self, tool_name: str, defaults: dict) -> dict:
        """Loads a tool's config from a JSON file."""
        config_path = os.path.join(self.config_dir, f"{tool_name}.json")

        if not os.path.exists(config_path):
            print(f"Config for '{tool_name}' not found. Creating with defaults.")
            self.save_config(tool_name, defaults)
            return defaults

        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            print(f"Error reading config for '{tool_name}': {e}. Using defaults.")
            return defaults

    def save_config(self, tool_name: str, settings_data: dict):
        """Saves a tool's settings dictionary to its JSON file."""
        config_path = os.path.join(self.config_dir, f"{tool_name}.json")
        try:
            with open(config_path, 'w', encoding='utf-8') as f:
                json.dump(settings_data, f, indent=4)
        except IOError as e:
            print(f"Error saving config for '{tool_name}': {e}")

    def get_temp_dir(self, tool_name: str) -> str:
        """Gets the path to a tool's dedicated temp directory, creating it if needed."""
        tool_temp_dir = os.path.join(self.temp_dir, tool_name)
        os.makedirs(tool_temp_dir, exist_ok=True)
        return tool_temp_dir
