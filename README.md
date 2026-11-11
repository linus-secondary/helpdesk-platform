# IT Helpdesk Platform

A secure IT service-desk (ticketing) platform built from Python microservices on AWS.

- **Login:** Keycloak with MFA for IT staff.
- **AI:** ticket triage with Claude on Amazon Bedrock (schema-validated structured output), plus a knowledge-base help assistant.
- **Operations:** SLA tracking with alerts, and an audit trail of every ticket change.
- **Edge protection:** AWS WAF in front of an Application Load Balancer.
- **Encryption and threat detection:** KMS everywhere, GuardDuty and CloudTrail.
- **Observability:** Prometheus/Grafana monitoring and ELK logging.
- **Delivery:** infrastructure in **Terraform and CloudFormation**, delivered with a **Jenkins** CI/CD pipeline.

Everything except AWS is free and open source. You can run the whole platform on your laptop at no cost.

---

## Table of contents

1. [Architecture](#1-architecture)
2. [Tech stack](#2-tech-stack)
3. [Project structure](#3-project-structure)
4. [Prerequisites](#4-prerequisites)
5. [Run locally with Docker Compose](#5-run-locally-with-docker-compose-fastest)
6. [Using the app (employee, agent, admin)](#6-using-the-app)
7. [AI triage and help assistant (Amazon Bedrock)](#7-ai-triage-and-help-assistant-amazon-bedrock)
8. [SLA engine and notifications](#8-sla-engine-and-notifications)
9. [Monitoring and logging](#9-monitoring-prometheus--grafana-and-logging-elk)
10. [Run on local Kubernetes (minikube)](#10-run-on-local-kubernetes-minikube)
11. [Deploy to AWS (Terraform + CloudFormation)](#11-deploy-to-aws-terraform--cloudformation)
12. [CI/CD with Jenkins](#12-cicd-with-jenkins)
13. [Testing](#13-testing)
14. [Security design](#14-security-design)
15. [API reference](#15-api-reference)
16. [Cost control](#16-cost-control)
17. [Troubleshooting](#17-troubleshooting)

---

## 1. Architecture

```
                                   Internet
                                      |
                   +------------------v------------------+
                   |  AWS WAF (OWASP rules, SQLi, bad    |   CloudFormation: CloudTrail, GuardDuty,
                   |  inputs, IP reputation, rate limit) |   IAM Access Analyzer, Security Hub (opt.)
                   +------------------+------------------+
                                      |
                   +------------------v------------------+
                   |  Application Load Balancer (ALB)    |   created by the AWS Load Balancer
                   |   /auth -> Keycloak   / -> gateway  |   Controller from a Kubernetes Ingress
                   +--------+--------------------+-------+
                            |                    |
                  +---------v------+    +--------v---------+
  Amazon EKS      |   Keycloak     |    |   API gateway    |  rate limit (Redis), security headers,
  (private        | OIDC, MFA/TOTP,|    |  + web frontend  |  blocks /internal/*, correlation IDs
   subnets,       | roles, events  |    +--------+---------+
   2 AZs)         +----------------+             |
                         ^ JWKS       +----------+-----------+---------------+
                         |            |          |           |               |
                         +------- tickets       sla     notifications     chatbot
                                      | \        |           |    \          |  \
                                      |  +-- internal REST --+     SNS       |   Amazon Bedrock
                                      |          |           |   (email)     |   (Claude: triage
                                      +----------+-----------+---------------+    + help answers)
                                                 |
                                Amazon RDS PostgreSQL (private, KMS-encrypted)

   Secrets Manager (DB URL, KMS) - ECR (images, KMS, scan on push) - VPC Flow Logs - CloudWatch alarms
   Prometheus + Grafana (metrics, SLA gauges, alerts) - ELK (JSON logs with correlation IDs)
```

| Service | Responsibility |
|---|---|
| **gateway** | Public entry point. Serves the web app, routes `/api/<service>/...`, applies Redis rate limiting and security headers (CSP, X-Frame-Options), and refuses `/internal/*`. |
| **tickets** | Ticket lifecycle, comments and internal notes, role-based access, and an audit trail (`ticket_events`). Calls the chatbot service for triage. |
| **sla** | SLA policy per priority. A background watcher flags first-response and resolution breaches, alerts agents and exposes compliance reports and Prometheus gauges. |
| **notifications** | In-app notifications. Urgent alerts are also published to **Amazon SNS** (email/SMS). |
| **chatbot** | AI triage (Claude, JSON-schema structured output, with a keyword-rule fallback) and a knowledge-base help assistant (RAG). |

### What happens when an employee raises a ticket

```
employee -> gateway -> tickets --(internal)--> chatbot: triage
                                                 |-- Bedrock enabled: Claude returns {category, priority, suggestion}
                                                 |   (validated against an enum schema; invalid -> rules)
                                                 '-- otherwise: keyword rules + best knowledge-base article
                         tickets: SLA deadlines from priority (P1: 15 min response / 4 h resolution)
                         tickets: insert ticket + audit event (one transaction)
                         tickets --(internal)--> notifications: "New P1 Security ticket" -> agents (+ SNS for P1)
                  sla watcher (every 60 s): deadline missed? -> flag once, audit, alert agents (+ SNS for P1/P2)
```

---

## 2. Tech stack

| Area | Technology |
|---|---|
| Backend | Python 3.12, FastAPI, REST APIs, microservices, background workers |
| Data | PostgreSQL 16 (constraints, transactions, advisory locks, full-text search), Redis 7 |
| Identity | Keycloak 26: OAuth 2.0 / OpenID Connect, PKCE, JWT (RS256), **TOTP MFA for agents/admins**, RBAC, brute-force protection, password policy, login event auditing |
| AI | Claude on Amazon Bedrock (Anthropic Python SDK), structured outputs (JSON schema), RAG over a knowledge base |
| Frontend | HTML, CSS, JavaScript (ES modules), keycloak-js |
| Containers | Docker (non-root, health check), Docker Compose |
| Orchestration | Kubernetes, Kustomize overlays, HPA, PodDisruptionBudgets, NetworkPolicies, probes, security contexts |
| AWS | VPC (2 AZs, flow logs), EKS, RDS, ECR, ALB + AWS Load Balancer Controller, **WAF**, **KMS**, Secrets Manager, **SNS**, CloudWatch, **CloudTrail**, **GuardDuty**, IAM Access Analyzer, Security Hub (optional), IAM/IRSA, Budgets, Bedrock |
| IaC | **Terraform** (network, compute, data, security) + **CloudFormation** (account security baseline) |
| CI/CD | **Jenkins** (Jenkinsfile): Ruff, pytest, Checkov, cfn-lint, Trivy, ECR push, EKS deploy, smoke test |
| Observability | Prometheus (+ business metrics), Grafana, alert rules, ELK, correlation IDs |

---

## 3. Project structure

```
helpdesk-platform/
├── app/                          # one Docker image for every service (SERVICE=<name>)
│   ├── main.py  common.py        # service selector; auth, logging, metrics, DB helpers
│   ├── gateway.py                # API gateway + security headers + static frontend
│   ├── tickets_service.py        # tickets, comments, RBAC, audit trail
│   ├── sla_service.py            # SLA watcher, reports, Prometheus gauges
│   ├── notifications_service.py  # in-app + Amazon SNS alerts
│   ├── chatbot_service.py  llm.py  # AI triage + KB assistant, Bedrock client
│   ├── rules.py                  # SLA policy, lifecycle, triage rules (pure, unit tested)
│   ├── schema.sql                # tables, constraints, indexes, knowledge-base articles
│   └── static/                   # web frontend
├── k8s/base/                     # Deployments, Services, HPA, PDB, NetworkPolicies, Redis, Keycloak (+ realm)
├── k8s/overlays/local/           # + PostgreSQL, dev secrets
├── k8s/overlays/aws/             # + ALB Ingress with WAF, IRSA, secrets from Secrets Manager
├── terraform/                    # VPC, EKS, RDS, ECR, KMS, WAF, SNS, IAM, LB controller, Budget
├── cloudformation/               # security baseline: CloudTrail, GuardDuty, Access Analyzer, Security Hub
├── monitoring/  logging/  jenkins/
├── scripts/                      # aws-deploy.sh, aws-destroy.sh, local-k8s-up.sh, smoke_test.py
├── tests/
├── docker-compose.yml  Jenkinsfile  .env.example
```

---

## 4. Prerequisites

| Tool | Needed for | Check |
|---|---|---|
| [Docker Desktop](https://www.docker.com/products/docker-desktop/) | everything | `docker version` |
| [Python 3.12+](https://www.python.org/downloads/) | tests, smoke test | `python --version` |
| [Git](https://git-scm.com/) (+ Git Bash on Windows) | `.sh` scripts | `git --version` |
| An authenticator app (Google/Microsoft Authenticator) | agent/admin login (MFA) | n/a |
| [kubectl](https://kubernetes.io/docs/tasks/tools/), [minikube](https://minikube.sigs.k8s.io/docs/start/) | Kubernetes | `kubectl version --client` |
| [Terraform 1.6+](https://developer.hashicorp.com/terraform/install), [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html) | AWS | `terraform version`, `aws --version` |

On Windows you can install them with `winget install Docker.DockerDesktop Kubernetes.kubectl Kubernetes.minikube Hashicorp.Terraform Amazon.AWSCLI`.

---

## 5. Run locally with Docker Compose (fastest)

```bash
git clone https://github.com/linus-secondary/helpdesk-platform.git
cd helpdesk-platform
cp .env.example .env            # Windows PowerShell: Copy-Item .env.example .env
docker compose up -d --build
```

Wait 2–4 minutes for Keycloak to import the realm, then check:

```bash
docker compose ps
python scripts/smoke_test.py http://localhost:8000      # needs: pip install httpx
```

The smoke test should end with `All 20 checks passed`.

| URL | What | Login |
|---|---|---|
| http://localhost:8000 | Helpdesk web app | see section 6 |
| http://localhost:8080/auth/admin | Keycloak admin console | `admin` / `admin` |
| http://localhost:3000 | Grafana | `admin` / `admin` |
| http://localhost:9090 | Prometheus | none |

> Run only one of the two projects at a time locally: both use ports 8000, 8080, 3000 and 9090.

To stop, run `docker compose down`. To also delete the data, run `docker compose down -v`.

---

## 6. Using the app

| User | Password | Role | MFA |
|---|---|---|---|
| `employee1` | `Employee@1234` | user | no |
| `agent1` | `Agent@12345` | agent | **yes**: scan the QR code with your authenticator app on first login |
| `admin1` | `Admin@12345` | agent + admin | **yes** |

**As an employee** (`employee1`):
1. Open **New ticket** and start typing a title such as "VPN not connecting". Matching knowledge-base articles appear as you type, so many issues get solved without a ticket.
2. Submit the ticket. It is triaged automatically (category, priority and a suggested fix), and its SLA deadlines are shown.
3. Use **Help assistant** to ask questions answered from the knowledge base.
4. When an agent resolves your ticket, click **Confirm fixed** or **Still broken (reopen)**.

**As an agent** (`agent1`, after setting up MFA):
1. **Tickets** shows the whole queue sorted by priority. Use the filters or *Assigned to me*.
2. Open a ticket, then *Assign to me*, change status or priority, and reply. Your first public reply records the **first response time**. *Internal notes* are hidden from the employee.
3. Expand **Audit trail** to see every change, who made it and when.
4. Click the **Notifications** button to see new tickets and SLA breach alerts.

**As an admin** (`admin1`): everything an agent can do, plus the **SLA dashboard** (compliance %, average resolution time, AI-triaged count, breached tickets).

To see an SLA breach quickly, create a P1 ticket (for example "Ransomware on the whole team's laptops"), set `SLA_CHECK_SECONDS=10` in `.env`, run `docker compose up -d sla`, and wait 15 minutes.

---

## 7. AI triage and help assistant (Amazon Bedrock)

With `BEDROCK_ENABLED=false` (the default) everything still works: triage uses keyword rules, and the assistant answers with the best-matching knowledge-base article. To use Claude:

1. In the AWS console, open **Amazon Bedrock → Model access** in your region and enable the Anthropic Claude model.
2. Run `aws configure` on your machine. The IAM user needs `bedrock:InvokeModel`.
3. In `.env`, set `BEDROCK_ENABLED=true`, `AWS_REGION=us-east-1` and `BEDROCK_MODEL_ID=anthropic.claude-opus-5-5`. A smaller Claude model (for example `anthropic.claude-haiku-5-5`) lowers the cost per ticket.
4. Run `docker compose up -d chatbot`. It reads `~/.aws` read-only.

**Design (in `app/chatbot_service.py`):**
- **Triage** uses *structured outputs*. Claude must return JSON matching a schema whose `category` and `priority` are enums, and the result is validated again in `rules.validate_triage`. If the output is invalid or Bedrock fails, the service falls back to keyword rules, so ticket creation never fails because of the AI.
- **Guard-rails in the prompt:** never ask for passwords, only safe steps with no admin rights, and for suspected security incidents always tell the user to disconnect and wait for the security team.
- **Help assistant (RAG):** PostgreSQL full-text search retrieves the top knowledge-base articles, and Claude answers only from them; otherwise it tells the user to raise a ticket.
- Every ticket records `triaged_by` (`ai`, `rules` or `agent`), so the SLA dashboard shows how many tickets the AI triaged and agents can correct it.
- On EKS only the chatbot pod can call Bedrock, through **IRSA**, with no keys stored anywhere.

---

## 8. SLA engine and notifications

| Priority | Meaning | First response | Resolution |
|---|---|---|---|
| P1 | Security incident / outage / many people blocked | 15 min | 4 h |
| P2 | One person blocked / deadline at risk | 1 h | 8 h |
| P3 | Degraded, workaround exists | 4 h | 24 h |
| P4 | Request / question | 8 h | 72 h |

- The **sla** service checks deadlines every `SLA_CHECK_SECONDS`. A PostgreSQL advisory lock guarantees only one pod runs the check, so scaling out is safe. Each breach is flagged once, written to the audit trail, and alerted to agents.
- P1/P2 breaches and new P1 tickets are also published to **Amazon SNS** when `SNS_TOPIC_ARN` is set (Terraform creates the topic and an email subscription; confirm the email AWS sends you).
- Prometheus gauges `helpdesk_open_tickets` and `helpdesk_sla_breached_open_tickets` feed Grafana and the `P1TicketBreachedSLA` alert.

---

## 9. Monitoring (Prometheus + Grafana) and logging (ELK)

- **Grafana** (http://localhost:3000): open **Dashboards → Platform → Services overview** to see requests/s, p95 latency, error rate, open tickets by priority and SLA breaches.
- **Alerts** (http://localhost:9090/alerts):
  - Technical: ServiceDown, HighErrorRate, HighLatencyP95.
  - Business: P1TicketBreachedSLA, TicketBacklogGrowing, LoginFailuresSpike.
- **Logs:** every service writes JSON with `correlation_id`. Run `docker compose logs -f tickets`, or start ELK with `docker compose --profile logging up -d`. Then in Kibana (http://localhost:5601), create the data view `app-logs-*`.
- **Keycloak auditing:** login, login-error and MFA events are stored. In the Keycloak admin console, open **Realm `helpdesk` → Events**.

---

## 10. Run on local Kubernetes (minikube)

```bash
bash scripts/local-k8s-up.sh
kubectl port-forward -n helpdesk svc/gateway 8000:80        # terminal 1
kubectl port-forward -n helpdesk svc/keycloak 8080:8080     # terminal 2
```

Open http://localhost:8000. The manifests include:
- Two replicas for the gateway and tickets services, with PodDisruptionBudgets.
- HPA.
- NetworkPolicies (backend isolated; Redis reachable only from the gateway).
- Non-root, read-only containers.
- Probes, plus requests and limits.

Clean up with `kubectl delete namespace helpdesk`.

---

## 11. Deploy to AWS (Terraform + CloudFormation)

> **This costs money while it runs** (roughly $0.30–0.40 per hour: EKS, NAT, nodes, ALB, RDS, WAF). Deploy, demo, destroy.

### 11.1 Infrastructure with Terraform (about 25 minutes)

```bash
aws configure                                   # admin IAM user, region us-east-1
cd terraform
cp terraform.tfvars.example terraform.tfvars    # set budget_email (also receives SNS alerts)
terraform init
terraform plan -out tfplan
terraform apply tfplan
```

| Resource | Security / reliability details |
|---|---|
| VPC | 2 AZs; public subnets only for the ALB; private subnets for nodes; isolated database subnets; **VPC Flow Logs** |
| EKS 1.31 | Private nodes (1–3 × t3.medium), Kubernetes Secrets encrypted with KMS, API/audit/authenticator logs, NetworkPolicy enforcement |
| AWS Load Balancer Controller | Installed with Helm; its IAM role is scoped by IRSA |
| RDS PostgreSQL 16 | Private, **KMS-encrypted**, 7-day backups, logs to CloudWatch; Multi-AZ standby optional (`db_multi_az = true`) |
| KMS | One customer-managed key with **automatic yearly rotation** for RDS, Secrets Manager, ECR and SNS |
| AWS WAF | AWS managed rules (Common/OWASP, SQL injection, known bad inputs, IP reputation) + per-IP rate limit; logs blocked requests to CloudWatch; alarm on block spikes |
| SNS | KMS-encrypted alert topic with an email subscription |
| IAM | Least privilege: chatbot → `bedrock:InvokeModel` only; notifications → `sns:Publish` on one topic only |
| Secrets Manager | `DATABASE_URL` with a random password |
| Budgets | Email at 80% / 100% of the monthly limit |

### 11.2 Account security baseline with CloudFormation

```bash
cd ..
aws cloudformation deploy \
  --stack-name helpdesk-platform-security \
  --template-file cloudformation/security-baseline.yaml \
  --parameter-overrides AlertTopicArn=$(terraform -chdir=terraform output -raw sns_topic_arn)
```

This creates:
- **CloudTrail**: multi-region, log-file validation, encrypted S3 bucket with public access blocked, TLS-only.
- **GuardDuty**: including EKS audit-log and S3 protection. High-severity findings go to SNS by email.
- **IAM Access Analyzer**.

Add `EnableSecurityHub=true` to turn on Security Hub (30-day free trial). If GuardDuty is already on in this region, add `CreateGuardDutyDetector=false`.

### 11.3 Deploy the application

```bash
bash scripts/aws-deploy.sh
python scripts/smoke_test.py http://<alb-hostname-printed-by-the-script>
```

The script:
1. Builds the image and pushes it to ECR.
2. Installs metrics-server.
3. Creates the **ALB Ingress with WAF attached**.
4. Waits for its hostname.
5. Writes secrets (from Secrets Manager) and IRSA role ARNs.
6. Updates the Keycloak redirect URLs.
7. Applies `k8s/overlays/aws`.

Test the WAF: `curl "http://<alb>/api/tickets/tickets?q=1%27%20OR%201=1--"` should return **403** from AWS WAF (SQL injection rule).

### 11.4 Destroy everything

```bash
bash scripts/aws-destroy.sh
```

This runs in order: Kubernetes namespace (removes the ALB), then the CloudFormation stack (empties the CloudTrail bucket first), then `terraform destroy`.

> Production next steps: a Route 53 domain + ACM certificate + HTTPS listener on the ALB, Keycloak in production mode (`start`) on RDS, `db_multi_az = true`, and one NAT gateway per AZ.

---

## 12. CI/CD with Jenkins

```
Unit tests (ruff + pytest) -> IaC scans (Checkov: Terraform, K8s, CloudFormation; cfn-lint) -> Build image
  -> Trivy scan (fail on fixable CRITICAL) -> Push to ECR -> Deploy to EKS -> Smoke test   (last three: main only)
```

```bash
docker compose -f jenkins/docker-compose.yml up -d --build
docker compose -f jenkins/docker-compose.yml exec jenkins cat /var/jenkins_home/secrets/initialAdminPassword
```

1. Open http://localhost:8081, install the suggested plugins and create an admin user.
2. Go to **Manage Jenkins → Credentials → Global → Add**: kind **AWS Credentials**, ID `aws-credentials`.
3. **New Item → Multibranch Pipeline**, branch source **GitHub** (your repository URL), Script Path `Jenkinsfile`.
4. (Optional) Add a GitHub webhook to `http://<jenkins-host>/github-webhook/` so every push builds.

---

## 13. Testing

```bash
python -m venv .venv && .venv/Scripts/activate      # Linux/macOS: source .venv/bin/activate
pip install -r app/requirements.txt pytest
pytest -q tests
```

| Test | Covers |
|---|---|
| `tests/test_rules.py` | SLA deadlines and compliance %, ticket state machine, rule-based triage (security incidents, outages, access, requests), AI output validation, SNS alert selection |
| `tests/test_api.py` | Security headers, health/metrics, internal routes blocked, rate limit 429, internal-token check, AI triage with valid/invalid/missing model output, invalid JWT rejected |
| `scripts/smoke_test.py` | 20 end-to-end checks on a running deployment (login, triage, SLA, RBAC, hidden tickets, KB assistant) |

---

## 14. Security design

| Threat | Control |
|---|---|
| Stolen passwords | Keycloak password policy + history, brute-force lockout, **mandatory TOTP MFA for agents/admins**, short-lived tokens (5 min), login event audit |
| Broken access control | JWT validated in every service (signature, issuer); RBAC (`user` / `agent` / `admin`); employees can only see their own tickets (others return 404, not 403); internal notes hidden from requesters |
| Web attacks (OWASP Top 10) | AWS WAF managed rules; parameterized SQL; Pydantic input validation; output escaping in the frontend; CSP and anti-framing headers |
| Abuse / DoS | WAF per-IP rate rule + gateway rate limiting in Redis + HPA autoscaling |
| Lateral movement | NetworkPolicies, an internal-token on `/internal/*`, the gateway refuses internal routes, and a separate IAM role per pod (IRSA) with least privilege |
| Data exposure | RDS private and KMS-encrypted; Secrets Manager (KMS); Kubernetes Secrets envelope-encrypted with KMS; no secrets in Git (`.gitignore`) |
| Undetected compromise | CloudTrail (log-file validation), GuardDuty (incl. EKS audit logs) → SNS, VPC Flow Logs, WAF logs, IAM Access Analyzer, application audit trail |
| Vulnerable dependencies / misconfiguration | Trivy (build fails on fixable CRITICAL), ECR scan on push, Checkov + cfn-lint on all IaC, pinned versions, non-root read-only containers |
| AI misuse | The model only sees ticket text and KB articles; schema-validated output with a rules fallback; prompt forbids asking for credentials |
| Cost attack / runaway spend | AWS Budget alerts, node group max size, WAF rate limit |

---

## 15. API reference

Call everything through the gateway as `/api/<service>/<path>`. Interactive docs are at `/api/<service>/docs`.

| Method & path | Role | Description |
|---|---|---|
| `POST /api/tickets/tickets` | user | Raise a ticket (auto-triaged) |
| `GET /api/tickets/tickets?status=&priority=&mine=` | user / agent | Own tickets (user) or the whole queue (agent) |
| `GET /api/tickets/tickets/{id}` | owner / agent | Detail, comments (+ audit trail for agents) |
| `POST /api/tickets/tickets/{id}/comments` | owner / agent | Reply (`internal: true` = agent note) |
| `PATCH /api/tickets/tickets/{id}` | agent (owner: close/reopen) | Status, priority, category, assign |
| `GET /api/sla/sla/summary` · `/sla/breaches` | agent / admin | SLA compliance report, breached tickets |
| `GET /api/notifications/notifications` | any | My notifications |
| `POST /api/chat/chat` · `GET /api/chat/kb?q=` | any | Help assistant, knowledge-base search |

---

## 16. Cost control

| Item | Cost |
|---|---|
| Local: Docker Compose, minikube, Jenkins, Keycloak, Prometheus, Grafana, ELK | Free |
| Bedrock | Pay per token (cents while testing); off by default |
| AWS: EKS, EC2, NAT, ALB, RDS, WAF (~$5/month + $1 per rule), KMS (~$1/month) | Roughly $7–9 per day if left running, so **run `aws-destroy.sh` after demos** |
| GuardDuty / Security Hub | 30-day free trials, then usage-based |

---

## 17. Troubleshooting

| Problem | Fix |
|---|---|
| Login loops or "Invalid redirect uri" | Use exactly `http://localhost:8000`. On AWS, after the ALB hostname changes, run `kubectl delete pod -n helpdesk -l app=keycloak` so the realm is re-imported. |
| Agent login asks for a code you don't have | Expected on first login: scan the QR code with an authenticator app. To reset, go to Keycloak admin → Users → agent1 → Credentials → delete OTP. |
| 401 "Invalid token: issuer" | `KEYCLOAK_ISSUER` must equal the browser-facing Keycloak URL + `/realms/helpdesk`. |
| Triage always says `rules` | `BEDROCK_ENABLED` is false or Bedrock failed; check `docker compose logs chatbot`. |
| Ingress has no address | Check `kubectl logs -n kube-system deploy/aws-load-balancer-controller`. Public subnets need the tag `kubernetes.io/role/elb=1` (Terraform sets it). |
| CloudFormation fails on GuardDuty | A detector already exists; redeploy with `CreateGuardDutyDetector=false`. |
| `terraform destroy` hangs on the VPC | The ALB still exists; delete the `helpdesk` namespace first (the destroy script does this). |
| SNS emails don't arrive | Confirm the subscription email from AWS Notifications (check spam). |
