"""AWS service interactions."""

import json
from typing import Any

import boto3
from rich.console import Console

from infractl.config import ClusterConfig

console = Console()


class AWSService:
    """Service for AWS operations."""

    def __init__(self, region: str) -> None:
        self.region = region
        self.sts = boto3.client("sts", region_name=region)
        self.ec2 = boto3.client("ec2", region_name=region)
        self.eks = boto3.client("eks", region_name=region)
        self.iam = boto3.client("iam", region_name=region)

    def get_account_id(self) -> str:
        """Get the current AWS account ID."""
        response = self.sts.get_caller_identity()
        return str(response["Account"])

    def get_cluster_info(self, cluster_name: str) -> dict[str, Any]:
        """Get EKS cluster information."""
        response = self.eks.describe_cluster(name=cluster_name)
        return dict(response["cluster"])

    def get_vpc_subnets(self, vpc_id: str) -> dict[str, dict[str, str]]:
        """Get subnet information for a VPC."""
        response = self.ec2.describe_subnets(
            Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
        )

        private: dict[str, str] = {}
        public: dict[str, str] = {}

        for subnet in response["Subnets"]:
            az = subnet["AvailabilityZone"]
            subnet_id = subnet["SubnetId"]
            az_suffix = az[-1]  # a, b, c

            if subnet["MapPublicIpOnLaunch"]:
                public[az_suffix] = subnet_id
            else:
                private[az_suffix] = subnet_id

        return {"private": private, "public": public}

    def get_internet_gateway(self, vpc_id: str) -> str:
        """Get Internet Gateway ID for a VPC."""
        response = self.ec2.describe_internet_gateways(
            Filters=[{"Name": "attachment.vpc-id", "Values": [vpc_id]}]
        )
        if response["InternetGateways"]:
            return str(response["InternetGateways"][0]["InternetGatewayId"])
        return ""

    def get_nat_gateway(self, vpc_id: str) -> str:
        """Get NAT Gateway ID for a VPC."""
        response = self.ec2.describe_nat_gateways(
            Filters=[
                {"Name": "vpc-id", "Values": [vpc_id]},
                {"Name": "state", "Values": ["available"]},
            ]
        )
        if response["NatGateways"]:
            return str(response["NatGateways"][0]["NatGatewayId"])
        return ""

    def populate_cluster_config(self, config: ClusterConfig) -> ClusterConfig:
        """Populate cluster config with AWS resource information."""
        config.account_id = self.get_account_id()

        cluster_info = self.get_cluster_info(config.cluster_name)
        config.vpc_id = cluster_info["resourcesVpcConfig"]["vpcId"]
        config.oidc_provider = cluster_info["identity"]["oidc"]["issuer"].replace(
            "https://", ""
        )
        config.oidc_id = config.oidc_provider.split("/")[-1]

        subnets = self.get_vpc_subnets(config.vpc_id)
        config.private_subnets = subnets["private"]
        config.public_subnets = subnets["public"]

        config.igw_id = self.get_internet_gateway(config.vpc_id)
        config.nat_id = self.get_nat_gateway(config.vpc_id)

        return config

    def policy_exists(self, policy_arn: str) -> bool:
        """Check if an IAM policy exists."""
        try:
            self.iam.get_policy(PolicyArn=policy_arn)
            return True
        except self.iam.exceptions.NoSuchEntityException:
            return False

    def role_exists(self, role_name: str) -> bool:
        """Check if an IAM role exists."""
        try:
            self.iam.get_role(RoleName=role_name)
            return True
        except self.iam.exceptions.NoSuchEntityException:
            return False

    def create_permission_boundary(self, config: ClusterConfig, policy_document: dict[str, Any]) -> None:
        """Create the Crossplane permission boundary policy."""
        if self.policy_exists(config.permission_boundary_arn):
            console.print(
                f"[yellow]Permission boundary {config.permission_boundary_name} already exists[/yellow]"
            )
            return

        self.iam.create_policy(
            PolicyName=config.permission_boundary_name,
            PolicyDocument=json.dumps(policy_document),
        )
        console.print(f"[green]Created permission boundary {config.permission_boundary_name}[/green]")

    def create_crossplane_role(self, config: ClusterConfig) -> None:
        """Create the Crossplane IAM role with IRSA trust policy."""
        if self.role_exists(config.crossplane_role_name):
            console.print(
                f"[yellow]Crossplane role {config.crossplane_role_name} already exists[/yellow]"
            )
            return

        trust_policy = {
            "Version": "2012-10-17",
            "Statement": [
                {
                    "Effect": "Allow",
                    "Principal": {
                        "Federated": f"arn:aws:iam::{config.account_id}:oidc-provider/{config.oidc_provider}"
                    },
                    "Action": "sts:AssumeRoleWithWebIdentity",
                    "Condition": {
                        "StringLike": {
                            f"{config.oidc_provider}:sub": "system:serviceaccount:crossplane-system:provider-aws-*"
                        }
                    },
                }
            ],
        }

        self.iam.create_role(
            RoleName=config.crossplane_role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            PermissionsBoundary=config.permission_boundary_arn,
        )

        self.iam.attach_role_policy(
            RoleName=config.crossplane_role_name,
            PolicyArn="arn:aws:iam::aws:policy/AdministratorAccess",
        )

        console.print(f"[green]Created Crossplane role {config.crossplane_role_name}[/green]")

    def update_kubeconfig(self, cluster_name: str) -> None:
        """Update kubeconfig for the cluster."""
        import subprocess

        subprocess.run(
            ["aws", "eks", "update-kubeconfig", "--name", cluster_name, "--region", self.region],
            check=True,
        )
