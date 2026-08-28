"""
Runtime patch for a litellm bug (confirmed present in 1.89.1 through the current
latest release, 1.98.0, identical code in both) in
litellm.utils.TextCompletionStreamWrapper.convert_to_text_completion_object.

Bug: when streaming against the legacy /v1/completions endpoint with
stream_options={"include_usage": True}, the provider (confirmed with Azure
OpenAI) sends a trailing chunk with choices=[] carrying only the usage
totals. litellm's converter does `chunk["choices"][0]["delta"]`
unconditionally, with no guard for an empty choices list, so that trailing
chunk raises IndexError, which litellm re-wraps and which surfaces to the
client as a 500.

This does NOT touch site-packages / the installed litellm package. It
replaces the method on the class object at runtime, from your own
application code, so it survives `pip install`/upgrade of litellm untouched
(though you should re-verify against the source, e.g. via the "verify"
function below, whenever you do upgrade, in case litellm ever changes this
method's internals in a way that makes the patch stale).

Usage, call once at process startup before serving any requests:

    from genai_litellm.patches.litellm_text_completion_stream_patch import (
        patch_text_completion_stream_usage_chunk,
    )
    patch_text_completion_stream_usage_chunk()
"""
import logging

from litellm.types.utils import Choices, TextChoices, TextCompletionResponse
from litellm.utils import TextCompletionStreamWrapper

logger = logging.getLogger(__name__)

_PATCH_MARKER = "_is_empty_choices_usage_chunk_patch"


def _patched_convert_to_text_completion_object(self, chunk):
    try:
        response = TextCompletionResponse()
        response["id"] = chunk.get("id", None)
        response["object"] = "text_completion"
        response["created"] = chunk.get("created", None)
        response["model"] = chunk.get("model", None)

        if isinstance(chunk, Choices):  # chunk should always be of type StreamingChoices
            raise Exception

        choices = chunk.get("choices") or []

        if not choices:
            # Trailing usage-only chunk from stream_options={"include_usage": True}.
            # litellm's own converter never guards this before indexing
            # choices[0] - this is the fix. No text choice to build, since
            # there's no delta on this chunk, only usage totals.
            response["choices"] = []
            if self.stream_options and self.stream_options.get("include_usage", False) is True:
                response["usage"] = chunk.get("usage", None)
            return response

        text_choices = TextChoices()
        delta = choices[0]["delta"]
        text_choices["text"] = delta["content"]
        text_choices["reasoning_content"] = delta.get("reasoning_content")
        text_choices["index"] = choices[0]["index"]
        text_choices["finish_reason"] = choices[0]["finish_reason"]
        response["choices"] = [text_choices]

        # only pass usage when stream_options["include_usage"] is True
        if self.stream_options and self.stream_options.get("include_usage", False) is True:
            response["usage"] = chunk.get("usage", None)

        return response
    except Exception as e:
        raise Exception(
            f"Error occurred converting to text completion object - chunk: {chunk}; Error: {str(e)}"
        )


def patch_text_completion_stream_usage_chunk() -> bool:
    """Apply the patch. Idempotent - safe to call more than once (e.g. from
    multiple startup hooks). Returns True if it applied the patch just now,
    False if it was already applied."""
    current = TextCompletionStreamWrapper.convert_to_text_completion_object
    if getattr(current, _PATCH_MARKER, False):
        logger.info("litellm TextCompletionStreamWrapper already patched, skipping")
        return False

    setattr(_patched_convert_to_text_completion_object, _PATCH_MARKER, True)
    TextCompletionStreamWrapper.convert_to_text_completion_object = (
        _patched_convert_to_text_completion_object
    )
    logger.info(
        "Patched litellm TextCompletionStreamWrapper.convert_to_text_completion_object "
        "for the empty-choices usage-chunk bug (see module docstring)"
    )
    return True


def is_patch_applied() -> bool:
    return getattr(
        TextCompletionStreamWrapper.convert_to_text_completion_object, _PATCH_MARKER, False
    )
