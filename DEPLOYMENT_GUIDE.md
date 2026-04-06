# Log Aggregator – Deployment Guide
## CloudFormation Stack: `log-aggregator-infra.yaml`

---

## ZIP Folder Structure (what to upload to S3)

Your ZIP must have this exact layout so CodeBuild can find all Dockerfiles and source code:

```
log-aggregator.zip
└── log-aggregator/               ← top-level folder (matches your existing repo)
    ├── dockerfiles/
    │   ├── Dockerfile.app
    │   ├── Dockerfile.dashboard
    │   └── Dockerfile.chatbot
    ├── Application/
    │   ├── app.py
    │   ├── logger.py
    │   ├── requirements.txt
    │   └── routes/
    │       ├── __init__.py
    │       ├── core.py
    │       ├── simulator.py
    │       ├── infrastructure.py
    │       ├── payments.py
    │       ├── auth.py
    │       ├── orders.py
    │       └── users.py
    ├── Dashboard/
    │   ├── dashboard_blueprint.py
    │   ├── dashboard_data_service.py
    │   ├── dashboard_pdf_service.py
    │   ├── bedrock_chat_service.py
    │   └── templates/
    │       └── dashboard.html
    ├── Conversion/               ← if used by your app
    ├── tests/
    └── requirements.txt          ← root-level requirements for CodeBuild test phase
```

### How to create the ZIP

**On Mac/Linux:**
```bash
cd /path/to/your/project
zip -r log-aggregator.zip log-aggregator/ -x "*.pyc" -x "__pycache__/*" -x ".git/*" -x "*.egg-info/*"
```

**On Windows:**
Right-click the `log-aggregator` folder → Send to → Compressed (zipped) folder

---

## Step-by-Step Deployment

### Step 1 – Deploy the CloudFormation Stack

1. Open **AWS Console → CloudFormation → Create Stack → With new resources**
2. Upload `log-aggregator-infra.yaml`
3. Fill in parameters:
   | Parameter | What to put |
   |-----------|-------------|
   | EnvironmentName | `prod` (or `dev`) |
   | VpcId | Your existing VPC |
   | PublicSubnetIds | 2 public subnets for ALB |
   | PrivateSubnetIds | 2 private subnets for ECS |
   | SourceProvider | `S3` |
   | S3SourceBucket | Leave blank for now (you'll fill after stack creates the bucket) |
   | S3SourceKey | `log-aggregator.zip` |
   | BedrockAgentId | Your agent ID (or leave placeholder) |
   | BedrockAgentAliasId | Your alias ID (or `TSTALIASID`) |
   | SchedulerState | `DISABLED` (enable later) |

4. Acknowledge IAM capabilities → **Create Stack**
5. Wait ~5 minutes for stack to complete

---

### Step 2 – Upload Your ZIP to S3

After the stack creates, go to **S3 → look for bucket named:**
`log-aggregator-artifacts-<accountid>-prod`

Upload your `log-aggregator.zip` to the root of that bucket.

---

### Step 3 – Run CodeBuild (First Build)

1. Go to **CodeBuild → Projects → `log-aggregator-build-prod`**
2. Click **Start Build**
3. Watch the build logs — it will:
   - Log into ECR
   - Find and flatten your source ZIP if nested
   - Build all 3 Docker images
   - Push to ECR
   - Force-deploy all 3 ECS services

> First build takes ~8–12 minutes (no Docker cache yet). Subsequent builds are faster.

---

### Step 4 – Update CloudFormation with S3 Bucket Name

Go back to CloudFormation → Update Stack → and set:
- `S3SourceBucket` = `log-aggregator-artifacts-<accountid>-prod`

This makes future builds work automatically when you re-upload the ZIP.

---

### Step 5 – Update Bedrock Agent ID (if not set during stack creation)

1. Go to **Secrets Manager → `log-aggregator/bedrock-prod`**
2. Click **Retrieve secret value → Edit**
3. Update `BEDROCK_AGENT_ID` and `BEDROCK_AGENT_ALIAS_ID` with your real values
4. Force-redeploy the chatbot service:
   ```bash
   aws ecs update-service \
     --cluster log-aggregator-cluster-prod \
     --service log-aggregator-chatbot-svc-prod \
     --force-new-deployment
   ```

---

## Access Your Application

After deployment, find the ALB URL in:
**CloudFormation → Outputs → AlbUrl**

| Service | URL |
|---------|-----|
| App (backend) | `http://<alb-dns>/` |
| Dashboard | `http://<alb-dns>/dashboard` |
| Chatbot | `http://<alb-dns>/chatbot` |

---

## Manual Setup Required Before First Deploy

| Item | Action |
|------|--------|
| VPC + Subnets | Must exist with proper routing (public subnets need IGW, private subnets need NAT Gateway) |
| Bedrock Agent | Create in AWS Console → Bedrock → Agents, get Agent ID + Alias ID |
| GitHub PAT | Only if using GitHub source — create token at github.com/settings/tokens |
| NAT Gateway | Private subnets must have NAT so ECS tasks can pull ECR images |

---

## Updating Your Application

1. Make code changes
2. Re-ZIP your project (same structure)
3. Upload new ZIP to S3 bucket (same key: `log-aggregator.zip`)
4. Go to CodeBuild → Start Build
5. Done — all 3 services redeploy automatically

---

## Enabling Scheduled Builds

To auto-trigger CodeBuild on a schedule (e.g., daily rebuild):
1. Go to CloudFormation → Update Stack
2. Set `SchedulerState` = `ENABLED`
3. Adjust `SchedulerExpression` if needed (e.g., `rate(1 day)`)
