import datetime
import re
import secrets

import boto3

rds_client = boto3.client("rds")
res_client = boto3.client("resourcegroupstaggingapi")
states = {}


class NotReady(Exception):
    pass


def _tags(uid=None):
    tags = [
        {
            "Key": "RDS-sanitized-snapshots",
            "Value": "yes",
        },
    ]

    if uid:
        tags.append(
            {
                "Key": "RDS-sanitized-snapshots-temp",
                "Value": uid,
            },
        )

    return tags


def _check_status(s):
    if "stop" in s or "delet" in s or "fail" in s or "incompatible" in s or "inaccessible" in s or "error" in s:
        raise ValueError("Bad status `${s}`")


def state_function(name):
    def decorator(f):
        states[name] = f
        return f

    return decorator


@state_function("Initialize")
def initialize(state, uid):
    orig_db = rds_client.describe_db_instances(DBInstanceIdentifier=state["db_identifier"])["DBInstances"][0]
    state["engine"] = orig_db["Engine"]
    state["temporary_snapshot_id"] = state["db_identifier"][:55] + "-" + secrets.token_hex(5)
    state["temp_db_id"] = state["db_identifier"][:55] + "-" + secrets.token_hex(5)
    state["target_snapshot_id"] = state["db_identifier"][:55] + "-" + secrets.token_hex(5)
    tsid = state["target_snapshot_id"] = state["snapshot_format"].format(
        database_identifier=state["db_identifier"],
        date=datetime.datetime.now(),
    )

    if not re.match("[a-z][a-z0-9\\-]{1,62}", tsid, re.I) or "--" in tsid or tsid[-1] == "-":
        # https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Limits.html
        raise ValueError(f"Invalid snapshot id generated from format - {tsid}")


@state_function("FindLatestSnapshot")
def find_latest_snapshot(state, uid):
    db_identifier = state["db_identifier"]
    snapshots = []
    for page in rds_client.get_paginator("describe_db_snapshots").paginate(DBInstanceIdentifier=db_identifier):
        for snapshot in page["DBSnapshots"]:
            snapshots.append((snapshot["SnapshotCreateTime"], snapshot))

    snapshots.sort()
    state["snapshot_id"] = snapshots[-1][1]["DBSnapshotIdentifier"]


@state_function("TakeSnapshot")
def take_snapshot(state, uid):
    state["snapshot_id"] = state["temp_snapshot_id"]

    rds_client.create_db_snapshot(
        DBInstanceIdentifier=state["db_identifier"],
        DBSnapshotIdentifier=state["snapshot_id"],
        Tags=_tags(uid),
    )


@state_function("TakeFinalSnapshot")
def take_final_snapshot(state, uid):
    state["snapshot_id"] = state["target_snapshot_id"]

    rds_client.create_db_snapshot(
        DBInstanceIdentifier=state["temp_db_id"],
        DBSnapshotIdentifier=state["snapshot_id"],
        Tags=_tags(),
    )


@state_function("WaitForSnapshot")
@state_function("WaitForFinalSnapshot")
def wait_for_snapshot(state, uid):
    snapshot = rds_client.describe_db_snapshots(DBSnapshotIdentifier=state["snapshot_id"])["DBSnapshots"][0]
    status = snapshot["Status"]
    if status == "available":
        return
    _check_status(status)
    raise NotReady()


@state_function("CreateTempDatabase")
def create_temp_database(state, uid):
    rds_client.restore_db_instance_from_db_snapshot(
        DBInstanceIdentifier=state["temp_db_id"],
        DBSnapshotIdentifier=state["snapshot_id"],
        PubliclyAccessible=False,
        AutoMinorVersionUpgrade=False,
        VpcSecurityGroupIds=[
            state["security_group_id"],
        ],
        DBSubnetGroupName=state["subnet_group_id"],
        Tags=_tags(uid),
    )


@state_function("WaitForTempDatabase")
@state_function("WaitForPassword")
def wait_for_temp_database(state, uid):
    db = rds_client.describe_db_instances(DBInstanceIdentifier=state["temp_db_id"])["DBInstances"][0]
    status = db["DBInstanceStatus"]
    if status == "available" and not db["PendingModifiedValues"]:
        return
    _check_status(status)
    raise NotReady()


@state_function("SetTempPassword")
def set_temp_password(state, uid):
    db = rds_client.describe_db_instances(DBInstanceIdentifier=state["temp_db_id"])["DBInstances"][0]
    state["db"] = {
        "host": db["Endpoint"]["Address"],
        "port": str(db["Endpoint"]["Port"]),
        "user": db["MasterUsername"],
        "password": secrets.token_hex(32),
        "database": db["DBName"],
    }

    rds_client.modify_db_instance(
        DBInstanceIdentifier=state["temp_db_id"],
        ApplyImmediately=True,
        BackupRetentionPeriod=0,
        MasterUserPassword=state["db"]["password"],
    )


@state_function("ShareSnapshot")
def share_snapshot(state, uid):
    if state["shared_accounts"]:
        rds_client.modify_db_snapshot_attribute(
            DBSnapshotIdentifier=state["snapshot_id"],
            AttributeName="restore",
            ValuesToAdd=state["shared_accounts"]
        )


def _ids(t, i):
    return [x["ResourceARN"].split(":")[-1] for x in res_client.get_resources(
        ResourceTypeFilters=[t],
        TagFilters=[
            {
                "Key": "RDS-sanitized-snapshots-temp",
                "Values": [i]
            }
        ]
    )["ResourceTagMappingList"]]


@state_function("Cleanup")
@state_function("ErrorCleanup")
def cleanup(state, uid):
    # we have to manually look for snapshots/dbs because error state doesn't pass parameters

    for db in _ids("rds:db", uid):
        print(f"Deleting temporary database {db}")
        rds_client.delete_db_instance(
            DBInstanceIdentifier=db,
            SkipFinalSnapshot=True,
            DeleteAutomatedBackups=True,
        )

    for sid in _ids("rds:snapshot", uid):
        print(f"Deleting temporary snapshot {sid}")
        rds_client.delete_db_snapshot(
            DBSnapshotIdentifier=sid,
        )


def handler(event, context):
    print("event:", event)

    state_name = event["state_name"]
    state = event["state"]

    states[state_name](state, event["uid"])

    return state
