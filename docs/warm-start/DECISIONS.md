# Warm start: decision log

Decisions from the grilling session of 2026-10-31 (operator: kriegcloud). Each entry gives the
question, the answer and the reason. [`DESIGN.md`](DESIGN.md) turns them into a build plan.

Baseline: fork `kriegcloud/TokenEconomizer` at `e428fc9` (upstream `microsoft/TokenEconomizer`).

## WS1 — Where the design lives

- **Question:** where do the design and decisions live?
- **Answer:** here, in `docs/warm-start/` in the fork.
- **Why:** it's an external Python tool, not beep-effect architecture. Keeping it here means it
  can be rebased onto upstream and offered upstream.

## WS2 — Upstream posture

- **Question:** a general feature, or one built only for this workstation?
- **Answer:** general and offerable upstream. The core is `te backfill`: it finds past session
  logs through the existing per-agent trace readers and folds them oldest-first. Extras specific
  to this workstation (finding sessions across many clones, mapping removed lanes, extra
  readers) go in separate commits, behind settings.
- **Why:** carrying a fork that has drifted from upstream costs more than designing for upstream
  from the start.

## WS3 — The paid proposer during backfill

- **Question:** how does backfill use the paid micro-skill proposer?
- **Answer:** replay all history through the free parts only: `envfacts`, tool-pruning evidence
  and the loop-guard envelope. Then run the paid proposer **once**, over a recent sample, with a
  hard cap (WS14).
- **Why:** replaying with the proposer every k=3 sessions would mean hundreds of side calls
  before any saving. The free parts carry most of what history can teach.

## WS4 — Phase-1 sources

- **Question:** which session histories does phase 1 read?
- **Answer:** `~/.claude/projects` and `~/.codex/sessions`. The first already covers `claude`,
  `claudex`, `claudeg` and `claudem` (Muse), because all of them are Claude Code. Phase 2 adds
  trace readers for Grok (`~/.grok/sessions`), Kimi (`~/.kimi`) and cursor-agent (`~/.cursor/chats`,
  `~/.local/share/cursor-agent`). ChatGPT desktop is out: it has no repo working directory and
  no tool trace.
- **Why:** readers already exist for Claude and Codex, so phase 1 is about finding sessions and
  feeding them in, not parsing new formats.

## WS5 — Measurement

- **Question:** backfill removes the cold-start baseline the paper measured against. How do we
  prove a saving?
- **Answer:** add an upstreamable `holdout` setting (default 0, trial 0.2). A deterministic hash
  of the session id decides which sessions run with every mechanism inert. The ledger still
  records them. Compare treated sessions with held-out ones, and also with pre-install history
  priced by TokenEconomizer's own cache-aware ledger.
- **Why:** a before/after comparison is confounded by changes in work mix and doctrine. A clone
  split is biased, because clones share learned state and work isn't randomly assigned to them.

## WS6 — Backfill window

- **Question:** how far back should backfill read?
- **Answer:** 30 days by default, oldest-first; `--since` overrides it.
- **Why:** doctrine moves fast (Graft retired 10-31, effect 4.1.0, model routing). Replaying
  oldest-first lets the fact lifecycle's later-contradicts-earlier rule retire stale facts.

## WS7 — Transcripts from removed lanes

- **Question:** a transcript whose folder no longer exists can't resolve a git remote. How is it
  attributed?
- **Answer:** walk up to the nearest existing ancestor folder (for example
  `beep-effect11/.claude/worktrees/x` → `beep-effect11`). For
  `beep-effect-worktrees/<lane>` folders, use the transcript's own `cwd`/`gitBranch` hints
  to find the owning clone. Use that checkout's remote, and log every inferred mapping.
  Transcripts that still can't be resolved are skipped and counted.
- **Why:** most lane history lives in removed worktrees. Skipping them would throw away most of
  the evidence.

## WS8 — Facts across agents

- **Question:** should repo facts be shared across Claude and Codex?
- **Answer:** pool micro-skill facts per repository, behind a setting. Tool-pruning evidence and
  the loop-guard envelope stay per agent, as upstream does.
- **Why:** facts such as "wrap bun in `zsh -ic`" belong to the repo. Tool sets and turn length
  belong to the agent.

## WS9 — Subagent sidechains

- **Question:** how are subagent and Workflow-child transcripts used?
- **Answer:** they feed `envfacts` only. They never count toward tool-pruning session thresholds
  or the loop-guard envelope. Main sessions feed every mechanism.
- **Why:** repeated failures inside a sidechain are real repo facts. But each sidechain has its
  own tool set, and counting it would inflate tool-pruning evidence.

## WS10 — Side-call model

- **Question:** which model do side calls use in Claude sessions?
- **Answer:** `claude-opus-5-5`. The side call clears the proxy environment it inherits
  (`ANTHROPIC_BASE_URL`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_DEFAULT_*_MODEL`), so a call made
  inside a `claudex`/`claudeg` session neither routes to `gpt-6-luna` nor hits the 401 from
  inherited host auth.
- **Why:** it matches the workstation's pin for Claude children and its no-aliases rule. In
  upstream terms, `paid_model` stays configurable and only its value changes.

## WS11 — Trigger

- **Question:** how is backfill started?
- **Answer:** with an explicit command,
  `te backfill [--since 30d] [--hosts claude,codex] [--apply]`, which defaults to a dry run.
  The dry run prints a census: sessions per clone, inferred mappings, sidechain counts and
  projected paid tokens. `--apply` folds under the existing fold lock while live sessions keep
  running.
- **Why:** a background run on first sight would be surprising and hard to review.

## WS12 — Tool-pruning mode for the trial

- **Answer:** start with `toolprune=propose`. Review the deny list backfill produces, run
  `toolprune apply`, then switch to `on` after a week if nothing needed was denied. Micro-skills
  runs as `envfacts` plus the one capped paid pass. The loop guard is on.

## WS13 — Gate for keeping it

- **Answer:** after at least 2 weeks and at least 50 held-out sessions, treated sessions' mean
  cache-aware cost per session must be at least 10% below the held-out sessions', the plugin's
  overhead included. Abandoned or failed turns must not rise, and there must be no loop-guard
  redirect the operator flags as wrong. Otherwise, uninstall, or keep only the mechanisms that
  cleared the bar on their own.

## WS14 — Paid cap

- **Answer:** 500k effective tokens for the one-time paid pass, over roughly the 60 most recent
  main sessions. The pass stops at the cap and records what it covered.

## WS15 — Who builds it

- **Answer:** one Codex `gpt-6.1-sol` medium lane in a worktree of the fork, working from
  `DESIGN.md`. Fable reviews it, runs `python -m pytest tests`, and commits.
- **Why:** it spares the Claude quota the tool is meant to protect.
