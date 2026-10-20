"""OAuth protocol supplied by MCP SDK; owner consent is granted only in local CLI."""
import hashlib
import secrets
import time
from urllib.parse import urlencode
from mcp.server.auth.provider import AuthorizationCode, AccessToken, RefreshToken, AuthorizeError, TokenError, RegistrationError
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

class LocalOAuth:
    def __init__(self,store,issuer,redirect_uris):
        self.store,self.issuer,self.redirect_uris=store,issuer.rstrip('/')+'/',redirect_uris
        self.resource=self.issuer.rstrip('/')+'/mcp'
    def key(self,token): return hashlib.sha256(token.encode()).hexdigest()
    async def get_client(self,client_id):
        data=self.store.get('oauth_client',client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None
    async def register_client(self,client_info):
        if len(self.store.all('oauth_client'))>=32: raise RegistrationError('invalid_client_metadata','Registration limit reached')
        if not client_info.redirect_uris or any(str(uri) not in self.redirect_uris for uri in client_info.redirect_uris):
            raise RegistrationError('invalid_redirect_uri','Copy exact ChatGPT callback into local allowlist')
        self.store.put('oauth_client',client_info.client_id,client_info.model_dump(mode='json'))
    async def authorize(self,client,params):
        if params.resource!=self.resource: raise AuthorizeError('invalid_request','Wrong resource')
        if params.scopes and set(params.scopes)!={'bridge'}: raise AuthorizeError('invalid_scope')
        if len([r for r in self.store.all('oauth_consent') if r['expires_at']>time.time()])>=16:
            raise AuthorizeError('temporarily_unavailable','Consent request limit')
        id=secrets.token_hex(16)
        self.store.put('oauth_consent',id,{'id':id,'client_id':client.client_id,
            'params':params.model_dump(mode='json'),'approved':False,'expires_at':time.time()+600})
        self.store.audit('oauth_consent_requested',{'id':id,'client':client.client_id})
        return self.issuer.rstrip('/')+'/consent?'+urlencode({'request':id})
    def approve(self,id):
        r=self.store.get('oauth_consent',id)
        if not r or r['expires_at']<time.time(): raise ValueError('Consent expired/unknown')
        self.store.update('oauth_consent',id,approved=True)
        self.store.audit('oauth_consent_approved_locally',{'id':id})
    def finish(self,id):
        r=self.store.get('oauth_consent',id)
        if not r or r['expires_at']<time.time(): raise ValueError('Expired consent')
        if not r['approved']: return None
        r=self.store.consume('oauth_consent',id)
        if not r: raise ValueError('Consent already used')
        p=r['params']; code=secrets.token_urlsafe(32)
        data={'code':'','client_id':r['client_id'],'scopes':['bridge'], 'expires_at':time.time()+90,
              'code_challenge':p['code_challenge'],'redirect_uri':p['redirect_uri'],
              'redirect_uri_provided_explicitly':p['redirect_uri_provided_explicitly'],'resource':p['resource']}
        self.store.put('oauth_code',self.key(code),data)
        query={'code':code,'iss':self.issuer}
        if p.get('state'): query['state']=p['state']
        return p['redirect_uri']+('&' if '?' in p['redirect_uri'] else '?')+urlencode(query)
    async def load_authorization_code(self,client,authorization_code):
        data=self.store.get('oauth_code',self.key(authorization_code))
        if not data or data['client_id']!=client.client_id or data['expires_at']<time.time(): return None
        return AuthorizationCode.model_validate({**data,'code':authorization_code})
    def issue(self,client,scopes,resource):
        access=secrets.token_urlsafe(32); refresh=secrets.token_urlsafe(32)
        family=secrets.token_hex(16)
        common={'client_id':client.client_id,'scopes':scopes,'resource':resource,'family':family}
        self.store.put('oauth_access',self.key(access),{**common,'expires_at':int(time.time())+3600})
        self.store.put('oauth_refresh',self.key(refresh),{**common,'expires_at':int(time.time())+86400*7})
        return OAuthToken(access_token=access,token_type='Bearer',expires_in=3600,refresh_token=refresh,scope=' '.join(scopes))
    async def exchange_authorization_code(self,client,authorization_code):
        data=self.store.consume('oauth_code',self.key(authorization_code.code))
        if not data or data['client_id']!=client.client_id or data['expires_at']<time.time(): raise TokenError('invalid_grant')
        return self.issue(client,data['scopes'],data['resource'])
    async def load_refresh_token(self,client,refresh_token):
        data=self.store.get('oauth_refresh',self.key(refresh_token))
        if not data or data['client_id']!=client.client_id or data['expires_at']<time.time(): return None
        return RefreshToken.model_validate({**data,'token':refresh_token})
    async def exchange_refresh_token(self,client,refresh_token,scopes):
        data=self.store.consume('oauth_refresh',self.key(refresh_token.token))
        if not data or data['client_id']!=client.client_id or data['expires_at']<time.time(): raise TokenError('invalid_grant')
        if not set(scopes).issubset(data['scopes']): raise TokenError('invalid_scope')
        self.revoke_family(data['family'])
        return self.issue(client,scopes,data['resource'])
    async def load_access_token(self,token):
        data=self.store.get('oauth_access',self.key(token))
        if not data or data['expires_at']<time.time() or data['resource']!=self.resource: return None
        return AccessToken.model_validate({**data,'token':token})
    def revoke_family(self,family):
        for kind in ('oauth_access','oauth_refresh'):
            with self.store.connect() as c:
                rows=c.execute('SELECT id,data FROM records WHERE kind=?',(kind,)).fetchall()
                import json
                for id,data in rows:
                    if json.loads(data).get('family')==family: c.execute('DELETE FROM records WHERE kind=? AND id=?',(kind,id))
    async def revoke_token(self,token):
        for kind in ('oauth_access','oauth_refresh'):
            data=self.store.get(kind,self.key(token.token))
            if data: self.revoke_family(data['family'])
