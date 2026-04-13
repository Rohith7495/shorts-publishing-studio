from app.prompts import build_visual_user_prompt
from app.schemas import FrameSample, HookTitleCandidate
from app.services.vision import GeminiVisionService


def test_visual_prompt_mentions_visual_only_scope_and_timestamps() -> None:
    prompt = build_visual_user_prompt(
        frame_samples=[
            FrameSample(timestamp_seconds=0.0, image_path="frame-1.jpg"),
            FrameSample(timestamp_seconds=3.5, image_path="frame-2.jpg"),
        ],
        max_titles=5,
        max_hashtags=15,
    )

    assert "Analyze the provided video frames" in prompt
    assert "0.00s, 3.50s" in prompt
    assert "exactly 5 curiosity-led YouTube Shorts titles" in prompt
    assert "exactly 2 YouTube Shorts descriptions" in prompt
    assert "100 characters or fewer" in prompt
    assert "95 to 100 characters" in prompt
    assert "line 4: hashtags" in prompt


def test_hashtag_normalization_adds_shorts_and_deduplicates() -> None:
    service = GeminiVisionService(api_key="test-key", model_name="gemini-2.5-flash-lite")

    hashtags = service._normalize_hashtags(
        ["cars", "#Cars", "Auto Motive", "#shorts"],
        max_hashtags=10,
    )

    assert hashtags[0] == "#shorts"
    assert "#cars" in hashtags
    assert "#automotive" in hashtags


def test_hook_title_normalization_adds_tags_and_caps_length() -> None:
    service = GeminiVisionService(api_key="test-key", model_name="gemini-2.5-flash-lite")

    titles = service._normalize_hook_titles(
        [
            HookTitleCandidate(
                text="You Wont Guess What Made Everyone Stop For This Wild Street Reveal Right At The End",
                score=12,
            )
        ],
        ["#shorts", "#streetshow", "#viralclip", "#streetlife"],
        max_titles=5,
    )

    assert len(titles) == 1
    assert "guess" in titles[0].text.lower()
    assert len(service._extract_hashtags(titles[0].text)) >= 2
    assert len(titles[0].text) >= 95
    assert len(titles[0].text) <= 100
    assert titles[0].score == 10.0


def test_description_normalization_formats_multiline_shorts_copy() -> None:
    service = GeminiVisionService(api_key="test-key", model_name="gemini-2.5-flash-lite")

    descriptions = service._normalize_descriptions(
        [
            type("Description", (), {"text": "You won't guess what happens next. A dramatic street moment unfolds fast.", "angle": "Curiosity"})(),
            type("Description", (), {"text": "This reveal lands hard. The clip stays focused on the visual payoff.", "angle": "Reveal"})(),
        ],
        "A dramatic street reveal with a strong visual payoff.",
        ["#streetshow", "#viralclip", "#shorts", "#citylife"],
    )

    lines = descriptions[0].text.splitlines()
    assert len(lines) == 4
    assert lines[-1].startswith("#")
    assert "#shorts" in lines[-1]
