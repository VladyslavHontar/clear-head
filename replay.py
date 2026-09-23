#!/usr/bin/env python3
"""Replay logged payloads through a candidate /v1/systemone endpoint and compare with the verdicts
the hook actually logged — offline, without switching the live hook to the candidate.

sent.jsonl holds every state the hook sent (each claim + its excerpt); log.jsonl holds the
verdict for the same claim. So every session on the default backend is free evaluation data for
any candidate that speaks the same contract. Reference = rows logged by the `jev` backend.

Usage:  python3 replay.py http://127.0.0.1:8008/v1/systemone [hook_dir]
Prints agreement with the reference per label and, the number that matters for blocking, how
often the candidate says "contradicted" when the reference didn't — at which confidence. Writes
replay.jsonl next to the logs with every pair, for a threshold sweep.
"""
import json, pathlib, sys, urllib.request

URL = sys.argv[1]
HERE = pathlib.Path(sys.argv[2]).resolve() if len(sys.argv) > 2 else pathlib.Path(__file__).resolve().parent
CRITERIA = {"supported": "the evidence states the claim or directly implies it is true",
            "contradicted": "the evidence states the opposite of the claim or implies it is false",
            "not_addressed": "the evidence does not address what the claim asserts, either way"}


def ask(claim, excerpt):
    # one flat call per claim: the shape a small local model can actually read in full
    body = json.dumps({"state": {"claim": claim, "excerpt": excerpt},
                       "questions": {"q": {"type": "choice", "criteria": CRITERIA,
                                           "instructions": "How does `excerpt` relate to `claim`?"}}}).encode()
    req = urllib.request.Request(URL, body, {"Content-Type": "application/json", "User-Agent": "clear-head"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["answers"]["q"]


def main():
    ref = {}
    for line in open(HERE / "log.jsonl"):
        d = json.loads(line)
        if d.get("backend", "jev") != "jev":
            continue
        for claim, p in d.get("verdicts", {}).items():
            ref[(d.get("session"), claim)] = p
    rows = []
    with open(HERE / "replay.jsonl", "w") as out:
        for line in open(HERE / "sent.jsonl"):
            d = json.loads(line)
            for c in d.get("state", {}).get("claims", {}).values():
                p = ref.get((d.get("session"), c["claim"]))
                if not p:
                    continue
                v = ask(c["claim"], c["excerpt"])
                row = {"claim": c["claim"], "ref": max(p, key=p.get), "ref_p": p,
                       "cand": v["choice"], "cand_p": v["probabilities"], "cand_conf": v.get("confidence")}
                rows.append(row); out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(rows)} claims with both verdicts")
    for label in ("supported", "contradicted", "not_addressed"):
        rs = [r for r in rows if r["ref"] == label]
        if rs:
            conf = sorted(r["cand_conf"] or 0 for r in rs)
            print(f"  ref={label:14} n={len(rs):4}  candidate agrees {sum(r['cand'] == label for r in rs) / len(rs):.0%}  "
                  f"conf p10/p50/p90 = {conf[len(conf) // 10]:.2f}/{conf[len(conf) // 2]:.2f}/{conf[len(conf) * 9 // 10]:.2f}")
    fp = sorted(r["cand_conf"] or 0 for r in rows if r["cand"] == "contradicted" and r["ref"] != "contradicted")
    tp = sorted(r["cand_conf"] or 0 for r in rows if r["cand"] == "contradicted" and r["ref"] == "contradicted")
    print(f"  candidate-only 'contradicted': n={len(fp)}" + (f" conf p50={fp[len(fp) // 2]:.2f} p90={fp[len(fp) * 9 // 10]:.2f}" if fp else ""))
    print(f"  both 'contradicted':           n={len(tp)}" + (f" conf min={tp[0]:.2f} p50={tp[len(tp) // 2]:.2f}" if tp else ""))
    print("  precision of candidate 'contradicted' by <NAME>_FIRM threshold:")
    for t in (0.15, 0.3, 0.5, 0.6, 0.7):
        k = [r for r in rows if r["cand"] == "contradicted" and (r["cand_conf"] or 0) >= t]
        hit = sum(r["ref"] == "contradicted" for r in k)
        print(f"    {t:.2f}: kept {len(k):4}, agree {hit:3} ({hit / max(1, len(k)):.0%})")


if __name__ == "__main__":
    main()
