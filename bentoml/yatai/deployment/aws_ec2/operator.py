import os
import boto3
from pathlib import Path
from uuid import uuid4
import base64
import json
from botocore.exceptions import ClientError

from bentoml.utils.tempdir import TempDirectory

from bentoml.utils.lazy_loader import LazyLoader
from bentoml.saved_bundle import loader
from bentoml.yatai.deployment.operator import DeploymentOperatorBase
from bentoml.yatai.proto.deployment_pb2 import (
    ApplyDeploymentResponse,
    DeleteDeploymentResponse,
    DescribeDeploymentResponse,
    DeploymentState,
)
from bentoml.yatai.status import Status
from bentoml.utils import status_pb_to_error_code_and_message
from bentoml.utils.s3 import create_s3_bucket_if_not_exists
from bentoml.utils.ruamel_yaml import YAML
from bentoml.yatai.deployment.utils import ensure_docker_available_or_raise
from bentoml.yatai.deployment.aws_utils import (
    generate_aws_compatible_string,
    get_default_aws_region,
    ensure_sam_available_or_raise,
    validate_sam_template,
    SUCCESS_CLOUDFORMATION_STACK_STATUS,
    FAILED_CLOUDFORMATION_STACK_STATUS,
    cleanup_s3_bucket_if_exist,
)
from bentoml.utils.docker_utils import (
    to_valid_docker_image_name,
    to_valid_docker_image_version,
    validate_tag,
    containerize_bento_service,
)
from bentoml.exceptions import (
    BentoMLException,
    InvalidArgument,
    YataiDeploymentException,
)
from bentoml.yatai.proto.repository_pb2 import GetBentoRequest, BentoUri
from bentoml.yatai.proto import status_pb2
from bentoml.yatai.deployment.aws_ec2.utils import (
    build_template,
    package_template,
    deploy_template,
)

yatai_proto = LazyLoader("yatai_proto", globals(), "bentoml.yatai.proto")
SAM_TEMPLATE_NAME = "template.yml"


def _create_ecr_repo(repo_name, region):
    try:
        ecr_client = boto3.client("ecr", region)
        repository = ecr_client.create_repository(
            repositoryName=repo_name, imageScanningConfiguration={"scanOnPush": False}
        )
        registry_id = repository["repository"]["registryId"]

    except ecr_client.exceptions.RepositoryAlreadyExistsException:
        all_repositories = ecr_client.describe_repositories(repositoryNames=[repo_name])
        registry_id = all_repositories["repositories"][0]["registryId"]

    return registry_id


def _get_ecr_password(registry_id, region):
    ecr_client = boto3.client("ecr", region)
    try:
        token_data = ecr_client.get_authorization_token(registryIds=["registry_id"])
        token = token_data["authorizationData"][0]["authorizationToken"]
        registry_endpoint = token_data["authorizationData"][0]["proxyEndpoint"]
        return token, registry_endpoint

    except ClientError as error:
        if (
            error.response
            and error.response["Error"]["Code"] == "InvalidParameterException"
        ):
            raise BentoMLException(
                "Could not get token for registry {registry_id},{error}".format(
                    registry_id=registry_id, error=error.response["Error"]["Message"]
                )
            )


def _get_creds_from_token(token):
    cred_string = base64.b64decode(token).decode("ascii")
    username, password = str(cred_string).split(":")
    return username, password


def _make_user_data(username, password, registry, tag):
    base_format = """MIME-Version: 1.0
Content-Type: multipart/mixed; boundary=\"==MYBOUNDARY==\"

--==MYBOUNDARY==
Content-Type: text/cloud-config; charset=\"us-ascii\"

runcmd:

- sudo yum update -y
- sudo amazon-linux-extras install docker -y
- sudo service docker start
- sudo usermod -a -G docker ec2-user
- curl 'https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip' -o 'awscliv2.zip'
- unzip awscliv2.zip
- sudo ./aws/install
- ln -s /usr/bin/aws aws
- docker login --username {0} --password {1} {2}
- sudo docker pull {3}
- sudo docker run -p 5000:5000 {3}

--==MYBOUNDARY==--
""".format(
        username, password, registry, tag
    )
    encoded = base64.b64encode(base_format.encode("ascii")).decode("ascii")
    return encoded


def _make_cloudformation_template(
    project_dir,
    user_data,
    s3_bucket_name,
    sam_template_name,
    ami_id,
    instance_type,
    autoscaling_min_size,
    autoscaling_desired_size,
    autoscaling_max_size,
):
    """
    NOTE: SSH ACCESS TO INSTANCE MAY NOT BE REQUIRED
    """
    if (
        autoscaling_min_size <= 0
        or autoscaling_max_size < autoscaling_min_size
        or autoscaling_desired_size < autoscaling_min_size
        or autoscaling_desired_size > autoscaling_max_size
    ):
        raise BentoMLException(
            "Wrong autoscaling capacity specified.It should be min_size <= desired_size <= max_size"
        )

    template_file_path = os.path.join(project_dir, sam_template_name)
    yaml = YAML()
    sam_config = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Transform": "AWS::Serverless-2016-10-31",
        "Description": "BentoML load balanced template template",
        "Parameters": {
            "AmazonLinux2LatestAmiId": {
                "Type": "AWS::SSM::Parameter::Value<AWS::EC2::Image::Id>",
                "Default": ami_id,
            },
        },
    }
    yaml.dump(sam_config, Path(template_file_path))

    with open(template_file_path, "a") as f:
        f.write(
            """\
Resources:
    SecurityGroupResource:
        Type: AWS::EC2::SecurityGroup
        Properties:
            GroupDescription: "security group for bentoservice"
            SecurityGroupIngress:
                -
                    IpProtocol: tcp
                    CidrIp: 0.0.0.0/0
                    FromPort: 5000
                    ToPort: 5000
                -
                    IpProtocol: tcp
                    CidrIp: 0.0.0.0/0
                    FromPort: 22
                    ToPort: 22

    LaunchTemplateResource:
        Type: AWS::EC2::LaunchTemplate
        Properties:
            LaunchTemplateName: {template_name}
            #Key and security gorups remainign for logging in
            LaunchTemplateData:
                ImageId: !Ref AmazonLinux2LatestAmiId
                InstanceType: {instance_type}
                UserData: "{user_data}"
                SecurityGroupIds:
                - !GetAtt SecurityGroupResource.GroupId
    
    AutoScalingGroup:
        Type: AWS::AutoScaling::AutoScalingGroup
        Properties:
            MinSize: {autoscaling_min_size}
            MaxSize: {autoscaling_max_size}
            DesiredCapacity: {autoscaling_desired_size}
            AvailabilityZones: !GetAZs
            LaunchTemplate: 
                LaunchTemplateId: !Ref LaunchTemplateResource
                Version: !GetAtt LaunchTemplateResource.LatestVersionNumber
Outputs: 
    S3Bucket:
        Value: {s3_bucket_name},
        Description: 'S3 Bucket for saving artifacts and lambda bundle'
""".format(
                template_name=sam_template_name,
                instance_type=instance_type,
                user_data=user_data,
                autoscaling_min_size=autoscaling_min_size,
                autoscaling_desired_size=autoscaling_desired_size,
                autoscaling_max_size=autoscaling_max_size,
                s3_bucket_name=s3_bucket_name,
            )
        )
    return template_file_path


class AwsEc2DeploymentOperator(DeploymentOperatorBase):
    def deploy_service(
        self,
        deployment_pb,
        deployment_spec,
        bento_path,
        aws_ec2_deployment_config,
        s3_bucket_name,
        region,
    ):
        sam_template_name = "template.yml"

        deployment_stack_name = generate_aws_compatible_string(
            "btml-stack-{namespace}-{name}".format(
                namespace=deployment_pb.namespace, name=deployment_pb.name
            )
        )

        repo_name = generate_aws_compatible_string(
            "btml-repo-{namespace}-{name}".format(
                namespace=deployment_pb.namespace, name=deployment_pb.name
            )
        )

        with TempDirectory() as project_path:
            registry_id = _create_ecr_repo(repo_name, region)
            registry_token, registry_url = _get_ecr_password(registry_id, region)
            registry_username, registry_password = _get_creds_from_token(registry_token)

            registry_domain = registry_url.replace("https://", "")
            tag = f"{registry_domain}/{repo_name}"

            containerize_bento_service(
                bento_name=deployment_spec.bento_name,
                bento_version=deployment_spec.bento_version,
                saved_bundle_path=bento_path,
                push=True,
                tag=tag,
                build_arg={},
                username=registry_username,
                password=registry_password,
            )

            encoded_user_data = _make_user_data(
                registry_username, registry_password, registry_url, tag
            )

            template_file_path = _make_cloudformation_template(
                project_path,
                encoded_user_data,
                s3_bucket_name,
                sam_template_name,
                aws_ec2_deployment_config.ami_id,
                aws_ec2_deployment_config.instance_type,
                aws_ec2_deployment_config.autoscale_min_capacity,
                aws_ec2_deployment_config.autoscale_desired_capacity,
                aws_ec2_deployment_config.autoscale_max_capacity,
            )

            validate_sam_template(
                sam_template_name, aws_ec2_deployment_config.region, project_path
            )

            build_template(
                template_file_path, project_path, aws_ec2_deployment_config.region
            )

            package_template(
                s3_bucket_name, project_path, aws_ec2_deployment_config.region
            )

            deploy_template(
                deployment_stack_name,
                s3_bucket_name,
                project_path,
                aws_ec2_deployment_config.region,
            )

    def add(self, deployment_pb):
        try:
            deployment_spec = deployment_pb.spec
            deployment_spec.aws_ec2_operator_config.region = (
                deployment_spec.aws_ec2_operator_config.region
                or get_default_aws_region()
            )
            if not deployment_spec.aws_ec2_operator_config.region:
                raise InvalidArgument("AWS region is missing")

            ensure_sam_available_or_raise()
            ensure_docker_available_or_raise()

            bento_pb = self.yatai_service.GetBento(
                GetBentoRequest(
                    bento_name=deployment_spec.bento_name,
                    bento_version=deployment_spec.bento_version,
                )
            )

            if bento_pb.bento.uri.type not in (BentoUri.LOCAL, BentoUri.S3):
                raise BentoMLException(
                    "BentoML currently not support {} repository".format(
                        BentoUri.StorageType.Name(bento_pb.bento.uri.type)
                    )
                )
            bento_path = bento_pb.bento.uri.uri

            return self._add(deployment_pb, bento_pb, bento_path)
        except BentoMLException as error:
            # raise error
            deployment_pb.state.state = DeploymentState.ERROR
            print("error is ", error)
            print("proto ", error.status_proto)
            deployment_pb.state.error_message = f"Error: {str(error)}"
            print("deployment pb ", deployment_pb)
            return ApplyDeploymentResponse(
                status=error.status_proto, deployment=deployment_pb
            )

    def _add(self, deployment_pb, bento_pb, bento_path):
        try:
            if loader._is_remote_path(bento_path):
                with loader._resolve_remote_bundle_path(bento_path) as local_path:
                    return self._add(deployment_pb, bento_pb, local_path)

            deployment_spec = deployment_pb.spec
            aws_ec2_deployment_config = deployment_spec.aws_ec2_operator_config

            artifact_s3_bucket_name = generate_aws_compatible_string(
                "btml-{namespace}-{name}-{random_string}".format(
                    namespace=deployment_pb.namespace,
                    name=deployment_pb.name,
                    random_string=uuid4().hex[:6].lower(),
                )
            )
            create_s3_bucket_if_not_exists(
                artifact_s3_bucket_name, aws_ec2_deployment_config.region
            )

            self.deploy_service(
                deployment_pb,
                deployment_spec,
                bento_path,
                aws_ec2_deployment_config,
                artifact_s3_bucket_name,
                aws_ec2_deployment_config.region,
            )
        except BentoMLException as error:
            if artifact_s3_bucket_name and aws_ec2_deployment_config.region:
                cleanup_s3_bucket_if_exist(
                    artifact_s3_bucket_name, aws_ec2_deployment_config.region
                )
            raise error
        return ApplyDeploymentResponse(status=Status.OK(), deployment=deployment_pb)

    def delete(self, deployment_pb):
        try:
            deployment_spec = deployment_pb.spec
            ec2_deployment_config = deployment_spec.aws_ec2_operator_config
            ec2_deployment_config.region = (
                ec2_deployment_config.region or get_default_aws_region()
            )
            if not ec2_deployment_config.region:
                raise InvalidArgument("AWS region is missing")

            # delete stack
            deployment_spec = deployment_pb
            deployment_stack_name = generate_aws_compatible_string(
                "btml-stack-{namespace}-{name}".format(
                    namespace=deployment_pb.namespace, name=deployment_pb.name
                )
            )

            cf_client = boto3.client("cloudformation", ec2_deployment_config.region)
            cf_client.delete_stack(StackName=deployment_stack_name)

            # delete repo from ecr
            repository_name = generate_aws_compatible_string(
                "btml-repo-{namespace}-{name}".format(
                    namespace=deployment_pb.namespace, name=deployment_pb.name
                )
            )
            ecr_client = boto3.client("ecr", ec2_deployment_config.region)
            ecr_client.delete_repository(repositoryName=repository_name)
            return DeleteDeploymentResponse(status=Status.OK())
        except BentoMLException as error:
            return DeleteDeploymentResponse(status=error.status_proto)

    def update(self, deployment_pb, previous_deployment):
        try:
            ensure_sam_available_or_raise()
            ensure_docker_available_or_raise()
            deployment_spec = deployment_pb.spec
            ec2_deployment_config = deployment_spec.aws_ec2_operator_config
            ec2_deployment_config.region = (
                ec2_deployment_config.region or get_default_aws_region()
            )
            if not ec2_deployment_config.region:
                raise InvalidArgument("AWS region is missing")

            bento_pb = self.yatai_service.GetBento(
                GetBentoRequest(
                    bento_name=deployment_spec.bento_name,
                    bento_version=deployment_spec.bento_version,
                )
            )

            if bento_pb.bento.uri.type not in (BentoUri.LOCAL, BentoUri.S3):
                raise BentoMLException(
                    "BentoML currently not support {} repository".format(
                        BentoUri.StorageType.Name(bento_pb.bento.uri.type)
                    )
                )

            return self._update(
                deployment_pb,
                previous_deployment,
                bento_pb.bento.uri.uri,
                ec2_deployment_config.region,
            )
        except BentoMLException as error:
            # raise error
            deployment_pb.state.state = DeploymentState.ERROR
            print("error is ", error)
            print("proto ", error.status_proto)
            deployment_pb.state.error_message = f"Error: {str(error)}"
            print("deployment pb ", deployment_pb)
            return ApplyDeploymentResponse(
                status=error.status_proto, deployment=deployment_pb
            )

    def _update(self, deployment_pb, previous_deployment_pb, bento_path, region):
        if loader._is_remote_path(bento_path):
            with loader._resolve_remote_bundle_path(bento_path) as local_path:
                return self._update(
                    deployment_pb, previous_deployment_pb, local_path, region
                )

        updated_deployment_spec = deployment_pb.spec
        updated_deployment_config = updated_deployment_spec.aws_ec2_operator_config

        describe_result = self.describe(deployment_pb)
        if describe_result.status.status_code != status_pb2.Status.OK:
            error_code, error_message = status_pb_to_error_code_and_message(
                describe_result.status
            )
            raise YataiDeploymentException(
                f"Failed fetching Lambda deployment current status - "
                f"{error_code}:{error_message}"
            )

        previous_deployment_state = json.loads(describe_result.state.info_json)

        if "s3_bucket" in previous_deployment_state:
            s3_bucket_name = previous_deployment_state["s3_bucket"]
        else:
            raise BentoMLException(
                "S3 Bucket is missing in the AWS Lambda deployment, please make sure "
                "it exists and try again"
            )

        self.deploy_service(
            deployment_pb,
            updated_deployment_spec,
            bento_path,
            updated_deployment_config,
            s3_bucket_name,
            region,
        )

        return ApplyDeploymentResponse(status=Status.OK(), deployment=deployment_pb)

    def describe(self, deployment_pb):
        try:
            deployment_spec = deployment_pb.spec
            ec2_deployment_config = deployment_spec.aws_ec2_operator_config
            ec2_deployment_config.region = (
                ec2_deployment_config.region or get_default_aws_region()
            )
            if not ec2_deployment_config.region:
                raise InvalidArgument("AWS region is missing")

            deployment_stack_name = generate_aws_compatible_string(
                "btml-stack-{namespace}-{name}".format(
                    namespace=deployment_pb.namespace, name=deployment_pb.name
                )
            )
            # deployment_stack_name = "mutract-stack"  # TODO: DELETE THIHS
            try:
                cf_client = boto3.client("cloudformation", ec2_deployment_config.region)

                cloudformation_stack_result = cf_client.describe_stacks(
                    StackName=deployment_stack_name
                )
                stack_result = cloudformation_stack_result.get("Stacks")[0]

                if stack_result["StackStatus"] in SUCCESS_CLOUDFORMATION_STACK_STATUS:
                    if stack_result.get("Outputs"):
                        outputs = stack_result.get("Outputs")
                    else:
                        return DescribeDeploymentResponse(
                            status=Status.ABORTED('"Outputs" field is not present'),
                            state=DeploymentState(
                                state=DeploymentState.ERROR,
                                error_message='"Outputs" field is not present',
                            ),
                        )

                elif stack_result["StackStatus"] in FAILED_CLOUDFORMATION_STACK_STATUS:
                    state = DeploymentState(state=DeploymentState.FAILED)
                    return DescribeDeploymentResponse(status=Status.OK(), state=state)

                else:
                    state = DeploymentState(state=DeploymentState.PENDING)
                    return DescribeDeploymentResponse(status=Status.OK(), state=state)

            except Exception as error:  # pylint: disable=broad-except
                state = DeploymentState(
                    state=DeploymentState.ERROR, error_message=str(error)
                )
                return DescribeDeploymentResponse(
                    status=Status.INTERNAL(str(error)), state=state
                )

            outputs = {o["OutputKey"]: o["OutputValue"] for o in outputs}
            info_json = {}

            if "S3Bucket" in outputs:
                info_json["s3_bucket"] = outputs["S3Bucket"]

            state = DeploymentState(
                state=DeploymentState.RUNNING, info_json=json.dumps(info_json)
            )
            return DescribeDeploymentResponse(status=Status.OK(), state=state)

        except BentoMLException as error:
            return DescribeDeploymentResponse(status=error.status_proto)
