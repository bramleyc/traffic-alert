# Traffic Alert

Serverless traffic alerting tool. Checks journey times on configured routes
at scheduled times and sends a push notification if traffic is significantly
worse than normal, or if you are likely to miss a target arrival time.

Uses the TomTom API and ntfy.sh with limited calls to keep within free tiers.

**Stack:** AWS Lambda + EventBridge + SSM Parameter Store · TomTom Routing API · ntfy.sh

---

## How it works

- A Lambda function runs every minute via EventBridge (configurable - currently runs during daytime only)
- On each invocation it checks whether the current UTC time and day matches
  any configured route check
- If it matches, it calls the TomTom Routing API twice: once with live
  traffic, once without (free-flow)
- It compares the two and evaluates two alert conditions:
  - Journey time is more than `alert_threshold_pct`% above free-flow
  - Current journey time means you will miss your `target_arrival_utc`
- A push notification is sent via ntfy.sh if either condition is met
- An all-clear notification is sent if the previous check was an alert but
  traffic has since returned to normal OR the traffic on the route looks all-clear
- State (whether the last check alerted) is persisted in SSM Parameter Store

---

## Files

```
traffic-alert/
  lambda_function.py   — Lambda handler
  config.json          — Route and schedule configuration (you edit this but do not deploy)
  requirements.txt     — Python dependencies
  deploy.sh            — Deploys everything to AWS
  README.md            — This file
```

---

## Prerequisites

- AWS CLI installed and configured (see AWS Setup below)
- Python 3 and pip installed locally
- A TomTom API key — free at https://developer.tomtom.com, no credit card needed
- ntfy app installed on your phone (iOS / Android, free)

---

## AWS Setup

### IAM Role for deployment

This project uses an IAM Role (`traffic-alert-deployer`) rather than static
IAM User credentials for deployment. Your local CLI assumes the role
temporarily when running deploy commands, rather than using long-lived keys
scoped to a specific project.

#### 1. Create the Role in the AWS Console

IAM → Roles → Create role

- Trusted entity type: **AWS account** → This account
- Skip the permissions screen and proceed to the Name step
- Role name: `traffic-alert-deployer`
- Create role

#### 2. Add an inline permissions policy to the Role

IAM → Roles → traffic-alert-deployer → Permissions tab
→ Add permissions → Create inline policy → JSON tab

Paste the following, replacing `YOUR_ACCOUNT_ID` with your 12-digit AWS
account ID (visible in the top-right corner of the console):

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "LambdaDeploy",
      "Effect": "Allow",
      "Action": [
        "lambda:CreateFunction",
        "lambda:UpdateFunctionCode",
        "lambda:UpdateFunctionConfiguration",
        "lambda:GetFunction",
        "lambda:AddPermission",
        "lambda:GetPolicy",
        "lambda:InvokeFunction"
      ],
      "Resource": "arn:aws:lambda:eu-west-2:YOUR_ACCOUNT_ID:function:traffic-alert"
    },
    {
      "Sid": "IAMRoleSetup",
      "Effect": "Allow",
      "Action": [
        "iam:CreateRole",
        "iam:GetRole",
        "iam:AttachRolePolicy",
        "iam:PutRolePolicy",
        "iam:PassRole"
      ],
      "Resource": "arn:aws:iam::YOUR_ACCOUNT_ID:role/traffic-alert-role"
    },
    {
      "Sid": "EventBridge",
      "Effect": "Allow",
      "Action": [
        "events:PutRule",
        "events:PutTargets",
        "events:DescribeRule"
      ],
      "Resource": "arn:aws:events:eu-west-2:YOUR_ACCOUNT_ID:rule/traffic-alert-*"
    },
    {
      "Sid": "SSM",
      "Effect": "Allow",
      "Action": [
        "ssm:PutParameter",
        "ssm:GetParameter"
      ],
      "Resource": "arn:aws:ssm:eu-west-2:YOUR_ACCOUNT_ID:parameter/traffic-alert/*"
    },
    {
      "Sid": "CloudWatchLogs",
      "Effect": "Allow",
      "Action": [
        "logs:PutRetentionPolicy",
        "logs:CreateLogGroup",
        "logs:DescribeLogGroups",
        "logs:DescribeLogStreams",
        "logs:FilterLogEvents",
        "logs:GetLogEvents"
      ],
      "Resource": "arn:aws:logs:eu-west-2:YOUR_ACCOUNT_ID:log-group:/aws/lambda/traffic-alert*"
    },
    {
      "Sid": "STSIdentity",
      "Effect": "Allow",
      "Action": "sts:GetCallerIdentity",
      "Resource": "*"
    }
  ]
}
```

Name the policy `traffic-alert-deploy-policy` and create it.

#### 3. Update the Role's trust policy

IAM → Roles → traffic-alert-deployer → Trust relationships tab
→ Edit trust policy

Replace the contents with the following, substituting your account ID and
IAM username:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "AWS": "arn:aws:iam::YOUR_ACCOUNT_ID:user/YOUR_IAM_USERNAME"
      },
      "Action": "sts:AssumeRole"
    }
  ]
}
```

#### 4. Allow your IAM user to assume the Role

IAM → Users → YOUR_IAM_USERNAME → Add permissions
→ Attach policies directly → Create inline policy → JSON tab

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": "sts:AssumeRole",
      "Resource": "arn:aws:iam::YOUR_ACCOUNT_ID:role/traffic-alert-deployer"
    }
  ]
}
```

Name it `assume-traffic-alert-role` and create it.

#### 5. Configure the AWS CLI profile

Add the following to `~/.aws/config`, replacing values as appropriate:

```ini
[profile traffic-alert]
role_arn = arn:aws:iam::YOUR_ACCOUNT_ID:role/traffic-alert-deployer
source_profile = default
region = eu-west-2
output = json
```

`source_profile` should match the profile name in `~/.aws/credentials` where
your IAM user's access keys are stored.

#### 6. Verify

```bash
aws sts get-caller-identity --profile traffic-alert
```

The `Arn` in the response should contain `assumed-role/traffic-alert-deployer`.

---

## Secrets and credentials

| Secret | Where it lives | How it gets there |
|---|---|---|
| AWS CLI keys | `~/.aws/credentials` — never in this project | `aws configure` |
| TomTom API key | Lambda environment variable | Set manually after deploy (see below) |
| ntfy topic name | SSM Parameter Store (inside `config.json`) | Pushed by `deploy.sh` |
| Lambda runtime credentials | IAM Role (`traffic-alert-role`) | Created automatically by `deploy.sh` |

**Never commit `config.json` to version control** if it contains your real
ntfy topic name. Add it to `.gitignore`:

```
config.json
```

Keep a `config.example.json` with placeholder values in the repo instead.

The TomTom API key is intentionally kept out of `config.json` and set as a
Lambda environment variable directly, so it is never written to disk in this
project folder.

---

## Configuration

Edit `config.json` before deploying. The file is pushed to SSM by `deploy.sh`
and read by the Lambda at runtime - you never need to redeploy the Lambda
code just to change a route or schedule, only re-run `deploy.sh`.

Ensure `config.json` is in your `.gitignore`.

There is an example of the format to follow at `config.example.json`.


### Fields

**Top level**

- `ntfy_topic` — your ntfy.sh topic name. Make it unguessable as ntfy topics are public by default.
- `alert_threshold_pct` — percentage above free-flow journey time that
  triggers a warning alert. Default: `20`.

**Per route**

- `name` — display name used in notifications
- `origin` — latitude,longitude of start point
- `destination` — latitude,longitude of end point
- `waypoints` — optional list of intermediate latitude,longitude stops.
  Leave as `[]` if not needed.
- `checks` — list of scheduled checks for this route

**Per check**

- `time_utc` — time to run this check in UTC (24hr, e.g. `"07:15"`)
- `days` — list of days to run: `MON TUE WED THU FRI SAT SUN`
- `target_arrival_utc` — UTC time you need to arrive by. An alert fires if
  your ETA exceeds this. Set to `null` to disable this condition.

### UTC vs local time

All times in `config.json` must be in UTC.

- UK winter (GMT): UTC = local time
- UK summer (BST): UTC = local time − 1 hour
  - e.g. 07:30 BST → `"06:30"` in config

You will need to update check times in `config.json` and re-run `deploy.sh`
when the clocks change.

### Finding coordinates

In Google Maps, right-click any location and click the coordinates shown at
the top of the context menu to copy them. They are in `latitude,longitude`
format as required.

---

## Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will:

1. Push `config.json` to SSM Parameter Store
2. Package and deploy (or update) the Lambda function
3. Create the Lambda execution IAM role if it does not exist
4. Create the EventBridge rule (runs every minute by default unless you change the regex) if it does not exist
5. Set CloudWatch log retention to 1 day

### Set the TomTom API key

The deploy script prints this command at the end - run it with your real key. It will print this statement each time you deploy, but you only need to run it at the first time you set it up, unless you:
- Delete and recreate the Lambda function from scratch
- Accidentally run update-function-configuration with the placeholder REPLACE_ME still in it

```bash
aws lambda update-function-configuration \
  --function-name traffic-alert \
  --region eu-west-2 \
  --profile traffic-alert \
  --environment 'Variables={
    TOMTOM_API_KEY=your_key_here,
    SSM_CONFIG=/traffic-alert/config,
    SSM_STATE=/traffic-alert/state
  }'
```

### Updating routes or schedules

Edit `config.json` and re-run `./deploy.sh`. The Lambda code is not
repackaged unless `lambda_function.py` has changed.

---

## Testing

### 1. Test the TomTom API key and coordinates before deploying

```bash
curl "https://api.tomtom.com/routing/1/calculateRoute/ORIGIN:DESTINATION/json\
?key=YOUR_KEY&travelMode=car&traffic=true" \
| python3 -m json.tool | grep -E "travelTimeInSeconds|trafficDelayInSeconds"
```

Replace `ORIGIN` and `DESTINATION` with `lat,lon` values. A successful
response will contain `travelTimeInSeconds`. A `403` means the key is wrong;
a `400` means the coordinates are malformed.

### 2. Invoke the Lambda manually after deploying

Open two terminal windows.

**Terminal 1 — tail logs in real time:**

```bash
aws logs tail /aws/lambda/traffic-alert \
  --follow \
  --region eu-west-2 \
  --profile traffic-alert
```

**Terminal 2 — invoke with a test time:**

```bash
aws lambda invoke \
  --function-name traffic-alert \
  --profile traffic-alert \
  --region eu-west-2 \
  --payload '{"test_time_utc": "07:45", "test_day": "SUN"}' \
  --cli-binary-format raw-in-base64-out \
  response.json && cat response.json
```

Set `test_time_utc` and `test_day` to match one of your configured checks.
The Lambda will run as if it is that time and day, fetch live traffic, and
send a notification if the conditions are met. There is a test route in the config with an unrealistic time (Sun 07:45). If you want to test a likely "all clear" then use 06:15 and Mon.

A successful response looks like:

```json
{"status": "ok"}
```

Check Terminal 1 for the full log output including journey times and whether
an alert was sent.

---

## AWS free tier

This project runs entirely within AWS's permanent free tier (no 12-month
expiry).

| Service | Usage | Free tier |
|---|---|---|
| Lambda | ~44,640 invocations/month | 1,000,000/month |
| Lambda compute | ~2,800 GB-seconds/month | 400,000 GB-seconds/month |
| EventBridge Rules | 1 rule | Free |
| SSM Parameter Store | 2 standard parameters | Free |
| CloudWatch Logs | Minimal (1-day retention) | 5 GB/month |

TomTom free tier: 2,500 non-tile API requests/day. A typical setup with
2 routes × 3 checks × 2 API calls (live + free-flow) = 12 calls/day.
