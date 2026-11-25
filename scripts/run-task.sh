#!/usr/bin/env bash
# Run one job in AWS now (or `migrate`), e.g.:  scripts/run-task.sh refresh-sources   |   scripts/run-task.sh produce --day 2026-11-27
# Prints the task ARN; logs land in /samawi-bot/pipeline (stream prefix "job").
set -euo pipefail
export AWS_REGION="${AWS_REGION:-ap-southeast-3}"
cd "$(dirname "$0")/../infra/terraform"
SUBNETS=$(terraform output -json subnet_ids | python3 -c 'import json,sys;print(",".join(json.load(sys.stdin)))')
SG=$(terraform output -raw task_security_group)
# "migrate" is a top-level command; everything else is a job under "run".
CMD=$(python3 -c 'import json,sys;a=sys.argv[1:];print(json.dumps(a if a[0] in ("migrate",) else ["run", *a]))' "$@")
aws ecs run-task --cluster samawi --task-definition samawi-pipeline --launch-type FARGATE \
  --network-configuration "awsvpcConfiguration={subnets=[$SUBNETS],securityGroups=[$SG],assignPublicIp=ENABLED}" \
  --overrides "{\"containerOverrides\":[{\"name\":\"pipeline\",\"command\":$CMD}]}" \
  --query 'tasks[0].taskArn' --output text
