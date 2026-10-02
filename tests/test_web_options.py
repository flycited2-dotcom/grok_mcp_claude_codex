import pytest

from grok_imagine_mcp.drivers import DriverError
from grok_imagine_mcp.models import ImageRequest, VideoRequest
from grok_imagine_mcp.web_options import image_options, video_options

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


def test_plain_video_request_passes_through_without_notes():
    opts, notes = video_options(VideoRequest(prompt="лодка", duration_s=10, resolution="720p",
                                             aspect_ratio="16:9", generate_audio=False))
    assert (opts.prompt, opts.duration_s, opts.resolution, opts.aspect_ratio, opts.generate_audio) == \
        ("лодка", 10, "720p", "16:9", False)
    assert opts.image_paths == () and notes == []


@pytest.mark.parametrize("seconds", [1, 5, 8, 12])
def test_duration_outside_the_ui_choices_is_refused_with_the_list(seconds):
    with pytest.raises(DriverError, match="6, 10, 15"):
        video_options(VideoRequest(prompt="x", duration_s=seconds))


@pytest.mark.parametrize("ratio", ["4:3", "3:4"])
def test_aspect_the_ui_does_not_have_is_refused(ratio):
    with pytest.raises(DriverError, match="2:3, 3:2, 1:1, 9:16, 16:9"):
        video_options(VideoRequest(prompt="x", aspect_ratio=ratio))


def test_with_a_photo_the_aspect_is_ignored_and_the_user_is_told(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    opts, notes = video_options(VideoRequest(prompt="оживи", image_path=str(img), aspect_ratio="16:9"))
    assert opts.image_paths == (img,) and opts.aspect_ratio is None
    assert any("aspect_ratio проигнорирован" in n for n in notes)


def test_unsupported_aspect_is_refused_even_with_a_photo(tmp_path):
    img = tmp_path / "a.png"
    img.write_bytes(PNG)
    with pytest.raises(DriverError, match="aspect_ratio"):
        video_options(VideoRequest(prompt="x", image_path=str(img), aspect_ratio="4:3"))


def test_non_default_model_is_noted_not_silently_dropped():
    _, notes = video_options(VideoRequest(prompt="x", model="grok-imagine-video-1.5"))
    assert any("model" in n and "не применяется" in n for n in notes)


def test_image_request_keeps_aspect_and_notes_ignored_fields():
    opts, notes = image_options(ImageRequest(prompt="кот", aspect_ratio="1:1", resolution="2k",
                                             model="grok-imagine-image-quality"))
    assert opts.prompt == "кот" and opts.aspect_ratio == "1:1"
    assert any("resolution" in n for n in notes) and any("model" in n for n in notes)


def test_image_aspect_outside_the_ui_choices_is_refused():
    with pytest.raises(DriverError, match="aspect_ratio"):
        image_options(ImageRequest(prompt="x", aspect_ratio="19.5:9"))


# ---------- несколько картинок: первый кадр или референсы ----------

def _imgs(tmp_path, n):
    paths = []
    for i in range(n):
        p = tmp_path / f"r{i}.png"
        p.write_bytes(PNG)
        paths.append(str(p))
    return paths


def test_image_path_is_just_a_list_of_one(tmp_path):
    (one,) = _imgs(tmp_path, 1)
    opts, _ = video_options(VideoRequest(prompt="x", image_path=one))
    assert [str(p) for p in opts.image_paths] == [one]


def test_several_images_keep_the_aspect_because_grok_asks_for_it_in_reference_mode(tmp_path):
    """Проверено живьём: с одной картинкой формат берётся из неё, а с двумя и больше кнопка формата возвращается."""
    opts, notes = video_options(VideoRequest(prompt="x", image_paths=_imgs(tmp_path, 3), aspect_ratio="9:16"))
    assert len(opts.image_paths) == 3 and opts.aspect_ratio == "9:16"
    assert not any("aspect_ratio" in n for n in notes)


def test_1080p_with_several_images_is_refused_before_touching_the_page(tmp_path):
    """Проверено живьём: с двумя и больше картинками Grok предлагает только 480p и 720p."""
    with pytest.raises(DriverError, match="1080p недоступно"):
        video_options(VideoRequest(prompt="x", image_paths=_imgs(tmp_path, 2), resolution="1080p"))
    video_options(VideoRequest(prompt="x", image_paths=_imgs(tmp_path, 2), resolution="720p"))
    video_options(VideoRequest(prompt="x", image_paths=_imgs(tmp_path, 1), resolution="1080p"))     # с одной можно


def test_a_mention_must_point_at_an_attached_image(tmp_path):
    imgs = _imgs(tmp_path, 2)
    video_options(VideoRequest(prompt="@Image 1 и @Image 2 танцуют", image_paths=imgs))
    with pytest.raises(DriverError, match="@Image 3"):
        video_options(VideoRequest(prompt="@Image 3 танцует", image_paths=imgs))
    with pytest.raises(DriverError, match="@Image 1"):
        video_options(VideoRequest(prompt="@Image 1 танцует"))                  # картинок нет вообще
    with pytest.raises(DriverError, match="@Image 0"):
        video_options(VideoRequest(prompt="@Image 0 танцует", image_paths=imgs))
