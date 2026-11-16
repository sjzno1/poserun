"""Read-only Personal API surface; endpoints pending verification with real key.
Reference: indaco/garmin-personal-mcp src/api-client.ts, src/tools.ts.
Never follows redirects, sends key only to the fixed Garmin token endpoint.
"""
import json
import os
import re
import time
from urllib.request import Request, build_opener, HTTPRedirectHandler
from urllib.error import HTTPError

# China-region Personal API by default; override with GARMIN_API_BASE_URL
# (global host: https://personal.connectapi.garmin.com)
BASE = os.environ.get('GARMIN_API_BASE_URL', 'https://personal.connectapi.garmin.cn').rstrip('/')
TOKEN_PATH = '/personalapitoken-service/personal-api/personal-api-key/token'
ACTIVITIES_PATH = '/activitylist-service/activities/search/activities'


def validate_key(key):
    # Official shape not confirmed: only validate header safety, never invent a prefix.
    if not isinstance(key,str) or not 1 <= len(key) <= 4096 or not re.fullmatch(r'[!-~]+',key):
        raise ValueError('key 须为无空白的可打印 ASCII；具体格式待真实 key 核验')
    return key


class GarminError(Exception):
    def __init__(self, message='佳明请求失败（端点待真实 key 实测）', status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def http_transport(method, url, headers):
    try:
        with build_opener(NoRedirect()).open(Request(url,method=method,headers=headers),timeout=20) as response:
            body = response.read(5*1024*1024+1)
            if len(body)>5*1024*1024:
                raise GarminError('佳明响应过大')
            return response.status,body
    except HTTPError as exc:
        # No body previews: upstream may echo secrets.
        return exc.code,b''


class GarminClient:
    def __init__(self,key,transport=None,sleep=time.sleep):
        self._key = validate_key(key)
        self._token = None
        self._expires = 0
        self.transport = transport or http_transport
        self.sleep = sleep

    def _request(self,method,path,authorization):
        for attempt in range(3):
            try:
                status,body = self.transport(method,BASE+path,{'Authorization':authorization,'Accept':'*/*'})
            except Exception:
                raise GarminError('佳明网络请求失败；凭证与响应内容已隐藏') from None
            if status==429 or 500<=status<=599:
                if attempt<2:
                    self.sleep(2**attempt)
                    continue
            if status!=200:
                raise GarminError('佳明请求失败 HTTP '+str(status),status=status)
            return body
        raise GarminError()

    def test_key(self):
        body=self._request('POST',TOKEN_PATH,'ApiKey '+self._key)
        try:
            text=body.decode('utf-8').strip()
            value=json.loads(text) if text.startswith(('{','"')) else text
            if isinstance(value,dict):
                token=value.get('access_token') or value.get('accessToken') or value.get('token')
                ttl=float(value.get('expires_in',value.get('expiresIn',86400)))
            else:
                token=value;ttl=86400
            validate_key(token)
            if not 0 < ttl <= 86400: ttl=3600
        except Exception:
            raise GarminError('佳明 token 响应格式未核验；响应内容已隐藏') from None
        self._token=token
        self._expires=time.monotonic()+max(ttl-60,1)

    def list_activities(self,start=0,limit=100):
        if not self._token or time.monotonic()>=self._expires:
            self.test_key()
        path=ACTIVITIES_PATH+'?start='+str(int(start))+'&limit='+str(min(max(int(limit),1),100))
        for attempt in range(2):
            try:
                body=self._request('GET',path,'Bearer '+self._token)
                break
            except GarminError as exc:
                if exc.status==401 and attempt==0:
                    self.test_key()
                else: raise
        try:
            rows=json.loads(body)
            if not isinstance(rows,list) or len(rows)>100 or not all(isinstance(r,dict) for r in rows):
                raise ValueError()
            return rows
        except Exception:
            raise GarminError('佳明活动响应结构待核验；响应内容已隐藏') from None


class DemoGarminClient:
    def test_key(self):
        return None

    def list_activities(self,start=0,limit=100):
        if start: return []
        return [dict(activityId='demo-1',startTimeGMT='2026-11-16 06:00:00',
                     activityType={'typeKey':'running'},distance=5000,duration=1800,averageHR=145)]
