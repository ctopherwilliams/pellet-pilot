"""Manual per-cook notes -- the cut, its weight, a corrected on-grill time
(sensor logging can start well after the meat actually went on, e.g. mid
setup), and a free-text verdict/notes. None of this is sensor data; it's
what you tell it after the fact, same spirit as naming a probe.

Persisted to .cook_notes.json, keyed by the cook session's own logged start
timestamp (see history.session_key()) -- that logged start can itself be
late (that's the whole reason --on-grill exists), but it's still a stable,
unique key per cook.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
NOTES_FILE = os.path.join(HERE, ".cook_notes.json")

# A notes file this size is already nonsensical; refuse to parse rather than
# hand a giant blob to json.load. Mirrors plan.py/probe_names.py's own caps.
_MAX_NOTES_FILE_BYTES = 256 * 1024

FIELDS = ("cut", "weight_lb", "on_grill", "verdict", "notes")


def load_notes(path=None):
    # `path=None` (not `path=NOTES_FILE`) so a monkeypatched NOTES_FILE
    # (e.g. in tests) is honored even by callers that don't pass `path`
    # explicitly -- a default bound at def time would freeze the *original*
    # value instead of looking it up per call.
    path = path or NOTES_FILE
    if not os.path.exists(path):
        return {}
    if os.path.getsize(path) > _MAX_NOTES_FILE_BYTES:
        raise ValueError(
            f"{path} is larger than expected ({_MAX_NOTES_FILE_BYTES} bytes) -- refusing to parse")
    with open(path) as f:
        return json.load(f)


def save_note(session_key, path=None, **fields):
    """Merge the given fields into the note for `session_key` and persist.
    Unknown fields are ignored; omitted/None fields leave any existing value
    alone, so you can add a --verdict later without re-typing the weight.
    """
    path = path or NOTES_FILE
    notes = load_notes(path)
    note = dict(notes.get(session_key, {}))
    for k, v in fields.items():
        if k in FIELDS and v is not None:
            note[k] = v
    notes[session_key] = note
    with open(path, "w") as f:
        json.dump(notes, f, indent=2)
    return note


def get_note(session_key, path=None):
    return load_notes(path).get(session_key)
