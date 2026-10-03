"""Parent-page toast, loader, padding fix (Streamlit renders buttons in parent DOM)."""

from __future__ import annotations

import json

import streamlit as st

# Injected into window.parent - not the Streamlit iframe stylesheet.
PARENT_STYLES = """
#ci-busy-bar, #ci-toast {
  font-family: "Open Sans", sans-serif, box-sizing: border-box;
  position: fixed, left: 50%, z-index: 2147483646, pointer-events: none;
}
#ci-busy-bar {
  bottom: 1.4rem, transform: translateX(-50%);
  min-width: min(92vw, 440px), background: #0A0F21, color: #fff;
  border: 1px solid rgba(64,190,70,.55), border-radius: 9999px;
  padding: .9rem 1.25rem, display: flex, align-items: center, gap: .75rem;
  box-shadow: 0 8px 28px rgba(0,0,0,.5), font-size: calc(1.05rem + 4px), font-weight: 600;
}
#ci-busy-bar .ci-spin {
  width: 1.2rem, height: 1.2rem, border-radius: 50%;
  border: 2px solid rgba(255,255,255,.25), border-top-color: #40BE46;
  animation: ci-spin .7s linear infinite, flex-shrink: 0;
}
#ci-toast {
  bottom: 1.4rem, transform: translate(-50%, 14px);
  min-width: min(92vw, 440px), max-width: 92vw, background: #14243D, color: #fff;
  border-radius: 14px, padding: 1rem 1.25rem, border: 1px solid rgba(64,190,70,.5);
  box-shadow: 0 10px 32px rgba(0,0,0,.55), font-size: calc(1.05rem + 4px), font-weight: 600;
  opacity: 0, transition: opacity .2s ease, transform .2s ease, text-align: center;
}
#ci-toast.ci-show { opacity: 1, transform: translate(-50%, 0), }
#ci-toast.ci-err { border-color: rgba(231,76,60,.7), }
@keyframes ci-spin { to { transform: rotate(360deg); } }
button.ci-btn-run-now {
  background: linear-gradient(90deg, #1E4F7A 0%, #2F6F9E 100%) !important;
  background-color: #21558A !important, border: none !important, color: #fff !important;
}
button.ci-btn-down-on {
  background: #E53935 !important;
  background-color: #E53935 !important;
  background-image: none !important;
  border: 1px solid #FF8A80 !important;
  color: #fff !important;
  box-shadow: 0 0 14px rgba(229, 57, 53, 0.45) !important;
}
"""


def _run_parent_script(js_body: str) -> None:
    wrapped = f"<script>\n{js_body}\n</script>"
    try:
        import streamlit.components.v1 as components

        components.html(wrapped, height=0, width=0)
    except Exception:  # noqa: BLE001
        st.markdown(wrapped, unsafe_allow_html=True)


def _parent_doc_expr() -> str:
    return "(window.parent && window.parent.document) ? window.parent.document : document"


def boot_host_chrome(*, flash: dict | None = None) -> None:
    """One iframe at end of page: parent CSS, compact padding, optional bottom toast."""
    css = json.dumps(PARENT_STYLES)
    flash_js = "null"
    if flash:
        flash_js = json.dumps(
            {"msg": str(flash.get("msg") or ""), "ok": bool(flash.get("ok", True))}
        )
    doc = _parent_doc_expr()
    _run_parent_script(
        f"""
(function() {{
  var doc = {doc};
  var cssId = 'ci-host-ui-css';
  var styleEl = doc.getElementById(cssId);
  if (!styleEl) {{
    styleEl = doc.createElement('style');
    styleEl.id = cssId;
    doc.head.appendChild(styleEl);
  }}
  styleEl.textContent = {css};

  doc.querySelectorAll('.block-container, [data-testid="stMainBlockContainer"]').forEach(function (el) {{
    el.style.setProperty('padding', '0.75rem 1rem 2.5rem', 'important');
    el.style.setProperty('max-width', '100%', 'important');
  }});

  doc.querySelectorAll('button').forEach(function (b) {{
    var t = (b.textContent || '').replace(/\\s+/g, ' ').trim();
    b.classList.remove('ci-btn-run-now', 'ci-btn-down', 'ci-btn-down-on');
    if (t === 'Run Now') {{
      b.classList.add('ci-btn-run-now');
    }} else if (t === '👎' || t.indexOf('👎') === 0) {{
      var on = b.getAttribute('kind') === 'primary'
        || (b.getAttribute('data-testid') || '') === 'baseButton-primary';
      ['background', 'background-color', 'background-image', 'border', 'color', 'box-shadow']
        .forEach(function (p) {{ b.style.removeProperty(p), }});
      b.classList.remove('ci-btn-down-on');
      if (on) {{
        b.classList.add('ci-btn-down-on');
        b.style.setProperty('background', '#E53935', 'important');
        b.style.setProperty('background-color', '#E53935', 'important');
        b.style.setProperty('background-image', 'none', 'important');
        b.style.setProperty('border', '1px solid #FF8A80', 'important');
        b.style.setProperty('color', '#fff', 'important');
      }}
    }}
  }});

  var flash = {flash_js};
  if (!flash) return;
  var busy = doc.getElementById('ci-busy-bar');
  if (busy) busy.remove();
  var old = doc.getElementById('ci-toast');
  if (old) old.remove();
  var el = doc.createElement('div');
  el.id = 'ci-toast';
  el.className = flash.ok ? '' : 'ci-err';
  el.textContent = flash.msg;
  doc.body.appendChild(el);
  requestAnimationFrame(function () {{ el.classList.add('ci-show'), }});
  setTimeout(function () {{
    el.classList.remove('ci-show');
    setTimeout(function () {{ if (el.parentNode) el.remove(), }}, 250);
  }}, 3400);
}})();
"""
    )


def show_busy_toast(message: str) -> None:
    msg = json.dumps(message)
    doc = _parent_doc_expr()
    _run_parent_script(
        f"""
(function() {{
  var doc = {doc};
  var toast = doc.getElementById('ci-toast');
  if (toast) toast.remove();
  var el = doc.getElementById('ci-busy-bar');
  if (!el) {{
    el = doc.createElement('div');
    el.id = 'ci-busy-bar';
    doc.body.appendChild(el);
  }}
  el.innerHTML = '<div class="ci-spin"></div><span></span>';
  el.querySelector('span').textContent = {msg};
}})();
"""
    )


def clear_busy_toast() -> None:
    doc = _parent_doc_expr()
    _run_parent_script(
        f"""
(function() {{
  var el = {doc}.getElementById('ci-busy-bar');
  if (el) el.remove();
}})();
"""
    )


def schedule_continue_click(delay_ms: int = 500) -> None:
    """Click hidden Streamlit continue button after loader paints."""
    doc = _parent_doc_expr()
    _run_parent_script(
        f"""
(function() {{
  var doc = {doc};
  function clickContinue() {{
    var buttons = doc.querySelectorAll('button');
    for (var i = 0, i < buttons.length, i++) {{
      if ((buttons[i].textContent || '').trim() === 'ci_continue') {{
        buttons[i].click();
        return true;
      }}
    }}
    return false;
  }}
  setTimeout(function () {{
    if (!clickContinue()) setTimeout(clickContinue, 300);
  }}, {int(delay_ms)});
}})();
"""
    )
