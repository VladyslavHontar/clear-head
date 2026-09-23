#!/usr/bin/env python3
"""Replay logged Jev payloads through Laya, offline, to calibrate LAYA_FIRM / JEV_CONTRA for it.

sent.jsonl already holds exactly what the Laya path sends (each claim + its excerpt), and
log.jsonl holds Jev's verdict for the same claim — so every Jev-backed session you run is free
Laya training/eval data, without ever switching your live hook to Laya.

Usage:  python3 laya_replay.py [hook_dir]      (laya_server.py must be running)
Prints, per Jev label, Laya's confidence distribution and how often it agrees — the numbers
LAYA_FIRM should be set from. Writes laya_replay.jsonl next to the logs with every pair.
"""
import json, os, pathlib, sys, urllib.request

HERE = pathlib.Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else pathlib.Path(__file__).resolve().parent
URL = f"http://{os.environ.get('LAYA_HOST', '127.0.0.1')}:{os.environ.get('LAYA_PORT', '8787')}/predict"
CRITERIA = {"supported": "the evidence states the claim or directly implies it is true",
            "contradicted": "the evidence states the opposite of the claim or implies it is false",
            "not_addressed": "the evidence does not address what the claim asserts, either way"}


def laya(claim, excerpt):
    body = json.dumps({"state": {"claim": claim, "excerpt": excerpt},
                       "questions": {"q": {"type": "choice", "criteria": CRITERIA,
                                           "instructions": "How does `excerpt` relate to `claim`?"}}}).encode()
    with urllib.request.urlopen(urllib.request.Request(URL, body, {"Content-Type": "application/json"}), timeout=30) as r:
        return json.load(r)["q"]


def main():
    jev = {}  # (session, claim) -> the reference verdict logged for it
    for line in open(HERE / "log.jsonl"):
        d = json.loads(line)
        # rows before the `backend` field existed: per-claim `confidence` logging was added while
        # the hook ran on Laya, so a row with confidence but no backend is a Laya row — not a reference
        if d.get("backend", "laya" if "confidence" in d else "jev") != "jev":
            continue
        for claim, p in d.get("verdicts", {}).items():
            jev[(d.get("session"), claim)] = p
    out = open(HERE / "laya_replay.jsonl", "w")
    rows = []
    for line in open(HERE / "sent.jsonl"):
        d = json.loads(line)
        for c in d.get("state", {}).get("claims", {}).values():
            ref = jev.get((d.get("session"), c["claim"]))
            if not ref:
                continue
            v = laya(c["claim"], c["excerpt"])
            row = {"claim": c["claim"], "jev": max(ref, key=ref.get), "jev_p": ref,
                   "laya": v["choice"], "laya_p": v["probabilities"], "laya_conf": v["confidence"]}
            rows.append(row); out.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"{len(rows)} claims with both verdicts")
    for label in ("supported", "contradicted", "not_addressed"):
        rs = [r for r in rows if r["jev"] == label]
        if not rs:
            continue
        conf = sorted(r["laya_conf"] for r in rs)
        agree = sum(r["laya"] == label for r in rs)
        print(f"  Jev={label:14} n={len(rs):4}  Laya agrees {agree/len(rs):.0%}  "
              f"Laya conf p10/p50/p90 = {conf[len(conf)//10]:.2f}/{conf[len(conf)//2]:.2f}/{conf[len(conf)*9//10]:.2f}")
    # the number that matters for blocking: Laya says contradicted while Jev didn't — at what confidence?
    fp = sorted(r["laya_conf"] for r in rows if r["laya"] == "contradicted" and r["jev"] != "contradicted")
    tp = sorted(r["laya_conf"] for r in rows if r["laya"] == "contradicted" and r["jev"] == "contradicted")
    if fp:
        print(f"  Laya-only 'contradicted' (false positives if Jev is right): n={len(fp)} conf p50={fp[len(fp)//2]:.2f} p90={fp[len(fp)*9//10]:.2f}")
    if tp:
        print(f"  Both 'contradicted': n={len(tp)} conf min={tp[0]:.2f} p50={tp[len(tp)//2]:.2f}")


if __name__ == "__main__":
    main()
