from pydantic import BaseModel, Field, model_validator
from typing import Optional, List, Literal, Union


# =========================================================
# 🔹 ENUMS / TYPES
# =========================================================

ImageSize = Literal["256x256", "512x512", "1024x1024"]
ImageQuality = Literal["low", "medium", "high"]
ImageBackground = Literal["auto", "transparent"]
ResponseFormat = Literal["b64_json", "url"]

Mode = Literal["direct", "tool"]


# =========================================================
# 🔹 REQUEST SCHEMA
# =========================================================

class ImageGenerationRequest(BaseModel):
    """
    Unified request schema supporting:
    - Direct image generation (gpt-image-1)
    - Tool-based generation (text model + image_generation tool)
    """

    # Core
    prompt: str = Field(..., description="Text prompt for image generation")

    # Mode
    mode: Mode = Field(
        default="direct",
        description="direct = gpt-image-1, tool = text model + image_generation tool"
    )

    # Model (flexible for future)
    model: str = Field(
        default="gpt-image-1",
        description="Model name (gpt-image-1 OR gpt-4.1-mini for tool mode)"
    )

    # Optional configs
    size: Optional[ImageSize] = "1024x1024"
    quality: Optional[ImageQuality] = "high"
    background: Optional[ImageBackground] = "auto"
    n: Optional[int] = Field(1, ge=1, le=10)
    response_format: Optional[ResponseFormat] = "b64_json"

    # Tool-specific
    tools: Optional[List[dict]] = None

    # -----------------------------------------------------
    # 🔥 VALIDATION LOGIC
    # -----------------------------------------------------

    @model_validator(mode="after")
    def validate_mode_and_model(self):
        if self.mode == "direct":
            if not self.model.startswith("gpt-image"):
                raise ValueError(
                    "Direct mode requires an image model like 'gpt-image-1'"
                )

        if self.mode == "tool":
            if "image" in self.model:
                raise ValueError(
                    "Tool mode requires a text model like 'gpt-4.1-mini'"
                )

            if not self.tools:
                self.tools = [{"type": "image_generation"}]

        return self


# =========================================================
# 🔹 RAW OPENAI RESPONSE SCHEMAS
# =========================================================

# -------- Direct Mode --------
class ImageGenerationDirect(BaseModel):
    id: Optional[str]
    type: Literal["image_generation"]
    image_base64: Optional[str] = None
    url: Optional[str] = None


# -------- Tool Mode --------
class ImageGenerationToolCall(BaseModel):
    id: Optional[str]
    type: Literal["image_generation_call"]
    result: str  # base64 image


# -------- Union --------
RawImageOutput = Union[
    ImageGenerationDirect,
    ImageGenerationToolCall
]


class OpenAIImageResponse(BaseModel):
    id: str
    object: Literal["response"]
    created: int
    model: str
    output: List[RawImageOutput]


# =========================================================
# 🔹 NORMALIZED INTERNAL RESPONSE (SDK STANDARD)
# =========================================================

class ImageData(BaseModel):
    b64: Optional[str] = None
    url: Optional[str] = None


class ImageResponse(BaseModel):
    type: Literal["image"] = "image"
    data: List[ImageData]
    metadata: Optional[dict] = None


# =========================================================
# 🔹 TRANSFORM / ADAPTER
# =========================================================

def normalize_image_response(resp: OpenAIImageResponse) -> ImageResponse:
    images: List[ImageData] = []

    for item in resp.output:

        # Direct API
        if item.type == "image_generation":
            images.append(
                ImageData(
                    b64=item.image_base64,
                    url=item.url
                )
            )

        # Tool-based
        elif item.type == "image_generation_call":
            images.append(
                ImageData(
                    b64=item.result
                )
            )

    return ImageResponse(
        data=images,
        metadata={
            "model": resp.model,
            "created": resp.created,
            "id": resp.id
        }
    )


# =========================================================
# 🔹 OPTIONAL: SAFE MODEL ROUTING (FOR SDK)
# =========================================================

SUPPORTED_IMAGE_MODELS = ["gpt-image-1"]


def validate_model(model: str, mode: Mode):
    if mode == "direct":
        if model not in SUPPORTED_IMAGE_MODELS:
            raise ValueError(f"Unsupported image model: {model}")
