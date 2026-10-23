"""Exercise real OAuth and MCP routes using fabricated bank data."""
import base64
import asyncio
import datetime as dt
import hashlib
import json
from pathlib import Path
import secrets
import tempfile
from unittest.mock import Mock, patch
from urllib.parse import urlsplit, parse_qs

from starlette.testclient import TestClient
from bridge import create_bridge
from wise_tools import WiseData
from access_check import authorization


def check():
    base = "https://bridge.example"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root/"bridge-password").write_text("test-owner-password")
        (root/"config.env").write_text("ENABLE_BANKING_APP_ID=11111111-1111-4111-8111-111111111111\n")
        future = (dt.datetime.now(dt.timezone.utc)+dt.timedelta(hours=1)).isoformat()
        (root/"session.json").write_text(json.dumps({"access":{"valid_until":future},"accounts":[{"uid":"11111111-1111-4111-8111-111111111111","details":"Test main","currency":"EUR"}]}))
        mcp, provider = create_bridge(base,root)
        with TestClient(mcp.streamable_http_app(), base_url=base) as http:
            rpc_headers={"Accept":"application/json, text/event-stream"}
            def rpc(method, params=None, token=None):
                headers=dict(rpc_headers)
                if token:
                    headers["Authorization"]="Bearer "+token
                return http.post('/mcp',json={"jsonrpc":"2.0","id":1,"method":method,"params":params or {}},headers=headers)
            assert rpc('tools/list').status_code==401
            assert rpc('tools/list',token='wrong').status_code==401
            assert http.get('/private.key').status_code==404
            metadata=http.get('/.well-known/oauth-authorization-server').json()
            assert metadata['issuer']==base+'/' or metadata['issuer']==base
            bad=http.post('/register',json={"redirect_uris":["https://attacker.example/callback"]})
            assert bad.status_code==400
            callback='https://chatgpt.com/connector_platform/oauth_redirect'
            registration=http.post('/register',json={"redirect_uris":[callback],"client_name":"ChatGPT","token_endpoint_auth_method":"none","grant_types":["authorization_code","refresh_token"],"response_types":["code"],"scope":"wise:read"})
            assert registration.status_code==201, registration.text
            client=registration.json()['client_id']
            _, restored = create_bridge(base,root)
            assert asyncio.run(restored.get_client(client)) is not None, 'Server restart lost the registered client.'
            assert restored.clients[client] == provider.clients[client]
            assert (root/'bridge-clients.json').stat().st_mode & 0o777 == 0o600
            verifier=secrets.token_urlsafe(48)
            challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
            auth=http.get('/authorize',params={"client_id":client,"redirect_uri":callback,"response_type":"code","scope":"wise:read","state":"test-state","code_challenge":challenge,"code_challenge_method":"S256","resource":base+'/mcp'},follow_redirects=False)
            assert auth.status_code in (302,303,307),auth.text
            login=auth.headers['location']
            login_page=http.get(login)
            assert login_page.status_code==200
            assert login_page.headers['referrer-policy']=='same-origin', 'Approval privacy header hides the Origin on browser form POSTs.'
            policy=login_page.headers['content-security-policy']
            form_destinations=next(item.strip().split()[1:] for item in policy.split(';') if item.strip().startswith('form-action '))
            assert set(form_destinations)=={"'self'",'https://chatgpt.com','https://chat.openai.com'}, 'The browser blocks the OAuth callback after form submission.'
            ticket=parse_qs(urlsplit(login).query)['ticket'][0]
            csrf=provider.pending[ticket]['csrf']
            form={"ticket":ticket,"csrf":csrf,"password":"test-owner-password"}
            assert http.post('/owner-approval',data=form,headers={"Origin":"https://attacker.example"}).status_code==403
            assert http.post('/owner-approval',data={**form,"password":"wrong"},headers={"Origin":base}).status_code==403
            assert http.post('/owner-approval',data={**form,"csrf":"wrong"},headers={"Origin":base}).status_code==403
            cookie = http.cookies.get('wise_consent')
            http.cookies.clear()
            assert http.post('/owner-approval',data=form,headers={"Origin":base}).status_code==403
            http.get(login)
            approved=http.post('/owner-approval',data=form,headers={"Origin":base},follow_redirects=False)
            assert approved.status_code==303,approved.text
            returned=parse_qs(urlsplit(approved.headers['location']).query)
            assert returned['state']==['test-state']
            repeated=http.post('/owner-approval',data=form,headers={"Origin":base},follow_redirects=False)
            assert repeated.status_code==409 and 'already submitted' in repeated.text, 'A repeated approval is incorrectly reported as expired.'
            assert http.get(login).status_code==409
            unknown=http.post('/owner-approval',data={**form,"ticket":"unknown"},headers={"Origin":base})
            assert unknown.status_code==400 and 'no longer available' in unknown.text
            another=http.get('/authorize',params={"client_id":client,"redirect_uri":callback,"response_type":"code","scope":"wise:read","state":"test-expiry","code_challenge":challenge,"code_challenge_method":"S256","resource":base+'/mcp'},follow_redirects=False)
            expired_ticket=parse_qs(urlsplit(another.headers['location']).query)['ticket'][0]
            provider.pending[expired_ticket]['expires']=0
            expired=http.post('/owner-approval',data={**form,"ticket":expired_ticket},headers={"Origin":base})
            assert expired.status_code==400 and 'timed out after 10 minutes' in expired.text
            exchange={"grant_type":"authorization_code","client_id":client,"code":returned['code'][0],"redirect_uri":callback,"code_verifier":verifier,"resource":base+'/mcp'}
            assert http.post('/token',data={**exchange,"code_verifier":"bad"}).status_code==400
            token_response=http.post('/token',data=exchange)
            assert token_response.status_code==200,token_response.text
            token=token_response.json()['access_token']
            refresh_token=token_response.json()['refresh_token']
            refresh_days=(provider.refreshes[refresh_token].expires_at-dt.datetime.now(dt.timezone.utc).timestamp())/86400
            assert 179 < refresh_days <= 180, 'ChatGPT refresh access unnecessarily expires after one day.'
            _, restarted_auth=create_bridge(base,root)
            assert asyncio.run(restarted_auth.load_access_token(token)) is not None, 'Server restart lost ChatGPT access.'
            assert asyncio.run(restarted_auth.load_refresh_token(restarted_auth.clients[client],refresh_token)) is not None
            with patch('bridge_auth.time.time', return_value=dt.datetime.now(dt.timezone.utc).timestamp()+2*86400):
                assert asyncio.run(restarted_auth.load_refresh_token(restarted_auth.clients[client],refresh_token)) is not None, 'Refresh access fails after the first day.'
            assert (root/'bridge-tokens.json').stat().st_mode & 0o777 == 0o600
            assert http.post('/token',data=exchange).status_code==400
            response=rpc('initialize',{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"1"}},token)
            assert response.status_code==200,response.text
            tools=rpc('tools/list',token=token).json()['result']['tools']
            assert {t['name'] for t in tools}=={'list_accounts','get_balances','get_transactions'}
            assert all(t['annotations']['readOnlyHint'] for t in tools)
            result=rpc('tools/call',{"name":"list_accounts","arguments":{}},token).json()
            assert 'Test main' in json.dumps(result)
            provider.tokens[token].resource='https://other.example/mcp'
            assert rpc('tools/list',token=token).status_code==401
            refresh=token_response.json()['refresh_token']
            original_refresh_expiry=provider.refreshes[refresh].expires_at
            refresh_body={"grant_type":"refresh_token","client_id":client,"refresh_token":refresh,"resource":base+'/mcp'}
            rotated=http.post('/token',data=refresh_body)
            assert rotated.status_code==200,rotated.text
            assert rotated.json()['refresh_token']!=refresh
            assert provider.refreshes[rotated.json()['refresh_token']].expires_at==original_refresh_expiry
            assert http.post('/token',data=refresh_body).status_code==400
            assert token not in provider.tokens
            _, rotated_auth=create_bridge(base,root)
            assert token not in rotated_auth.tokens and refresh not in rotated_auth.refreshes, 'Restart restored revoked credentials.'
        bank=WiseData(root)
        def check_consent_request(method, path, body=None):
            if path.startswith('/aspsps?'):
                return {"aspsps":[{"name":"Wise","country":"BE","psu_types":["personal"],"maximum_consent_validity":180*86400}]}
            assert method=='POST' and path=='/auth'
            duration=(dt.datetime.fromisoformat(body['access']['valid_until'])-dt.datetime.now(dt.timezone.utc)).total_seconds()
            assert 179*86400 < duration <= 180*86400, 'Setup unnecessarily limits Wise consent to less than the bank allows.'
            assert body['access']['balances'] is True and body['access']['transactions'] is True
            raise RuntimeError('consent-window-checked')
        consent_client=Mock()
        consent_client.request.side_effect=check_consent_request
        with patch('access_check.validate_app'), patch('access_check.http.server.HTTPServer'), patch('access_check.server_context'):
            try:
                authorization(consent_client,root)
            except RuntimeError as error:
                assert str(error)=='consent-window-checked'
            else:
                raise AssertionError('Consent request was not checked')
        for method,path in [('POST','/payments'),('DELETE','/sessions/x'),('GET','https://other.example/accounts/x/balances'),('GET','/accounts/1111/balances?unexpected=1')]:
            try:
                bank.client.request(method,path)
            except RuntimeError:
                pass
            else:
                raise AssertionError('Non-read request accepted')
        calls=[]
        def fake(method,path,body=None):
            calls.append(path)
            return {"transactions":[{"transaction_amount":{"amount":"12.34","currency":"EUR"},"credit_debit_indicator":"DBIT","creditor":{"name":"Test shop","postal_address":{"secret":"hidden"}},"creditor_account":{"iban":"hidden"}}],"continuation_key":"page+two/="}
        with patch.object(bank.client,'request',fake):
            data=bank.transactions('account_1','2026-10-01','2026-10-20')
            assert data['complete_for_requested_range'] is False
            assert data['transactions'][0]['creditor_name']=='Test shop'
            assert 'hidden' not in json.dumps(data)
            bank.transactions('account_1','2026-10-01','2026-10-20',data['next_cursor'])
            assert 'continuation_key=page%2Btwo%2F%3D' in calls[-1]
            try:
                bank.transactions('account_999','2026-10-01','2026-10-20')
            except ValueError:
                pass
            else:
                raise AssertionError('Unknown account allowed')
        print('PASS: OAuth blocks unapproved clients, missing tokens, wrong password, wrong origin, wrong PKCE, code replay, and wrong resource.')
        print('PASS: authenticated MCP exposes exactly three read-only tools and returns account data.')
        print('PASS: registered client metadata survives server recreation in a private file.')
        print('PASS: new Wise approval requests the bank maximum, without a one-day cap.')
        print('PASS: ChatGPT can refresh for 180 days; private tokens and revocations survive server recreation.')
        print('PASS: transaction pagination preserves cursors and removes account numbers and addresses.')


def check_registration_recovery():
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / 'bridge-password').write_text('test-owner-password')
        (root / 'config.env').write_text('ENABLE_BANKING_APP_ID=11111111-1111-4111-8111-111111111111\n')
        mcp, _ = create_bridge('https://bridge.example', root)
        with TestClient(mcp.streamable_http_app(), base_url='https://bridge.example') as http:
            for _ in range(35):
                response = http.post('/register', json={'redirect_uris':['https://chatgpt.com/connector_platform/oauth_redirect']})
                assert response.status_code == 201, 'Unapproved registrations permanently blocked a new connection.'
        _, provider = create_bridge('https://bridge.example', root)
        assert len(provider.clients) <= 30
        last = response.json()['client_id']
        with patch('bridge_auth.time.time', return_value=dt.datetime.now(dt.timezone.utc).timestamp() + 601):
            assert asyncio.run(provider.get_client(last)) is None, 'Unapproved registration never expired.'
        print('PASS: unapproved registrations expire, and a registration flood cannot permanently lock out the owner.')


if __name__ == '__main__':
    check()
    check_registration_recovery()
