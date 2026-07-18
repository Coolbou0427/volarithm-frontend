"""Generate a self-signed SSL certificate for the Volarithm local server."""
import subprocess
import sys
import socket

# Use the machine's LAN IP so the cert matches the server binding
HOST = "10.0.0.147"

CERT_FILE = "cert.pem"
KEY_FILE = "key.pem"

SUBJ = f"/C=US/ST=CA/L=SF/O=Volarithm/OU=Dev/CN={HOST}"

result = subprocess.run(
    [
        "openssl", "req", "-x509", "-newkey", "rsa:2048",
        "-keyout", KEY_FILE,
        "-out", CERT_FILE,
        "-days", "365",
        "-nodes",
        "-subj", SUBJ,
        "-addext", f"subjectAltName=IP:{HOST}",
    ],
    capture_output=True,
    text=True,
)

if result.returncode != 0:
    print("openssl error:", result.stderr)
    sys.exit(1)

print(f"Generated {CERT_FILE} and {KEY_FILE} for {HOST}")
