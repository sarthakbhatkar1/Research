from pydantic import BaseModel, Field
from typing import Optional, List, Union, Literal, Annotated


# =========================================================
# 🔹 INPUT SCHEMA (OPENAI COMPLIANT)
# =========================================================

class InputText(BaseModel):
    type: Literal["input_text"]
    text: str


class InputImage(BaseModel):
    type: Literal["input_image"]
    image_url: Optional[str] = None
    image_base64: Optional[str] = None
    detail: Optional[Literal["low", "high", "auto", "original"]] = "auto"


class InputMessage(BaseModel):
    role: Literal["user"]
    content: List[Union[InputText, InputImage]]


# =========================================================
# 🔹 REQUEST SCHEMA
# =========================================================

class ImageGenerationRequest(BaseModel):
    """
    Fully OpenAI Responses API compliant request.
    Supports:
    - Direct image generation (gpt-image-1)
    - Tool-based generation (gpt-4.1 + image_generation tool)
    - Multimodal input (text + image)
    """

    model: str = "gpt-image-1"

    # OpenAI supports BOTH formats
    input: Union[str, List[InputMessage]]

    # Image configs (used in direct mode)
    size: Optional[Literal["256x256", "512x512", "1024x1024"]] = "1024x1024"
    quality: Optional[Literal["low", "medium", "high"]] = "high"
    background: Optional[Literal["auto", "transparent"]] = "auto"
    n: Optional[int] = Field(1, ge=1, le=10)
    response_format: Optional[Literal["b64_json", "url"]] = "b64_json"

    # Tool mode
    tools: Optional[List[dict]] = None


# =========================================================
# 🔹 RESPONSE SCHEMA (DISCRIMINATED UNION)
# =========================================================

class OutputText(BaseModel):
    type: Literal["output_text"]
    text: str


class ImageGeneration(BaseModel):
    type: Literal["image_generation"]
    image_base64: Optional[str] = None
    url: Optional[str] = None


class ImageGenerationCall(BaseModel):
    type: Literal["image_generation_call"]
    result: str  # base64


class ToolResult(BaseModel):
    type: Literal["tool_result"]
    output: Optional[list] = None


# 🔥 Discriminated union (CRITICAL for correctness)
ResponseItem = Annotated[
    Union[
        OutputText,
        ImageGeneration,
        ImageGenerationCall,
        ToolResult
    ],
    Field(discriminator="type")
]


class OpenAIResponse(BaseModel):
    id: str
    object: str
    created: int
    model: str
    output: List[ResponseItem]


# =========================================================
# 🔹 NORMALIZED INTERNAL RESPONSE (SDK STANDARD)
# =========================================================

class NormalizedImage(BaseModel):
    b64: Optional[str] = None
    url: Optional[str] = None


class NormalizedResponse(BaseModel):
    type: Literal["image"] = "image"
    data: List[NormalizedImage]
    metadata: Optional[dict] = None


# =========================================================
# 🔹 TRANSFORM / ADAPTER (ROBUST)
# =========================================================

def normalize_openai_response(resp: OpenAIResponse) -> NormalizedResponse:
    images: List[NormalizedImage] = []

    for item in resp.output:

        # Direct image output
        if item.type == "image_generation":
            images.append(
                NormalizedImage(
                    b64=item.image_base64,
                    url=item.url
                )
            )

        # Tool-based image output
        elif item.type == "image_generation_call":
            images.append(
                NormalizedImage(
                    b64=item.result
                )
            )

        # Nested tool results (future-proof)
        elif item.type == "tool_result" and item.output:
            for sub in item.output:
                if isinstance(sub, dict):
                    if sub.get("type") == "image_generation":
                        images.append(
                            NormalizedImage(
                                b64=sub.get("image_base64"),
                                url=sub.get("url")
                            )
                        )
                    elif sub.get("type") == "image_generation_call":
                        images.append(
                            NormalizedImage(
                                b64=sub.get("result")
                            )
                        )

    return NormalizedResponse(
        data=images,
        metadata={
            "model": resp.model,
            "created": resp.created,
            "id": resp.id
        }
    )


# =========================================================
# 🔹 HELPER UTILITIES (OPTIONAL BUT USEFUL)
# =========================================================

def extract_base64_images(resp: OpenAIResponse) -> List[str]:
    """Quick utility to get base64 images only"""
    normalized = normalize_openai_response(resp)
    return [img.b64 for img in normalized.data if img.b64]


def extract_image_urls(resp: OpenAIResponse) -> List[str]:
    """Quick utility to get URLs only"""
    normalized = normalize_openai_response(resp)
    return [img.url for img in normalized.data if img.url]
