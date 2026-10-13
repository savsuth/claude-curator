---
name: changelog-writer
description: Adds entries to this repository's CHANGELOG.md in the team's Keep a Changelog format and validates them. Use when asked to record, log, or write up a change in the changelog or release notes.
---

# Changelog writer

Record user-visible changes in `CHANGELOG.md` at the repository root.

## Format rules

- New entries go under `## [Unreleased]`, never under a released version.
- Group entries under exactly these headings, in this order, omitting empty ones:
  `### Added`, `### Changed`, `### Deprecated`, `### Removed`, `### Fixed`, `### Security`.
- One line per change, starting with a past-tense verb, ending with the pull request in parentheses, for example `(#123)`.
- Security fixes go under `### Security`, even when they are also bug fixes.

## Worked examples

A bug fix in the parser, pull request 412:

```markdown
## [Unreleased]

### Fixed

- Fixed a crash when the parser met an empty input file (#412)
```

A new flag and a removed endpoint in one change, pull request 87:

```markdown
## [Unreleased]

### Added

- Added the `--export` flag to the `report` command (#87)

### Removed

- Removed the deprecated `/v1/legacy` endpoint (#87)
```

## Validation

Before finishing, run the bundled validator from this skill's directory:

```bash
python3 scripts/check_changelog.py CHANGELOG.md
```

Fix every problem it reports and run it again until it prints `OK`.

## Style

Tone and wording rules are in [the house style](references/house-style.md).
