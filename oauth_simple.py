"""Super simple OAuth callback - just captures the code."""

import ssl
import json
import sys
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os

CERT_FILE = "C:/Max_AI/tokens/server.crt"
KEY_FILE = "C:/Max_AI/tokens/server.key"
CODE_FILE = "C:/Max_AI/tokens/oauth_code.txt"

class SimpleHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        sys.stdout.write(f"Request: {self.path}\n")
        sys.stdout.flush()

        params = parse_qs(urlparse(self.path).query)
        code = params.get("code", [None])[0]

        if code:
            sys.stdout.write(f"CODE FOUND: {code[:50]}...\n")
            sys.stdout.flush()

            # Save immediately
            with open(CODE_FILE, "w") as f:
                f.write(code)

            sys.stdout.write(f"Code saved to {CODE_FILE}\n")
            sys.stdout.flush()

            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Code captured! Close this window.</h1>")
        else:
            sys.stdout.write("No code in request\n")
            sys.stdout.flush()
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b"No code")

    def log_message(self, *args):
        pass

def main():
    # Remove old code file
    if os.path.exists(CODE_FILE):
        os.remove(CODE_FILE)

    server = HTTPServer(("127.0.0.1", 6969), SimpleHandler)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(CERT_FILE, KEY_FILE)
    server.socket = ctx.wrap_socket(server.socket, server_side=True)

    print("Listening on https://127.0.0.1:6969", flush=True)
    server.handle_request()
    print("Done", flush=True)

if __name__ == "__main__":
    main()
