# Copyright 2019 Atalaya Tech, Inc.

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at

# http://www.apache.org/licenses/LICENSE-2.0

# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import click

from bentoml.utils import status_pb_to_error_code_and_message
from bentoml.utils.lazy_loader import LazyLoader
from bentoml.cli.utils import Spinner
from bentoml.utils import get_default_yatai_client
from bentoml.cli.click_utils import (
    BentoMLCommandGroup,
    parse_bento_tag_callback,
    _echo,
    CLI_COLOR_SUCCESS,
    parse_labels_callback,
)
from bentoml.cli.deployment import (
    _print_deployment_info,
    _print_deployments_info,
)
from bentoml.yatai.deployment import ALL_NAMESPACE_TAG
from bentoml.exceptions import CLIException

yatai_proto = LazyLoader("yatai_proto", globals(), "bentoml.yatai.proto")


def get_aws_ec2_sub_command():
    @click.group(name="ec2", cls=BentoMLCommandGroup, help="commandas for ec2")
    def aws_ec2():
        pass

    @aws_ec2.command(help="Deploy BentoServide to ec2")
    @click.argument("name", type=click.STRING)
    @click.option(
        "-b",
        "--bento",
        type=click.STRING,
        required=True,
        callback=parse_bento_tag_callback,
    )
    @click.option(
        "--region",
        type=click.STRING,
        default="ap-south-1",
        help="Region to deploy service in",
    )
    @click.option(
        "--min_capacity",
        type=click.INT,
        default=1,
        help="The minimum limit helps ensure that you always have a certain number of instances running at all times.\
            Default is 1",
    )
    @click.option(
        "--desired_capacity",
        type=click.INT,
        default=1,
        help="Desired number of instances capacity to run bentoservice on.Should be between minimum and maximum capacities.\
            Default is 1",
    )
    @click.option(
        "--max_capacity",
        type=click.INT,
        default=1,
        help="The maximum limit lets Amazon EC2 Auto Scaling scale out the number of instances as needed to handle an increase in demand\
            Default is 1",
    )
    @click.option(
        "--instance_type",
        type=click.STRING,
        default="t2.micro",
        help="Instance type of ec2 container.Default is t2.micro",
    )
    @click.option(
        "--ami_id",
        type=click.STRING,
        default="/aws/service/ami-amazon-linux-latest/amzn2-ami-hvm-x86_64-gp2",
        help="AMI id.Default is Amazon Linux 2",
    )
    def deploy(
        name,
        bento,
        region,
        min_capacity,
        desired_capacity,
        max_capacity,
        instance_type,
        ami_id,
    ):
        yatai_client = get_default_yatai_client()
        bento_name, bento_version = bento.split(":")
        result = yatai_client.deployment.create_ec2_deployment(
            name=name,
            bento_name=bento_name,
            bento_version=bento_version,
            region=region,
            min_capacity=min_capacity,
            desired_capacity=desired_capacity,
            max_capacity=max_capacity,
            instance_type=instance_type,
            ami_id=ami_id,
        )

        _echo(f"Successfully created AWS EC2 deployment ", CLI_COLOR_SUCCESS)

    @aws_ec2.command(help="Delete AWS EC2 deployment")
    @click.argument("name", type=click.STRING)
    @click.option(
        "-n",
        "--namespace",
        type=click.STRING,
        help='Deployment namespace managed by BentoML, default value is "dev" which'
        "can be changed in BentoML configuration yatai_service/default_namespace",
    )
    @click.option(
        "--force",
        is_flag=True,
        help="force delete the deployment record in database and "
        "ignore errors when deleting cloud resources",
    )
    def delete(name, namespace, force):
        yatai_client = get_default_yatai_client()
        get_deployment_result = yatai_client.deployment.get(
            namespace=namespace, name=name
        )
        if get_deployment_result.status.status_code != yatai_proto.status_pb2.Status.OK:
            error_code, error_message = status_pb_to_error_code_and_message(
                get_deployment_result.status
            )
            raise CLIException(f"{error_code}:{error_message}")

        delete_deployment_result = yatai_client.deployment.delete(
            namespace=namespace, deployment_name=name, force_delete=force
        )
        if (
            delete_deployment_result.status.status_code
            != yatai_proto.status_pb2.Status.OK
        ):
            error_code, error_message = status_pb_to_error_code_and_message(
                get_deployment_result.status
            )
            raise CLIException(f"{error_code}:{error_message}")

        _echo(f"Successfiully deleted AWS EC2 deployment '{name}'", CLI_COLOR_SUCCESS)

    @aws_ec2.command(help="Get EC2 deployment")
    @click.argument("name", type=click.STRING)
    @click.option(
        "-n",
        "--namespace",
        type=click.STRING,
        help='Deployment namespace managed by BentoML, default value is "dev" which '
        'can be changed in BentoML configuration yatai_service/default_namespace',
    )
    @click.option(
        "-o", "--output", type=click.Choice(["json", "yaml", "table"]), default="json"
    )
    def get(name, namespace, output):
        yatai_client = get_default_yatai_client()
        describe_result = yatai_client.deployment.describe(namespace, name)

        if describe_result.status.status_code != yatai_proto.status_pb2.Status.OK:
            error_code, error_message = status_pb_to_error_code_and_message(
                describe_result.status
            )
            raise CLIException(f"{error_code}:{error_message}")

        get_result = yatai_client.deployment.get(namespace, name)
        if get_result.status.status_code != yatai_proto.status_pb2.Status.OK:
            error_code, error_message = status_pb_to_error_code_and_message(
                describe_result.status
            )
            raise CLIException(f"{error_code}:{error_message}")

        _print_deployment_info(get_result.deployment, output)

    @aws_ec2.command(help="Update existing AWS EC2 deployments")
    @click.argument("name", type=click.STRING)
    @click.option(
        "-b",
        "--bento",
        "--bento-service-bundle",
        type=click.STRING,
        callback=parse_bento_tag_callback,
        help="Target BentoService to be deployed, referenced by its name and version "
        'in format of name:version. For example: "iris_classifier:v1.2.0"',
    )
    @click.option(
        "-n",
        "--namespace",
        type=click.STRING,
        help='Deployment namespace managed by BentoML, default value is "dev" which '
        'can be changed in BentoML configuration yatai_service/default_namespace',
    )
    @click.option(
        "-o", "--output", type=click.Choice(["json", "yaml", "table"]), default="json"
    )
    @click.option(
        "--wait/--no-wait",
        default=True,
        help="Wait for apply action to complete or encounter an error."
        "If set to no-wait, CLI will return immediately. The default value is wait",
    )
    def update(name, namespace, bento, output, wait):
        yatai_client = get_default_yatai_client()
        if bento:
            bento_name, bento_version = bento.split(":")
        else:
            bento_name = None
            bento_version = None

        with Spinner("Updating EC2 deployment"):
            update_result = yatai_client.deployment.update_ec2_deployment(
                deployment_name=name,
                bento_name=bento_name,
                bento_version=bento_version,
                namespace=namespace,
                wait=wait,
            )
            if update_result.status.status_code != yatai_proto.status_pb2.Status.OK:
                error_code, error_message = status_pb_to_error_code_and_message(
                    update_result.status
                )
                raise CLIException(f"{error_code}:{error_message}")

        _print_deployment_info(update_result.deployment, output)

    @aws_ec2.command(name="list", help="List AWS Lambda deployments")
    @click.option(
        "-n",
        "--namespace",
        type=click.STRING,
        help='Deployment namespace managed by BentoML, default value is "dev" which '
        'can be changed in BentoML configuration yatai_service/default_namespace',
        default=ALL_NAMESPACE_TAG,
    )
    @click.option(
        "--limit",
        type=click.INT,
        help="The maximum amount of AWS Lambda deployments to be listed at once",
    )
    @click.option(
        "--offset",
        type=click.INT,
        help="The offset for list of AWS Lambda deployments",
    )
    @click.option(
        "-l",
        "--labels",
        type=click.STRING,
        help="Label query to filter Lambda deployments, supports '=', '!=', 'IN', "
        "'NotIn', 'Exists', and 'DoesNotExist'. (e.g. key1=value1, "
        "key2!=value2, key3 In (value3, value3a), key4 DoesNotExist)",
    )
    @click.option(
        "--order-by", type=click.Choice(["created_at", "name"]), default="created_at",
    )
    @click.option(
        "--asc/--desc",
        default=False,
        help="Ascending or descending order for list deployments",
    )
    @click.option(
        "-o",
        "--output",
        type=click.Choice(["json", "yaml", "table", "wide"]),
        default="table",
    )
    def list_deployments(namespace, limit, offset, labels, order_by, asc, output):
        yatai_client = get_default_yatai_client()
        list_result = yatai_client.deployment.list_ec2_deployments(
            limit=limit,
            labels=labels,
            offset=offset,
            namespace=namespace,
            order_by=order_by,
            ascending_order=asc,
        )
        if list_result.status.status_code != yatai_proto.status_pb2.Status.OK:
            error_code, error_message = status_pb_to_error_code_and_message(
                list_result.status
            )
            raise CLIException(f"{error_code}:{error_message}")
        else:
            _print_deployments_info(list_result.deployments, output)

    return aws_ec2
