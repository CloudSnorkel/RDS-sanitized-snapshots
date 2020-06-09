import boto3
import botocore.exceptions
import click
import troposphere.stepfunctions
import yaml

from cfm import generate_main_template


def _stack_exists(cf, name):
    try:
        cf.describe_stacks(StackName=name)
        return True
    except botocore.exceptions.ClientError as e:
        if e.response['Error']['Code'] == 'ValidationError':
            return False
        raise


@click.group()
def cli():
    pass


@click.command()
@click.option("--output", default="dist/RDS-sanitized-snapshots.yml", help="Template output file", type=click.File("w"))
def gen(output):
    output.write(generate_main_template())


def validate_subnets(ctx, param, value):
    if len(value) < 2:
        raise click.BadParameter("At least two subnets are required")
    return value


@click.command()
@click.option("--profile", help="AWS profile")
@click.option("--stack-name", help="CloudFormation stack name", default="RDS-sanitized-snapshots", show_default=True)
@click.option("--database", help="RDS database identifier to snapshot", required=True)
@click.option("--vpc", help="VPC id where temporary database will be created", required=True)
@click.option("--subnet", help="Subnet id where temporary database will be created", multiple=True,
              callback=validate_subnets)
@click.option("--sql", help="Sanitizing SQL statement", default="")
@click.option("--share-account", help="AWS account identifiers to share snapshots with", multiple=True)
@click.option("--new-snapshot", help="Take a new snapshot instead of using the latest available", is_flag=True)
@click.option("--snapshot_format", help="Snapshot name snapshot_format")
def deploy(profile, stack_name, database, vpc, subnet, sql, share_account, new_snapshot, snapshot_format):
    # this is more for testing and not really for user consumption...
    stack_template = generate_main_template()

    if profile:
        session = boto3.Session(profile_name=profile)
    else:
        session = boto3.Session()

    cf = session.client("cloudformation")

    parameters = [
        {
            "ParameterKey": "Db",
            "ParameterValue": database,
        },
        {
            "ParameterKey": "VpcId",
            "ParameterValue": vpc,
        },
        {
            "ParameterKey": "SubnetIds",
            "ParameterValue": ",".join(subnet),
        },
        {
            "ParameterKey": "NewSnapshot",
            "ParameterValue": "Take new snapshot" if new_snapshot else "Use latest existing snapshot",
        },
        {
            "ParameterKey": "SanitizeSQL",
            "ParameterValue": sql,
        },
        {
            "ParameterKey": "ShareAccounts",
            "ParameterValue": ",".join(share_account),
        }
    ]

    if snapshot_format:
        parameters.append({
            {
                "ParameterKey": "SnapshotFormat",
                "ParameterValue": snapshot_format,
            }
        })

    if _stack_exists(cf, stack_name):
        click.echo("Updating stack")
        try:
            cf.update_stack(
                StackName=stack_name,
                TemplateBody=stack_template,
                Capabilities=["CAPABILITY_IAM"],
                Parameters=parameters,
            )
        except botocore.exceptions.ClientError as e:
            if e.response['Error']['Code'] == 'ValidationError' \
                    and e.response['Error']['Message'] == 'No updates are to be performed.':
                print("Stack already up-to-date")
                return
            raise
        waiter = "stack_update_complete"
    else:
        click.echo("Creating stack")
        cf.create_stack(
            StackName=stack_name,
            TemplateBody=stack_template,
            Capabilities=["CAPABILITY_IAM"],
            Parameters=parameters,
        )
        waiter = "stack_create_complete"

    click.echo("Waiting for stack %s..." % (stack_name,))
    cf.get_waiter(waiter).wait(StackName=stack_name)


cli.add_command(gen)
cli.add_command(deploy)

if __name__ == "__main__":
    def literal_unicode_representer(dumper, data):
        return dumper.represent_scalar(u'tag:yaml.org,2002:str', data, style='|')


    yaml.add_representer(troposphere.Sub, literal_unicode_representer)

    cli()
