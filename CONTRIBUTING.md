# Contributing

Thank you for helping improve Herdr Codex Subagents.

## Before opening a change

- Search existing issues and pull requests.
- Keep behavior outside Herdr as a strict no-op.
- Preserve the parent-pane safety invariant.
- Never add telemetry or transmit rollout content.
- Discuss large behavior or compatibility changes in an issue first.

## Local setup

```bash
git clone https://github.com/arvkonstantin/herdr-codex-subagents.git
cd herdr-codex-subagents
uv sync --extra dev
uv run pytest --cov --cov-report=term-missing
uv run ruff check .
```

Tests must not operate on a developer's live Herdr session. Use the fake client in the test suite or
an explicitly named disposable Herdr session.

## Pull requests

1. Add tests for behavior changes.
2. Keep coverage at or above 90%.
3. Update the README and changelog when user-visible behavior changes.
4. Use focused commits with clear imperative subjects.
5. Complete the pull request checklist.

By contributing, you agree that your contribution is licensed under the MIT License.
