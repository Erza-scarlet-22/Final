# Log Aggregator – Full Deployment Guide
### AWS Console + CLI + CloudFormation | Step-by-Step

---

## Table of Contents

1. [Architecture Overview](#1-architecture-overview)
2. [Pre-Deployment Checklist](#2-pre-deployment-checklist)
3. [Phase 0 – Mandatory File Changes Before Anything Else](#3-phase-0--mandatory-file-changes-before-anything-else)
4. [Phase 1 – Prerequisites & AWS Secrets Manager](#4-phase-1--prerequisites--aws-secrets-manager)
5. [Phase 2 – ECS Cluster & Services Setup](#5-phase-2--ecs-cluster--services-setup)
6. [Phase 3 – Deploy CI/CD via CloudFormation (codepipeline.yml)](#6-phase-3--deploy-cicd-via-cloudformation-codepipelineyml)
7. [Phase 4 – Upload Source ZIP & Trigger Pipeline](#7-phase-4--upload-source-zip--trigger-pipeline)
8. [Phase 5 – Lambda Function Setup](#8-phase-5--lambda-function-setup)
9. [Phase 6 – Verify Deployment](#9-phase-6--verify-deployment)
10. [Mapping Your Existing Resources](#10-mapping-your-existing-resources)
11. [Common Errors & Debugging](#11-common-errors--debugging)

---

## 1. Architecture Overview

```
S3 (source ZIP upload)
        │  triggers
        ▼
CodePipeline
  ├── Stage 1: Source  (S3 → SourceArtifact)
  ├── Stage 2: Build   (CodeBuild: buildspec.yml)
  │     ├── Runs unit tests
  │     ├── Builds 4 Docker images → ECR
  │     │     app | processor | dashboard | chatbot
  │     └── Packages Lambda ZIP → S3
  └── Stage 3: Deploy  (parallel)
        ├── CodeBuild: buildspec-deploy.yml
        │     ├── Registers ECS task definitions
        │     ├── Renders appspec-ecs-*.yml files
        │     └── Updates Lambda code + publishes alias
        └── CodeDeploy: Blue/Green ECS for each service
              app-service | dashboard-service | chatbot-service

ECS Fargate (3 services)   Lambda (log processor)
```

**4 ECR Repos needed:**
- `log-aggregator/app`
- `log-aggregator/processor`
- `log-aggregator/dashboard`
- `log-aggregator/chatbot`

---

## 2. Pre-Deployment Checklist

Confirm each before starting:

- [ ] ECR repos exist (4 repos above)
- [ ] IAM roles exist: `log-aggregator-ecs-execution-role`, `log-aggregator-ecs-task-role`, CodeBuild role, CodePipeline role
- [ ] VPC, Subnets (at least 2 public + 2 private), Security Groups exist
- [ ] S3 buckets exist: source-zip bucket, pipeline-artifacts bucket, raw-logs bucket
- [ ] Application Load Balancer created with: 2 listeners (prod :80 or :443, test :8080) and 2 target groups per ECS service (blue + green)
- [ ] AWS Secrets Manager secret: `log-aggregator/bedrock` with keys `BEDROCK_AGENT_ID` and `BEDROCK_AGENT_ALIAS_ID`
- [ ] CloudWatch Log Groups created (or `awslogs-create-group: true` in task defs — already set)

---

## 3. Phase 0 – Mandatory File Changes Before Anything Else

> ⚠️ Do ALL of these substitutions before deploying. The placeholders `ACCOUNT_ID` and `REGION` appear in multiple files.

### 3.1 Your values reference sheet

Fill in your real values here first, then use them throughout:

| Placeholder | Your Real Value | Example |
|---|---|---|
| `ACCOUNT_ID` | Your 12-digit AWS account ID | `123456789012` |
| `REGION` | Your AWS region | `ap-south-1` |
| `YOUR_SOURCE_BUCKET` | S3 bucket for ZIP uploads | `my-log-agg-source` |
| `YOUR_ARTIFACT_BUCKET` | S3 bucket for pipeline artifacts | `my-log-agg-artifacts` |
| `YOUR_RAW_LOGS_BUCKET` | S3 bucket for raw logs + Lambda ZIPs | `my-log-agg-raw-logs` |
| `YOUR_ECR_PREFIX` | ECR repo prefix (check your actual repo names) | `log-aggregator` |
| `YOUR_ECS_CLUSTER` | Your ECS cluster name | `log-aggregator-cluster` |
| `YOUR_CODEBUILD_ROLE_ARN` | ARN of CodeBuild IAM role | `arn:aws:iam::123456789012:role/...` |
| `YOUR_CODEPIPELINE_ROLE_ARN` | ARN of CodePipeline IAM role | `arn:aws:iam::123456789012:role/...` |
| `YOUR_ECS_EXEC_ROLE_ARN` | ARN of ECS execution role | `arn:aws:iam::123456789012:role/...` |
| `YOUR_ECS_TASK_ROLE_ARN` | ARN of ECS task role | `arn:aws:iam::123456789012:role/...` |
| `ALB_PROD_LISTENER_ARN_APP` | ALB prod listener ARN for app service | `arn:aws:elasticloadbalancing:...` |
| `ALB_TEST_LISTENER_ARN_APP` | ALB test listener ARN for app service | `arn:aws:elasticloadbalancing:...` |

---

### 3.2 Changes to `ecs/taskdef-app.json`

Replace **every occurrence** of:
- `ACCOUNT_ID` → your 12-digit account ID
- `REGION` → your region (e.g., `ap-south-1`)

```json
// BEFORE
"executionRoleArn": "arn:aws:iam::ACCOUNT_ID:role/log-aggregator-ecs-execution-role",
"taskRoleArn":      "arn:aws:iam::ACCOUNT_ID:role/log-aggregator-ecs-task-role",
"image": "ACCOUNT_ID.dkr.ecr.REGION.amazonaws.com/log-aggregator/app:IMAGE_TAG_PLACEHOLDER",

// AFTER (example)
"executionRoleArn": "arn:aws:iam::123456789012:role/log-aggregator-ecs-execution-role",
"taskRoleArn":      "arn:aws:iam::123456789012:role/log-aggregator-ecs-task-role",
"image": "123456789012.dkr.ecr.ap-south-1.amazonaws.com/log-aggregator/app:IMAGE_TAG_PLACEHOLDER",
```

> ⚠️ **DO NOT replace `IMAGE_TAG_PLACEHOLDER`** — this is replaced at build time by `buildspec-deploy.yml` using `sed`.

Also update the Secrets Manager ARN in `secrets`:
```json
// BEFORE
"valueFrom": "arn:aws:secretsmanager:us-east-1:ACCOUNT_ID:secret:log-aggregator/bedrock:BEDROCK_AGENT_ID::"

// AFTER
"valueFrom": "arn:aws:secretsmanager:ap-south-1:123456789012:secret:log-aggregator/bedrock:BEDROCK_AGENT_ID::"
```

Also update `awslogs-region` in `logConfiguration`:
```json
// BEFORE
"awslogs-region": "us-east-1"

// AFTER
"awslogs-region": "ap-south-1"
```

**Repeat the same changes for `ecs/taskdef-dashboard.json` and `ecs/taskdef-chatbot.json`.**

---

### 3.3 Changes to `codepipeline.yml` (the CloudFormation template)

Locate the two hardcoded `ACCOUNT_ID` listener ARNs in the `DeploymentGroupApp` resource and replace them:

```yaml
# BEFORE (in DeploymentGroupApp LoadBalancerInfo)
ListenerArns:
  - "arn:aws:elasticloadbalancing:us-east-1:ACCOUNT_ID:listener/app/log-aggregator-alb/XXXXXXXX"
TestTrafficRoute:
  ListenerArns:
  - "arn:aws:elasticloadbalancing:us-east-1:ACCOUNT_ID:listener/app/log-aggregator-alb/YYYYYYYY"

# AFTER — use your real ALB listener ARNs
# Get these from: EC2 Console → Load Balancers → your ALB → Listeners tab
ListenerArns:
  - "arn:aws:elasticloadbalancing:ap-south-1:123456789012:listener/app/log-aggregator-alb/abc123/prod456"
TestTrafficRoute:
  ListenerArns:
  - "arn:aws:elasticloadbalancing:ap-south-1:123456789012:listener/app/log-aggregator-alb/abc123/test789"
```

Also fix the two `REPLACE_LISTENER_ARN` placeholders in `DeploymentGroupDashboard` and `DeploymentGroupChatbot`:

```yaml
# BEFORE
- !Sub "arn:aws:elasticloadbalancing:${AWSRegion}:${AWSAccountId}:listener/app/log-aggregator-dashboard-alb/REPLACE_LISTENER_ARN"

# AFTER (put your real dashboard ALB listener ARN)
- "arn:aws:elasticloadbalancing:ap-south-1:123456789012:listener/app/log-aggregator-dashboard-alb/abc123/listenerXXX"
```

> **How to find Listener ARNs:**
> AWS Console → EC2 → Load Balancers → click your ALB → Listeners tab → copy the ARN from each listener row.

---

### 3.4 Changes to `codedeploy/appspec-ecs.yml`

This file has a placeholder that is replaced at runtime by `buildspec-deploy.yml`. **No manual change needed** here — it is used as a reference template only.

---

### 3.5 If your ECR repo names differ from `log-aggregator/app`

If your existing ECR repos use a different naming convention (e.g., `myproject-app` instead of `log-aggregator/app`), you must update the ECR URIs in ALL of the following places:

- `ecs/taskdef-app.json` → `image` field
- `ecs/taskdef-dashboard.json` → `image` field
- `ecs/taskdef-chatbot.json` → `image` field
- `codepipeline.yml` → `ECR_APP_URI`, `ECR_PROCESSOR_URI`, `ECR_DASHBOARD_URI`, `ECR_CHATBOT_URI` environment variables

The `buildspec.yml` reads these ECR URIs from CodeBuild environment variables (injected by the CloudFormation template), so if you pass the correct values as CloudFormation parameters, `buildspec.yml` does not need to be edited.

---

## 4. Phase 1 – Prerequisites & AWS Secrets Manager

### 4.1 Create Secrets Manager secret

This is required before the ECS tasks can start. The task definitions pull `BEDROCK_AGENT_ID` and `BEDROCK_AGENT_ALIAS_ID` from here.

**Console:**
1. Go to **AWS Secrets Manager** → **Store a new secret**
2. Secret type: **Other type of secret**
3. Key/value pairs:
   - `BEDROCK_AGENT_ID` = `<your Bedrock Agent ID>`
   - `BEDROCK_AGENT_ALIAS_ID` = `<your Bedrock Agent Alias ID>`
4. Secret name: `log-aggregator/bedrock`
5. Keep all other defaults → **Store**

**CLI:**
```bash
aws secretsmanager create-secret \
  --name "log-aggregator/bedrock" \
  --region ap-south-1 \
  --secret-string '{
    "BEDROCK_AGENT_ID": "YOUR_BEDROCK_AGENT_ID",
    "BEDROCK_AGENT_ALIAS_ID": "YOUR_BEDROCK_AGENT_ALIAS_ID"
  }'
```

---

### 4.2 Verify S3 buckets have versioning enabled (required for CodePipeline)

CodePipeline requires S3 versioning on the **source bucket**.

**Console:**
S3 → your source bucket → **Properties** tab → **Bucket Versioning** → Enable

**CLI:**
```bash
aws s3api put-bucket-versioning \
  --bucket YOUR_SOURCE_BUCKET \
  --versioning-configuration Status=Enabled
```

---

### 4.3 Verify Target Groups exist for Blue/Green

For each ECS service (app, dashboard, chatbot) you need **two target groups** (blue + green). Names should match what's in `codepipeline.yml`:
- `log-aggregator-app-tg-blue`
- `log-aggregator-app-tg-green`
- `log-aggregator-dashboard-tg-blue`
- `log-aggregator-dashboard-tg-green`
- `log-aggregator-chatbot-tg-blue`
- `log-aggregator-chatbot-tg-green`

If your target group names are different, update them in `codepipeline.yml` under each `DeploymentGroup`'s `TargetGroupPairInfoList`.

---

## 5. Phase 2 – ECS Cluster & Services Setup

> If your ECS cluster and services already exist, skim this section to verify the configuration matches what the pipeline expects. The pipeline expects services named as set in the CloudFormation parameters.

### 5.1 ECS Cluster (skip if already exists)

**Console:**
1. ECS → **Clusters** → **Create Cluster**
2. Cluster name: `log-aggregator-cluster` (or your existing name)
3. Infrastructure: **AWS Fargate** (serverless)
4. **Create**

**CLI:**
```bash
aws ecs create-cluster \
  --cluster-name log-aggregator-cluster \
  --capacity-providers FARGATE \
  --region ap-south-1
```

---

### 5.2 Create ECS Services (one per container)

You need 3 services: `log-aggregator-app-service`, `log-aggregator-dashboard-service`, `log-aggregator-chatbot-service`.

> ⚠️ For CodeDeploy Blue/Green to work, the ECS service **must** be created with deployment controller type `CODE_DEPLOY`, not the default `ECS`. This cannot be changed after creation.

**Register an initial task definition first** (before services exist, use the taskdef JSONs after Phase 0 edits):

```bash
# Register initial task definitions (run from inside the unzipped log-aggregator/ folder)
aws ecs register-task-definition \
  --cli-input-json file://ecs/taskdef-app.json \
  --region ap-south-1

aws ecs register-task-definition \
  --cli-input-json file://ecs/taskdef-dashboard.json \
  --region ap-south-1

aws ecs register-task-definition \
  --cli-input-json file://ecs/taskdef-chatbot.json \
  --region ap-south-1
```

> Note: `IMAGE_TAG_PLACEHOLDER` in the image field will cause a task definition registration failure. Before registering manually, temporarily replace it with `latest` in the JSON file. The pipeline will overwrite the task definition with real tags on first run.

**Create each service via Console:**
1. ECS → your cluster → **Services** → **Create**
2. Launch type: **Fargate**
3. Task definition: select the family you just registered
4. Service name: `log-aggregator-app-service`
5. Desired tasks: `1`
6. Deployment options → Deployment type: **Blue/green deployment (powered by CodeDeploy)**
7. Network: select your VPC, private subnets, and security group
8. Load balancing: select your ALB → Production listener → your blue target group → Test listener → your green target group
9. **Create**

Repeat for dashboard and chatbot services.

---

## 6. Phase 3 – Deploy CI/CD via CloudFormation (codepipeline.yml)

This single CloudFormation template creates:
- CodeBuild project (build phase)
- CodeBuild project (deploy phase)
- CodeDeploy Applications (ECS + Lambda)
- CodeDeploy Deployment Groups (app, dashboard, chatbot, lambda)
- CodePipeline with all 3 stages

### 6.1 Deploy via Console

1. Go to **CloudFormation** → **Create stack** → **With new resources**
2. Template source: **Upload a template file** → select `codepipeline.yml`
3. **Next** → Fill in parameters:

| Parameter | Value to Enter |
|---|---|
| `EnvironmentName` | `log-aggregator` |
| `AWSAccountId` | Your 12-digit account ID |
| `AWSRegion` | Your region (e.g., `ap-south-1`) |
| `SourceBucket` | Your source ZIP S3 bucket name |
| `SourceZipKey` | `log-ag-main.zip` |
| `ArtifactBucket` | Your pipeline artifacts S3 bucket name |
| `RawLogsBucket` | Your raw logs S3 bucket name |
| `ECSCluster` | `log-aggregator-cluster` (or your name) |
| `ECSServiceApp` | `log-aggregator-app-service` |
| `ECSServiceDashboard` | `log-aggregator-dashboard-service` |
| `ECSServiceChatbot` | `log-aggregator-chatbot-service` |
| `LambdaFunctionName` | `log-aggregator-processor` |
| `CodeBuildRoleArn` | ARN of your CodeBuild IAM role |
| `CodePipelineRoleArn` | ARN of your CodePipeline IAM role |
| `ECSExecutionRoleArn` | ARN of your ECS execution role |
| `ECSTaskRoleArn` | ARN of your ECS task role |

4. **Next** → optionally add tags → **Next**
5. Check **"I acknowledge that AWS CloudFormation might create IAM resources"**
6. **Submit**

Wait for stack status: `CREATE_COMPLETE` (takes ~2–3 minutes).

### 6.2 Deploy via CLI

```bash
aws cloudformation create-stack \
  --stack-name log-aggregator-pipeline \
  --template-body file://codepipeline.yml \
  --region ap-south-1 \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameters \
    ParameterKey=AWSAccountId,ParameterValue=123456789012 \
    ParameterKey=AWSRegion,ParameterValue=ap-south-1 \
    ParameterKey=SourceBucket,ParameterValue=YOUR_SOURCE_BUCKET \
    ParameterKey=SourceZipKey,ParameterValue=log-ag-main.zip \
    ParameterKey=ArtifactBucket,ParameterValue=YOUR_ARTIFACT_BUCKET \
    ParameterKey=RawLogsBucket,ParameterValue=YOUR_RAW_LOGS_BUCKET \
    ParameterKey=ECSCluster,ParameterValue=log-aggregator-cluster \
    ParameterKey=ECSServiceApp,ParameterValue=log-aggregator-app-service \
    ParameterKey=ECSServiceDashboard,ParameterValue=log-aggregator-dashboard-service \
    ParameterKey=ECSServiceChatbot,ParameterValue=log-aggregator-chatbot-service \
    ParameterKey=LambdaFunctionName,ParameterValue=log-aggregator-processor \
    ParameterKey=CodeBuildRoleArn,ParameterValue=arn:aws:iam::123456789012:role/YOUR_CODEBUILD_ROLE \
    ParameterKey=CodePipelineRoleArn,ParameterValue=arn:aws:iam::123456789012:role/YOUR_CODEPIPELINE_ROLE \
    ParameterKey=ECSExecutionRoleArn,ParameterValue=arn:aws:iam::123456789012:role/YOUR_ECS_EXEC_ROLE \
    ParameterKey=ECSTaskRoleArn,ParameterValue=arn:aws:iam::123456789012:role/YOUR_ECS_TASK_ROLE
```

**Check stack status:**
```bash
aws cloudformation describe-stacks \
  --stack-name log-aggregator-pipeline \
  --region ap-south-1 \
  --query 'Stacks[0].StackStatus'
```

---

## 7. Phase 4 – Upload Source ZIP & Trigger Pipeline

### 7.1 Prepare the ZIP

After making all file changes from Phase 0, re-zip the folder:

```bash
# From the directory CONTAINING log-aggregator/
zip -r log-ag-main.zip log-aggregator/

# Verify the structure is correct
unzip -l log-ag-main.zip | head -20
```

The ZIP must contain the `log-aggregator/` folder at the root. CodeBuild handles path normalization automatically (the buildspec already has a flattening step for nested paths).

### 7.2 Upload to S3 (triggers pipeline automatically)

**Console:**
1. S3 → your source bucket → **Upload** → select `log-ag-main.zip`
2. Keep the key exactly as `log-ag-main.zip` (must match `SourceZipKey` parameter)
3. **Upload**

Pipeline auto-triggers within ~1 minute (it polls for S3 changes).

**CLI:**
```bash
aws s3 cp log-ag-main.zip s3://YOUR_SOURCE_BUCKET/log-ag-main.zip \
  --region ap-south-1
```

### 7.3 Monitor pipeline execution

**Console:**
CodePipeline → `log-aggregator-pipeline` → watch stages

**CLI:**
```bash
# Get latest execution ID
aws codepipeline list-pipeline-executions \
  --pipeline-name log-aggregator-pipeline \
  --region ap-south-1 \
  --max-results 1

# Watch a specific execution
aws codepipeline get-pipeline-execution \
  --pipeline-name log-aggregator-pipeline \
  --pipeline-execution-id EXECUTION_ID \
  --region ap-south-1
```

---

## 8. Phase 5 – Lambda Function Setup

The Lambda function (`log-aggregator-processor`) must exist **before** the deploy stage can update it. Create it once manually; CodePipeline will update its code on every subsequent run.

### 8.1 Create Lambda function

**Console:**
1. Lambda → **Create function** → **Author from scratch**
2. Function name: `log-aggregator-processor`
3. Runtime: **Python 3.12**
4. Execution role: **Use an existing role** → select your ECS task role or a dedicated Lambda role (must have S3 read + CloudWatch Logs write permissions)
5. **Create function**

**CLI:**
```bash
# Create a minimal placeholder ZIP first (the pipeline will replace it)
echo "def handler(e, c): pass" > /tmp/placeholder.py
cd /tmp && zip placeholder.zip placeholder.py

aws lambda create-function \
  --function-name log-aggregator-processor \
  --runtime python3.12 \
  --handler lambda_handler.handler \
  --role arn:aws:iam::123456789012:role/YOUR_LAMBDA_ROLE \
  --zip-file fileb:///tmp/placeholder.zip \
  --region ap-south-1
```

### 8.2 Create Lambda alias `live`

The deploy buildspec publishes versions and updates a `live` alias. Create it now (the script handles creation or update automatically, but pre-creating avoids a first-run edge case):

```bash
# Publish initial version
aws lambda publish-version \
  --function-name log-aggregator-processor \
  --region ap-south-1

# Create alias pointing to version 1
aws lambda create-alias \
  --function-name log-aggregator-processor \
  --name live \
  --function-version 1 \
  --region ap-south-1
```

---

## 9. Phase 6 – Verify Deployment

### 9.1 Check CodeBuild logs (Build stage)

**Console:**
CodeBuild → `log-aggregator-build` → Build history → click latest build → **Build logs**

Look for:
```
All tests passed.
Building Flask app image...
Pushing images to ECR...
Build complete. Image tag -> abc12345
```

**CLI:**
```bash
# Get the latest build ID
aws codebuild list-builds-for-project \
  --project-name log-aggregator-build \
  --region ap-south-1 \
  --query 'ids[0]'

# Tail logs via CloudWatch
aws logs tail /aws/codebuild/log-aggregator-build \
  --region ap-south-1 \
  --follow
```

### 9.2 Verify ECR images were pushed

**Console:**
ECR → `log-aggregator/app` → Images tab → confirm a new image with a timestamp tag

**CLI:**
```bash
aws ecr describe-images \
  --repository-name log-aggregator/app \
  --region ap-south-1 \
  --query 'sort_by(imageDetails, &imagePushedAt)[-1]'
```

### 9.3 Verify ECS services are stable

**Console:**
ECS → `log-aggregator-cluster` → Services → each service should show **Running = 1, Desired = 1, Pending = 0**

**CLI:**
```bash
aws ecs describe-services \
  --cluster log-aggregator-cluster \
  --services log-aggregator-app-service log-aggregator-dashboard-service log-aggregator-chatbot-service \
  --region ap-south-1 \
  --query 'services[*].{name:serviceName,running:runningCount,desired:desiredCount,status:status}'
```

### 9.4 Verify Lambda was updated

```bash
aws lambda get-function \
  --function-name log-aggregator-processor \
  --region ap-south-1 \
  --query 'Configuration.{LastModified:LastModified,CodeSize:CodeSize}'

aws lambda get-alias \
  --function-name log-aggregator-processor \
  --name live \
  --region ap-south-1
```

### 9.5 Check application health

If your ALB is set up, hit the health endpoint:
```bash
curl http://YOUR_ALB_DNS/api/status
# Expected: {"status": "ok", ...}
```

---

## 10. Mapping Your Existing Resources

### 10.1 Existing ECR Repos

If your ECR repo names are different from `log-aggregator/app`, update these 3 places:

**In `codepipeline.yml` parameters section** — these feed the environment variables to CodeBuild:
```yaml
# The ECR_*_URI env vars are computed as:
# ${AWSAccountId}.dkr.ecr.${AWSRegion}.amazonaws.com/${EnvironmentName}/app
# So if your repos are named differently, override them in the DeployProject/BuildProject
# environment variable section directly.
```

Simplest approach: rename your ECR repos to match `log-aggregator/app`, `log-aggregator/processor`, etc. If that's not possible, edit the `ECR_APP_URI` etc. environment variable `Value` fields inside both `BuildProject` and `DeployProject` in `codepipeline.yml` to your actual URIs.

### 10.2 Existing IAM Roles

Pass them as CloudFormation parameters:
- `CodeBuildRoleArn` → your existing CodeBuild service role ARN
- `CodePipelineRoleArn` → your existing CodePipeline service role ARN
- `ECSExecutionRoleArn` → your existing ECS task execution role ARN
- `ECSTaskRoleArn` → your existing ECS task role ARN

**Required permissions checklist for each role:**

**CodeBuild Role** must have:
- `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage`, `ecr:PutImage`, `ecr:InitiateLayerUpload`, `ecr:UploadLayerPart`, `ecr:CompleteLayerUpload`
- `s3:GetObject`, `s3:PutObject`, `s3:GetBucketVersioning` on artifact + source buckets
- `ecs:RegisterTaskDefinition`
- `lambda:UpdateFunctionCode`, `lambda:PublishVersion`, `lambda:UpdateAlias`, `lambda:CreateAlias`, `lambda:GetFunction`
- `secretsmanager:GetSecretValue` on `log-aggregator/bedrock`
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`

**CodePipeline Role** must have:
- `codebuild:StartBuild`, `codebuild:BatchGetBuilds`
- `codedeploy:CreateDeployment`, `codedeploy:GetDeployment`, `codedeploy:GetDeploymentConfig`, `codedeploy:RegisterApplicationRevision`
- `s3:GetObject`, `s3:PutObject`, `s3:GetBucketVersioning` on artifact bucket
- `ecs:RegisterTaskDefinition`

**ECS Execution Role** must have:
- `ecr:GetAuthorizationToken`, `ecr:BatchCheckLayerAvailability`, `ecr:GetDownloadUrlForLayer`, `ecr:BatchGetImage`
- `logs:CreateLogGroup`, `logs:CreateLogStream`, `logs:PutLogEvents`
- `secretsmanager:GetSecretValue` on `log-aggregator/bedrock`

**ECS Task Role** must have:
- `bedrock:InvokeAgent`, `bedrock:InvokeModel` (for chatbot service)
- `s3:GetObject`, `s3:PutObject` on raw-logs bucket

### 10.3 Existing VPC/Subnets

VPC and subnet configuration is used by ECS services, not by the CloudFormation CI/CD template. When creating/editing ECS services:

- **Subnets**: use your **private** subnets for ECS tasks (tasks pull from ECR via NAT or VPC endpoints)
- **Security Group for ECS tasks**: must allow inbound on port 5000 from the ALB security group
- **Security Group for ALB**: must allow inbound 80/443 from 0.0.0.0/0 and outbound to ECS task SG on port 5000

If ECS tasks are in private subnets with no NAT gateway, you need VPC endpoints for:
- `com.amazonaws.REGION.ecr.api`
- `com.amazonaws.REGION.ecr.dkr`
- `com.amazonaws.REGION.s3`
- `com.amazonaws.REGION.secretsmanager`
- `com.amazonaws.REGION.logs`

### 10.4 Existing S3 Buckets

Pass your bucket names as CloudFormation parameters — no code changes needed. The pipeline references them via environment variables.

| CloudFormation Parameter | Your Bucket | Used For |
|---|---|---|
| `SourceBucket` | source ZIP bucket | Pipeline trigger source |
| `ArtifactBucket` | pipeline artifacts bucket | CodePipeline inter-stage artifacts |
| `RawLogsBucket` | raw logs bucket | Lambda package storage + app log storage |

---

## 11. Common Errors & Debugging

### 11.1 ECS Service Deployment Failures

**Error: `CannotPullContainerError`**

Cause: ECS task can't reach ECR.

Fixes:
1. Confirm ECS task is in a subnet with NAT Gateway or ECR VPC endpoints
2. Check the task execution role has `ecr:GetAuthorizationToken` permission
3. Check Security Group allows outbound HTTPS (443) traffic

```bash
# Check task failure reason
aws ecs describe-tasks \
  --cluster log-aggregator-cluster \
  --tasks TASK_ARN \
  --region ap-south-1 \
  --query 'tasks[0].containers[0].reason'
```

**Error: `ResourceInitializationError: unable to pull secrets`**

Cause: ECS can't read from Secrets Manager.

Fix: Ensure the execution role has `secretsmanager:GetSecretValue` on `arn:aws:secretsmanager:REGION:ACCOUNT:secret:log-aggregator/bedrock*` and that the secret ARN in the task definition matches exactly (including the `-XXXXXX` suffix that Secrets Manager appends).

```bash
# Get the full secret ARN with suffix
aws secretsmanager describe-secret \
  --secret-id log-aggregator/bedrock \
  --region ap-south-1 \
  --query 'ARN'
```

Update the `valueFrom` in all 3 task definition JSONs to use the full ARN.

**Error: `Health checks failing`**

The health check hits `http://localhost:5000/api/status`. If the container starts slowly:
1. Increase `startPeriod` in the health check (currently `60` seconds — raise to `120` if needed)
2. Check app logs in CloudWatch: `/ecs/log-aggregator/app`

---

### 11.2 CodeDeploy Blue/Green Failures

**Error: `The deployment failed because no instances were found`**

Cause: CodeDeploy deployment group is referencing an ECS service that doesn't exist yet or is in the wrong cluster.

Fix: Verify ECS cluster name and service name exactly match what's in the deployment group configuration.

**Error: `Deployment group references a target group that does not exist`**

Fix: Target group names in the CloudFormation template must exactly match your ALB's target group names. Update `log-aggregator-app-tg-blue` etc. in `codepipeline.yml` to your actual target group names.

**Error: `The ECS service must use the CODE_DEPLOY deployment controller`**

Fix: You cannot change the deployment controller on an existing service. Delete and recreate the service with `DeploymentController: CODE_DEPLOY`.

**BeforeAllowTraffic / AfterAllowTraffic hook failures:**

The appspec hooks reference Lambda functions `log-aggregator-pretraffic-hook` and `log-aggregator-posttraffic-hook`. These must exist in your account or the deployment will fail.

Quick fix — remove the hooks entirely from `buildspec-deploy.yml` (in the 3 rendered appspec heredocs) if you don't have these Lambda functions yet:

```yaml
# In buildspec-deploy.yml, change each rendered appspec from:
Hooks:
  - BeforeAllowTraffic: "log-aggregator-pretraffic-hook"
  - AfterAllowTraffic: "log-aggregator-posttraffic-hook"

# To (remove hooks entirely):
Hooks: []
```

---

### 11.3 CodeBuild Failures

**Error: `Tests failed — aborting build`**

Unit tests run before Docker builds. Check the test output in the build log.

```bash
aws logs tail /aws/codebuild/log-aggregator-build \
  --region ap-south-1 \
  --since 1h
```

**Error: `denied: Your authorization token has expired`**

The ECR login in `pre_build` must run before any Docker operations. This is already in the buildspec. If you see this, it usually means the CodeBuild role is missing ECR permissions.

**Error: `Cannot find Dockerfile`**

The buildspec uses `dockerfiles/Dockerfile.app` etc. Confirm the directory structure in your ZIP matches:
```
log-aggregator/
  dockerfiles/
    Dockerfile.app
    Dockerfile.processor
    Dockerfile.dashboard
    Dockerfile.chatbot
  buildspec.yml
  ...
```

**Error: `Error reading secret from Secrets Manager`**

buildspec.yml pulls `BEDROCK_AGENT_ID` and `BEDROCK_AGENT_ALIAS_ID` from Secrets Manager at build time (the `secrets-manager` block). Confirm:
1. The secret `log-aggregator/bedrock` exists in the same region
2. The CodeBuild role has `secretsmanager:GetSecretValue` on that secret

---

### 11.4 Debugging with CloudWatch Logs

**ECS container logs:**
```bash
# App service logs
aws logs tail /ecs/log-aggregator/app \
  --region ap-south-1 \
  --follow

# Dashboard logs
aws logs tail /ecs/log-aggregator/dashboard \
  --region ap-south-1 \
  --follow

# Chatbot logs
aws logs tail /ecs/log-aggregator/chatbot \
  --region ap-south-1 \
  --follow
```

**CodeDeploy deployment events:**
```bash
aws codedeploy get-deployment \
  --deployment-id DEPLOYMENT_ID \
  --region ap-south-1 \
  --query 'deploymentInfo.{status:status,errorInformation:errorInformation}'
```

**CloudFormation stack events (for template errors):**
```bash
aws cloudformation describe-stack-events \
  --stack-name log-aggregator-pipeline \
  --region ap-south-1 \
  --query 'StackEvents[?ResourceStatus==`CREATE_FAILED`].[LogicalResourceId,ResourceStatusReason]'
```

---

## Deployment Order Summary

Execute phases in this exact order:

```
Phase 0  →  Edit all files (ACCOUNT_ID, REGION, listener ARNs, target group names)
Phase 1  →  Create Secrets Manager secret; enable S3 versioning; verify target groups
Phase 2  →  Register ECS task definitions; create ECS services (with CODE_DEPLOY controller)
Phase 3  →  Deploy codepipeline.yml via CloudFormation (creates CodeBuild + CodeDeploy + Pipeline)
Phase 5  →  Create Lambda function + "live" alias (one-time manual step)
Phase 4  →  Upload log-ag-main.zip to source S3 bucket → pipeline auto-triggers
Phase 6  →  Verify: CodeBuild logs → ECR images → ECS services running → Lambda updated
```

---

*Generated for log-aggregator codebase | April 2026*
