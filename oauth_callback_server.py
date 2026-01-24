"""Simple HTTPS callback server to capture Schwab OAuth code."""

import ssl
import json
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
import os

# Generate self-signed cert if needed
CERT_FILE = "C:/Max_AI/tokens/server.crt"
KEY_FILE = "C:/Max_AI/tokens/server.key"


class OAuthCallbackHandler(BaseHTTPRequestHandler):
    """Handle OAuth callback requests."""

    def do_GET(self):
        """Handle GET request from Schwab OAuth redirect."""
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)

        code = params.get("code", [None])[0]

        if code:
            # Save code to file for easy retrieval
            with open("C:/Max_AI/tokens/oauth_code.txt", "w") as f:
                f.write(code)

            # Send success response
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()

            html = f"""
            <!DOCTYPE html>
            <html>
            <head><title>MAX_AI - Authorization Successful</title></head>
            <body style="font-family: Arial, sans-serif; max-width: 800px; margin: 50px auto; padding: 20px;">
                <h1 style="color: #2e7d32;">Authorization Successful!</h1>
                <p>Your Schwab account has been connected to MAX_AI Scanner.</p>
                <div style="background: #f5f5f5; padding: 15px; border-radius: 5px; margin: 20px 0;">
                    <strong>Authorization Code:</strong><br>
                    <code style="word-break: break-all; font-size: 12px;">{code[:50]}...</code>
                </div>
                <p>The code has been saved. You can close this window.</p>
                <p>Run this command to complete authentication:</p>
                <pre style="background: #263238; color: #aed581; padding: 10px; border-radius: 5px;">
curl -X POST http://127.0.0.1:8787/auth/callback -H "Content-Type: application/json" -d '{{"code": "{code}"}}'
                </pre>
            </body>
            </html>
            """
            self.wfile.write(html.encode())
            print(f"\n{'='*60}")
            print("AUTHORIZATION CODE RECEIVED!")
            print(f"{'='*60}")
            print(f"Code: {code[:80]}...")
            print(f"\nCode saved to: C:/Max_AI/tokens/oauth_code.txt")
            print(f"{'='*60}\n")
        else:
            self.send_response(400)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Error: No authorization code received</h1>")

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
