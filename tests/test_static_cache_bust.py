"""Static URLs carry ?v=<mtime> so a week-long browser/CDN cache can never pair
a new page with an old script (3.0.1's stock buttons did nothing on Prod for
browsers holding the previous inventory-common.js)."""
import os
import re

from flask import url_for

from tests.conftest import login


def test_static_urls_carry_the_file_mtime(fresh_app):
    path = os.path.join(fresh_app.static_folder, 'js', 'pages', 'inventory-common.js')
    with fresh_app.test_request_context('/'):
        url = url_for('static', filename='js/pages/inventory-common.js')
    assert url == f'/static/js/pages/inventory-common.js?v={int(os.stat(path).st_mtime)}'


def test_explicit_v_and_missing_files_are_left_alone(fresh_app):
    with fresh_app.test_request_context('/'):
        assert url_for('static', filename='js/rscp-common.js', v='x').endswith('?v=x')
        assert url_for('static', filename='uploads/no-such-photo.jpg') == '/static/uploads/no-such-photo.jpg'


def test_pages_link_versioned_scripts_and_styles(fresh_client):
    login(fresh_client, 1)
    html = fresh_client.get('/inventory/').get_data(as_text=True)
    srcs = re.findall(r'<script src="(/static/[^"]+)"', html)
    assert srcs and all('?v=' in s for s in srcs), srcs
    assert any('inventory-common.js?v=' in s for s in srcs)
    hrefs = re.findall(r'<link[^>]+href="(/static/[^"]+\.css[^"]*)"', html)
    assert all('?v=' in h for h in hrefs), hrefs


def test_versioned_script_is_still_served(fresh_client):
    login(fresh_client, 1)
    resp = fresh_client.get('/static/js/pages/inventory-common.js?v=123')
    assert resp.status_code == 200 and b'stockStep' in resp.data
