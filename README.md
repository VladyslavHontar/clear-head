# clear-head

A Claude Code [Stop hook](https://docs.claude.com/en/docs/claude-code/hooks) that checks the
factual claims in an AI assistant's answer against the evidence it actually read that session,
using [TypeSafe's Jev](https://typesafe.ai) as a fast, cheap, calibrated judge. If a claim is
contradicted by, or unsupported by, what was read, the turn is blocked with the specific claims
listed, and the assistant has to verify them or mark them as assumptions before finishing.

## Why

LLMs state wrong things about a codebase with the same confident tone as right things. That's
expensive precisely because it doesn't sound like a guess. This hook runs on every turn without
you asking for it, and turns "I read the code and I'm sure" into something that's actually checked
against what got read.

## What it sends, and what it doesn't

Per claim, it sends only the tool-output *lines* that share keywords with that claim — not whole
files — plus a list of file paths and commands used, and any doc comments found in what was read.
Every payload sent is logged to `sent.jsonl` next to the script, so you can see exactly what left
the machine on every stop. Read [`stop_verify.py`](stop_verify.py) — it's a few hundred lines, not
a black box.

This still means excerpts of your code and your session leave your machine and go to TypeSafe's
API. Don't install this on a repo you wouldn't hand to a third-party API.

## Install

```bash
git clone https://github.com/VladyslavHontar/clear-head
cd clear-head
./install.sh              # this user, every project
./install.sh --project    # just the current project
```

You'll be prompted for a TypeSafe API key ([get one here](https://typesafe.ai)) unless
`TYPESAFE_API_KEY` is already set in your environment. The installer verifies the key works
before it finishes.

### Local backend: Kev (no API key, nothing leaves your machine)

```bash
./install.sh --kev
```

[Kev](https://github.com/jaredpalmer/kev) is an Apache-2.0 model family (0.8B/4B/9B, Qwen3.5
base) that speaks TypeSafe's exact wire contract — same `{state, questions}`, same
`probabilities` + `confidence` — served locally by `python -m kev.serve`. `--kev` clones it into
the hook directory, runs `uv sync --extra serve` (MLX on Apple Silicon, CUDA/ROCm elsewhere),
starts the 4B server on `127.0.0.1:8009` and persists `VERIFIER_BACKEND=kev` in `.env`. Needs
`uv`, `git`, ~10 GB of RAM while serving, and a first-run download of ~8 GB. The server doesn't
survive a reboot; restart it with `~/.claude/hooks/jev/kev_serve.sh &` or wire that into whatever
you already use to keep background processes alive. Use that script rather than `python -m
kev.serve` directly: on Apple Silicon MLX's buffer cache is unbounded by default and the server
grew from 18 GB to 24 GB over a day of use — the script caps it at 1 GB, which holds it at ~10 GB
with no slowdown.

**What was measured, on this project's own sessions** (`replay.py`, 1309 claims with a Jev
verdict as reference; details in the source comments):
- Kev-4b agrees with Jev on 61% of "supported", 75% of "not addressed", 12% of "contradicted".
  A manual review of the disagreements found the two judges wrong about equally often — Kev
  caught a real contradiction Jev called supported (tests that silently skip without an env var)
  and Jev caught one Kev missed. Treat them as different judges, not a copy and an original.
- Kev's known false positives: a claim whose excerpt merely *contains* a negated word ("no
  silent fallback" vs a log line saying "fallback works") comes back "contradicted" at 0.98; and
  an empty excerpt reads as evidence. The hook sends an explicit note instead of an empty list,
  and never blocks on a contradiction when the excerpt shares no keyword with the claim.
- Speed is the real cost. One call per claim (batching all claims into one state — the Jev
  path — gave near-identical verdicts for every claim and overran Kev's 8192-token row limit
  once the command list was included), and each call is prefill-bound: ~1.5 s for a claim with
  a real ~540-token excerpt on an M1 Pro, plus ~10 s for the sentence-classification pass. An
  18-sentence answer took 44 s end to end where Jev took 1 s, so `--kev` registers the hook with
  a 180 s timeout instead of 60. Expect a noticeable pause at the end of long answers; a faster
  GPU changes this, the code doesn't. Kev-0.8b is 5× faster and not usable: it called an
  off-topic excerpt "supported" 0.90.

## Uninstall

```bash
./uninstall.sh              # or --project, matching how you installed it
```

Unregisters the hook from Claude Code settings and optionally deletes the installed copy.

## Use

Nothing to call — it runs automatically at the end of a turn that produced a substantial answer
and used at least one tool. Most turns it checks silently and you see nothing. When it blocks,
you'll see:

```
Jev claim check: these statements about the codebase are contradicted by, or absent from,
what you read this session. Verify each with a read/grep, or rewrite it as an explicit
assumption, then finish.
- [CONTRADICTED] ...
- [UNSUPPORTED] ...
```

Disable it for one shell — useful for sensitive work, or to A/B it against your own workflow:

```bash
JEV_HOOK=off claude
```

## Tuning

Every project's evidence shape is different — a config-heavy repo, a dense codebase, a
documentation-first project will all need different thresholds. Start with the defaults, look at
`log.jsonl` after a few dozen stops, and adjust:

| Env var | Default | What it controls |
|---|---|---|
| `JEV_CONTRA` | 0.5 | Contradiction probability that triggers a block. Lower = stricter. |
| `JEV_THRESH` | 0.7 | "Not addressed" probability that counts as unsupported. |
| `JEV_FACT` | 0.7 | How confidently a sentence must read as a factual claim to be checked at all. |
| `JEV_FIRM` | 0.6 | Minimum confidence in Jev's own verdict before acting on it. Below this, it's logged but never blocks. |
| `JEV_EVIDENCE_FLOOR` | 0.3 | See below. |
| `JEV_MAX_TURNS_BACK` | 20 | How many user turns of evidence to keep. Lower = less stale-evidence noise in a long session, but a recap further back than this stops being checkable. |
| `JEV_FAIL_CLOSED` | unset | If the checker itself throws (network down, bad key, malformed input), fail open by default — never block real work over a broken checker. Set this if you'd rather know the check didn't run than risk it silently not running. |
| `VERIFIER_BACKEND` | `jev` | `jev` (TypeSafe's API) or `kev` (local). Always explicit — an unknown name fails loudly rather than falling back. Persisted in `.env` by `install.sh --kev`. |
| `KEV_FIRM` | 0.5 | `JEV_FIRM` for the Kev backend — separate because the two models' confidence scales differ. 0.5 is where Kev's "contradicted" verdicts agreed with Jev most often on 1309 replayed claims (47%, vs 27% at 0.15). |
| `KEV_PORT` | 8009 | Where `kev.serve` listens (always on 127.0.0.1). |
| `JEV_MUTABLE` | 0.6 | How surely a sentence must read as a claim about *mutable outside state* — a PR or issue's status, CI, a running process, a remote branch — for the STALE rule below to apply. |
| `JEV_STALE_TURNS` | 3 | Such a claim blocks as `[STALE]` when the freshest tool-output line matching it is this many user turns old, or nothing matches at all. |

**On `JEV_EVIDENCE_FLOOR`:** a calibrated judge like Jev tells you whether the evidence you gave
it *supports* a claim — it isn't built to *derive* an unstated fact, like tracing exactly what a
loop iterates over. When the excerpt genuinely contains the relevant code but Jev still can't
confirm the specific claim, that's usually this limit, not a real gap, and it shouldn't block.
Coverage measures how well the best single piece of retrieved evidence actually addresses the
claim; below the floor, nothing relevant was found at all, which is the real "unverified claim"
this hook exists to catch. See the comment above `EVIDENCE_FLOOR` in the source for the full
reasoning and a worked example.

## How it works, briefly

1. Pull the final answer and every tool-output line from the current session's transcript.
2. Split the answer into sentences; ask Jev to classify each as a factual claim about the
   existing system, a proposal, a recap of the conversation, or neither.
3. For every factual claim, retrieve the tool-output lines most likely to bear on it (a small,
   keyword-and-frequency retriever — see `excerpt()`), plus any doc comments found this session.
   Each line is tagged with the file or command that produced it, matched by tool call id rather
   than position — so a line from one file can't get mistaken for evidence about another. This
   doesn't disambiguate *within* a single command's combined output (e.g. two refs in one `git
   log` dump still need the position-boost in `excerpt()` to tell apart).
4. Ask Jev whether the evidence supports, contradicts, or doesn't address each claim.
5. Block the turn if anything is contradicted, or unsupported with no relevant evidence found at
   all (see the coverage note above).
6. Separately, in step 2, ask whether each sentence asserts the *current state of something
   outside the repo that changes on its own* — "the PR is still open", "the server is running",
   "CI is green". If it does and the freshest tool output matching it is `JEV_STALE_TURNS` old
   (or there is none), block it as `[STALE]` regardless of the judge's verdict: the fault is the
   missing check, not the wording. This came from a real miss — three PRs described as "open,
   waiting for merge" hours after the user had merged them, with nothing in the session having
   checked; both judges let it through because an old `pull/36` URL from a `git push` counted as
   on-topic evidence. On 1585 logged sentences the rule fires on that one and nothing else.

## Known limits

- The retriever is keyword-based, not semantic — it can miss a paraphrase, and it can be misled
  by an identifier that appears in an unrelated context sharing the same words. See the comments
  in `excerpt()` for the specific failure modes already found and fixed.
- It only checks claims about the *current* session's evidence. A claim that's true but wasn't
  read this session will still get flagged as unsupported.
- Evidence accumulates across a bounded window of recent turns (`JEV_MAX_TURNS_BACK`, default 20),
  not the whole session, so a recap of earlier work stays checkable without pulling in everything
  ever read. In a long session that covers several unrelated topics, a new claim can still match
  stale evidence from earlier within that window on generic keyword overlap alone — the window
  bounds this, it doesn't eliminate it, since there's still no topic-boundary signal. If you hit
  this, it usually shows up as a block whose cited evidence is visibly about something else.
- The keyword matcher covers Latin identifiers and any other script's letters (e.g. Cyrillic) as
  of the current version — but it's still exact-word matching, not semantic, so it still misses a
  claim phrased differently from its supporting evidence, in any language.
- It's excerpts, not full files — see "What it sends" above.
- TypeSafe's API sits behind a Cloudflare firewall that rejects request bodies which look like
  command injection — and a coding session's list of commands run (`curl -H ...`, `cat /sys/...`,
  heredocs) trips it reliably. On that 403 the hook retries once without the command list, so the
  judge sees each claim's excerpts and doc comments but not what was run to get them: a weaker
  check for that turn, not a wrong one. Before this, the same firewall also rejected Python's
  default User-Agent outright, and since the hook fails open by default (`JEV_FAIL_CLOSED`), it
  had silently stopped checking anything — set `JEV_FAIL_CLOSED` if you'd rather be told.

## License

MIT — see [LICENSE](LICENSE).
