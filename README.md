# Traffic Alert

Serverless traffic alerting tool. Checks journey times on configured routes
at scheduled times and sends a push notification if traffic is significantly
worse than normal, or if you are likely to miss a target arrival time — or if
it's all clear, for reassurance. Also automatically picks up events from Google
Calendar and generates alerts based on the free-flow journey time from home.

Uses the TomTom API and ntfy.sh with limited calls to keep within free tiers.

**Stack:** AWS Lambda + EventBridge + SSM Parameter Store · TomTom Routing API · Google Calendar API · ntfy.sh

---

## How it works

- A Lambda function runs every minute via EventBridge (currently 05:00–20:00 UTC)
- On each invocation it handles two types of checks:

**Scheduled route checks** (defined in `config.json`):
- Checks whether the current UTC time and day matches any configured route check
- If it matches, calls the TomTom Routing API twice: once with live traffic, once without (free-flow)
- Evaluates two alert conditions:
  - Journey time is more than `alert_threshold_pct`% above free-flow
  - Current journey time means you will miss your `target_arrival_utc`
- Sends a push notification via ntfy.sh if either condition is met, or an all-clear if not

**Calendar-based checks** (from Google Calendar):
- Fetches today's events from each profile's Google Calendar
- Events without a Location field are ignored; the location is geocoded via TomTom
- On first encounter, calculates free-flow time from home and sets two check windows:
  2× and 1.5× free-flow before the event start time (rounded to 5 min)
- At those computed times, runs a traffic check with the event start as the target arrival

State (whether the last check alerted) is persisted in SSM Parameter Store.

---

## Files

```
traffic-alert/
  lambda_function.py      — Lambda handler
  config.json             — Route, schedule, and profile configuration (gitignored)
  config.example.json     — Example config to copy from
  requirements.txt        — Python dependencies
  deploy.sh               — Deploys everything to AWS
  google-credentials.json — Google service account key (gitignored, pushed to SSM by deploy.sh)
  README.md               — This file
```

---

## Prerequisites

- AWS CLI installed and configured (see AWS Setup below)
- Python 3 and pip installed locally
- A TomTom API key — free at https://developer.tomtom.com, no credit card needed
- ntfy app installed on your phone (iOS / Android, free)
- A Google Cloud project with the Calendar API enabled (see Google Calendar Setup below)

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

## Google Calendar Setup

Calendar alerts are driven by a Google service account that reads each
profile's calendar. Events without a Location field are ignored.

#### 1. Create a Google Cloud project and enable the Calendar API

1. Go to [console.cloud.google.com](https://console.cloud.google.com)
2. Create a new project (e.g. `traffic-alert`)
3. Use the top search bar to find **Google Calendar API** → Enable it

#### 2. Create a service account

1. Search for **Service Accounts** in the console
2. Click **+ Create Service Account** — name it `traffic-alert`
3. Skip both optional permissions steps (no IAM role is needed)
4. Open the created service account → **Keys** tab → **Add Key → Create new key → JSON**
5. Save the downloaded file as `google-credentials.json` in this directory
   (it is gitignored and pushed to SSM automatically by `deploy.sh`)

#### 3. Share each calendar with the service account

1. In [Google Calendar](https://calendar.google.com), open Settings for the calendar
2. Under **Share with specific people** → add the service account email
   (looks like `traffic-alert@your-project-id.iam.gserviceaccount.com`)
3. Set permission to **See all event details**

#### 4. Get the calendar ID

In Google Calendar settings → **Integrate calendar** → copy the **Calendar ID**.
Set it as `calendar_id` in `config.json` for the relevant profile.

---

## Secrets and credentials

| Secret | Where it lives | How it gets there |
|---|---|---|
| AWS CLI keys | `~/.aws/credentials` — never in this project | `aws configure` |
| TomTom API key | Lambda environment variable | Set manually after first deploy (see below) |
| ntfy topic name | SSM Parameter Store (inside `config.json`) | Pushed by `deploy.sh` |
| Google service account key | SSM Parameter Store (`/traffic-alert/google-credentials`) | Pushed by `deploy.sh` if `google-credentials.json` exists |
| Lambda runtime credentials | IAM Role (`traffic-alert-role`) | Created automatically by `deploy.sh` |

---

## Configuration

Edit `config.json` before deploying. It is pushed to SSM by `deploy.sh` and
read by the Lambda at runtime — you never need to redeploy Lambda code just to
change a route or schedule, only re-run `deploy.sh`.

**Never commit `config.json` to version control** — it is gitignored.

See `config.example.json` for a full example.

### Schema

**Top level**

- `google_credentials_ssm` — SSM path to the Google service account JSON.
  Defaults to `/traffic-alert/google-credentials`.
- `profiles` — list of user profiles (see below).

**Per profile**

- `name` — display name, used in log output and as a state key prefix.
- `ntfy_topic` — ntfy.sh topic name. Make it unguessable as topics are public by default.
- `home` — `"lat,lon"` of the person's home address. Used as the origin for all calendar-based route checks.
- `calendar_id` — Google Calendar ID to monitor for events (optional).
- `alert_threshold_pct` — percentage above free-flow journey time that triggers a warning. Default: `20`.
- `routes` — list of scheduled route checks (see below).

**Per route**

- `name` — display name used in notifications.
- `origin` — `"lat,lon"` of the start point.
- `destination` — `"lat,lon"` of the end point.
- `waypoints` — optional list of intermediate `"lat,lon"` stops. Leave as `[]` if not needed.
- `checks` — list of scheduled checks for this route.

**Per check**

- `time_utc` — time to run this check in UTC (24hr, e.g. `"07:15"`).
- `days` — list of days to run: `MON TUE WED THU FRI SAT SUN`.
- `target_arrival_utc` — UTC time you need to arrive by. An alert fires if
  your ETA exceeds this. Omit to disable this condition.

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

### Calendar events

Any event in the monitored Google Calendar that has a **Location** field set
will automatically generate traffic alerts. The location is geocoded via
TomTom on first encounter. Events without a location are silently ignored.

Check windows are calculated as **2× and 1.5× the free-flow journey time**
before the event start (rounded to the nearest 5 min). For example, a 30-minute
free-flow journey to a 10:00 event generates checks at 08:00 and 08:30.

---

## Deploy

```bash
chmod +x deploy.sh
./deploy.sh
```

The script will:

1. Push `config.json` to SSM Parameter Store
2. Push `google-credentials.json` to SSM (if the file exists)
3. Package and deploy (or update) the Lambda function
4. Create the Lambda execution IAM role if it does not exist
5. Create/update the EventBridge rule (runs every minute, 05:00–20:00 UTC)
6. Set CloudWatch log retention to 1 day

### Set the TomTom API key

The deploy script prints this command at the end. Run it once with your real
key after the first deploy (you do not need to re-run it on subsequent deploys
unless you recreate the Lambda from scratch).

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

Edit `config.json` and re-run `./deploy.sh`.

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
A successful response looks like:

```json
{"status": "ok"}
```

Check Terminal 1 for the full log output including journey times and whether
an alert was sent.

---

## Free tier summary

| Service | Usage | Free tier |
|---|---|---|
| Lambda | ~13,500 invocations/month | 1,000,000/month |
| Lambda compute | ~850 GB-seconds/month | 400,000 GB-seconds/month |
| EventBridge Rules | 1 rule | Free |
| SSM Parameter Store | 3 standard parameters | Free |
| CloudWatch Logs | Minimal (1-day retention) | 5 GB/month |
| Google Calendar API | ~900 reads/day | 1,000,000/day |
| TomTom Routing API | ~12 calls/day (scheduled) + 2 per calendar check | 2,500/day |
| TomTom Geocoding API | Once per new calendar event (cached) | 2,500/day |
