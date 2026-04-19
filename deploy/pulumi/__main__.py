"""social-surveyor EC2 deployment.

Provisions a single EC2 instance running under systemd, with an IAM role
granting SSM Session Manager access and read-only access to SSM Parameter
Store under the configured prefix. Outbound-only security group; SSM is
the primary access path (SSH keypair optional).
"""

from __future__ import annotations

import json

import pulumi
import pulumi_aws as aws

config = pulumi.Config()
project_name = config.require("project_name")
vpc_id = config.require("vpc_id")
subnet_id = config.require("subnet_id")
instance_type = config.get("instance_type") or "t4g.micro"
ssm_parameter_prefix = config.require("ssm_parameter_prefix")
ssh_key_name = config.get("ssh_key_name") or ""

aws_config = pulumi.Config("aws")
region = aws_config.require("region")

caller = aws.get_caller_identity()
account_id = caller.account_id

# Strip leading slash so the SSM parameter ARN interpolates cleanly.
# Stored prefix form: "/social-surveyor/opendata"
# ARN resource form:  "parameter/social-surveyor/opendata/..."
ssm_prefix_clean = ssm_parameter_prefix.lstrip("/")

# --- AMI lookup: latest Ubuntu 24.04 LTS ARM64 from Canonical ---

ami = aws.ec2.get_ami(
    most_recent=True,
    owners=["099720109477"],  # Canonical
    filters=[
        {
            "name": "name",
            "values": ["ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-arm64-server-*"],
        },
        {"name": "virtualization-type", "values": ["hvm"]},
        {"name": "architecture", "values": ["arm64"]},
        {"name": "root-device-type", "values": ["ebs"]},
    ],
)

# --- IAM role for the instance ---

trust_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Principal": {"Service": "ec2.amazonaws.com"},
                "Action": "sts:AssumeRole",
            }
        ],
    }
)

role = aws.iam.Role(
    "social-surveyor-instance-role",
    name=f"social-surveyor-{project_name}-instance-role",
    assume_role_policy=trust_policy,
    tags={"Project": "social-surveyor", "ManagedBy": "pulumi"},
)

# Managed policy — AmazonSSMManagedInstanceCore gives Session Manager access.
aws.iam.RolePolicyAttachment(
    "ssm-managed-core",
    role=role.name,
    policy_arn="arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
)

# Inline policy — read parameters under the project prefix + decrypt SecureString.
ssm_parameter_read_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ssm:GetParameter",
                    "ssm:GetParameters",
                    "ssm:GetParametersByPath",
                ],
                "Resource": [
                    f"arn:aws:ssm:{region}:{account_id}:parameter/{ssm_prefix_clean}",
                    f"arn:aws:ssm:{region}:{account_id}:parameter/{ssm_prefix_clean}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["kms:Decrypt"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"kms:ViaService": f"ssm.{region}.amazonaws.com"}
                },
            },
        ],
    }
)

aws.iam.RolePolicy(
    "ssm-parameter-read",
    role=role.id,
    policy=ssm_parameter_read_policy,
)

# Inline policy — EBS snapshot permissions for future backup lifecycle (5a-polish).
# Granted now so the lifecycle work doesn't need an IAM change; not exercised in 5a.
ebs_snapshot_policy = json.dumps(
    {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": [
                    "ec2:DescribeSnapshots",
                    "ec2:DescribeVolumes",
                    "ec2:DescribeTags",
                ],
                "Resource": "*",
            },
            {
                "Effect": "Allow",
                "Action": ["ec2:CreateSnapshot", "ec2:CreateTags"],
                "Resource": "*",
                "Condition": {
                    "StringEquals": {"aws:ResourceTag/Project": "social-surveyor"}
                },
            },
        ],
    }
)

aws.iam.RolePolicy(
    "ebs-snapshot",
    role=role.id,
    policy=ebs_snapshot_policy,
)

instance_profile = aws.iam.InstanceProfile(
    "social-surveyor-instance-profile",
    name=f"social-surveyor-{project_name}-instance-profile",
    role=role.name,
    tags={"Project": "social-surveyor", "ManagedBy": "pulumi"},
)

# --- Security group: outbound-only ---

security_group = aws.ec2.SecurityGroup(
    "social-surveyor-sg",
    name=f"social-surveyor-{project_name}-sg",
    description=f"social-surveyor {project_name} - outbound only (SSM-managed)",
    vpc_id=vpc_id,
    egress=[
        {
            "protocol": "-1",
            "from_port": 0,
            "to_port": 0,
            "cidr_blocks": ["0.0.0.0/0"],
            "description": "All outbound - instance reaches Anthropic, Slack, Reddit, HN, X, GitHub",
        }
    ],
    tags={"Project": "social-surveyor", "ManagedBy": "pulumi"},
)

# --- EC2 instance ---

user_data = """#!/bin/bash
set -e
# SSM agent is pre-installed on Ubuntu 24.04 via snap; ensure it's enabled+running.
systemctl enable snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || true
systemctl restart snap.amazon-ssm-agent.amazon-ssm-agent.service 2>/dev/null || true
"""

instance_kwargs: dict = {
    "ami": ami.id,
    "instance_type": instance_type,
    "subnet_id": subnet_id,
    "vpc_security_group_ids": [security_group.id],
    "iam_instance_profile": instance_profile.name,
    "user_data": user_data,
    "root_block_device": {
        "volume_type": "gp3",
        "volume_size": 10,
        "delete_on_termination": True,
        "encrypted": True,
        "tags": {
            "Name": f"social-surveyor-{project_name}-root",
            "Project": "social-surveyor",
            "ManagedBy": "pulumi",
        },
    },
    "tags": {
        "Name": f"social-surveyor-{project_name}",
        "Project": "social-surveyor",
        "ManagedBy": "pulumi",
    },
}

if ssh_key_name:
    instance_kwargs["key_name"] = ssh_key_name

instance = aws.ec2.Instance("social-surveyor", **instance_kwargs)

# --- Outputs ---

pulumi.export("instance_id", instance.id)
pulumi.export("instance_public_dns", instance.public_dns)
pulumi.export("instance_public_ip", instance.public_ip)
pulumi.export("instance_private_ip", instance.private_ip)
pulumi.export("role_name", role.name)
pulumi.export("role_arn", role.arn)
pulumi.export("security_group_id", security_group.id)
pulumi.export("ami_id", ami.id)
pulumi.export(
    "ssm_connect_command",
    pulumi.Output.concat(
        "AWS_PROFILE=prod aws ssm start-session --target ",
        instance.id,
        " --region ",
        region,
    ),
)
