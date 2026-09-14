"""Thin client for the Infinity-Parser commercial gateway.

Pure standard library — no third-party dependencies, so this runs anywhere a
Python 3.9+ interpreter does.

This module deliberately holds no CLI logic and no skill-specific behaviour: it
is written to be lifted wholesale into a standalone client package later.

Two functions localise everything that depends on the gateway's wire contract:
``_build_payload`` (what we send) and ``_unpack_response`` (what we read back).
If the gateway's field names differ from what is assumed here, those are the
only two places to change.
"""

import base64
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple

__all__ = [
    "TIERS",
    "AUTO",
    "VALID_TIERS",
    "SUPPORTED_TASK_TYPES",
    "SUPPORTED_OUTPUT_FORMATS",
    "InfinityParserClient",
    "ParseResult",
    "InfinityParserError",
    "AuthError",
    "TierUnavailableError",
    "GatewayError",
]

# Ordered quality/cost ladder. Cost and latency increase left to right.
TIERS = ("nano", "flash", "pro", "max")

# Let the gateway pick the tier from the document. This is the default: the
# client deliberately does not inspect the document itself, which would mean a
# PDF dependency and would freeze the routing policy into the client.
AUTO = "auto"

VALID_TIERS = (AUTO,) + TIERS

# Tier used when the gateway does not understand AUTO yet.
AUTO_FALLBACK_TIER = "pro"

SUPPORTED_TASK_TYPES = ("doc2json", "doc2md", "custom")
SUPPORTED_OUTPUT_FORMATS = ("md", "json", "md,json")

DEFAULT_TIMEOUT = 600

# Error bodies are truncated before being raised: a gateway that answers with an
# HTML error page should not flood the caller's context.
_MAX_ERROR_BODY = 500

_B64_RE = re.compile(r"^[A-Za-z0-9+/\s]+={0,2}$")

_EXT_MIME = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".gif": "image/gif",
}


class InfinityParserError(Exception):
    """Base class for every error this client raises."""


class AuthError(InfinityParserError):
    """The gateway rejected the API key."""


class TierUnavailableError(InfinityParserError):
    """The requested tier exists but is not served yet (nano/max rollout)."""


class GatewayError(InfinityParserError):
    """The gateway failed for any other reason."""


@dataclass
class ParseResult:
    """One parsed document.

    Attributes:
        outputs: Parsed content keyed by format — ``"md"`` and/or ``"json"``.
        tier_used: Tier the gateway actually ran. Differs from the requested
            tier when AUTO was resolved server-side, or when a tier degraded
            (``nano`` falls back to ``flash`` on documents with no text layer).
        tier_requested: What the caller asked for.
        raw: The decoded response body, for callers that need more.
    """

    outputs: Dict[str, str]
    tier_used: Optional[str] = None
    tier_requested: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def degraded(self) -> bool:
        """True when the gateway ran a different tier than the one requested."""
        if not self.tier_used or not self.tier_requested:
            return False
        if self.tier_requested == AUTO:
            return False
        return self.tier_used != self.tier_requested


def normalize_tier(tier: Optional[str]) -> str:
    """Validate and lowercase a tier name.

    Raises:
        ValueError: If the tier is not one of VALID_TIERS.
    """
    if tier is None:
        return AUTO
    normalized = str(tier).strip().lower()
    if normalized not in VALID_TIERS:
        raise ValueError(
            "Unknown tier: {!r}. Valid tiers: {}.".format(
                tier, ", ".join(VALID_TIERS)
            )
        )
    return normalized


def validate_formats(task_type: str, output_format: str) -> Tuple[str, str]:
    """Validate task type and output format, including their interaction.

    JSON output describes a layout, which only ``doc2json`` produces.

    Raises:
        ValueError: On an unknown value, or on a JSON request for a task that
            cannot produce one.
    """
    if task_type not in SUPPORTED_TASK_TYPES:
        raise ValueError(
            "Unknown task_type: {!r}. Supported: {}.".format(
                task_type, ", ".join(SUPPORTED_TASK_TYPES)
            )
        )
    if output_format not in SUPPORTED_OUTPUT_FORMATS:
        raise ValueError(
            "Unknown output_format: {!r}. Supported: {}.".format(
                output_format, ", ".join(SUPPORTED_OUTPUT_FORMATS)
            )
        )
    if "json" in output_format and task_type != "doc2json":
        raise ValueError(
            "output_format={!r} requires task_type='doc2json'; "
            "{!r} can only produce 'md'.".format(output_format, task_type)
        )
    return task_type, output_format


def _looks_like_base64(value: str) -> bool:
    """Heuristic: long enough, and nothing outside the base64 alphabet."""
    if len(value) < 64:
        return False
    return _B64_RE.match(value) is not None


def classify_source(source: str, file_name: Optional[str] = None):
    """Work out which of the gateway's three input forms a source is.

    The gateway accepts a URL, a base64 blob, or an uploaded file. Local paths
    are read and encoded here, so the wire always carries a URL or base64.

    Args:
        source: An ``http(s)://`` URL, a path to a local file, or a base64 blob.
        file_name: Name to report to the gateway. Required for a bare base64
            blob, since there is no path to derive it from.

    Returns:
        ``(kind, value, file_name)`` where kind is ``"url"`` or ``"base64"``.

    Raises:
        ValueError: If the source is none of the three, or a base64 blob was
            passed with no file name.
    """
    text = source.strip() if isinstance(source, str) else source

    parsed = urllib.parse.urlparse(str(text))
    if parsed.scheme in ("http", "https"):
        name = file_name or os.path.basename(parsed.path) or "document"
        return "url", str(text), name

    if os.path.isfile(str(text)):
        with open(str(text), "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        return "base64", encoded, file_name or os.path.basename(str(text))

    if isinstance(text, str) and _looks_like_base64(text):
        if not file_name:
            raise ValueError(
                "A base64 source needs an explicit file_name so the gateway "
                "knows the document type."
            )
        return "base64", re.sub(r"\s+", "", text), file_name

    raise ValueError(
        "Source is not a readable file, an http(s) URL, or a base64 blob: "
        "{!r}".format(source)
    )


def guess_mime(file_name: str) -> str:
    """Map a file name to a MIME type, defaulting to octet-stream."""
    _, ext = os.path.splitext(file_name.lower())
    return _EXT_MIME.get(ext, "application/octet-stream")


class InfinityParserClient:
    """Calls the Infinity-Parser gateway.

    Example:
        >>> client = InfinityParserClient(api_url, api_key)
        >>> result = client.parse("invoice.pdf", tier="pro")
        >>> result.outputs["md"]
    """

    def __init__(
        self,
        api_url: str,
        api_key: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        if not api_url:
            raise ValueError(
                "api_url is required. Set INFINITY_PARSER_API_URL or pass "
                "--api-url."
            )
        self.api_url = api_url
        self.api_key = api_key
        self.timeout = timeout

    # -- wire contract -----------------------------------------------------
    # The two methods below are the only places that know the gateway's field
    # names. Adjust them here if the contract differs.

    def _build_payload(
        self,
        kind: str,
        value: str,
        file_name: str,
        tier: str,
        task_type: str,
        output_format: str,
        pages: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Assemble the request body.

        ``tier`` is its own field. It is never folded into ``model`` — the two
        are independent: tier selects the parsing pipeline, model pins a
        specific served build for reproducibility and is normally omitted.
        """
        payload: Dict[str, Any] = {
            "tier": tier,
            "file_name": file_name,
            "task_type": task_type,
            "output_format": output_format,
        }

        if kind == "url":
            payload["file_url"] = value
        else:
            payload["file_base64"] = value
            payload["mime_type"] = guess_mime(file_name)

        if pages:
            payload["pages"] = pages
        if custom_prompt:
            payload["custom_prompt"] = custom_prompt
        if model:
            payload["model"] = model

        return payload

    @staticmethod
    def _unpack_response(
        data: Dict[str, Any], output_format: str
    ) -> Tuple[Dict[str, str], Optional[str]]:
        """Pull parsed content and the tier actually used out of a response.

        Accepts either a structured body (``{"md": ..., "json": ...}``, at the
        top level or inside the message) or an OpenAI-shaped envelope carrying
        a single content string.

        Returns:
            ``(outputs, tier_used)``.
        """
        wanted = output_format.split(",")
        tier_used = data.get("tier") or data.get("tier_used")

        message: Dict[str, Any] = {}
        choices = data.get("choices")
        if isinstance(choices, list) and choices:
            first = choices[0]
            if isinstance(first, dict) and isinstance(first.get("message"), dict):
                message = first["message"]
        if not tier_used:
            tier_used = message.get("tier")

        # Structured bodies win: they carry both formats unambiguously.
        for container in (message, data):
            found = {
                key: container[key]
                for key in wanted
                if isinstance(container.get(key), str)
            }
            if found:
                return found, tier_used

        content = message.get("content")
        if content is None:
            content = data.get("content")
        if not isinstance(content, str):
            raise GatewayError(
                "Could not find parsed content in the gateway response. "
                "Top-level keys: {}.".format(sorted(data))
            )

        if len(wanted) == 1:
            return {wanted[0]: content}, tier_used

        # One string but both formats asked for: doc2json returns the layout
        # JSON, so that is what this is.
        return {"json": content}, tier_used

    # -- transport ---------------------------------------------------------

    def _post(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            key = self.api_key
            headers["Authorization"] = (
                key if key.startswith("Bearer ") else "Bearer " + key
            )

        request = urllib.request.Request(
            self.api_url, data=body, headers=headers, method="POST"
        )

        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            raise self._classify_http_error(exc, payload.get("tier")) from None
        except urllib.error.URLError as exc:
            raise GatewayError(
                "Could not reach the gateway at {}: {}".format(
                    self.api_url, exc.reason
                )
            ) from None

        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            raise GatewayError(
                "Gateway returned a non-JSON body: {}".format(_truncate(raw))
            ) from None

    @staticmethod
    def _classify_http_error(
        exc: "urllib.error.HTTPError", tier: Optional[str]
    ) -> InfinityParserError:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001 - the body is best-effort context
            detail = ""
        detail = _truncate(detail)

        if exc.code in (401, 403):
            return AuthError(
                "Gateway rejected the API key (HTTP {}). Check "
                "INFINITY_PARSER_API_KEY. {}".format(exc.code, detail)
            )
        if exc.code in (400, 404, 422) and tier and tier in TIERS:
            return TierUnavailableError(
                "Gateway rejected tier {!r} (HTTP {}). It may not be rolled "
                "out yet. {}".format(tier, exc.code, detail)
            )
        return GatewayError(
            "Gateway returned HTTP {}. {}".format(exc.code, detail)
        )

    # -- public API --------------------------------------------------------

    def parse(
        self,
        source: str,
        tier: str = AUTO,
        task_type: str = "doc2json",
        output_format: str = "md",
        pages: Optional[str] = None,
        custom_prompt: Optional[str] = None,
        model: Optional[str] = None,
        file_name: Optional[str] = None,
    ) -> ParseResult:
        """Parse one document.

        Args:
            source: Local file path, ``http(s)://`` URL, or base64 blob.
            tier: One of ``nano``, ``flash``, ``pro``, ``max``, or ``auto``.
            task_type: ``doc2json`` (layout JSON), ``doc2md``, or ``custom``.
            output_format: ``md``, ``json``, or ``md,json``.
            pages: 1-based physical PDF page selection, e.g. ``"1-3,5"``.
            custom_prompt: Required when ``task_type="custom"``.
            model: Optional pin to a specific served build. Independent of tier.
            file_name: Required only when source is a bare base64 blob.

        Returns:
            A ParseResult.

        Raises:
            ValueError: On invalid arguments.
            InfinityParserError: On any gateway failure.
        """
        requested = normalize_tier(tier)
        validate_formats(task_type, output_format)
        if task_type == "custom" and not custom_prompt:
            raise ValueError("task_type='custom' requires custom_prompt.")

        kind, value, name = classify_source(source, file_name)

        payload = self._build_payload(
            kind=kind,
            value=value,
            file_name=name,
            tier=requested,
            task_type=task_type,
            output_format=output_format,
            pages=pages,
            custom_prompt=custom_prompt,
            model=model,
        )

        try:
            data = self._post(payload)
        except TierUnavailableError:
            # A gateway that does not know AUTO yet should not be a hard
            # failure — retry once at the documented fallback tier.
            if requested != AUTO:
                raise
            payload["tier"] = AUTO_FALLBACK_TIER
            data = self._post(payload)
            requested = AUTO_FALLBACK_TIER

        outputs, tier_used = self._unpack_response(data, output_format)
        return ParseResult(
            outputs=outputs,
            tier_used=tier_used,
            tier_requested=requested,
            raw=data,
        )

    def build_payload_for_preview(self, **kwargs) -> Dict[str, Any]:
        """Assemble a request body without sending it (used by ``--dry-run``)."""
        requested = normalize_tier(kwargs.pop("tier", AUTO))
        task_type = kwargs.pop("task_type", "doc2json")
        output_format = kwargs.pop("output_format", "md")
        validate_formats(task_type, output_format)
        source = kwargs.pop("source")
        file_name = kwargs.pop("file_name", None)
        kind, value, name = classify_source(source, file_name)
        return self._build_payload(
            kind=kind,
            value=value,
            file_name=name,
            tier=requested,
            task_type=task_type,
            output_format=output_format,
            **kwargs,
        )


def _truncate(text: str, limit: int = _MAX_ERROR_BODY) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[:limit] + "... [truncated]"
