"""Styling and CSP of the public unsubscribe pages (``/u/*``).

``/static/`` is admin-only, so these pages carry their own CSS in one inline ``<style>`` block,
allowed by its SHA-256 hash and nothing else. No scripts, images, fonts or connections; forms may
only post back to this site; no framing; no ``<base>``. ``app.security.RequestGuard`` sends
``UNSUBSCRIBE_CSP`` on every ``/u/`` response. Kept free of app imports so the guard can use it.
"""

import base64
import hashlib

PAGE_CSS = (
    "html{background:#f7f1e3;color:#2b2b2b;font:17px/1.5 Georgia,'Times New Roman',serif}"
    "body{margin:0;padding:3rem 1rem}"
    "main{max-width:32rem;margin:0 auto;background:#fffdf7;border:1px solid #e3d6b5;"
    "border-top:6px solid #9e1b1b;border-radius:8px;padding:1.5rem 1.75rem}"
    "h1{color:#9e1b1b;font-size:1.5rem;margin:0 0 .75rem}"
    "p{margin:0 0 1rem}"
    "button{font:inherit;font-weight:bold;color:#fff;background:#1f5130;border:0;"
    "border-radius:6px;padding:.6rem 1.4rem;cursor:pointer}"
    "button:hover,button:focus{background:#163d24}"
    "button:focus{outline:3px solid #c9a227;outline-offset:2px}"
    ".small{color:#666;font-size:.9rem;margin:0}"
    "label{display:block;font-weight:bold;margin:0 0 .3rem}"
    "input{font:inherit;box-sizing:border-box;width:100%;padding:.5rem .6rem;"
    "border:1px solid #8a7d5c;border-radius:6px;background:#fff;color:#2b2b2b}"
    "input:focus{outline:3px solid #c9a227;outline-offset:1px}"
    ".error{background:#fbe9e7;border-left:4px solid #9e1b1b;padding:.6rem .8rem;border-radius:4px}"
)
PAGE_CSS_HASH = "sha256-" + base64.b64encode(hashlib.sha256(PAGE_CSS.encode()).digest()).decode()
UNSUBSCRIBE_CSP = (
    f"default-src 'none'; style-src '{PAGE_CSS_HASH}'; img-src 'none'; form-action 'self'; "
    "frame-ancestors 'none'; base-uri 'none'"
)
