# EKS Bootstrap with eksctl

This bootstrap process creates a self-managing EKS cluster with Crossplane and ArgoCD.

## Prerequisites

- AWS CLI configured with appropriate credentials
- eksctl
- kubectl
- helm

## Bootstrap Process

```bash
cd k8s/bootstrap/eksctl

# Optional: Set environment variables
export CLUSTER_NAME=stack-ai
export AWS_REGION=us-east-1

# Run bootstrap
./bootstrap.sh
```

## What it creates

1. **EKS Cluster** with:
   - OIDC provider enabled (for IRSA)
   - 3 nodes across 3 AZs
   - VPC with public/private subnets
   - NAT Gateway

2. **IAM Resources**:
   - Permission boundary policy
   - Crossplane IAM role with IRSA trust

3. **Crossplane**:
   - Core Crossplane
   - AWS providers (EC2, EKS, IAM, RDS, S3, Route53)
   - Helm and Kubernetes providers
   - Provider configs with IRSA

4. **ArgoCD**:
   - Full installation
   - Ready to manage applications

5. **EnvironmentConfig**:
   - Cluster metadata for compositions

## Post-Bootstrap Steps

### 1. Get cluster resource IDs

The bootstrap script outputs the VPC ID. Get subnet IDs:

```bash
VPC_ID=$(aws eks describe-cluster --name stack-ai --query "cluster.resourcesVpcConfig.vpcId" --output text)

# Get private subnet IDs
aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" \
  --query "Subnets[?MapPublicIpOnLaunch==\`false\`].[SubnetId,AvailabilityZone]" --output table
```

### 2. Update import-existing.yaml

Edit `k8s/infrastructure/overlays/dev/patches/import-existing.yaml` with actual IDs.

### 3. Apply ArgoCD ApplicationSets

```bash
kubectl apply -k k8s/argocd/base
```

### 4. Access ArgoCD UI

```bash
# Get password
kubectl -n argocd get secret argocd-initial-admin-secret -o jsonpath='{.data.password}' | base64 -d

# Port forward
kubectl port-forward svc/argocd-server -n argocd 8080:443
```

Open https://localhost:8080 (username: admin)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ Bootstrap (one-time)                                                │
│                                                                     │
│  eksctl create cluster                                              │
│       │                                                             │
│       ├── Creates VPC, Subnets, NAT, EKS                           │
│       ├── Enables OIDC                                              │
│       └── Creates IAM role for Crossplane                          │
│                                                                     │
│  bootstrap.sh                                                       │
│       │                                                             │
│       ├── Installs Crossplane + Providers                          │
│       ├── Configures IRSA                                          │
│       ├── Installs ArgoCD                                          │
│       └── Creates EnvironmentConfig                                │
└─────────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────┐
│ GitOps (ongoing)                                                    │
│                                                                     │
│  ArgoCD watches git repo                                            │
│       │                                                             │
│       ├── infrastructure (Crossplane imports existing resources)   │
│       │       └── Creates new resources (Aurora, S3, etc.)         │
│       │                                                             │
│       └── supabase (deploys application)                           │
└─────────────────────────────────────────────────────────────────────┘
```

## Cleanup

```bash
# Delete via ArgoCD first (lets Crossplane clean up)
kubectl delete applicationsets --all -n argocd

# Wait for resources to be deleted
kubectl get managed

# Delete cluster
eksctl delete cluster --name stack-ai --region us-east-1

# Clean up IAM
aws iam detach-role-policy --role-name crossplane-provider-aws \
  --policy-arn arn:aws:iam::aws:policy/AdministratorAccess
aws iam delete-role --role-name crossplane-provider-aws
aws iam delete-policy --policy-arn arn:aws:iam::$(aws sts get-caller-identity --query Account --output text):policy/crossplaneBoundary
```

## References

- [AWS Labs crossplane-on-eks](https://github.com/awslabs/crossplane-on-eks)
- [Crossplane Import Existing Resources](https://docs.crossplane.io/latest/guides/import-existing-resources/)
- [AWS GitOps with Crossplane and ArgoCD](https://aws.amazon.com/blogs/containers/gitops-model-for-provisioning-and-bootstrapping-amazon-eks-clusters-using-crossplane-and-argo-cd/)
