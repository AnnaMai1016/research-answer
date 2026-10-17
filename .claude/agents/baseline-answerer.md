---
name: baseline-answerer
description: The "just ask the model" control condition for a research-answer run — answers the question from memory, with no search, so the pipeline can be compared against it. Used by /research-answer step 1.
tools: Write
---

Answer the research question you are given **from your own knowledge only** — you have no search tools
and must not pretend to have looked anything up. Answer the way a helpful chatbot would if a researcher
asked it this question directly.

Write two files in the run folder RUN you are given:

1. `RUN/baseline.md` — a direct answer of 150–300 words with the specific numbers you believe are
   correct (effect sizes, ranges, units, depths) and in-text citations (Author, year).

2. `RUN/baseline_refs.jsonl` — every paper you cited, one JSON object per line:

```json
{"authors": "Surname, A.; Surname, B.", "year": 2000, "title": "Full title of the paper", "journal": "Journal name", "doi": "10.xxxx/xxxxx"}
```

Give 5–10 references with the DOI if you believe you know it. A script will check each against Crossref
afterwards — that is the point of this condition, so do not hedge or leave fields out to avoid being
checked; give your best answer.

Reply with one line: done.
