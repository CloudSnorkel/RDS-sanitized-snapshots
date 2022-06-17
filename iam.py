import typing

import troposphere.awslambda
import troposphere.ecs
import troposphere.iam
import troposphere.stepfunctions

HANDLER_POLICIES = [
    troposphere.iam.Policy(
        PolicyName="RDS",
        PolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Sid": "OriginalDB",
                    "Effect": "Allow",
                    "Action": [
                        troposphere.If("KmsEmpty", troposphere.NoValue, "rds:CopyDBSnapshot"),
                        "rds:DescribeDBInstances",
                        "rds:DescribeDBSnapshots",
                        "rds:CreateDBSnapshot",
                        "rds:AddTagsToResource",
                    ],
                    "Resource": [
                        troposphere.Sub(
                            "arn:${AWS::Partition}:rds:${AWS::Region}:${AWS::AccountId}:db:${Db}"),
                        # TODO only allow access to all snapshots when using latest snapshot option
                        troposphere.Sub(
                            "arn:${AWS::Partition}:rds:${AWS::Region}:${AWS::AccountId}:snapshot:*"),
                    ]
                },
                {
                    "Sid": "Snapshot",
                    "Effect": "Allow",
                    "Action": "rds:RestoreDBInstanceFromDBSnapshot",
                    "Resource": "*",
                    "Condition": {
                        "StringEquals": {
                            "rds:req-tag/RDS-sanitized-snapshots": "yes",
                        }
                    }
                },
                {
                    "Sid": "TempDB",
                    "Effect": "Allow",
                    "Action": [
                        "rds:CreateDBInstance",
                        "rds:DeleteDBInstance",
                        "rds:DescribeDBInstances",
                        "rds:ModifyDBInstance",
                        "rds:CreateDBSnapshot",
                        "rds:DeleteDBSnapshot",
                        "rds:ModifyDBSnapshotAttribute",
                    ],
                    "Resource": "*",
                    "Condition": {
                        "ForAllValues:StringEquals": {
                            "aws:TagKeys": [
                                "RDS-sanitized-snapshots"
                            ]
                        }
                    }
                },
                {
                    "Sid": "Cleanup",
                    "Effect": "Allow",
                    "Action": "tag:GetResources",
                    "Resource": "*",
                    "Condition": {
                        "ForAllValues:StringEquals": {
                            "aws:TagKeys": [
                                "RDS-sanitized-snapshots-temp",
                            ]
                        }
                    }
                },
                troposphere.If(
                    "KmsEmpty",
                    troposphere.NoValue,
                    {
                        "Sid": "Copy",
                        "Effect": "Allow",
                        "Action": [
                            "kms:CreateGrant",
                            "kms:DescribeKey",
                        ],
                        "Resource": troposphere.Ref("KMS"),
                    }
                ),
            ]
        }
    )
]


def add_lambda_role(template: troposphere.Template, name: str, policies: typing.Iterable[troposphere.iam.Policy]):
    role = troposphere.iam.Role(f"{name}Role", template)
    role.AssumeRolePolicyDocument = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {
                    "Service": [
                        "lambda.amazonaws.com"
                    ]
                },
                "Action": [
                    "sts:AssumeRole"
                ]
            }
        ],
    }
    role.ManagedPolicyArns = ["arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"]
    role.Policies = policies
    return role


def add_fargate_task_execution_role(template: troposphere.Template):
    return troposphere.iam.Role(
        "FargateServiceRole", template,
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": [
                            "ecs-tasks.amazonaws.com"
                        ]
                    },
                    "Action": [
                        "sts:AssumeRole"
                    ]
                }
            ],
        },
        ManagedPolicyArns=[
            "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
        ]
    )


def add_state_machine_role(template: troposphere.Template,
                           function: troposphere.awslambda.Function,
                           tasks: typing.Iterable[troposphere.ecs.TaskDefinition]) -> troposphere.iam.Role:
    role = troposphere.iam.Role(
        "StateMachineRole", template,
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": [
                            "states.amazonaws.com"
                        ]
                    },
                    "Action": [
                        "sts:AssumeRole"
                    ]
                }
            ],
        },
        Policies=[
            troposphere.iam.Policy(
                PolicyName="CallHandlers",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "lambda:InvokeFunction",
                            "Resource": function.get_att("Arn"),
                        },
                    ]
                }
            ),
            troposphere.iam.Policy(
                PolicyName="SyncFargate",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ecs:RunTask"
                            ],
                            "Resource": [
                                task.ref() for task in tasks
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "ecs:StopTask",
                                "ecs:DescribeTasks"
                            ],
                            "Resource": "*"
                        },
                        {
                            "Effect": "Allow",
                            "Action": [
                                "events:PutTargets",
                                "events:PutRule",
                                "events:DescribeRule"
                            ],
                            "Resource": [
                                troposphere.Sub(
                                    "arn:${AWS::Partition}:events:${AWS::Region}:${AWS::AccountId}:rule/StepFunctionsGetEventsForECSTaskRule")
                            ]
                        },
                        {
                            "Effect": "Allow",
                            "Action": "iam:PassRole",
                            "Resource": troposphere.GetAtt("FargateServiceRole", "Arn"),
                            "Condition": {
                                "StringEquals": {
                                    "iam:PassedToService": [
                                        "ecs-tasks.amazonaws.com"
                                    ]
                                }
                            }
                        }
                    ]
                }
            )
        ]
    )

    return role


def add_cloudwatch_role(template: troposphere.Template, state_machine: troposphere.stepfunctions.StateMachine):
    return troposphere.iam.Role(
        "CloudWatchRole", template,
        AssumeRolePolicyDocument={
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Service": [
                            "events.amazonaws.com"
                        ]
                    },
                    "Action": [
                        "sts:AssumeRole"
                    ]
                }
            ],
        },
        Policies=[
            troposphere.iam.Policy(
                PolicyName="StepFunction",
                PolicyDocument={
                    "Version": "2012-10-17",
                    "Statement": [
                        {
                            "Effect": "Allow",
                            "Action": "states:StartExecution",
                            "Resource": state_machine.ref(),
                        },
                    ]
                }
            )
        ]
    )
