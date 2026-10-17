"""research-answer: the deterministic half of the /research-answer agent.

Claude (the agent) decides what to search, which papers to keep, what each paper says, and how
to answer. This script does everything that should NOT be left to a language model: talking to
the literature APIs, checking that DOIs exist, downloading open-access PDFs, and checking that
every quote the agent extracted really appears in the paper.

    python tools/ra.py new "How much do cover crops change soil organic carbon?"
    python tools/ra.py search  RUN --queries "cover crop soil organic carbon meta-analysis" ...
    python tools/ra.py fetch   RUN [--max 15]
    python tools/ra.py verify  RUN
    python tools/ra.py report  RUN
    python tools/ra.py check-answer   RUN
    python tools/ra.py check-baseline RUN

RUN is a folder under runs/, created by `new`. Every step reads and writes plain files there,
so each step can be inspected, re-run or graded by hand.
"""

from __future__ import annotations

import argparse
import csv
import difflib
import json
import os
import random
import re
import sys
import time
import unicodedata
from datetime import date
from pathlib import Path
from urllib.parse import quote

import requests

ROOT = Path(__file__).resolve().parent.parent
RUNS = ROOT / "runs"
OPENALEX = "https://api.openalex.org/works"
CROSSREF = "https://api.crossref.org/works/"
MAILTO = os.environ.get("CONTACT_EMAIL", "")  # optional: OpenAlex/Crossref polite pool, Unpaywall
SCIHUB_URL = os.environ.get("SCIHUB_URL", "")         # e.g. https://sci-hub.se or local instance URL
EZPROXY_PREFIX = os.environ.get("EZPROXY_PREFIX", "")  # e.g. https://ezproxy.univ.edu/login?url=
UA_API = f"research-answer/0.1 (mailto:{MAILTO})" if MAILTO else "research-answer/0.1"
UA_BROWSER = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

api = requests.Session()
api.headers["User-Agent"] = UA_API


# ---------- small helpers ----------

def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for i, line in enumerate(path.read_text("utf-8").splitlines(), 1):
        if line.strip():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError as e:
                sys.exit(f"{path.relative_to(ROOT)} line {i} is not valid JSON: {e}")
    return rows


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), "utf-8")


def run_dir(arg: str) -> Path:
    p = Path(arg)
    p = p if p.is_absolute() else (ROOT / p if (ROOT / p).exists() else RUNS / p)
    if not (p / "question.md").exists():
        sys.exit(f"{arg} is not a run folder (no question.md). Create one with: ra.py new \"QUESTION\"")
    return p


def slugify(text: str, n: int = 6) -> str:
    words = re.findall(r"[a-z0-9]+", text.lower())
    stop = {"how", "much", "do", "does", "the", "a", "an", "of", "in", "on", "to", "is", "are", "what", "and", "for", "by"}
    return "-".join([w for w in words if w not in stop][:n]) or "question"


def norm(text: str) -> str:
    """Normalise PDF text and quotes the same way: ligatures, dashes, minus signs, hyphenation, spaces."""
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"<<<PAGE \d+>>>", " ", text)
    text = re.sub(r"[‐-―−‒]", "-", text)
    text = text.replace(" ", " ").replace("’", "'").replace("‘", "'")
    text = re.sub(r"[“”]", '"', text)
    text = re.sub(r"-\s*\n\s*", "", text)  # words split across lines
    return re.sub(r"\s+", " ", text).strip().lower()


def alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", norm(text))


def numbers_in(text: str) -> list[str]:
    t = norm(text).replace(",", "")
    return re.findall(r"\d+(?:\.\d+)?", t)


# ---------- new ----------

def cmd_new(a) -> int:
    folder = RUNS / f"{date.today():%Y-%m-%d}_{slugify(a.question)}"
    if folder.exists() and not a.reuse:
        k = 2
        while (folder.parent / f"{folder.name}-{k}").exists():
            k += 1
        folder = folder.parent / f"{folder.name}-{k}"
    for sub in ("pdf", "text", "extract", "review"):
        (folder / sub).mkdir(parents=True, exist_ok=True)
    (folder / "question.md").write_text(f"# Question\n\n{a.question}\n", "utf-8")
    print(folder.relative_to(ROOT))
    return 0


# ---------- search ----------

def abstract_from_index(inv: dict | None) -> str:
    if not inv:
        return ""
    return " ".join(w for _, w in sorted((i, w) for w, idx in inv.items() for i in idx))


def search_openalex(query: str, since: int, n: int) -> list[dict]:
    params = {
        "search": query,
        "filter": f"from_publication_date:{since}-01-01,type:article|review,has_doi:true",
        "per_page": n,
        "select": "id,doi,title,publication_year,primary_location,cited_by_count,authorships,"
                  "abstract_inverted_index,type,open_access,best_oa_location,relevance_score",
    }
    if MAILTO:
        params["mailto"] = MAILTO
    r = api.get(OPENALEX, params=params, timeout=30)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        src = (w.get("primary_location") or {}).get("source") or {}
        oa = w.get("open_access") or {}
        out.append({
            "openalex": w["id"].rsplit("/", 1)[-1],
            "doi": (w.get("doi") or "").replace("https://doi.org/", "").lower(),
            "title": re.sub(r"<[^>]+>", "", w.get("title") or ""),
            "authors": [x["author"]["display_name"] for x in w.get("authorships", [])[:4] if x.get("author")],
            "year": w.get("publication_year"),
            "venue": src.get("display_name") or "",
            "type": w.get("type"),
            "cited_by": w.get("cited_by_count", 0),
            "oa_status": oa.get("oa_status") or "closed",
            "license": (w.get("best_oa_location") or {}).get("license") or "",
            "abstract": abstract_from_index(w.get("abstract_inverted_index"))[:1200],
            "score": round(w.get("relevance_score") or 0, 1),
        })
    return out


def fetch_by_doi(doi: str) -> dict | None:
    """Fetch one paper's metadata from OpenAlex by exact DOI (bypasses ranking cutoffs)."""
    doi = doi.lower().removeprefix("https://doi.org/")
    params = {"select": "id,doi,title,publication_year,primary_location,cited_by_count,"
                        "authorships,type,open_access,best_oa_location,abstract_inverted_index"}
    if MAILTO:
        params["mailto"] = MAILTO
    try:
        r = api.get(f"{OPENALEX}/doi:{quote(doi, safe='/()')}", params=params, timeout=30)
        if r.status_code != 200:
            return None
        w = r.json()
        src = (w.get("primary_location") or {}).get("source") or {}
        oa = w.get("open_access") or {}
        return {
            "openalex": w["id"].rsplit("/", 1)[-1],
            "doi": (w.get("doi") or "").replace("https://doi.org/", "").lower(),
            "title": re.sub(r"<[^>]+>", "", w.get("title") or ""),
            "authors": [x["author"]["display_name"] for x in w.get("authorships", [])[:4] if x.get("author")],
            "year": w.get("publication_year"),
            "venue": src.get("display_name") or "",
            "type": w.get("type"),
            "cited_by": w.get("cited_by_count", 0),
            "oa_status": oa.get("oa_status") or "closed",
            "license": (w.get("best_oa_location") or {}).get("license") or "",
            "abstract": abstract_from_index(w.get("abstract_inverted_index"))[:1200],
            "score": 0.0,
        }
    except (requests.RequestException, ValueError):
        return None


def verify_doi(doi: str) -> dict | None:
    """Crossref record for a DOI, or None if the DOI does not exist."""
    if not doi:
        return None
    try:
        r = api.get(CROSSREF + quote(doi, safe="/()"), timeout=20)
        if r.status_code == 200:
            m = r.json()["message"]
            return {"title": (m.get("title") or [""])[0],
                    "year": ((m.get("issued") or {}).get("date-parts") or [[None]])[0][0],
                    "venue": (m.get("container-title") or [""])[0]}
        # DataCite DOIs (Zenodo, datasets) are not in Crossref; fall back to the resolver
        r = api.head(f"https://doi.org/{doi}", allow_redirects=False, timeout=20)
        if r.status_code in (301, 302, 303):
            return {"title": "", "year": None, "venue": ""}
    except (requests.RequestException, ValueError, KeyError):
        pass
    return None


def cmd_search(a) -> int:
    run = run_dir(a.run)
    queries = a.queries
    (run / "queries.json").write_text(json.dumps(queries, indent=2), "utf-8")
    hits: dict[str, dict] = {}
    for q in queries:
        try:
            res = search_openalex(q, a.since, a.per_query)
        except requests.RequestException as e:
            print(f"[warn] OpenAlex failed for '{q}': {e}", file=sys.stderr)
            continue
        for w in res:
            if w["doi"] and w["title"]:
                hits.setdefault(w["doi"], w | {"queries": []})["queries"].append(q)
        print(f"[search] {len(res):>3} hits  {q}")
        time.sleep(0.5)

    for doi in (a.seed_dois or []):
        doi_clean = doi.lower().removeprefix("https://doi.org/")
        if doi_clean in hits:
            print(f"[seed]  already in hits: {doi_clean}")
            continue
        w = fetch_by_doi(doi_clean)
        if w and w["title"]:
            hits[doi_clean] = w | {"queries": ["[seed]"]}
            print(f"[seed]  {doi_clean} — {w['title'][:60]}")
        else:
            print(f"[seed]  not found in OpenAlex: {doi_clean}", file=sys.stderr)
        time.sleep(0.2)

    kept, dropped = [], 0
    for w in sorted(hits.values(), key=lambda w: (-len(w["queries"]), -w["score"])):
        cr = verify_doi(w["doi"])
        if cr is None:
            print(f"[drop] DOI not in Crossref: {w['doi']}")
            dropped += 1
            continue
        kept.append(w | {"doi_verified": True})
        time.sleep(0.2)
    for i, w in enumerate(kept, 1):
        w["cand"] = f"C{i:02d}"
    write_jsonl(run / "candidates.jsonl", kept)

    lines = [f"# Candidates ({len(kept)} papers, DOIs verified in Crossref)", "",
             "Screen these for the question. `oa` = open-access status (closed papers cannot be downloaded).", ""]
    for w in kept:
        lines += [f"## {w['cand']} · {w['title']}",
                  f"{', '.join(w['authors'][:3])} ({w['year']}) *{w['venue']}* · {w['type']} · cited {w['cited_by']} · "
                  f"oa: {w['oa_status']} · doi: {w['doi']} · matched {len(w['queries'])} queries", "",
                  w["abstract"] or "(no abstract in OpenAlex)", ""]
    (run / "candidates.md").write_text("\n".join(lines), "utf-8")
    funnel = load_funnel(run) | {"search_hits": len(hits), "doi_unverifiable": dropped, "candidates": len(kept)}
    save_funnel(run, funnel)
    print(f"\n{len(hits)} unique hits, {dropped} dropped (DOI not verifiable), {len(kept)} candidates "
          f"-> {(run / 'candidates.md').relative_to(ROOT)}")
    return 0


# ---------- fetch ----------

def scihub_pdf_url(s: requests.Session, doi: str) -> str | None:
    """Resolve a Sci-Hub page to the direct PDF URL, or None if unavailable or SCIHUB_URL unset."""
    if not SCIHUB_URL:
        return None
    try:
        r = s.get(f"{SCIHUB_URL.rstrip('/')}/{doi}", timeout=30, allow_redirects=True)
        if r.status_code != 200:
            return None
        for pat in (rb'<iframe[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
                    rb'<embed[^>]+src=["\']([^"\']+\.pdf[^"\']*)["\']',
                    rb'"pdf_url"\s*:\s*"([^"]+)"'):
            m = re.search(pat, r.content, re.IGNORECASE)
            if m:
                u = m.group(1).decode()
                return ("https:" + u) if u.startswith("//") else u
    except (requests.RequestException, ValueError):
        pass
    return None


def oa_urls(s: requests.Session, doi: str) -> list[str]:
    urls: list[str] = []
    try:
        w = s.get(f"{OPENALEX}/doi:{quote(doi, safe='/()')}", timeout=30).json()
        for loc in [w.get("best_oa_location") or {}] + (w.get("locations") or []):
            if loc and loc.get("is_oa"):
                for key in ("pdf_url", "landing_page_url"):
                    if loc.get(key) and loc[key] not in urls:
                        urls.append(loc[key])
    except (requests.RequestException, ValueError) as e:
        print(f"[warn] OpenAlex {doi}: {e}", file=sys.stderr)
    try:
        pdf = (s.get(f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}",
                     params={"fields": "openAccessPdf"}, timeout=30).json().get("openAccessPdf") or {}).get("url")
        if pdf and pdf not in urls:
            urls.append(pdf)
    except (requests.RequestException, ValueError):
        pass
    if MAILTO:
        try:
            u = s.get(f"https://api.unpaywall.org/v2/{quote(doi, safe='/()')}", params={"email": MAILTO}, timeout=30).json()
            for loc in u.get("oa_locations") or []:
                for key in ("url_for_pdf", "url"):
                    if loc.get(key) and loc[key] not in urls:
                        urls.append(loc[key])
        except (requests.RequestException, ValueError):
            pass
    urls.sort(key=lambda u: 0 if "pdf" in u.lower() else 1)
    # Institutional access via EZproxy (tried after OA sources; needs valid session cookie)
    if EZPROXY_PREFIX:
        ezp = f"{EZPROXY_PREFIX}https://doi.org/{doi}"
        if ezp not in urls:
            urls.append(ezp)
    # Sci-Hub (last resort; resolves HTML page to direct PDF URL)
    if SCIHUB_URL:
        sh = scihub_pdf_url(s, doi)
        if sh and sh not in urls:
            urls.append(sh)
    return urls


def download(s: requests.Session, url: str, dest: Path, depth: int = 0) -> str | None:
    try:
        r = s.get(url, timeout=60, allow_redirects=True)
    except requests.RequestException as e:
        return type(e).__name__
    if r.status_code != 200:
        return f"HTTP {r.status_code}"
    if not r.content.startswith(b"%PDF"):
        m = re.search(rb'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"', r.content)
        if m and depth == 0:
            return download(s, m.group(1).decode(), dest, 1)
        return "not a PDF (landing page or blocked)"
    dest.write_bytes(r.content)
    return None


def pdf_to_text(pdf: Path, txt: Path) -> int:
    import pymupdf
    doc = pymupdf.open(pdf)
    txt.write_text("\n".join(f"<<<PAGE {k}>>>\n{p.get_text()}" for k, p in enumerate(doc, 1)), "utf-8")
    return len(doc)


def pdf_to_figures(pdf: Path, fig_dir: Path) -> list[dict]:
    """Extract figure images from a PDF. Stub — not yet implemented; interface reserved.

    Future: use pymupdf to extract embedded images, match with nearest caption text, and save
    as PNG files in fig_dir named <stem>_fig_NNN.png.
    Returns: [{"page": int, "index": int, "path": str, "caption": str, "bbox": [x0, y0, x1, y1]}]
    """
    return []


def cmd_fetch(a) -> int:
    run = run_dir(a.run)
    cands = {c["cand"]: c for c in read_jsonl(run / "candidates.jsonl")}
    screen = read_jsonl(run / "screen.jsonl")
    if not screen:
        sys.exit("No screen.jsonl yet. Screen candidates.md first (one JSON line per candidate).")
    unknown = [s["cand"] for s in screen if s.get("cand") not in cands]
    if unknown:
        sys.exit(f"screen.jsonl names candidates that do not exist: {unknown}")
    included = [cands[s["cand"]] | {"why": s.get("reason", "")} for s in screen if s.get("include")]

    s = requests.Session()
    s.headers["User-Agent"] = UA_BROWSER
    papers, n_ok = [], 0
    for c in included:
        if n_ok >= a.max:
            break
        pid = f"P{len(papers) + 1:02d}"
        row = {"paper": pid} | {k: c[k] for k in ("cand", "doi", "title", "authors", "year", "venue", "type", "oa_status", "license")}
        pdf = run / "pdf" / f"{pid}.pdf"
        if c["oa_status"] == "closed":
            row |= {"status": "closed access", "note": "get it through your library and save as pdf/" + pdf.name}
        else:
            errors = []
            for u in oa_urls(s, c["doi"]):
                err = download(s, u, pdf)
                if err is None:
                    row |= {"status": "downloaded", "note": u}
                    break
                errors.append(err)
                time.sleep(0.5)
            else:
                row |= {"status": "open access, download blocked",
                        "note": "; ".join(dict.fromkeys(errors)) or "no OA link found"}
        if row["status"] == "downloaded":
            try:
                row["pages"] = pdf_to_text(pdf, run / "text" / f"{pid}.txt")
                figs = pdf_to_figures(pdf, run / "text")
                if figs:
                    write_jsonl(run / "text" / f"{pid}_figures.jsonl", figs)
                n_ok += 1
            except Exception as e:  # corrupt PDF
                row |= {"status": "download corrupt", "note": str(e)}
        papers.append(row)
        print(f"[{pid}] {row['status']:<30} {c['doi']}")
        time.sleep(0.5)

    # PDFs the user dropped in by hand (pdf/Pnn.pdf) are picked up on re-run
    for row in papers:
        pdf, txt = run / "pdf" / f"{row['paper']}.pdf", run / "text" / f"{row['paper']}.txt"
        if row["status"] != "downloaded" and pdf.exists() and pdf.read_bytes()[:4] == b"%PDF":
            row |= {"status": "added by hand", "pages": pdf_to_text(pdf, txt)}

    write_jsonl(run / "papers.jsonl", papers)
    todo = [p for p in papers if not p.get("pages")]
    lines = ["# Papers without full text", "",
             "Options: (1) set `SCIHUB_URL` or `EZPROXY_PREFIX` env vars and re-run `ra.py fetch`; "
             "(2) download through your library and save as `pdf/<id>.pdf`, then re-run fetch.", "",
             "| id | paper | status | link |", "|---|---|---|---|"]
    lines += [f"| {p['paper']} | {cite(p)} {p['title'][:90]} | {p['status']} | https://doi.org/{p['doi']} |" for p in todo]
    (run / "to_download.md").write_text("\n".join(lines) + "\n", "utf-8")
    readable = [p for p in papers if p.get("pages")]
    funnel = load_funnel(run) | {"screened_in": len(included), "attempted": len(papers), "full_text": len(readable),
                                 "closed_access": sum(p["status"] == "closed access" for p in papers),
                                 "oa_blocked": sum(p["status"].startswith("open access") for p in papers)}
    save_funnel(run, funnel)
    print(f"\n{len(readable)} papers with full text in text/  "
          f"({funnel['closed_access']} closed access, {funnel['oa_blocked']} OA but blocked)")
    return 0


# ---------- verify ----------

def locate(quote_: str, text: str) -> tuple[str, float, int]:
    """How well a quote matches the paper: (kind, similarity, char offset in alnum text)."""
    q, t = alnum(quote_), alnum(text)
    if len(q) < 20:
        return "too short", 0.0, -1
    i = t.find(q)
    if i >= 0:
        return "exact", 1.0, i
    # fuzzy: anchor on 12-char shingles of the quote, score the best-supported window
    votes: dict[int, int] = {}
    for k in range(0, len(q) - 12, 6):
        for m in re.finditer(re.escape(q[k:k + 12]), t):
            start = m.start() - k
            votes[start // 10] = votes.get(start // 10, 0) + 1
    best, best_i = 0.0, -1
    for bucket, _ in sorted(votes.items(), key=lambda kv: -kv[1])[:5]:
        start = max(0, bucket * 10 - 20)
        window = t[start:start + len(q) + 40]
        sm = difflib.SequenceMatcher(None, q, window, autojunk=False)
        ratio = sum(b.size for b in sm.get_matching_blocks()) / len(q)
        if ratio > best:
            best, best_i = ratio, start
    if best >= 0.92:
        return "near-exact", round(best, 3), best_i
    if best >= 0.75:
        return "partial", round(best, 3), best_i
    return "not found", round(best, 3), -1


def page_of(offset: int, text: str) -> int | None:
    """Page number holding a given offset of alnum(text)."""
    if offset < 0:
        return None
    pos, page = 0, 1
    for chunk in re.split(r"(<<<PAGE \d+>>>)", text):
        m = re.fullmatch(r"<<<PAGE (\d+)>>>", chunk)
        if m:
            page = int(m.group(1))
            continue
        pos += len(alnum(chunk))
        if pos > offset:
            return page
    return page


def cmd_verify(a) -> int:
    run = run_dir(a.run)
    out, missing = [], []
    for f in sorted((run / "extract").glob("P*.jsonl")):
        pid = f.stem
        txt = run / "text" / f"{pid}.txt"
        if not txt.exists():
            missing.append(pid)
            continue
        text = txt.read_text("utf-8")
        for r in read_jsonl(f):
            kind, sim, off = locate(r.get("quote", ""), text)
            found_page = page_of(off, text)
            nums = numbers_in(r.get("quote", ""))
            value_nums = numbers_in(str(r.get("value", "")))
            out.append({
                "row": r.get("row"), "paper": pid,
                "quote_match": kind, "similarity": sim,
                "page_claimed": r.get("page"), "page_found": found_page,
                "page_ok": found_page is not None and str(found_page) == str(r.get("page")),
                "value_in_quote": bool(value_nums) and all(v in nums for v in value_nums),
            })
    write_jsonl(run / "verify.jsonl", out)
    by = {}
    for v in out:
        by[v["quote_match"]] = by.get(v["quote_match"], 0) + 1
    bad = [v for v in out if v["quote_match"] in ("not found", "too short", "partial") or not v["value_in_quote"]]
    print(f"{len(out)} rows checked: " + ", ".join(f"{k} {n}" for k, n in sorted(by.items())))
    print(f"value not in quote: {sum(not v['value_in_quote'] for v in out)} · page wrong: {sum(not v['page_ok'] for v in out)}")
    for v in bad:
        print(f"  [flag] {v['row']}: quote {v['quote_match']} ({v['similarity']}), value_in_quote={v['value_in_quote']}")
    if missing:
        print(f"[warn] extract files without full text: {missing}")
    save_funnel(run, load_funnel(run) | {"rows_extracted": len(out),
                                          "rows_quote_ok": sum(v["quote_match"] in ("exact", "near-exact") for v in out)})
    return 0


# ---------- report ----------

def final_rows(run: Path) -> tuple[list[dict], dict]:
    """Extracted rows merged with the mechanical check and the reviewer's verdict."""
    ver = {v["row"]: v for v in read_jsonl(run / "verify.jsonl")}
    rev = {}
    for f in (run / "review").glob("P*.jsonl"):
        for r in read_jsonl(f):
            rev[r["row"]] = r
    papers = {p["paper"]: p for p in read_jsonl(run / "papers.jsonl")}
    rows = []
    for f in sorted((run / "extract").glob("P*.jsonl")):
        for r in read_jsonl(f):
            v, w = ver.get(r["row"], {}), rev.get(r["row"], {})
            r = r | (w.get("corrected") or {})
            quote_ok = v.get("quote_match") in ("exact", "near-exact")
            verdict = w.get("verdict", "not reviewed")
            r |= {"quote_match": v.get("quote_match", "not checked"), "value_in_quote": v.get("value_in_quote"),
                  "verdict": verdict, "issue": w.get("issue", ""),
                  "accepted": quote_ok and bool(v.get("value_in_quote")) and verdict in ("accept", "correct"),
                  "doi": papers.get(r["paper"], {}).get("doi", ""),
                  "cite": cite(papers.get(r["paper"], {}))}
            rows.append(r)
    return rows, papers


def cite(p: dict) -> str:
    if not p:
        return ""
    first = (p.get("authors") or ["?"])[0].split()[-1]
    return f"{first}{' et al.' if len(p.get('authors') or []) > 1 else ''} ({p.get('year')})"


def unit_kind(r: dict) -> str:
    u = (str(r.get("unit", "")) + " " + str(r.get("value_kind", ""))).lower()
    if "%" in u or "percent" in u:
        return "percent"
    if re.search(r"(mg|t|ton|tonne)s?\s*c?\s*(/|\s)?\s*ha", u) and re.search(r"yr|year|a-1|y-1", u):
        return "rate"
    return "other"


def first_number(x) -> float | None:
    m = re.search(r"-?\d+(?:\.\d+)?", str(x).replace("−", "-").replace(",", ""))
    return float(m.group()) if m else None


def plot(rows: list[dict], path: Path) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False
    panels = [(k, [r for r in rows if unit_kind(r) == k and first_number(r.get("value")) is not None])
              for k, label in (("percent", ""), ("rate", ""))]
    panels = [(k, rs) for k, rs in panels if rs]
    if not panels:
        return False
    titles = {"percent": "Relative SOC change (%)", "rate": "SOC change rate (Mg C ha⁻¹ yr⁻¹)"}
    colors = {"meta-analysis": "#2a6fdb", "field experiment": "#e07b39", "modeling": "#7a5195", "review": "#5b8c5a"}
    h = max(2.5, 0.32 * max(len(rs) for _, rs in panels) + 1.2)
    fig, axes = plt.subplots(1, len(panels), figsize=(6.2 * len(panels), h), squeeze=False)
    for ax, (k, rs) in zip(axes[0], panels):
        rs = sorted(rs, key=lambda r: first_number(r["value"]))
        y = range(len(rs))
        for yi, r in zip(y, rs):
            c = colors.get(str(r.get("study_type", "")).lower(), "#888888")
            ax.plot(first_number(r["value"]), yi, "o", color=c, ms=6)
        ax.set_yticks(list(y))
        ax.set_yticklabels([f"{r['row']}  {r['cite']}" for r in rs], fontsize=8)
        ax.axvline(0, color="#999", lw=0.8)
        ax.set_title(titles[k], fontsize=10, loc="left", fontweight="bold")
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="x", color="#eee")
    handles = [plt.Line2D([], [], marker="o", ls="", color=c, label=t) for t, c in colors.items()]
    fig.legend(handles=handles, loc="lower center", ncol=4, frameon=False, fontsize=8)
    fig.text(0.01, 0.005, "Only rows whose quote was found in the paper and that the reviewer accepted.",
             fontsize=7, color="#777")
    fig.tight_layout(rect=(0, 0.06, 1, 1))
    fig.savefig(path, dpi=160)
    return True


def cmd_report(a) -> int:
    run = run_dir(a.run)
    rows, papers = final_rows(run)
    if not rows:
        sys.exit("No extracted rows yet (extract/Pnn.jsonl).")
    cols = ["row", "cite", "doi", "study_type", "location", "soil_depth_cm", "duration_yr", "cover_crop",
            "comparison", "metric", "value", "unit", "uncertainty", "n", "page", "quote",
            "quote_match", "value_in_quote", "verdict", "issue", "accepted"]
    with open(run / "evidence.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    acc = [r for r in rows if r["accepted"]]
    md = ["# Evidence table", "", f"{len(acc)} of {len(rows)} extracted rows accepted "
          "(quote found in the paper **and** accepted by the reviewer agent).", "",
          "| row | source | type | where | depth (cm) | years | metric | value | unit | p. |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    for r in acc:
        md.append("| " + " | ".join(str(r.get(k, "")).replace("|", "/") for k in
                  ("row", "cite", "study_type", "location", "soil_depth_cm", "duration_yr", "metric", "value", "unit", "page")) + " |")
    rejected = [r for r in rows if not r["accepted"]]
    if rejected:
        md += ["", "## Rejected rows", "", "| row | why |", "|---|---|"]
        for r in rejected:
            why = r["issue"] or (f"quote {r['quote_match']}" if r["quote_match"] not in ("exact", "near-exact") else r["verdict"])
            md.append(f"| {r['row']} | {str(why).replace('|', '/')} |")
    missed = [r for f in sorted((run / "review").glob("P*.jsonl")) for r in read_jsonl(f) if r.get("verdict") == "missed"]
    if missed:
        md += ["", "## Results the reviewer says the extractor missed (not used in the answer)", ""]
        md += [f"- **{m['row']}** {(m.get('corrected') or {}).get('metric', '')}: {(m.get('corrected') or {}).get('value', '')} "
               f"{(m.get('corrected') or {}).get('unit', '')} — {m.get('issue', '')}" for m in missed]
    md += ["", "## Papers", ""] + [f"- **{p['paper']}** {cite(p)} {p['title']}. *{p['venue']}*. "
                                   f"https://doi.org/{p['doi']} — {p['status']}" for p in papers.values()]
    (run / "evidence.md").write_text("\n".join(md) + "\n", "utf-8")
    has_fig = plot(acc, run / "fig_effects.png")

    # sample for a human to grade (the point of the exercise: how often is the agent wrong?)
    random.seed(a.seed)
    sample = random.sample(rows, min(a.sample, len(rows)))
    with open(run / "grading.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["row", "accepted_by_pipeline", "doi", "page", "metric", "value", "unit", "quote",
                    "human_quote_in_paper(y/n)", "human_value_correct(y/n)", "human_is_cover_crop_effect(y/n)",
                    "human_context_ok(y/n)", "notes"])
        for r in sample:
            w.writerow([r["row"], r["accepted"], f"https://doi.org/{r['doi']}", r.get("page"), r.get("metric"),
                        r.get("value"), r.get("unit"), r.get("quote"), "", "", "", "", ""])
    funnel = load_funnel(run) | {"rows_accepted": len(acc),
                                 "rows_rejected_by_reviewer": sum(r["verdict"] == "reject" for r in rows),
                                 "rows_corrected_by_reviewer": sum(r["verdict"] == "correct" for r in rows)}
    save_funnel(run, funnel)
    print("\n".join(f"{k:>28}: {v}" for k, v in funnel.items()))
    print(f"\n-> evidence.md, evidence.csv, grading.csv ({len(sample)} rows to grade by hand)"
          + (", fig_effects.png" if has_fig else ""))
    return 0


# ---------- checks on the final answer and on the baseline ----------

def cmd_check_answer(a) -> int:
    run = run_dir(a.run)
    ans = (run / "answer.md").read_text("utf-8")
    rows, _ = final_rows(run)
    by = {r["row"]: r for r in rows}
    cited = re.findall(r"\[(P\d{2}-E\d+)\]", ans)
    unknown = sorted({c for c in cited if c not in by})
    not_acc = sorted({c for c in cited if c in by and not by[c]["accepted"]})
    # numbers stated in the answer must appear in at least one row cited in the same sentence
    loose = []
    for sent in re.split(r"(?<=[.!?])\s+", re.sub(r"\n+", " ", ans.split("## Evidence", 1)[0])):
        ids = re.findall(r"\[(P\d{2}-E\d+)\]", sent)
        nums = [n for n in numbers_in(re.sub(r"\[P\d{2}-E\d+\]", "", sent)) if not re.fullmatch(r"(19|20)\d{2}", n)]
        if nums and not ids:
            loose.append(sent.strip()[:160])
        elif ids:
            pool = set()
            for i in ids:
                if i in by:
                    pool |= set(numbers_in(" ".join(str(by[i].get(k, "")) for k in ("value", "uncertainty", "n", "quote", "soil_depth_cm", "duration_yr"))))
            missing = [n for n in nums if n not in pool]
            if missing:
                loose.append(f"{sent.strip()[:160]}  <- {missing} not in cited rows")
    report = ["# Answer check", "", f"- citations: {len(cited)} ({len(set(cited))} distinct rows)",
              f"- cite rows that do not exist: {unknown or 'none'}",
              f"- cite rows that were rejected: {not_acc or 'none'}",
              f"- sentences with numbers not backed by a cited row: {len(loose)}"]
    report += [f"  - {s}" for s in loose]
    (run / "answer_check.md").write_text("\n".join(report) + "\n", "utf-8")
    print("\n".join(report))
    save_funnel(run, load_funnel(run) | {"answer_citations": len(cited), "answer_bad_citations": len(unknown) + len(not_acc),
                                          "answer_unbacked_numbers": len(loose)})
    return 1 if unknown or not_acc or loose else 0


def title_sim(a_: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, alnum(a_), alnum(b)).ratio(), 2)


def find_by_title(title: str) -> dict | None:
    try:
        r = api.get(OPENALEX, params={"search": title, "per_page": 1, "select": "doi,title,publication_year"}, timeout=30)
        res = r.json().get("results") or []
        return res[0] if res else None
    except (requests.RequestException, ValueError):
        return None


def cmd_check_baseline(a) -> int:
    run = run_dir(a.run)
    refs = read_jsonl(run / "baseline_refs.jsonl")
    if not refs:
        sys.exit("No baseline_refs.jsonl (the baseline agent writes it).")
    rows = []
    for r in refs:
        doi = (r.get("doi") or "").lower().removeprefix("https://doi.org/")
        cr = verify_doi(doi) if doi else None
        if cr and cr["title"]:
            sim = title_sim(r.get("title", ""), cr["title"])
            status = "real, matches" if sim >= 0.8 else "DOI exists but is a different paper"
        else:
            hit = find_by_title(r.get("title", ""))
            sim = title_sim(r.get("title", ""), (hit or {}).get("title") or "")
            if sim >= 0.9:
                status = "real paper, DOI wrong or missing"
            else:
                status = "not found (likely fabricated)"
        rows.append(r | {"check": status, "title_similarity": sim, "crossref_title": (cr or {}).get("title", "")})
        print(f"[{status:<36}] {r.get('title', '')[:80]}")
        time.sleep(0.3)
    write_jsonl(run / "baseline_check.jsonl", rows)
    counts = {}
    for r in rows:
        counts[r["check"]] = counts.get(r["check"], 0) + 1
    save_funnel(run, load_funnel(run) | {"baseline_refs": len(rows)} | {f"baseline: {k}": v for k, v in counts.items()})
    print("\n" + ", ".join(f"{k}: {v}" for k, v in counts.items()))
    return 0


# ---------- funnel (numbers for the slide) ----------

def load_funnel(run: Path) -> dict:
    p = run / "funnel.json"
    return json.loads(p.read_text("utf-8")) if p.exists() else {}


def save_funnel(run: Path, d: dict) -> None:
    (run / "funnel.json").write_text(json.dumps(d, indent=2), "utf-8")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("new"); p.add_argument("question"); p.add_argument("--reuse", action="store_true")
    p = sub.add_parser("search"); p.add_argument("run"); p.add_argument("--queries", nargs="+", required=True)
    p.add_argument("--since", type=int, default=1990); p.add_argument("--per-query", type=int, default=15)
    p.add_argument("--seed-dois", nargs="*", default=[], metavar="DOI",
                   help="DOIs to add regardless of ranking (fetched directly from OpenAlex)")
    p = sub.add_parser("fetch"); p.add_argument("run"); p.add_argument("--max", type=int, default=15,
                                                                       help="stop after this many full texts")
    p = sub.add_parser("verify"); p.add_argument("run")
    p = sub.add_parser("report"); p.add_argument("run")
    p.add_argument("--sample", type=int, default=20); p.add_argument("--seed", type=int, default=1)
    p = sub.add_parser("check-answer"); p.add_argument("run")
    p = sub.add_parser("check-baseline"); p.add_argument("run")
    a = ap.parse_args()
    return {"new": cmd_new, "search": cmd_search, "fetch": cmd_fetch, "verify": cmd_verify, "report": cmd_report,
            "check-answer": cmd_check_answer, "check-baseline": cmd_check_baseline}[a.cmd](a)


if __name__ == "__main__":
    sys.exit(main())
