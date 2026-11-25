#!/usr/bin/env bash
# Build the ARM64 image, push it to ECR, and roll the dashboard. Jobs pick up :latest on their next run.
# This Mac builds with Colima (`colima start`).
set -euo pipefail
export AWS_REGION="${AWS_REGION:-ap-southeast-3}"
REPO=249746593249.dkr.ecr.ap-southeast-3.amazonaws.com/samawi-bot
TAG=$(git rev-parse --short HEAD 2>/dev/null || date +%Y%m%d%H%M)
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO%%/*}" >/dev/null
docker buildx build --platform linux/arm64 -t "$REPO:latest" -t "$REPO:$TAG" --push "$(dirname "$0")/.."
aws ecs update-service --cluster samawi --service samawi-dashboard --force-new-deployment --query 'service.serviceName' --output text
echo "Pushed $REPO:$TAG"
