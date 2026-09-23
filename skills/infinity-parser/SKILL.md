---
name: infinity-parser
description: Extract Markdown, tables, formulas, and layout blocks from PDFs or images with Infinity Parser. Use Nano for simple digital PDFs, Flash for routine scans and images, and Pro for complex layouts, handwriting, or poor-quality scans.
---

# Infinity Parser Skill

Submit one PDF or supported image per request to get Markdown and layout blocks. Download URLs or capture webpage screenshots before sending the resulting file.

Copy `.env.example` to `.env`, then set `INF_API_URL` to the API base URL and `INF_API_KEY` to your key. The Python command reads `.env` automatically without printing the key. Environment variables with the same names override the file and can also be used without one.

## Choose a model

| Model | Best for |
| --- | --- |
| `infinity-parser-nano` | Digital PDFs with a real text layer and simple layouts. Fast and lightweight; GPU processing is rarely needed. |
| `infinity-parser-flash` | Scanned documents and images with ordinary layouts. Balances speed and cost for high-volume processing. |
| `infinity-parser-pro` | Complex documents with multi-column layouts, dense tables, formulas, handwriting, or poor-quality scans. |

## Input and options

- Send `multipart/form-data` with required `file` and `model`.
- PDFs and single-frame PNG, JPEG, WEBP, BMP, TIFF, and GIF are supported. The Gateway identifies content from bytes, so a wrong extension or MIME type does not prevent parsing.
- Multi-frame images, HTML, URL strings, Office files, SVG, and archives are unsupported. Download a web URL or capture a webpage screenshot in the platform, then send the resulting PDF or image.

| Option | Behavior |
| --- | --- |
| `pages` | Optional 1-based page selection, such as `1-3,5`. Duplicates are removed and pages are parsed in ascending order. Omit it for all PDF pages; for an image, omit it or use `1`. |
| `keep_header_footer` | Boolean, default `false`. Set to `true` to retain headers, footers, and page footnotes in Markdown. Their blocks remain in the response either way. |
| `parse_chart` | Boolean, default `true`. Set to `false` to skip extra descriptions or data extraction for image and chart regions. |

```bash
python3 skills/infinity-parser/scripts/parse.py /path/to/report.pdf \
  -m pro -o /path/to/output --pages 1-3,5
```

The command saves `report.json` and `report.md` in the `-o` directory. Omit `-o` to save them next to the input. Omit `--pages` to parse all pages. The default model is Flash. Use `--keep-header-footer` to include header/footer text in Markdown or `--no-parse-chart` to skip extra chart extraction. Partial page failures still save both files and return exit code 2. Read [references/gateway-api.md](references/gateway-api.md) for response and streaming event details.

Use the multipart endpoint in preference to the legacy `/v1/chat/completions` JSON endpoint. That JSON endpoint accepts PDF Base64 only and requires `data:application/pdf;base64,...` plus a `.pdf` filename.
