#!/usr/bin/env python3
"""CLI entry point for the Infinity-Parser skill.

Parses documents through the Infinity-Parser gateway and writes the results to
disk. All parsing logic lives in client.py; this file only handles arguments,
input expansion, and output.

    python3 parse.py invoice.pdf --tier pro -o ./out --output-format md,json
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import (  # noqa: E402
    AUTO,
    SUPPORTED_OUTPUT_FORMATS,
    SUPPORTED_TASK_TYPES,
    VALID_TIERS,
    InfinityParserClient,
    InfinityParserError,
)

SUPPORTED_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif")

FORMAT_FILENAMES = {"md": "result.md", "json": "result.json"}


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="parse.py",
        description="Parse documents through the Infinity-Parser gateway.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Tiers (cost and latency increase left to right):
  nano   电子版  digital PDFs with a text layer; simple single-column layout
  flash  快速版  scans and images, ordinary layout, high volume
  pro    专业版  complex layout: multi-column, dense tables, formulas, handwriting
  max    高精版  what pro cannot handle: cross-page tables, critical documents
  auto           let the gateway choose from the document (default)

Examples:
  # Auto-selected tier, both output formats
  python3 parse.py report.pdf -o ./out --output-format md,json

  # Bulk digital PDFs on the cheapest tier
  python3 parse.py ./invoices -o ./out --tier nano

  # Selected pages of a hard document
  python3 parse.py contract.pdf -o ./out --tier max --pages 1-3,7

  # Remote file, no download needed
  python3 parse.py https://example.com/doc.pdf -o ./out

  # Show the request without sending it
  python3 parse.py report.pdf --tier pro --dry-run
        """,
    )
    parser.add_argument(
        "input",
        nargs="+",
        help="File path(s), directory path, or http(s) URL(s). A directory is "
             "expanded to the supported files directly inside it.",
    )
    parser.add_argument(
        "--tier",
        default=os.environ.get("INFINITY_PARSER_TIER", AUTO),
        choices=list(VALID_TIERS),
        help="Parsing tier. Defaults to $INFINITY_PARSER_TIER, else 'auto'.",
    )
    parser.add_argument(
        "-o", "--output-dir",
        default=None,
        help="Write results to <output-dir>/<input-name>/result.md|json. "
             "Without it results go to stdout, which floods the caller's "
             "context — prefer writing to disk.",
    )
    parser.add_argument(
        "--output-format",
        default="md",
        choices=list(SUPPORTED_OUTPUT_FORMATS),
        # One of the choices contains a comma, so the default rendering
        # ("{md,json,md,json}") reads as four separate values.
        metavar="md|json|md,json",
        help="Output format. Defaults to 'md'.",
    )
    parser.add_argument(
        "--task",
        default="doc2json",
        choices=list(SUPPORTED_TASK_TYPES),
        help="Parsing task. 'json' output requires 'doc2json'. Defaults to "
             "'doc2json'.",
    )
    parser.add_argument(
        "--prompt",
        default=None,
        help="Custom prompt, required when --task custom.",
    )
    parser.add_argument(
        "--pages",
        default=None,
        help="1-based physical PDF page selection, e.g. '1-3,5,8-10'. Ignored "
             "for images. Defaults to all pages.",
    )
    parser.add_argument(
        "--model",
        default=os.environ.get("INFINITY_PARSER_MODEL") or None,
        help="Pin a specific served build for reproducibility. Independent of "
             "--tier; normally omitted so the gateway picks the current build.",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("INFINITY_PARSER_API_URL"),
        help="Gateway URL. Defaults to $INFINITY_PARSER_API_URL.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("INFINITY_PARSER_API_KEY"),
        help="API key. Defaults to $INFINITY_PARSER_API_KEY.",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=600,
        help="Per-request timeout in seconds. Defaults to 600.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the request body that would be sent and exit, without "
             "contacting the gateway or needing an API key.",
    )
    return parser


def expand_inputs(raw_inputs):
    """Expand directories into the supported files directly inside them."""
    expanded = []
    for item in raw_inputs:
        if os.path.isdir(item):
            children = sorted(
                os.path.join(item, name)
                for name in os.listdir(item)
                if name.lower().endswith(SUPPORTED_EXTENSIONS)
            )
            if not children:
                raise SystemExit(
                    "No supported documents in {}. Supported: {}".format(
                        item, ", ".join(SUPPORTED_EXTENSIONS)
                    )
                )
            expanded.extend(children)
        else:
            expanded.append(item)
    return expanded


def label(source: str, limit: int = 80) -> str:
    """Short stand-in for a source in messages — a base64 blob is not printable."""
    if len(source) <= limit:
        return source
    return source[:limit] + "... [{} chars]".format(len(source))


def output_name(source: str) -> str:
    """Directory name for one input's results, matching the CLI convention."""
    name = os.path.basename(source.rstrip("/").split("?", 1)[0])
    return name or "document"


def write_outputs(output_dir: str, source: str, outputs) -> list:
    target_dir = os.path.join(output_dir, output_name(source))
    os.makedirs(target_dir, exist_ok=True)
    written = []
    for fmt, content in outputs.items():
        path = os.path.join(target_dir, FORMAT_FILENAMES.get(fmt, fmt))
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)
        written.append(path)
    return written


def preview_payload(payload):
    """Shorten base64 so a dry run stays readable."""
    shown = dict(payload)
    blob = shown.get("file_base64")
    if isinstance(blob, str):
        shown["file_base64"] = "<{} base64 chars> {}...".format(
            len(blob), blob[:32]
        )
    return json.dumps(shown, indent=2, ensure_ascii=False)


def main(argv=None) -> int:
    args = build_arg_parser().parse_args(argv)

    if args.task == "custom" and not args.prompt:
        print("Error: --task custom requires --prompt.", file=sys.stderr)
        return 2

    try:
        sources = expand_inputs(args.input)
    except SystemExit as exc:
        print("Error: {}".format(exc), file=sys.stderr)
        return 2

    client = InfinityParserClient(
        api_url=args.api_url or ("https://dry-run.invalid" if args.dry_run else ""),
        api_key=args.api_key,
        timeout=args.timeout,
    )

    call_kwargs = dict(
        tier=args.tier,
        task_type=args.task,
        output_format=args.output_format,
        pages=args.pages,
        custom_prompt=args.prompt,
        model=args.model,
    )

    failures = 0
    for source in sources:
        try:
            if args.dry_run:
                payload = client.build_payload_for_preview(
                    source=source, **call_kwargs
                )
                print("=== {} ===".format(label(source)))
                print(preview_payload(payload))
                continue

            result = client.parse(source, **call_kwargs)

            if result.tier_used and result.tier_used != args.tier:
                print(
                    "[infinity-parser] {}: requested tier {!r}, gateway ran "
                    "{!r}.".format(label(source), args.tier, result.tier_used),
                    file=sys.stderr,
                )

            if args.output_dir:
                for path in write_outputs(
                    args.output_dir, source, result.outputs
                ):
                    print("[infinity-parser] wrote {}".format(path))
            else:
                for fmt, content in result.outputs.items():
                    print("=== {} ({}) ===".format(label(source), fmt))
                    print(content)

        except (ValueError, InfinityParserError) as exc:
            print(
                "Error parsing {}: {}".format(label(source), exc),
                file=sys.stderr,
            )
            failures += 1

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
