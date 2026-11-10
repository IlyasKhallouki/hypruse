# hypruse for Hermes

A computer-use provider for [Hermes Agent](https://github.com/NousResearch/hermes-agent)
that drives Hyprland through hypruse. Its documentation (requirements,
install, configuration, how each action maps, refusals and limits) is in
[hermes_hypruse/README.md](hermes_hypruse/README.md), the page Hermes' plugin
catalog shows.

This folder also holds the Python package that links the provider into
`$HERMES_HOME/plugins/` (`hermes-hypruse install`) and the provider's tests.

## Tests

The unit tests use a fake `hypruse` on a fake desktop and need only pytest:

```sh
cd integrations/hermes
uv run --isolated --no-project --with pytest python -m pytest tests
```

With a hermes-agent checkout, the same tests run against Hermes' real
provider contract, and `test_hermes_contract.py` drives the provider through
Hermes' own `computer_use` tool: discovery, approvals, dispatch, response
shaping and `capture_after`:

```sh
HERMES_AGENT_SRC=/path/to/hermes-agent uv run --isolated --frozen \
  --directory /path/to/hermes-agent --with pytest \
  python -m pytest /path/to/hypruse/integrations/hermes/tests
hermes plugins validate /path/to/hypruse/integrations/hermes/hermes_hypruse
```

`hermes plugins validate` runs the checks Hermes' plugin catalog runs at
admission, and this provider passes them.
