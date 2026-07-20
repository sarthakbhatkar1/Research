"""
brand_pdf_extractor.py

Five-stage pipeline for extracting structured brand guideline data from a PDF.

Stage 1  Structural extraction (PyMuPDF)   -- deterministic, highest confidence
Stage 2  Page classification (phrase match) -- deterministic
Stage 3  Template layout extraction         -- geometric, heuristic classification
Stage 4  Semantic labeling (LLM)            -- the only stage that calls a model
Stage 5  OCR fallback                       -- triggered only for pages with no text layer

Output per run: two JSON files, sharing one run_id / timestamp, so later
stages or a re-run can find each other's output within the same run:

  <base_path>/<run_id>_layer1.json   raw, document-shaped (sections as found in the PDF)
  <base_path>/<run_id>_layer2.json   derived, schema-shaped (matches the product's
                                      brand_pdf_extraction.schema.json)

Layer 1 keeps only storage-path placeholders for images, never raw bytes,
so the persisted JSON stays small regardless of how many images a document
contains. Actual image files are written under <image_path>/<run_id>/.

Missing or low-confidence values in Layer 2 are represented as
role="unassigned" / null / empty, and logged in missingElements. This
pipeline never raises on incomplete extraction, a gap is data for the
onboarding UI to flag, not a failure.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# document_path = r"D:/work/paid/slice/research/data/Oranje/Brand Presentations/Oranje_Brand Guideline.pdf"
document_path = r"D:\work\paid\slice\research\data\additional\Slack\slack-2020.pdf"
# extracted_path = r"D:/work/paid/slice/research/data/extracted/slack"
extracted_path = r"D:/work/paid/slice/research/data/extracted/slack"

BASE_PATH = Path(document_path)
IMAGE_STORAGE_PATH = Path(extracted_path)

# DEFAULT_DOCUMENT_PATH = r"Mango.pdf"
DEFAULT_DOCUMENT_PATH = r"Slack\slack-2020.pdf"
# DEFAULT_EXTRACTED_PATH = r"extracted/mango"
DEFAULT_EXTRACTED_PATH = r"extracted/slack"

# Below this, a Stage 4 role/tag assignment gets flagged in missingElements
# rather than accepted silently.
MIN_CONFIDENCE_FOR_AUTO_ACCEPT = 0.7

# Stage 2's phrase library. Deliberately meant to grow as new documents
# surface phrasing not covered here, not a fixed, one-shot list.
REFERENCE_ONLY_PHRASES = [
    "images are used for reference only",
    "for reference only",
    "for illustrative purposes",
    "sample imagery",
    "images for illustration purposes only",
    "for illustration purposes only",
]

PLACEHOLDER_TEXT_PHRASES = [
    "lorem ipsum",
]

TEMPLATE_TYPE_KEYWORDS = {
    "social-post": ["instagram", "linkedin", "facebook", "twitter", "pinterest", "post"],
    "print": ["a3 ratio", "a4 ratio", "print design", "poster"],
    "id-card": ["id card", "identity card"],
    "badge": ["badge"],
    "certificate": ["certificate"],
    "letterhead": ["letterhead", "to, the manager"],
}

logger = logging.getLogger("brand_pdf_extractor")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    logger.warning("python-dotenv not installed, .env will not be loaded automatically; "
                    "set AZURE_OPENAI_API_KEY / AZURE_OPENAI_ENDPOINT / AZURE_API_VERSION / "
                    "AZURE_CHAT_MODEL some other way (shell env, etc).")


# ---------------------------------------------------------------------------
# LLM client: Azure OpenAI-compatible endpoint via LiteLLM
# ---------------------------------------------------------------------------
#
# Reads config from .env (via python-dotenv) or the environment directly,
# using the exact variable names already in use elsewhere in this project:
#   AZURE_OPENAI_API_KEY
#   AZURE_OPENAI_ENDPOINT
#   AZURE_API_VERSION
#   AZURE_CHAT_MODEL      -- the deployment name, used for all three LLM
#                            call sites (Stage 4, 4b, and 3's classifier),
#                            since only one chat deployment is configured.
#                            If you later deploy a second, cheaper model
#                            for Stage 3's lighter classification task,
#                            split this into two env vars and pass the
#                            second one through _classify_fields_llm's
#                            deployment argument instead.
#
# GPT_IMAGE_ENDPOINT / GPT_IMAGE_API_KEY are loaded but intentionally NOT
# wired into anything in this file. Nothing in this pipeline generates or
# needs an image-generation call, the only image-touching step is Stage
# 5's rare OCR fallback, which reads text off a rendered page, a
# different capability than what an image-generation deployment provides.
# Flag if you actually want GPT_IMAGE_* used for Stage 5 specifically,
# that's a real design choice, not a plumbing detail.


class _TextBlock:
    def __init__(self, text: str):
        self.text = text


class _LLMResponse:
    def __init__(self, text: str):
        self.content = [_TextBlock(text)]


class AzureFoundryClient:
    """
    Thin adapter over litellm.completion(), using the azure/ provider
    (Azure OpenAI-compatible, requires api_version), exposing the same
    llm_client.messages.create(model=..., max_tokens=..., messages=...)
    shape Stage 4/4b/3 already call. The `model` argument passed by
    callers is accepted for compatibility but ignored, the actual
    deployment is fixed to AZURE_CHAT_MODEL, since only one is configured.
    """

    def __init__(self, api_key: str, api_base: str, api_version: str, deployment: str):
        self.api_key = api_key
        self.api_base = api_base
        self.api_version = api_version
        self.deployment = deployment
        self.messages = self._Messages(self)

    class _Messages:
        def __init__(self, outer: "AzureFoundryClient"):
            self._outer = outer

        def create(self, model: str = None, max_tokens: int = 1000, messages=None, **kwargs) -> _LLMResponse:
            import litellm
            response = litellm.completion(
                model=f"azure/{self._outer.deployment}",
                api_key=self._outer.api_key,
                api_base=self._outer.api_base,
                api_version=self._outer.api_version,
                max_tokens=max_tokens,
                messages=messages,
            )
            text = response.choices[0].message.content
            return _LLMResponse(text)


def build_llm_client() -> Optional[AzureFoundryClient]:
    """Builds the Azure OpenAI-compatible client from .env / environment
    variables, or returns None (triggering graceful degradation in every
    stage that takes an llm_client) if any required value is missing."""
    api_key = os.environ.get("AZURE_OPENAI_API_KEY")
    api_base = os.environ.get("AZURE_OPENAI_ENDPOINT")
    api_version = os.environ.get("AZURE_API_VERSION")
    deployment = os.environ.get("AZURE_CHAT_MODEL")

    missing = [name for name, val in [
        ("AZURE_OPENAI_API_KEY", api_key),
        ("AZURE_OPENAI_ENDPOINT", api_base),
        ("AZURE_API_VERSION", api_version),
        ("AZURE_CHAT_MODEL", deployment),
    ] if not val]
    if missing:
        logger.warning(
            f"Missing env vars {missing}, Stage 4/4b/3-LLM will degrade "
            f"gracefully (heuristic/empty output, not a crash)."
        )
        return None
    return AzureFoundryClient(api_key=api_key, api_base=api_base, api_version=api_version, deployment=deployment)


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class ExtractedColor:
    hex: str
    rgb: Optional[dict] = None
    cmyk: Optional[dict] = None
    role: str = "unassigned"
    label: Optional[str] = None
    source_page: int = 0
    extraction_source: str = "drawn-fill"  # drawn-fill | text-label | inferred
    confidence: float = 0.0


@dataclass
class ExtractedFont:
    name: str
    matched_online: bool = False
    embedded_file_found: bool = False
    weights_found: list = field(default_factory=list)
    usage: str = "unspecified"


@dataclass
class ExtractedLogoVariant:
    variant: str
    asset_page: int
    usage_rule: str = ""
    clear_space: Optional[str] = None
    confidence: float = 0.0


@dataclass
class TemplateLayoutField:
    field: str  # logo | headline | subhead | cta | body
    position: dict  # {"x": float, "y": float, "width": float, "height": float}, all 0-1


@dataclass
class ExtractedTemplate:
    name: str
    type: str
    source_pages: list
    reference_image_detected: bool
    aspect_ratio: Optional[str] = None
    layout_fields: list = field(default_factory=list)
    status: str = "discovered"
    preview_image_path: Optional[str] = None


@dataclass
class MissingElement:
    field: str
    message: str
    severity: str = "warning"


@dataclass
class ExtractedSection:
    """Layer 1 unit. Preserves the source document's own structure, one
    entry per detected section, rather than forcing content into fixed
    product buckets up front. Different guideline PDFs contain different
    sections; this generalizes across all of them."""
    name: str
    category: str
    start_page: int
    end_page: int
    rules: list = field(default_factory=list)
    image_refs: list = field(default_factory=list)  # [{"path": ..., "page": ...}], placeholders only
    raw_text: str = ""


# ---------------------------------------------------------------------------
# Stage 1: Structural extraction (PyMuPDF)
# ---------------------------------------------------------------------------

def stage1_structural_extraction(doc: fitz.Document, image_output_dir: Path) -> dict:
    """
    Per page: pull drawn vector shapes (get_drawings), positioned text
    spans with font metadata (get_text('dict')), embedded raster images
    with placement (get_images / get_image_rects), and detected fonts
    (get_fonts, embedded vs referenced-only).

    Purely positional and structural. No semantic judgment happens here,
    hence no model call. This is the highest-confidence stage in the
    pipeline: everything is read directly from the PDF's own rendering
    instructions, not inferred.
    """
    pages_data = []
    image_output_dir.mkdir(parents=True, exist_ok=True)

    for page_index in range(len(doc)):
        page = doc[page_index]
        page_num = page_index + 1

        # Vector drawings -> candidate color swatches. Filtered to simple
        # rectangular fills only (path complexity <= 4 items), which
        # excludes the dozens of tiny gradient-shaded paths that make up
        # complex logo artwork, confirmed necessary when this produced 25
        # false-positive "colors" from the Oranje PDF's textured logo page.
        swatches = []
        try:
            for d in page.get_drawings():
                fill = d.get("fill")
                rect = d.get("rect")
                items = d.get("items", [])
                if fill is None or rect is None or len(fill) != 3:
                    continue
                if len(items) > 4:
                    continue  # too complex to be a plain color swatch
                width, height = rect.width, rect.height
                if width < 10 or height < 10:
                    continue  # too small to be an intentional swatch
                if width * height > (page.rect.width * page.rect.height) * 0.5:
                    continue  # likely a full-page background fill, not a swatch
                r, g, b = [max(0, min(255, int(round(c * 255)))) for c in fill]
                swatches.append({
                    "hex": f"#{r:02X}{g:02X}{b:02X}",
                    "rgb": {"r": r, "g": g, "b": b},
                    "bbox": [rect.x0, rect.y0, rect.x1, rect.y1],
                })
        except Exception as e:
            logger.warning(f"page {page_num}: get_drawings failed, {e}")

        # Text spans, with position and font metadata
        spans = []
        try:
            text_dict = page.get_text("dict")
            for block in text_dict.get("blocks", []):
                for line in block.get("lines", []):
                    for span in line.get("spans", []):
                        txt = span.get("text", "").strip()
                        if txt:
                            spans.append({
                                "text": txt,
                                "bbox": list(span.get("bbox", [0, 0, 0, 0])),
                                "font": span.get("font"),
                                "size": span.get("size", 0),
                                "color": span.get("color"),
                            })
        except Exception as e:
            logger.warning(f"page {page_num}: get_text failed, {e}")

        # Embedded raster images, with placement bbox, written to disk as
        # PNG placeholders (path only ever goes into the JSON, not bytes)
        images = []
        for img_index, img in enumerate(page.get_images(full=True)):
            xref = img[0]
            bbox = None
            try:
                rects = page.get_image_rects(xref)
                if rects:
                    bbox = [rects[0].x0, rects[0].y0, rects[0].x1, rects[0].y1]
            except Exception:
                pass
            try:
                pix = fitz.Pixmap(doc, xref)
                if pix.n - pix.alpha >= 4:  # CMYK/other -> RGB before saving as PNG
                    pix = fitz.Pixmap(fitz.csRGB, pix)
                out_path = image_output_dir / f"p{page_num}_img{img_index}.png"
                pix.save(str(out_path))
                images.append({"path": str(out_path), "bbox": bbox, "xref": xref})
            except Exception as e:
                logger.warning(f"page {page_num} image {img_index}: extraction failed, {e}")

        fonts_on_page = []
        try:
            for f in page.get_fonts(full=True):
                fonts_on_page.append({"name": f[3], "embedded": bool(f[1])})
        except Exception as e:
            logger.warning(f"page {page_num}: get_fonts failed, {e}")

        pages_data.append({
            "page_num": page_num,
            "page_width": page.rect.width,
            "page_height": page.rect.height,
            "swatches": swatches,
            "spans": spans,
            "images": images,
            "fonts": fonts_on_page,
            "has_text_layer": len(spans) > 0,
        })

    logger.info(f"Stage 1 complete: {len(pages_data)} pages processed")
    return {"pages": pages_data}


def _pair_swatches_to_labels(pages_data: dict, max_label_distance: float = 80.0) -> list[ExtractedColor]:
    """
    Pair each drawn color swatch to its hex/rgb/cmyk text label using two
    strategies, tried in order:

      1. Horizontal containment: is the label's x-center inside the
         swatch's x-range? This correctly handles column/band-style
         swatches (a full-page-height color strip with its label near the
         top, far away in straight-line distance but clearly "above" the
         right swatch), confirmed necessary against the Oranje PDF's
         actual page-4 layout, which uses exactly this pattern.
      2. Nearest euclidean center distance, within max_label_distance,
         as a fallback for small square/point-style swatches where
         containment doesn't apply.

    If neither strategy finds a label within a sane range, no label is
    assigned rather than forcing a match to whatever's closest.
    """
    hex_pattern = re.compile(r"#[0-9A-Fa-f]{6}")
    colors: list[ExtractedColor] = []

    for page in pages_data["pages"]:
        hex_spans = [s for s in page["spans"] if hex_pattern.search(s["text"])]

        for swatch in page["swatches"]:
            sx0, sy0, sx1, sy1 = swatch["bbox"]
            sx = (sx0 + sx1) / 2
            sy = (sy0 + sy1) / 2

            label = None

            # Strategy 1: horizontal containment
            contained = [
                s for s in hex_spans
                if sx0 <= (s["bbox"][0] + s["bbox"][2]) / 2 <= sx1
            ]
            if len(contained) == 1:
                label = contained[0]["text"]
            elif len(contained) > 1:
                # multiple labels horizontally inside this swatch, take the
                # vertically nearest one rather than guessing
                label = min(
                    contained,
                    key=lambda s: abs((s["bbox"][1] + s["bbox"][3]) / 2 - sy)
                )["text"]

            # Strategy 2: nearest euclidean distance, fallback only
            if label is None:
                nearest_label, nearest_dist = None, float("inf")
                for s in hex_spans:
                    cx = (s["bbox"][0] + s["bbox"][2]) / 2
                    cy = (s["bbox"][1] + s["bbox"][3]) / 2
                    dist = ((cx - sx) ** 2 + (cy - sy) ** 2) ** 0.5
                    if dist < nearest_dist:
                        nearest_dist, nearest_label = dist, s["text"]
                if nearest_dist <= max_label_distance:
                    label = nearest_label

            colors.append(ExtractedColor(
                hex=swatch["hex"],
                rgb=swatch["rgb"],
                label=label,
                source_page=page["page_num"],
                extraction_source="drawn-fill",
                confidence=0.95 if label else 0.6,
            ))

    # de-duplicate identical hex values found on multiple pages, keep the
    # first occurrence and its page reference
    seen = {}
    for c in colors:
        if c.hex not in seen:
            seen[c.hex] = c
    deduped = list(seen.values())
    logger.info(f"Stage 1 color pairing: {len(colors)} swatches found, {len(deduped)} unique")
    return deduped


# ---------------------------------------------------------------------------
# Stage 2: Page classification (deterministic phrase matching)
# ---------------------------------------------------------------------------

def stage2_classify_pages(pages_data: dict) -> dict:
    """
    No model call. Flags, per page:
      reference_image_only     -- carries a 'reference/illustrative only'
                                   disclaimer; any embedded image on this
                                   page must not become a persisted brand
                                   asset.
      placeholder_text         -- contains lorem-ipsum style filler; its
                                   prose must be excluded from anything
                                   used as real brand voice/copy.
      likely_template_structure -- comparatively few words, several drawn
                                   shapes; candidate for Stage 3.

    REFERENCE_ONLY_PHRASES and PLACEHOLDER_TEXT_PHRASES are meant to grow
    as new documents surface phrasing not covered yet, this is not a
    one-shot classifier.
    """
    classified = []
    for page in pages_data["pages"]:
        full_text = " ".join(s["text"] for s in page["spans"]).lower()
        word_count = len(full_text.split())

        classified.append({
            "page_num": page["page_num"],
            "reference_image_only": any(p in full_text for p in REFERENCE_ONLY_PHRASES),
            "placeholder_text": any(p in full_text for p in PLACEHOLDER_TEXT_PHRASES),
            "likely_template_structure": len(page["swatches"]) >= 2 and word_count < 40,
            "word_count": word_count,
        })

    logger.info("Stage 2 complete: page classification done")
    return {"pages": classified}


# ---------------------------------------------------------------------------
# Stage 3: Template layout extraction (geometry + heuristic field typing)
# ---------------------------------------------------------------------------

def _classify_fields_llm(spans: list, llm_client, model: str = "configured-deployment") -> Optional[dict]:
    """
    Third LLM call site, separate from Stage 4 and Stage 4b, per the same
    two-calls-now-combine-later approach. Sends only structured span data
    (text, size, position), never a rendered image, and asks for a field
    type per span index. Returns None on any failure so the caller can
    fall back to the geometric heuristic rather than losing the page.
    """
    if llm_client is None:
        return None

    span_summary = [
        {"index": i, "text": s["text"][:80], "size": s["size"],
         "x": round(s["bbox"][0], 1), "y": round(s["bbox"][1], 1)}
        for i, s in enumerate(spans)
    ]
    prompt = f"""Classify each text span below from one page of a marketing
creative template. Use only the text, size, and position given, spans are
listed with their original size and top-left x/y position on the page.

Spans:
{json.dumps(span_summary)}

For each span index, assign exactly one type: headline, subhead, cta,
logo, body, or other. Respond with JSON only, exactly this shape:
{{"classifications": [{{"index": 0, "field": "headline"}}, ...]}}"""

    try:
        response = llm_client.messages.create(
            model=model, max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw)
        return {c["index"]: c["field"] for c in parsed.get("classifications", [])}
    except Exception as e:
        logger.warning(f"Stage 3 LLM field classification failed, falling back to heuristic: {e}")
        return None


def stage3_extract_templates(
    pages_data: dict,
    page_classification: dict,
    doc: fitz.Document,
    preview_output_dir: Path,
    llm_client=None,
) -> list[ExtractedTemplate]:
    """
    For pages Stage 2 flagged likely_template_structure: convert text-span
    bounding boxes into fractional (0-1) coordinates relative to actual
    page size, matching the product's existing layer model. Field type
    (headline vs subhead vs cta) is assigned either by an LLM call or the
    font-size/position heuristic fallback.

    Additionally renders the source page itself to a PNG and records its
    path as preview_image_path, so a discovered template has an actual
    visual asset to show in a Keep/Discard review UI, not just invisible
    coordinates. This is a full-page render, including whatever disclaimer
    text or surrounding chrome the page carries, a cropped-to-layout
    version is a reasonable follow-up, not done here.
    """
    templates: list[ExtractedTemplate] = []
    class_by_page = {c["page_num"]: c for c in page_classification["pages"]}
    preview_output_dir.mkdir(parents=True, exist_ok=True)

    for page in pages_data["pages"]:
        cls = class_by_page.get(page["page_num"])
        if not cls or not cls["likely_template_structure"]:
            continue

        spans = page["spans"]
        if not spans:
            continue

        page_w = page["page_width"] or 1
        page_h = page["page_height"] or 1
        spans_sorted = sorted(spans, key=lambda s: s["size"], reverse=True)

        llm_classification = _classify_fields_llm(spans, llm_client)

        layout_fields = []
        headline_text = spans_sorted[0]["text"]

        if llm_classification is not None:
            for i, s in enumerate(spans):
                field_type = llm_classification.get(i, "other")
                if field_type == "other":
                    continue
                layout_fields.append(TemplateLayoutField(
                    field=field_type,
                    position={
                        "x": s["bbox"][0] / page_w,
                        "y": s["bbox"][1] / page_h,
                        "width": (s["bbox"][2] - s["bbox"][0]) / page_w,
                        "height": (s["bbox"][3] - s["bbox"][1]) / page_h,
                    },
                ))
        else:
            # heuristic fallback: largest span is headline, next few are
            # subhead unless short and low on the page, then cta
            headline = spans_sorted[0]
            layout_fields.append(TemplateLayoutField(
                field="headline",
                position={
                    "x": headline["bbox"][0] / page_w,
                    "y": headline["bbox"][1] / page_h,
                    "width": (headline["bbox"][2] - headline["bbox"][0]) / page_w,
                    "height": (headline["bbox"][3] - headline["bbox"][1]) / page_h,
                },
            ))
            for s in spans_sorted[1:4]:
                is_bottom_area = (s["bbox"][1] / page_h) > 0.6
                field_type = "cta" if len(s["text"]) < 20 and is_bottom_area else "subhead"
                layout_fields.append(TemplateLayoutField(
                    field=field_type,
                    position={
                        "x": s["bbox"][0] / page_w,
                        "y": s["bbox"][1] / page_h,
                        "width": (s["bbox"][2] - s["bbox"][0]) / page_w,
                        "height": (s["bbox"][3] - s["bbox"][1]) / page_h,
                    },
                ))

        page_text_lower = " ".join(s["text"] for s in spans).lower()
        template_type = "other"
        for t_type, keywords in TEMPLATE_TYPE_KEYWORDS.items():
            if any(k in page_text_lower for k in keywords):
                template_type = t_type
                break

        preview_path = None
        try:
            fitz_page = doc[page["page_num"] - 1]
            pix = fitz_page.get_pixmap(dpi=150)
            preview_path = preview_output_dir / f"template_p{page['page_num']}_preview.png"
            pix.save(str(preview_path))
        except Exception as e:
            logger.warning(f"Stage 3: could not render preview for page {page['page_num']}, {e}")

        templates.append(ExtractedTemplate(
            name=headline_text[:60],
            type=template_type,
            source_pages=[page["page_num"]],
            reference_image_detected=cls["reference_image_only"],
            layout_fields=[asdict(f) for f in layout_fields],
            status="discovered",
            preview_image_path=str(preview_path) if preview_path else None,
        ))

    logger.info(f"Stage 3 complete: {len(templates)} candidate templates found "
                f"(field classification: {'LLM' if llm_client else 'heuristic'})")
    return templates


# ---------------------------------------------------------------------------
# Stage 4: Semantic labeling (the only stage that calls a model)
# ---------------------------------------------------------------------------

def stage4_semantic_labeling(
    colors: list[ExtractedColor],
    pages_data: dict,
    llm_client=None,
    model: str = "configured-deployment",
) -> tuple[list[ExtractedColor], dict, list[MissingElement]]:
    """
    Deliberately narrow: never sees raw pixels, only already-structured
    Stage 1 output (colors, plus surrounding text). Assigns a role to
    colors Stage 1 couldn't determine from context, and extracts
    mood/imagery/lighting/composition tags from descriptive prose into
    structured fields.

    If llm_client is None (no API key / not requested), this degrades
    gracefully: every color keeps role='unassigned', imagery fields stay
    empty, and a missingElements entry explains why. It never raises.
    """
    missing_elements: list[MissingElement] = []
    imagery = {"tags": [], "mood": [], "lighting": "", "composition": ""}

    if llm_client is None:
        logger.warning("Stage 4: no LLM client provided, semantic labeling skipped")
        missing_elements.append(MissingElement(
            field="colors",
            message="Semantic labeling was skipped (no LLM client configured). "
                     "All colors default to role=unassigned.",
            severity="warning",
        ))
        return colors, imagery, missing_elements

    full_text = "\n".join(s["text"] for p in pages_data["pages"] for s in p["spans"])
    color_list_str = "\n".join(f"- {c.hex} (nearby label: {c.label or 'none'})" for c in colors)

    prompt = f"""You are labeling brand guideline data that has already been
structurally extracted from a PDF. Do not invent colors or values not
listed below, only work with what is given.

Colors found (hex value plus any printed label near that swatch):
{color_list_str}

Surrounding document text, for context only:
{full_text[:6000]}

For each color listed above, assign a role: primary, secondary, accent,
neutral, background, or unassigned if the text gives no real basis to
decide (do not guess when there is no signal). Also extract, only if
actually present in the text: imagery tags, mood tags, a lighting
descriptor, and a composition rule.

Respond with JSON only, exactly this shape, no other text:
{{"colors": [{{"hex": "...", "role": "...", "confidence": 0.0}}],
  "imagery": {{"tags": [], "mood": [], "lighting": "", "composition": ""}}}}"""

    try:
        response = llm_client.messages.create(
            model=model,
            max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw)

        role_by_hex = {c["hex"]: c for c in parsed.get("colors", [])}
        for c in colors:
            match = role_by_hex.get(c.hex)
            if match:
                c.role = match.get("role", "unassigned")
                c.confidence = float(match.get("confidence", 0.5))
            if c.role == "unassigned" or c.confidence < MIN_CONFIDENCE_FOR_AUTO_ACCEPT:
                missing_elements.append(MissingElement(
                    field=f"colors[{c.hex}].role",
                    message=f"Color {c.hex} could not be confidently assigned a role "
                             f"(confidence={c.confidence:.2f}).",
                    severity="warning",
                ))

        imagery = parsed.get("imagery", imagery)

    except Exception as e:
        logger.error(f"Stage 4: LLM call/parse failed, {e}")
        missing_elements.append(MissingElement(
            field="colors",
            message=f"Semantic labeling failed ({e}). All colors default to role=unassigned.",
            severity="warning",
        ))

    logger.info(f"Stage 4 complete: {len(missing_elements)} missing-element flags raised")
    return colors, imagery, missing_elements


def stage4b_extract_logo_and_doavoid(
    sections: list[ExtractedSection],
    llm_client=None,
    model: str = "configured-deployment",
) -> tuple[list[ExtractedLogoVariant], dict, list[MissingElement]]:
    """
    Second, separate LLM call (kept independent from
    stage4_semantic_labeling per the decision to run two calls initially,
    combine them later once both are proven out). Scopes its context to
    sections whose name suggests logo-related content, falls back to the
    full document text if nothing matches, then asks the model for
    per-variant usage rules and do/avoid lists.

    An empty result here (logo_variants=[] / do_avoid={"do": [], "avoid": []})
    is itself the signal the UI uses to flag "nothing found", per the
    agreed rule, so this function only adds a missingElements entry when
    the call outright fails, not when it legitimately finds nothing.
    """
    if llm_client is None:
        logger.warning("Stage 4b: no LLM client provided, logo/do-avoid extraction skipped")
        return [], {"do": [], "avoid": []}, [MissingElement(
            field="logo",
            message="Logo usage and do/avoid extraction was skipped, no LLM client configured.",
            severity="warning",
        )]

    logo_sections = [s for s in sections if "logo" in s.name.lower()]
    context_sections = logo_sections if logo_sections else sections
    context_text = "\n\n".join(f"[{s.name}]\n{s.raw_text[:2000]}" for s in context_sections)

    prompt = f"""You are extracting logo usage rules and general do/avoid
guidance from brand guideline text that has already been extracted from a
PDF. Only use what is actually stated below, do not invent rules that
aren't there.

Document text:
{context_text[:6000]}

Identify any distinct logo variants mentioned (for example: full color,
single-tone, grayscale, textured) and any stated usage rule or clear
space rule for each. Separately, list any general "do" and "avoid"
guidance stated anywhere in the text (not specific to one logo variant).
If nothing is stated for a given thing, leave that list empty, do not
guess or pad it.

Respond with JSON only, exactly this shape, no other text:
{{"logo_variants": [{{"variant": "full-color|single-tone|grayscale|textured|other",
                       "usage_rule": "...", "clear_space": "..." }}],
  "do_avoid": {{"do": [], "avoid": []}}}}"""

    try:
        response = llm_client.messages.create(
            model=model,
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        raw = re.sub(r"^```(json)?|```$", "", raw.strip(), flags=re.MULTILINE).strip()
        parsed = json.loads(raw)

        allowed_variants = {"full-color", "single-tone", "grayscale", "textured", "other"}
        logo_variants = []
        for v in parsed.get("logo_variants", []):
            variant = v.get("variant", "other")
            if variant not in allowed_variants:
                variant = "other"
            logo_variants.append(ExtractedLogoVariant(
                variant=variant,
                asset_page=logo_sections[0].start_page if logo_sections else 0,
                usage_rule=v.get("usage_rule", "") or "",
                clear_space=v.get("clear_space") or None,
                confidence=0.75,
            ))

        do_avoid = parsed.get("do_avoid", {"do": [], "avoid": []})
        logger.info(f"Stage 4b complete: {len(logo_variants)} logo variants, "
                    f"{len(do_avoid.get('do', []))} do / {len(do_avoid.get('avoid', []))} avoid rules found")
        return logo_variants, do_avoid, []

    except Exception as e:
        logger.error(f"Stage 4b: LLM call/parse failed, {e}")
        return [], {"do": [], "avoid": []}, [MissingElement(
            field="logo",
            message=f"Logo usage and do/avoid extraction failed ({e}).",
            severity="warning",
        )]


# ---------------------------------------------------------------------------
# Stage 5: OCR fallback (triggered only for pages with no extractable text)
# ---------------------------------------------------------------------------

def stage5_ocr_fallback(doc: fitz.Document, pages_data: dict) -> dict:
    """
    Fires only for pages where Stage 1 found has_text_layer == False, never
    as a default first pass, since most guideline PDFs exported from a
    design tool have a real text layer throughout. Requires pytesseract
    plus a local tesseract binary; degrades to a logged warning, not a
    crash, if unavailable.
    """
    try:
        import pytesseract
        from PIL import Image
    except ImportError:
        logger.warning("Stage 5: pytesseract/Pillow not installed, OCR fallback unavailable")
        return pages_data

    pages_needing_ocr = [p for p in pages_data["pages"] if not p["has_text_layer"]]
    if not pages_needing_ocr:
        logger.info("Stage 5: no pages required OCR fallback")
        return pages_data

    for page_entry in pages_needing_ocr:
        page_num = page_entry["page_num"]
        page = doc[page_num - 1]
        try:
            pix = page.get_pixmap(dpi=300)
            img = Image.open(io.BytesIO(pix.tobytes("png")))
            ocr_text = pytesseract.image_to_string(img)
            if ocr_text.strip():
                page_entry["spans"].append({
                    "text": ocr_text.strip(),
                    "bbox": [0, 0, pix.width, pix.height],
                    "font": "ocr-fallback",
                    "size": 0,
                    "color": 0,
                })
                page_entry["has_text_layer"] = True
                logger.info(f"Stage 5: OCR recovered text on page {page_num}")
            else:
                logger.warning(f"Stage 5: OCR found no text on page {page_num}")
        except Exception as e:
            logger.warning(f"Stage 5: OCR failed on page {page_num}, {e}")

    return pages_data


# ---------------------------------------------------------------------------
# Section detection (builds Layer 1's document-shaped structure)
# ---------------------------------------------------------------------------

def detect_sections(pages_data: dict, page_classification: dict) -> list[ExtractedSection]:
    """
    Groups pages into sections using a first-pass heuristic: a page whose
    largest text span is meaningfully larger than the document's median
    span size is treated as a new section start. This will not be perfect
    on every layout, real section boundaries in design-led PDFs don't
    always follow one consistent typographic rule, but it's a reasonable
    default and cheap to override later.
    """
    all_sizes = [s["size"] for p in pages_data["pages"] for s in p["spans"] if s["size"]]
    median_size = sorted(all_sizes)[len(all_sizes) // 2] if all_sizes else 12
    heading_threshold = median_size * 1.8

    class_by_page = {c["page_num"]: c for c in page_classification["pages"]}
    sections: list[ExtractedSection] = []
    current: Optional[ExtractedSection] = None

    for page in pages_data["pages"]:
        spans = page["spans"]
        largest = max(spans, key=lambda s: s["size"], default=None) if spans else None
        is_new_section = largest is not None and largest["size"] >= heading_threshold

        if current is None or is_new_section:
            if current is not None:
                sections.append(current)
            current = ExtractedSection(
                name=largest["text"][:80] if largest else f"Untitled (p{page['page_num']})",
                category="other",
                start_page=page["page_num"],
                end_page=page["page_num"],
                raw_text=" ".join(s["text"] for s in spans),
            )
        else:
            current.end_page = page["page_num"]
            current.raw_text += " " + " ".join(s["text"] for s in spans)

        cls = class_by_page.get(page["page_num"], {})
        if cls.get("placeholder_text"):
            current.rules.append(
                f"[placeholder text detected on page {page['page_num']}, "
                f"excluded from voice/copy reference]"
            )

        for img in page["images"]:
            current.image_refs.append({"path": img["path"], "page": page["page_num"]})

    if current is not None:
        sections.append(current)

    logger.info(f"Section detection complete: {len(sections)} sections found")
    return sections


# ---------------------------------------------------------------------------
# Persistence: timestamped Layer 1 / Layer 2 JSON
# ---------------------------------------------------------------------------

def _to_camel_case(snake_str: str) -> str:
    parts = snake_str.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:])


def _camelize_keys(obj):
    """Recursively converts dict keys from snake_case to camelCase, so
    persisted JSON matches brand_pdf_extraction.schema.json's field
    naming (sourcePage, layoutFields, etc), rather than the Python-native
    snake_case the dataclasses use internally."""
    if isinstance(obj, dict):
        return {_to_camel_case(k): _camelize_keys(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_camelize_keys(v) for v in obj]
    return obj


def new_run_id() -> str:
    """Timestamp-based run id, shared by Layer 1 and Layer 2 output for a
    single run, so later stages (or a resumed run) can find each other's
    files without any separate lookup table."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")[:-3] + "Z"


def save_layer1_json(sections: list[ExtractedSection], run_id: str, base_path: Path) -> Path:
    """
    Layer 1: raw, document-shaped extraction. image_refs holds storage
    path placeholders only, never raw bytes, so this file's size does not
    scale with how many images the source document contains.
    """
    base_path.mkdir(parents=True, exist_ok=True)
    out_path = base_path / f"{run_id}_layer1.json"
    payload = {
        "run_id": run_id,
        "layer": 1,
        "extracted_at": datetime.now(timezone.utc).isoformat(),
        "sections": [asdict(s) for s in sections],
    }
    payload = _camelize_keys(payload)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Layer 1 saved: {out_path}")
    return out_path


def save_layer2_json(
    colors: list[ExtractedColor],
    fonts: list[ExtractedFont],
    logo_variants: list[ExtractedLogoVariant],
    imagery: dict,
    do_avoid: dict,
    templates: list[ExtractedTemplate],
    missing_elements: list[MissingElement],
    run_id: str,
    source_filename: str,
    page_count: int,
    base_path: Path,
) -> Path:
    """
    Layer 2: derived, schema-shaped output matching
    brand_pdf_extraction.schema.json. Missing fields are represented as
    null / role='unassigned' / empty arrays, per the agreed rule, this
    function never raises on incomplete data.
    """
    base_path.mkdir(parents=True, exist_ok=True)
    out_path = base_path / f"{run_id}_layer2.json"

    primary_font = asdict(fonts[0]) if fonts else asdict(ExtractedFont(name="unknown"))
    secondary_font = asdict(fonts[1]) if len(fonts) > 1 else None

    payload = {
        "source": {
            "fileName": source_filename,
            "pageCount": page_count,
            "extractedAt": datetime.now(timezone.utc).isoformat(),
            "extractionMethod": "hybrid",
        },
        "colors": [asdict(c) for c in colors],
        "typography": {
            "primaryFont": primary_font,
            "secondaryFont": secondary_font,
            "fallbackNeeded": len(fonts) < 2,
        },
        "logo": [asdict(v) for v in logo_variants],
        "imagery": imagery,
        "doAvoid": do_avoid,
        "templates": [asdict(t) for t in templates],
        "missingElements": [asdict(m) for m in missing_elements],
    }
    payload = _camelize_keys(payload)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    logger.info(f"Layer 2 saved: {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

def run_pipeline(
    pdf_path: str,
    base_path: Path = BASE_PATH,
    image_path: Path = IMAGE_STORAGE_PATH,
    llm_client=None,
) -> dict:
    """Runs all five stages in order against one PDF, writes Layer 1 and
    Layer 2 JSON under base_path, named by a shared run_id timestamp."""
    run_id = new_run_id()
    logger.info(f"Starting extraction run {run_id} for {pdf_path}")

    doc = fitz.open(pdf_path)
    run_image_dir = image_path / run_id

    pages_data = stage1_structural_extraction(doc, run_image_dir)
    page_classification = stage2_classify_pages(pages_data)

    # Stage 5 fires only for pages Stage 1 found no text layer on; re-run
    # Stage 2's classification afterward so any newly-OCR'd text is seen.
    pages_data = stage5_ocr_fallback(doc, pages_data)
    page_classification = stage2_classify_pages(pages_data)

    templates = stage3_extract_templates(
        pages_data, page_classification, doc=doc,
        preview_output_dir=run_image_dir / "template_previews",
        llm_client=llm_client,
    )
    colors = _pair_swatches_to_labels(pages_data)

    seen_fonts: dict[str, ExtractedFont] = {}
    for page in pages_data["pages"]:
        for f in page["fonts"]:
            if f["name"] not in seen_fonts:
                seen_fonts[f["name"]] = ExtractedFont(name=f["name"], embedded_file_found=f["embedded"])
    fonts = list(seen_fonts.values())

    colors, imagery, missing_elements = stage4_semantic_labeling(colors, pages_data, llm_client=llm_client)

    sections = detect_sections(pages_data, page_classification)

    logo_variants, do_avoid, logo_missing = stage4b_extract_logo_and_doavoid(sections, llm_client=llm_client)
    missing_elements.extend(logo_missing)

    layer1_path = save_layer1_json(sections, run_id, base_path)
    layer2_path = save_layer2_json(
        colors=colors,
        fonts=fonts,
        logo_variants=logo_variants,
        imagery=imagery,
        do_avoid=do_avoid,
        templates=templates,
        missing_elements=missing_elements,
        run_id=run_id,
        source_filename=os.path.basename(pdf_path),
        page_count=len(doc),
        base_path=base_path,
    )

    doc.close()
    logger.info(f"Run {run_id} complete. Layer 1: {layer1_path} | Layer 2: {layer2_path}")
    return {
        "run_id": run_id,
        "layer1_path": str(layer1_path),
        "layer2_path": str(layer2_path),
        "sections_found": len(sections),
        "colors_found": len(colors),
        "templates_found": len(templates),
        "missing_elements": len(missing_elements),
    }


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Local default paths (edit these for your machine, or override via CLI args)
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Brand guideline PDF extraction pipeline")
    parser.add_argument("pdf_path", nargs="?", default=DEFAULT_DOCUMENT_PATH,
                         help=f"Path to the brand guideline PDF (default: {DEFAULT_DOCUMENT_PATH})")
    parser.add_argument("--base-path", default=DEFAULT_EXTRACTED_PATH,
                         help=f"Directory to write Layer 1/2 JSON into (default: {DEFAULT_EXTRACTED_PATH})")
    parser.add_argument("--image-path", default=str(Path(DEFAULT_EXTRACTED_PATH) / "images"),
                         help="Directory to write extracted images into")
    parser.add_argument("--use-llm", action="store_true", help="Enable Stage 4/4b semantic labeling via Claude")
    args = parser.parse_args()

    client = None
    if args.use_llm:
        client = build_llm_client()

    result = run_pipeline(
        args.pdf_path,
        base_path=Path(args.base_path),
        image_path=Path(args.image_path),
        llm_client=client,
    )
    print(json.dumps(result, indent=2))
