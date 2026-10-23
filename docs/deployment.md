# Container, infrastructure, and release pipeline

## Local container stack

Copy `.env.example` to `.env` and replace the development secrets with unique random values. The Compose file overrides the database and Redis URLs to use its local PostgreSQL and Redis containers. Start the dependencies and API with `docker compose up --build -d`; run schema migrations separately with `docker compose --profile tools run --rm migrate`. The API is available at `http://localhost:8000`. Local database credentials and Redis settings in `compose.yaml` are development-only and must never be reused in shared or production environments.

The image installs only locked runtime dependencies, runs as UID 10001, drops Linux capabilities, uses a read-only root filesystem, and exposes a liveness check. It runs one Uvicorn process per container; horizontal scaling is handled by ECS, avoiding per-process metric aggregation problems. Compose does not publish database or Redis ports. Persistent local data is in named volumes.

## AWS reference deployment

`infra/terraform` provisions an AWS ECS Fargate service behind an HTTPS Application Load Balancer, private application and data subnets across two availability zones, PostgreSQL RDS Multi-AZ, encrypted/failover-enabled ElastiCache Redis, ECR, Secrets Manager, CloudWatch logs, and CPU-based ECS task autoscaling. ALB traffic is redirected from HTTP to HTTPS. PostgreSQL and Redis accept traffic only from the application task security group. RDS backups are retained for 14 days, Redis snapshots for 7 days, and RDS deletion protection is enabled.

This is an AWS reference topology, not a universal sizing recommendation. Review database/cache instance sizes, retention, residency, subnet ranges, autoscaling thresholds, service quotas, and regional availability against measured load and the applicable clinical-data policy before applying it. NAT gateways, Multi-AZ databases, and managed caches incur material recurring costs. The configured DB/cache sizes and scaling ceiling need a load test against the 100,000-consultations/day workload; they do not by themselves demonstrate the latency or availability targets.

Before first `terraform init`, create a dedicated S3 state bucket with versioning, encryption, public-access blocking, and tightly restricted access. Initialize with an S3 backend configuration, for example `terraform init -backend-config="bucket=<state-bucket>" -backend-config="key=amrutam/production/terraform.tfstate" -backend-config="region=<state-region>" -backend-config="encrypt=true" -backend-config="use_lockfile=true"`. Keep state access restricted: Terraform state contains generated application credentials even though outputs do not reveal them.

The AWS account must have the GitHub Actions OIDC provider `https://token.actions.githubusercontent.com` registered. Supply `aws_region`, `app_host`, `certificate_arn`, `github_repository`, and an initial immutable `container_image` using a protected `terraform.tfvars` file or equivalent; never commit credentials or secret values. The ACM certificate must already exist in the selected region. Apply the Terraform plan from a reviewed, authenticated operator session, then create a DNS alias from `app_host` to the `load_balancer_dns_name` output.

Terraform generates unique database, Redis, JWT, encryption, and payment-webhook secrets and stores them in Secrets Manager. The secret values are also present in Terraform state, which is why a remote encrypted backend with strict access controls is mandatory. Application tasks have only the execution-role permission needed to retrieve this one secret. Rotation must coordinate with the application/database/cache and be exercised before production use.

## CI and release

Pull requests and pushes run the lockfile check, Ruff lint/format checks, tests, and Docker image build. CI starts a disposable PostgreSQL 16 service, applies the full Alembic migration chain, and runs a PostgreSQL integration test that races two patients for one slot and verifies exactly one booking succeeds. A push to `master` publishes an immutable SHA-tagged image to ECR, registers a task definition using that image, runs `alembic upgrade head` as a one-off Fargate task, then updates the ECS service and waits for it to become stable. The migration runs once per serialized release; database changes must use backward-compatible expand/migrate/contract sequencing because the old application tasks can remain live during migration and rollout.

Set these GitHub repository **variables** from the reviewed Terraform outputs:

- `AWS_ROLE_TO_ASSUME`: `github_release_role_arn`
- `AWS_REGION`: the deployment region
- `ECR_REPOSITORY`: `ecr_repository_url` (the full repository URI; append `:sha-<commit>` to the initial `container_image` value)
- `ECS_CLUSTER`: `ecs_cluster_name`
- `ECS_SERVICE`: `ecs_service_name`
- `ECS_TASK_FAMILY`: `ecs_task_family`
- `APPLICATION_SUBNET_IDS`: `application_subnet_ids`, comma-separated
- `APPLICATION_SECURITY_GROUP_ID`: `application_security_group_id`

The CI role uses GitHub OIDC instead of long-lived AWS access keys and is scoped to the configured repository's `master` branch claim. Protect `master` with required CI checks and restrict who can modify workflow files, Terraform, and production variables. The release workflow serializes production deployments. Image rollback is performed by rerunning the release with a previously built immutable SHA image; schema rollback is not automatic, so migrations must be backward-compatible and have a tested forward-repair plan.
