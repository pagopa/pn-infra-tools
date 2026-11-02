# Lambda Runtime Audit

Audit AWS Lambda functions across AWS accounts and regions, and generate an
Excel report for runtimes below the configured minimum. By default, the audit
checks Node.js runtimes older than version 22. Python runtimes can optionally
be checked as well.

The report includes the function runtime, account and region, CloudFormation
stack and logical ID, `Microservice` tag, last-modified timestamp, and function
ARN. It also contains summary counts by account/region, counts by runtime, and
the audit parameters.

## Requirements

- Python 3
- AWS CLI profiles with credentials and access to the target accounts
- AWS permissions to call `sts:GetCallerIdentity`, `lambda:ListFunctions`, and
  `lambda:ListTags`

Install the Python dependencies:

```sh
python3 -m pip install boto3 openpyxl
```

For AWS IAM Identity Center (SSO) profiles, sign in before running the audit:

```sh
aws sso login --profile sso_pn-core-dev
```

## Usage

Run from this directory:

```sh
python3 lambda_runtime_audit.py
```

By default, the script audits its configured PN profiles and uses the region
configured for each profile. The generated workbook is saved in `output/` with
a timestamped filename.

Examples:

```sh
# Check Node.js runtimes older than version 22 and Python runtimes older than 3.12
python3 lambda_runtime_audit.py --nodejs-min 22 --check-python --python-min 3.12

# Audit selected profiles and regions
python3 lambda_runtime_audit.py \
  --profiles sso_pn-core-dev \
  --regions eu-south-1 eu-central-1

# Set a custom report path
python3 lambda_runtime_audit.py --output output/custom-report.xlsx
```

Use `python3 lambda_runtime_audit.py --help` to see all available options.
The script exits with a non-zero status if it encounters an account or region
error during the audit.
