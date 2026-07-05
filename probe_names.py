"""Optional human labels for probes, e.g. "probe 1" -> "the pork butt".

Nobody thinks in probe numbers -- they know what meat is on which probe. This
lets spoken announcements and alarm messages say that instead of a bare index.

Label spec syntax  [PROBE:]NAME  (mirrors plan.py's stage spec syntax):
  "pork butt"      -> probe 1, "pork butt"
  "2:brisket"      -> probe 2, "brisket"

Persisted to .probe_names.json (same pattern as plan.py's .cook_plan.json) so
a label set once via --probe-name survives across --watch runs.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NAMES_FILE = os.path.join(HERE, ".probe_names.json")

# Mirrors plan.py's plan-file cap -- a config file this size is already
# nonsensical; refuse to parse rather than hand a giant blob to json.load.
_MAX_NAMES_FILE_BYTES = 64 * 1024


def parse_name(spec):
    """Return (probe:int, name:str), or None for a blank spec."""
    spec = spec.strip()
    if not spec:
        return None
    if ":" in spec:
        probe_s, name = spec.split(":", 1)
        probe_s, name = probe_s.strip(), name.strip()
        if probe_s.isdigit() and name:
            return (int(probe_s), name)
        return None
    return (1, spec)


def build_names(specs):
    """Iterable of label specs -> {probe:int -> name:str}."""
    names = {}
    for spec in specs:
        parsed = parse_name(spec)
        if parsed:
            probe, name = parsed
            names[probe] = name
    return names


def save_names(names, path=NAMES_FILE):
    with open(path, "w") as f:
        json.dump({str(p): n for p, n in names.items()}, f)


def load_names(path=NAMES_FILE):
    if not os.path.exists(path):
        return {}
    if os.path.getsize(path) > _MAX_NAMES_FILE_BYTES:
        raise ValueError(
            f"{path} is larger than expected ({_MAX_NAMES_FILE_BYTES} bytes) -- refusing to parse")
    with open(path) as f:
        raw = json.load(f)
    return {int(p): n for p, n in raw.items()}


def label(probe, names):
    """Spoken/printed noun phrase for a probe -- "the pork butt" if named,
    else the generic "probe N" fallback."""
    name = (names or {}).get(probe)
    return f"the {name}" if name else f"probe {probe}"
