# Repeatable user-local PM Swarm plugin pattern

Session learning: when extending Hermes behavior for this user's own orchestration workflow, do not patch Hermes source/worktrees as the first path. Source edits under `~/.hermes/hermes-agent` or repo worktrees can be clobbered by update and are not repeatable for the live profile.

Preferred durable shape:

- User-local plugin under `~/.hermes/plugins/<plugin-name>/`
- User-local class-level skill under `~/.hermes/skills/<category>/<skill-name>/`
- Enable with `hermes plugins enable <plugin-name>` rather than hand-editing config.
- Verify with `hermes plugins list --json` and a fresh CLI invocation of the plugin command.
- Tell the user if a running session needs restart/reset for model tool registry reload.

PM Swarm specifics:

- Plugin path: `~/.hermes/plugins/pm-swarm/`
- Skill path: `~/.hermes/skills/devops/pm-swarm/SKILL.md`
- Enable command: `hermes plugins enable pm-swarm`
- Verification command: `hermes pm-swarm --help`

Pitfall:

Directly writing a `plugins.enabled` section into config may not match Hermes' current enablement semantics or may be rewritten by config tooling. Prefer the CLI enable command so the same key that `PluginManager` matches is written.
