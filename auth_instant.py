"""Instant OAuth - exchanges code the MOMENT it arrives."""

import ssl
import json
import base64
import os
import sys
import gzip
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, unquote
from datetime import datetime, timedelta
import urllib.request
import urllib.parse

CERT_FILE = "C:/Max_AI/tokens/server.crt"
KEY_FILE = "C:/Max_AI/tokens/server.key"

CLIENT_ID = "iYa2563asjgdr2RAYpJxAcATc1yPkzEB"
CLIENT_SECRET = "w5whj4x525yZt62u"
REDIRECT_URI = "HTTPS://127.0.0.1:6969"

code_received = False

class InstantHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        global code_received

        print(f">>> REQUEST: {self.path}", flush=True)

        # Skip favicon requests
        if "favicon" in self.path:
            self.send_response(404)
            self.end_headers()
            return

        params = parse_qs(urlparse(self.path).query)
        raw_code = params.get("code", [None])[0]

        # Ensure code is properly URL-decoded
        code = unquote(raw_code) if raw_code else None

        print(f">>> PARAMS: {list(params.keys())}", flush=True)
        print(f">>> RAW CODE: {raw_code[:50] if raw_code else 'NONE'}...", flush=True)
        print(f">>> DECODED CODE: {code[:50] if code else 'NONE'}...", flush=True)

        if not code:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code - waiting for auth redirect...")
            return

        print(f"\n>>> CODE RECEIVED! Exchanging NOW...", flush=True)

        # Exchange IMMEDIATELY
        try:
            credentials = f"{CLIENT_ID}:{CLIENT_SECRET}"
            encoded_creds = base64.b64encode(credentials.encode()).decode()

            # Build the request data
            post_data = {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            }

            print(f">>> POST DATA: grant_type=authorization_code, redirect_uri={REDIRECT_URI}", flush=True)
            print(f">>> CODE LENGTH: {len(code)}", flush=True)

            data = urllib.parse.urlencode(post_data).encode()

            req = urllib.request.Request(
                "https://api.schwabapi.com/v1/oauth/token",
                data=data,
                headers={
                    "Authorization": f"Basic {encoded_creds}",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            )

            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
                result = json.loads(response.read().decode())

                expiry = datetime.utcnow() + timedelta(seconds=result.get("expires_in", 1800))

                # Save to MAX_AI
                max_ai_tokens = {
                    "access_token": result["access_token"],
                    "refresh_token": result["refresh_token"],
                    "expiry": expiry.isoformat(),
                }
                os.makedirs("C:/Max_AI/tokens", exist_ok=True)
                with open("C:/Max_AI/tokens/schwab_token.json", "w") as f:
                    json.dump(max_ai_tokens, f, indent=2)

                # Save to Morpheus
                morpheus_tokens = {
                    "access_token": result["access_token"],
                    "refresh_token": result["refresh_token"],
                    "token_type": result.get("token_type", "Bearer"),
                    "expires_in": result.get("expires_in", 1800),
                    "scope": result.get("scope", "api"),
                    "issued_at": datetime.utcnow().timestamp(),
                }
                os.makedirs("C:/Morpheus/Morpheus_AI/tokens", exist_ok=True)
                with open("C:/Morpheus/Morpheus_AI/tokens/schwab_token.json", "w") as f:
                    json.dump(morpheus_tokens, f, indent=2)

                print(">>> SUCCESS! Tokens saved to both locations!", flush=True)
                code_received = True

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<h1 style='color:green'>SUCCESS! Tokens saved. Close this window.</h1>")
                return

        except urllib.error.HTTPError as e:
            raw_error = e.read()
            # Try to decompress if gzip
            try:
                if raw_error[:2] == b'\x1f\x8b':
                    error = gzip.decompress(raw_error).decode('utf-8', errors='replace')
                else:
                    error = raw_error.decode('utf-8', errors='replace')
            except:
                error = repr(raw_error[:200])

            # ASCII-safe print
            safe_error = error.encode('ascii', 'replace').decode('ascii')
            print(f">>> FAILED HTTP {e.code}: {safe_error}", flush=True)
            code_received = True
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"<h1>Failed HTTP {e.code}: {safe_error}</h1>".encode())
        except Exception as e:
            print(f">>> ERROR: {e}", flush=True)
            code_received = True
            self.send_response(500)
            self.end_headers()
            self.wfile.write(f"<h1>Error: {e}</h1>".encode())

    def log_message(self, *args):
        pass

def main():
    global code_received
    code_received = False

    server = HTTPServer(("127.0.0.1", 6969), InstantHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print("=" * 50, flush=True)
    print("INSTANT AUTH SERVER READY", flush=True)
    print("Listening on https://127.0.0.1:6969", flush=True)
    print("Take your time logging in - server will wait...", flush=True)
    print("=" * 50, flush=True)

    # Handle multiple requests until we get a code
    while not code_received:
        server.handle_request()

    print("Server done.", flush=True)

if __name__ == "__main__":
    main()
