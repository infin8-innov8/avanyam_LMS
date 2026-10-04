#!/usr/bin/env bash
# Generate SeaweedFS s3.json identities (K11).
#
# Why this exists: s3.json is deliberately gitignored because it holds live
# credentials, which left a fresh clone with no identity file at all -- the S3
# gateway would come up with no identities and reject every request, and
# provision-s3.sh had nothing to read. This script closes that gap.
#
# Run once on a new host, BEFORE `docker compose up -d`:
#   ./scripts/gen-s3-identity.sh
# Then provision the buckets:
#   ./scripts/provision-s3.sh
# And verify:
#   ./scripts/verify-s3.sh
#
# It is safe to re-run only if you intend to INVALIDATE every existing object
# credential, because SeaweedFS reads identities from this file at startup.

set -euo pipefail

CONF="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/seaweedfs/conf/s3.json"
BUCKET_APP="${BUCKET_APP:-avanyam-media}"

if [ -e "$CONF" ]; then
    echo "refusing to overwrite existing $CONF" >&2
    echo "delete it first if you really mean to rotate every credential:" >&2
    echo "  sudo rm $CONF && sudo $0" >&2
    exit 1
fi

mkdir -p "$(dirname "$CONF")"

# Credentials are generated with python secrets, not shell $RANDOM (which is
# predictable) and not openssl (whose base64 alphabet includes '/' and '+',
# which some S3 clients mishandle in a secret key).
CONF="$CONF" BUCKET_APP="$BUCKET_APP" python3 - <<'PY'
import json, os, secrets, string

# Unambiguous alphabet: no 0/O, no 1/l/I. These keys get pasted into config
# files, .env files and shell history by hand, and a transposed character is a
# support ticket rather than a security event.
ALPHA = "".join(c for c in string.ascii_letters + string.digits if c not in "0O1lI")

def token(prefix: str, n: int) -> str:
    return prefix + "".join(secrets.choice(ALPHA) for _ in range(n))

app_ak = token("AVANYAM", 20)
app_sk = token("", 40)
# The provisioner keeps a deterministic access key because verify-s3.sh and
# provision-s3.sh reference it; only its secret is random.
prov_ak = "AVANYAMPROVISION00001"
prov_sk = token("", 40)

doc = {
    "identities": [
        {
            "name": "avanyam-app",
            "credentials": [{"accessKey": app_ak, "secretKey": app_sk}],
            # No "Admin": the application must not be able to create buckets or
            # change bucket policy. verify-s3.sh asserts this.
            "actions": ["Read", "Write", "List"],
        },
        {
            "name": "avanyam-provisioner",
            "credentials": [{"accessKey": prov_ak, "secretKey": prov_sk}],
            # "Admin" is needed once, to create the bucket and set its policy.
            "actions": ["Admin", "Read", "Write", "List"],
        },
    ]
}

path = os.environ["CONF"]
with open(path, "w") as fh:
    json.dump(doc, fh, indent=2)
    fh.write("\n")
os.chmod(path, 0o600)

print(f"wrote {path}")
print(f"  avanyam-app        accessKey={app_ak}  actions=Read,Write,List")
print(f"  avanyam-provisioner accessKey={prov_ak}  actions=Admin,Read,Write,List")
print()
print("The secret keys are NOT printed. Read them when you need them:")
print(f"  sudo python3 -c \"import json;print(json.load(open('{path}'))"
      "['identities'][0]['credentials'][0]['secretKey'])\"")
print()
print("next:")
print(f"  ./scripts/provision-s3.sh      # creates {os.environ['BUCKET_APP']}")
print("  ./scripts/verify-s3.sh")
print("  sudo docker compose up -d      # SeaweedFS reads identities at startup")
PY
