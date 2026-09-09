# Changelog

All notable changes to this project are documented in this file. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and releases follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.3] - 2026-09-09

### Fixed

- Keep subagent panes open for the active task when Codex transcripts contain
  inherited completed tasks.

## [0.1.2] - 2026-09-09

### Added

- Rich plugin catalog metadata and a dedicated plugin icon.
- HOL Plugin Scanner enforcement for pull requests and the default branch.

### Changed

- Restructure the repository as an installable root-level plugin and package the
  correct plugin contents in release archives.

## [0.1.1] - 2026-09-09

### Added

- Balanced, alternating pane layout that keeps the parent in the left half.

### Fixed

- Use Codex's three-second maximum timeout for the `SessionEnd` hook.

## [0.1.0] - 2026-09-09

### Added

- Automatic right-hand Herdr panes for Codex subagents.
- Exact `agent_id` to `pane_id` lifecycle tracking.
- Non-focusing, read-only live rollout viewer.
- Concurrent lifecycle serialization and stale-session cleanup.
- Strict no-op behavior outside Herdr.
- Python 3.10+ test matrix with branch coverage enforcement.

[Unreleased]: https://github.com/arvkonstantin/herdr-codex-subagents/compare/v0.1.3...HEAD
[0.1.3]: https://github.com/arvkonstantin/herdr-codex-subagents/compare/v0.1.2...v0.1.3
[0.1.2]: https://github.com/arvkonstantin/herdr-codex-subagents/compare/v0.1.1...v0.1.2
[0.1.1]: https://github.com/arvkonstantin/herdr-codex-subagents/compare/v0.1.0...v0.1.1
[0.1.0]: https://github.com/arvkonstantin/herdr-codex-subagents/releases/tag/v0.1.0
