---
name: infinity-parser
description: Parse PDFs, scanned documents, and document images (screenshots, invoices, contracts, paper documents, whiteboard photos) into Markdown and structured layout JSON via the Infinity-Parser API. Four quality tiers — nano (电子版), flash (快速版), pro (专业版), max (高精版) — picked from the document or named by the user. Use when the user wants to extract text, tables, formulas, or structured data from visual documents; mentions OCR, text recognition, or document parsing; or asks to parse, digitize, or extract content from a document. Requires an Infinity-Parser gateway URL and API key.
---

# Infinity-Parser Skill

Parses documents through the Infinity-Parser gateway. No `pip install` and no
model download — `scripts/parse.py` is standard-library-only and talks to the
API directly.

## Tiers

Cost and latency increase down the table. Pick from the document's features,
never by asking the user how accurate they want it.

| tier | 中文 | Use for |
|---|---|---|
| `nano` | 电子版 | Digital PDFs with a real text layer. Single-column, no tables or regular ones. Rules + a small model, so most pages never touch a GPU. |
| `flash` | 快速版 | Scans and images with ordinary layout. High volume, cost-sensitive. |
| `pro` | 专业版 | Complex layout: multi-column, dense tables, formulas, handwriting, poor-quality scans. |
| `max` | 高精版 | What `pro` cannot handle: cross-page tables, complex charts, critical documents worth cross-checking (contracts, financial reports). Slowest and most expensive. |

`auto` is the default and lets the gateway choose. Prefer it — the routing
policy improves server-side without this skill changing.

When the user names a tier ("用高精版", "走 nano", "use flash"), use it. Do not
override that with `auto`.

### nano is the one tier with a precondition

`nano` needs a text layer. On a digital PDF it is both cheaper *and* more
accurate than `flash`, because the text layer is ground truth and OCR can only
introduce errors — so the ladder is not monotonic at this step. On a scan there
is no text layer to exploit and the gateway degrades the request to `flash`.

`scripts/parse.py` prints a line to stderr whenever the gateway ran a different
tier than the one requested. **Pass that on to the user** — they need to know
what they were billed for.

## Configuration

```bash
export INFINITY_PARSER_API_URL=<gateway URL>
export INFINITY_PARSER_API_KEY=<api-key>
```

Check `INFINITY_PARSER_API_URL` is exported before the first call. Never
request, print, or echo the API key.

## Usage

Paths below are relative to this skill's directory.

```bash
python3 scripts/parse.py <input> -o <output-dir> [options]
```

```bash
# Auto-selected tier, Markdown + layout JSON
python3 scripts/parse.py report.pdf -o ./out --output-format md,json

# Bulk digital PDFs on the cheapest tier
python3 scripts/parse.py ./invoices -o ./out --tier nano

# A hard contract, selected pages
python3 scripts/parse.py contract.pdf -o ./out --tier max --pages 1-3,7

# Remote file — no download needed
python3 scripts/parse.py https://example.com/doc.pdf -o ./out
```

Options:

- `--tier nano|flash|pro|max|auto` — defaults to `$INFINITY_PARSER_TIER`, else `auto`.
- `-o/--output-dir <dir>` — **always pass this.** Without it results print to
  stdout, which floods the context with the entire document.
- `--output-format md|json|md,json` — defaults to `md`.
- `--task doc2json|doc2md|custom` — defaults to `doc2json`. `--prompt` is
  required with `custom`.
- `--pages 1-3,5` — 1-based physical PDF pages. Ignored for images.
- `--dry-run` — print the request body without sending it. Use this to check
  configuration without spending a call.

Inputs accept a file path, a directory (expanded to the supported files inside
it), an `http(s)://` URL, or several of these at once.

## Output

Each input gets its own subdirectory:

```
<output-dir>/<input-name>/result.md
<output-dir>/<input-name>/result.json
```

`--output-format json` and `md,json` require `--task doc2json` (the default),
since only that task produces a layout. `doc2md` and `custom` are Markdown-only
and the script rejects a JSON request for them before making a call.

After parsing, tell the user where the files landed and which tier ran. Read
the result files only if you need their content — for a bulk conversion, the
paths are the answer.

## Errors

- **Auth failure** — the key is wrong or missing. Ask the user to check
  `INFINITY_PARSER_API_KEY`; never print its value.
- **Tier unavailable** — `nano` and `max` are still rolling out. Say so, then
  offer `pro` rather than silently substituting it.
- **Gateway unreachable** — report the URL being used (not the key) so the user
  can check it.

## Self-hosting

This skill only talks to the hosted API. Running the open-weights
Infinity-Parser2 models on your own GPU is a separate path with its own SDK —
see `Infinity-Parser2/README.md` in this repo. The two do not depend on each
other, and `pip install infinity_parser2` is not needed here.
