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

### Laya backend (local, free, no API key)

```bash
./install.sh --laya
```

Uses [Laya](https://github.com/NandhaKishorM/laya) — a local, Apache-2.0, self-hostable model
with the same choice/score/noul primitives as Jev — instead of TypeSafe's cloud API. Nothing
leaves your machine on this backend. Its own [benchmarks](https://github.com/NandhaKishorM/laya/blob/main/BENCHMARKS.md)
compare it against Jev, but by their own admission those numbers are against third-party
published Jev figures, not a controlled head-to-head — treat them as indicative, not settled.

`--laya` installs `pip install laya` (downloads model weights, several hundred MB to a few GB
depending on checkpoint) and starts `laya_server.py` in the background. **Why a server and not
a plain library import:** the hook runs as a fresh process on every Claude Code turn. Importing
Laya and loading its weights inside `stop_verify.py` directly would pay that load cost — real
seconds, more on CPU — on every single stop. `laya_server.py` loads the model once and stays
warm; the hook just makes a fast localhost call, the same latency shape as the Jev backend.

The server doesn't survive a reboot. Restart it with:
```bash
python3 ~/.claude/hooks/jev/laya_server.py &
```
or wire it into your own startup process (a `launchd`/`systemd` user service, tmux session,
whatever you already use to keep background processes alive across reboots — not something this
project prescribes for you). It only binds to `127.0.0.1` — leave `LAYA_HOST` alone unless you
specifically want to serve inference to your network, which is a different, larger decision than
this hook makes for you.

Switch backends any time by editing `VERIFIER_BACKEND` in the installed `.env` (`jev` or
`laya`), or per-shell with `VERIFIER_BACKEND=laya`.

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
| `VERIFIER_BACKEND` | `jev` | `jev` (cloud) or `laya` (local). Persisted in `.env` by `install.sh --laya` the same way as the API key — see the Laya section above. |
| `LAYA_HOST` / `LAYA_PORT` | `127.0.0.1` / `8787` | Where `laya_server.py` listens and where the hook looks for it. Keep the host local. |

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

## License

MIT — see [LICENSE](LICENSE).
