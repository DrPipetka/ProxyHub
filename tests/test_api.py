import asyncio

from fastapi.testclient import TestClient

from proxyhub.main import create_app
from conftest import FakeCore, UUID, seed


def client_for(tmp_path):
    return TestClient(create_app(tmp_path, core=FakeCore(), background=False), base_url='http://127.0.0.1')


def test_auth_import_and_full_config(tmp_path):
    with client_for(tmp_path) as c:
        assert c.get('/status').status_code == 401
        assert c.post('/auth/login', json={}).status_code == 200
        r = c.post('/proxies', json={"data": f"trojan://supersecret@example.com:443", "source": "test"})
        assert r.json()["accepted"] == 1
        listing = c.get('/proxies').json()
        assert listing['items'][0]['link'] == 'trojan://supersecret@example.com:443'
        assert 'masked_link' not in listing['items'][0]
        assert "supersecret" in c.get('/proxies/1/link').json()['link']
        assert c.get('/status').json()['states']['new'] == 1
        assert c.get('/proxies?page=0').status_code == 400
        token = c.get('/settings').json()['values']['api_token']
        c.cookies.clear()
        assert c.get('/status', headers={"Authorization": "Bearer " + token}).status_code == 200


def test_api_documentation_requires_auth_and_matches_routes(tmp_path):
    with client_for(tmp_path) as c:
        assert c.get('/openapi.json').status_code == 401
        assert c.get('/subscriptions').status_code == 401
        c.post('/auth/login', json={})
        schema = c.get('/openapi.json').json()
        assert schema['paths']['/proxies']['get']['security'] == [{'BearerAuth': []}]
        assert schema['paths']['/health']['get']['security'] == []
        assert '422' not in schema['paths']['/config']['get']['responses']
        assert schema['paths']['/proxies']['post']['requestBody']['content']['application/json']['example']['interval_minutes'] == 60
        assert '/subscriptions/{subscription_id}/refresh' in schema['paths']
        assert any(p['name'] == 'lease' for p in schema['paths']['/config']['get']['parameters'])
        assert 'text/plain' in schema['paths']['/config']['get']['responses']['200']['content']


def test_config_leasing_used_and_count(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        seed(c.app.state.service.db, 2)
        assert c.get('/config?lease=0').json()['id'] == 1
        assert c.get('/config?format=text').text.startswith('vless://')
        assert c.get('/count').json()['count'] == 0
        p = c.get('/proxy').json()
        assert p['port'] and p['id'] == 2
        assert c.get('/count?format=text').text == '1'
        assert c.post('/proxy/used', json={'addr': p['addr']}).json()['state'] == 'cooldown'
        assert c.get('/count').json()['count'] == 0
        assert c.get('/proxy').status_code == 404


def test_delete_confirmation_and_source(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        seed(c.app.state.service.db, 2, source='test')
        assert c.delete('/proxies').status_code == 400
        assert c.delete('/proxies?confirm=yes&scope=source&source=other').json()['deleted'] == 0
        assert c.delete('/proxies?confirm=yes&scope=source&source=test').json()['deleted'] == 2


def test_settings_validation_csrf_and_password(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        assert c.patch('/settings', json={'pool_size': 51}).status_code == 400
        assert c.patch('/settings', json={'web_host':'0.0.0.0'}).status_code == 400
        assert c.post('/actions', json={'ids':[1], 'action':'disable'}, headers={'Origin':'https://evil.example'}).status_code == 403
        assert c.patch('/settings', json={'ui_password': 'good-password'}).status_code == 200
        assert c.get('/status').status_code == 401
        assert c.post('/auth/login', json={'password':'wrong'}).status_code == 401
        assert c.post('/auth/login', json={'password':'good-password'}).status_code == 200
        assert c.get('/status').status_code == 200


def test_body_limit_and_security_headers(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        c.patch('/settings', json={'max_body_mb': 1})
        r = c.post('/proxies', content='a' * (1024 * 1024 + 1), headers={'Content-Type':'text/plain'})
        assert r.status_code == 413
        response = c.get('/')
        assert response.status_code == 200 and 'frame-ancestors' in response.headers['Content-Security-Policy']
        assert c.get('/', headers={'Host':'attacker.example'}).status_code == 403


def test_event_fragment_escapes_html(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        c.app.state.service.db.event('<script>alert(1)</script>')
        html = c.get('/ui/events-fragment').text
        assert '<script>' not in html and '&lt;script&gt;' in html


def test_unexpected_error_does_not_log_credentials(tmp_path):
    with client_for(tmp_path) as c:
        c.post('/auth/login', json={})
        async def broken(*args, **kwargs):
            raise RuntimeError('PASSWORD-MUST-NOT-APPEAR')
        c.app.state.service.pool.acquire = broken
        result = c.get('/proxy')
        assert result.status_code == 500 and result.json()['error'] == 'internal_error'
        assert 'PASSWORD-MUST-NOT-APPEAR' not in result.text
    assert 'PASSWORD-MUST-NOT-APPEAR' not in (tmp_path/'proxyhub.log').read_text(encoding='utf-8')
