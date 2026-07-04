<!-- Thanks for contributing to Pellet Pilot! -->

## What & why
<!-- What does this change and why? Link any related issue: Closes #123 -->

## Type
- [ ] Bug fix
- [ ] New feature
- [ ] Docs / chore
- [ ] Security

## Checklist
- [ ] `bandit -c bandit.yaml -r *.py -ll` and `pip-audit -r requirements.txt` pass locally
- [ ] `python -c "import traeger_client, poll, trend"` succeeds
- [ ] No secrets, tokens, `.env`, `.bw_session`, or device thing-names in the diff
- [ ] Read-only design preserved (no grill control beyond `command:"90"`)
- [ ] Docs updated if behavior changed
