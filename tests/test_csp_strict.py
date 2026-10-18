"""The Content-Security-Policy forbids inline scripts, and no template may need them."""
import glob
import os
import re

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
JINJA_COMMENT = re.compile(r'\{#.*?#\}', re.S)
INLINE_SCRIPT = re.compile(r'<script(?![^>]*\bsrc=)(?![^>]*type="application/json")[^>]*>', re.I)
HANDLER_ATTR = re.compile(r'\s(on[a-z]+)\s*=\s*["\']', re.I)
JS_URL = re.compile(r'(href|action|src)\s*=\s*["\']\s*javascript:', re.I)


def test_csp_has_no_unsafe_inline_scripts(fresh_client):
    csp = fresh_client.get('/login').headers['Content-Security-Policy']
    script_src = next(d for d in csp.split(';') if d.strip().startswith('script-src'))
    assert "'unsafe-inline'" not in script_src and "'unsafe-eval'" not in script_src
    assert "object-src 'none'" in csp


def test_no_template_contains_inline_javascript():
    offenders = []
    for path in glob.glob(os.path.join(ROOT, 'templates', '**', '*.html'), recursive=True):
        with open(path, encoding='utf-8') as f:
            src = JINJA_COMMENT.sub('', f.read())
        rel = os.path.relpath(path, ROOT)
        for pattern, label in ((INLINE_SCRIPT, 'inline <script>'), (HANDLER_ATTR, 'on* handler'),
                               (JS_URL, 'javascript: URL')):
            for m in pattern.finditer(src):
                offenders.append(f"{rel}:{src.count(chr(10), 0, m.start()) + 1} {label}")
    assert offenders == [], '\n'.join(offenders)
