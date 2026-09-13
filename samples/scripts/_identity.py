"""Tokens for the two CLI identities.

IAP on Cloud Run uses the Google-managed OAuth client, which admits users in a
browser but not from scripts. The scripts therefore act as the operator and
approver service accounts (infra/envs/dev/operators.tf): anyone in the users
group may sign a JWT for either, and IAP accepts it with the service URL plus
"/*" as audience. This mirrors scripts/iap-token.sh in Python.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

from milos.client import Client

RUNTIME_PROJECT = os.environ.get("MILOS_RUNTIME_PROJECT", "milos-yo-runtime-dev")
API_URL = os.environ["MILOS_API_URL"]
OPERATOR = f"milos-operator@{RUNTIME_PROJECT}.iam.gserviceaccount.com"
APPROVER = f"milos-approver@{RUNTIME_PROJECT}.iam.gserviceaccount.com"


def iap_token(service_account: str, *, ttl: int = 3600) -> str:
    now = int(time.time())
    claims = {
        "iss": service_account,
        "sub": service_account,
        "email": service_account,
        "aud": API_URL.rstrip("/") + "/*",
        "iat": now,
        "exp": now + ttl,
    }
    with tempfile.TemporaryDirectory() as tmp:
        claims_path, out_path = Path(tmp) / "claims.json", Path(tmp) / "token.jwt"
        claims_path.write_text(json.dumps(claims))
        subprocess.run(
            [
                "gcloud",
                "iam",
                "service-accounts",
                "sign-jwt",
                f"--iam-account={service_account}",
                str(claims_path),
                str(out_path),
            ],
            check=True,
            capture_output=True,
        )
        return out_path.read_text().strip()


def client_as(service_account: str) -> Client:
    return Client(API_URL, token=iap_token(service_account))
