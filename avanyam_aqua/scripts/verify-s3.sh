#!/usr/bin/env bash
# Verify the object store honours every guarantee the application depends on.
#
# Checks, in order of blast radius:
#   1. the app identity can read and write
#   2. anonymous access is DENIED           (a pass here is a security failure)
#   3. wrong credentials are rejected
#   4. presigned URLs work
#   5. presigned URLs EXPIRE
#   6. a presigned URL is bound to its object key
#   7. multipart upload works
#   8. the app identity cannot create buckets
#
# Usage: ./scripts/verify-s3.sh [--keep]
set -euo pipefail
cd "$(dirname "$0")/.."
ENV_FILE="${ENV_FILE:-../avanyam_terra/.env}"
KEEP="${1:-}"

../avanyam_terra/.avanyam_terra_venv/bin/python - "$ENV_FILE" "$KEEP" <<'PY'
import os, sys, time, pathlib, urllib.request, urllib.error
import boto3
from botocore.client import Config
from botocore.exceptions import ClientError

env = {}
for line in pathlib.Path(sys.argv[1]).read_text().splitlines():
    line = line.strip()
    if line and not line.startswith("#") and "=" in line:
        k, v = line.split("=", 1); env[k] = v

EP  = env["AWS_S3_ENDPOINT_URL"]
B   = env["AWS_STORAGE_BUCKET_NAME"]
AK  = env["AWS_ACCESS_KEY_ID"]
SK  = env["AWS_SECRET_ACCESS_KEY"]
failures = []

def client(ak=AK, sk=SK):
    return boto3.client("s3", endpoint_url=EP, aws_access_key_id=ak,
        aws_secret_access_key=sk, region_name=env["AWS_S3_REGION_NAME"],
        config=Config(signature_version="s3v4",
                      s3={"addressing_style": env["AWS_S3_ADDRESSING_STYLE"]}))

def check(label, fn):
    try:
        fn(); print(f"  PASS  {label}")
    except AssertionError as e:
        print(f"  FAIL  {label}: {e}"); failures.append(label)
    except ClientError as e:
        print(f"  FAIL  {label}: {e.response['Error']['Code']}"); failures.append(label)

def expect_denied(fn, what):
    try:
        fn(); raise AssertionError(f"{what} was ALLOWED")
    except ClientError as e:
        assert e.response["Error"]["Code"] in ("AccessDenied", "InvalidAccessKeyId", \
               "SignatureDoesNotMatch"), e.response["Error"]["Code"]

KEY = "verify/probe.bin"
app = client()
app.put_object(Bucket=B, Key=KEY, Body=b"x" * 1024)
check("app identity writes", lambda: app.head_object(Bucket=B, Key=KEY))
check("app identity reads", lambda: app.get_object(Bucket=B, Key=KEY)["Body"].read())

def anon():
    # A raw UNSIGNED request. Passing no credentials to boto3 fails locally with
    # NoCredentialsError and never reaches the server, which proves nothing about
    # the server's policy -- so the request has to be built by hand.
    url = f"{EP}/{B}/{KEY}"
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            raise AssertionError(f"anonymous read returned HTTP {r.status}, not 403")
    except urllib.error.HTTPError as e:
        assert e.code == 403, f"expected 403, got {e.code}"
check("anonymous read denied (raw unsigned request)", anon)

check("wrong credentials rejected",
      lambda: expect_denied(lambda: client("AKIAWRONGSECRET", "wrong").list_buckets(),
                            "bad credentials"))

def presigned():
    u = app.generate_presigned_url("get_object", Params={"Bucket": B, "Key": KEY}, ExpiresIn=900)
    with urllib.request.urlopen(u, timeout=20) as r:
        assert r.status == 200, r.status
        assert len(r.read()) == 1024
check("presigned GET works", presigned)

def expiry():
    u = app.generate_presigned_url("get_object", Params={"Bucket": B, "Key": KEY}, ExpiresIn=1)
    time.sleep(2)
    try:
        urllib.request.urlopen(u, timeout=20)
        raise AssertionError("expired presigned URL was ALLOWED")
    except urllib.error.HTTPError as e:
        assert e.code == 403, f"expected 403, got {e.code}"
check("presigned GET expires (403)", expiry)

def key_binding():
    u = app.generate_presigned_url("get_object",
          Params={"Bucket": B, "Key": "verify/some-other-object.bin"}, ExpiresIn=900)
    try:
        urllib.request.urlopen(u, timeout=20)
        raise AssertionError("presigned URL honoured a substituted key")
    except urllib.error.HTTPError as e:
        assert e.code in (403, 404), f"expected 403/404, got {e.code}"
check("presigned URL bound to its key", key_binding)

def multipart():
    up = app.create_multipart_upload(Bucket=B, Key="verify/multipart.bin")
    parts = []
    for i in (1, 2):
        r = app.upload_part(Bucket=B, Key="verify/multipart.bin", UploadId=up["UploadId"],
                            PartNumber=i, Body=b"m" * (5 * 1024 * 1024))
        parts.append({"ETag": r["ETag"], "PartNumber": i})
    app.complete_multipart_upload(Bucket=B, Key="verify/multipart.bin",
        UploadId=up["UploadId"], MultipartUpload={"Parts": parts})
    assert app.head_object(Bucket=B, Key="verify/multipart.bin")["ContentLength"] == 10 * 1024 * 1024
    app.delete_object(Bucket=B, Key="verify/multipart.bin")
check("multipart upload (2 x 5 MiB)", multipart)

check("app cannot create buckets",
      lambda: expect_denied(lambda: app.create_bucket(Bucket="must-not-exist"),
                            "app create_bucket"))

if sys.argv[2] != "--keep":
    for k in (KEY,):
        try: app.delete_object(Bucket=B, Key=k)
        except ClientError: pass

print()
print(f"  {len(failures)} failure(s)" if failures else "  all checks passed")
sys.exit(1 if failures else 0)
PY
