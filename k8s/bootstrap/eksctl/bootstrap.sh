#!/bin/bash
set -euo pipefail

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Configuration
CLUSTER_NAME="${CLUSTER_NAME:-stack-ai}"
REGION="${AWS_REGION:-us-east-1}"
ARGOCD_NAMESPACE="argocd"
CROSSPLANE_NAMESPACE="crossplane-system"

# Check prerequisites
command -v aws >/dev/null 2>&1 || error "aws CLI is required"
command -v eksctl >/dev/null 2>&1 || error "eksctl is required"
command -v kubectl >/dev/null 2>&1 || error "kubectl is required"

log "Starting bootstrap for cluster: ${CLUSTER_NAME}"

# Get AWS account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
log "AWS Account ID: ${ACCOUNT_ID}"

# ============================================================
# Phase 1: Create EKS Cluster
# ============================================================
log "Phase 1: Creating EKS cluster..."

if eksctl get cluster --name "${CLUSTER_NAME}" --region "${REGION}" 2>/dev/null; then
    warn "Cluster ${CLUSTER_NAME} already exists, skipping creation"
else
    # Update cluster.yaml with region and name
    sed -i.bak "s/region: us-east-1/region: ${REGION}/g" cluster.yaml
    sed -i "s/name: stack-ai/name: ${CLUSTER_NAME}/g" cluster.yaml

    eksctl create cluster -f cluster.yaml
    log "EKS cluster created successfully"
fi

# Update kubeconfig
aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}"

# ============================================================
# Phase 2: Setup IAM for Crossplane (IRSA)
# ============================================================
log "Phase 2: Setting up IAM for Crossplane..."

# Get OIDC provider
OIDC_PROVIDER=$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${REGION}" \
    --query "cluster.identity.oidc.issuer" --output text | sed -e "s/^https:\/\///")
log "OIDC Provider: ${OIDC_PROVIDER}"

# Create permission boundary
sed -i.bak "s/ACCOUNT_ID/${ACCOUNT_ID}/g" permission-boundary.json

if aws iam get-policy --policy-arn "arn:aws:iam::${ACCOUNT_ID}:policy/crossplaneBoundary" 2>/dev/null; then
    warn "Permission boundary policy already exists"
else
    aws iam create-policy \
        --policy-name crossplaneBoundary \
        --policy-document file://permission-boundary.json
    log "Permission boundary created"
fi

PERMISSION_BOUNDARY_ARN="arn:aws:iam::${ACCOUNT_ID}:policy/crossplaneBoundary"

# Create trust policy for Crossplane
cat > trust.json << EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Principal": {
        "Federated": "arn:aws:iam::${ACCOUNT_ID}:oidc-provider/${OIDC_PROVIDER}"
      },
      "Action": "sts:AssumeRoleWithWebIdentity",
      "Condition": {
        "StringLike": {
          "${OIDC_PROVIDER}:sub": "system:serviceaccount:${CROSSPLANE_NAMESPACE}:provider-aws-*"
        }
      }
    }
  ]
}
EOF

# Create Crossplane IAM role
if aws iam get-role --role-name crossplane-provider-aws 2>/dev/null; then
    warn "Crossplane IAM role already exists"
else
    aws iam create-role \
        --role-name crossplane-provider-aws \
        --assume-role-policy-document file://trust.json \
        --permissions-boundary "${PERMISSION_BOUNDARY_ARN}"

    aws iam attach-role-policy \
        --role-name crossplane-provider-aws \
        --policy-arn arn:aws:iam::aws:policy/AdministratorAccess

    log "Crossplane IAM role created"
fi

CROSSPLANE_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/crossplane-provider-aws"

# ============================================================
# Phase 3: Install ArgoCD (only thing installed manually)
# ============================================================
log "Phase 3: Installing ArgoCD..."

if kubectl get namespace "${ARGOCD_NAMESPACE}" 2>/dev/null; then
    warn "ArgoCD namespace already exists"
else
    kubectl create namespace "${ARGOCD_NAMESPACE}"
fi

kubectl apply -n "${ARGOCD_NAMESPACE}" \
    -f https://raw.githubusercontent.com/argoproj/argo-cd/v2.10.0/manifests/install.yaml

log "Waiting for ArgoCD to be ready..."
kubectl wait --for=condition=available --timeout=300s deployment/argocd-server -n "${ARGOCD_NAMESPACE}"

# ============================================================
# Phase 4: Create Crossplane namespace and IRSA config
# ============================================================
log "Phase 4: Preparing Crossplane namespace..."

kubectl create namespace "${CROSSPLANE_NAMESPACE}" --dry-run=client -o yaml | kubectl apply -f -

# Create a ConfigMap with cluster info for reference
VPC_ID=$(aws eks describe-cluster --name "${CLUSTER_NAME}" --region "${REGION}" \
    --query "cluster.resourcesVpcConfig.vpcId" --output text)

PRIVATE_SUBNETS=$(aws ec2 describe-subnets \
    --filters "Name=vpc-id,Values=${VPC_ID}" "Name=map-public-ip-on-launch,Values=false" \
    --query "Subnets[].SubnetId" --output text | tr '\t' ',')

cat <<EOF | kubectl apply -f -
apiVersion: v1
kind: ConfigMap
metadata:
  name: cluster-info
  namespace: ${CROSSPLANE_NAMESPACE}
data:
  account_id: "${ACCOUNT_ID}"
  region: "${REGION}"
  cluster_name: "${CLUSTER_NAME}"
  vpc_id: "${VPC_ID}"
  oidc_provider: "${OIDC_PROVIDER}"
  crossplane_role_arn: "${CROSSPLANE_ROLE_ARN}"
  private_subnet_ids: "${PRIVATE_SUBNETS}"
EOF

log "Cluster info ConfigMap created"

# ============================================================
# Phase 5: Generate import-existing.yaml with real resource IDs
# ============================================================
log "Phase 5: Generating import-existing.yaml with actual resource IDs..."

# Get all subnet IDs
SUBNETS_JSON=$(aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
    --query "Subnets[].{Id:SubnetId,Az:AvailabilityZone,Public:MapPublicIpOnLaunch}" --output json)

# Parse subnet IDs by type and AZ
PRIVATE_A=$(echo "$SUBNETS_JSON" | jq -r '.[] | select(.Public==false and (.Az | endswith("a"))) | .Id')
PRIVATE_B=$(echo "$SUBNETS_JSON" | jq -r '.[] | select(.Public==false and (.Az | endswith("b"))) | .Id')
PRIVATE_C=$(echo "$SUBNETS_JSON" | jq -r '.[] | select(.Public==false and (.Az | endswith("c"))) | .Id')
PUBLIC_A=$(echo "$SUBNETS_JSON" | jq -r '.[] | select(.Public==true and (.Az | endswith("a"))) | .Id')
PUBLIC_B=$(echo "$SUBNETS_JSON" | jq -r '.[] | select(.Public==true and (.Az | endswith("b"))) | .Id')
PUBLIC_C=$(echo "$SUBNETS_JSON" | jq -r '.[] | select(.Public==true and (.Az | endswith("c"))) | .Id')

# Get Internet Gateway
IGW_ID=$(aws ec2 describe-internet-gateways --filters "Name=attachment.vpc-id,Values=${VPC_ID}" \
    --query "InternetGateways[0].InternetGatewayId" --output text)

# Get NAT Gateway
NAT_ID=$(aws ec2 describe-nat-gateways --filter "Name=vpc-id,Values=${VPC_ID}" "Name=state,Values=available" \
    --query "NatGateways[0].NatGatewayId" --output text)

# Get Security Group for Aurora (create if doesn't exist)
AURORA_SG=$(aws ec2 describe-security-groups --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=aurora-sg" \
    --query "SecurityGroups[0].GroupId" --output text 2>/dev/null || echo "")

if [ -z "$AURORA_SG" ] || [ "$AURORA_SG" == "None" ]; then
    log "Creating Aurora security group..."
    AURORA_SG=$(aws ec2 create-security-group --group-name aurora-sg \
        --description "Security group for Aurora" --vpc-id "${VPC_ID}" \
        --query "GroupId" --output text)
    aws ec2 authorize-security-group-ingress --group-id "${AURORA_SG}" \
        --protocol tcp --port 5432 --cidr 10.0.0.0/16
fi

# Get Route53 hosted zone (create if doesn't exist)
HOSTED_ZONE_ID=$(aws route53 list-hosted-zones-by-vpc --vpc-id "${VPC_ID}" --vpc-region "${REGION}" \
    --query "HostedZoneSummaries[?Name=='supabase.internal.'].HostedZoneId" --output text 2>/dev/null || echo "")

if [ -z "$HOSTED_ZONE_ID" ] || [ "$HOSTED_ZONE_ID" == "None" ]; then
    log "Creating Route53 private hosted zone..."
    HOSTED_ZONE_ID=$(aws route53 create-hosted-zone --name supabase.internal \
        --vpc VPCRegion="${REGION}",VPCId="${VPC_ID}" \
        --caller-reference "$(date +%s)" \
        --query "HostedZone.Id" --output text | sed 's|/hostedzone/||')
fi

IMPORT_FILE="../../infrastructure/overlays/dev/patches/import-existing.yaml"

cat > "${IMPORT_FILE}" << EOF
# Auto-generated by bootstrap.sh - imports eksctl-created resources
# Generated: $(date -u +"%Y-%m-%dT%H:%M:%SZ")
#
# These resources already exist (created by eksctl).
# Crossplane will import and manage them instead of creating new ones.

# Import existing VPC
apiVersion: ec2.aws.upbound.io/v1beta1
kind: VPC
metadata:
  name: main
  annotations:
    crossplane.io/external-name: "${VPC_ID}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
# Import Internet Gateway
apiVersion: ec2.aws.upbound.io/v1beta1
kind: InternetGateway
metadata:
  name: main
  annotations:
    crossplane.io/external-name: "${IGW_ID}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
# Import Private Subnets
apiVersion: ec2.aws.upbound.io/v1beta1
kind: Subnet
metadata:
  name: private-a
  annotations:
    crossplane.io/external-name: "${PRIVATE_A}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
apiVersion: ec2.aws.upbound.io/v1beta1
kind: Subnet
metadata:
  name: private-b
  annotations:
    crossplane.io/external-name: "${PRIVATE_B}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
apiVersion: ec2.aws.upbound.io/v1beta1
kind: Subnet
metadata:
  name: private-c
  annotations:
    crossplane.io/external-name: "${PRIVATE_C}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
# Import Public Subnets
apiVersion: ec2.aws.upbound.io/v1beta1
kind: Subnet
metadata:
  name: public-a
  annotations:
    crossplane.io/external-name: "${PUBLIC_A}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
apiVersion: ec2.aws.upbound.io/v1beta1
kind: Subnet
metadata:
  name: public-b
  annotations:
    crossplane.io/external-name: "${PUBLIC_B}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
apiVersion: ec2.aws.upbound.io/v1beta1
kind: Subnet
metadata:
  name: public-c
  annotations:
    crossplane.io/external-name: "${PUBLIC_C}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
# Import NAT Gateway
apiVersion: ec2.aws.upbound.io/v1beta1
kind: NATGateway
metadata:
  name: nat-a
  annotations:
    crossplane.io/external-name: "${NAT_ID}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
# Aurora Security Group (created by bootstrap)
apiVersion: ec2.aws.upbound.io/v1beta1
kind: SecurityGroup
metadata:
  name: aurora
  annotations:
    crossplane.io/external-name: "${AURORA_SG}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    region: ${REGION}
---
# Route53 Private Hosted Zone
apiVersion: route53.aws.upbound.io/v1beta1
kind: Zone
metadata:
  name: internal
  annotations:
    crossplane.io/external-name: "${HOSTED_ZONE_ID}"
spec:
  managementPolicies: ["Observe", "LateInitialize", "Update"]
  deletionPolicy: Orphan
  forProvider:
    name: supabase.internal
---
# EKS Cluster - DO NOT import via Crossplane (circular dependency)
# The cluster manages itself. Instead, store reference in aws-config.yaml
EOF

log "Generated ${IMPORT_FILE}"

# Update aws-config.yaml with actual values
AWS_CONFIG="../../infrastructure/overlays/dev/patches/aws-config.yaml"

log "Updating ${AWS_CONFIG} with actual resource IDs..."

# Update EKSCluster claim with real subnet IDs
sed -i.bak "s/subnet-dev-private-a/${PRIVATE_A}/g" "${AWS_CONFIG}"
sed -i "s/subnet-dev-private-b/${PRIVATE_B}/g" "${AWS_CONFIG}"
sed -i "s/subnet-dev-private-c/${PRIVATE_C}/g" "${AWS_CONFIG}"
sed -i "s/sg-dev-aurora/${AURORA_SG}/g" "${AWS_CONFIG}"
sed -i "s/Z0123456789DEV/${HOSTED_ZONE_ID}/g" "${AWS_CONFIG}"
sed -i "s/vpc-dev/${VPC_ID}/g" "${AWS_CONFIG}"
sed -i "s/111111111111/${ACCOUNT_ID}/g" "${AWS_CONFIG}"
sed -i "s/DEV_OIDC_ID/$(echo ${OIDC_PROVIDER} | sed 's|.*/||')/g" "${AWS_CONFIG}"

log "Updated ${AWS_CONFIG}"

# ============================================================
# Phase 6: Commit generated files to git
# ============================================================
log "Phase 6: Committing generated files to git..."

cd "$(git rev-parse --show-toplevel)"

git add k8s/infrastructure/overlays/dev/patches/import-existing.yaml
git add k8s/infrastructure/overlays/dev/patches/aws-config.yaml

if git diff --cached --quiet; then
    warn "No changes to commit"
else
    git commit -m "feat(infrastructure): add generated AWS resource IDs for ${CLUSTER_NAME}

Auto-generated by bootstrap.sh with actual resource IDs:
- VPC: ${VPC_ID}
- Region: ${REGION}
- Account: ${ACCOUNT_ID}
"
    git push
    log "Changes pushed to git"
fi

cd - > /dev/null

# ============================================================
# Phase 7: Apply ArgoCD ApplicationSets
# ============================================================
log "Phase 7: Applying ArgoCD ApplicationSets..."

# Apply the ArgoCD project and applicationsets
kubectl apply -f ../../argocd/base/project.yaml
kubectl apply -f ../../argocd/base/applicationsets.yaml

log "Waiting for Applications to be created..."
sleep 5

# ============================================================
# Phase 8: Trigger syncs in order
# ============================================================
log "Phase 8: Triggering ArgoCD syncs in order..."

# Check if argocd CLI is available, otherwise use kubectl
if command -v argocd >/dev/null 2>&1; then
    # Get ArgoCD admin password
    ARGOCD_PASSWORD=$(kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d)

    # Login to ArgoCD (localhost since we're in-cluster context)
    argocd login argocd-server.argocd.svc.cluster.local:443 --username admin --password "${ARGOCD_PASSWORD}" --insecure --grpc-web 2>/dev/null || \
    warn "Could not login to ArgoCD CLI, will use kubectl for sync"

    log "Syncing Crossplane..."
    argocd app sync crossplane-dev --timeout 300 || warn "Crossplane sync timeout - may still be progressing"
    argocd app wait crossplane-dev --timeout 300 || warn "Crossplane not fully healthy yet"

    log "Syncing Crossplane Providers..."
    argocd app sync crossplane-providers-dev --timeout 300 || warn "Providers sync timeout"
    argocd app wait crossplane-providers-dev --timeout 600 || warn "Providers not fully healthy yet"

    log "Syncing Infrastructure (with imported resources)..."
    argocd app sync infrastructure-dev --timeout 300 || warn "Infrastructure sync timeout"
    argocd app wait infrastructure-dev --timeout 600 || warn "Infrastructure not fully healthy yet"

    log "Syncing Supabase..."
    argocd app sync supabase-dev --timeout 300 || warn "Supabase sync timeout"

    log "Enabling auto-sync for infrastructure-dev and supabase-dev..."
    argocd app set infrastructure-dev --sync-policy automated --self-heal --auto-prune
    argocd app set supabase-dev --sync-policy automated --self-heal --auto-prune
else
    warn "ArgoCD CLI not installed - triggering syncs via kubectl"

    # Trigger sync by adding a refresh annotation
    log "Refreshing crossplane-dev..."
    kubectl patch application crossplane-dev -n argocd --type merge \
        -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}},"operation":{"initiatedBy":{"username":"bootstrap"},"sync":{"syncStrategy":{"apply":{"force":false}}}}}'

    log "Waiting for Crossplane to be ready..."
    kubectl wait --for=condition=Available --timeout=300s deployment/crossplane -n crossplane-system 2>/dev/null || \
        warn "Crossplane deployment not ready yet"

    log "Refreshing crossplane-providers-dev..."
    kubectl patch application crossplane-providers-dev -n argocd --type merge \
        -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'

    log "Waiting for providers to be healthy (this may take a few minutes)..."
    sleep 60

    log "Refreshing infrastructure-dev..."
    kubectl patch application infrastructure-dev -n argocd --type merge \
        -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'

    log "Waiting for infrastructure resources to be created..."
    sleep 120

    log "Refreshing supabase-dev..."
    kubectl patch application supabase-dev -n argocd --type merge \
        -p '{"metadata":{"annotations":{"argocd.argoproj.io/refresh":"hard"}}}'

    warn "Auto-sync not enabled - enable manually or install argocd CLI"
    warn "Run: kubectl patch application infrastructure-dev -n argocd --type merge -p '{\"spec\":{\"syncPolicy\":{\"automated\":{\"prune\":true,\"selfHeal\":true}}}}'"
    warn "Run: kubectl patch application supabase-dev -n argocd --type merge -p '{\"spec\":{\"syncPolicy\":{\"automated\":{\"prune\":true,\"selfHeal\":true}}}}'"
fi

log "Syncs triggered - ArgoCD is now deploying:"
log "  1. Crossplane (via Kustomize helmCharts)"
log "  2. Crossplane Providers"
log "  3. Infrastructure (imports existing VPC/subnets)"
log "  4. Supabase (after infrastructure is ready)"

# ============================================================
# Phase 9: Output information
# ============================================================
log "Bootstrap complete!"

echo ""
echo "============================================================"
echo "Cluster Information"
echo "============================================================"
echo "Cluster Name:      ${CLUSTER_NAME}"
echo "Region:            ${REGION}"
echo "Account ID:        ${ACCOUNT_ID}"
echo "VPC ID:            ${VPC_ID}"
echo "OIDC Provider:     ${OIDC_PROVIDER}"
echo "Crossplane Role:   ${CROSSPLANE_ROLE_ARN}"
echo "Private Subnets:   ${PRIVATE_SUBNETS}"
echo ""
echo "============================================================"
echo "ArgoCD Access"
echo "============================================================"
echo "Get admin password:"
echo "  kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d"
echo ""
echo "Port-forward UI:"
echo "  kubectl port-forward svc/argocd-server -n argocd 8080:443"
echo ""
echo "Open: https://localhost:8080 (username: admin)"
echo ""
echo "============================================================"
echo "What happens next"
echo "============================================================"
echo "ArgoCD will automatically sync (in order via sync-waves):"
echo "  Wave -3: Crossplane"
echo "  Wave -2: Crossplane Providers"
echo "  Wave  0: Infrastructure (imports existing VPC/EKS)"
echo "  Wave  0: Supabase"
echo ""
echo "Watch progress:"
echo "  kubectl get applications -n argocd -w"
echo ""
