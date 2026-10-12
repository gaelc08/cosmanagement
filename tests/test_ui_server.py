import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from cosmanagement.ui.server import Handler, build_allowed_hosts, host_allowed


def test_host_allowed():
    allowed = build_allowed_hosts(8765, ['cos.example.lu'])
    assert host_allowed('127.0.0.1:9999', allowed) and host_allowed('localhost', allowed) and host_allowed('[::1]:80', allowed)
    assert host_allowed('cos.example.lu', allowed) and host_allowed('cos.example.lu:8765', allowed)
    assert not host_allowed('evil.example.com', allowed) and not host_allowed('127.0.0.1.evil.com', allowed)


@pytest.fixture
def server():
    Handler.config = {}
    Handler.allowed_hosts = build_allowed_hosts(0)
    httpd = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f'http://127.0.0.1:{httpd.server_address[1]}'
    httpd.shutdown()
    httpd.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url) as res:
            return res.status, res.headers, res.read().decode()
    except urllib.error.HTTPError as err:
        return err.code, err.headers, err.read().decode()


def test_page_and_static_files(server):
    status, headers, body = get(server + '/')
    assert status == 200 and 'script-src \'self\'' in headers['Content-Security-Policy'] and '/static/js/core.js' in body
    assert get(server + '/static/style.css')[0] == 200
    assert get(server + '/static/js/main.js')[0] == 200


def test_static_refuses_other_types_and_traversal(server):
    assert get(server + '/static/index.html')[0] == 404
    assert get(server + '/static/..%2fserver.py')[0] == 404
    assert get(server + '/static/js/..%2f..%2fserver.py')[0] == 404


def test_search_validates_its_input(server):
    status, _, body = get(server + '/api/search?kind=bucket&q=a,b')
    assert status == 400 and 'Invalid name' in json.loads(body)['error']
    assert get(server + '/api/search?kind=nope&q=a')[0] == 400
    assert get(server + '/api/search?kind=tenant&q=**')[0] == 400
