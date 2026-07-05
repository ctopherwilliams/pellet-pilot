"""Cook presets -- named YAML files bundling the --stage/--probe-name specs
for a cut of meat, so `pellet watch --preset brisket` replaces typing out
`--stage 165:wrap --stage 203:done --probe-name brisket` every cook.

A preset is intentionally just a bag of the SAME spec strings plan.py /
probe_names.py already parse (stage_specs, name_specs) -- no separate schema
or parsing logic to keep in sync with the real thing.

Preset file (presets/brisket.yaml):
  name: Brisket
  stage_specs:
    - "165:wrap"
    - "203:done"
  name_specs:
    - "brisket"
"""
import os

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
PRESETS_DIR = os.path.join(HERE, "presets")

# A preset is a small, trusted, repo-shipped config file -- this cap just
# guards against a corrupt/malicious file being handed to yaml.safe_load.
_MAX_PRESET_FILE_BYTES = 64 * 1024


def list_presets(presets_dir=PRESETS_DIR):
    if not os.path.isdir(presets_dir):
        return []
    return sorted(f[:-5] for f in os.listdir(presets_dir) if f.endswith(".yaml"))


def load_preset(name, presets_dir=PRESETS_DIR):
    """Load a preset by name (no extension, no path separators). Returns a
    dict with 'stage_specs' and 'name_specs' lists (either may be empty).
    """
    if not name or os.sep in name or (os.altsep and os.altsep in name) or name in (".", ".."):
        raise ValueError(f"Invalid preset name: {name!r}")
    path = os.path.join(presets_dir, f"{name}.yaml")
    if not os.path.exists(path):
        available = ", ".join(list_presets(presets_dir)) or "none"
        raise ValueError(f"Unknown preset {name!r}. Available: {available}")
    if os.path.getsize(path) > _MAX_PRESET_FILE_BYTES:
        raise ValueError(
            f"{path} is larger than expected ({_MAX_PRESET_FILE_BYTES} bytes) -- refusing to parse")
    with open(path) as f:
        data = yaml.safe_load(f) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must be a YAML mapping, got {type(data).__name__}")
    return {
        "name": data.get("name", name),
        "stage_specs": [str(s) for s in (data.get("stage_specs") or [])],
        "name_specs": [str(s) for s in (data.get("name_specs") or [])],
    }
