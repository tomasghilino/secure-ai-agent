"""Operate the AWS staging environment: migrations, seeding, demo users, tokens, uploads.

Reads the environment's identifiers from `terraform output`, and uses the operator's AWS
credentials (an IAM Identity Center profile: AWS_PROFILE). Usage:

    uv run python -m scripts.staging migrate            # apply database migrations (ECS task)
    uv run python -m scripts.staging seed               # load the business records (ECS task)
    uv run python -m scripts.staging users              # create the demo users (idempotent)
    uv run python -m scripts.staging token sup-1        # print an access token for sup-1
    uv run python -m scripts.staging upload             # upload the generated PDFs via the API

No password is stored anywhere: `token` sets a fresh random password for the demo user and
signs in with the admin flow, which only an operator with IAM permissions can call.
"""

from __future__ import annotations

import argparse
import json
import secrets
import string
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import boto3
import httpx

if TYPE_CHECKING:
    from types_boto3_cognito_idp.client import CognitoIdentityProviderClient

ROOT = Path(__file__).resolve().parents[1]
TERRAFORM_DIR = ROOT / "infra" / "terraform"
GENERATED_DIR = ROOT / "data" / "generated"


@dataclass(frozen=True)
class DemoUser:
    username: str
    tenant: str
    role: str


# The same identities the local scenarios use: two supervisors to show separation of
# duties, and a second tenant to show isolation.
DEMO_USERS = (
    DemoUser("agent-1", "nsi-amer", "support_agent"),
    DemoUser("fin-1", "nsi-amer", "finance_analyst"),
    DemoUser("sup-1", "nsi-amer", "supervisor"),
    DemoUser("sup-2", "nsi-amer", "supervisor"),
    DemoUser("editor-1", "nsi-amer", "editor"),
    DemoUser("sup-9", "nsi-emea", "supervisor"),
    DemoUser("editor-9", "nsi-emea", "editor"),
)
EDITORS = {"nsi-amer": "editor-1", "nsi-emea": "editor-9"}


def terraform_outputs() -> dict[str, Any]:
    result = subprocess.run(  # noqa: S603 - fixed arguments, no shell
        ["terraform", f"-chdir={TERRAFORM_DIR}", "output", "-json"],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return {name: item["value"] for name, item in json.loads(result.stdout).items()}


def cognito() -> CognitoIdentityProviderClient:
    return boto3.session.Session().client("cognito-idp")


def run_ops_task(outputs: dict[str, Any], command: list[str]) -> None:
    """Run the ops image once in the private subnets and fail if the command fails."""
    if not outputs.get("ops_task_definition"):
        sys.exit("No ops task definition: apply Terraform with image_tag set first.")
    ecs = boto3.session.Session().client("ecs")
    started = ecs.run_task(
        cluster=outputs["ecs_cluster"],
        taskDefinition=outputs["ops_task_definition"],
        launchType="FARGATE",
        platformVersion="1.4.0",
        networkConfiguration={"awsvpcConfiguration": outputs["ops_network_configuration"]},
        overrides={"containerOverrides": [{"name": "ops", "command": command}]},
        startedBy="scripts.staging",
    )
    if started["failures"] or not started["tasks"]:
        sys.exit(f"The task did not start: {started['failures']}")
    task_arn = started["tasks"][0]["taskArn"]
    task_id = task_arn.rsplit("/", 1)[-1]
    print(f"{' '.join(command)}: task {task_id}, logs in /aws/ecs/{outputs['ecs_cluster']}/ops")
    ecs.get_waiter("tasks_stopped").wait(
        cluster=outputs["ecs_cluster"],
        tasks=[task_arn],
        WaiterConfig={"Delay": 6, "MaxAttempts": 100},
    )
    task = ecs.describe_tasks(cluster=outputs["ecs_cluster"], tasks=[task_arn])["tasks"][0]
    container = task["containers"][0]
    code = container.get("exitCode")
    if code != 0:
        sys.exit(f"Failed (exit {code}): {container.get('reason') or task.get('stoppedReason')}")
    print("done")


def random_password() -> str:
    """Meets the pool policy: 14+ characters with every character class."""
    alphabet = string.ascii_letters + string.digits + "!#%+-=?@^_"
    while True:
        password = "".join(secrets.choice(alphabet) for _ in range(32))
        if (
            any(c.islower() for c in password)
            and any(c.isupper() for c in password)
            and any(c.isdigit() for c in password)
            and any(not c.isalnum() for c in password)
        ):
            return password


def create_users(outputs: dict[str, Any]) -> None:
    client, pool = cognito(), outputs["cognito_user_pool_id"]
    for user in DEMO_USERS:
        try:
            client.admin_create_user(
                UserPoolId=pool,
                Username=user.username,
                UserAttributes=[{"Name": "custom:tenant_id", "Value": user.tenant}],
                MessageAction="SUPPRESS",  # no invitation e-mail: demo users have none
            )
            state = "created"
        except client.exceptions.UsernameExistsException:
            state = "exists"
        client.admin_add_user_to_group(UserPoolId=pool, Username=user.username, GroupName=user.role)
        print(f"{user.username:<10} {user.tenant:<9} {user.role:<16} {state}")


def access_token(outputs: dict[str, Any], username: str) -> str:
    client, pool = cognito(), outputs["cognito_user_pool_id"]
    password = random_password()
    client.admin_set_user_password(
        UserPoolId=pool, Username=username, Password=password, Permanent=True
    )
    result = client.admin_initiate_auth(
        UserPoolId=pool,
        ClientId=outputs["cognito_client_id"],
        AuthFlow="ADMIN_USER_PASSWORD_AUTH",
        AuthParameters={"USERNAME": username, "PASSWORD": password},
    )
    return result["AuthenticationResult"]["AccessToken"]


def upload_documents(outputs: dict[str, Any], *, wait: bool) -> None:
    manifest: list[dict[str, Any]] = json.loads((GENERATED_DIR / "manifest.json").read_text())
    tokens = {tenant: access_token(outputs, editor) for tenant, editor in EDITORS.items()}
    jobs: list[tuple[str, str]] = []
    with httpx.Client(base_url=outputs["api_url"].rstrip("/"), timeout=60) as http:
        for entry in manifest:
            path = GENERATED_DIR / entry["file"]
            response = http.post(
                "/v1/documents",
                headers={"Authorization": f"Bearer {tokens[entry['tenant_id']]}"},
                data={
                    "doc_key": entry["doc_id"],
                    "version": entry["version"],
                    "title": entry["title"],
                    "effective_date": entry["effective_date"],
                    "classification": entry["classification"],
                    "status": entry["status"],
                },
                files={"file": (path.name, path.read_bytes(), "application/pdf")},
            )
            response.raise_for_status()
            body = response.json()
            print(
                f"{entry['tenant_id']:<9} {entry['doc_id']} v{entry['version']}: {body['status']}"
            )
            if body.get("job_id"):
                jobs.append((entry["tenant_id"], body["job_id"]))
        if wait:
            for tenant, job_id in jobs:
                print(f"job {job_id}: {wait_for_job(http, tokens[tenant], job_id)}")


def wait_for_job(http: httpx.Client, token: str, job_id: str, timeout: float = 300) -> str:
    deadline = time.monotonic() + timeout
    while True:
        response = http.get(f"/v1/jobs/{job_id}", headers={"Authorization": f"Bearer {token}"})
        response.raise_for_status()
        state = str(response.json()["status"])
        if state in ("succeeded", "failed") or time.monotonic() > deadline:
            return state
        time.sleep(3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("migrate", help="apply the database migrations (one-off ECS task)")
    commands.add_parser("seed", help="load the business records (one-off ECS task)")
    commands.add_parser("users", help="create the demo users in Cognito (idempotent)")
    token = commands.add_parser("token", help="print an access token for a demo user")
    token.add_argument("username", choices=[user.username for user in DEMO_USERS])
    upload = commands.add_parser("upload", help="upload data/generated PDFs through the API")
    upload.add_argument("--no-wait", action="store_true", help="do not wait for ingestion")
    args = parser.parse_args()

    outputs = terraform_outputs()
    match args.command:
        case "migrate":
            run_ops_task(outputs, ["alembic", "upgrade", "head"])
        case "seed":
            run_ops_task(outputs, ["python", "-m", "scripts.seed_business_data"])
        case "users":
            create_users(outputs)
        case "token":
            sys.stdout.write(access_token(outputs, args.username) + "\n")
        case "upload":
            upload_documents(outputs, wait=not args.no_wait)


if __name__ == "__main__":
    main()
