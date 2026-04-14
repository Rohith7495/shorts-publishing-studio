from __future__ import annotations

import os
import re
from io import BytesIO
from itertools import combinations
from pathlib import Path
from typing import Any, Optional, Tuple

from app.prompts import VISION_SYSTEM_PROMPT, build_visual_user_prompt
from app.schemas import CoverTextCandidate, FrameInsight, FrameSample, VisionModelOutput


TITLE_MAX_LENGTH = 100
TITLE_TARGET_MIN_LENGTH = 95
TITLE_TARGET_IDEAL_LENGTH = 99
MAX_TITLE_TAGS = 3
MAX_SUFFIX_CANDIDATE_TAGS = 6
DESCRIPTION_MAX_HASHTAGS = 5
THUMBNAIL_TEXT_MAX_LENGTH = 42
COVER_TEXT_OPTION_COUNT = 3
HASHTAG_PATTERN = re.compile(r"#[A-Za-z0-9_]+")
WHITESPACE_PATTERN = re.compile(r"\s+")
SENTENCE_SPLIT_PATTERN = re.compile(r"(?<=[.!?])\s+")
CTA_SIGNAL_PATTERN = re.compile(r"\b(watch|stay|wait|see|catch)\b")
NON_WORD_THUMBNAIL_PATTERN = re.compile(r"[^A-Za-z0-9' ]+")
TITLE_TRAILING_STOPWORDS = {
    "a",
    "an",
    "and",
    "at",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


class GeminiVisionServiceError(RuntimeError):
    """Raised when Gemini-backed vision analysis cannot complete."""


class GeminiVisionService:
    def __init__(self, api_key: Optional[str], model_name: str, image_model_name: Optional[str] = None) -> None:
        self.api_key = api_key
        self.model_name = model_name
        self.image_model_name = image_model_name or ""
        self._client: Optional[Any] = None
        self._types_module: Optional[Any] = None

    def analyze_frames(
        self,
        frame_samples: list[FrameSample],
        max_titles: int,
        max_hashtags: int,
    ) -> Tuple[VisionModelOutput, list[str]]:
        if not frame_samples:
            raise GeminiVisionServiceError("No frames could be extracted from the uploaded video.")

        client, types_module = self._get_client_and_types()
        contents = self._build_request_contents(
            frame_samples=frame_samples,
            max_titles=max_titles,
            max_hashtags=max_hashtags,
            types_module=types_module,
        )

        try:
            response = client.models.generate_content(
                model=self.model_name,
                contents=contents,
                config={
                    "response_mime_type": "application/json",
                    "response_json_schema": VisionModelOutput.model_json_schema(),
                },
            )
        except Exception as error:
            raise GeminiVisionServiceError(f"Gemini vision request failed: {error}") from error

        response_text = getattr(response, "text", None)
        if not response_text:
            raise GeminiVisionServiceError("Gemini returned an empty response.")

        try:
            parsed = VisionModelOutput.model_validate_json(response_text)
        except Exception as error:
            raise GeminiVisionServiceError(f"Gemini returned invalid structured output: {error}") from error

        normalized = self._normalize_output(parsed, frame_samples, max_titles, max_hashtags)
        notes = [
            f"Analyzed {len(frame_samples)} sampled frames with Gemini model `{self.model_name}`.",
            "Hook titles, descriptions, hashtags, objects, and frame insights were generated from visual evidence only.",
            "Each title was normalized to stay within 100 characters, lean into curiosity, and end with relevant hashtags.",
            "Descriptions were formatted into hook, context, CTA, and hashtag lines for YouTube Shorts.",
        ]
        return normalized, notes

    def generate_cover_source_image(
        self,
        reference_image_path: Path,
        destination_path: Path,
        cover_text: str,
        visual_basis: str,
        frame_summary: str,
        width: Optional[int],
        height: Optional[int],
    ) -> Path:
        client, types_module = self._get_client_and_types()
        prompt = self._build_cover_image_prompt(
            cover_text=cover_text,
            visual_basis=visual_basis,
            frame_summary=frame_summary,
            width=width,
            height=height,
        )

        try:
            response = client.models.generate_content(
                model=self.image_model_name,
                contents=[
                    prompt,
                    self._part_from_path(types_module, reference_image_path),
                ],
                config={
                    "response_modalities": ["TEXT", "IMAGE"],
                },
            )
        except Exception as error:
            raise GeminiVisionServiceError(f"Gemini image generation failed: {error}") from error

        image_bytes = self._extract_generated_image_bytes(response)
        if image_bytes is None:
            raise GeminiVisionServiceError("Gemini image generation did not return an image.")

        destination_path.parent.mkdir(parents=True, exist_ok=True)
        image_module = self._import_pillow_image_module()
        try:
            with image_module.open(BytesIO(image_bytes)) as generated_image:
                generated_image.convert("RGB").save(destination_path, format="JPEG", quality=94, optimize=True)
        except Exception as error:
            raise GeminiVisionServiceError(f"Gemini returned an unreadable image payload: {error}") from error

        return destination_path

    def _get_client_and_types(self) -> tuple[Any, Any]:
        if self._client is not None and self._types_module is not None:
            return self._client, self._types_module

        try:
            from google import genai  # type: ignore
            from google.genai import types  # type: ignore
        except ImportError as error:
            raise GeminiVisionServiceError(
                "The `google-genai` package is not installed. Run `pip install -r requirements.txt` in the backend virtual environment."
            ) from error

        api_key = self.api_key or os.getenv("GEMINI_API_KEY")
        if not api_key:
            raise GeminiVisionServiceError(
                "GEMINI_API_KEY is not set. Add it to the backend environment before generating hooks."
            )

        self._client = genai.Client(api_key=api_key)
        self._types_module = types
        return self._client, self._types_module

    def _build_request_contents(
        self,
        frame_samples: list[FrameSample],
        max_titles: int,
        max_hashtags: int,
        types_module: Any,
    ) -> list[Any]:
        contents: list[Any] = [
            VISION_SYSTEM_PROMPT,
            build_visual_user_prompt(
                frame_samples=frame_samples,
                max_titles=max_titles,
                max_hashtags=max_hashtags,
            ),
        ]

        for index, sample in enumerate(frame_samples, start=1):
            contents.append(f"Frame {index} timestamp: {sample.timestamp_seconds:.2f} seconds.")
            contents.append(self._part_from_path(types_module, Path(sample.image_path)))

        return contents

    def _normalize_output(
        self,
        output: VisionModelOutput,
        frame_samples: list[FrameSample],
        max_titles: int,
        max_hashtags: int,
    ) -> VisionModelOutput:
        output.hashtags = self._normalize_hashtags(output.hashtags, max_hashtags)
        output.hook_titles = self._normalize_hook_titles(output.hook_titles, output.hashtags, max_titles)
        output.descriptions = self._normalize_descriptions(
            output.descriptions,
            output.visual_basis,
            output.hashtags,
        )
        output.first_comment_text = self._normalize_first_comment(output.first_comment_text, output.visual_basis)
        output.detected_objects = self._normalize_detected_objects(output.detected_objects)
        output.frame_insights = self._normalize_frame_insights(output.frame_insights, frame_samples)
        return output

    def _normalize_hook_titles(
        self,
        hook_titles: list[Any],
        hashtags: list[str],
        max_titles: int,
    ) -> list[Any]:
        normalized: list[Any] = []

        for item in hook_titles[:max_titles]:
            base_title = self._clean_title_text(str(item.text))
            available_tags = self._collect_title_tag_candidates(
                title_hashtags=self._extract_hashtags(str(item.text)),
                global_hashtags=hashtags,
            )

            item.text = self._compose_title_near_limit(base_title, available_tags)
            item.score = self._normalize_score(item.score)
            normalized.append(item)

        normalized.sort(key=lambda item: item.score, reverse=True)
        return normalized[:max_titles]

    def _normalize_descriptions(
        self,
        descriptions: list[Any],
        visual_basis: str,
        hashtags: list[str],
    ) -> list[Any]:
        normalized: list[Any] = []
        hashtag_line = self._build_description_hashtag_line(hashtags)

        for item in descriptions[:2]:
            lines = self._build_description_lines(
                description_text=str(item.text),
                angle=str(item.angle),
                visual_basis=visual_basis,
                hashtag_line=hashtag_line,
            )
            item.text = "\n".join(lines)
            normalized.append(item)

        return normalized

    def _normalize_hashtags(self, hashtags: list[str], max_hashtags: int) -> list[str]:
        normalized: list[str] = []
        seen: set[str] = set()

        for item in hashtags:
            cleaned = self._normalize_hashtag_value(item)
            if not cleaned:
                continue
            if cleaned in seen:
                continue
            seen.add(cleaned)
            normalized.append(cleaned)
            if len(normalized) >= max_hashtags:
                break

        if "#shorts" not in seen:
            normalized.insert(0, "#shorts")

        return normalized[:max_hashtags]

    def _normalize_hashtag_value(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        if not cleaned.startswith("#"):
            cleaned = f"#{cleaned}"
        return cleaned.replace(" ", "").lower()

    def _extract_hashtags(self, value: str) -> list[str]:
        extracted: list[str] = []
        seen: set[str] = set()

        for match in HASHTAG_PATTERN.findall(value):
            cleaned = self._normalize_hashtag_value(match)
            if not cleaned or cleaned in seen:
                continue
            seen.add(cleaned)
            extracted.append(cleaned)

        return extracted

    def _normalize_thumbnail_text(self, value: Any, visual_basis: str) -> str:
        raw_text = WHITESPACE_PATTERN.sub(" ", str(value or "")).strip()
        raw_text = raw_text.replace("#", "")
        raw_text = NON_WORD_THUMBNAIL_PATTERN.sub("", raw_text)
        raw_text = WHITESPACE_PATTERN.sub(" ", raw_text).strip()

        if not raw_text:
            raw_text = visual_basis

        words = raw_text.split()
        if len(words) > 7:
            words = words[:7]
        normalized = " ".join(words).strip()

        if len(normalized) > THUMBNAIL_TEXT_MAX_LENGTH:
            normalized = normalized[:THUMBNAIL_TEXT_MAX_LENGTH].rsplit(" ", 1)[0].strip() or normalized[:THUMBNAIL_TEXT_MAX_LENGTH]

        return normalized.upper()

    def _normalize_cover_text_options(
        self,
        options: list[Any],
        primary_text: Any,
        visual_basis: str,
    ) -> list[CoverTextCandidate]:
        normalized: list[CoverTextCandidate] = []
        seen: set[str] = set()

        def add_candidate(text_value: Any, score_value: Any, fallback_score: float = 8.5) -> None:
            cleaned = self._normalize_thumbnail_text(text_value, visual_basis)
            if not cleaned or cleaned in seen:
                return
            seen.add(cleaned)
            normalized.append(
                CoverTextCandidate(
                    text=cleaned,
                    score=self._normalize_score(score_value if score_value is not None else fallback_score),
                )
            )

        for item in options[:COVER_TEXT_OPTION_COUNT]:
            add_candidate(getattr(item, "text", item), getattr(item, "score", None))

        add_candidate(primary_text, 9.0)

        for fallback_text in self._build_cover_text_fallbacks(
            primary_text=self._normalize_thumbnail_text(primary_text, visual_basis),
            visual_basis=visual_basis,
        ):
            add_candidate(fallback_text, 7.8)
            if len(normalized) >= COVER_TEXT_OPTION_COUNT:
                break

        normalized.sort(key=lambda item: item.score, reverse=True)
        return normalized[:COVER_TEXT_OPTION_COUNT]

    def _build_cover_text_fallbacks(self, primary_text: str, visual_basis: str) -> list[str]:
        seeds: list[str] = []
        base = primary_text or self._normalize_thumbnail_text(visual_basis, visual_basis)
        words = [word for word in base.split() if word]

        if base:
            seeds.append(base)

        if words:
            focus = " ".join(words[: min(3, len(words))]).strip()
            if focus:
                seeds.append(f"WATCH {focus}")
                seeds.append(f"WAIT FOR {focus}")
                seeds.append(f"{focus} REVEAL")

        return seeds

    def _normalize_thumbnail_timestamp(
        self,
        value: Optional[float],
        frame_samples: list[FrameSample],
    ) -> Optional[float]:
        if not frame_samples:
            return None

        try:
            target = float(value) if value is not None else frame_samples[len(frame_samples) // 2].timestamp_seconds
        except (TypeError, ValueError):
            target = frame_samples[len(frame_samples) // 2].timestamp_seconds

        best_sample = min(
            frame_samples,
            key=lambda sample: abs(sample.timestamp_seconds - target),
        )
        return best_sample.timestamp_seconds

    def _normalize_first_comment(self, value: Any, visual_basis: str) -> str:
        cleaned = WHITESPACE_PATTERN.sub(" ", str(value or "")).strip()
        if not cleaned:
            cleaned = f"What would you do in this moment? {visual_basis}"
        if len(cleaned) > 220:
            cleaned = cleaned[:220].rsplit(" ", 1)[0].strip() or cleaned[:220]
        return cleaned

    def _collect_title_tag_candidates(
        self,
        title_hashtags: list[str],
        global_hashtags: list[str],
    ) -> list[str]:
        ordered: list[str] = []
        seen: set[str] = set()

        for tag in [*title_hashtags, *global_hashtags]:
            if not tag or tag in seen:
                continue
            seen.add(tag)
            ordered.append(tag)

        prioritized = [tag for tag in ordered if tag != "#shorts"]

        if "#shorts" in seen:
            prioritized.append("#shorts")

        if not prioritized:
            return ["#shorts"]

        return prioritized[:MAX_SUFFIX_CANDIDATE_TAGS]

    def _clean_title_text(self, value: str) -> str:
        cleaned = HASHTAG_PATTERN.sub("", value)
        cleaned = cleaned.replace("_", " ")
        cleaned = WHITESPACE_PATTERN.sub(" ", cleaned)
        cleaned = cleaned.strip().strip("\"'`")
        cleaned = cleaned.rstrip(" ,;:/|-")
        return cleaned or "Watch The Reveal"

    def _compose_title_near_limit(self, title_text: str, available_tags: list[str]) -> str:
        fallback = self._truncate_text(title_text, TITLE_MAX_LENGTH)
        candidates: list[str] = []
        seen: set[str] = set()

        unique_tags: list[str] = []
        unique_seen: set[str] = set()
        for tag in available_tags:
            if not tag or tag in unique_seen:
                continue
            unique_seen.add(tag)
            unique_tags.append(tag)

        upper_size = min(MAX_TITLE_TAGS, len(unique_tags))
        for size in range(upper_size, 0, -1):
            for combo in combinations(unique_tags, size):
                ordered_combo = self._order_title_tags(list(combo))
                candidate = self._compose_title_with_tags(title_text, ordered_combo)
                if candidate and candidate not in seen:
                    seen.add(candidate)
                    candidates.append(candidate)

        if not candidates:
            return fallback

        return max(candidates, key=self._score_title_candidate)

    def _compose_title_with_tags(self, title_text: str, hashtags: list[str]) -> str:
        ordered_tags = self._order_title_tags(hashtags)
        if not ordered_tags:
            return self._truncate_text(title_text, TITLE_MAX_LENGTH)

        suffix = f" {' '.join(ordered_tags)}"
        available_length = TITLE_MAX_LENGTH - len(suffix)
        if available_length <= 0:
            return self._truncate_text(title_text, TITLE_MAX_LENGTH)

        base = self._truncate_text(title_text, available_length)
        if not base:
            return self._truncate_text(title_text, TITLE_MAX_LENGTH)

        return f"{base}{suffix}"

    def _order_title_tags(self, hashtags: list[str]) -> list[str]:
        topical = [tag for tag in hashtags if tag != "#shorts"]
        shorts = [tag for tag in hashtags if tag == "#shorts"]
        return topical + shorts

    def _score_title_candidate(self, candidate: str) -> tuple[int, int, int, int]:
        length = len(candidate)
        hashtags = self._extract_hashtags(candidate)
        curiosity_signals = [
            "this",
            "why",
            "what",
            "how",
            "when",
            "moment",
            "before",
            "after",
            "until",
            "suddenly",
        ]
        lowercase = candidate.lower()
        curiosity_bonus = sum(1 for token in curiosity_signals if token in lowercase)
        under_target_penalty = max(TITLE_TARGET_MIN_LENGTH - length, 0)
        ideal_distance = abs(TITLE_TARGET_IDEAL_LENGTH - length)
        shorts_bonus = 1 if "#shorts" in hashtags else 0

        return (
            -under_target_penalty,
            -ideal_distance,
            curiosity_bonus + shorts_bonus,
            len(hashtags),
        )

    def _build_description_lines(
        self,
        description_text: str,
        angle: str,
        visual_basis: str,
        hashtag_line: str,
    ) -> list[str]:
        cleaned_text = WHITESPACE_PATTERN.sub(" ", description_text.replace("\n", " ")).strip()
        sentences = [
            sentence.strip()
            for sentence in SENTENCE_SPLIT_PATTERN.split(cleaned_text)
            if sentence.strip()
        ]
        hook_source = sentences[0] if sentences else visual_basis

        hook_line = self._ensure_line_punctuation(
            self._pick_description_hook(sentences, angle, visual_basis)
        )
        context_line = self._ensure_line_punctuation(
            self._pick_description_context(sentences, hook_source, visual_basis)
        )
        cta_line = self._ensure_line_punctuation(
            self._pick_description_cta(sentences, angle)
        )

        return [hook_line, context_line, cta_line, hashtag_line]

    def _pick_description_hook(
        self,
        sentences: list[str],
        angle: str,
        visual_basis: str,
    ) -> str:
        if sentences:
            first = sentences[0]
            if self._looks_like_hook(first):
                return first
            return self._make_hook_like(first, angle)

        return self._make_hook_like(visual_basis, angle)

    def _pick_description_context(
        self,
        sentences: list[str],
        hook_source: str,
        visual_basis: str,
    ) -> str:
        for sentence in sentences:
            if sentence != hook_source:
                return self._make_context_like(sentence)
        return self._make_context_like(visual_basis)

    def _pick_description_cta(self, sentences: list[str], angle: str) -> str:
        for sentence in sentences:
            lowered = sentence.lower()
            if CTA_SIGNAL_PATTERN.search(lowered):
                return sentence

        if "curiosity" in angle.lower():
            return "Watch till the end to catch the payoff."
        return "Stay till the end to see the full moment land."

    def _make_hook_like(self, value: str, angle: str) -> str:
        base = self._truncate_text(value, 72)
        if self._looks_like_hook(base):
            return base

        prefix = "You will want to see why"
        if "reveal" in angle.lower():
            prefix = "Wait till you see how"
        elif "hook" in angle.lower():
            prefix = "You will want to see what"

        combined = f"{prefix} {base.lower()}"
        return self._truncate_text(combined, 78)

    def _make_context_like(self, value: str) -> str:
        base = self._truncate_text(value, 78)
        lowered = base.lower()
        if lowered.startswith(("watch", "see", "wait", "you will", "you won't")):
            return f"This Short shows {lowered}."
        if lowered.endswith((".", "!", "?")):
            return base
        return f"This Short shows {lowered}."

    def _build_description_hashtag_line(self, hashtags: list[str]) -> str:
        selected = hashtags[:DESCRIPTION_MAX_HASHTAGS]
        if "#shorts" not in selected and "#shorts" in hashtags:
            if len(selected) >= DESCRIPTION_MAX_HASHTAGS:
                selected = [selected[0], *selected[1:DESCRIPTION_MAX_HASHTAGS - 1], "#shorts"]
            else:
                selected.append("#shorts")
        if not selected:
            selected = ["#shorts"]
        return " ".join(selected)

    def _looks_like_hook(self, value: str) -> bool:
        lowered = value.lower()
        return any(
            token in lowered
            for token in ("why", "what", "how", "wait", "watch", "guess", "until", "before", "moment")
        )

    def _ensure_line_punctuation(self, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            return ""
        if cleaned.endswith((".", "!", "?")):
            return cleaned
        return f"{cleaned}."

    def _truncate_text(self, value: str, max_length: int) -> str:
        cleaned = WHITESPACE_PATTERN.sub(" ", value).strip()
        if len(cleaned) <= max_length:
            return self._strip_trailing_connectors(cleaned.rstrip(" ,;:/|-"))

        clipped = cleaned[:max_length].rstrip()
        if max_length >= 20 and " " in clipped:
            clipped = clipped.rsplit(" ", 1)[0].rstrip()

        clipped = self._strip_trailing_connectors(clipped.rstrip(" ,;:/|-"))
        if clipped:
            return clipped

        return cleaned[:max_length].strip()

    def _strip_trailing_connectors(self, value: str) -> str:
        trimmed = value.strip()
        while " " in trimmed:
            last_word = trimmed.rsplit(" ", 1)[-1].lower()
            if last_word not in TITLE_TRAILING_STOPWORDS:
                break
            trimmed = trimmed.rsplit(" ", 1)[0].rstrip(" ,;:/|-")
        return trimmed

    def _normalize_score(self, value: Any) -> float:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return 7.5
        return round(min(max(numeric, 1.0), 10.0), 1)

    def _build_cover_image_prompt(
        self,
        cover_text: str,
        visual_basis: str,
        frame_summary: str,
        width: Optional[int],
        height: Optional[int],
    ) -> str:
        orientation = "portrait" if height and width and height > width else "landscape"
        ratio = f"{width}:{height}" if width and height else ("9:16" if orientation == "portrait" else "16:9")
        return f"""
Use case: photorealistic-natural
Asset type: YouTube Shorts cover background
Primary request: Create a hyperrealistic, highly clickable YouTube Shorts cover image based on the provided reference frame.
Input images: Image 1: reference frame from the uploaded video
Scene/backdrop: {visual_basis}
Subject: {frame_summary}
Style/medium: cinematic photoreal image, premium thumbnail-quality realism
Composition/framing: same aspect ratio as the source video, stronger focal point, cleaner composition, visually dramatic, mobile-first readability
Lighting/mood: high contrast, polished, vivid, dramatic but believable
Text (verbatim): "{cover_text}"
Constraints: preserve the core subject and scene topic from the frame; generate a new thumbnail-style image that feels more intense and polished than the raw frame; keep it realistic; no collage; no split panels; no watermark; no logos; no UI chrome; no subtitles; do not render any text into the image
Avoid: cartoon look, illustration look, blurry output, surreal distortions, extra hands, duplicate objects, unreadable clutter
Additional instruction: treat the quoted text as hook intent only, not as text to draw. The final image should be in a {orientation} composition matching approximately {ratio}.
""".strip()

    def _normalize_detected_objects(self, detected_objects: list[Any]) -> list[Any]:
        unique: dict[str, Any] = {}
        for item in detected_objects:
            label = item.label.strip().lower()
            if not label:
                continue
            count = max(int(item.count), 1)
            if label not in unique or unique[label].count < count:
                item.label = label
                item.count = count
                unique[label] = item
        return sorted(unique.values(), key=lambda item: (-item.count, item.label))

    def _normalize_frame_insights(
        self,
        frame_insights: list[FrameInsight],
        frame_samples: list[FrameSample],
    ) -> list[FrameInsight]:
        insight_map = {round(item.timestamp_seconds, 2): item for item in frame_insights}
        normalized: list[FrameInsight] = []

        for sample in frame_samples:
            timestamp = round(sample.timestamp_seconds, 2)
            if timestamp in insight_map:
                insight = insight_map[timestamp]
                insight.timestamp_seconds = timestamp
                normalized.append(insight)
                continue

            normalized.append(
                FrameInsight(
                    timestamp_seconds=timestamp,
                    summary="The model did not return a frame-specific note for this timestamp.",
                    tags=[],
                )
            )

        return normalized

    @staticmethod
    def _part_from_path(types_module: Any, image_path: Path) -> Any:
        suffix = image_path.suffix.lower()
        mime_type = "image/png" if suffix == ".png" else "image/jpeg"
        return types_module.Part.from_bytes(
            data=image_path.read_bytes(),
            mime_type=mime_type,
        )

    @staticmethod
    def _extract_generated_image_bytes(response: Any) -> Optional[bytes]:
        candidates = []

        direct_parts = getattr(response, "parts", None)
        if direct_parts:
            candidates.extend(direct_parts)

        for candidate in getattr(response, "candidates", []) or []:
            content = getattr(candidate, "content", None)
            parts = getattr(content, "parts", None) if content is not None else None
            if parts:
                candidates.extend(parts)

        for part in candidates:
            inline_data = getattr(part, "inline_data", None)
            data = getattr(inline_data, "data", None) if inline_data is not None else None
            if data:
                return data

        return None

    @staticmethod
    def _import_pillow_image_module() -> Any:
        try:
            from PIL import Image  # type: ignore
        except ImportError as error:
            raise GeminiVisionServiceError(
                "Pillow is required for AI-generated cover handling. Install it with `pip install -r requirements.txt`."
            ) from error

        return Image
