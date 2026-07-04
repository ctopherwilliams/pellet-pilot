<!-- Thanks for contributing to Pellet Pilot! -->

## What & why
<!-- What does this change and why? Link any related issue: Closes #123 -->

## Type
- [ ] Bug fix
- [ ] New feature
- [ ] Docs / chore
- [ ] Security

## Checklist
- [ ] `python tests/smoke.py` passes locally
- [ ] `bandit -c bandit.yaml -r . -ll` and `pip-audit -r requirements.txt` pass locally
- [ ] No secrets, tokens, `.env`, `.bw_session`, or device thing-names in the diff
- [ ] Read-only design preserved (no grill control beyond `command:"90"`)
- [ ] Docs updated if behavior changed

<!-- First-time / external contributor? An automated AI review will post one advisory
     comment here before a maintainer looks -- it's informational only, not a gate. -->
