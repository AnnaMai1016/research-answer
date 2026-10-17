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

Optional: `export CONTACT_EMAIL=you@university.edu` puts API requests in OpenAlex's and Crossref's
polite pool, and also asks Unpaywall for open-access copies.

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

| Piece | File |
|---|---|
| Orchestrator (the slash command) | [.claude/skills/research-answer/SKILL.md](.claude/skills/research-answer/SKILL.md) |
| Extractor agent | [.claude/agents/evidence-extractor.md](.claude/agents/evidence-extractor.md) |
| Reviewer agent | [.claude/agents/evidence-reviewer.md](.claude/agents/evidence-reviewer.md) |
| Baseline agent | [.claude/agents/baseline-answerer.md](.claude/agents/baseline-answerer.md) |
| Deterministic tools | [tools/ra.py](tools/ra.py) |

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

- **It only reads open access.** Closed-access papers, often the key meta-analyses, are listed in
  `to_download.md`. Drop PDFs from your library into `pdf/Pnn.pdf` and re-run `fetch`.
- Some publishers block scripted downloads even of open-access papers.
- Tables and figures come out of PDF text extraction poorly, so numbers that appear only in a figure are missed.
- Meta-analyses pool overlapping field studies. The answer is told not to treat them as independent,
  but nothing enforces this.
- Screening is done by a model reading abstracts. Relevant papers can be excluded, and you will not see that unless you read `screen.jsonl`.

## Responsible use

- Treat the output as a **reading list with pointers**, not as a result. Before any number goes into a
  paper or a decision, a human reads the source.
- Full texts stay on your machine. Do not commit `pdf/` or `text/`, or redistribute them.
- The baseline exists to show, on your own question, what goes wrong without retrieval and verification.
