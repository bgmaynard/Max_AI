"""Simple HTTPS callback server to capture Schwab OAuth code and exchange for tokens."""

import ssl
import json
import base64
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os
import urllib.request
import urllib.parse

# Generate self-signed cert if needed
CERT_FILE = "C:/Max_AI/tokens/server.crt"
KEY_FILE = "C:/Max_AI/tokens/server.key"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback requests."""

    def do_GET(self):
        """Handle GET request from Schwab OAuth redirect."""
        print(f"Received request: {self.path}", flush=True)
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        print(f"Parsed params: {list(params.keys())}", flush=True)

        code = params.get("code", [None])[0]
        print(f"Code found: {bool(code)}", flush=True)

        if code:
            print(f"\n{'='*60}", flush=True)
            print("AUTHORIZATION CODE RECEIVED!", flush=True)
            print(f"{'='*60}", flush=True)
            print(f"Code: {code[:50]}...", flush=True)
            print("Starting token exchange NOW...", flush=True)

            # IMMEDIATELY exchange code for tokens
            token_result = self.exchange_code_for_tokens(code)
            print(f"Exchange result: {token_result}", flush=True)

            if token_result.get("success"):
                # Save tokens to BOTH locations
                self.save_tokens(token_result["tokens"])

                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                html = """
                <!DOCTYPE html>
                <html>
                <head><title>MAX_AI - Authentication Complete!</title></head>
                <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px;">
                    <h1 style="color: #2e7d32;">Authentication Complete!</h1>
                    <p>Your Schwab account has been connected successfully.</p>
                    <p>Tokens have been saved. You can close this window.</p>
                    <p><strong>Restart both services to use the new tokens.</strong></p>
                </body>
                </html>
                """
                self.wfile.write(html.encode())
                print(f"\n{'='*60}")
                print("TOKENS SAVED SUCCESSFULLY!")
                print(f"{'='*60}\n")
            else:
                self.send_response(500)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                error = token_result.get("error", "Unknown error")
                html = f"""
                <!DOCTYPE html>
                <html>
                <head><title>MAX_AI - Token Exchange Failed</title></head>
                <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px;">
                    <h1 style="color: #c62828;">Token Exchange Failed</h1>
                    <p>Error: {error}</p>
                    <p>Please try again.</p>
                </body>
                </html>
                """
                self.wfile.write(html.encode())
                print(f"Token exchange failed: {error}")
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Error: No authorization code received</h1>")

    def exchange_code_for_tokens(self, code):
        """Exchange authorization code for access tokens immediately."""
        from datetime import datetime, timedelta

        client_id = os.environ.get("SCHWAB_CLIENT_ID", "iYa2563asjgdr2RAYpJxAcATc1yPkzEB")
        client_secret = os.environ.get("SCHWAB_CLIENT_SECRET", "w5whj4x525yZt62u")
        redirect_uri = "HTTPS://127.0.0.1:6969"

        print(f"Exchanging code for tokens...")

        try:
            # Create Basic auth header
            credentials = f"{client_id}:{client_secret}"
            encoded_credentials = base64.b64encode(credentials.encode()).decode()

            # Prepare request
            data = urllib.parse.urlencode({
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            }).encode()

            req = urllib.request.Request(
                "https://api.schwabapi.com/v1/oauth/token",
                data=data,
                headers={
                    "Authorization": f"Basic {encoded_credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                }
            )

            # Make request (ignore SSL verification for speed)
            import ssl
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE

            with urllib.request.urlopen(req, context=ctx, timeout=10) as response:
                result = json.loads(response.read().decode())

                # Calculate expiry
                expires_in = result.get("expires_in", 1800)
                expiry = datetime.utcnow() + timedelta(seconds=expires_in)

                return {
                    "success": True,
                    "tokens": {
                        "access_token": result.get("access_token"),
                        "refresh_token": result.get("refresh_token"),
                        "token_type": result.get("token_type", "Bearer"),
                        "expires_in": expires_in,
                        "scope": result.get("scope", "api"),
                        "expiry": expiry.isoformat(),
                        "issued_at": datetime.utcnow().timestamp(),
                    }
                }

        except urllib.error.HTTPError as e:
            try:
                import gzip
                raw = e.read()
                # Try to decompress if gzipped
                if raw[:2] == b'\x1f\x8b':
                    error_body = gzip.decompress(raw).decode('utf-8', errors='replace')
                else:
                    error_body = raw.decode('utf-8', errors='replace')
            except:
                error_body = str(e)
            return {"success": False, "error": f"HTTP {e.code}: {error_body}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def save_tokens(self, tokens):
        """Save tokens to both MAX_AI and Morpheus locations."""
        # MAX_AI format
        max_ai_tokens = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "expiry": tokens["expiry"],
        }

        # Morpheus format
        morpheus_tokens = {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "token_type": tokens.get("token_type", "Bearer"),
            "expires_in": tokens.get("expires_in", 1800),
            "scope": tokens.get("scope", "api"),
            "issued_at": tokens.get("issued_at"),
        }

        # Save to MAX_AI
        os.makedirs("C:/Max_AI/tokens", exist_ok=True)
        with open("C:/Max_AI/tokens/schwab_token.json", "w") as f:
            json.dump(max_ai_tokens, f, indent=2)
        print(f"Saved tokens to: C:/Max_AI/tokens/schwab_token.json")

        # Save to Morpheus
        os.makedirs("C:/Morpheus/Morpheus_AI/tokens", exist_ok=True)
        with open("C:/Morpheus/Morpheus_AI/tokens/schwab_token.json", "w") as f:
            json.dump(morpheus_tokens, f, indent=2)
        print(f"Saved tokens to: C:/Morpheus/Morpheus_AI/tokens/schwab_token.json")

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass


def generate_self_signed_cert():
    """Generate self-signed certificate for HTTPS."""
    import subprocess

    os.makedirs("C:/Max_AI/tokens", exist_ok=True)

    if os.path.exists(CERT_FILE) and os.path.exists(KEY_FILE):
        print("Using existing SSL certificate")
        return

    print("Generating self-signed SSL certificate...")

    # Use OpenSSL to generate cert
    cmd = [
        "openssl", "req", "-x509", "-newkey", "rsa:4096",
        "-keyout", KEY_FILE,
        "-out", CERT_FILE,
        "-days", "365",
        "-nodes",
        "-subj", "/CN=localhost"
    ]

    try:
        subprocess.run(cmd, check=True, capture_output=True)
        print("SSL certificate generated successfully")
    except Exception as e:
        print(f"Could not generate cert with openssl: {e}")
        print("Trying alternative method...")
        # Fall back to Python cryptography if available
        try:
            from cryptography import x509
            from cryptography.x509.oid import NameOID
            from cryptography.hazmat.primitives import hashes, serialization
            from cryptography.hazmat.primitives.asymmetric import rsa
            from datetime import datetime, timedelta

            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

            subject = issuer = x509.Name([
                x509.NameAttribute(NameOID.COMMON_NAME, "localhost"),
            ])

            cert = (
                x509.CertificateBuilder()
                .subject_name(subject)
                .issuer_name(issuer)
                .public_key(key.public_key())
                .serial_number(x509.random_serial_number())
                .not_valid_before(datetime.utcnow())
                .not_valid_after(datetime.utcnow() + timedelta(days=365))
                .add_extension(
                    x509.SubjectAlternativeName([
                        x509.DNSName("localhost"),
                        x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                    ]),
                    critical=False,
                )
                .sign(key, hashes.SHA256())
            )

            with open(KEY_FILE, "wb") as f:
                f.write(key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.TraditionalOpenSSL,
                    encryption_algorithm=serialization.NoEncryption(),
                ))

            with open(CERT_FILE, "wb") as f:
                f.write(cert.public_bytes(serialization.Encoding.PEM))

            print("SSL certificate generated with cryptography library")
        except ImportError:
            print("ERROR: Could not generate SSL certificate")
            print("Install cryptography: pip install cryptography")
            raise


def main():
    """Run the OAuth callback server."""
    import ipaddress

    generate_self_signed_cert()

    server_address = ("127.0.0.1", 6969)
    httpd = HTTPServer(server_address, OAuthCallbackHandler)

    # Wrap with SSL
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(CERT_FILE, KEY_FILE)
    httpd.socket = context.wrap_socket(httpd.socket, server_side=True)

    print(f"\n{'='*60}")
    print("MAX_AI OAuth Callback Server")
    print(f"{'='*60}")
    print(f"Listening on: https://127.0.0.1:6969")
    print(f"Waiting for Schwab OAuth callback...")
    print(f"{'='*60}\n")

    try:
        httpd.handle_request()  # Handle just one request
        print("\nCallback received. Server shutting down.")
    except KeyboardInterrupt:
        print("\nServer stopped.")


if __name__ == "__main__":
    main()
