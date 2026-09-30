"""Public auth-callback route.

Supabase's magic-link redirect appends the JWT to the URL as a hash fragment
(`#access_token=...`), which browsers do not send to the server. Streamlit's
`st.components.v1.html` renders in a sandboxed iframe with no `allow-same-
origin`, so JS there cannot read the parent frame's fragment either.

This route serves a plain HTML page (unsandboxed, same-origin as itself) whose
JS reads its own fragment, extracts `access_token`/`refresh_token`, and
redirects the top-level browser to the Streamlit app with those tokens as
query params — which Streamlit *can* read via `st.query_params`.

Public: no auth required. The tokens never touch our server (they stay in
the browser's URL bar), so this is a pure client-side handoff.
"""
from __future__ import annotations

import os

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter(tags=["auth"])

# Where to forward the tokens to. Must match the Streamlit app's public URL.
# Overridable via env so production deploys don't need a code change.
_STREAMLIT_URL = os.environ.get("STREAMLIT_URL", "http://localhost:8501/")


_CALLBACK_HTML = """<!doctype html>
<html>
<head>
  <meta charset="utf-8">
  <title>Signing you in…</title>
  <style>
    body {{ font-family: system-ui, sans-serif; text-align: center; padding: 4rem 1rem; }}
    .msg {{ color: #444; }}
    .err {{ color: #b00020; }}
  </style>
</head>
<body>
  <p class="msg">Signing you in…</p>
  <script>
    (function () {{
      const streamlit = {streamlit_url!r};
      const hash = window.location.hash || "";
      if (!hash || hash.indexOf("access_token") === -1) {{
        document.body.innerHTML =
          '<p class="err">No session token in URL. Please click the magic link again.</p>';
        return;
      }}
      const p = new URLSearchParams(hash.substring(1));
      const at = p.get("access_token");
      const rt = p.get("refresh_token") || "";
      if (!at) {{
        document.body.innerHTML =
          '<p class="err">Session token missing. Please retry sign-in.</p>';
        return;
      }}
      const q = new URLSearchParams({{ at: at, rt: rt }}).toString();
      const dest = streamlit + (streamlit.indexOf("?") === -1 ? "?" : "&") + q;
      window.location.replace(dest);
    }})();
  </script>
</body>
</html>
"""


@router.get("/auth/callback", response_class=HTMLResponse)
def auth_callback() -> HTMLResponse:
    return HTMLResponse(content=_CALLBACK_HTML.format(streamlit_url=_STREAMLIT_URL))
