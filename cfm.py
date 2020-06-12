import json
import typing

import python_minifier
import troposphere.awslambda
import troposphere.ec2
import troposphere.ecs
import troposphere.events
import troposphere.logs
import troposphere.rds
import troposphere.stepfunctions

from iam import add_lambda_role, HANDLER_POLICIES, add_fargate_task_execution_role, add_state_machine_role, \
    add_cloudwatch_role


def add_parameter(template: troposphere.Template, title: str, label: str, group: str, **kwargs):
    p = troposphere.Parameter(
        title,
        template=template,
        **kwargs
    )
    template.set_parameter_label(p, label)
    template.add_parameter_to_group(p, group)
    return p


def add_lambda(template: troposphere.Template, name: str, code: str, policies, **kwargs):
    func = troposphere.awslambda.Function(f"{name}Function", template, **kwargs)
    func.Runtime = "python3.7"
    func.Code = troposphere.awslambda.Code(ZipFile=code)
    func.Handler = "index.handler"
    func.Role = add_lambda_role(template, name, policies).get_att("Arn")

    return func


def add_state_machine_handler(template: troposphere.Template):
    # minify so it fits in 4096 bytes and we don't need to upload to S3 first
    code = python_minifier.minify(
        open("lambda/rds.py").read(),
        "lambda/rds.py",
        rename_globals=True,
        preserve_globals=["handler", "NotReady"]
    )

    return add_lambda(
        template,
        "Handler",
        code,
        HANDLER_POLICIES,
        Timeout=30,
    )


def add_fargate_cluster(template: troposphere.Template) -> troposphere.ecs.Cluster:
    security_group_id = troposphere.ec2.SecurityGroup(
        "SecurityGroup", template,
        GroupDescription="Group for communication between sanitizing job and database",
        VpcId=troposphere.Ref("VpcId"),
        Tags=[
            {
                "Key": "Name",
                "Value": "RDS-sanitized-snapshots"
            },
        ],
    ).ref()
    troposphere.ec2.SecurityGroupIngress(
        "SecurityGroupRule", template,
        GroupId=security_group_id,
        SourceSecurityGroupId=security_group_id,
        IpProtocol="tcp",
        FromPort=0,
        ToPort=65535,
    )

    return troposphere.ecs.Cluster(
        "FargateCluster", template,
    )


def add_fargate_task_definition(template: troposphere.Template) -> typing.Dict[str, troposphere.ecs.TaskDefinition]:
    log_group = troposphere.logs.LogGroup("SanitizerLogs", template)
    role = add_fargate_task_execution_role(template)

    return {
        engine: troposphere.ecs.TaskDefinition(
            f"{engine.title()}SanitizerTask", template,
            ContainerDefinitions=[
                troposphere.ecs.ContainerDefinition(
                    Name="sql",
                    Image=engine,
                    Command=cmd,
                    LogConfiguration=troposphere.ecs.LogConfiguration(
                        LogDriver="awslogs",
                        Options={
                            "awslogs-group": log_group.ref(),
                            "awslogs-region": troposphere.Region,
                            "awslogs-stream-prefix": "sql",
                        }
                    )
                ),
            ],
            Cpu="512",
            Memory="1GB",
            NetworkMode="awsvpc",
            RequiresCompatibilities=["FARGATE"],
            ExecutionRoleArn=role.get_att("Arn"),
        )
        for engine, cmd in {
            "postgres": ["psql", "-c", troposphere.Ref("SanitizeSQL")],
            "mysql": ["mysql", "-e", troposphere.Ref("SanitizeSQL")],
            "mariadb": ["mysql", "-e", troposphere.Ref("SanitizeSQL")],
        }.items()
    }


def add_state_machine(template, function: troposphere.awslambda.Function, cluster: troposphere.ecs.Cluster,
                      tasks: typing.Dict[str, troposphere.ecs.TaskDefinition]):
    fn_arn_sub = "${" + function.title + ".Arn}"
    cluster_arn_sub = "${" + cluster.title + "}"

    states = {
        "Success": {
            "Type": "Succeed",
        },
        "Failure": {
            "Type": "Fail",
        }
    }

    def add_state(state_name: str, next_state_name: str, catch_state: str = None, retry=None):
        states[state_name] = {
            "Type": "Task",
            "Resource": fn_arn_sub,
            "Parameters": {
                "uid.$": "$$.Execution.Id",
                "state_name": state_name,
                "state.$": "$",
            },
            "Next": next_state_name,
        }

        if catch_state:
            states[state_name]["Catch"] = [
                {
                    "ErrorEquals": ["States.ALL"],
                    "Next": catch_state,
                }
            ]

        if retry:
            states[state_name]["Retry"] = [retry]

    def add_waiting_state(state_name: str, next_state_name: str, catch_state: str = None):
        add_state(state_name, next_state_name, catch_state=catch_state, retry={
            "ErrorEquals": ["NotReady"],
            "IntervalSeconds": 60,
            "MaxAttempts": 300,  # 5 hours max wait time
            "BackoffRate": 1,
        })

    def add_task_state(state_name: str, task_arn: str, next_state_name: str, catch_state: str = None):
        states[state_name] = {
            "Type": "Task",
            "Resource": "arn:aws:states:::ecs:runTask.sync",
            "OutputPath": "$",
            "ResultPath": "$.SanitizeResult",
            "Parameters": {
                "TaskDefinition": task_arn,
                "Cluster": cluster_arn_sub,
                "LaunchType": "FARGATE",
                "NetworkConfiguration": {
                    "AwsvpcConfiguration": {
                        "AssignPublicIp": "ENABLED",  # TODO what about subnets with no public facing IP?
                        "SecurityGroups": [
                            "${SecurityGroup}",
                        ],
                        "Subnets": [
                            "${SubnetIdsJoined}",
                        ],
                    }
                },
                "Overrides": {
                    "ContainerOverrides": [{
                        "Name": "sql",
                        "Environment": [
                            {
                                "Name": "PGHOST",
                                "Value.$": "$.db.host",
                            },
                            {
                                "Name": "PGPORT",
                                "Value.$": "$.db.port",
                            },
                            {
                                "Name": "PGUSER",
                                "Value.$": "$.db.user",
                            },
                            {
                                "Name": "PGPASSWORD",
                                "Value.$": "$.db.password",
                            },
                            {
                                "Name": "PGDATABASE",
                                "Value.$": "$.db.database",
                            },
                            {
                                "Name": "PGCONNECT_TIMEOUT",
                                "Value": "30",
                            },
                            {
                                "Name": "MYSQL_HOST",
                                "Value.$": "$.db.host",
                            },
                            {
                                "Name": "MYSQL_PORT",
                                "Value.$": "$.db.port",
                            },
                            {
                                "Name": "MYSQL_USER",
                                "Value.$": "$.db.user",
                            },
                            {
                                "Name": "MYSQL_PASSWORD",
                                "Value.$": "$.db.password",
                            },
                            {
                                "Name": "MYSQL_DATABASE",
                                "Value.$": "$.db.database",
                            },
                        ]
                    }],
                }
            },
            "Next": next_state_name,
        }

        if catch_state:
            states[state_name]["Catch"] = [
                {
                    "ErrorEquals": ["States.ALL"],
                    "Next": catch_state,
                }
            ]

    add_state("Initialize", "ChooseSnapshot", catch_state="ErrorCleanup")
    del states["Initialize"]["Parameters"]["state.$"]
    states["Initialize"]["Parameters"]["state"] = {
        "db_identifier": "${Db}",
        "vpc_id": "${VpcId}",
        "subnet_group_id": "${SubnetGroup}",
        "security_group_id": "${SecurityGroup}",
        "new_snapshot": "${NewSnapshot}",
        "shared_accounts": ["${ShareAccountsJoined}"],
        "snapshot_format": "${SnapshotFormat}",
        "kms": "${KMS}",
    }

    # TODO create add_choice()
    states["ChooseSnapshot"] = {
        "Type": "Choice",
        "Choices": [
            {
                "Variable": "$.new_snapshot",
                "StringEquals": "Take new snapshot",
                "Next": "TakeSnapshot",
            },
            {
                "Variable": "$.new_snapshot",
                "StringEquals": "Use latest existing snapshot",
                "Next": "FindLatestSnapshot",
            },
        ],
        "Default": "ErrorCleanup",
    }

    add_state("TakeSnapshot", "WaitForSnapshot", catch_state="ErrorCleanup")
    add_waiting_state("WaitForSnapshot", "ShouldEncrypt", catch_state="ErrorCleanup")
    add_state("FindLatestSnapshot", "ShouldEncrypt", catch_state="ErrorCleanup")

    states["ShouldEncrypt"] = {
        "Type": "Choice",
        "Choices": [
            {
                "Variable": "$.kms",
                "StringEquals": "",
                "Next": "CreateTempDatabase",
            },
        ],
        "Default": "Encrypt",
    }

    add_state("Encrypt", "WaitForEncrypt", catch_state="ErrorCleanup")
    add_waiting_state("WaitForEncrypt", "CreateTempDatabase", catch_state="ErrorCleanup")

    add_state("CreateTempDatabase", "WaitForTempDatabase", catch_state="ErrorCleanup")
    add_waiting_state("WaitForTempDatabase", "SetTempPassword", catch_state="ErrorCleanup")
    add_state("SetTempPassword", "WaitForPassword", catch_state="ErrorCleanup")
    add_waiting_state("WaitForPassword", "ChooseSanitizer", catch_state="ErrorCleanup")

    states["ChooseSanitizer"] = {
        "Type": "Choice",
        "Choices": [
            {
                "Variable": "$.engine",
                "StringEquals": "postgres",
                "Next": "SanitizePostgres",
            },
            {
                "Variable": "$.engine",
                "StringEquals": "mysql",
                "Next": "SanitizeMySQL",
            },
            {
                "Variable": "$.engine",
                "StringEquals": "mariadb",
                "Next": "SanitizeMariaDB",
            },
        ],
        "Default": "ErrorCleanup",
    }

    for engine in ["Postgres", "MySQL", "MariaDB"]:
        add_task_state(f"Sanitize{engine}", "${" + tasks[engine.lower()].title + "}", "TakeFinalSnapshot",
                       catch_state="ErrorCleanup")

    add_state("TakeFinalSnapshot", "WaitForFinalSnapshot", catch_state="ErrorCleanup")
    add_waiting_state("WaitForFinalSnapshot", "ShareSnapshot", catch_state="ErrorCleanup")

    add_state("ShareSnapshot", "Cleanup", catch_state="ErrorCleanup")

    add_state("Cleanup", "Success", retry={
        "ErrorEquals": ["States.ALL"],
        "IntervalSeconds": 120,
        "MaxAttempts": 10,
    })

    add_state("ErrorCleanup", "Failure", retry={
        "ErrorEquals": ["States.ALL"],
        "IntervalSeconds": 120,
        "MaxAttempts": 10,
    })

    state_machine = troposphere.stepfunctions.StateMachine(
        "SnapshotSanitizeAndCopy", template,
        DefinitionString=troposphere.Sub(
            json.dumps(
                {
                    "StartAt": "Initialize",
                    "States": states
                },
                indent=2
            ),
            {
                "SubnetIdsJoined": troposphere.Join('", "', troposphere.Ref("SubnetIds")),
                "ShareAccountsJoined": troposphere.Join('", "', troposphere.Ref("ShareAccounts")),
            }
        ),
        RoleArn=add_state_machine_role(template, function, tasks.values()).get_att("Arn"),
    )

    return state_machine


def add_schedule(template: troposphere.Template, state_machine: troposphere.stepfunctions.StateMachine):
    troposphere.events.Rule(
        "ScheduleRule", template,
        Description="RDS-sanitized-snapshots schedule",
        ScheduleExpression=troposphere.Ref("Schedule"),
        Targets=[
            troposphere.events.Target(
                Id="SnapshotAndSanitize",
                Arn=state_machine.ref(),
                RoleArn=add_cloudwatch_role(template, state_machine).get_att("Arn"),
            ),
        ],
    )


def generate_main_template():
    template = troposphere.Template("Sanitize and copy latest RDS snapshot to a different account")

    add_parameter(template, "Db", "Source database identifier", "Database", Type="String")
    add_parameter(template, "NewSnapshot", "Use existing snapshot or create new one", "Database", Type="String",
                  AllowedValues=["Take new snapshot", "Use latest existing snapshot"],
                  Default="Use latest existing snapshot")
    # TODO List<String> and run in parallel?
    add_parameter(template, "Schedule", "Snapshot schedule", "Options", Type="String", Default="rate(7 days)")
    add_parameter(template, "SanitizeSQL", "Sanitization SQL statements", "Options", Type="String")
    add_parameter(template, "ShareAccounts", "List of AWS accounts to share snapshot with (leave empty to not share)",
                  "Options", Type="List<String>", Default="")
    add_parameter(template, "SnapshotFormat", "Snapshot name format using Python .format() function",
                  "Options", Type="String", Default="{database_identifier:.42}-sanitized-{date:%Y-%m-%d}")
    add_parameter(template, "KMS", "KMS key id to re-encrypt snapshots (leave empty to not encrypt)",
                  "Options", Type="String", Default="")
    add_parameter(template, "VpcId", "VPC for temporary database", "Network", Type="AWS::EC2::VPC::Id")
    add_parameter(template, "SubnetIds", "Subnets for temporary database (at least two)", "Network",
                  Type="List<AWS::EC2::Subnet::Id>")

    template.add_condition(
        "KmsEmpty",
        troposphere.Equals(
            troposphere.Ref("KMS"),
            ""
        )
    )

    troposphere.rds.DBSubnetGroup(
        "SubnetGroup", template,
        DBSubnetGroupDescription="Temporary database used for RDS-sanitize-snapshots",
        SubnetIds=troposphere.Ref("SubnetIds"),
    )

    function = add_state_machine_handler(template)
    cluster = add_fargate_cluster(template)
    tasks = add_fargate_task_definition(template)
    # TODO try to remove public ip with 137112412989.dkr.ecr.us-east-1.amazonaws.com/amazonlinux:latest
    # TODO still requires VPC Endpoint...
    # https://docs.aws.amazon.com/AmazonECR/latest/userguide/vpc-endpoints.html
    # https://docs.aws.amazon.com/AWSCloudFormation/latest/UserGuide/aws-resource-ec2-vpcendpoint.html
    # task = add_task(
    #     template,
    #     "137112412989.dkr.ecr.us-east-1.amazonaws.com/amazonlinux:latest",
    #     [
    #         "sh", "-c",
    #         "yum", "install", "-y", "postgresql", "&&"
    #         "psql", "-c", troposphere.Ref("SanitizeSQL")
    #     ]
    # )
    state_machine = add_state_machine(template, function, cluster, tasks)
    add_schedule(template, state_machine)

    return template.to_yaml(clean_up=True, long_form=True)
