#!/usr/bin/env bash
# Provision the Avanyam media bucket.
#
# WHY THIS IS A SCRIPT AND NOT A ONE-LINER
# SeaweedFS keeps filer metadata and volume data in the MOUNTED VOLUME, not in
# the image. Recreating the container with the wrong volume path yields a
# running, healthy, fully-authenticating S3 endpoint that contains nothing.
# `list_buckets()` returning [] is the only symptom. Always run this after a
# first `docker compose up -d` on a new host, and after any volume change.
#
# The app identity deliberately has NO Admin action, so it cannot create the
# bucket: a separate provisioner identity is used here. This is intentional --
# the web process should never hold bucket-creation rights.
#
# Usage: ./scripts/provision-s3.sh [--dry-run]
set -euo pipefail

cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-../avanyam_terra/.env}"
DRY_RUN="${1:-}"

[ -f "$ENV_FILE" ] || { echo "FATAL: $ENV_FILE not found" >&2; exit 1; }

echo "==> endpoint  $(grep '^AWS_S3_ENDPOINT_URL=' "$ENV_FILE" | cut -d= -f2)"
echo "==> bucket    $(grep '^AWS_STORAGE_BUCKET_NAME=' "$ENV_FILE" | cut -d= -f2)"

# Pull the two identities out of the store's own config. The provisioner secret
# never appears in an application env file.
CONF=./data/seaweedfs/conf/s3.json
[ -f "$CONF" ] || { echo "FATAL: $CONF not found" >&2; exit 1; }
PROV_KEY=$(python3 -c "import json;print(next(i['credentials'][0]['accessKey'] for i in json.load(open('$CONF'))['identities'] if 'provision' in i['name']))")
PROV_SECRET=$(python3 -c "import json;print(next(i['credentials'][0]['secretKey'] for i in json.load(open('$CONF'))['identities'] if 'provision' in i['name']))")

if [ "$DRY_RUN" = "--dry-run" ]; then echo "--> dry run, no changes"; exit 0; fi

# Bucket naming is validated locally against the real S3 rules, because an
# invalid name surfaces as a confusing gateway error rather than a clear one.
# Hyphens and single interior periods ARE legal; adjacent periods are not.
BUCKET=$(grep '^AWS_STORAGE_BUCKET_NAME=' "$ENV_FILE" | cut -d= -f2-)
python3 - "$BUCKET" <<'PY'
import re, sys
b = sys.argv[1]
if not 3 <= len(b) <= 63:
    sys.exit(f"FATAL: bucket name must be 3-63 characters, got {len(b)}")
if not re.fullmatch(r"[a-z0-9][a-z0-9.-]*[a-z0-9]", b):
    sys.exit(f"FATAL: '{b}' must be lowercase letters, digits, hyphens and "
             "periods, and must start and end with a letter or digit")
if ".." in b:
    sys.exit(f"FATAL: '{b}' contains adjacent periods")
if re.fullmatch(r"[0-9.]+", b):
    sys.exit(f"FATAL: '{b}' must not be formatted as an IPv4 address")
PY

../avanyam_terra/.avanyam_terra_venv/bin/python - "$PROV_KEY" "$PROV_SECRET" "$BUCKET" <<'PY'
import sys, pathlib, urllib.request
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

prov_key, prov_secret, bucket = sys.argv[1], sys.argv[2], sys.argv[3]
env = {}
for line in pathlib.Path("../avanyam_terra/.env").read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k] = v
endpoint = env["AWS_S3_ENDPOINT_URL"]

def client(ak, sk):
    return boto3.client("s3", endpoint_url=endpoint, aws_access_key_id=ak,
        aws_secret_access_key=sk, region_name=env["AWS_S3_REGION_NAME"],
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}))

prov = client(prov_key, prov_secret)
try:
    prov.create_bucket(Bucket=bucket); print(f"    created {bucket}")
except ClientError as e:
    code = e.response["Error"]["Code"]
    print(f"    {'already exists' if 'BucketAlready' in code else 'ERROR ' + code}")
    if "BucketAlready" not in code: sys.exit(1)

# Prove the app identity can actually USE the bucket, not just authenticate.
app = client(env["AWS_ACCESS_KEY_ID"], env["AWS_SECRET_ACCESS_KEY"])
probe = "provisioning/healthcheck"
app.put_object(Bucket=bucket, Key=probe, Body=b"ok")
assert app.get_object(Bucket=bucket, Key=probe)["Body"].read() == b"ok"
app.delete_object(Bucket=bucket, Key=probe)

try:
    app.create_bucket(Bucket="app-must-not-create")
    print("    ERROR: app identity can create buckets -- least privilege is broken"); sys.exit(1)
except ClientError as e:
    assert e.response["Error"]["Code"] == "AccessDenied", e.response["Error"]
    print("    app identity: read/write OK, create_bucket DENIED (correct)")
print("    provisioning OK")
PY

echo "==> done"
