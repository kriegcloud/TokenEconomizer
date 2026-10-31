"""Warm start uses synthetic host logs and private temporary state, never agent history."""
import hashlib
import json
import os
import time
from pathlib import Path

import pytest

from tokeneconomizer.plugin import arms, backfill, cli, facts, fold, hosts, paths, repos, sidecall
from tokeneconomizer.plugin.config import LiveConfig, load_config, write_config
from tokeneconomizer.plugin.context import LiveContext
from tokeneconomizer.plugin.controller import LiveController
from tokeneconomizer.plugin.store import Store
from tokeneconomizer.trace import claude, codex


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("TOKENECONOMIZER_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("TE_FOLD_SYNC", "1")
    monkeypatch.setenv("TE_BACKFILL_PAID_CAP", "0")
    monkeypatch.setattr(sidecall, "runner_for", lambda *a: pytest.fail("unexpected paid call"))


def log(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    os.utime(path, (time.time() - 3600, time.time() - 3600))
    return str(path)


def stamp(age=86400):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(time.time() - age, timezone.utc).isoformat()


def checkout(path, remote="https://example.org/team/repo.git"):
    (path / ".git").mkdir(parents=True)
    (path / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
    (path / ".git" / "config").write_text(f'[remote "origin"]\nurl = {remote}\n',
                                           encoding="utf-8")
    return path


def claude_log(path, cwd, sid="s1", age=86400, side=False, failed=False, complete=True):
    records = [{"type": "user", "sessionId": sid, "cwd": str(cwd), "timestamp": stamp(age),
                "isSidechain": side, "message": {"content": "fix it"}},
               {"type": "assistant", "message": {"id": sid + "-call", "model": "claude-x",
                "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                             "input": {"command": "python -m pytest"}}],
                "usage": {"input_tokens": 100, "output_tokens": 10}}},
               {"type": "user", "message": {"content": [{"type": "tool_result",
                "tool_use_id": "t1", "is_error": failed,
                "content": "1 failed in 1.0s" if failed else "1 passed in 1.0s"}]}}]
    if complete:
        records.append({"type": "assistant", "message": {"id": sid + "-end",
                        "content": [{"type": "text", "text": "done"}],
                        "stop_reason": "end_turn"}})
    return log(path, records)


def source(cwd, sid="s1", **kw):
    return claude_log(Path(os.environ["HOME"]) / ".claude/projects/project" / (sid + ".jsonl"),
                      cwd, sid, **kw)


def codex_log(path, cwd, sid="c1", age=86400):
    return log(path, [
        {"type": "session_meta", "timestamp": stamp(age),
         "payload": {"id": sid, "cwd": str(cwd)}},
        {"type": "event_msg", "payload": {"type": "task_started"}},
        {"type": "event_msg", "payload": {"type": "item_completed", "item": {
            "type": "CommandExecution", "command": ["python", "-m", "pytest"],
            "exit_code": 0, "stdout": "1 passed in 1.0s"}}},
        {"type": "event_msg", "payload": {"type": "task_complete"}},
        {"type": "event_msg", "payload": {"type": "token_count", "info": {
            "total_token_usage": {"input_tokens": 100, "cached_input_tokens": 10,
                                  "output_tokens": 5}}}},
    ])


def context(cwd, host="claude"):
    return LiveContext.build(hosts.HOSTS[host](), str(cwd), settle=False)


def test_discovery_main_sidechain_first_cwd_and_window(tmp_path):
    root = Path(os.environ["HOME"]) / ".claude/projects"
    source(tmp_path, "main")
    source(tmp_path, "old", age=40 * 86400)
    source(tmp_path, "flagged", side=True)
    claude_log(root / "project/main/subagents/agent-a.jsonl", tmp_path, "main", side=True)
    (root / "project/main.jsonl").open("a", encoding="utf-8").write(
        json.dumps({"cwd": "/moved", "sessionId": "main"}) + "\n")
    found = list(claude.discover(backfill.cutoff("30d")))
    assert len(found) == 3
    assert {r[0] for r in found} == {"main", "flagged--flagged", "main--agent-a"}
    assert all(r[2] == str(tmp_path) for r in found)
    assert sum(r[4] for r in found) == 2


def test_codex_discovery_and_replay(tmp_path):
    repo = checkout(tmp_path / "repo")
    codex_log(Path(os.environ["HOME"]) / ".codex/sessions/2026/10/30/rollout-c1.jsonl", repo)
    rows = list(codex.discover(backfill.cutoff("30d")))
    assert len(rows) == 1 and rows[0][0] == "c1" and rows[0][2] == str(repo)
    result = backfill.run(apply=True, host_names=("codex",))
    ctx = context(repo, "codex")
    assert result["folded"] == 1
    assert ctx.store.read("envelope.json")["samples"][0]["source"] == "backfill"
    assert ctx.store.read("untool.json")["sessions"] == 1


def test_dry_run_cli_writes_nothing(tmp_path, capsys):
    repo = checkout(tmp_path / "repo")
    source(repo)
    state = Path(os.environ["TOKENECONOMIZER_HOME"])
    assert cli.main(["backfill", "--json", "--repo", str(repo)]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["candidates"] == 1 and result["apply"] is False
    assert not state.exists()


def test_replay_order_idempotency_sidechain_and_ledger(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    source(repo, "new", age=86400)
    source(repo, "old", age=2 * 86400)
    source(repo, "side", side=True)
    original = fold.fold_session
    order = []
    def recorded(ctx, sid, *args, **kw):
        order.append(sid)
        assert ctx.config.shadow and ctx.config.toolprune == "propose"
        return original(ctx, sid, *args, **kw)
    monkeypatch.setattr(fold, "fold_session", recorded)
    out = backfill.run(apply=True)
    assert out["folded"] == 3 and order[0] == "old"
    ctx = context(repo)
    assert ctx.store.read("untool.json")["sessions"] == 2
    assert ctx.store.read("untool.json")["prompts"] == 2
    assert len(ctx.store.read("envelope.json")["samples"]) == 2
    assert ctx.store.read("injection.json")["sessions"] == 3
    assert ctx.store.read_lines("ledger.jsonl")[-1]["kind"] == "backfill"
    from tokeneconomizer.plugin import accounting
    assert accounting.summarize(ctx.store.read_lines("ledger.jsonl"))["sessions"] == 2
    assert repos.pinned(ctx.root, "claude", "old").key == ctx.repo.key
    before = ctx.store.read_lines("ledger.jsonl")
    assert backfill.run(apply=True)["folded"] == 0
    assert ctx.store.read_lines("ledger.jsonl") == before
    assert not (repo / ".claude").exists()


def test_direct_sidechain_fold_preserves_tool_state(tmp_path):
    repo = checkout(tmp_path / "repo")
    path = source(repo, "child", side=True)
    ctx = context(repo)
    ctx.store.write("untool.json", {"sessions": 17, "prompts": 90})
    rec = fold.fold_session(ctx, "child", path, role="sidechain")
    assert rec["role"] == "sidechain" and rec["untool"] is None
    assert ctx.store.read("untool.json") == {"sessions": 17, "prompts": 90}
    assert not Path(ctx.store.path("envelope.json")).exists()
    assert ctx.store.read("injection.json")["profile"] == "envfacts"


@pytest.mark.parametrize("failed,complete,expected", [(False, True, 1), (True, True, 0),
                                                      (False, False, 0)])
def test_envelope_completion_and_outcome(tmp_path, failed, complete, expected):
    repo = checkout(tmp_path / "repo")
    path = source(repo, failed=failed, complete=complete)
    ctx = context(repo)
    backfill.record_envelope(ctx, "s1", path)
    backfill.record_envelope(ctx, "s1", path)
    assert len(ctx.store.read("envelope.json")["resolved_turns"]) == expected


def test_dead_lane_opt_in_and_mapping_receipt(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "app")
    gone = repo / ".claude/worktrees/removed"
    source(gone)
    assert backfill.run()["skipped"][0]["reason"] == "unresolved cwd"
    monkeypatch.setenv("TE_BACKFILL_RESOLVE_DEAD", "on")
    out = backfill.run(repo_path=str(repo))
    assert out["mappings"][0]["method"] == "git ancestor"
    assert not Path(os.environ["TOKENECONOMIZER_HOME"]).exists()
    assert backfill.run(apply=True)["folded"] == 1
    assert Store(os.environ["TOKENECONOMIZER_HOME"]).read_lines("backfill-mappings.jsonl")[0][
        "cwd"] == str(gone)


def test_dead_sibling_remotes_must_agree(tmp_path):
    checkout(tmp_path / "app")
    checkout(tmp_path / "app2")
    gone = str(tmp_path / "app-worktrees/lane")
    assert repos.resolve_dead(gone)[0].remote == "example.org/team/repo"
    checkout(tmp_path / "app3", "https://example.org/other/repo.git")
    assert repos.resolve_dead(gone) is None


def test_paid_sample_stops_at_cap(tmp_path):
    repo = checkout(tmp_path / "repo")
    rows = []
    for i in range(3):
        rows.append(dict(session_id=f"s{i}", started=i, role="main",
                         transcript=source(repo, f"s{i}"), _ctx=context(repo),
                         effective_tokens=10_000_000))
    one, cost = backfill.paid_sample(rows[-1:], 500000)
    assert len(one) == 1
    assert backfill.paid_sample(rows, cost)[0] == one
    assert backfill.paid_sample(rows, cost - 1) == ([], 0)
    assert backfill.paid_sample(rows, 0) == ([], 0)


def test_one_paid_call_after_free_replay(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    for i in range(5):
        source(repo, "s" + str(i), age=(i + 1) * 86400)
    monkeypatch.setenv("TE_BACKFILL_PAID_CAP", "500000")
    monkeypatch.setenv("TE_MICROSKILLS", "on")
    calls = []
    def runner(prompt, model):
        assert context(repo).store.read("untool.json")["sessions"] == 5
        calls.append(prompt)
        return '{"facts": [], "extractors": []}', 42
    monkeypatch.setattr(sidecall, "runner_for", lambda *a: runner)
    out = backfill.run(apply=True)
    assert len(calls) == 1 and len(out["paid"]) == 1
    assert calls[0].index('"iid": "s0"') < calls[0].index('"iid": "s4"')
    assert out["paid"][0]["optimizer_tokens"] == 42
    assert (len(calls[0]) + 3) // 4 + backfill.PAID_OUTPUT_RESERVE <= 500000
    assert out["paid"][0]["estimated_proposer_tokens"] <= 500000
    backfill.run(apply=True)
    assert len(calls) == 1


def test_paid_sample_does_not_truncate_at_live_window(tmp_path, monkeypatch):
    from tokeneconomizer.microskills.distiller import Distiller
    paths_ = [(f"s{i}", source(tmp_path, f"s{i}")) for i in range(12)]
    prompts = []
    d = Distiller("repo", runner=lambda p, m: (prompts.append(p) or "{}", 1))
    d.propose_history(paths_)
    assert all(f'"iid": "s{i}"' in prompts[0] for i in range(12))
    assert len(d.window) == 8


def test_shared_migration_keeps_most_confirmed_and_host_tools(tmp_path, monkeypatch):
    from tokeneconomizer.microskills.envfacts import EnvFactsDistiller, EnvFact
    repo = checkout(tmp_path / "repo")
    for host, count in (("claude", 3), ("codex", 5)):
        ctx = context(repo, host)
        dist = EnvFactsDistiller(ctx.repo.label)
        fact = EnvFact("same", "empty_probe", False, {})
        fact.life.confirms = [(i, []) for i in range(count)]
        dist.facts["same"] = fact
        dist.now = count
        dist.save(ctx.mech_dir)
        ctx.store.write("untool.json", {"sessions": count})
    monkeypatch.setenv("TE_MICROSKILLS_SCOPE", "repo")
    monkeypatch.setenv("TE_MICROSKILLS", "envfacts")
    ctx = context(repo)
    dst = ctx.facts_store
    state = dst.read("mechanisms/envfacts_state/team__repo.json")
    assert len(state["facts"]["same"]["life"]["confirms"]) == 5
    assert state["now"] == 5
    assert context(repo, "codex").facts_store.base == dst.base
    assert context(repo).store.read("untool.json")["sessions"] == 3
    assert context(repo, "codex").store.read("untool.json")["sessions"] == 5
    assert context(repo).store.read("mechanisms/envfacts_state/team__repo.json")
    assert ctx.facts_store.read("mechanisms/envfacts_state/team__repo.json") == state


@pytest.mark.parametrize("fraction", [0, 0.2, 0.999])
def test_arm_hash_is_exact_and_deterministic(fraction):
    sid = "stable-session"
    expected = int(hashlib.sha256(sid.encode()).hexdigest()[:8], 16) / 2**32 < fraction
    assert arms.assignment(sid, fraction) == ("holdout" if expected else "treated")
    assert arms.assignment(sid, fraction) == arms.assignment(sid, fraction)


def test_holdout_hooks_inert_but_record_and_fold(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    sid = next(str(i) for i in range(100) if arms.assignment(str(i), 0.9) == "holdout")
    monkeypatch.setenv("TE_HOLDOUT", "0.9")
    monkeypatch.setenv("TE_MICROSKILLS", "envfacts")
    ctx = context(repo)
    ctx.store.write("injection.json", {"rendered": "SECRET FACT"})
    monkeypatch.setattr(LiveController, "_judge", lambda *a: pytest.fail("holdout judge"))
    path = source(repo, sid)
    def hook(event, **extra):
        payload = dict(hook_event_name=event, session_id=sid, cwd=str(repo),
                       transcript_path=path, **extra)
        return LiveController(context(repo), payload).handle()
    assert hook("SessionStart") == {}
    assert hook("UserPromptSubmit", prompt="go") == {}
    assert hook("PostToolUse", tool_name="Bash", tool_input={"command": "pytest"},
                tool_response={"exit_code": 0}) == {}
    hook("Stop")
    hook("SessionEnd")
    ledger = ctx.store.read_lines("ledger.jsonl")
    assert ledger[-1]["arm"] == "holdout" and ledger[-1]["turns"] == 1
    assert ctx.store.read("injection.json")["sessions"] == 1
    # Resuming after changing the configured fraction retains the original arm.
    monkeypatch.setenv("TE_HOLDOUT", "0")
    assert hook("SessionStart", source="resume") == {}


def test_status_arms_includes_overhead_and_caveat(tmp_path, capsys):
    ctx = context(checkout(tmp_path / "repo"))
    ctx.store.append("ledger.jsonl", {"kind": "session", "session_id": "s", "arm": "treated",
        "effective_tokens": 100, "turns": 2, "nudges": 1,
        "overhead": {"optimizer_tokens": 10}})
    ctx.store.append("ledger.jsonl", {"kind": "overhead", "session_id": "s",
                                     "overhead": {"spiral_judge_tokens": 5}})
    ctx.store.append("ledger.jsonl", {"kind": "session", "arm": "holdout",
                                     "source": "backfill", "effective_tokens": 999})
    assert cli.main(["--cwd", ctx.repo.root, "--host", "claude", "status", "--arms", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["arms"]["treated"]["mean_cost"] == 115
    assert result["arms"]["holdout"]["mean_cost"] is None
    assert ".claude/settings.local.json" in result["arms_caveat"]


@pytest.mark.real_sidecalls
@pytest.mark.parametrize("scrub", [False, True])
def test_claude_sidecall_scrubs_only_child(tmp_path, monkeypatch, scrub):
    keys = ["ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN"] + [
        "ANTHROPIC_DEFAULT_" + m + "_MODEL" for m in ("HAIKU", "SONNET", "OPUS")]
    for key in keys:
        monkeypatch.setenv(key, "synthetic")
    monkeypatch.setattr(sidecall, "claude_no_mcp", lambda *a: [])
    monkeypatch.setattr(sidecall, "claude_flags", lambda *a: [])
    seen = []
    def run(cmd, **kw):
        seen.append(kw["env"])
        return type("Result", (), {"stdout": '{"result":"ok"}'})()
    monkeypatch.setattr(sidecall.subprocess, "run", run)
    sidecall.claude_call("prompt", cli="synthetic-cli", scrub_env=scrub)
    assert all((key not in seen[0]) == scrub for key in keys)
    assert all(os.environ[key] == "synthetic" for key in keys)
    assert seen[0]["TE_SIDECALL"] == "1"


@pytest.mark.parametrize("key,value", [("holdout", "nan"), ("holdout", 1),
    ("holdout", -0.1), ("backfill_paid_cap", -1), ("microskills_scope", "other")])
def test_settings_reject_invalid_values(key, value):
    assert not LiveConfig().set(key, value)


def test_settings_files_and_environment(tmp_path, monkeypatch):
    root = os.environ["TOKENECONOMIZER_HOME"]
    write_config(str(Path(root) / "config.json"), {"holdout": "0.2", "microskills_scope": "repo",
        "backfill_resolve_dead": "on", "backfill_paid_cap": 0, "sidecall_scrub_env": "on"})
    cfg = load_config(root)
    assert cfg.holdout == 0.2 and cfg.backfill_resolve_dead and cfg.sidecall_scrub_env
    assert cfg.microskills_scope == "repo" and cfg.backfill_paid_cap == 0
    monkeypatch.setenv("TE_HOLDOUT", "0.3")
    assert load_config(root).holdout == 0.3


def test_proposals_keep_confirmation_gate_and_free_future_observations(tmp_path):
    from tokeneconomizer.microskills.distiller import Distiller
    repo = checkout(tmp_path / "repo")
    ctx = context(repo)
    spec = {"id": "check", "claim": "use pytest", "injection": "pytest works here",
            "detector": {"scope": "pytest", "confirm": "pytest", "fired": "pytest"},
            "cite": ["s1", "s2"]}
    dist = Distiller(ctx.repo.label, runner=lambda p, m: (
        json.dumps({"facts": [spec]}), 5))
    dist.propose_history([("s2", source(repo, "s2")), ("s1", source(repo, "s1"))])
    assert dist.facts["check"].state == "CANDIDATE"
    dist.save(ctx.mech_dir)
    ctx.store.write("backfill-proposer.json", {"at": time.time()})
    ctx.config.microskills = "envfacts"
    fold.fold_session(ctx, "s3", source(repo, "s3"))
    saved = Distiller.load(ctx.repo.label, ctx.mech_dir)
    assert saved.facts["check"].state == "ACTIVE"
    assert len(saved.facts["check"].life.confirms) == 3
    assert saved.opt_tokens == 5


def test_large_source_usage_small_digests_selects_many(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    monkeypatch.chdir(repo)
    monkeypatch.setenv("TE_BACKFILL_PAID_CAP", "500000")
    for i in range(60):
        path = Path(source(repo, f"s{i}", age=(i + 1) * 3600))
        records = [json.loads(line) for line in path.read_text().splitlines()]
        records[1]["message"]["usage"]["input_tokens"] = 10_000_000
        log(path, records)
    source(repo, "side", side=True)
    plan = backfill.run()
    assert len(plan["paid_sample"]) == 60
    assert all(r["effective_tokens"] > 1_000_000 for r in plan["paid_sample"])
    assert 4096 < plan["projected_paid_tokens"] < 500000
    assert not Path(os.environ["TOKENECONOMIZER_HOME"]).exists()


def test_paid_stream_prefers_cwd_then_explicit_repo_then_busiest(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    other = checkout(tmp_path / "other", "https://example.org/team/other.git")
    source(repo, "ours", age=3 * 86400)
    source(other, "newest", age=86400)
    source(other, "other2", age=2 * 86400)
    monkeypatch.setenv("TE_BACKFILL_PAID_CAP", "500000")
    monkeypatch.chdir(repo)
    assert [r["session_id"] for r in backfill.run()["paid_sample"]] == ["ours"]
    assert len(backfill.run(repo_path=str(other))["paid_sample"]) == 2
    monkeypatch.chdir(tmp_path)
    assert len(backfill.run()["paid_sample"]) == 2
    monkeypatch.chdir(checkout(tmp_path / "empty", "https://example.org/team/empty.git"))
    assert backfill.run()["paid_sample"] == []


def test_target_repository_filter_and_active_log(tmp_path):
    repo = checkout(tmp_path / "repo")
    other = checkout(tmp_path / "other", "https://example.org/team/other.git")
    path = source(repo)
    source(other, "other")
    result = backfill.run(repo_path=str(repo))
    assert result["candidates"] == 1 and result["skipped"][0]["reason"] == "other repository"
    os.utime(path, None)
    result = backfill.run(repo_path=str(repo))
    assert result["candidates"] == 0
    assert "transcript still active" in {r["reason"] for r in result["skipped"]}


def test_shared_backfill_uses_one_paid_call_across_hosts(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    source(repo)
    codex_log(Path(os.environ["HOME"]) / ".codex/sessions/2026/10/30/c.jsonl", repo)
    monkeypatch.setenv("TE_MICROSKILLS_SCOPE", "repo")
    monkeypatch.setenv("TE_BACKFILL_PAID_CAP", "500000")
    calls = []
    monkeypatch.setattr(sidecall, "runner_for", lambda *a: lambda p, m: (
        calls.append(p) or '{"facts": []}', 12))
    out = backfill.run(apply=True)
    assert out["folded"] == 2 and len(calls) == 1
    assert context(repo).facts_store.read("injection.json")["sessions"] == 2
    assert context(repo).store.read("untool.json")["sessions"] == 1
    assert context(repo, "codex").store.read("untool.json")["sessions"] == 1


@pytest.mark.parametrize("window", ["bad", "-1d", "30", "1y"])
def test_bad_windows_are_rejected(window):
    with pytest.raises(ValueError):
        backfill.cutoff(window)


def test_paid_pass_is_one_call_with_separate_host_streams(tmp_path, monkeypatch):
    repo = checkout(tmp_path / "repo")
    source(repo, age=86400)
    codex_log(Path(os.environ["HOME"]) / ".codex/sessions/2026/10/30/c.jsonl", repo,
              age=2 * 86400)
    monkeypatch.setenv("TE_BACKFILL_PAID_CAP", "500000")
    calls = []
    monkeypatch.setattr(sidecall, "runner_for", lambda host, cfg: lambda p, m: (
        calls.append(host) or '{"facts": []}', 12))
    out = backfill.run(apply=True)
    assert out["folded"] == 2 and calls == ["claude"]
    assert out["paid"][0]["sessions"] == ["s1"]


def test_claude_result_reads_object_and_event_list():
    from tokeneconomizer.plugin.sidecall import claude_result
    obj = {"type": "result", "result": "ok", "usage": {"input_tokens": 3}}
    assert claude_result(json.dumps(obj)) == obj
    events = [{"type": "system", "subtype": "init"}, {"type": "assistant"}, obj]
    assert claude_result(json.dumps(events)) == obj
    assert claude_result("not json") == {}
    assert claude_result(json.dumps([{"type": "system"}])) == {}
