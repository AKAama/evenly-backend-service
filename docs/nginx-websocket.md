# Nginx WebSocket for voice expense

iOS voice expense connects to:

```text
wss://evenly.ismyh.cn/api/expenses/ledgers/{ledger_id}/voice-session
```

## Smoking-gun log

If backend access log shows a **plain GET** with **HTTP/1.0** and 404:

```text
GET /expenses/ledgers/.../voice-session HTTP/1.0" 404
```

that means the reverse proxy **did not upgrade** the connection. Uvicorn only
registers a WebSocket handler; a normal GET is not a match (or, after the
diagnostic route, returns **426** with a clear message).

iOS then surfaces:

```text
NSURLErrorDomain Code=-1011
There was a bad response from the server
```

## Required proxy settings

Put the `map` in `http { }` (once). Put the rest in the API `location`.

```nginx
# inside http { }
map $http_upgrade $connection_upgrade {
    default upgrade;
    ''      close;
}

# API location (example)
location /api/ {
    # CRITICAL: WebSocket needs HTTP/1.1 to the upstream
    proxy_http_version 1.1;
    proxy_pass http://127.0.0.1:8000/;   # keep path mapping consistent with your setup

    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;

    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header Authorization $http_authorization;

    proxy_read_timeout 3600s;
    proxy_send_timeout 3600s;
    proxy_buffering off;
}
```

### Common mistakes

| Mistake | Result |
|--------|--------|
| Missing `proxy_http_version 1.1` | Upstream sees **HTTP/1.0**, WS fails |
| `Connection keep-alive` hard-coded | Upgrade stripped |
| No `Upgrade` header | Backend GET → 404/426 |
| Short `proxy_read_timeout` | Socket dies mid-session |

## Quick checks

```bash
# Expect: HTTP/1.1 101 Switching Protocols
# Also send your app login header (Authorization Bearer …). Omit real tokens from docs/commits.
curl -i -N \
  -H "Connection: Upgrade" \
  -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" \
  -H "Sec-WebSocket-Key: sample-ws-nonce" \
  "https://evenly.ismyh.cn/api/expenses/ledgers/LEDGER_UUID/voice-session"
```

That heredoc might be wrong for curl --header @-. Simpler: just document without Authorization in the command.

Plain GET (no Upgrade) after deploying the diagnostic route should return **426**
with a Chinese detail string — not a silent 404.

Also ensure Tencent ASR / OpenAI keys are set; after the socket opens, missing
ASR config returns a JSON `error` event instead of -1011.
