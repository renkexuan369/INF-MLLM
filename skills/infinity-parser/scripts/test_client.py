#!/usr/bin/env python3
"""Tests for client.py. Standard library only, no network.

    python3 test_client.py
"""

import base64
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from client import (  # noqa: E402
    AUTO,
    AUTO_FALLBACK_TIER,
    TIERS,
    GatewayError,
    InfinityParserClient,
    TierUnavailableError,
    classify_source,
    guess_mime,
    normalize_tier,
    validate_formats,
)

API_URL = "https://gateway.example.com/v1/chat/completions"


class TestNormalizeTier(unittest.TestCase):
    def test_accepts_every_tier(self):
        for tier in TIERS:
            self.assertEqual(normalize_tier(tier), tier)

    def test_accepts_auto(self):
        self.assertEqual(normalize_tier(AUTO), AUTO)

    def test_none_defaults_to_auto(self):
        self.assertEqual(normalize_tier(None), AUTO)

    def test_is_case_insensitive_and_strips(self):
        self.assertEqual(normalize_tier("  PRO "), "pro")
        self.assertEqual(normalize_tier("Max"), "max")

    def test_unknown_tier_lists_valid_values(self):
        with self.assertRaises(ValueError) as ctx:
            normalize_tier("ultra")
        message = str(ctx.exception)
        for tier in TIERS:
            self.assertIn(tier, message)

    def test_old_tier_names_are_rejected(self):
        # hybrid/agentic were the previous taxonomy; they must not silently work.
        for stale in ("hybrid", "agentic"):
            with self.assertRaises(ValueError):
                normalize_tier(stale)


class TestValidateFormats(unittest.TestCase):
    def test_doc2json_allows_every_format(self):
        for fmt in ("md", "json", "md,json"):
            self.assertEqual(
                validate_formats("doc2json", fmt), ("doc2json", fmt)
            )

    def test_doc2md_allows_markdown_only(self):
        self.assertEqual(validate_formats("doc2md", "md"), ("doc2md", "md"))

    def test_doc2md_rejects_json(self):
        for fmt in ("json", "md,json"):
            with self.assertRaises(ValueError):
                validate_formats("doc2md", fmt)

    def test_unknown_values_raise(self):
        with self.assertRaises(ValueError):
            validate_formats("doc2pdf", "md")
        with self.assertRaises(ValueError):
            validate_formats("doc2json", "html")


class TestClassifySource(unittest.TestCase):
    def test_https_url_passes_through(self):
        kind, value, name = classify_source("https://example.com/a/report.pdf")
        self.assertEqual(kind, "url")
        self.assertEqual(value, "https://example.com/a/report.pdf")
        self.assertEqual(name, "report.pdf")

    def test_local_file_is_encoded(self):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as handle:
            handle.write(b"%PDF-1.7 fake")
            path = handle.name
        try:
            kind, value, name = classify_source(path)
            self.assertEqual(kind, "base64")
            self.assertEqual(base64.b64decode(value), b"%PDF-1.7 fake")
            self.assertEqual(name, os.path.basename(path))
        finally:
            os.unlink(path)

    def test_bare_base64_needs_a_file_name(self):
        blob = base64.b64encode(b"x" * 200).decode("ascii")
        with self.assertRaises(ValueError):
            classify_source(blob)
        kind, _, name = classify_source(blob, file_name="scan.png")
        self.assertEqual(kind, "base64")
        self.assertEqual(name, "scan.png")

    def test_missing_path_raises(self):
        with self.assertRaises(ValueError):
            classify_source("/no/such/file.pdf")


class TestGuessMime(unittest.TestCase):
    def test_known_and_unknown_extensions(self):
        self.assertEqual(guess_mime("a.pdf"), "application/pdf")
        self.assertEqual(guess_mime("A.JPG"), "image/jpeg")
        self.assertEqual(guess_mime("a.xyz"), "application/octet-stream")


class TestBuildPayload(unittest.TestCase):
    def setUp(self):
        self.client = InfinityParserClient(API_URL, "key")

    def payload(self, **overrides):
        kwargs = dict(
            source="https://example.com/doc.pdf",
            tier="pro",
            task_type="doc2json",
            output_format="md",
        )
        kwargs.update(overrides)
        return self.client.build_payload_for_preview(**kwargs)

    def test_tier_is_its_own_field(self):
        self.assertEqual(self.payload()["tier"], "pro")

    def test_tier_never_becomes_the_model(self):
        # The whole point of the tier axis: it must not be folded into model.
        for tier in TIERS:
            body = self.payload(tier=tier)
            self.assertEqual(body["tier"], tier)
            self.assertNotIn("model", body)
            self.assertNotIn("model_name", body)

    def test_model_is_sent_only_when_pinned(self):
        body = self.payload(model="inf-mllm")
        self.assertEqual(body["model"], "inf-mllm")
        self.assertEqual(body["tier"], "pro")

    def test_url_source_uses_file_url(self):
        body = self.payload()
        self.assertEqual(body["file_url"], "https://example.com/doc.pdf")
        self.assertNotIn("file_base64", body)

    def test_local_source_uses_base64_and_mime(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(b"\x89PNG fake")
            path = handle.name
        try:
            body = self.payload(source=path)
            self.assertIn("file_base64", body)
            self.assertEqual(body["mime_type"], "image/png")
            self.assertNotIn("file_url", body)
        finally:
            os.unlink(path)

    def test_optional_fields_are_omitted_when_empty(self):
        body = self.payload()
        for key in ("pages", "custom_prompt", "model"):
            self.assertNotIn(key, body)

    def test_pages_passed_through(self):
        self.assertEqual(self.payload(pages="1-3,5")["pages"], "1-3,5")

    def test_invalid_tier_rejected_before_any_request(self):
        with self.assertRaises(ValueError):
            self.payload(tier="ultra")


class TestUnpackResponse(unittest.TestCase):
    def unpack(self, data, output_format="md"):
        return InfinityParserClient._unpack_response(data, output_format)

    def test_openai_envelope_single_content(self):
        data = {"choices": [{"message": {"content": "# Title"}}]}
        outputs, tier = self.unpack(data)
        self.assertEqual(outputs, {"md": "# Title"})
        self.assertIsNone(tier)

    def test_structured_body_carries_both_formats(self):
        data = {
            "choices": [{"message": {"md": "# Title", "json": "{}"}}],
            "tier": "pro",
        }
        outputs, tier = self.unpack(data, "md,json")
        self.assertEqual(outputs, {"md": "# Title", "json": "{}"})
        self.assertEqual(tier, "pro")

    def test_top_level_structured_body(self):
        outputs, _ = self.unpack({"md": "# Title"}, "md")
        self.assertEqual(outputs, {"md": "# Title"})

    def test_tier_used_read_from_message(self):
        data = {"choices": [{"message": {"content": "x", "tier": "flash"}}]}
        _, tier = self.unpack(data)
        self.assertEqual(tier, "flash")

    def test_both_formats_from_one_string_is_layout_json(self):
        data = {"choices": [{"message": {"content": '{"blocks": []}'}}]}
        outputs, _ = self.unpack(data, "md,json")
        self.assertEqual(outputs, {"json": '{"blocks": []}'})

    def test_missing_content_raises(self):
        with self.assertRaises(GatewayError):
            self.unpack({"id": "abc"})


class _StubClient(InfinityParserClient):
    """Records requests and replays queued responses instead of sending."""

    def __init__(self, responses):
        super().__init__(API_URL, "key")
        self._responses = list(responses)
        self.sent = []

    def _post(self, payload):
        self.sent.append(dict(payload))
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


OK = {"choices": [{"message": {"content": "# ok"}}], "tier": "pro"}


class TestParseFlow(unittest.TestCase):
    def test_auto_falls_back_when_gateway_rejects_it(self):
        client = _StubClient([TierUnavailableError("no auto"), OK])
        result = client.parse("https://example.com/a.pdf", tier=AUTO)
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(client.sent[0]["tier"], AUTO)
        self.assertEqual(client.sent[1]["tier"], AUTO_FALLBACK_TIER)
        self.assertEqual(result.outputs, {"md": "# ok"})

    def test_explicit_tier_does_not_fall_back(self):
        client = _StubClient([TierUnavailableError("nano not rolled out")])
        with self.assertRaises(TierUnavailableError):
            client.parse("https://example.com/a.pdf", tier="nano")
        self.assertEqual(len(client.sent), 1)

    def test_degraded_is_flagged(self):
        data = {"choices": [{"message": {"content": "# ok"}}], "tier": "flash"}
        client = _StubClient([data])
        result = client.parse("https://example.com/a.pdf", tier="nano")
        self.assertEqual(result.tier_used, "flash")
        self.assertEqual(result.tier_requested, "nano")
        self.assertTrue(result.degraded)

    def test_matching_tier_is_not_degraded(self):
        client = _StubClient([OK])
        result = client.parse("https://example.com/a.pdf", tier="pro")
        self.assertFalse(result.degraded)

    def test_custom_task_requires_a_prompt(self):
        client = _StubClient([OK])
        with self.assertRaises(ValueError):
            client.parse(
                "https://example.com/a.pdf", task_type="custom",
            )
        self.assertEqual(client.sent, [])

    def test_missing_api_url_is_rejected_at_construction(self):
        with self.assertRaises(ValueError):
            InfinityParserClient("", "key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
