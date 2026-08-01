"""Let the month picker's arrow close the list it opened.

Streamlit's select is searchable, so tapping its control while the list is
open keeps the field focused for typing instead of closing. On a phone that
leaves the arrow looking like a toggle that only works one way: it opens the
months and then does nothing.

This adds the missing half. A tap on the arrow while the list is open is
turned into the same Escape the widget already closes on, so the list shuts
without changing the selection. Tapping the text is left alone, so typing to
filter still works.

Everything here is defensive: if the widget's markup ever changes, no arrow
is matched and the picker simply behaves as it does today.
"""

from __future__ import annotations

from typing import Any


__all__ = ["render_select_arrow_toggle"]


_SCRIPT = """
<script>
(function () {
  var doc;
  try {
    doc = window.parent && window.parent.document ? window.parent.document : document;
  } catch (error) {
    return;  // A cross-origin parent means there is nothing to fix here.
  }
  if (!doc || doc.__getreceiptSelectArrowToggle) return;
  doc.__getreceiptSelectArrowToggle = true;

  var arrowUnder = function (target) {
    if (!target || !target.closest) return null;
    var select = target.closest('[data-baseweb="select"]');
    if (!select) return null;
    var svg = select.querySelector('svg');
    var arrow = svg ? svg.parentElement : null;
    if (!arrow || !arrow.contains(target)) return null;
    return { select: select, input: select.querySelector('input[role="combobox"]') };
  };

  // Closing on pointerdown is not enough on its own: the mouse events that
  // follow a pointer event are separate, so the widget would reopen the list
  // the instant it was closed. They are swallowed for a moment instead.
  var swallowUntil = 0;
  var swallow = function (event) {
    if (Date.now() > swallowUntil) return;
    if (!arrowUnder(event.target)) return;
    event.preventDefault();
    event.stopPropagation();
  };

  doc.addEventListener('pointerdown', function (event) {
    var hit = arrowUnder(event.target);
    if (!hit || !hit.input) return;
    if (hit.input.getAttribute('aria-expanded') !== 'true') return;

    event.preventDefault();
    event.stopPropagation();
    swallowUntil = Date.now() + 700;
    ['keydown', 'keyup'].forEach(function (type) {
      hit.input.dispatchEvent(new KeyboardEvent(type, {
        key: 'Escape', code: 'Escape', keyCode: 27, which: 27,
        bubbles: true, cancelable: true
      }));
    });
  }, true);

  ['mousedown', 'mouseup', 'click', 'touchstart', 'touchend'].forEach(function (type) {
    doc.addEventListener(type, swallow, true);
  });
})();
</script>
"""


def render_select_arrow_toggle(st: Any) -> None:
    """Install the behaviour once per page render."""

    try:
        import streamlit.components.v1 as components
    except Exception:
        return
    try:
        components.html(_SCRIPT, height=0, width=0)
    except Exception:
        # A picker that only opens is a small annoyance. It must never be a
        # reason the page fails to render.
        return
