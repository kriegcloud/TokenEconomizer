"""Micro-skill state routing and a copy-only migration for repository-scoped facts.

Tools and turn envelopes retain their host stores. The shared mechanism lock serializes both
hosts, including the initial import. Duplicate facts keep one earned history, never the sum of
two histories that might describe the same sessions.
"""
import copy
import os

from .store import Store


def store_for(ctx):
    if ctx.config.microskills_scope == "repo":
        return Store(os.path.join(os.path.dirname(ctx.store.base), "_shared"))
    return ctx.store


def _shift(fact, delta):
    fact = copy.deepcopy(fact)
    life = fact.get("life") or {}
    life["confirms"] = [[i + delta, fs] for i, fs in life.get("confirms", [])]
    life["contradicts"] = [i + delta for i in life.get("contradicts", [])]
    for key in ("promoted_at", "retired_at"):
        if life.get(key) is not None:
            life[key] += delta
    return fact


def migrate(ctx, dst):
    """Called under dst's mechanism lock; source files remain available for host scope."""
    if ctx.config.microskills_scope != "repo":
        return False
    from .hosts import HOSTS
    marker = dst.read("host-imports.json") or {}
    changed = False
    label = ctx.repo.label.replace("/", "__") + ".json"
    for host in sorted(HOSTS):
        if marker.get(host):
            continue
        src = Store(os.path.join(os.path.dirname(ctx.store.base), host))
        if not os.path.isdir(src.base):
            continue
        with src.locked("mechanisms") as got:
            if not got:
                raise RuntimeError("micro-skill migration source busy")
            for sub in ("envfacts_state", "manual_state"):
                name = os.path.join("mechanisms", sub, label)
                incoming = src.read(name)
                if not incoming:
                    continue
                old = dst.read(name) or {}
                now = max(old.get("now", 0), incoming.get("now", 0))
                merged = dict(incoming if not old else old)
                facts = {k: _shift(v, now - old.get("now", 0))
                         for k, v in old.get("facts", {}).items()}
                for k, v in incoming.get("facts", {}).items():
                    count = lambda f: len((f.get("life") or {}).get("confirms", []))
                    if k not in facts or count(v) > count(facts[k]):
                        facts[k] = _shift(v, now - incoming.get("now", 0))
                merged.update(now=now, facts=facts)
                merged["extractors"] = dict(incoming.get("extractors") or {},
                                             **(old.get("extractors") or {}))
                dst.write(name, merged)
                changed = True
            proposal = src.read("backfill-proposer.json")
            if proposal and not dst.read("backfill-proposer.json"):
                dst.write("backfill-proposer.json", proposal)
            inj = src.read("injection.json") or {}
            prior = dst.read("injection.json") or {}
            if inj.get("sessions", 0) > prior.get("sessions", 0):
                dst.write("injection.json", inj)
        marker[host] = True
    dst.write("host-imports.json", marker)
    return changed


def render(ctx, dst, profile=None):
    """Refresh the injection cache from the persisted facts without advancing their clocks."""
    from ..config import InjectionConfig
    from ..microskills import PrefixStage
    from .adapter import LiveAdapter
    profile = profile or ctx.config.microskills
    if profile == "off":
        return
    if profile == "envfacts" and dst.read("backfill-proposer.json"):
        profile = "envfacts+distiller"
    stage = PrefixStage(InjectionConfig(enabled=True, profile=profile,
                        max_facts=ctx.config.microskills_max_facts,
                        max_tokens=ctx.config.microskills_max_tokens),
                        ctx.repo.label, dst.ensure("mechanisms"), False,
                        adapter=LiveAdapter(ctx.host, ctx.repo.root,
                                            vocabulary=ctx.config.vocabulary))
    prior = dst.read("injection.json") or {}
    dst.write("injection.json", dict(prior, profile=profile, rendered=stage.render() or "",
                                    rendered_ids=stage.injected_ids(),
                                    facts=stage.distiller.debug_row()))


def prepare(ctx):
    dst = store_for(ctx)
    if ctx.config.microskills_scope == "repo":
        with dst.locked("mechanisms") as got:
            if not got:
                raise RuntimeError("shared micro-skill state busy")
            if migrate(ctx, dst):
                render(ctx, dst)
    return dst
