# ADR-0039: AWS as the deployment target: ECS on Fargate, Aurora PostgreSQL, ElastiCache for Valkey

- **Status:** Proposed (design complete; the stack currently runs on Docker Compose)
- **Area:** Operations / cloud
- **Full design:** [docs/deployment/AWS_DEPLOYMENT.md](../deployment/AWS_DEPLOYMENT.md)

## Context

HealthGuard runs as a Compose stack: `api`, `worker`, `ui`, a one-shot `migrate` job, Postgres 17 + pgvector and Redis 8 ([ADR-0037](0037-packaging-and-deployment.md)). A cloud deployment must keep the properties the design depends on: Postgres roles and Row-Level Security, pgvector and full-text search, long-lived SSE streams, WebSockets with sticky sessions for Streamlit, one process per container, a shared fast store for caches, locks and the queue, and a path to handling real PHI.

## Decision

Map the Compose services one-to-one onto managed AWS services in `us-east-1`:

| Compose | AWS |
|---|---|
| `api`, `worker`, `ui` | **ECS services on Fargate (arm64)** in private subnets, the same image with different commands |
| `migrate` (and `ingest`, `eval`) | **ECS one-off tasks**, run by the pipeline (migrate) or EventBridge (ingest on corpus change) |
| `postgres` | **Aurora PostgreSQL 17** with pgvector; writer + reader; the agent's read-only pool uses the reader |
| `redis` | **ElastiCache for Valkey** (Multi-AZ, TLS, `volatile-lru`), with a vector-search compatibility gate for the semantic cache |
| Ingress | **Route 53 → WAF → ALB** (ACM TLS, 300 s idle timeout, sticky UI target group, `/metrics` blocked) |
| Data volume | **S3**, synced into tasks at start |
| `.env` | **Secrets Manager** (secrets, rotation) + **SSM Parameter Store** (config) |
| Observability | CloudWatch Logs; ADOT sidecar → Amazon Managed Prometheus and X-Ray; Managed Grafana |
| Delivery | GitHub Actions with IAM OIDC → ECR → migrate task → rolling update with circuit breaker; Terraform for infrastructure |
| Model gateway | OpenRouter over NAT for synthetic data; **Bedrock over PrivateLink** for real PHI |

## Why

- **Lowest-risk move:** the same image and process model; no re-architecture.
- **Fargate over EKS/Lambda:** Lambda doesn't fit 150 s SSE streams, in-process pools or a process sandbox; EKS adds a control plane for four services. Each Fargate task is a microVM, which also strengthens code-sandbox isolation.
- **ALB over API Gateway:** native streaming, WebSockets and stickiness for long-lived connections.
- **Aurora over a vector database:** RLS, roles and hybrid search in one engine; a reader endpoint isolates generated SQL from writes.
- **ElastiCache over MemoryDB:** the tier holds only rebuildable or best-effort state, so durability isn't worth paying for.
- **HIPAA-eligible services throughout**, with a contained Bedrock path for the one non-AWS dependency.

## Alternatives considered

EKS, Lambda, App Runner, ECS on EC2; RDS for PostgreSQL, OpenSearch, DynamoDB; MemoryDB, Redis Cloud, a pgvector-backed semantic cache; API Gateway, CloudFront; CloudFormation, AWS CDK. Each is evaluated in the [full design](../deployment/AWS_DEPLOYMENT.md).

## Consequences

- **Required changes** before the first deploy (all outside the agent logic): a separate agent database host setting, driver-specific TLS, `rediss://`, `volatile-lru`, `FORWARDED_ALLOW_IPS`, a turn timeout that fits Fargate's 120 s stop window, S3 sync for data and eval outputs, a one-time database bootstrap, and a check that revoking `set_config` is permitted on Aurora ([§20](../deployment/AWS_DEPLOYMENT.md#20-changes-required-before-the-first-deploy)).
- **Cost:** roughly $125–300/month for dev and $1,000–1,500/month for a small Multi-AZ prod (estimates), with model spend dominating at volume.
- **Open:** the semantic cache's vector-search parity on Valkey must be proven; separating the sandbox into its own credential-less service is recommended before real data.

## Interview soundbite

> "The AWS design maps each Compose service onto a managed equivalent: ECS on Fargate behind WAF and an ALB, Aurora PostgreSQL with pgvector and a reader for the agent's RLS-scoped SQL, ElastiCache for Valkey for caches, locks and the queue, S3 for data, Secrets Manager, and GitHub OIDC deploys. Working through it surfaced concrete gotchas, such as Fargate's 120-second stop limit versus my 150-second turns, client IPs behind the ALB, and managed Postgres not having a true superuser, and each one has a specific fix."
