"""Eval harness (PLAN.md Phase 4).

evals/corpus.jsonl   every unique post parsed from fixtures/ (gitignored: names/phones)
evals/labeled.jsonl  {"id", "gold": {...Listing fields...}, "note"} per post (gitignored)
evals/runs/<ts>.json full per-post predictions of each `flatwatch eval` run (gitignored)

Metrics:
- prefilter recall on real listings (gold intent=offering), plus pass rate
- extraction field accuracy (intent over all posts; other fields over gold offerings)
- end-to-end instant-alert (P1/P2) precision and recall; gold tier = match(gold listing)
"""

from __future__ import annotations

import glob
import json
import time
from collections import Counter, defaultdict
from pathlib import Path

from loguru import logger

from .config import WORK_DIR, AppConfig
from .extract import PROMPT_VERSION, AuthError, ExtractError, extract_batch
from .match import match
from .models import Listing, RawPost
from .prefilter import PREFILTER_VERSION, prefilter

EVAL_DIR = WORK_DIR / "evals"
CORPUS = EVAL_DIR / "corpus.jsonl"
LABELS = EVAL_DIR / "labeled.jsonl"
SYNTHETIC = EVAL_DIR / "synthetic.jsonl"   # hand-written boundary cases, no real people (committed)
RUNS = EVAL_DIR / "runs"

FIELDS = ["intent", "offering_type", "unit", "locality", "rent_inr", "gender_pref", "private_bathroom"]
INSTANT = {"P1", "P2"}
TARGETS = {"prefilter_recall": 1.0, "e2e_recall": 0.95, "e2e_precision": 0.80}


# ------------------------------------------------------------------ corpus --

def build_corpus() -> list[RawPost]:
    from .sources.facebook import parse_payloads
    from .sources.reddit import parse_dom_items, parse_listing_json

    by_id: dict[str, RawPost] = {}

    def keep(p: RawPost) -> None:
        old = by_id.get(p.id)
        if old is None or len(p.text) > len(old.text):
            by_id[p.id] = p

    for d in sorted(Path(p) for p in glob.glob(str(WORK_DIR / "fixtures/fb/*/*/"))):
        meta = json.loads((d / "meta.json").read_text())
        gql = [f.read_text() for f in sorted(d.glob("gql_*.txt"))]
        html = [f.read_text() for f in sorted(d.glob("html_*.json"))]
        for p in parse_payloads(gql, html, meta["url"]):
            keep(p)
    for d in sorted(glob.glob(str(WORK_DIR / "fixtures/reddit/*/*/"))):
        sub = json.loads(Path(d, "meta.json").read_text())["sub"]
        if Path(d, "new.json").exists():
            for p in parse_listing_json(json.loads(Path(d, "new.json").read_text()), sub):
                keep(p)
        if Path(d, "dom.json").exists():
            for p in parse_dom_items(json.loads(Path(d, "dom.json").read_text()), sub):
                keep(p)
    posts = sorted(by_id.values(), key=lambda p: p.id)
    EVAL_DIR.mkdir(exist_ok=True)
    CORPUS.write_text("".join(p.model_dump_json() + "\n" for p in posts))
    return posts


def load_synthetic() -> list[dict]:
    if not SYNTHETIC.exists():
        return []
    return [json.loads(l) for l in SYNTHETIC.read_text().splitlines() if l.strip()]


def synthetic_post(row: dict) -> RawPost:
    return RawPost(source="facebook", source_key="synthetic", post_id=row["id"], text=row["text"])


def load_corpus(include_synthetic: bool = True) -> dict[str, RawPost]:
    out = {p.id: p for p in (RawPost.model_validate_json(l) for l in CORPUS.read_text().splitlines() if l.strip())}
    if include_synthetic:
        out.update({(p := synthetic_post(r)).id: p for r in load_synthetic()})
    return out


def load_labels(include_synthetic: bool = True) -> dict[str, Listing]:
    out = {}
    if include_synthetic:
        for r in load_synthetic():
            out[synthetic_post(r).id] = Listing.model_validate(r["gold"])
    for line in LABELS.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        out[row["id"]] = Listing.model_validate(row["gold"])
    return out


# --------------------------------------------------------------------- run --

# The matcher treats these identically (both are P2 room offers), so a swap is not an error.
EQUIVALENT = {"offering_type": [{"flatmate", "replacement"}]}


def _eq(field: str, pred, gold) -> bool:
    for group in EQUIVALENT.get(field, []):
        if pred in group and gold in group:
            return True
    if field == "rent_inr" and pred is not None and gold is not None:
        return abs(pred - gold) <= max(500, 0.05 * gold)
    return pred == gold


async def run_eval(cfg: AppConfig) -> dict:
    corpus, labels = load_corpus(), load_labels()
    ids = [i for i in labels if i in corpus]
    if missing := [i for i in labels if i not in corpus]:
        logger.warning("{} labelled ids not in corpus (ignored)", len(missing))
    crit = cfg.criteria

    pf = {i: prefilter(corpus[i], crit) for i in ids}
    to_llm = [i for i in ids if pf[i][0]]
    preds: dict[str, Listing] = {}
    usage, failed_batches = [], 0
    bs = cfg.extract.batch_size
    for k in range(0, len(to_llm), bs):
        chunk = [corpus[i] for i in to_llm[k : k + bs]]
        try:
            res = await extract_batch(chunk, cfg)
        except AuthError:
            raise
        except ExtractError as e:
            failed_batches += 1
            logger.error("batch {} failed: {}", k // bs, e)
            continue
        preds.update(res.listings)
        usage += res.usage
        logger.info("extracted batch {}/{}", k // bs + 1, -(-len(to_llm) // bs))

    rows = []
    for i in ids:
        gold = labels[i]
        gold_tier = match(gold, crit).tier
        pred = preds.get(i)
        pred_tier = match(pred, crit).tier if pred else "DROP"
        rows.append({"id": i, "synthetic": corpus[i].source_key == "synthetic", "prefilter": pf[i], "gold": gold.model_dump(mode="json"),
                     "pred": pred.model_dump(mode="json") if pred else None,
                     "gold_tier": gold_tier, "pred_tier": pred_tier})

    # prefilter
    real = [r for r in rows if r["gold"]["intent"] == "offering"]
    pf_recall = sum(r["prefilter"][0] for r in real) / len(real) if real else 1.0
    pf_missed = [r["id"] for r in real if not r["prefilter"][0]]

    # field accuracy (only on posts the LLM actually saw)
    acc, wrong = {}, defaultdict(list)
    for f in FIELDS:
        scope = [r for r in rows if r["pred"] is not None and (f == "intent" or r["gold"]["intent"] == "offering")]
        ok = 0
        for r in scope:
            if _eq(f, r["pred"][f], r["gold"][f]):
                ok += 1
            else:
                wrong[f].append((r["id"], r["gold"][f], r["pred"][f]))
        acc[f] = (ok / len(scope) if scope else None, len(scope))

    # end-to-end instant alerts (all, real-only, synthetic-only)
    def e2e(subset):
        tp = sum(r["gold_tier"] in INSTANT and r["pred_tier"] in INSTANT for r in subset)
        fp = sum(r["gold_tier"] not in INSTANT and r["pred_tier"] in INSTANT for r in subset)
        fn = sum(r["gold_tier"] in INSTANT and r["pred_tier"] not in INSTANT for r in subset)
        caught = sum(r["gold_tier"] in INSTANT and r["pred_tier"] in INSTANT | {"DIGEST"} for r in subset)
        lost_digest = sum(r["gold_tier"] == "DIGEST" and r["pred_tier"] == "DROP" for r in subset)
        return {"tp": tp, "fp": fp, "fn": fn,
                "precision": tp / (tp + fp) if tp + fp else 1.0, "recall": tp / (tp + fn) if tp + fn else 1.0,
                "gold_instant": tp + fn, "gold_instant_caught_incl_digest": caught,
                "gold_digest_dropped": lost_digest, "n": len(subset)}

    e2e_all = e2e(rows)
    e2e_real = e2e([r for r in rows if not r["synthetic"]])
    e2e_syn = e2e([r for r in rows if r["synthetic"]])
    confusion = Counter((r["gold_tier"], r["pred_tier"]) for r in rows)

    report = {
        "prompt_version": PROMPT_VERSION, "prefilter_version": PREFILTER_VERSION, "model": cfg.extract.model,
        "n_labeled": len(ids), "n_prefilter_pass": len(to_llm), "n_extracted": len(preds),
        "failed_batches": failed_batches,
        "prefilter_recall": pf_recall, "prefilter_missed": pf_missed,
        "field_accuracy": acc, "field_errors": dict(wrong),
        "e2e": e2e_all, "e2e_real": e2e_real, "e2e_synthetic": e2e_syn,
        "confusion": {f"{g}->{p}": n for (g, p), n in sorted(confusion.items())},
        "llm_calls": len(usage), "cost_usd": sum(u.cost_usd or 0 for u in usage),
        "input_tokens": sum(u.input_tokens or 0 for u in usage),
        "output_tokens": sum(u.output_tokens or 0 for u in usage),
        "rows": rows,
    }
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"{time.strftime('%Y%m%d-%H%M%S')}-{PROMPT_VERSION}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False, default=str))
    report["run_file"] = str(out.relative_to(WORK_DIR))
    return report


def format_report(r: dict, verbose: bool = False) -> str:
    def pct(x):
        return "n/a" if x is None else f"{100 * x:.1f}%"

    def mark(ok):
        return "✅" if ok else "❌"

    lines = [
        f"flatwatch eval · prompt {r['prompt_version']} · prefilter {r['prefilter_version']} · {r['model']}",
        f"labelled posts: {r['n_labeled']} · passed prefilter: {r['n_prefilter_pass']} "
        f"({pct(r['n_prefilter_pass'] / r['n_labeled'] if r['n_labeled'] else 0)}) · extracted: {r['n_extracted']}"
        + (f" · FAILED BATCHES: {r['failed_batches']}" if r["failed_batches"] else ""),
        "",
        f"{mark(r['prefilter_recall'] >= TARGETS['prefilter_recall'])} prefilter recall on real listings: "
        f"{pct(r['prefilter_recall'])}  (target 100%)" + (f"  missed: {r['prefilter_missed']}" if r["prefilter_missed"] else ""),
        "",
        "extraction field accuracy:",
    ]
    for f, (a, n) in r["field_accuracy"].items():
        lines.append(f"   {f:17} {pct(a):>7}  (n={n})")
    e = r["e2e"]
    lines += [
        "",
        f"{mark(e['recall'] >= TARGETS['e2e_recall'])} instant-alert recall:    {pct(e['recall'])}  "
        f"({e['tp']}/{e['gold_instant']}, target ≥95%)",
        f"{mark(e['precision'] >= TARGETS['e2e_precision'])} instant-alert precision: {pct(e['precision'])}  "
        f"({e['tp']}/{e['tp'] + e['fp']}, target ≥80%)",
    ]
    for label, key in (("real posts", "e2e_real"), ("synthetic", "e2e_synthetic")):
        x = r.get(key)
        if x and x["n"]:
            lines.append(f"   {label:10} n={x['n']:3}  recall {x['tp']}/{x['gold_instant']}  precision {x['tp']}/{x['tp'] + x['fp']}"
                         f"  gold digest dropped: {x['gold_digest_dropped']}")
    lines += [
        f"   gold instant caught as instant or digest: {e['gold_instant_caught_incl_digest']}/{e['gold_instant']}",
        "   gold->pred: " + ", ".join(f"{k}={v}" for k, v in r["confusion"].items()),
        "",
        f"LLM: {r['llm_calls']} calls · {r['input_tokens']} in / {r['output_tokens']} out tokens · ${r['cost_usd']:.4f}",
        f"full results: {r.get('run_file', '')}",
    ]
    if verbose:
        lines.append("\nfield errors (id: gold -> pred):")
        for f, errs in r["field_errors"].items():
            for i, g, p in errs:
                lines.append(f"   {f:17} {i}: {g!r} -> {p!r}")
        lines.append("\ntier errors:")
        for row in r["rows"]:
            if row["gold_tier"] != row["pred_tier"]:
                lines.append(f"   {row['id']}: {row['gold_tier']} -> {row['pred_tier']}")
    return "\n".join(lines)
