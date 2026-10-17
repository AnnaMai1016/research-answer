---
name: research-answer
description: Answer a quantitative research question from the peer-reviewed literature with a self-checking agent pipeline (search → screen → retrieve → extract → verify → review → answer), where every number is tied to a quote that a script has found in the paper. Use when the user runs /research-answer or asks for a literature-backed, quote-verified answer.
argument-hint: "<question>  e.g. How much do cover crops change soil organic carbon?"
---

# /research-answer

Question: **$ARGUMENTS**

You are the orchestrator. You make the judgement calls (queries, screening, synthesis); the script
`tools/ra.py` does everything that must not be left to a language model (API calls, DOI checks,
downloads, checking quotes against the PDF text). Subagents read papers and review each other's work.

Ground rules for the whole run:
- **Never state a number that is not in an accepted evidence row.** No numbers from memory.
- Do not read PDFs yourself to extract evidence — that is the extractor's job, so that the reviewer is independent.
- Every step writes a file in the run folder. If a step fails, say so and continue with what you have; never paper over a gap.
- Keep the user posted with one short line per step (e.g. "Search: 87 hits → 54 candidates").

If no question was given, ask for one and stop.

## 0. Setup

```bash
test -x .venv/bin/python || (python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt)
.venv/bin/python tools/ra.py new "$ARGUMENTS"
```

The last line printed is the run folder, written `RUN` below (e.g. `runs/2026-10-18_cover-crops-change-soil-organic-carbon`).

## 1. Baseline (in the background)

Launch the **baseline-answerer** subagent now, in the background, with the question and RUN. It answers
from memory only — the "just ask the chatbot" condition we compare against. Do not read its answer
until step 8, and do not let it influence your queries or screening.

## 2. Search

Write 4–6 English queries for OpenAlex (3–8 words each, no boolean operators). Cover: the direct effect,
meta-analyses / global syntheses, long-term field experiments, and the main moderators (soil depth,
duration, climate, cover crop type). Then:

```bash
.venv/bin/python tools/ra.py search RUN --queries "q1" "q2" "q3" "q4"
```

The script drops any DOI Crossref does not know.

## 3. Screen

Read `RUN/candidates.md` in full. Write `RUN/screen.jsonl`, one line per candidate:

```json
{"cand": "C07", "include": true, "reason": "global meta-analysis of SOC under cover crops, reports Mg C/ha/yr"}
```

Include papers that report a **quantitative** effect of cover crops (vs. no cover crop / fallow) on the
outcome in the question. Prefer, in order: meta-analyses, long-term field experiments, regional
syntheses. Exclude papers that are off-topic, purely modelling with no observed comparison, or only
qualitative. Put the included papers in priority order (the best first) — fetch stops after `--max`
full texts. Aim for roughly 20 included so that ~10–15 end up with full text.

## 4. Retrieve

```bash
.venv/bin/python tools/ra.py fetch RUN --max 15
```

Closed-access papers are listed, not downloaded. Tell the user which important ones were closed; if they
drop a PDF into `RUN/pdf/Pnn.pdf` by hand, re-running fetch picks it up.

## 5. Extract (parallel subagents)

For each paper in `RUN/papers.jsonl` that has `pages`, launch one **evidence-extractor** subagent, all in
the same message so they run in parallel (batches of at most 8). Give each: the question, RUN, and the
paper id (e.g. `P03`). Each writes `RUN/extract/P03.jsonl`.

## 6. Verify (script)

```bash
.venv/bin/python tools/ra.py verify RUN
```

This checks, without any model: does each quote occur in the paper's text, on the stated page, and
does the extracted value appear inside the quote. Report the counts to the user.

## 7. Review (parallel subagents)

For each paper with an extract file, launch one **evidence-reviewer** subagent (parallel, batches of ≤8),
giving it the question, RUN and paper id. It reads the paper and the rows with their verify results and
writes `RUN/review/P03.jsonl`. Then:

```bash
.venv/bin/python tools/ra.py report RUN
```

## 8. Answer

Read `RUN/evidence.md` and `RUN/evidence.csv`. Write `RUN/answer.md`:

```markdown
# <question>

**Short answer.** 2–4 sentences with the headline numbers and their range, each number followed by the row it comes from, e.g. "0.32 Mg C ha⁻¹ yr⁻¹ [P02-E1]".

## What the evidence says
Group by kind of evidence (meta-analyses vs. individual experiments) and by unit (rates vs. % change);
do not average across different units, depths or designs. Point out where meta-analyses overlap
(they may pool the same field studies — do not count them as independent).

## What it depends on
Moderators the rows support: depth, duration, climate, cover crop species, tillage, baseline SOC.

## Caveats
Closed-access papers not read, sampling-depth issues (shallow sampling overstates gains), equivalent soil
mass vs. fixed depth, publication bias, anything the reviewer flagged.

## Evidence
Pointer to evidence.md and the figure (fig_effects.png) if it exists.
```

Rules: cite only rows with `accepted = True`, as `[Pnn-En]`. Every sentence with a number needs a citation in that sentence.

Then run the check and fix every problem it lists (rewrite the sentence, or remove the number), until it passes:

```bash
.venv/bin/python tools/ra.py check-answer RUN
```

## 9. Compare with the baseline

Wait for the baseline agent if it is still running, then:

```bash
.venv/bin/python tools/ra.py check-baseline RUN
```

## 10. Wrap up

Show the user, briefly:
1. The short answer (copied from answer.md).
2. The funnel from `RUN/funnel.json`: hits → candidates → screened in → full text → rows extracted → quotes verified → accepted.
3. Pipeline vs. baseline: how many baseline references were fabricated or wrong, and whether its headline numbers fall inside the range the evidence supports.
4. What still needs a human: `RUN/grading.csv` (a random sample of rows to check against the PDFs), and the closed-access papers.

Links to `RUN/answer.md`, `RUN/evidence.md`, and `RUN/fig_effects.png`.
