# Releasing

This repository uses Release Please to create semantic-versioned releases from Conventional Commits.

## Versioning

Versions follow Semantic Versioning:

- `fix:` commits produce patch releases, for example `1.0.1`.
- `feat:` commits produce minor releases, for example `1.1.0`.
- commits with `!` or a `BREAKING CHANGE:` footer produce major releases, for example `2.0.0`.

Examples:

```text
fix(task-end): handle missing docs directory
feat: add pm-swarm workflow skill
feat!: rename workflow skill identifiers
```

## Release Flow

1. Merge Conventional Commit PRs into `main`.
2. The `Release Please` workflow opens or updates a release PR.
3. Review the generated changelog and version bumps.
4. Merge the release PR.
5. Release Please creates the GitHub release and tag.

The release configuration updates:

- `package.json`
- `.claude-plugin/plugin.json`
- `CHANGELOG.md`
- `.release-please-manifest.json`

The marketplace metadata currently keeps its plugin version static unless updated manually or by a future release config enhancement.
