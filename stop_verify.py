#!/usr/bin/env python3
"""Claude Code Stop hook: checks the final answer's factual claims against evidence actually
read this session, using TypeSafe's Jev (https://typesafe.ai) as a fast, cheap judge. Blocks
the turn from ending (decision=block) when a claim is contradicted by, or unsupported by,
anything that was read.

Sends EXCERPTS only — small, keyword-matched snippets of tool output per claim, plus a list
of file paths/commands used this session — never whole files. Every payload sent is logged to
sent.jsonl next to this script so you can audit exactly what left the machine.

Setup: run install.sh, or set TYPESAFE_API_KEY in the environment (or a .env file next to this
script) and register this script as a Stop hook command in Claude Code settings.

Env vars:
  JEV_HOOK=off           disable for this shell (useful for an A/B comparison, or sensitive work)
  JEV_THRESH             not-addressed threshold to flag a claim as unsupported (default 0.7)
  JEV_CONTRA             contradiction threshold to block (default 0.5)
  JEV_FACT               how confidently a sentence must read as a factual claim to be checked (default 0.7)
  JEV_FIRM               minimum Jev confidence in its own verdict to act on it (default 0.6)
  JEV_EVIDENCE_FLOOR     coverage floor below which "not addressed" means nothing relevant was
                         found at all, vs. relevant evidence existing but not proving the claim
                         (default 0.3) — see the comment above EVIDENCE_FLOOR for why this exists
  VERIFIER_BACKEND       which judge answers: "jev" (default). Always an explicit choice, never a
                         silent fallback. Each backend reads its own <NAME>_FIRM (e.g. JEV_FIRM):
                         confidence scales differ between models, so one threshold can't be shared.
"""
import json, os, re, sys, urllib.request, urllib.error, time, pathlib, math, collections

HERE = pathlib.Path(__file__).parent
LOG, SENT = HERE / "log.jsonl", HERE / "sent.jsonl"
API = "https://api.typesafe.ai/v1/systemone"
LINES_PER_CLAIM, CHARS_PER_CLAIM, LINE_CAP, DOC_CHARS = 12, 1200, 160, 3000
# hand-tuned starting points — every project's evidence shape differs, so these are meant to be
# overridden via env vars once you have a few dozen logged verdicts to tune against (see README)
THRESH = float(os.environ.get("JEV_THRESH", 0.7))
CONTRA = float(os.environ.get("JEV_CONTRA", 0.5))
FACT = float(os.environ.get("JEV_FACT", 0.7))
EVIDENCE_FLOOR = float(os.environ.get("JEV_EVIDENCE_FLOOR", 0.3))
STOP = set("this that with from have does into only also than then they were been what when which their about there these those would could should".split())


def _config(var, default=""):
    env = os.environ.get(var, "")
    if env:
        return env
    envfile = HERE / ".env"
    if envfile.exists():
        for line in envfile.read_text().splitlines():
            if line.startswith(var + "="):
                return line.split("=", 1)[1].strip()
    return default


def key():
    return _config("TYPESAFE_API_KEY")


# Read the same way as the key (env, then .env) so an installer can persist the choice — a shell
# `export` may not reach a hook launched from a Claude Code session started elsewhere.
VERIFIER_BACKEND = _config("VERIFIER_BACKEND", "jev")


def jev(state, questions):
    def post(st):
        body = json.dumps({"model": "jev-latest", "state": st, "questions": questions}).encode()
        # Cloudflare in front of api.typesafe.ai rejects urllib's default User-Agent with
        # "403 error code: 1010" (browser-signature ban); any explicit UA passes.
        req = urllib.request.Request(API, body, {"Authorization": f"Bearer {key()}", "Content-Type": "application/json",
                                                 "User-Agent": "clear-head"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)["answers"]
    try:
        return post(state)
    except urllib.error.HTTPError as e:
        # The same WAF also 403s (an HTML page) on bodies that look like command injection. A
        # coding session's command list trips it reliably — 5 commands like `curl -H`,
        # `cat /sys/...`, a heredoc did; 66KB of prose didn't. Retry once without that list; the
        # per-claim excerpts alone passed in every case measured. The verdicts then lack the
        # "what was run" context, which is a weaker check, not a wrong one.
        if e.code == 403 and "reads_this_session" in state:
            return post({**state, "reads_this_session": "(omitted: the API's firewall rejected the command list)"})
        raise


KEV_URL = f"http://127.0.0.1:{os.environ.get('KEV_PORT', '8009')}/v1/systemone"


def kev(state, questions):
    # Same wire contract as TypeSafe (https://github.com/jaredpalmer/kev), served locally by
    # `python -m kev.serve` — see install.sh --kev. Nothing leaves the machine on this backend.
    body = json.dumps({"model": "kev-latest", "state": state, "questions": questions}).encode()
    req = urllib.request.Request(KEV_URL, body, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=50) as r:
            return json.load(r)["answers"]
    except urllib.error.URLError as e:
        raise RuntimeError(f"VERIFIER_BACKEND=kev but {KEV_URL} isn't reachable ({e}); start the server "
                           "(see install.sh --kev)") from e


BACKENDS = {"jev": jev, "kev": kev}
# Kev is a 4B model trained on states of at most 384 tokens. Given the whole batched state (all
# claims, doc comments, nested `claims.cN.excerpt` refs) it returned near-identical verdicts for
# every claim (spread of `contradicted` 0.01-0.10 across 18 real claims) and hit its 8192-token
# row limit once the command list was included. One flat {claim, excerpt} call per claim is what
# it can read in full. Prefill-bound: ~1.5 s per claim with a real ~540-token excerpt on an M1
# Pro (0.45 s on a one-line one), plus ~10 s for the batched pass-1 — so install.sh registers the
# hook with a 180 s timeout for this backend instead of 60.
PER_CLAIM = {"kev"}
# Confidence scales differ per model, so each gets its own default. Kev's 0.5 comes from replaying
# 1309 logged claims: its "contradicted" verdicts agreed with Jev's most often at that threshold
# (47%, vs 27% at 0.15) — and a manual check of the disagreements found both judges wrong about
# equally often, so this is where its signal is, not proof it's right.
FIRM_DEFAULT = {"jev": 0.6, "kev": 0.5}
FIRM = float(os.environ.get(f"{VERIFIER_BACKEND.upper()}_FIRM", FIRM_DEFAULT.get(VERIFIER_BACKEND, 0.6)))


def judge(state, questions):
    """Single dispatch point: a new backend is one function with jev()'s signature and one entry
    in BACKENDS, not if/else scattered through main(). Unknown names fail loudly on purpose."""
    if VERIFIER_BACKEND not in BACKENDS:
        raise SystemExit(f"VERIFIER_BACKEND={VERIFIER_BACKEND!r} is not one of {sorted(BACKENDS)}")
    return BACKENDS[VERIFIER_BACKEND](state, questions)


def text_of(c):
    if isinstance(c, str):
        return c
    return "\n".join(b.get("text", "") for b in c if isinstance(b, dict) and b.get("type") == "text")


MAX_TURNS_BACK = int(os.environ.get("JEV_MAX_TURNS_BACK", 20))


def last_turn(path):
    """(final assistant text, evidence lines, reads) — evidence accumulates across recent turns
    (not just the latest one) so a recap of earlier work stays checkable; only the answer resets
    on a new user message. Bounded to the last MAX_TURNS_BACK user turns: unbounded accumulation
    in a long, multi-topic session lets a new claim match stale evidence from an unrelated earlier
    part of the conversation on generic keyword overlap alone — see README Known limits."""
    answer, all_lines, all_reads, turn, tool_desc = "", [], [], 0, {}
    for raw in open(path):
        try:
            d = json.loads(raw)
        except Exception:
            continue
        m = d.get("message", {}); c = m.get("content")
        if d.get("type") == "user":
            if isinstance(c, list) and any(b.get("type") == "tool_result" for b in c):
                for b in c:
                    if b.get("type") == "tool_result":
                        # tag each line with WHERE it came from (matched by tool_use_id, not
                        # position) — an anonymous line and a line tagged "[stop_verify.py]" are
                        # very different evidence; without the tag a claim about file A can be
                        # "confirmed" by a same-keyword line that actually came from file B
                        src = tool_desc.get(b.get("tool_use_id"), "")
                        tag = f"[{src}] " if src else ""
                        all_lines += [(turn, (tag + l.strip())[:LINE_CAP]) for l in text_of(b.get("content") or "").splitlines() if l.strip()]
            elif text_of(c or "").strip():
                answer = ""; turn += 1
        elif d.get("type") == "assistant":
            for b in m.get("content", []):
                if b.get("type") == "text" and len(b["text"]) > 200:
                    answer = b["text"]
                elif b.get("type") == "tool_use":
                    i = b.get("input", {})
                    desc = (i.get("file_path") or i.get("command") or i.get("query") or i.get("args") or "")
                    all_reads.append((turn, desc[:200]))
                    if b.get("id"):
                        tool_desc[b["id"]] = desc[:60]
    cutoff = turn - MAX_TURNS_BACK
    lines = [l for t, l in all_lines if t >= cutoff]
    reads = [r for t, r in all_reads if t >= cutoff]
    return answer, lines, reads


def sentences(t):
    t = re.sub(r"```.*?```", " ", t, flags=re.S)
    t = re.sub(r"[#*_`|]", " ", t)
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", t) if len(s.strip()) > 40]


def keywords(s):
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s).replace("_", " ").lower()
    # ascii identifiers (letters+digits, e.g. "sha256") | any OTHER script's letters run 3+ (e.g. Cyrillic —
    # a claim written in a non-Latin language previously got ZERO keywords and so zero coverage, always
    # below EVIDENCE_FLOOR, always blockable regardless of truth | bare numbers
    return {w for w in re.findall(r"[a-z][a-z0-9]{2,}|[^\W\da-z]{3,}|\d{2,}", s) if w not in STOP}


def excerpt(claim, lines, kws, idf):
    """Pick the tool-output lines most likely to bear on `claim`, and score how well the best
    single line actually covers it (used later to decide whether an absence verdict means
    "nothing relevant was read" vs. "relevant evidence exists but doesn't spell this out")."""
    kw = keywords(claim)

    def raw(n):  # unboosted overlap — used only to measure coverage, never to rank
        return sum(idf.get(w, 0) for w in kw & kws[n])

    def rank_score(n, l):
        # A keyword matched as the line's subject (its leading token — an identifier or name
        # the line is actually ABOUT) outweighs the same keyword found buried mid-line, e.g. as
        # a substring inside an unrelated description that happens to mention it in passing.
        # Without this boost, a line that only *mentions* the claim's subject can outscore the
        # line that actually defines or documents it.
        lead = keywords(l[:40])
        return sum(idf[w] * (3 if w in lead else 1) for w in kw & kws[n])

    scored = sorted(((rank_score(n, l), n, l) for n, l in enumerate(lines)), reverse=True)
    out, used = [], 0
    for sc, n, l in scored[:LINES_PER_CLAIM]:
        if sc == 0 or used + len(l) > CHARS_PER_CLAIM:
            break
        out.append(l); used += len(l)
    # Coverage = the BEST SINGLE LINE's idf-weighted overlap with the claim (unboosted — the
    # position boost above is for ranking, not for this). A union across the whole excerpt is
    # easy to fool: several claim keywords can each appear somewhere, in unrelated lines, and
    # add up to a high score without any single piece of evidence actually addressing the
    # claim's subject. One line that covers most of the claim together is the real "found it"
    # signal; scattered partial matches across many lines are not.
    total_w = sum(idf.get(w, 0) for w in kw)
    coverage = max((raw(n) for _, n, _ in scored[:LINES_PER_CLAIM]), default=0.0) / total_w if total_w > 0 else 0.0
    return out, coverage


def doc_lines(lines):
    """Module/item doc comments in the evidence — usually the highest-signal statements of
    design intent or known limitations in a codebase, worth sending on every claim."""
    seen, out, used = set(), [], 0
    for l in lines:
        core = re.sub(r"^\[[^\]]*\]\s*", "", l)              # strip the "[source] " tag
        core = re.sub(r"^\S*[:\-]\d+[:\-]\s*", "", core)      # strip a leading "path:NN:" grep prefix
        if re.match(r"(//[!/]|\"\"\"|#!)", core) and core not in seen and len(core) > 20:
            seen.add(core); out.append(core); used += len(core)
            if used > DOC_CHARS:
                break
    return out


def main():
    inp = json.load(sys.stdin)
    if os.environ.get("JEV_HOOK", "on") == "off" or inp.get("stop_hook_active"):
        return
    answer, lines, reads = last_turn(inp["transcript_path"])
    if not answer or not reads:
        return
    sents = sentences(answer)[:60]
    if not sents:
        return

    # Pass 1: which sentences are factual claims about the existing system, as opposed to
    # proposals, opinions, or a recap of the conversation itself?
    q1 = {str(i): {"type": "choice", "instructions": f"Sentence: {s}",
          "criteria": {"fact_about_existing_code": "asserts how the code/system currently is or behaves (present tense, checkable in the repo)",
                       "proposal_or_opinion": "recommends, proposes, predicts, or describes a design that does not exist yet",
                       "about_this_conversation": "describes what was done, found, built, or decided during this session, what the user should do next, or asserts that something was NOT done, tested, or verified (this session or in general) — there is no code to check a claim of absent action against",
                       "other": "general knowledge, meta commentary, headings, or list fragments"}}
          for i, s in enumerate(sents)}
    a1 = judge({"context": "Sentences from an AI assistant's answer about a software codebase. Classify each sentence "
                         "using the full answer for context: sentences inside a proposed design are proposals even if present tense.",
              "full_answer": answer[:12000]}, q1)
    fact_p = {i: a1[str(i)]["probabilities"].get("fact_about_existing_code", 0) for i in range(len(sents))}
    claims = list(enumerate(sents))  # verify every sentence, not just high-fact_p ones — a proposal can also be contradicted by the code
    if not claims:
        return

    kws = [keywords(l) for l in lines]
    df = collections.Counter(w for k in kws for w in k)
    idf = {w: math.log(len(lines) / (1 + c)) for w, c in df.items()}

    state = {"reads_this_session": reads,
             "note": "doc_comments = module/item doc comments from files read this session (design invariants). "
                     "Each claim has its own excerpt: tool-output lines sharing rare keywords with it. Judge each "
                     "claim against doc_comments + its excerpt + the reads list. not_addressed = nothing read bears on it.",
             "doc_comments": doc_lines(lines),
             "claims": {}}
    coverage = {}
    for i, s in claims:
        exc, cov = excerpt(s, lines, kws, idf)
        state["claims"][f"c{i}"] = {"claim": s, "excerpt": exc}
        coverage[i] = cov

    # Criteria wording follows TypeSafe's citation-check cookbook; nested-path references in
    # `instructions` follow their state guidance (see https://docs.typesafe.ai).
    criteria = {"supported": "the evidence states the claim or directly implies it is true",
                "contradicted": "the evidence states the opposite of the claim or implies it is false",
                "not_addressed": "the evidence does not address what the claim asserts, either way"}

    with open(SENT, "a") as f:
        f.write(json.dumps({"ts": time.time(), "session": inp.get("session_id"), "state": state}) + "\n")
    if VERIFIER_BACKEND in PER_CLAIM:
        # An empty excerpt list is read as evidence by a small model — Kev answered "contradicted"
        # at 0.85 to a claim with []; the same claim with an explicit note got "not addressed" 0.97.
        a2 = {f"c{i}": judge({"claim": s, "excerpt": state["claims"][f"c{i}"]["excerpt"]
                              or "(no tool output this session shares a keyword with this claim)"},
                             {"q": {"type": "choice", "criteria": criteria,
                                    "instructions": "How does `excerpt` relate to `claim`?"}})["q"]
              for i, s in claims}
    else:
        q2 = {f"c{i}": {"type": "choice", "criteria": criteria,
              "instructions": f"How do `doc_comments`, `claims.c{i}.excerpt` and `reads_this_session` relate to the claim `claims.c{i}.claim`?"}
              for i, s in claims}
        a2 = judge(state, q2)

    bad, soft = [], []
    for i, s in claims:
        r = a2[f"c{i}"]; p = r["probabilities"]
        if r.get("confidence", 1) < FIRM:
            continue  # a verdict the judge itself isn't confident in is for the log, not for blocking
        # coverage == 0: the excerpt shares not one keyword with the claim, so a "contradiction" is
        # about something else (a local model gave 0.78 for "Paris is the capital of France" vs
        # "the sky is blue"). Logged, never blocked — a contradiction needs evidence on the subject.
        if p.get("contradicted", 0) >= CONTRA and coverage[i] > 0:
            bad.append(("CONTRADICTED", s))
        elif fact_p[i] >= FACT and p.get("not_addressed", 0) >= THRESH:
            # A calibrated judge like Jev tells you whether text SUPPORTS a claim; it isn't built
            # to DERIVE an unstated fact (e.g. tracing what a loop actually iterates over) — see
            # https://docs.typesafe.ai/concepts/how-to-build-with-system-one.md. When relevant
            # evidence was genuinely found (high coverage) but Jev still can't confirm the claim,
            # that's usually this limit, not a real gap, so it shouldn't block. Low coverage means
            # nothing relevant was read at all, which is the actual "unverified claim" this hook
            # exists to catch.
            if coverage[i] < EVIDENCE_FLOOR:
                bad.append(("UNSUPPORTED", s))
            else:
                soft.append(("low-coverage, not blocked", s, coverage[i]))

    with open(LOG, "a") as f:
        f.write(json.dumps({"ts": time.time(), "session": inp.get("session_id"), "backend": VERIFIER_BACKEND,
                            "n_sent": len(sents), "n_claims": len(claims),
                            "blocked": bool(bad), "fact_p": {s: round(fact_p[i], 2) for i, s in claims},
                            "coverage": {s: round(coverage[i], 2) for i, s in claims}, "soft": len(soft),
                            "verdicts": {s: a2[f"c{i}"]["probabilities"] for i, s in claims},
                            "confidence": {s: a2[f"c{i}"].get("confidence") for i, s in claims}}) + "\n")

    if bad:
        print(json.dumps({"decision": "block", "reason":
            "Jev claim check: these statements about the codebase are contradicted by, or absent from, "
            "what you read this session. Verify each with a read/grep, or rewrite it as an explicit "
            "assumption, then finish.\n" + "\n".join(f"- [{k}] {s}" for k, s in bad)}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        with open(LOG, "a") as f:
            f.write(json.dumps({"ts": time.time(), "error": str(e)}) + "\n")
        # fail-open by default (a broken checker shouldn't block real work) — flip with
        # JEV_FAIL_CLOSED=1 if you'd rather know the check didn't run than risk it silently
        # not running (pattern borrowed from jev-guard's JEV_GUARD_FAIL_CLOSED)
        if os.environ.get("JEV_FAIL_CLOSED"):
            print(json.dumps({"decision": "block", "reason":
                f"Jev claim check itself failed and JEV_FAIL_CLOSED is set: {e}\n"
                "Unset JEV_FAIL_CLOSED to fail open instead, or fix the underlying error."}))
