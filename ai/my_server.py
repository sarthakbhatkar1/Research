"""
Image Flow Studio — Artifact-based conversational campaign generation.

Two models required:
  • GPT-4o — reasoning, vision analysis, prompt construction
  • gpt-image-2 — actual image generation/editing

Flow:
1. User uploads files → they become "artifacts" in the session
2. User sends a message and optionally attaches artifact IDs
3. Only attached artifacts are used for that specific generation
4. GPT-4o sees attached images inline → builds a rich prompt
5. gpt-image-2 generates/edits → result saved as a new artifact
6. Logos attached to a message are composited via PIL on the output
"""

import os
import json
import base64
import hashlib
import time
import io
import uuid
from typing import Optional

import fitz  # pip install pymupdf
from fastapi import FastAPI, UploadFile, File, Form, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from openai import AzureOpenAI
from PIL import Image as PILImage

# —— Config ————————————————————————————————————————————————
os.environ.setdefault("AZURE_OPENAI_ENDPOINT", "https://lostvaynesarthak-2644-resource.services.ai.azure.com")
os.environ.setdefault("AZURE_API_VERSION", "2025-04-01-preview")

UI_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(UI_DIR, "generated")
os.makedirs(OUTPUT_DIR, exist_ok=True)

app = FastAPI(title="Image Flow Studio")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# —— Session state ————————————————————————————————————————————
# Artifacts: all uploaded files + generated images available for reuse
_artifacts: dict[str, dict] = {}  # id → {id, name, type, category, bytes, filename, src_b64}
_conversation: list[dict] = []
_last_image_path: Optional[str] = None
# Brand guidelines (extracted once from PDF, injected into every call)
_brand_guidelines: Optional[dict] = None


# —— System Prompt ————————————————————————————————————————————
SYSTEM_PROMPT = """You are "Image Flow", a creative campaign designer AI.

You generate and edit campaign images through natural conversation.

## TOOLS
- generate_image: Creates a NEW image from a detailed prompt
- edit_image: Modifies an attached image based on instructions

## CONTEXT ON ATTACHMENTS (per-message):
The user may attach artifacts to their message. These are categorized:
- LOGO: Will be composited AUTOMATICALLY on top by the system. Do NOT include logos in your prompt. Leave top-left and bottom-right corners empty for logo placement.
- CREATIVE: The generated image MUST closely replicate this creative's layout, composition, and style.
- REFERENCE: Use for color/font/style guidance. Incorporate the visual style you see.
- GENERATED: A previously generated image that can be edited or used as reference.

## RULES
1. When asked to create/generate → call generate_image with a detailed prompt (150+ words). Include specific colors (hex codes), composition, typography style, mood, layout.
2. When asked to edit/modify an existing image → call edit_image with change instructions.
3. NEVER mention logos or brand names in image prompts — logos are composited automatically.
4. If no image generation is needed → respond with text only.
5. Leave top-left and bottom-right corners empty for logo placement when logos are attached.
6. Be concise in text responses, very detailed in image prompts.
7. If a CREATIVE is attached, describe its exact layout in your prompt so the output matches.
8. When a previously generated image is auto-attached and the user describes changes, ALWAYS use edit_image (not generate_image) to modify it.
9. If BRAND GUIDELINES are provided, ALWAYS use the specified colors (hex codes), fonts, and visual style in your prompts. This is mandatory for every image generation.
"""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "generate_image",
            "description": "Generate a brand new image. Use when creating something new.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "Detailed visual description (150+ words). Include style, colors, composition, mood, typography. Do NOT mention logos or brand names."
                    },
                    "size": {
                        "type": "string",
                        "enum": ["1024x1024", "1536x1024", "1024x1536"],
                    }
                },
                "required": ["prompt"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "edit_image",
            "description": "Edit an attached image. Use for modifications to existing images.",
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {
                        "type": "string",
                        "description": "What to change. Be specific. Do NOT mention logos."
                    },
                    "size": {
                        "type": "string",
                        "enum": ["1024x1024", "1536x1024", "1024x1536"],
                    }
                },
                "required": ["prompt"]
            }
        }
    }
]


# —— Helpers ————————————————————————————————————————————————
def _get_client():
    key = os.environ.get("AZURE_OPENAI_API_KEY", "")
    if not key:
        raise HTTPException(500, "AZURE_OPENAI_API_KEY not set")
    return AzureOpenAI(
        api_version=os.environ["AZURE_API_VERSION"],
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=key,
    )


def _to_png_bytes(raw: bytes) -> bytes:
    img = PILImage.open(io.BytesIO(raw)).convert("RGBA")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _save_image(img_b64: str, prompt: str) -> tuple[str, str]:
    ts = int(time.time())
    slug = hashlib.md5(prompt[:80].encode()).hexdigest()[:6]
    filename = f"campaign_{ts}_{slug}.png"
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "wb") as f:
        f.write(base64.b64decode(img_b64))
    return filename, path


def _composite_logos(base_path: str, logos: list[dict]) -> None:
    """Overlay logo artifacts on the generated image."""
    if not logos:
        return
    base = PILImage.open(base_path).convert("RGBA")
    w, h = base.size
    pad = int(w * 0.03)

    positions = [
        (pad, pad, 0.18),          # top-left
        (None, None, 0.14),        # bottom-right (calculated)
        (None, None, 0.12),        # top-right (calculated)
    ]

    for i, logo_data in enumerate(logos[:3]):
        logo = PILImage.open(io.BytesIO(logo_data["bytes"])).convert("RGBA")
        scale = positions[i][2] if i < len(positions) else 0.12
        lw = int(w * scale)
        lh = int(logo.height * (lw / logo.width))
        logo = logo.resize((lw, lh), PILImage.LANCZOS)

        if i == 0:
            base.paste(logo, (pad, pad), logo)
        elif i == 1:
            base.paste(logo, (w - lw - pad, h - lh - pad), logo)
        elif i == 2:
            base.paste(logo, (w - lw - pad, pad), logo)

    base.convert("RGB").save(base_path, "PNG")


def _execute_generate(prompt: str, size: str, logos: list[dict]) -> dict:
    """Generate image via gpt-image-2, composite logos."""
    global _last_image_path
    client = _get_client()

    result = client.images.generate(
        model="gpt-image-2", prompt=prompt, n=1, quality="medium", size=size,
    )
    data = json.loads(result.model_dump_json())["data"][0]

    img_b64 = data.get("b64_json")
    if not img_b64 and data.get("url"):
        import urllib.request
        with urllib.request.urlopen(data["url"]) as resp:
            img_b64 = base64.b64encode(resp.read()).decode()

    if not img_b64:
        raise Exception("No image data returned")

    filename, path = _save_image(img_b64, prompt)
    _composite_logos(path, logos)

    with open(path, "rb") as f:
        final_b64 = base64.b64encode(f.read()).decode()
    _last_image_path = path
    return {"filename": filename, "image_base64": final_b64, "mode": "generate"}


def _execute_edit(prompt: str, size: str, source_bytes: bytes, logos: list[dict]) -> dict:
    """Edit an image via gpt-image-2, composite logos."""
    global _last_image_path
    client = _get_client()

    png_bytes = _to_png_bytes(source_bytes)

    result = client.images.edit(
        model="gpt-image-2", image=("image.png", png_bytes, "image/png"),
        prompt=prompt, n=1, size=size,
    )
    data = json.loads(result.model_dump_json())["data"][0]

    img_b64 = data.get("b64_json")
    if not img_b64 and data.get("url"):
        import urllib.request
        with urllib.request.urlopen(data["url"]) as resp:
            img_b64 = base64.b64encode(resp.read()).decode()

    if not img_b64:
        raise Exception("No image data returned")

    filename, path = _save_image(img_b64, prompt)
    _composite_logos(path, logos)

    with open(path, "rb") as f:
        final_b64 = base64.b64encode(f.read()).decode()
    _last_image_path = path
    return {"filename": filename, "image_base64": final_b64, "mode": "edit"}


# —— Serve Static UI ————————————————————————————————————————————
@app.get("/")
async def index():
    return FileResponse(os.path.join(UI_DIR, "index.html"))


@app.get("/styles.css")
async def css():
    return FileResponse(os.path.join(UI_DIR, "styles.css"), media_type="text/css")


@app.get("/app.js")
async def js_file():
    return FileResponse(os.path.join(UI_DIR, "app.js"), media_type="application/javascript")


@app.get("/generated/{filename}")
async def serve_generated(filename: str):
    safe_name = os.path.basename(filename)
    path = os.path.join(OUTPUT_DIR, safe_name)
    if not os.path.isfile(path):
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")


# —— Artifact Management ————————————————————————————————————————
@app.post("/api/artifacts/upload")
async def upload_artifact(
    file: UploadFile = File(...),
    category: str = Form(...),  # "logo" | "reference" | "creative"
):
    """Upload a file as an artifact. Returns artifact metadata."""
    raw = await file.read()
    artifact_id = str(uuid.uuid4())[:8]

    # Generate thumbnail base64
    try:
        img = PILImage.open(io.BytesIO(raw)).convert("RGB")
        img.thumbnail((150, 150))
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=70)
        thumb_b64 = base64.b64encode(buf.getvalue()).decode()
    except Exception:
        thumb_b64 = base64.b64encode(raw[:100]).decode()

    artifact = {
        "id": artifact_id,
        "name": file.filename,
        "category": category,    # logo, reference, creative
        "type": "uploaded",
        "bytes": raw,
        "thumb_b64": thumb_b64,
    }
    _artifacts[artifact_id] = artifact

    return {
        "id": artifact_id,
        "name": file.filename,
        "category": category,
        "type": "uploaded",
        "thumb_b64": thumb_b64,
    }


@app.get("/api/artifacts")
async def list_artifacts():
    """List all artifacts in the session."""
    return [
        {
            "id": a["id"],
            "name": a["name"],
            "category": a["category"],
            "type": a["type"],
            "thumb_b64": a["thumb_b64"],
        }
        for a in _artifacts.values()
    ]


@app.delete("/api/artifacts/{artifact_id}")
async def delete_artifact(artifact_id: str):
    if artifact_id in _artifacts:
        del _artifacts[artifact_id]
    return {"status": "ok"}


# —— Chat Endpoint ————————————————————————————————————————————
@app.post("/api/chat")
async def chat(
    message: str = Form(...),
    attached_artifacts: str = Form(default=""),  # comma-separated artifact IDs
):
    """
    Conversational endpoint. Only uses artifacts explicitly attached by their IDs.
    No auto-inclusion of logos/refs from previous calls.
    """
    global _last_image_path

    client = _get_client()

    # —— Resolve attached artifacts ————————————————————————————
    attached_ids = [aid.strip() for aid in attached_artifacts.split(",") if aid.strip()]
    attached = [_artifacts[aid] for aid in attached_ids if aid in _artifacts]

    logos = [a for a in attached if a["category"] == "logo"]
    creatives = [a for a in attached if a["category"] == "creative"]
    references = [a for a in attached if a["category"] == "reference"]
    generated = [a for a in attached if a["category"] == "generated"]

    # —— Auto-attach last generated image if nothing is attached ——
    # When user sends a message with no artifacts, auto-include the
    # most recent generated image so edits apply to it seamlessly.
    auto_attached_last = False
    if not attached and _last_image_path and os.path.isfile(_last_image_path):
        # Find the last generated artifact
        last_gen = None
        for a in reversed(list(_artifacts.values())):
            if a["category"] == "generated":
                last_gen = a
                break
        if last_gen:
            generated = [last_gen]
            auto_attached_last = True

    # —— Build user message for GPT-4o ————————————————————————
    user_content = [{"type": "text", "text": message}]

    # Include creative images for GPT-4o to see
    for c in creatives[:1]:
        b64 = base64.b64encode(c["bytes"]).decode()
        user_content.append({"type": "text", "text": "[CREATIVE attached — match this layout/style]"})
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}})

    # Include references for GPT-4o to see
    for r in references[:3]:
        b64 = base64.b64encode(r["bytes"]).decode()
        user_content.append({"type": "text", "text": "[REFERENCE — use for style guidance]"})
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "low"}})

    # Include generated images being edited
    for g in generated[:1]:
        b64 = base64.b64encode(g["bytes"]).decode()
        label = "[Last generated image — auto-attached for editing]" if auto_attached_last else "[GENERATED image attached — available for editing]"
        user_content.append({"type": "text", "text": label})
        user_content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}})

    _conversation.append({"role": "user", "content": user_content})

    # —— Build context for system prompt ————————————————————————
    context_parts = []
    if logos:
        context_parts.append(f"\n\n## THIS MESSAGE: {len(logos)} logo(s) attached — will be auto-composited. Leave corners empty.")
    if creatives:
        context_parts.append("\n\n## THIS MESSAGE: Creative attached — closely match its layout.")
    if references:
        context_parts.append(f"\n\n## THIS MESSAGE: {len(references)} reference(s) attached — use their style/colors.")
    if generated and auto_attached_last:
        context_parts.append("\n\n## THIS MESSAGE: The last generated image is auto-attached. If the user is requesting changes/edits, use edit_image. If they want something completely new, use generate_image.")
    elif generated:
        context_parts.append("\n\n## THIS MESSAGE: Previously generated image attached — can be edited via edit_image.")

    # Inject brand guidelines if available (always present)
    if _brand_guidelines:
        context_parts.append(f"\n\n## BRAND GUIDELINES (always apply these):\n{json.dumps(_brand_guidelines, indent=2)}")

    system = SYSTEM_PROMPT + "".join(context_parts)
    messages = [{"role": "system", "content": system}] + _conversation

    # —— Call GPT-4o (reasoning model) ————————————————————————
    try:
        response = client.chat.completions.create(
            model="gpt-5.4",
            messages=messages,
            tools=TOOLS,
            tool_choice="auto",
            max_completion_tokens=1000,
            temperature=0.7,
        )
    except Exception as e:
        raise HTTPException(500, f"Chat failed: {e}")

    choice = response.choices[0]
    assistant_msg = choice.message

    # —— Handle response ————————————————————————————————————————
    image_result = None
    text_response = ""
    new_artifact = None

    if assistant_msg.tool_calls:
        tool_call = assistant_msg.tool_calls[0]
        fn_name = tool_call.function.name
        fn_args = json.loads(tool_call.function.arguments)

        logo_data = [{"bytes": l["bytes"]} for l in logos]

        try:
            if fn_name == "generate_image":
                image_result = _execute_generate(
                    prompt=fn_args["prompt"],
                    size=fn_args.get("size", "1024x1024"),
                    logos=logo_data,
                )
            elif fn_name == "edit_image":
                # Find source image: attached generated > attached creative > last image
                source_bytes = None
                if generated:
                    source_bytes = generated[0]["bytes"]
                elif creatives:
                    source_bytes = creatives[0]["bytes"]
                elif _last_image_path and os.path.isfile(_last_image_path):
                    with open(_last_image_path, "rb") as f:
                        source_bytes = f.read()

                if source_bytes:
                    image_result = _execute_edit(
                        prompt=fn_args["prompt"],
                        size=fn_args.get("size", "1024x1024"),
                        source_bytes=source_bytes,
                        logos=logo_data,
                    )
                else:
                    # No source → generate instead
                    image_result = _execute_generate(
                        prompt=fn_args["prompt"],
                        size=fn_args.get("size", "1024x1024"),
                        logos=logo_data,
                    )
        except Exception as e:
            text_response = f"Sorry, image generation failed: {str(e)}"

        # Save tool interaction to conversation
        _conversation.append({
            "role": "assistant", "content": None,
            "tool_calls": [{"id": tool_call.id, "type": "function", "function": {"name": fn_name, "arguments": tool_call.function.arguments}}]
        })
        _conversation.append({
            "role": "tool", "tool_call_id": tool_call.id,
            "content": json.dumps({"status": "success" if image_result else "failed", "filename": image_result["filename"] if image_result else None})
        })

        # —— Save generated image as a new artifact ————————————————
        if image_result:
            art_id = str(uuid.uuid4())[:8]
            img_bytes = base64.b64decode(image_result["image_base64"])

            # Thumbnail
            img = PILImage.open(io.BytesIO(img_bytes)).convert("RGB")
            img.thumbnail((150, 150))
            buf = io.BytesIO()
            img.save(buf, format="JPEG", quality=70)
            thumb_b64 = base64.b64encode(buf.getvalue()).decode()

            _artifacts[art_id] = {
                "id": art_id,
                "name": image_result["filename"],
                "category": "generated",
                "type": "generated",
                "bytes": img_bytes,
                "thumb_b64": thumb_b64,
            }
            new_artifact = {
                "id": art_id,
                "name": image_result["filename"],
                "category": "generated",
                "type": "generated",
                "thumb_b64": thumb_b64,
            }

        # Get follow-up text
        if image_result and not text_response:
            followup_msgs = messages + [
                {"role": "assistant", "content": None, "tool_calls": [{"id": tool_call.id, "type": "function", "function": {"name": fn_name, "arguments": tool_call.function.arguments}}]},
                {"role": "tool", "tool_call_id": tool_call.id, "content": json.dumps({"status": "success", "filename": image_result["filename"]})},
            ]
            try:
                followup = client.chat.completions.create(
                    model="gpt-5.4", messages=followup_msgs, max_completion_tokens=300, temperature=0.7,
                )
                text_response = followup.choices[0].message.content or ""
            except Exception:
                text_response = "Here's your generated campaign image!"
    else:
        text_response = assistant_msg.content or ""

    _conversation.append({"role": "assistant", "content": text_response})

    return {
        "text": text_response,
        "image": image_result,
        "new_artifact": new_artifact,
        "artifacts_count": len(_artifacts),
    }


# —— Brand Guidelines (PDF upload + extraction) ————————————————————
def _extract_pdf_text_metadata(pdf_bytes: bytes) -> dict:
    """Extract fonts, colors, sizes from PDF text spans using PyMuPDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    fonts, colors, sizes = set(), set(), set()
    for page in doc:
        for block in page.get_text("dict")["blocks"]:
            if "lines" not in block:
                continue
            for line in block["lines"]:
                for span in line["spans"]:
                    fonts.add(span["font"])
                    sizes.add(round(span["size"], 1))
                    colors.add(f"#{span['color']:06x}")
    doc.close()
    return {"fonts": sorted(fonts), "colors": sorted(colors), "font_sizes": sorted(sizes)}


def _extract_pdf_visual_analysis(client, pdf_bytes: bytes, max_pages: int = 5) -> dict:
    """Send PDF pages as images to GPT-4o for visual brand analysis."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    content = [{
        "type": "text",
        "text": (
            "Analyze these brand guideline pages. Return a JSON object with:\n"
            "- brand_name\n- primary_colors (hex list)\n- secondary_colors (hex list)\n"
            "- background_colors (hex list)\n"
            "- typography: {heading_font, body_font, heading_weight, body_weight}\n"
            "- visual_style\n- tone\n- layout_rules\n- do_not_use (list)\n"
            "Return ONLY valid JSON, no markdown."
        )
    }]
    for i, page in enumerate(doc):
        if i >= max_pages:
            break
        pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
        b64 = base64.b64encode(pix.tobytes("png")).decode()
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/png;base64,{b64}", "detail": "high"}
        })
    doc.close()

    resp = client.chat.completions.create(
        model="gpt-5.4",
        messages=[
            {"role": "system", "content": "Brand design expert. Return valid JSON only."},
            {"role": "user", "content": content}
        ],
        max_completion_tokens=2000,
        temperature=0.1,
    )
    raw = resp.choices[0].message.content.strip()
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[1].rsplit("```", 1)[0]
    return json.loads(raw)


@app.post("/api/brand-guidelines")
async def upload_brand_guidelines(file: UploadFile = File(...)):
    """
    Upload a brand guidelines PDF. Extracts fonts, colors, styles via
    PyMuPDF + GPT-4o vision. Stored in session and injected into every call.
    """
    global _brand_guidelines

    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Only PDF files are accepted for brand guidelines")

    pdf_bytes = await file.read()
    client = _get_client()

    # Extract text metadata (fonts, colors, sizes)
    text_meta = _extract_pdf_text_metadata(pdf_bytes)

    # Extract visual analysis via GPT-4o
    try:
        visual = _extract_pdf_visual_analysis(client, pdf_bytes)
    except Exception as e:
        visual = {"error": f"Visual analysis failed: {str(e)[:100]}"}

    _brand_guidelines = {
        "source_file": file.filename,
        "fonts": text_meta["fonts"],
        "colors": text_meta["colors"],
        "font_sizes": text_meta["font_sizes"][:15],  # top sizes
        "visual_analysis": visual,
    }

    return {
        "status": "ok",
        "filename": file.filename,
        "guidelines": _brand_guidelines,
    }


@app.get("/api/brand-guidelines")
async def get_brand_guidelines():
    """Return current brand guidelines or null."""
    return {"guidelines": _brand_guidelines}


@app.delete("/api/brand-guidelines")
async def delete_brand_guidelines():
    global _brand_guidelines
    _brand_guidelines = None
    return {"status": "ok"}


# —— Session management ————————————————————————————————————————
@app.post("/api/new-session")
async def new_session():
    global _last_image_path, _brand_guidelines
    _conversation.clear()
    _artifacts.clear()
    _last_image_path = None
    _brand_guidelines = None
    return {"status": "ok"}


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "api_key_set": bool(os.environ.get("AZURE_OPENAI_API_KEY")),
        "conversation_length": len(_conversation),
        "artifacts_count": len(_artifacts),
        "has_last_image": _last_image_path is not None,
        "has_brand_guidelines": _brand_guidelines is not None,
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5500)