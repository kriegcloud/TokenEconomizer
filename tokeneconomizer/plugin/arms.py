"""Stable session arms: resumed sessions retain the assignment pinned by their first hook."""
import hashlib

CLAUDE_CAVEAT = ("Claude Code's deny list in .claude/settings.local.json applies to every "
                 "session in the checkout and cannot be undone per session. Holdout covers "
                 "micro-skills and the loop guard; tool pruning uses a before/after delta.")


def assignment(session_id, fraction):
    value = int(hashlib.sha256(str(session_id).encode("utf-8")).hexdigest()[:8], 16) / 2**32
    return "holdout" if value < fraction else "treated"


def summarize(records):
    from .accounting import overhead_total
    out = {}
    for arm in ("treated", "holdout"):
        rows = [r for r in records if r.get("kind") == "session" and r.get("arm") == arm
                and r.get("source") != "backfill" and r.get("role") != "sidechain"]
        priced = [r for r in rows if r.get("effective_tokens") is not None]
        ids = {r.get("session_id") for r in priced}
        cost = sum(r["effective_tokens"] + overhead_total(r.get("overhead")) for r in priced)
        cost += sum(overhead_total(r.get("overhead")) for r in records
                    if r.get("kind") == "overhead" and r.get("session_id") in ids)
        n = len(rows)
        out[arm] = {"sessions": n, "priced_sessions": len(priced),
                    "mean_cost": cost / len(priced) if priced else None,
                    "mean_turns": sum(r.get("turns", 0) for r in rows) / n if n else None,
                    "mean_redirects": sum(r.get("nudges", 0) for r in rows) / n if n else None}
    return out
