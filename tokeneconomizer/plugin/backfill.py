"""Explicit, reviewable replay of historical sessions; discovery never creates state.

The census and the apply path use the same plan. Replay uses the existing fold lock and only
free observers. The final paid pass budgets estimated proposer prompt/output tokens,
independently of source-session usage and actual recorded side-call overhead.
"""
import os
import re
import tempfile
import time
from dataclasses import replace

from .. import loopguard
from ..trace import claude, codex
from . import accounting, facts, fold, hosts, paths, repos, sidecall
from .config import load_config
from .context import LiveContext
from .store import Store

READERS = {"claude": claude, "codex": codex}


def cutoff(window, now=None):
    if not re.fullmatch(r"\d+(?:\.\d+)?[dhw]", window):
        raise ValueError("--since must be a duration such as 30d, 12h or 2w")
    return (time.time() if now is None else now) - float(window[:-1]) * {
        "d": 86400, "h": 3600, "w": 604800}[window[-1]]


def _context(host, repo, env):
    root = paths.state_root(env)
    base = paths.repo_state_dir(repo, host, env)
    return LiveContext(hosts.HOSTS[host](), repo, root, Store(base),
                       load_config(root, os.path.dirname(base), env), dict(env))


def census(since, host_names, repo_path=None, env=None, include_folded=False):
    """Read-only plan: candidate rows include contexts privately; public rows are JSON-safe.
    `include_folded` keeps already-folded sessions as candidates (a paid retry samples them;
    the fold loop still skips them)."""
    env = dict(os.environ if env is None else env)
    root = paths.state_root(env)
    target_repo = paths.repo_identity(repo_path, env) if repo_path else None
    target = target_repo.key if target_repo else None
    cfg = load_config(root, os.path.dirname(paths.repo_state_dir(
        target_repo, "claude", env)) if target_repo else None, env)
    rows, skipped, mappings, seen = [], [], [], set()
    for host in host_names:
        history_root = os.path.join(paths.home_dir(env), ".claude/projects" if host == "claude"
                                    else ".codex/sessions")
        for sid, transcript, cwd, started, side in READERS[host].discover(since, root=history_root):
            reason, inference = None, None
            if not re.fullmatch(r"[A-Za-z0-9._-]{1,200}", sid):
                reason = "invalid session id"
            elif (host, sid) in seen:
                reason = "duplicate session id"
            seen.add((host, sid))
            repo = paths.repo_identity(cwd, env) if cwd and os.path.isdir(cwd) else None
            if repo is None:
                resolved = repos.resolve_dead(cwd, env)
                if resolved:
                    candidate, mapping = resolved
                    resolver_cfg = load_config(root, os.path.dirname(paths.repo_state_dir(
                        candidate, host, env)), env)
                    if cfg.backfill_resolve_dead or resolver_cfg.backfill_resolve_dead:
                        repo, inference = candidate, mapping
            if repo is None:
                reason = reason or "unresolved cwd"
            elif target and repo.key != target:
                reason = reason or "other repository"
            ctx = _context(host, repo, env) if repo else None
            if ctx and not include_folded and sid in (ctx.store.read("folded.json") or {}):
                reason = reason or "already folded"
            if started > time.time():
                reason = reason or "future timestamp"
            # A log being written may still belong to a running session. Do not pin it as over.
            try:
                if time.time() - os.path.getmtime(transcript) < fold.STALE_S:
                    reason = reason or "transcript still active"
            except OSError:
                reason = reason or "transcript disappeared"
            row = {"session_id": sid, "host": host, "transcript": transcript, "cwd": cwd,
                   "started": started, "role": "sidechain" if side else "main",
                   "repo_key": repo.key if repo else None}
            if inference:
                mappings.append(inference)
            if reason:
                skipped.append(dict(row, reason=reason))
                continue
            usage = (claude.claude_session_usage(transcript, include_subagents=False)
                     if host == "claude" else codex.codex_session_usage(transcript))
            eff, _ = accounting.effective(usage)
            if host == "claude" and not usage.get("requests"):
                eff = None
            rows.append(dict(row, effective_tokens=eff, _ctx=ctx, _mapping=inference))
    return sorted(rows, key=lambda r: (r["started"], r["host"], r["session_id"])), skipped, mappings


# A planning allowance, not measured output or a provider billing guarantee.
PAID_OUTPUT_RESERVE = 4096


def paid_stream(rows, repo_path=None, env=None):
    """Prefer explicit repo, then cwd's git repo, else the busiest main fact stream."""
    main = [r for r in rows if r["role"] == "main"]
    repo = paths.repo_identity(repo_path, env) if repo_path else None
    if repo is None and paths.find_repo_root(os.getcwd(), env):
        repo = paths.repo_identity(os.getcwd(), env)
    if repo is not None:
        main = [r for r in main if r["repo_key"] == repo.key]
    groups = {}
    for row in main:
        groups.setdefault(facts.store_for(row["_ctx"]).base, []).append(row)
    # Stable tie break: busiest, then newest, then stream path.
    return max(groups.values(), key=lambda g: (len(g), max(r["started"] for r in g),
               facts.store_for(g[0]["_ctx"]).base), default=[])


def _generic(row):
    from .adapter import LiveAdapter
    ctx = row["_ctx"]
    return bool(getattr(LiveAdapter(ctx.host, ctx.repo.root,
                     vocabulary=ctx.config.vocabulary).env_vocabulary(), "generic", False))


def _digest(row):
    from ..microskills.distiller import build_digest
    if "_digest" not in row:
        ctx = row["_ctx"]
        with tempfile.TemporaryDirectory(prefix="te-backfill-plan-") as work:
            trace = ctx.host.mechanism_trace(row["transcript"], work)
            row["_digest"] = (build_digest(ctx.repo.label, trace, row["session_id"],
                              None, generic=_generic(row)) if trace else None)
    return row["_digest"]


def paid_sample(rows, cap):
    """Budget the serialized proposer input (chars/4) plus one output reserve."""
    from ..microskills.distiller import build_proposer_prompt
    selected, digests, tokens = [], [], 0
    for row in sorted(rows, key=lambda r: r["started"], reverse=True):
        if row["role"] != "main":
            continue
        dg = _digest(row)
        if dg is None:
            continue
        prompt = build_proposer_prompt(row["_ctx"].repo.label, digests + [dg],
                                       {}, set(), [], generic=_generic(row))
        estimate = (len(prompt) + 3) // 4 + PAID_OUTPUT_RESERVE
        if estimate > cap:
            break
        selected.append(row)
        digests.append(dg)
        tokens = estimate
    return selected, tokens


def record_envelope(ctx, sid, transcript):
    """Use the live guard's outcome rule without running its decision ladder or judge."""
    samples, turns = [], 0
    for index, (calls, ended) in enumerate(READERS[ctx.host.name].turns(transcript)):
        turns += 1
        guard = loopguard.TurnGuard()
        for name, args, text, ok in calls:
            guard.observe(name, args, text, None if ctx.host.name == "claude" and ok else int(not ok))
        if guard.resolved_proxy(ended):
            samples.append({"session_id": sid, "turn": index, "source": "backfill",
                            "length": guard.cum_turns, "snapshot": guard.whole.snapshot()})
    with ctx.store.locked("envelope", timeout=fold.FOLD_LOCK_WAIT_S) as got:
        if not got:
            raise RuntimeError("backfill envelope busy")
        tracker = loopguard.load_envelope(ctx.store, ctx.config.loopguard_aggr,
                                         ctx.config.loopguard_thresholds)
        seen = {(s.get("session_id"), s.get("turn")) for s in tracker.samples}
        for sample in samples:
            if (sid, sample["turn"]) not in seen:
                tracker.update(sample["length"], True, sample["snapshot"])
                tracker.samples.append(sample)
        loopguard.envelope.save_envelope(ctx.store, tracker)
    return turns


def _paid_pass(rows, cap):
    """One call on the selected fact stream; samples must never mix repository facts."""
    from ..microskills.distiller import Distiller
    from .adapter import LiveAdapter
    groups = {}
    for row in rows:
        ctx = row["_ctx"]
        if sidecall.paused(ctx.host.name, ctx.root):
            continue
        groups.setdefault(facts.store_for(ctx).base, []).append(row)
    out = []
    for group in list(groups.values())[:1]:
        ctx = group[0]["_ctx"]
        dst = facts.store_for(ctx)
        with dst.locked("mechanisms", timeout=fold.FOLD_LOCK_WAIT_S) as got:
            if not got:
                raise RuntimeError("backfill paid state busy")
            facts.migrate(ctx, dst)
            adapter = LiveAdapter(ctx.host, ctx.repo.root, vocabulary=ctx.config.vocabulary)
            from ..microskills.envfacts import EnvFactsDistiller
            env = EnvFactsDistiller.load(ctx.repo.label, dst.ensure("mechanisms"),
                                         vocab=adapter.env_vocabulary())
            runner = sidecall.runner_for(ctx.host.name, ctx.config)
            def budgeted_runner(prompt, model):
                if (len(prompt) + 3) // 4 + PAID_OUTPUT_RESERVE > cap:
                    raise ValueError("prepared proposer prompt exceeds paid cap")
                return runner(prompt, model)
            dist = Distiller.load(ctx.repo.label, dst.ensure("mechanisms"),
                                  runner=budgeted_runner,
                                  external_facts=env.injectable_claims,
                                  vocab=adapter.env_vocabulary())
            before = dist.opt_tokens
            error = None
            with tempfile.TemporaryDirectory(prefix="te-backfill-") as work:
                sessions = []
                for i, row in enumerate(group):
                    directory = os.path.join(work, str(i))
                    os.makedirs(directory)
                    trace = row["_ctx"].host.mechanism_trace(row["transcript"], directory)
                    if trace:
                        sessions.append((row["session_id"], trace))
                try:
                    dist.propose_history(sessions)
                except Exception as e:
                    error = f"{type(e).__name__}: {e}"
            dist.save(dst.ensure("mechanisms"))
            if not error:
                dst.write("backfill-proposer.json", {"at": time.time()})
            facts.render(ctx, dst, profile="envfacts+distiller")
            record = {"host": ctx.host.name, "repo_key": ctx.repo.key,
                      "sessions": [sid for sid, _ in sessions],
                      "estimated_proposer_tokens": paid_sample(group, cap)[1],
                      "optimizer_tokens": dist.opt_tokens - before, "error": error}
            out.append(record)
    return out


def run(since="30d", host_names=("claude", "codex"), repo_path=None, apply=False, env=None,
        retry_paid=False):
    """Plan first, then pin/fold only new sessions. A no-op rerun makes no paid call, unless
    `retry_paid` asks again for a stream whose paid pass never succeeded (no
    `backfill-proposer.json`): its already-folded sessions are then the sample."""
    start = cutoff(since)
    if not host_names or any(h not in READERS for h in host_names):
        raise ValueError("--hosts accepts claude,codex")
    rows, skipped, mappings = census(start, host_names, repo_path, env,
                                     include_folded=retry_paid)
    cap = min((r["_ctx"].config.backfill_paid_cap for r in rows), default=0)
    stream = paid_stream(rows, repo_path, env)
    sample, projected = paid_sample(stream, cap) if cap else ([], 0)
    counts = {}
    for row in rows:
        key = (row["host"], row["cwd"])
        count = counts.setdefault(key, {"host": row["host"], "cwd": row["cwd"],
                                        "sessions": 0, "sidechains": 0})
        count["sidechains" if row["role"] == "sidechain" else "sessions"] += 1
    out = {"apply": apply, "since": since, "cutoff": start, "census": list(counts.values()),
           "candidates": len(rows), "skipped": skipped, "mappings": mappings,
           "projected_paid_tokens": projected, "paid_cap": cap, "folded": 0, "paid": [],
           "paid_sample": [{k: r[k] for k in ("session_id", "host", "repo_key",
                                               "effective_tokens")} for r in sample],
           "paid_output_reserve": PAID_OUTPUT_RESERVE,
           "paid_selection_note": "At most one call: --repo, else cwd repository, else busiest "
                                  "main-session fact stream (busiest host within a repository). "
                                  "Estimated proposer tokens = prompt chars/4 (instructions once) "
                                  "+ 4096 output reserve; source usage is "
                                  "not budgeted. No history digest-count bound; live window is 8. "
                                  "Actual optimizer overhead is recorded separately."}
    if not apply or not rows:
        return out
    completed = []
    # Serialize command replays as well as each individual fold; a live fold still uses its
    # own existing lock. A concurrent backfill must recheck folded state before pinning.
    root = rows[0]["_ctx"].root
    with Store(root).locked("backfill", timeout=fold.FOLD_LOCK_WAIT_S) as got:
        if not got:
            raise RuntimeError("another backfill is running")
        for row in rows:
            ctx, sid = row["_ctx"], row["session_id"]
            if sid in (ctx.store.read("folded.json") or {}):
                continue
            # Replay only gathers evidence; it never schedules writes to checkout settings.
            cfg = replace(ctx.config, toolprune="propose" if ctx.config.toolprune == "auto"
                          else ctx.config.toolprune, shadow=True)
            replay = replace(ctx, config=cfg)
            repos.pin(ctx.root, ctx.host.name, sid, ctx.repo, row["cwd"])
            sess_name = os.path.join("sessions", sid + ".json")
            sess = ctx.store.read(sess_name) or {}
            ctx.store.write(sess_name, dict(sess, session_id=sid, cwd=row["cwd"],
                            started=row["started"], transcript=row["transcript"],
                            role=row["role"], source="backfill", ended=time.time(),
                            arm="historical"))
            rec = fold.fold_session(replay, sid, row["transcript"], role=row["role"],
                                    source="backfill")
            if rec and not rec.get("error"):
                completed.append(row)
                if row["_mapping"]:
                    Store(root).append("backfill-mappings.jsonl", dict(row["_mapping"],
                                       host=ctx.host.name, session_id=sid, at=time.time()))
        pool = [r for r in completed if r in stream]
        retry = [r for r in stream
                 if facts.store_for(r["_ctx"]).read("backfill-proposer.json") is None]
        if retry_paid and retry:
            pool = retry        # the whole stream, not just what ended since the failed pass
        sample, projected = paid_sample(pool, cap) if cap else ([], 0)
        out["paid_sample"] = [{k: r[k] for k in ("session_id", "host", "repo_key",
                                                    "effective_tokens")} for r in sample]
        out["projected_paid_tokens"] = projected
        out["paid"] = _paid_pass(sample, cap)
        out["folded"] = len(completed)
        for base in sorted({r["_ctx"].store.base for r in completed + sample}):
            own = [r for r in completed if r["_ctx"].store.base == base]
            ref = (own or [r for r in sample if r["_ctx"].store.base == base])[0]
            paid = [p for p in out["paid"] if p["host"] == ref["host"]
                    and p["repo_key"] == ref["repo_key"]]
            Store(base).append("ledger.jsonl", {"kind": "backfill", "at": time.time(),
                "since": since, "cutoff": start, "sessions": len(own),
                "sidechains": sum(r["role"] == "sidechain" for r in own), "paid": paid,
                "estimated_proposer_tokens": sum(p["estimated_proposer_tokens"] for p in paid),
                "overhead": {"optimizer_tokens": sum(p["optimizer_tokens"] for p in paid)}})
    return out
