## Summary

Describe the user-visible outcome and why it is needed.

## Verification

- [ ] `uv run ruff check .`
- [ ] `uv run pytest --cov --cov-report=term-missing`
- [ ] Tests do not touch a live Herdr session
- [ ] Parent panes cannot be closed by the change
- [ ] Non-Herdr environments remain strict no-ops
- [ ] Documentation and changelog are updated when needed

## Screenshots or traces

Include sanitized evidence when it helps. Never attach private rollout content.
