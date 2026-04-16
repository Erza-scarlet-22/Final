# Log Aggregator Enhancement Design
## Multi-Agent Auto-Remediation System

---

## Architecture Overview

```
Dummy App (ECS Fargate)
    │ generates known errors
    │ writes logs → S3 raw-logs
    ↓
Lambda Processor → CSV/JSON → S3 processed
    ↓
Dashboard (existing) → User clicks error row → Chatbot opens
    ↓
Bedrock Orchestrator Agent
    ├── ServiceNow Agent     → creates incident ticket
    ├── SSL Agent            → ACM cert provision/rotate
    ├── Password Reset Agent → Secrets Manager rotate
    ├── DB Agent             → RDS storage/compute scale
    └── Compute Agent        → ECS desired count scale up
    ↓
Dummy App receives fix → writes resolved log → Dashboard updates
```

---

## Part 1 — The Dummy Application

### What It Is
A small Flask app called `dummy-infra-app` running on ECS Fargate.
It simulates a real infrastructure application with known, fixable errors.

### Error Types It Generates

| Error | HTTP Code | Log Message | What Fix Does |
|---|---|---|---|
| SSL cert expired | 495 | `SSL certificate expired for domain api.dummy-app.internal` | ACM issues new cert, app reloads |
| SSL cert expiring soon | 200 | `SSL certificate expires in 7 days` | ACM rotates cert proactively |
| Password reset required | 401 | `Service account password expired, authentication failed` | Secrets Manager rotates secret |
| DB storage critical | 507 | `Database storage at 92% capacity, writes may fail` | RDS modifies storage allocation |
| Compute overload | 503 | `CPU at 95%, memory at 88%, dropping requests` | ECS increases desired count |
| DB connection timeout | 504 | `RDS connection pool exhausted, timeout after 30s` | RDS modifies instance class |

### Dummy App File Structure

```
dummy-infra-app/
  app.py                    ← Flask app with error endpoints
  error_simulator.py        ← Generates known error log entries
  log_shipper.py            ← Uploads logs to S3 raw-logs bucket
  requirements.txt
  Dockerfile
  config/
    app_config.json         ← Holds cert ARN, DB endpoint, service account name
```

### Key Endpoints

```
GET  /health                     → returns app status
GET  /api/dummy/status           → returns current error state
POST /api/dummy/trigger-error    → triggers a specific error type
GET  /api/dummy/errors           → lists all active errors
POST /api/dummy/resolve/{type}   → marks error as resolved (called by agents)
```

### How It Ships Logs
Every 60 seconds `log_shipper.py` uploads the current log file to:
```
s3://log-aggregator-raw-logs-{account}-prod/raw-logs/dummy-app-{timestamp}.log
```
This triggers your existing Lambda processor automatically.

---

## Part 2 — Bedrock Agent Architecture

### Agent 1 — Orchestrator Agent (Main chatbot)

This is your EXISTING chatbot agent, enhanced with new capabilities.

**What changes:** Add action groups that let it call sub-agents instead of
just answering questions.

**New system prompt addition:**
```
When a user asks to FIX an error (not just explain it), you must:
1. ALWAYS create a ServiceNow incident first via the servicenow_action_group
2. Then call the appropriate remediation agent based on error type:
   - SSL/certificate errors → call ssl_remediation_action_group
   - Password/auth errors → call password_reset_action_group
   - Database storage/connection errors → call db_remediation_action_group
   - CPU/memory/compute errors → call compute_remediation_action_group
3. Report back to the user with: ticket number, action taken, and expected resolution time
```

**New action groups to add:**

| Action Group | Lambda Function | What It Does |
|---|---|---|
| `servicenow_action_group` | `servicenow-integration-lambda` | Creates ServiceNow incident |
| `ssl_remediation_action_group` | `ssl-remediation-lambda` | Handles cert issues |
| `password_reset_action_group` | `password-reset-lambda` | Handles auth issues |
| `db_remediation_action_group` | `db-remediation-lambda` | Handles DB issues |
| `compute_remediation_action_group` | `compute-remediation-lambda` | Handles scaling issues |

---

## Part 3 — The 5 Lambda Action Functions

### Lambda 1 — ServiceNow Integration

**Function name:** `log-aggregator-servicenow-lambda`

**Triggered by:** Orchestrator agent for EVERY error fix request

**What it does:**
1. Receives error details (type, description, status code, count, last seen)
2. Calls ServiceNow REST API to create an incident
3. Returns ticket number to orchestrator agent

**ServiceNow API call:**
```python
POST https://YOUR_INSTANCE.service-now.com/api/now/table/incident
{
  "short_description": "Auto-detected: SSL Certificate Expired on dummy-infra-app",
  "description": "Error detected by Log Aggregator. Status: 495. Count: 12. Last seen: 2026-04-06",
  "urgency": "2",
  "impact": "2",
  "category": "infrastructure",
  "assignment_group": "AWS Operations",
  "u_source": "AWS Log Aggregator",
  "u_aws_error_code": "9001",
  "u_remediation_status": "automated"
}
```

**Credentials storage:**
```
AWS Secrets Manager secret: servicenow/credentials
{
  "instance_url": "https://YOUR_INSTANCE.service-now.com",
  "username": "log-aggregator-svc",
  "password": "YOUR_PASSWORD"
}
```

---

### Lambda 2 — SSL Remediation

**Function name:** `log-aggregator-ssl-lambda`

**Two scenarios:**

**Scenario A — Cert expired (495 error):**
```
1. Check ACM for existing cert for the domain
2. If cert exists but expired → request new cert (ACM.request_certificate)
3. If using DNS validation → add CNAME to Route53
4. Store new cert ARN in Secrets Manager: dummy-app/ssl-cert-arn
5. Call dummy app endpoint POST /api/dummy/resolve/ssl
   → app reads new cert ARN from Secrets Manager
   → app reloads with new cert
6. Write resolution log to S3
7. Return: { "action": "cert_renewed", "cert_arn": "...", "expires": "..." }
```

**Scenario B — Cert expiring soon (proactive rotation):**
```
1. Request new cert in ACM
2. Wait for validation (DNS auto-validation via Route53)
3. Update dummy app config in Parameter Store
4. Return: { "action": "cert_rotated_proactively", "days_until_expiry": 90 }
```

**AWS services used:**
- `acm:RequestCertificate`
- `acm:DescribeCertificate`
- `acm:ListCertificates`
- `route53:ChangeResourceRecordSets`
- `secretsmanager:PutSecretValue`
- `ssm:PutParameter`

---

### Lambda 3 — Password Reset

**Function name:** `log-aggregator-password-reset-lambda`

**What it does:**
```
1. Identify which service account needs rotation
   (from error context: "service account password expired")
2. Generate new secure password
3. Update in Secrets Manager: dummy-app/service-account-credentials
4. Call dummy app endpoint POST /api/dummy/resolve/password
   → app reads new password from Secrets Manager
   → reconnects with new credentials
5. Optionally update RDS user password if DB-related
6. Return: { "action": "password_rotated", "secret_arn": "...", "next_rotation": "90 days" }
```

**AWS services used:**
- `secretsmanager:RotateSecret`
- `secretsmanager:PutSecretValue`
- `secretsmanager:GetSecretValue`
- `rds:ModifyDBInstance` (if DB password)

---

### Lambda 4 — DB Remediation

**Function name:** `log-aggregator-db-lambda`

**Two scenarios:**

**Scenario A — Storage critical (507 error):**
```
1. Describe current RDS instance
2. Get current allocated storage
3. Increase by 20% or to next tier
4. aws rds modify-db-instance --allocated-storage NEW_SIZE
5. Return: { "action": "storage_increased", "old_gb": 100, "new_gb": 120 }
```

**Scenario B — Connection pool exhausted (504 error):**
```
1. Check current instance class
2. Modify to next tier up (e.g. db.t3.medium → db.t3.large)
3. Apply immediately (not during maintenance window)
4. Return: { "action": "instance_upgraded", "old_class": "db.t3.medium", "new_class": "db.t3.large" }
```

**AWS services used:**
- `rds:DescribeDBInstances`
- `rds:ModifyDBInstance`
- `cloudwatch:GetMetricStatistics`

---

### Lambda 5 — Compute Remediation

**Function name:** `log-aggregator-compute-lambda`

**What it does:**
```
1. Identify which ECS service is overloaded
   (from error context: dummy-infra-app service)
2. Get current desired count
3. Increase desired count by 1 (or double it)
4. aws ecs update-service --desired-count NEW_COUNT
5. Optionally update Auto Scaling target tracking policy
6. Return: { "action": "scaled_up", "old_count": 1, "new_count": 2, "service": "..." }
```

**AWS services used:**
- `ecs:DescribeServices`
- `ecs:UpdateService`
- `application-autoscaling:PutScalingPolicy`
- `cloudwatch:GetMetricStatistics`

---

## Part 4 — ServiceNow ↔ AWS Integration

### Two-Way Integration

```
AWS Log Aggregator → ServiceNow (creating tickets)
ServiceNow → AWS (updating ticket status when fix is done)
```

### AWS → ServiceNow Flow
When orchestrator agent decides to fix an error:
1. Lambda calls ServiceNow REST API → creates incident
2. Returns INC number (e.g. INC0012345)
3. Orchestrator reports ticket number to user in chat
4. Lambda calls ServiceNow again when fix is complete → updates ticket to Resolved

### ServiceNow → AWS Flow (optional webhook)
ServiceNow can be configured to call an API Gateway webhook when a ticket
is manually resolved or updated, which can trigger additional AWS actions.

```
ServiceNow (Business Rule on incident update)
    → POST https://API_GATEWAY_URL/servicenow-webhook
    → Lambda receives update
    → Updates status in DynamoDB
    → Dashboard shows ticket status
```

### ServiceNow Credentials in AWS
```
Secrets Manager secret: servicenow/credentials
{
  "instance_url": "https://devXXXXXX.service-now.com",
  "username": "aws-integration-user",
  "password": "XXXX",
  "client_id": "XXXX",       ← if using OAuth
  "client_secret": "XXXX"    ← if using OAuth
}
```

---

## Part 5 — Dashboard Changes Needed

### New UI Elements in dashboard.html

1. **"Fix This Error" button** on each error row
   - Sends `POST /api/chat-insights` with message: "Please fix this error automatically"
   - Opens chat panel automatically

2. **Remediation status badge** on error rows
   - Shows: `Pending` / `In Progress` / `Fixed` / `Ticket: INC0012345`

3. **Action log panel** in the chat modal
   - Shows each step the agent took: "Creating ServiceNow ticket...", "Rotating SSL cert..."

4. **ServiceNow ticket link** in chat response
   - Clickable link to the ServiceNow incident

### New API endpoint in dashboard_blueprint.py
```python
POST /api/fix-error
{
  "error": { ...error row data... },
  "auto_fix": true
}
→ calls orchestrator agent with fix instruction
→ returns { "ticket": "INC0012345", "actions": [...], "status": "in_progress" }
```

---

## Part 6 — Files You Need to Create/Change

### New Files

```
dummy-infra-app/
  app.py                          ← NEW: dummy Flask app
  error_simulator.py              ← NEW: generates error logs
  log_shipper.py                  ← NEW: ships logs to S3
  Dockerfile                      ← NEW

lambda-actions/
  servicenow_lambda/
    handler.py                    ← NEW
    requirements.txt              ← NEW
  ssl_lambda/
    handler.py                    ← NEW
    requirements.txt              ← NEW
  password_reset_lambda/
    handler.py                    ← NEW
    requirements.txt              ← NEW
  db_lambda/
    handler.py                    ← NEW
    requirements.txt              ← NEW
  compute_lambda/
    handler.py                    ← NEW
    requirements.txt              ← NEW

bedrock/
  orchestrator_agent_prompt.txt   ← NEW: updated system prompt
  action_group_schemas/
    servicenow.json               ← NEW: OpenAPI schema for action group
    ssl.json                      ← NEW
    password_reset.json           ← NEW
    db_remediation.json           ← NEW
    compute_remediation.json      ← NEW
```

### Existing Files to Change

```
Dashboard/dashboard_blueprint.py  ← add /api/fix-error endpoint
Dashboard/templates/dashboard.html ← add Fix button + status badge + action log
Dashboard/bedrock_chat_service.py  ← pass fix intent to agent
3.yaml (CloudFormation)            ← add new Lambda functions + IAM roles
```

---

## Part 7 — Bedrock Agent Setup Steps

### Step 1 — Create Action Group Lambda Functions
Deploy all 5 Lambda functions above to AWS.

### Step 2 — Create OpenAPI Schema for Each Action Group
Each action group needs an OpenAPI 3.0 schema describing what the Lambda accepts.

Example for ServiceNow action group:
```json
{
  "openapi": "3.0.0",
  "info": { "title": "ServiceNow Integration", "version": "1.0" },
  "paths": {
    "/create-incident": {
      "post": {
        "operationId": "createIncident",
        "description": "Creates a ServiceNow incident for a detected error",
        "requestBody": {
          "content": {
            "application/json": {
              "schema": {
                "type": "object",
                "properties": {
                  "error_type": { "type": "string" },
                  "error_description": { "type": "string" },
                  "status_code": { "type": "integer" },
                  "count": { "type": "integer" },
                  "last_seen": { "type": "string" },
                  "urgency": { "type": "string", "enum": ["1","2","3"] }
                }
              }
            }
          }
        }
      }
    }
  }
}
```

### Step 3 — Update Orchestrator Agent in Bedrock Console
1. Bedrock → Agents → your existing chatbot agent
2. Add each action group with its schema + Lambda ARN
3. Update system prompt to include fix routing instructions
4. Create new alias and update BEDROCK_AGENT_ALIAS_ID in Secrets Manager

### Step 4 — Test Each Action Group Individually
Before wiring them all together, test each Lambda directly with a sample event.

---

## Part 8 — Deployment Order

```
1.  Deploy dummy-infra-app to ECS (new CloudFormation stack or add to 3.yaml)
2.  Verify dummy app generates logs and they appear in dashboard
3.  Deploy 5 action Lambda functions
4.  Store ServiceNow credentials in Secrets Manager
5.  Create OpenAPI schemas for each action group
6.  Add action groups to Bedrock orchestrator agent
7.  Update orchestrator agent system prompt
8.  Test: trigger SSL error from dummy app → appears in dashboard →
         click Fix → agent creates ticket + rotates cert → resolved
9.  Add Fix button to dashboard HTML
10. Test end-to-end for all 5 error types
```

---

## Part 9 — IAM Permissions Needed

### Orchestrator Agent Execution Role (add to existing)
```json
{
  "Action": [
    "lambda:InvokeFunction"
  ],
  "Resource": [
    "arn:aws:lambda:*:*:function:log-aggregator-servicenow-lambda",
    "arn:aws:lambda:*:*:function:log-aggregator-ssl-lambda",
    "arn:aws:lambda:*:*:function:log-aggregator-password-reset-lambda",
    "arn:aws:lambda:*:*:function:log-aggregator-db-lambda",
    "arn:aws:lambda:*:*:function:log-aggregator-compute-lambda"
  ]
}
```

### SSL Lambda Role
```json
{
  "Action": [
    "acm:RequestCertificate",
    "acm:DescribeCertificate",
    "acm:ListCertificates",
    "route53:ChangeResourceRecordSets",
    "secretsmanager:PutSecretValue",
    "secretsmanager:GetSecretValue",
    "ssm:PutParameter"
  ],
  "Resource": "*"
}
```

### ServiceNow Lambda Role
```json
{
  "Action": [
    "secretsmanager:GetSecretValue"
  ],
  "Resource": "arn:aws:secretsmanager:*:*:secret:servicenow/*"
}
```

---

## Summary: What to Build Next (in order)

1. **Dummy app** — small Flask app, 5-6 error endpoints, log shipper to S3
2. **ServiceNow Lambda** — call ServiceNow REST API, store creds in Secrets Manager
3. **SSL Lambda** — ACM cert operations
4. **Password Reset Lambda** — Secrets Manager rotation
5. **DB Lambda** — RDS modify
6. **Compute Lambda** — ECS scale
7. **Update Bedrock agent** — add all action groups + new system prompt
8. **Dashboard Fix button** — one new button per error row
9. **Test end-to-end**

Tell me which part you want to start with and I will give you the complete code.
