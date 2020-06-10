## AWS RDS Sanitized Snapshots

Periodically take snapshots of RDS databases, sanitize them, and share with selected accounts.

Use this to automate your development and/or QA database creation, instead of forcing them to use a database that was
created last year and was kind of kept in shape by random acts of kindness. Developers and QA love real data and this
lets you create non-production databases with sanitized production data. Use the sanitization step to delete passwords,
remove credit card numbers, eliminate PII, etc.  

* Available for easy deployment on [AWS Serverless Application Repository](https://serverlessrepo.aws.amazon.com/applications/arn:aws:serverlessrepo:us-east-1:859319237877:applications~RDS-sanitized-snapshots).
* Or get CloudFormation template directly from [GitHub releases](https://github.com/CloudSnorkel/RDS-sanitized-snapshots/releases).

### Overview

![Architecture diagram](https://github.com/CloudSnorkel/RDS-sanitized-snapshots/raw/master/architecture.svg?sanitize=true)

This project supplies a CloudFormation template that setups a step function and a timer to execute this function. The
function will create a sanitized snapshot of a given database and share it with configured accounts. Those accounts can
then create new databases from those snapshots.

The step function does the following to create the snapshot:

 1. Get a snapshot of the given database by either:
    * Finding the latest snapshot for the given database
    * Creating and waiting for a new fresh snapshot
 1. Create a temporary database from the snapshot
 1. Wait for the database to be ready
 1. Reset the master password on the temporary database to a random password
 1. Wait for the password to be set
 1. Use a Fargate task to connect to the temporary database and run configured SQL statements to sanitize the data
 1. Take a snapshot of the temporary database
 1. Optionally share the snapshot with other accounts (if you have separate accounts for developers/QA)
 1. Delete temporary database and snapshot

### Deploy

RDS-sanitized-snapshots is contained in one CloudFormation template and has no external dependencies but the RDS
database itself. It is completely serverless, so you only ever pay for what you use.

You can download the CloudFormation template and deploy it yourself, or do it using [Serverless Application Repository](https://serverlessrepo.aws.amazon.com/applications/arn:aws:serverlessrepo:us-east-1:859319237877:applications~RDS-sanitized-snapshots).

#### Parameters

| Parameter | Description |
| --- | --- |
| Source database identifier | The id (not ARN) of the database you want to snapshot. |
| Use existing snapshot or create new one | Choose whether to create a new snapshot of the database, or to use the latest available snapshot. The latest available would usually be the automatic back-up so it might be a week old. |
| Snapshot schedule | [Cron expression](https://docs.aws.amazon.com/AmazonCloudWatch/latest/events/ScheduledEvents.html) describing when the job should run. |
| Sanitization SQL statements | SQL statement used to sanitize the temporary database. Use this to remove any data you don't want in the final snapshot, or the trim the data for size. You can separate multiple statements with a semicolon. |
| List of AWS accounts to share snapshot with | A comma-separated list of AWS accounts to share the final snapshot with. These accounts will see the snapshot under the "Shared with me" tab in the RDS console. |
| Snapshot name format | Final snapshot name format. A new snapshot will be created periodically, so this should contain the date to provide uniqueness. Make sure it follows the [naming rules of AWS](https://docs.aws.amazon.com/AmazonRDS/latest/UserGuide/CHAP_Limits.html). |
| Network | Network parameters are required to create the temporary database. Make sure to select at least two subnets that are associated with the selected VPC |

### Known Limitations

* The chosen VPC and subnet must have internet access for Fargate to be able to download the right Docker image used to
  connect to the temporary database.
* Encrypted snapshots are not supported yet.
* Database clusters are not supported yet.
* Only PostgreSQL, MySQL and MariaDB are supported for now.

### Troubleshooting

* Check the status of the state machine for the step function. Click on the failed step and check out the input, output
  and exception.
* Look for sanitization errors in CloudWatch log group `<MY STACK NAME>-SanitizerLogs-<RANDOM>`
