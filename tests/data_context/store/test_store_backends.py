import datetime
import json
import os
from unittest import mock

import boto3
import pyparsing as pp
import pytest
from moto import mock_s3

import tests.test_utils as test_utils
from great_expectations.core.data_context_key import DataContextVariableKey
from great_expectations.core.expectation_suite import ExpectationSuite
from great_expectations.core.run_identifier import RunIdentifier
from great_expectations.core.yaml_handler import YAMLHandler
from great_expectations.data_context import DataContext
from great_expectations.data_context.data_context_variables import (
    DataContextVariableSchema,
)
from great_expectations.data_context.store import (
    InMemoryStoreBackend,
    StoreBackend,
    TupleAzureBlobStoreBackend,
    TupleFilesystemStoreBackend,
    TupleGCSStoreBackend,
    TupleS3StoreBackend,
)
from great_expectations.data_context.store.inline_store_backend import (
    InlineStoreBackend,
)
from great_expectations.data_context.types.base import DataContextConfig
from great_expectations.data_context.types.resource_identifiers import (
    ExpectationSuiteIdentifier,
    ValidationResultIdentifier,
)
from great_expectations.data_context.util import file_relative_path
from great_expectations.exceptions import InvalidKeyError, StoreBackendError, StoreError
from great_expectations.self_check.util import expectationSuiteSchema
from great_expectations.util import (
    gen_directory_tree_str,
    get_context,
    is_library_loadable,
)

yaml = YAMLHandler()


@pytest.fixture()
def basic_data_context_config_for_validation_operator():
    return DataContextConfig(
        config_version=2,
        plugins_directory=None,
        evaluation_parameter_store_name="evaluation_parameter_store",
        expectations_store_name="expectations_store",
        datasources={},
        stores={
            "expectations_store": {"class_name": "ExpectationsStore"},
            "evaluation_parameter_store": {"class_name": "EvaluationParameterStore"},
            "validation_result_store": {"class_name": "ValidationsStore"},
            "metrics_store": {"class_name": "MetricStore"},
        },
        validations_store_name="validation_result_store",
        data_docs_sites={},
        validation_operators={
            "store_val_res_and_extract_eval_params": {
                "class_name": "ActionListValidationOperator",
                "action_list": [
                    {
                        "name": "store_validation_result",
                        "action": {
                            "class_name": "StoreValidationResultAction",
                            "target_store_name": "validation_result_store",
                        },
                    },
                    {
                        "name": "extract_and_store_eval_parameters",
                        "action": {
                            "class_name": "StoreEvaluationParametersAction",
                            "target_store_name": "evaluation_parameter_store",
                        },
                    },
                ],
            },
            "errors_and_warnings_validation_operator": {
                "class_name": "WarningAndFailureExpectationSuitesValidationOperator",
                "action_list": [
                    {
                        "name": "store_validation_result",
                        "action": {
                            "class_name": "StoreValidationResultAction",
                            "target_store_name": "validation_result_store",
                        },
                    },
                    {
                        "name": "extract_and_store_eval_parameters",
                        "action": {
                            "class_name": "StoreEvaluationParametersAction",
                            "target_store_name": "evaluation_parameter_store",
                        },
                    },
                ],
            },
        },
    )


@pytest.fixture
def validation_operators_data_context(
    basic_data_context_config_for_validation_operator, filesystem_csv_4
):
    data_context = get_context(basic_data_context_config_for_validation_operator)

    data_context.add_datasource(
        "my_datasource",
        class_name="PandasDatasource",
        batch_kwargs_generators={
            "subdir_reader": {
                "class_name": "SubdirReaderBatchKwargsGenerator",
                "base_directory": str(filesystem_csv_4),
            }
        },
    )
    data_context.add_expectation_suite("f1.foo")

    df = data_context.get_batch(
        batch_kwargs=data_context.build_batch_kwargs(
            "my_datasource", "subdir_reader", "f1"
        ),
        expectation_suite_name="f1.foo",
    )
    df.expect_column_values_to_be_between(column="x", min_value=1, max_value=9)
    failure_expectations = df.get_expectation_suite(discard_failed_expectations=False)

    df.expect_column_values_to_not_be_null(column="y")
    warning_expectations = df.get_expectation_suite(discard_failed_expectations=False)

    failure_expectations.expectation_suite_name = "f1.failure"
    data_context.add_expectation_suite(expectation_suite=failure_expectations)
    warning_expectations.expectation_suite_name = "f1.warning"
    data_context.add_expectation_suite(expectation_suite=warning_expectations)

    return data_context


@pytest.fixture()
def parameterized_expectation_suite(empty_data_context_stats_enabled):
    context: DataContext = empty_data_context_stats_enabled
    fixture_path = file_relative_path(
        __file__,
        "../../test_fixtures/expectation_suites/parameterized_expression_expectation_suite_fixture.json",
    )
    with open(
        fixture_path,
    ) as suite:
        expectation_suite_dict: dict = expectationSuiteSchema.load(json.load(suite))
        return ExpectationSuite(**expectation_suite_dict, data_context=context)


@pytest.mark.unit
def test_StoreBackendValidation():
    backend = InMemoryStoreBackend()

    backend._validate_key(("I", "am", "a", "string", "tuple"))

    with pytest.raises(TypeError):
        backend._validate_key("nope")

    with pytest.raises(TypeError):
        backend._validate_key(("I", "am", "a", "string", 100))

    with pytest.raises(TypeError):
        backend._validate_key(("I", "am", "a", "string", None))

    # zero-length tuple is allowed
    backend._validate_key(())


def check_store_backend_store_backend_id_functionality(
    store_backend: StoreBackend, store_backend_id: str = None
) -> None:
    """
    Assertions to check if a store backend is handling reading and writing a store_backend_id appropriately.
    Args:
        store_backend: Instance of subclass of StoreBackend to test e.g. TupleFilesystemStoreBackend
        store_backend_id: Manually input store_backend_id
    Returns:
        None
    """
    # Check that store_backend_id exists can be read
    assert store_backend.store_backend_id is not None
    store_error_uuid = "00000000-0000-0000-0000-00000000e003"
    assert store_backend.store_backend_id != store_error_uuid
    if store_backend_id:
        assert store_backend.store_backend_id == store_backend_id
    # Check that store_backend_id is a valid UUID
    assert test_utils.validate_uuid4(store_backend.store_backend_id)
    # Check in file stores that the actual file exists
    assert store_backend.has_key(key=(".ge_store_backend_id",))

    # Check file stores for the file in the correct format
    store_backend_id_from_file = store_backend.get(key=(".ge_store_backend_id",))
    store_backend_id_file_parser = "store_backend_id = " + pp.Word(pp.hexnums + "-")
    parsed_store_backend_id = store_backend_id_file_parser.parseString(
        store_backend_id_from_file
    )
    assert test_utils.validate_uuid4(parsed_store_backend_id[1])


@pytest.mark.integration
@mock_s3
def test_StoreBackend_id_initialization(tmp_path_factory):
    """
    What does this test and why?

    A StoreBackend should have a store_backend_id property. That store_backend_id should be read and initialized
    from an existing persistent store_backend_id during instantiation, or a new store_backend_id should be generated
    and persisted. The store_backend_id should be a valid UUIDv4
    If a new store_backend_id cannot be persisted, use an ephemeral store_backend_id.
    Persistence should be in a .ge_store_backend_id file for for filesystem and blob-stores.

    Note: StoreBackend & TupleStoreBackend are abstract classes, so we will test the
    concrete classes that inherit from them.
    See also test_database_store_backend::test_database_store_backend_id_initialization
    """

    # InMemoryStoreBackend
    # Initialize without store_backend_id and check that it is generated correctly
    in_memory_store_backend = InMemoryStoreBackend()
    check_store_backend_store_backend_id_functionality(
        store_backend=in_memory_store_backend
    )

    # Create a new store with the same config and make sure it reports the same store_backend_id
    # in_memory_store_backend_duplicate = InMemoryStoreBackend()
    # assert in_memory_store_backend.store_backend_id == in_memory_store_backend_duplicate.store_backend_id
    # This is not currently implemented for the InMemoryStoreBackend, the store_backend_id is ephemeral since
    # there is no place to persist it.

    # TupleFilesystemStoreBackend
    # Initialize without store_backend_id and check that it is generated correctly
    path = "dummy_str"
    full_test_dir = tmp_path_factory.mktemp("test_StoreBackend_id_initialization__dir")
    test_dir = full_test_dir.parts[-1]
    project_path = str(full_test_dir)

    tuple_filesystem_store_backend = TupleFilesystemStoreBackend(
        root_directory=project_path,
        base_directory=os.path.join(project_path, path),
    )
    # Check that store_backend_id is created on instantiation, before being accessed
    desired_directory_tree_str = f"""\
{test_dir}/
    dummy_str/
        .ge_store_backend_id
"""
    assert gen_directory_tree_str(project_path) == desired_directory_tree_str
    check_store_backend_store_backend_id_functionality(
        store_backend=tuple_filesystem_store_backend
    )
    assert gen_directory_tree_str(project_path) == desired_directory_tree_str

    # Repeat the above with a filepath template
    full_test_dir_with_file_template = tmp_path_factory.mktemp(
        "test_StoreBackend_id_initialization__dir"
    )
    test_dir_with_file_template = full_test_dir_with_file_template.parts[-1]
    project_path_with_filepath_template = str(full_test_dir_with_file_template)

    tuple_filesystem_store_backend_with_filepath_template = TupleFilesystemStoreBackend(
        root_directory=os.path.join(project_path, path),
        base_directory=project_path_with_filepath_template,
        filepath_template="my_file_{0}",
    )
    check_store_backend_store_backend_id_functionality(
        store_backend=tuple_filesystem_store_backend_with_filepath_template
    )
    assert (
        gen_directory_tree_str(project_path_with_filepath_template)
        == f"""\
{test_dir_with_file_template}/
    .ge_store_backend_id
"""
    )

    # Create a new store with the same config and make sure it reports the same store_backend_id
    tuple_filesystem_store_backend_duplicate = TupleFilesystemStoreBackend(
        root_directory=project_path,
        base_directory=os.path.join(project_path, path),
        # filepath_template="my_file_{0}",
    )
    check_store_backend_store_backend_id_functionality(
        store_backend=tuple_filesystem_store_backend_duplicate
    )
    assert (
        tuple_filesystem_store_backend.store_backend_id
        == tuple_filesystem_store_backend_duplicate.store_backend_id
    )

    # TupleS3StoreBackend
    # Initialize without store_backend_id and check that it is generated correctly
    bucket = "leakybucket"
    prefix = "this_is_a_test_prefix"

    # create a bucket in Moto's mock AWS environment
    conn = boto3.resource("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    s3_store_backend = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )

    check_store_backend_store_backend_id_functionality(store_backend=s3_store_backend)

    # Create a new store with the same config and make sure it reports the same store_backend_id
    s3_store_backend_duplicate = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )
    check_store_backend_store_backend_id_functionality(
        store_backend=s3_store_backend_duplicate
    )
    assert (
        s3_store_backend.store_backend_id == s3_store_backend_duplicate.store_backend_id
    )

    # TupleGCSStoreBackend
    # TODO: Improve GCS Testing e.g. using a docker service to mock
    # Note: Currently there is not a great way to mock GCS so here we are just testing that a config
    # with unreachable bucket returns the error store backend id
    # If we were to mock GCS, we would need to provide the value returned from the TupleGCSStoreBackend which
    # is circumventing actually testing the store backend.

    bucket = "leakybucket"
    prefix = "this_is_a_test_prefix"
    project = "dummy-project"
    base_public_path = "http://www.test.com/"

    gcs_store_backend_with_base_public_path = TupleGCSStoreBackend(
        filepath_template=None,
        bucket=bucket,
        prefix=prefix,
        project=project,
        base_public_path=base_public_path,
    )
    store_error_uuid = "00000000-0000-0000-0000-00000000e003"
    assert gcs_store_backend_with_base_public_path.store_backend_id == store_error_uuid


@mock_s3
@pytest.mark.integration
def test_TupleS3StoreBackend_store_backend_id():
    # TupleS3StoreBackend
    # Initialize without store_backend_id and check that it is generated correctly
    bucket = "leakybucket2"
    prefix = "this_is_a_test_prefix"

    # create a bucket in Moto's mock AWS environment
    conn = boto3.resource("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    s3_store_backend = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )

    check_store_backend_store_backend_id_functionality(store_backend=s3_store_backend)

    # Create a new store with the same config and make sure it reports the same store_backend_id
    s3_store_backend_duplicate = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )

    store_error_uuid = "00000000-0000-0000-0000-00000000e003"
    assert s3_store_backend.store_backend_id != store_error_uuid
    assert s3_store_backend_duplicate.store_backend_id != store_error_uuid

    assert (
        s3_store_backend.store_backend_id == s3_store_backend_duplicate.store_backend_id
    )


@pytest.mark.unit
def test_InMemoryStoreBackend():

    my_store = InMemoryStoreBackend()

    my_key = ("A",)
    with pytest.raises(InvalidKeyError):
        my_store.get(my_key)

    my_store.set(my_key, "aaa")
    assert my_store.get(my_key) == "aaa"

    my_store.set(("B",), {"x": 1})

    assert my_store.has_key(my_key) is True
    assert my_store.has_key(("B",)) is True
    assert my_store.has_key(("A",)) is True
    assert my_store.has_key(("C",)) is False
    assert my_store.list_keys() == [(".ge_store_backend_id",), ("A",), ("B",)]

    with pytest.raises(StoreError):
        my_store.get_url_for_key(my_key)


@pytest.mark.integration
def test_tuple_filesystem_store_filepath_prefix_error(tmp_path_factory):
    path = str(
        tmp_path_factory.mktemp("test_tuple_filesystem_store_filepath_prefix_error")
    )
    project_path = str(tmp_path_factory.mktemp("my_dir"))

    with pytest.raises(StoreBackendError) as e:
        TupleFilesystemStoreBackend(
            root_directory=project_path,
            base_directory=os.path.join(project_path, path),
            filepath_prefix="invalid_prefix_ends_with/",
        )
    assert "filepath_prefix may not end with" in e.value.message

    with pytest.raises(StoreBackendError) as e:
        TupleFilesystemStoreBackend(
            root_directory=project_path,
            base_directory=os.path.join(project_path, path),
            filepath_prefix="invalid_prefix_ends_with\\",
        )
    assert "filepath_prefix may not end with" in e.value.message


@pytest.mark.integration
def test_FilesystemStoreBackend_two_way_string_conversion(tmp_path_factory):
    path = str(
        tmp_path_factory.mktemp(
            "test_FilesystemStoreBackend_two_way_string_conversion__dir"
        )
    )
    project_path = str(tmp_path_factory.mktemp("my_dir"))

    my_store = TupleFilesystemStoreBackend(
        root_directory=project_path,
        base_directory=os.path.join(project_path, path),
        filepath_template="{0}/{1}/{2}/foo-{2}-expectations.txt",
    )

    tuple_ = ("A__a", "B-b", "C")
    converted_string = my_store._convert_key_to_filepath(tuple_)
    assert converted_string == "A__a/B-b/C/foo-C-expectations.txt"

    recovered_key = my_store._convert_filepath_to_key(
        "A__a/B-b/C/foo-C-expectations.txt"
    )
    assert recovered_key == tuple_

    with pytest.raises(ValueError):
        tuple_ = ("A/a", "B-b", "C")
        converted_string = my_store._convert_key_to_filepath(tuple_)


@pytest.mark.integration
def test_TupleFilesystemStoreBackend(tmp_path_factory):
    path = "dummy_str"
    full_test_dir = tmp_path_factory.mktemp("test_TupleFilesystemStoreBackend__dir")
    test_dir = full_test_dir.parts[-1]
    project_path = str(full_test_dir)
    base_public_path = "http://www.test.com/"

    my_store = TupleFilesystemStoreBackend(
        root_directory=project_path,
        base_directory=os.path.join(project_path, path),
        filepath_template="my_file_{0}",
    )

    with pytest.raises(InvalidKeyError):
        my_store.get(("AAA",))

    my_store.set(("AAA",), "aaa")
    assert my_store.get(("AAA",)) == "aaa"

    my_store.set(("BBB",), "bbb")
    assert my_store.get(("BBB",)) == "bbb"

    assert set(my_store.list_keys()) == {(".ge_store_backend_id",), ("AAA",), ("BBB",)}
    assert (
        gen_directory_tree_str(project_path)
        == f"""\
{test_dir}/
    dummy_str/
        .ge_store_backend_id
        my_file_AAA
        my_file_BBB
"""
    )
    my_store.remove_key(("BBB",))
    with pytest.raises(InvalidKeyError):
        assert my_store.get(("BBB",)) == ""

    my_store_with_base_public_path = TupleFilesystemStoreBackend(
        root_directory=project_path,
        base_directory=os.path.join(project_path, path),
        filepath_template="my_file_{0}",
        base_public_path=base_public_path,
    )
    my_store_with_base_public_path.set(("CCC",), "ccc")
    url = my_store_with_base_public_path.get_public_url_for_key(("CCC",))
    assert url == "http://www.test.com/my_file_CCC"


@pytest.mark.integration
def test_TupleFilesystemStoreBackend_ignores_jupyter_notebook_checkpoints(
    tmp_path_factory,
):
    full_test_dir = tmp_path_factory.mktemp("things")
    test_dir = full_test_dir.parts[-1]
    project_path = str(full_test_dir)

    checkpoint_dir = os.path.join(project_path, ".ipynb_checkpoints")
    os.mkdir(checkpoint_dir)
    assert os.path.isdir(checkpoint_dir)
    nb_file = os.path.join(checkpoint_dir, "foo.json")

    with open(nb_file, "w") as f:
        f.write("")
    assert os.path.isfile(nb_file)
    my_store = TupleFilesystemStoreBackend(
        root_directory=os.path.join(project_path, "dummy_str"),
        base_directory=project_path,
    )

    my_store.set(("AAA",), "aaa")
    assert my_store.get(("AAA",)) == "aaa"

    assert (
        gen_directory_tree_str(project_path)
        == f"""\
{test_dir}/
    .ge_store_backend_id
    AAA
    .ipynb_checkpoints/
        foo.json
"""
    )

    assert set(my_store.list_keys()) == {(".ge_store_backend_id",), ("AAA",)}


@mock_s3
@pytest.mark.integration
def test_TupleS3StoreBackend_with_prefix():
    """
    What does this test test and why?

    We will exercise the store backend's set method twice and then verify
    that the we calling get and list methods will return the expected keys.

    We will also check that the objects are stored on S3 at the expected location,
    and that the correct S3 URL for the object can be retrieved.

    """
    bucket = "leakybucket"
    prefix = "this_is_a_test_prefix"
    base_public_path = "http://www.test.com/"

    # create a bucket in Moto's mock AWS environment
    conn = boto3.resource("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    my_store = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )

    # We should be able to list keys, even when empty
    keys = my_store.list_keys()
    assert len(keys) == 1

    my_store.set(("AAA",), "aaa", content_type="text/html; charset=utf-8")
    assert my_store.get(("AAA",)) == "aaa"

    obj = boto3.client("s3").get_object(Bucket=bucket, Key=prefix + "/my_file_AAA")
    assert obj["ContentType"] == "text/html; charset=utf-8"
    assert obj["ContentEncoding"] == "utf-8"

    my_store.set(("BBB",), "bbb")
    assert my_store.get(("BBB",)) == "bbb"

    assert set(my_store.list_keys()) == {("AAA",), ("BBB",), (".ge_store_backend_id",)}
    assert {
        s3_object_info["Key"]
        for s3_object_info in boto3.client("s3").list_objects_v2(
            Bucket=bucket, Prefix=prefix
        )["Contents"]
    } == {
        "this_is_a_test_prefix/.ge_store_backend_id",
        "this_is_a_test_prefix/my_file_AAA",
        "this_is_a_test_prefix/my_file_BBB",
    }

    assert (
        my_store.get_url_for_key(("AAA",))
        == f"https://s3.amazonaws.com/{bucket}/{prefix}/my_file_AAA"
    )
    assert (
        my_store.get_url_for_key(("BBB",))
        == f"https://s3.amazonaws.com/{bucket}/{prefix}/my_file_BBB"
    )

    assert my_store.remove_key(("BBB",))
    with pytest.raises(InvalidKeyError):
        my_store.get(("BBB",))
    # Check that the rest of the keys still exist in the bucket
    assert {
        s3_object_info["Key"]
        for s3_object_info in boto3.client("s3").list_objects_v2(
            Bucket=bucket, Prefix=prefix
        )["Contents"]
    } == {
        "this_is_a_test_prefix/.ge_store_backend_id",
        "this_is_a_test_prefix/my_file_AAA",
    }

    # Call remove_key on an already deleted object
    assert not my_store.remove_key(("BBB",))
    # Check that the rest of the keys still exist in the bucket
    assert {
        s3_object_info["Key"]
        for s3_object_info in boto3.client("s3").list_objects_v2(
            Bucket=bucket, Prefix=prefix
        )["Contents"]
    } == {
        "this_is_a_test_prefix/.ge_store_backend_id",
        "this_is_a_test_prefix/my_file_AAA",
    }

    # Call remove_key on a non-existent key
    assert not my_store.remove_key(("NON_EXISTENT_KEY",))
    # Check that the rest of the keys still exist in the bucket
    assert {
        s3_object_info["Key"]
        for s3_object_info in boto3.client("s3").list_objects_v2(
            Bucket=bucket, Prefix=prefix
        )["Contents"]
    } == {
        "this_is_a_test_prefix/.ge_store_backend_id",
        "this_is_a_test_prefix/my_file_AAA",
    }

    # testing base_public_path
    my_new_store = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
        base_public_path=base_public_path,
    )

    my_new_store.set(("BBB",), "bbb", content_type="text/html; charset=utf-8")

    assert (
        my_new_store.get_public_url_for_key(("BBB",))
        == "http://www.test.com/my_file_BBB"
    )


# NOTE: Chetan - 20211118: I am commenting out this test as it was introduced in: https://github.com/great-expectations/great_expectations/pull/3377
# We've decided to roll back these changes for the time being since they introduce some unintendend consequences.
#
# This is a gentle reminder to future contributors to please re-enable this test when revisiting the matter.

# @mock_s3
# def test_tuple_s3_store_backend_expectation_suite_and_validation_operator_share_prefix(
#     validation_operators_data_context: DataContext,
#     parameterized_expectation_suite: ExpectationSuite,
# ):
#     """
#     What does this test test and why?

#     In cases where an s3 store is used with the same prefix for both validations
#     and expectations, the list_keys() operation picks up files ending in .json
#     from both validations and expectations stores.

#     To avoid this issue, the expectation suite configuration, if available, is used
#     to locate the specific key for the suite in place of calling list_keys().

#     NOTE: It is an issue with _all stores_ when the result of list_keys() contain paths
#     with a period (.) and are passed to ExpectationSuiteIdentifier.from_tuple() method,
#     as happens in the DataContext.store_evaluation_parameters() method. The best fix is
#     to choose a different delimiter for generating expectation suite identifiers (or
#     perhaps escape the period in path names).

#     For now, the fix is to avoid the call to list_keys() in
#     DataContext.store_evaluation_parameters() if the expectation suite is known (from config).
#     This approach should also improve performance.

#     This test confirms the fix for GitHub issue #3054.
#     """
#     bucket = "leakybucket"
#     prefix = "this_is_a_test_prefix"

#     # create a bucket in Moto's mock AWS environment
#     conn = boto3.resource("s3", region_name="us-east-1")
#     conn.create_bucket(Bucket=bucket)

#     # replace store backends with the mock, both with the same prefix (per issue #3054)
#     validation_operators_data_context.validations_store._store_backend = (
#         TupleS3StoreBackend(
#             bucket=bucket,
#             prefix=prefix,
#         )
#     )
#     validation_operators_data_context.expectations_store._store_backend = (
#         TupleS3StoreBackend(
#             bucket=bucket,
#             prefix=prefix,
#         )
#     )

#     validation_operators_data_context.save_expectation_suite(
#         parameterized_expectation_suite, "param_suite"
#     )

#     # ensure the suite is in the context
#     assert validation_operators_data_context.expectations_store.has_key(
#         ExpectationSuiteIdentifier("param_suite")
#     )

#     res = validation_operators_data_context.run_validation_operator(
#         "store_val_res_and_extract_eval_params",
#         assets_to_validate=[
#             (
#                 validation_operators_data_context.build_batch_kwargs(
#                     "my_datasource", "subdir_reader", "f1"
#                 ),
#                 "param_suite",
#             )
#         ],
#         evaluation_parameters={
#             "urn:great_expectations:validations:source_patient_data.default:expect_table_row_count_to_equal.result"
#             ".observed_value": 3
#         },
#     )

#     assert (
#         res["success"] is True
#     ), "No exception thrown, validation operators ran successfully"


@mock_s3
@pytest.mark.integration
def test_tuple_s3_store_backend_slash_conditions():
    bucket = "my_bucket"
    prefix = None
    conn = boto3.resource("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    client = boto3.client("s3")

    my_store = TupleS3StoreBackend(
        bucket=bucket,
        prefix=prefix,
        platform_specific_separator=False,
        filepath_prefix="foo__",
        filepath_suffix="__bar.json",
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo__/.ge_store_backend_id", "foo__/my_suite__bar.json"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo__/my_suite__bar.json"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    my_store = TupleS3StoreBackend(
        bucket=bucket,
        prefix=prefix,
        platform_specific_separator=False,
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = [".ge_store_backend_id", "my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    my_store = TupleS3StoreBackend(
        bucket=bucket,
        prefix=prefix,
        platform_specific_separator=True,
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = [".ge_store_backend_id", "my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    prefix = "/foo/"
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=True
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    prefix = "foo"
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=True
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=False
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    prefix = "foo/"
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=True
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=False
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    prefix = "/foo"
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=True
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )

    client.delete_objects(
        Bucket=bucket, Delete={"Objects": [{"Key": key} for key in expected_s3_keys]}
    )
    assert len(client.list_objects_v2(Bucket=bucket).get("Contents", [])) == 0
    my_store = TupleS3StoreBackend(
        bucket=bucket, prefix=prefix, platform_specific_separator=False
    )
    my_store.set(("my_suite",), '{"foo": "bar"}')
    expected_s3_keys = ["foo/.ge_store_backend_id", "foo/my_suite"]
    assert [
        obj["Key"] for obj in client.list_objects_v2(Bucket=bucket)["Contents"]
    ] == expected_s3_keys
    assert (
        my_store.get_url_for_key(("my_suite",))
        == "https://s3.amazonaws.com/my_bucket/foo/my_suite"
    )


@mock_s3
@pytest.mark.integration
def test_TupleS3StoreBackend_with_empty_prefixes():
    """
    What does this test test and why?

    We will exercise the store backend's set method twice and then verify
    that the we calling get and list methods will return the expected keys.

    We will also check that the objects are stored on S3 at the expected location,
    and that the correct S3 URL for the object can be retrieved.
    """
    bucket = "leakybucket"
    prefix = ""

    # create a bucket in Moto's mock AWS environment
    conn = boto3.resource("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    my_store = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )

    # We should be able to list keys, even when empty
    keys = my_store.list_keys()
    assert len(keys) == 1

    my_store.set(("AAA",), "aaa", content_type="text/html; charset=utf-8")
    assert my_store.get(("AAA",)) == "aaa"

    obj = boto3.client("s3").get_object(Bucket=bucket, Key=prefix + "my_file_AAA")
    assert my_store._build_s3_object_key(("AAA",)) == "my_file_AAA"
    assert obj["ContentType"] == "text/html; charset=utf-8"
    assert obj["ContentEncoding"] == "utf-8"

    my_store.set(("BBB",), "bbb")
    assert my_store.get(("BBB",)) == "bbb"

    assert set(my_store.list_keys()) == {("AAA",), ("BBB",), (".ge_store_backend_id",)}
    assert {
        s3_object_info["Key"]
        for s3_object_info in boto3.client("s3").list_objects_v2(
            Bucket=bucket, Prefix=prefix
        )["Contents"]
    } == {"my_file_AAA", "my_file_BBB", ".ge_store_backend_id"}

    assert (
        my_store.get_url_for_key(("AAA",))
        == "https://s3.amazonaws.com/leakybucket/my_file_AAA"
    )
    assert (
        my_store.get_url_for_key(("BBB",))
        == "https://s3.amazonaws.com/leakybucket/my_file_BBB"
    )


@mock_s3
@pytest.mark.integration
def test_TupleS3StoreBackend_with_s3_put_options():

    bucket = "leakybucket"
    conn = boto3.client("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    my_store = TupleS3StoreBackend(
        bucket=bucket,
        # Since not all out options are supported in moto, only Metadata and StorageClass is passed here.
        s3_put_options={
            "Metadata": {"test": "testMetadata"},
            "StorageClass": "REDUCED_REDUNDANCY",
        },
    )

    assert my_store.config["s3_put_options"] == {
        "Metadata": {"test": "testMetadata"},
        "StorageClass": "REDUCED_REDUNDANCY",
    }

    my_store.set(("AAA",), "aaa")

    res = conn.get_object(Bucket=bucket, Key="AAA")

    assert res["Metadata"] == {"test": "testMetadata"}
    assert res["StorageClass"] == "REDUCED_REDUNDANCY"

    assert my_store.get(("AAA",)) == "aaa"
    assert my_store.has_key(("AAA",))
    assert my_store.list_keys() == [(".ge_store_backend_id",), ("AAA",)]


@pytest.mark.skipif(
    not is_library_loadable(library_name="google.cloud"),
    reason="google is not installed",
)
@pytest.mark.skipif(
    not is_library_loadable(library_name="google"),
    reason="google is not installed",
)
@pytest.mark.integration
def test_TupleGCSStoreBackend_base_public_path():
    """
    What does this test and why?

    the base_public_path parameter allows users to point to a custom DNS when hosting Data docs.

    This test will exercise the get_url_for_key method twice to see that we are getting the expected url,
    with or without base_public_path
    """
    bucket = "leakybucket"
    prefix = "this_is_a_test_prefix"
    project = "dummy-project"
    base_public_path = "http://www.test.com/"

    with mock.patch("google.cloud.storage.Client", autospec=True) as mock_gcs_client:
        mock_client = mock_gcs_client.return_value
        mock_bucket = mock_client.get_bucket.return_value
        mock_blob = mock_bucket.blob.return_value

        my_store_with_base_public_path = TupleGCSStoreBackend(
            filepath_template=None,
            bucket=bucket,
            prefix=prefix,
            project=project,
            base_public_path=base_public_path,
        )

        my_store_with_base_public_path.set(
            ("BBB",), b"bbb", content_encoding=None, content_type="image/png"
        )

    run_id = RunIdentifier("my_run_id", datetime.datetime.utcnow())
    key = ValidationResultIdentifier(
        ExpectationSuiteIdentifier(expectation_suite_name="my_suite_name"),
        run_id,
        "my_batch_id",
    )
    run_time_string = run_id.to_tuple()[1]

    url = my_store_with_base_public_path.get_public_url_for_key(key.to_tuple())
    assert (
        url
        == "http://www.test.com/leakybucket"
        + f"/this_is_a_test_prefix/my_suite_name/my_run_id/{run_time_string}/my_batch_id"
    )


@pytest.mark.skipif(
    not is_library_loadable(library_name="google.cloud"),
    reason="google is not installed",
)
@pytest.mark.skipif(
    not is_library_loadable(library_name="google"),
    reason="google is not installed",
)
@pytest.mark.slow  # 1.35s
@pytest.mark.integration
def test_TupleGCSStoreBackend():
    # pytest.importorskip("google-cloud-storage")
    """
    What does this test test and why?

    Since no package like moto exists for GCP services, we mock the GCS client
    and assert that the store backend makes the right calls for set, get, and list.

    TODO : One option may be to have a GCS Store in Docker, which can be use to "actually" run these tests.
    """

    bucket = "leakybucket"
    prefix = "this_is_a_test_prefix"
    project = "dummy-project"
    base_public_path = "http://www.test.com/"

    with mock.patch("google.cloud.storage.Client", autospec=True) as mock_gcs_client:

        mock_client = mock_gcs_client.return_value
        mock_bucket = mock_client.bucket.return_value
        mock_blob = mock_bucket.blob.return_value

        my_store = TupleGCSStoreBackend(
            filepath_template="my_file_{0}",
            bucket=bucket,
            prefix=prefix,
            project=project,
        )

        my_store.set(("AAA",), "aaa", content_type="text/html")

        mock_gcs_client.assert_called_with("dummy-project")
        mock_client.bucket.assert_called_with("leakybucket")
        mock_bucket.blob.assert_called_with("this_is_a_test_prefix/my_file_AAA")
        # mock_bucket.blob.assert_any_call("this_is_a_test_prefix/.ge_store_backend_id")
        mock_blob.upload_from_string.assert_called_with(
            b"aaa", content_type="text/html"
        )

    with mock.patch("google.cloud.storage.Client", autospec=True) as mock_gcs_client:
        mock_client = mock_gcs_client.return_value
        mock_bucket = mock_client.bucket.return_value
        mock_blob = mock_bucket.blob.return_value

        my_store_with_no_filepath_template = TupleGCSStoreBackend(
            filepath_template=None, bucket=bucket, prefix=prefix, project=project
        )

        my_store_with_no_filepath_template.set(
            ("AAA",), b"aaa", content_encoding=None, content_type="image/png"
        )

        mock_gcs_client.assert_called_with("dummy-project")
        mock_client.bucket.assert_called_with("leakybucket")
        mock_bucket.blob.assert_called_with("this_is_a_test_prefix/AAA")
        # mock_bucket.blob.assert_any_call("this_is_a_test_prefix/.ge_store_backend_id")
        mock_blob.upload_from_string.assert_called_with(
            b"aaa", content_type="image/png"
        )

    with mock.patch("google.cloud.storage.Client", autospec=True) as mock_gcs_client:

        mock_client = mock_gcs_client.return_value
        mock_bucket = mock_client.bucket.return_value
        mock_blob = mock_bucket.get_blob.return_value
        mock_str = mock_blob.download_as_string.return_value

        my_store.get(("BBB",))

        mock_gcs_client.assert_called_once_with("dummy-project")
        mock_client.bucket.assert_called_once_with("leakybucket")
        mock_bucket.get_blob.assert_called_once_with(
            "this_is_a_test_prefix/my_file_BBB"
        )
        mock_blob.download_as_string.assert_called_once()
        mock_str.decode.assert_called_once_with("utf-8")

    with mock.patch("google.cloud.storage.Client", autospec=True) as mock_gcs_client:

        mock_client = mock_gcs_client.return_value

        my_store.list_keys()

        mock_client.list_blobs.assert_called_once_with(
            "leakybucket", prefix="this_is_a_test_prefix"
        )

        my_store.remove_key("leakybucket")

        from google.cloud.exceptions import NotFound

        try:
            mock_client.bucket.assert_called_once_with("leakybucket")
        except NotFound:
            pass

    with mock.patch("google.cloud.storage.Client", autospec=True) as mock_gcs_client:
        mock_gcs_client.side_effect = InvalidKeyError("Hi I am an InvalidKeyError")
        with pytest.raises(InvalidKeyError):
            my_store.get(("non_existent_key",))

    run_id = RunIdentifier("my_run_id", datetime.datetime.utcnow())
    key = ValidationResultIdentifier(
        ExpectationSuiteIdentifier(expectation_suite_name="my_suite_name"),
        run_id,
        "my_batch_id",
    )
    run_time_string = run_id.to_tuple()[1]

    url = my_store_with_no_filepath_template.get_url_for_key(key.to_tuple())
    assert (
        url
        == "https://storage.googleapis.com/leakybucket"
        + f"/this_is_a_test_prefix/my_suite_name/my_run_id/{run_time_string}/my_batch_id"
    )


@pytest.mark.integration
def test_TupleAzureBlobStoreBackend_connection_string():
    pytest.importorskip("azure.storage.blob")
    pytest.importorskip("azure.identity")
    """
    What does this test test and why?
    Since no package like moto exists for Azure-Blob services, we mock the Azure-blob client
    and assert that the store backend makes the right calls for set, get, and list.
    """
    connection_string = "DefaultEndpointsProtocol=https;AccountName=dummy;AccountKey=secret;EndpointSuffix=core.windows.net"
    prefix = "this_is_a_test_prefix"
    container = "dummy-container"

    my_store = TupleAzureBlobStoreBackend(
        connection_string=connection_string, prefix=prefix, container=container
    )

    with mock.patch(
        "great_expectations.compatibility.azure.BlobServiceClient", autospec=True
    ) as mock_azure_blob_client:

        mock_container_client = my_store._container_client
        mock_azure_blob_client.from_connection_string.assert_called_once()

        my_store.set(("AAA",), "aaa")
        mock_container_client.upload_blob.assert_called_once_with(
            name="this_is_a_test_prefix/AAA",
            data="aaa",
            encoding="utf-8",
            overwrite=True,
        )

        my_store.get(("BBB",))
        mock_container_client.download_blob.assert_called_once_with(
            "this_is_a_test_prefix/BBB"
        )

        my_store.list_keys()
        mock_container_client.list_blobs.assert_called_once_with(
            name_starts_with="this_is_a_test_prefix"
        )


@pytest.mark.integration
def test_TupleAzureBlobStoreBackend_account_url():
    pytest.importorskip("azure.storage.blob")
    pytest.importorskip("azure.identity")
    """
    What does this test test and why?
    Since no package like moto exists for Azure-Blob services, we mock the Azure-blob client
    and assert that the store backend makes the right calls for set, get, and list.
    """
    account_url = "this_is_a_test_account_url"
    prefix = "this_is_a_test_prefix"
    container = "dummy-container"

    my_store = TupleAzureBlobStoreBackend(
        account_url=account_url, prefix=prefix, container=container
    )

    with mock.patch(
        "great_expectations.compatibility.azure.BlobServiceClient", autospec=True
    ) as mock_azure_blob_client:
        with mock.patch(
            "great_expectations.compatibility.azure.DefaultAzureCredential",
            autospec=True,
        ) as mock_azure_credential:
            mock_container_client = my_store._container_client
            mock_azure_blob_client.assert_called_once()

            my_store.get(("BBB",))
            mock_container_client.download_blob.assert_called_once_with(
                "this_is_a_test_prefix/BBB"
            )
            mock_azure_credential.assert_called_once()


@mock_s3
@pytest.mark.slow  # 14.36s
@pytest.mark.integration
def test_TupleS3StoreBackend_list_over_1000_keys():
    """
    What does this test test and why?

    TupleS3StoreBackend.list_keys() should be able to list over 1000 keys
    which is the current limit for boto3.list_objects and boto3.list_objects_v2 methods.
    See https://boto3.amazonaws.com/v1/documentation/api/latest/guide/paginators.html

    We will create a bucket with over 1000 keys, list them with TupleS3StoreBackend.list_keys()
    and make sure all are accounted for.
    """
    bucket = "leakybucket"
    prefix = "my_prefix"

    # create a bucket in Moto's mock AWS environment
    conn = boto3.resource("s3", region_name="us-east-1")
    conn.create_bucket(Bucket=bucket)

    # Assert that the bucket is empty before creating store
    assert (
        boto3.client("s3").list_objects_v2(Bucket=bucket, Prefix=prefix).get("Contents")
        is None
    )

    my_store = TupleS3StoreBackend(
        filepath_template="my_file_{0}",
        bucket=bucket,
        prefix=prefix,
    )

    # We should be able to list keys, even when empty
    # len(keys) == 1 because of the .ge_store_backend_id
    keys = my_store.list_keys()
    assert len(keys) == 1

    # Add more than 1000 keys
    max_keys_in_a_single_call = 1000
    num_keys_to_add = int(1.2 * max_keys_in_a_single_call)

    for key_num in range(num_keys_to_add):
        my_store.set(
            (f"AAA_{key_num}",),
            f"aaa_{key_num}",
            content_type="text/html; charset=utf-8",
        )
    assert my_store.get(("AAA_0",)) == "aaa_0"
    assert my_store.get((f"AAA_{num_keys_to_add-1}",)) == f"aaa_{num_keys_to_add-1}"

    # Without pagination only list max_keys_in_a_single_call
    # This is belt and suspenders to make sure mocking s3 list_objects_v2 implements
    # the same limit as the actual s3 api
    assert (
        len(
            boto3.client("s3").list_objects_v2(Bucket=bucket, Prefix=prefix)["Contents"]
        )
        == max_keys_in_a_single_call
    )

    # With pagination list all keys
    keys = my_store.list_keys()
    # len(keys) == num_keys_to_add + 1 because of the .ge_store_backend_id
    assert len(keys) == num_keys_to_add + 1


@pytest.mark.integration
def test_InlineStoreBackend(empty_data_context: DataContext) -> None:
    inline_store_backend: InlineStoreBackend = InlineStoreBackend(
        data_context=empty_data_context,
        resource_type=DataContextVariableSchema.CONFIG_VERSION,
    )
    new_config_version: float = 5.0

    # test invalid .set
    key = DataContextVariableKey()
    tuple_ = key.to_tuple()
    with pytest.raises(StoreBackendError) as e:
        inline_store_backend.set(tuple_, "a_random_string_value")  # Invalid type

    assert "ValueError while calling _set on store backend" in str(e.value)

    # test valid .set
    key = DataContextVariableKey()
    tuple_ = key.to_tuple()
    with mock.patch(
        "great_expectations.data_context.store.InlineStoreBackend._save_changes"
    ) as mock_save:
        inline_store_backend.set(tuple_, new_config_version)

    assert empty_data_context.variables.config.config_version == new_config_version
    assert mock_save.call_count == 1

    # test .get
    key = DataContextVariableKey()
    tuple_ = key.to_tuple()
    ret = inline_store_backend.get(tuple_)
    assert ret == new_config_version

    # test .list_keys
    inline_store_backend = InlineStoreBackend(
        data_context=empty_data_context,
        resource_type=DataContextVariableSchema.ALL_VARIABLES,
    )
    assert sorted(inline_store_backend.list_keys()) == [
        ("anonymous_usage_statistics",),
        ("checkpoint_store_name",),
        ("concurrency",),
        ("config_variables_file_path",),
        ("config_version",),
        ("data_docs_sites",),
        ("datasources",),
        ("evaluation_parameter_store_name",),
        ("expectations_store_name",),
        ("fluent_datasources",),
        ("include_rendered_content",),
        ("notebooks",),
        ("plugins_directory",),
        ("progress_bars",),
        ("stores",),
        ("validations_store_name",),
    ]

    # test .move
    key1 = DataContextVariableKey()
    tuple1 = key1.to_tuple()

    key2 = DataContextVariableKey()
    tuple2 = key2.to_tuple()

    with pytest.raises(StoreBackendError) as e:
        inline_store_backend.move(tuple1, tuple2)

    assert "InlineStoreBackend does not support moving of keys" in str(e.value)

    # test invalid .remove_key
    inline_store_backend: InlineStoreBackend = InlineStoreBackend(
        data_context=empty_data_context,
        resource_type=DataContextVariableSchema.PROGRESS_BARS,
    )
    key = DataContextVariableKey(resource_name="profilers")
    tuple_ = key.to_tuple()
    with pytest.raises(StoreBackendError) as e:
        inline_store_backend.remove_key(tuple_)

    assert "Could not find a value associated with key" in str(e.value)

    key = DataContextVariableKey()
    tuple_ = key.to_tuple()
    with pytest.raises(StoreBackendError) as e:
        inline_store_backend.remove_key(tuple_)

    assert "InlineStoreBackend does not support the deletion of top level keys" in str(
        e.value
    )

    # test valid .remove_key
    inline_store_backend: InlineStoreBackend = InlineStoreBackend(
        data_context=empty_data_context,
        resource_type=DataContextVariableSchema.STORES,
    )
    store_name: str = "my_store"
    store_value: dict = {
        "class_name": "ExpectationsStore",
        "store_backend": {
            "class_name": "TupleFilesystemStoreBackend",
        },
    }
    key = DataContextVariableKey(resource_name=store_name)

    tuple_ = key.to_tuple()
    inline_store_backend.set(key=tuple_, value=store_value)
    inline_store_backend.remove_key(tuple_)


@pytest.mark.integration
def test_InlineStoreBackend_with_mocked_fs(empty_data_context: DataContext) -> None:
    path_to_great_expectations_yml: str = os.path.join(
        empty_data_context.root_directory, empty_data_context.GX_YML
    )

    # 1. Set simple string config value and confirm it persists in the GX.yml

    inline_store_backend: InlineStoreBackend = InlineStoreBackend(
        data_context=empty_data_context,
        resource_type=DataContextVariableSchema.CONFIG_VERSION,
    )

    with open(path_to_great_expectations_yml) as data:
        config_commented_map_from_yaml = yaml.load(data)

    assert config_commented_map_from_yaml["config_version"] == 3.0

    new_config_version: float = 5.0
    key = DataContextVariableKey()
    tuple_ = key.to_tuple()

    inline_store_backend.set(tuple_, new_config_version)

    with open(path_to_great_expectations_yml) as data:
        config_commented_map_from_yaml = yaml.load(data)

    assert config_commented_map_from_yaml["config_version"] == new_config_version

    # 2. Set nested dictionary config value and confirm it persists in the GX.yml

    inline_store_backend = InlineStoreBackend(
        data_context=empty_data_context,
        resource_type=DataContextVariableSchema.DATASOURCES,
    )

    with open(path_to_great_expectations_yml) as data:
        config_commented_map_from_yaml = yaml.load(data)

    assert config_commented_map_from_yaml["datasources"] == {}

    datasource_config_string: str = """
        class_name: Datasource

        execution_engine:
            class_name: PandasExecutionEngine

        data_connectors:
            my_other_data_connector:
                class_name: ConfiguredAssetFilesystemDataConnector
                base_directory: my/base/dir/
                glob_directive: "*.csv"

                default_regex:
                    pattern: (.+)\\.csv
                    group_names:
                        - name
        """
    datasource_config: dict = yaml.load(datasource_config_string)

    key = DataContextVariableKey(
        resource_name="my_datasource",
    )
    tuple_ = key.to_tuple()

    inline_store_backend.set(tuple_, datasource_config)

    with open(path_to_great_expectations_yml) as data:
        config_commented_map_from_yaml = yaml.load(data)

    datasources: dict = config_commented_map_from_yaml["datasources"]
    assert len(datasources) == 1
    assert datasources["my_datasource"] == datasource_config


@pytest.mark.unit
def test_InMemoryStoreBackend_move_overwrites_key() -> None:
    store_backend = InMemoryStoreBackend()

    key_1 = ("my_key_1",)
    key_2 = ("my_key_2",)

    store_backend.set(key_1, 123)
    assert store_backend.has_key(key_1)
    assert not store_backend.has_key(key_2)

    store_backend.move(key_1, key_2)
    assert not store_backend.has_key(key_1)
    assert store_backend.has_key(key_2)


@pytest.mark.unit
def test_InMemoryStoreBackend_move_nonexistent_key_raises_error() -> None:
    store_backend = InMemoryStoreBackend()

    with pytest.raises(KeyError):
        store_backend.move(("my_fake_key_1",), ("my_fake_key_2",))


@pytest.mark.unit
def test_InMemoryStoreBackend_config_and_defaults() -> None:
    store_backend = InMemoryStoreBackend()
    assert store_backend.config == {
        "class_name": "InMemoryStoreBackend",
        "fixed_length_key": False,
        "module_name": "great_expectations.data_context.store.in_memory_store_backend",
        "suppress_store_backend_id": False,
    }


@pytest.mark.unit
def test_InMemoryStoreBackend_build_Key() -> None:
    store_backend = InMemoryStoreBackend()
    name = "my_backend_key"
    assert store_backend.build_key(name=name) == DataContextVariableKey(
        resource_name=name
    )


@pytest.mark.unit
def test_InMemoryStoreBackend_add_success():
    store_backend = InMemoryStoreBackend()
    key = ("foo",)
    value = "bar"

    store_backend.add(key=key, value=value)
    assert key in store_backend.list_keys()


@pytest.mark.unit
def test_InMemoryStoreBackend_add_failure():
    store_backend = InMemoryStoreBackend()
    key = ("foo",)
    value = "bar"

    store_backend.add(key=key, value=value)
    with pytest.raises(StoreBackendError) as e:
        store_backend.add(key=key, value=value)

    assert "Store already has the following key" in str(e.value)


@pytest.mark.unit
def test_InMemoryStoreBackend_update_success():
    store_backend = InMemoryStoreBackend()
    key = ("foo",)
    value = "bar"
    updated_value = "baz"

    store_backend.add(key=key, value=value)
    store_backend.update(key=key, value=updated_value)

    assert store_backend.get(key) == updated_value


@pytest.mark.unit
def test_InMemoryStoreBackend_update_failure():
    store_backend = InMemoryStoreBackend()
    key = ("foo",)
    value = "bar"

    with pytest.raises(StoreBackendError) as e:
        store_backend.update(key=key, value=value)

    assert "Store does not have a value associated the following key" in str(e.value)


@pytest.mark.unit
@pytest.mark.parametrize("previous_key_exists", [True, False])
def test_InMemoryStoreBackend_add_or_update(previous_key_exists: bool):
    store_backend = InMemoryStoreBackend()
    key = ("foo",)
    value = "bar"

    if previous_key_exists:
        store_backend.add(key=key, value=None)

    store_backend.add_or_update(key=key, value=value)
    assert store_backend.get(key) == value


@pytest.mark.unit
def test_store_backend_path_special_character_escape():
    path = "/validations/default/pandas_data_asset/20230315T205136.109084Z/default_pandas_datasource-#ephemeral_pandas_asset.html"
    escaped_path = StoreBackend._url_path_escape_special_characters(path=path)
    assert (
        escaped_path
        == "/validations/default/pandas_data_asset/20230315T205136.109084Z/default_pandas_datasource-%23ephemeral_pandas_asset.html"
    )
