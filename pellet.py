#!/usr/bin/env python3
"""Unified Pellet Pilot CLI -- one command, thin dispatch to the existing
tested scripts (poll.py/history.py/trend.py/plot.py/export.py). Adds nothing
of its own except --preset expansion; every flag below is documented in the
script it dispatches to.

Usage:
  pellet watch [--preset NAME] [...poll.py flags...]   # log + optionally --speak/--chart
  pellet history [...history.py flags...]              # browse past cooks
  pellet trend [...trend.py flags...]                  # rate-of-rise analysis
  pellet chart [...plot.py flags...]                   # render a cook chart
  pellet report [...report.py flags...]                # shareable self-contained Cook Report HTML
  pellet export [...export.py flags...]                # Grafana-ingestible export
  pellet presets                                        # list available --preset names

--preset expands to --stage/--probe-name specs (see presets/*.yaml) BEFORE
any --stage/--probe-name flags you also pass, so your own flags still apply
(plan.py/probe_names.py keep the last value per probe on conflict).
"""
import sys

import presets as presets_mod


def _expand_preset(argv):
    """Replace a `--preset NAME` pair with the --stage/--probe-name specs it
    stands for, prepended so any explicit flags you also typed take effect
    the same way as before (they're just appended after)."""
    if "--preset" not in argv:
        return argv
    i = argv.index("--preset")
    if i + 1 >= len(argv):
        sys.exit("--preset needs a name -- try `pellet presets` to list them")
    name = argv[i + 1]
    try:
        data = presets_mod.load_preset(name)
    except ValueError as e:
        sys.exit(str(e))
    extra = []
    for spec in data["stage_specs"]:
        extra += ["--stage", spec]
    for spec in data["name_specs"]:
        extra += ["--probe-name", spec]
    rest = argv[:i] + argv[i + 2:]
    print(f"Preset: {data['name']}")
    return extra + rest


def main():
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd, rest = argv[0], argv[1:]

    if cmd == "presets":
        names = presets_mod.list_presets()
        print("\n".join(names) if names else "No presets found.")
        return

    if cmd == "watch":
        import poll
        rest = _expand_preset(rest)
        if "--watch" not in rest:
            rest = rest + ["--watch", "30"]
        sys.argv = ["pellet watch"] + rest
        poll.main()
        return

    if cmd == "history":
        import history
        sys.argv = ["pellet history"] + rest
        history.main()
        return

    if cmd == "trend":
        import trend
        sys.argv = ["pellet trend"] + rest
        trend.main()
        return

    if cmd == "chart":
        import plot
        sys.argv = ["pellet chart"] + rest
        plot.main()
        return

    if cmd == "report":
        import report
        sys.argv = ["pellet report"] + rest
        report.main()
        return

    if cmd == "export":
        import export
        sys.argv = ["pellet export"] + rest
        export.main()
        return

    sys.exit(f"Unknown command {cmd!r}. Try: watch, history, trend, chart, report, export, presets, --help")


if __name__ == "__main__":
    main()
