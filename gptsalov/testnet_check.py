"""Unsigned Testnet connectivity only. No account access or order submission."""
import json
from urllib.request import Request, build_opener
from .market import NoRedirect

BASE = 'https://demo-fapi.binance.com'

def main():
    opener=build_opener(NoRedirect())
    results={}
    for path in ('/fapi/v1/time','/fapi/v1/exchangeInfo'):
        with opener.open(Request(BASE+path,method='GET'),timeout=12) as r:
            body=r.read(8000001)
            if len(body)>8000000:
                raise ValueError('Response too large')
            data=json.loads(body)
        if path.endswith('/time'):
            results['server_time']=data['serverTime']
        else:
            results['symbols']=len(data['symbols'])
    print(json.dumps({'mode':'TESTNET_PUBLIC_PREFLIGHT','host':BASE,
        'results':results,'signed_order_tests':'NOT_RUN','real_orders':0}))

if __name__=='__main__':
    main()
