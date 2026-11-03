"""Live check of the Kernel binding policy against the deployed product API.

Registers a throwaway owner account (binding needs a manager session), creates
a project, then probes the three binding outcomes: configured, foreign URL,
foreign Kernel workspace.
"""

import os
import uuid

import httpx

BASE = "http://api:8000"
ADMIN = {"X-Admin-Token": os.environ["ADMIN_TOKEN"]}
slug = uuid.uuid4().hex[:8]

registered = httpx.post(f"{BASE}/v1/auth/register", timeout=30, json={
    "email": f"policy-{slug}@example.test",
    "password": "correct-horse-battery-staple",
    "display_name": f"policy {slug}",
    "workspace_name": f"policy-{slug}",
})
print("register", registered.status_code, registered.text[:120])
token = registered.json()["token"]
auth = {"Authorization": f"Bearer {token}"}

project = httpx.post(f"{BASE}/v1/projects", headers=auth, timeout=30,
                     json={"name": "smoke-policy", "repo": "/repos/smoke.git"})
print("create-project", project.status_code, project.text[:100])
name = project.json().get("name") or project.json().get("project")

cases = [
    ("configured", {"repo_path": "/repos/smoke.git",
                    "kernel_workspace_id": "1028e289-8b66-444c-bc8b-099dfedeffeb",
                    "kernel_base_url": "http://veynod-kernel-api:8001"}, 200),
    ("foreign-url", {"repo_path": "/repos/smoke.git",
                     "kernel_workspace_id": "1028e289-8b66-444c-bc8b-099dfedeffeb",
                     "kernel_base_url": "https://attacker.example"}, 422),
    ("foreign-workspace", {"repo_path": "/repos/smoke.git",
                           "kernel_workspace_id": "00000000-0000-0000-0000-000000000001",
                           "kernel_base_url": "http://veynod-kernel-api:8001"}, 403),
]
failed = 0
for label, body, expect in cases:
    response = httpx.post(f"{BASE}/v1/projects/{name}/repositories", headers=auth,
                          json=body, timeout=30)
    ok = response.status_code == expect
    failed += not ok
    print(f"{label}: {response.status_code} (expect {expect}) "
          f"{'OK' if ok else 'MISMATCH'} {response.text[:130]}")
print("POLICY-OK" if not failed else "POLICY-FAIL")
