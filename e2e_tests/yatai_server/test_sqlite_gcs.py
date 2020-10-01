import logging

from bentoml.yatai.proto.repository_pb2 import BentoUri
from e2e_tests.sample_bento_service import SampleBentoService
from e2e_tests.cli_operations import delete_bento
from e2e_tests.yatai_server.utils import (
    local_yatai_server,
    get_bento_service_info,
    execute_bentoml_run_command,
)

logger = logging.getLogger("bentoml.test")


def test_yatai_server_with_sqlite_and_gcs():
    gcs_bucket_name = "gs://bentoml-e2e-tests/"

    with local_yatai_server(repo_base_url=gcs_bucket_name):
        logger.info("Saving bento service")
        svc = SampleBentoService()
        svc.save()
        bento_tag = f"{svc.name}:{svc.version}"
        logger.info("BentoService saved")

        logger.info("Display bentoservice info")
        get_svc_result = get_bento_service_info(svc.name, svc.version)
        logger.info(get_svc_result)
        assert (
            get_svc_result.bento.uri.type == BentoUri.GCS
        ), "BentoService storage type mismatched, expect GCS"

        logger.info("Validate BentoService prediction result")
        run_result = execute_bentoml_run_command(bento_tag, "[]")
        logger.info(run_result)
        assert "cat" in run_result, "Unexpected BentoService prediction result"

        logger.info(f"Deleting saved bundle {bento_tag}")
        delete_svc_result = delete_bento(bento_tag)
        assert f"{bento_tag} deleted" in delete_svc_result
