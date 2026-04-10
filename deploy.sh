#!/usr/bin/env bash
set -euo pipefail
export AWS_PROFILE="traffic-alert"

FUNCTION_NAME="traffic-alert"
REGION="eu-west-2"
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
ROLE_NAME="${FUNCTION_NAME}-role"

echo "==> Pushing config to SSM..."
aws ssm put-parameter \
  --name "/traffic-alert/config" \
  --value file://config.json \
  --type String \
  --overwrite \
  --region "$REGION" > /dev/null

echo "==> Packaging Lambda..."
pip install -r requirements.txt -t package/ --quiet
cp lambda_function.py package/
cd package && zip -r ../function.zip . -x "*.pyc" > /dev/null && cd ..
rm -rf package

# IAM role
if ! aws iam get-role --role-name "$ROLE_NAME" > /dev/null 2>&1; then
  echo "==> Creating IAM role..."
  aws iam create-role \
    --role-name "$ROLE_NAME" \
    --assume-role-policy-document '{
      "Version":"2012-10-17",
      "Statement":[{"Effect":"Allow","Principal":{"Service":"lambda.amazonaws.com"},"Action":"sts:AssumeRole"}]
    }' > /dev/null

  aws iam attach-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-arn arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

  aws iam put-role-policy \
    --role-name "$ROLE_NAME" \
    --policy-name ssm-access \
    --policy-document "{
      \"Version\":\"2012-10-17\",
      \"Statement\":[{
        \"Effect\":\"Allow\",
        \"Action\":[\"ssm:GetParameter\",\"ssm:PutParameter\"],
        \"Resource\":\"arn:aws:ssm:${REGION}:${ACCOUNT_ID}:parameter/traffic-alert/*\"
      }]
    }"

  echo "Waiting for role propagation..."
  sleep 10
fi

ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

# Lambda create or update
if aws lambda get-function --function-name "$FUNCTION_NAME" --region "$REGION" > /dev/null 2>&1; then
  echo "==> Updating Lambda code..."
  aws lambda update-function-code \
    --function-name "$FUNCTION_NAME" \
    --zip-file fileb://function.zip \
    --region "$REGION" > /dev/null
else
  echo "==> Creating Lambda..."
  aws lambda create-function \
    --function-name "$FUNCTION_NAME" \
    --runtime python3.12 \
    --role "$ROLE_ARN" \
    --handler lambda_function.lambda_handler \
    --zip-file fileb://function.zip \
    --timeout 30 \
    --memory-size 128 \
    --region "$REGION" \
    --environment "Variables={
      TOMTOM_API_KEY=REPLACE_ME,
      SSM_CONFIG=/traffic-alert/config,
      SSM_STATE=/traffic-alert/state
    }" > /dev/null
fi

# EventBridge rule: every minute during the day
RULE_NAME="${FUNCTION_NAME}-every-minute"

if ! aws events describe-rule --name "$RULE_NAME" --region "$REGION" > /dev/null 2>&1; then
  echo "==> Creating EventBridge rule (every minute)..."
  aws events put-rule \
    --name "$RULE_NAME" \
    --schedule-expression "cron(* 5-20 ? * * *)" \
    --state ENABLED \
    --region "$REGION" > /dev/null

  LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

  aws events put-targets \
    --rule "$RULE_NAME" \
    --targets "Id=1,Arn=${LAMBDA_ARN}" \
    --region "$REGION" > /dev/null

  aws lambda add-permission \
    --function-name "$FUNCTION_NAME" \
    --statement-id "allow-eventbridge" \
    --action lambda:InvokeFunction \
    --principal events.amazonaws.com \
    --source-arn "arn:aws:events:${REGION}:${ACCOUNT_ID}:rule/${RULE_NAME}" \
    --region "$REGION" > /dev/null
fi

# Short log retention to stay within CloudWatch free tier
aws logs put-retention-policy \
  --log-group-name "/aws/lambda/${FUNCTION_NAME}" \
  --retention-in-days 1 \
  --region "$REGION" 2>/dev/null || true

echo ""
echo "==> Done."
echo ""
echo "Next: set your TomTom API key:"
echo ""
echo "  aws lambda update-function-configuration \\"
echo "    --function-name $FUNCTION_NAME \\"
echo "    --region $REGION \\"
echo "    --environment 'Variables={TOMTOM_API_KEY=your_key_here,SSM_CONFIG=/traffic-alert/config,SSM_STATE=/traffic-alert/state}'"
echo ""
echo "Remember: all times in config.json are UTC. BST = UTC+1 in summer."
rm -f function.zip
