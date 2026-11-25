#!/usr/bin/env bash
# Opens the Samawi Bot dashboard at http://localhost:3200/samawi through an ECS Exec port-forward.
# The dashboard has no inbound network access at all; this tunnel (which needs your AWS
# credentials) is the only way in, and sign-in still needs the emailed link.
# Requires: AWS CLI + Session Manager plugin (brew install --cask session-manager-plugin).
# Leave it running while you use the dashboard; Ctrl-C closes it.
set -euo pipefail
CLUSTER=samawi
SERVICE=samawi-dashboard
PORT="${1:-3200}"
export AWS_REGION="${AWS_REGION:-ap-southeast-3}"

TASK=$(aws ecs list-tasks --cluster "$CLUSTER" --service-name "$SERVICE" --desired-status RUNNING --query 'taskArns[0]' --output text)
[ "$TASK" != "None" ] || { echo "No running dashboard task. Check: aws ecs describe-services --cluster $CLUSTER --services $SERVICE" >&2; exit 1; }
RUNTIME=$(aws ecs describe-tasks --cluster "$CLUSTER" --tasks "$TASK" --query 'tasks[0].containers[?name==`dashboard`].runtimeId | [0]' --output text)

echo "Dashboard: http://localhost:$PORT/samawi   (Ctrl-C to close)"
exec aws ssm start-session \
  --target "ecs:${CLUSTER}_${TASK##*/}_${RUNTIME}" \
  --document-name AWS-StartPortForwardingSession \
  --parameters "portNumber=3200,localPortNumber=$PORT"
