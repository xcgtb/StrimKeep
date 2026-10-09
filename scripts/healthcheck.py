#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Container liveness probe for Docker HEALTHCHECK.
Only does one thing: request /api/health on localhost, treat 2xx as healthy.
Deliberately minimal and decoupled from frontend implementation details.
For full project diagnostics use dev/tools/storage_audit.py instead.
"""
import os
import sys
import urllib.request

PORT = os.environ.get('PORT', '8321')
URL = 'http://127.0.0.1:%s/api/health' % PORT


def main():
    try:
        with urllib.request.urlopen(URL, timeout=5) as r:
            if 200 <= r.status < 300:
                sys.exit(0)
            print('healthcheck failed: HTTP %s' % r.status, file=sys.stderr)
            sys.exit(1)
    except Exception as e:
        print('healthcheck failed: %s' % e, file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
