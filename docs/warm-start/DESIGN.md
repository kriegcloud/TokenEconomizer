# Warm start: design

The plugin learns only from sessions it saw live, so every repository starts cold. The paper's
cost curve only fell below default after about six tasks. Agents already keep weeks of session
logs on disk. Warm start replays those logs through the mechanisms before the plugin first
acts, so they start from experience instead of from nothing. Every decision cited here (WS*) is
in [`DECISIONS.md`](DECISIONS.md).

## What exists

- `plugin/fold.py`: `fold_session(ctx, session_id, transcript_path)` runs `PrefixStage`
  (micro-skills) and `ToolingStage` (tool pruning) over one finished session. A per-session
  fold lock and `folded.json` make each fold happen once. `pending_sessions()` finds only
  sessions the plugin itself pinned under `<state>/sessions/`.
- `trace/`: one reader per agent (`claude`, `codex`, `copilot`, `opencode`, `pi`), plus
  `call_cwds` and `tool_usage`.
- `plugin/repos.py` and `plugin/paths.py`: a repository's identity is its normalized `origin`
  remote (`BY_REMOTE`), falling back to its path (`BY_PATH`). Every clone of one remote shares
  one state directory.
- The loop-guard envelope is filled from Stop hooks, not from logs.

## Changes

### 1. `te backfill` (upstreamable core; WS2, WS11)

```
te backfill [--since 30d] [--hosts claude,codex] [--repo <path>] [--apply] [--json]
```

1. **Discover.** For each host, a new `trace.<host>.discover(since)` yields
   `(session_id, transcript_path, cwd, started, is_sidechain)`:
   - claude: `~/.claude/projects/*/*.jsonl` for main sessions. Subagent files are sidechains:
     `*/subagents/*.jsonl`, or records with `isSidechain: true`. `cwd` comes from the first
     record that has one.
   - codex: `~/.codex/sessions/YYYY/MM/DD/*.jsonl`, using `cwd` from `session_meta`.
2. **Attribute** each `cwd` to a `RepoId` through `paths.repo_identity`. If the folder is gone,
   use the dead-lane resolver (section 3).
3. **Filter** to the target repository (with `--repo`, otherwise every repository found), to
   the window (WS6), and to sessions not yet in `folded.json`.
4. **Dry run** (the default) prints the census: sessions and sidechains per host and per
   checkout, inferred mappings, sessions skipped and why, and the projected paid tokens for the
   paid pass. No state is written.
5. **`--apply`** pins each session the way a live SessionStart would. It then calls
   `fold_session` oldest-first under the fold lock, with the paid proposer **disabled** (WS3).
   Micro-skills runs as `envfacts`; tool pruning accrues evidence. Afterwards it runs the paid
   pass (section 5) and records a `backfill` entry in the ledger: counts, window, tokens.
6. **Loop-guard envelope.** It derives the per-turn length samples from main sessions' logs,
   using the same "ended well" rule the Stop hook applies (no failing check after the last
   edit). Samples are tagged `source=backfill` so they can be told apart.

Idempotent: a second run folds only what is new.

### 2. Sidechains (WS9)

`fold_session(..., role="sidechain")` runs only the `envfacts` observer. It doesn't touch
`untool.json`, doesn't advance tool-pruning session or prompt counts, and contributes no
envelope samples.

### 3. Dead-lane resolver (workstation commits; WS7)

Behind the setting `backfill_resolve_dead=on` (default off upstream):

1. Walk up from `cwd` to the nearest existing ancestor that is a git checkout, stopping at
   `$HOME`.
2. For `<root>-worktrees/<lane>` folders whose sibling `<root>` checkouts all share one
   remote, use that remote.
3. Record each inference in `<state>/backfill-mappings.jsonl`. Anything still unresolved is
   counted and skipped.

### 4. Facts shared across agents (WS8)

Behind the setting `microskills_scope=repo` (upstream default `host`), micro-skill state
(`injection.json` and the distiller's state) moves from `repos/<key>/<host>/` to
`repos/<key>/_shared/`. `untool.json` and the envelope stay per agent. A migration copies
existing per-agent facts in, keeping the most-confirmed copy of any duplicate.

### 5. One capped paid pass (WS3, WS14)

`te backfill --apply` ends with one proposer run over recent main-session digests, selected
newest-first until the next digest would exceed `backfill_paid_cap` (default 500000; 0 skips).
The cap bounds the paid pass itself, in the paid model's effective tokens: the serialized
prompt at one token per three characters plus ~20k tokens of the CLI's own context, both priced
as cache writes, plus a 32k output reserve priced as output. The Claude call's output (thinking
included) is capped at that reserve with `CLAUDE_CODE_MAX_OUTPUT_TOKENS`. Calibration: the first
real pass estimated 153k under the old chars/4 rule and reported 461k. Original sessions' effective
usage does not consume this budget. `projected_paid_tokens` reports the estimate; actual
optimizer overhead remains separate. The complete prepared prompt is checked again before
calling, including learned facts and features. This estimate is not a provider billing guarantee.
History proposals have no digest-count bound; the live eight-digest window does not truncate them.

Choose `--repo` when supplied, else cwd's git repository when resolvable, else the fact stream
with the most candidate main sessions in the window. Within a repository, choose the busiest
fact stream (hosts pool only with repo scope). Ties favor newest session, then stream path.
A resolved cwd repository without candidates gets no paid call. Free replay still covers all
candidate repositories. Facts proposed still need the usual 3 confirmations; backfilled
sessions confirm as usual.

### 6. Session holdout (upstreamable; WS5)

The setting `holdout` (a float in [0, 1), default 0). At SessionStart a session is held out
when `int(sha256(session_id)[:8], 16) / 2**32 < holdout`. Nothing is injected into a
held-out session, no deny list is applied to it, and the loop guard doesn't act in it. It is
still folded and recorded in the ledger with `arm=holdout`. `te status --arms` reports mean
cost per session, turns and redirects per arm.

**Caveat:** on Claude Code, a deny list lives in `.claude/settings.local.json` and applies to
every session in the checkout. A held-out session can't un-deny tools. For the tool-pruning
comparison, a held-out session restores the list for its own duration only if the agent
supports that. Otherwise tool pruning is measured as a before/after delta, and `holdout` covers
micro-skills and the loop guard. Document which applied in `te status --arms`.

### 7. Side-call pin and env scrub (workstation commits; WS10)

`claude_call` removes `ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN` and
`ANTHROPIC_DEFAULT_{HAIKU,SONNET,OPUS}_MODEL` from the child environment when
`sidecall_scrub_env=on`. The trial config sets `paid_model=claude-opus-5-5`.

## Phase 2 (WS4)

Trace readers plus `discover()` for Grok (`~/.grok/sessions/<url-encoded cwd>/`), Kimi
(`~/.kimi`) and cursor-agent (`~/.cursor/chats`, `~/.local/share/cursor-agent`). These agents
have no hook plugin, so they feed backfill only: facts (shared under WS8) and nothing else.

## Trial runbook (WS12, WS13)

1. Install the plugin from this fork. Claude Code is 2.1.316 and Codex 0.166.0, both newer than
   the tested range, so run `te doctor` first.
2. Set the repo config: `toolprune=propose`, `microskills=envfacts`, `loopguard=on`,
   `holdout=0.2`, `paid_model=claude-opus-5-5`, `sidecall_scrub_env=on`,
   `microskills_scope=repo`, `backfill_resolve_dead=on`.
3. Run `te backfill`, review the census, then run `te backfill --apply`.
4. Review `te microskills` and `te toolprune`; run `toolprune apply` if the list looks sane.
   After a week with nothing wrongly denied, switch to `toolprune=on`.
5. Check the gate (WS13) after 2 weeks and at least 50 held-out sessions.

## Tests

Use offline fixtures under `tests/`, shaped like the real logs:

- discovery: main and sidechain Claude files, Codex `session_meta`, the window filter;
- dead-lane mapping;
- idempotent re-runs;
- the sidechain fold leaves tool-pruning counts unchanged;
- the paid pass stops at the cap;
- holdout arms are deterministic;
- the scrubbed side-call environment.

`tests/test_plugin_package.py`'s boundary rules still hold.
