#!/usr/bin/env bash
# Generate the local development TLS material for Nginx (K5).
#
# avanyam_intro.txt L1176 specifies "TLS via internal CA". This creates a local
# CA plus a leaf certificate for avanyam.local, which is the honest development
# equivalent: the browser validates the chain for real, so the TLS path is
# genuinely exercised. The alternative -- shipping a self-signed leaf and telling
# people to click through, or worse `ssl_verify off` -- means TLS bugs stay
# hidden until production.
#
# The CA private key is what you would normally protect with SOPS/age. It is
# written mode 600 under avanyam_terra/conf/tls/ and that directory is gitignored.
#
# Usage: sudo ./scripts/gen-dev-tls.sh
# Re-running overwrites the leaf but keeps the CA, so the CA can stay trusted.

set -euo pipefail

# The script lives in avanyam_terra/scripts, so the VM root is one level up.
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TLS_DIR="$ROOT/conf/tls"
CA_DIR="$TLS_DIR/ca"
INSTALL_DIR=/etc/nginx/tls

mkdir -p "$CA_DIR"
umask 077

if [ ! -f "$CA_DIR/ca.key" ]; then
    echo "  creating local development CA"
    openssl req -x509 -newkey rsa:4096 -sha256 -days 3650 -nodes \
        -keyout "$CA_DIR/ca.key" -out "$CA_DIR/ca.crt" \
        -subj "/CN=Avanyam Development CA/O=Avanyam (dev only)" \
        -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
        -addext "keyUsage=critical,keyCertSign,cRLSign" 2>/dev/null
else
    echo "  reusing existing development CA"
fi

cat > "$TLS_DIR/leaf.cnf" <<'EOF'
[req]
default_bits       = 2048
prompt             = no
default_md         = sha256
distinguished_name = dn
req_extensions     = ext

[dn]
CN = avanyam.local
O  = Avanyam (dev only)

[ext]
basicConstraints       = critical,CA:FALSE
keyUsage               = critical,digitalSignature,keyEncipherment
extendedKeyUsage       = serverAuth
subjectAltName         = @alt

[alt]
DNS.1 = avanyam.local
DNS.2 = localhost
DNS.3 = keycloak.local
DNS.4 = s3.local
IP.1  = 127.0.0.1
EOF

echo "  issuing leaf certificate for avanyam.local"
openssl req -newkey rsa:2048 -nodes \
    -keyout "$TLS_DIR/avanyam.key" -out "$TLS_DIR/avanyam.csr" \
    -config "$TLS_DIR/leaf.cnf" 2>/dev/null

openssl x509 -req -in "$TLS_DIR/avanyam.csr" \
    -CA "$CA_DIR/ca.crt" -CAkey "$CA_DIR/ca.key" -CAcreateserial \
    -out "$TLS_DIR/avanyam.crt" -days 825 -sha256 \
    -extfile "$TLS_DIR/leaf.cnf" -extensions ext 2>/dev/null

rm -f "$TLS_DIR/avanyam.csr"

echo "  verifying chain"
openssl verify -CAfile "$CA_DIR/ca.crt" "$TLS_DIR/avanyam.crt"

echo
echo "  installing to $INSTALL_DIR"
install -d -m 0755 "$INSTALL_DIR"
install -m 0644 "$TLS_DIR/avanyam.crt" "$INSTALL_DIR/avanyam.crt"
install -m 0600 "$TLS_DIR/avanyam.key" "$INSTALL_DIR/avanyam.key"
install -m 0644 "$CA_DIR/ca.crt"      "$INSTALL_DIR/avanyam-ca.crt"

echo
echo "  To trust the dev CA in a browser, import:"
echo "    $CA_DIR/ca.crt"
echo
echo "  next: nginx -t && systemctl reload nginx"
