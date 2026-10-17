# research-answer

A Claude Code agent that answers a quantitative research question from the peer-reviewed literature,
and checks its own work. Every number in the final answer is tied to a verbatim quote that a script
found in the paper's full text, on the stated page.

```
/research-answer How much do cover crops change soil organic carbon?
```

## Quick start

You need [Claude Code](https://claude.com/claude-code) and Python 3.10+.

```bash
git clone https://github.com/<you>/research-answer.git
cd research-answer
claude
```

Then, inside Claude Code:

```
/research-answer How much do cover crops change soil organic carbon?
```

On the first run it creates `.venv/` and installs `requests`, `pymupdf` and `matplotlib`. A run
takes about 15–30 minutes, depending on how many papers have open-access full text.

Non-interactive, straight from the shell:

```bash
claude -p "/research-answer How much do cover crops change soil organic carbon?" --permission-mode acceptEdits
```

## Configuration

All configuration is through environment variables. Set them before running:

| Variable | Purpose | Example |
|---|---|---|
| `CONTACT_EMAIL` | Polite-pool requests to OpenAlex / Crossref; enables Unpaywall OA lookup | `you@university.edu` |
| `SCIHUB_URL` | Adds Sci-Hub as a last-resort PDF source (after all OA attempts fail) | `https://sci-hub.se` |
| `EZPROXY_PREFIX` | Wraps publisher URLs with your institution's EZproxy login prefix | `https://ezproxy.univ.edu/login?url=` |

PDF sources are tried in order: **OpenAlex OA locations → Semantic Scholar → Unpaywall → EZproxy → Sci-Hub**.

**Paper missing from the search results?** Pass its DOI directly — it bypasses the relevance-ranking
cutoff and is still Crossref-verified and screened by the model:

```bash
.venv/bin/python tools/ra.py search RUN --queries "..." \
    --seed-dois "10.1111/gcb.16632" "10.1038/s41467-021-27500-2"
```

## How it works

```
            question
               │
   ┌───────────┴────────────┐
   │                        │  (background)
   ▼                        ▼
 search  OpenAlex       baseline-answerer agent ── answers from memory, no tools
 + Crossref DOI check        │                      (the "just ask the chatbot" control)
   │                         │
 screen  (orchestrator reads titles + abstracts)
   │                         │
 retrieve  open-access PDFs → text, page by page
   │                         │
 extract   one evidence-extractor agent per paper, in parallel
   │         → rows: value, unit, depth, duration, verbatim quote, page
   │                         │
 verify    SCRIPT: is the quote in the paper? on that page? is the value inside the quote?
   │                         │
 review    one evidence-reviewer agent per paper, in parallel
   │         → accept / correct / reject (secondary citations, wrong outcome, wrong unit…)
   │                         │
 answer    orchestrator writes answer.md, citing rows as [P03-E1]
   │                         │
 check     SCRIPT: every cited row exists and was accepted; every number is in a cited row
   │                         │
   └──────── compare ────────┘
             SCRIPT: are the baseline's references real?
```

The split is deliberate. **Language models make the judgement calls**: queries, screening, reading,
reviewing, synthesis. **Scripts do everything that can be checked mechanically**: API calls, DOI
existence, downloads, string matching of quotes against the PDF, citation bookkeeping. The reviewer
agents never see the extractor's reasoning, only its output, so they serve as a second reader rather than
a rubber stamp.

### Three layers

**Orchestration layer — [`SKILL.md`](.claude/skills/research-answer/SKILL.md).** This file *is* the
`/research-answer` slash command. It tells the orchestrator (Claude) the exact sequence of steps to
run, which `ra.py` commands to call, and when to spawn subagents. The orchestrator makes every
judgement call: what queries to write, which papers to include, how to synthesise the answer.

**Deterministic layer — [`tools/ra.py`](tools/ra.py).** A plain Python script with no language-model
calls. It does everything that *can be checked mechanically*: querying OpenAlex and Crossref,
downloading PDFs, converting PDF pages to numbered text, checking that each extracted quote actually
occurs in the paper on the stated page, verifying that the answer's numbers match cited rows.

**Subagent layer — the `.claude/agents/` files.** Three specialised agents spawned by the
orchestrator during a run:

| Agent | File | What it does |
|---|---|---|
| Evidence extractor | [`evidence-extractor.md`](.claude/agents/evidence-extractor.md) | Reads one paper's full text; extracts value + unit + verbatim quote + page per result |
| Evidence reviewer | [`evidence-reviewer.md`](.claude/agents/evidence-reviewer.md) | Reads the same paper cold, without the extractor's reasoning; accepts / corrects / rejects each row |
| Baseline answerer | [`baseline-answerer.md`](.claude/agents/baseline-answerer.md) | Answers the question from memory with no tools — the "just ask the chatbot" control condition |

The extractor and reviewer never share a context window, which makes the reviewer a genuine second
reader rather than a rubber stamp.

## What a run produces

Everything lands in `runs/<date>_<slug>/`, as plain files you can open, grade and re-run:

| File | What it is |
|---|---|
| `answer.md` | The answer, with every number cited to an evidence row |
| `evidence.md` / `evidence.csv` | All extracted rows: accepted, corrected and rejected, with reasons |
| `fig_effects.png` | Accepted effect sizes, by unit and study type |
| `funnel.json` | Counts at each step: hits → candidates → full text → rows → verified → accepted |
| `answer_check.md` | Mechanical check of the answer's citations and numbers |
| `baseline.md`, `baseline_check.jsonl` | The no-tools answer, and which of its references are real |
| `grading.csv` | A random sample of rows for **you** to check against the PDFs |
| `to_download.md` | Papers without full text (closed access or download blocked), with links |
| `pdf/`, `text/` | Full texts. Kept local and git-ignored, because they are copyrighted |

## Evaluating it

The pipeline checks itself, but it cannot see what it never read, and a script cannot tell whether a
correctly quoted number answers the question. So evaluation has three layers:

1. **Mechanical** (free, automatic): quote match rate, value-in-quote rate, rows the reviewer rejected,
   and answer citations that fail `check-answer`. All of these are in `funnel.json`.
2. **Human** (about 20 minutes): fill in `grading.csv`. For each sampled row, open the PDF and mark:
   is the quote really there, is the value right, is it a cover-crop effect, is the context (depth,
   duration) right. This gives the agent's real error rate, including errors that slipped past the reviewer.
3. **Baseline comparison**: how many references in the no-tools answer are fabricated or carry the wrong
   DOI, and whether its headline numbers fall inside the range the verified evidence supports.

## Limitations

- **Closed-access papers are not downloaded automatically.** They are listed in `to_download.md`.
  Options: (1) set `SCIHUB_URL` or `EZPROXY_PREFIX` and re-run `fetch`; (2) download through your
  library and drop the PDF into `pdf/Pnn.pdf`, then re-run `fetch` to pick it up.
- Some publishers block scripted downloads even of open-access papers (rate limiting, CAPTCHA, IP
  blocks). Sci-Hub and EZproxy are tried as fallbacks if configured.
- Tables and figures come out of PDF text extraction poorly, so numbers that appear only in a figure are missed.
- Meta-analyses pool overlapping field studies. The answer is told not to treat them as independent,
  but nothing enforces this.
- Screening is done by a model reading abstracts. Relevant papers can be excluded, and you will not see that unless you read `screen.jsonl`.

## Responsible use

- Treat the output as a **reading list with pointers**, not as a result. Before any number goes into a
  paper or a decision, a human reads the source.
- Full texts stay on your machine. Do not commit `pdf/` or `text/`, or redistribute them.
- The baseline exists to show, on your own question, what goes wrong without retrieval and verification.
