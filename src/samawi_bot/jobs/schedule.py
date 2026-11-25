"""One-off EventBridge schedules for each slot's publish (spec §8.1: prayer-anchored publish).

`produce` creates `at(...)` schedules for the day's slots in the `samawi` schedule group; each runs
the ECS task with `run publish --plan-id N` and deletes itself afterwards. The hourly `publish-due`
sweep is the safety net, and publishing is idempotent, so a double fire is harmless.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from sqlalchemy import select

from ..app import App
from ..db import session_scope
from ..db.models import ContentPlan
from ..timeutil import utc


def schedule_day(app: App, day: date) -> list[str]:
    st = app.settings
    if app.scheduler is None or not st.ecs_cluster_arn:
        return []
    with session_scope(app.db) as s:
        plans = [(p.id, p.slot, p.scheduled_at) for p in s.scalars(select(ContentPlan).where(ContentPlan.date == day))]
    made = []
    for pid, slot, at in plans:
        if at is None or utc(at) <= app.now():
            continue
        name = f"samawi-{day:%Y%m%d}-{slot}".replace("_", "-")
        target: dict[str, Any] = {
            "Arn": st.ecs_cluster_arn,
            "RoleArn": st.scheduler_role_arn,
            "Input": json.dumps({"containerOverrides": [{"name": "pipeline", "command": ["run", "publish", "--plan-id", str(pid)]}]}),
            "EcsParameters": {
                "TaskDefinitionArn": st.ecs_task_definition_arn,
                "LaunchType": "FARGATE",
                "NetworkConfiguration": {
                    "awsvpcConfiguration": {
                        "Subnets": [x for x in st.ecs_subnets.split(",") if x],
                        "SecurityGroups": [x for x in st.ecs_security_groups.split(",") if x],
                        "AssignPublicIp": "ENABLED",
                    }
                },
                "PropagateTags": "TASK_DEFINITION",
            },
            "RetryPolicy": {"MaximumRetryAttempts": 2, "MaximumEventAgeInSeconds": 900},
        }
        args = {
            "Name": name,
            "GroupName": st.scheduler_group,
            "ScheduleExpression": f"at({utc(at):%Y-%m-%dT%H:%M:%S})",
            "ScheduleExpressionTimezone": "UTC",
            "FlexibleTimeWindow": {"Mode": "OFF"},
            "ActionAfterCompletion": "DELETE",
            "Target": target,
        }
        try:
            app.scheduler.create_schedule(**args)
        except app.scheduler.exceptions.ConflictException:
            app.scheduler.update_schedule(**{k: v for k, v in args.items() if k != "ActionAfterCompletion"}, ActionAfterCompletion="DELETE")
        made.append(name)
    return made
