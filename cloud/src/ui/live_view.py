"""Show the live acquisition browser so the owner can clear a gate by hand.

Some providers guard their own sign-in with a control no program may answer
for the owner - Epos uses a slide puzzle. Rather than ending the acquisition
there, the page is mirrored into the app: the owner works the control from
their phone, and the acquisition carries on from the very same tab.

Only the provider's own https page is ever mirrored or driven.
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from urllib.parse import urlsplit


__all__ = ["LiveViewUnavailableError", "assert_official_page", "render_live_view"]

# The widest the mirrored page is drawn; a phone shrinks it further.
_MAX_DISPLAY_WIDTH = 720


class LiveViewUnavailableError(RuntimeError):
    """Raised when the live page cannot be mirrored or safely driven."""


def assert_official_page(url: str, allowed_hosts: tuple[str, ...]) -> None:
    """Reject anything but the provider's own https page.

    The owner is about to drive this page with real input, so an off-site or
    downgraded page must never be reachable through the viewer.
    """

    parsed = urlsplit(str(url or ""))
    hostname = (parsed.hostname or "").lower()
    allowed = {str(host).strip().lower() for host in allowed_hosts if str(host).strip()}
    if (
        parsed.scheme.lower() != "https"
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or hostname not in allowed
    ):
        raise LiveViewUnavailableError("公式サイト以外の画面は操作できません。")


def render_live_view(
    st: Any,
    browser: Any,
    *,
    key: str,
    allowed_hosts: tuple[str, ...],
) -> None:
    """Mirror the live page and forward the owner's taps back to it.

    A slide puzzle needs a drag, which one tap cannot express, so the owner
    marks the piece and then its destination and the two points are replayed
    as a single continuous movement.
    """

    try:
        from PIL import Image
        from streamlit_image_coordinates import streamlit_image_coordinates
    except Exception:
        st.error(
            "画面表示に必要なライブラリが読み込めませんでした。"
            "requirements.txtの再インストールが必要です。",
            icon=":material/error:",
        )
        return

    try:
        target = browser.current_page_target()
        assert_official_page(str(target.get("url") or ""), allowed_hosts)
        frame = browser.screenshot_current_page()
        image = Image.open(BytesIO(frame))
        image.load()
    except LiveViewUnavailableError as error:
        st.error(str(error), icon=":material/gpp_bad:")
        return
    except Exception as error:
        st.error(
            f"取得ブラウザの画面を表示できません。[{type(error).__name__}]",
            icon=":material/error:",
        )
        return

    natural_width, natural_height = image.size
    display_width = min(_MAX_DISPLAY_WIDTH, natural_width)
    pending_key = f"{key}__drag_start"
    processed_key = f"{key}__last_tap"
    pending = st.session_state.get(pending_key)

    if pending:
        st.info(
            "ピースの移動先をタップしてください。もう一度最初からやり直す場合は"
            "「選択をやり直す」を押してください。",
            icon=":material/drag_pan:",
        )
    else:
        st.caption(
            "動かしたいピースをタップし、次に移動先をタップすると、"
            "同じChromeへドラッグ操作を送ります。ボタンを押すだけの場合は"
            "そのボタンを2回タップしてください。"
        )

    tapped = streamlit_image_coordinates(
        image,
        width=display_width,
        key=f"{key}__image",
        cursor="crosshair",
    )

    if isinstance(tapped, dict):
        token = str(tapped.get("unix_time") or "")
        if token and st.session_state.get(processed_key) != token:
            st.session_state[processed_key] = token
            point = _page_point(
                tapped,
                display_width=display_width,
                natural_width=natural_width,
                natural_height=natural_height,
            )
            if pending:
                st.session_state.pop(pending_key, None)
                try:
                    browser.drag_current_page(
                        int(pending[0]), int(pending[1]), point[0], point[1]
                    )
                except Exception as error:
                    st.error(
                        f"操作を送信できませんでした。[{type(error).__name__}]",
                        icon=":material/error:",
                    )
            else:
                st.session_state[pending_key] = point
            st.rerun()

    columns = st.columns(2)
    if columns[0].button(
        "画面を更新",
        key=f"{key}__refresh",
        use_container_width=True,
        icon=":material/refresh:",
    ):
        st.rerun()
    if columns[1].button(
        "選択をやり直す",
        key=f"{key}__reset",
        use_container_width=True,
        disabled=not pending,
        icon=":material/undo:",
    ):
        st.session_state.pop(pending_key, None)
        st.rerun()


def _page_point(
    tapped: dict[str, Any],
    *,
    display_width: int,
    natural_width: int,
    natural_height: int,
) -> tuple[int, int]:
    """Map a tap on the drawn image back to a point on the real page."""

    rendered_width = max(1, int(tapped.get("width") or display_width))
    rendered_height = max(
        1,
        int(
            tapped.get("height")
            or round(display_width * natural_height / max(1, natural_width))
        ),
    )
    x = round(int(tapped.get("x") or 0) * natural_width / rendered_width)
    y = round(int(tapped.get("y") or 0) * natural_height / rendered_height)
    return (
        max(0, min(x, natural_width - 1)),
        max(0, min(y, natural_height - 1)),
    )
