from __future__ import annotations

import dataclasses
import logging
import sqlite3
from pprint import pformat as pf
from typing import (
    TYPE_CHECKING,
    AbstractSet,
    Any,
    Callable,
    ClassVar,
    Generic,
    List,
    Mapping,
    MutableSequence,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
)

import pandas as pd
import pydantic
from typing_extensions import Literal

import great_expectations.exceptions as gx_exceptions
from great_expectations.compatibility import sqlalchemy
from great_expectations.compatibility.sqlalchemy import (
    sqlalchemy as sa,
)
from great_expectations.core._docs_decorators import public_api
from great_expectations.core.batch_spec import PandasBatchSpec, RuntimeDataBatchSpec
from great_expectations.datasource.fluent.constants import (
    _DATA_CONNECTOR_NAME,
    _FIELDS_ALWAYS_SET,
)
from great_expectations.datasource.fluent.dynamic_pandas import (
    _generate_pandas_data_asset_models,
)
from great_expectations.datasource.fluent.interfaces import (
    Batch,
    BatchRequest,
    DataAsset,
    Datasource,
    _DataAssetT,
)
from great_expectations.datasource.fluent.signatures import _merge_signatures
from great_expectations.datasource.fluent.sources import (
    DEFAULT_PANDAS_DATA_ASSET_NAME,
)

_EXCLUDE_TYPES_FROM_JSON: list[Type] = [sqlite3.Connection]

if sa:
    _EXCLUDE_TYPES_FROM_JSON = _EXCLUDE_TYPES_FROM_JSON + [sqlalchemy.Engine]


if TYPE_CHECKING:
    import os

    MappingIntStrAny = Mapping[Union[int, str], Any]
    AbstractSetIntStr = AbstractSet[Union[int, str]]

    from great_expectations.datasource.fluent.interfaces import (
        BatchMetadata,
    )
    from great_expectations.execution_engine import PandasExecutionEngine
    from great_expectations.validator.validator import Validator

logger = logging.getLogger(__name__)


# this enables us to include dataframe in the json schema
_PandasDataFrameT = TypeVar("_PandasDataFrameT")


class PandasDatasourceError(Exception):
    pass


class _PandasDataAsset(DataAsset):
    _EXCLUDE_FROM_READER_OPTIONS: ClassVar[Set[str]] = {
        "batch_metadata",
        "name",
        "order_by",
        "type",
        "id",
    }

    class Config:
        """
        Need to allow extra fields for the base type because pydantic will first create
        an instance of `_PandasDataAsset` before we select and create the more specific
        asset subtype.
        Each specific subtype should `forbid` extra fields.
        """

        extra = pydantic.Extra.allow

    def _get_reader_method(self) -> str:
        raise NotImplementedError(
            """One needs to explicitly provide "reader_method" for Pandas DataAsset extensions as temporary \
work-around, until "type" naming convention and method for obtaining 'reader_method' from it are established."""
        )

    def test_connection(self) -> None:
        ...

    @property
    def batch_request_options(self) -> tuple[str, ...]:
        return tuple()

    def get_batch_list_from_batch_request(
        self, batch_request: BatchRequest
    ) -> list[Batch]:
        self._validate_batch_request(batch_request)
        batch_list: List[Batch] = []

        batch_spec = PandasBatchSpec(
            reader_method=self._get_reader_method(),
            reader_options=self.dict(
                exclude=self._EXCLUDE_FROM_READER_OPTIONS,
                exclude_unset=True,
                by_alias=True,
                config_provider=self._datasource._config_provider,
            ),
        )
        execution_engine: PandasExecutionEngine = self.datasource.get_execution_engine()
        data, markers = execution_engine.get_batch_data_and_markers(
            batch_spec=batch_spec
        )

        # batch_definition (along with batch_spec and markers) is only here to satisfy a
        # legacy constraint when computing usage statistics in a validator. We hope to remove
        # it in the future.
        # imports are done inline to prevent a circular dependency with core/batch.py
        from great_expectations.core import IDDict
        from great_expectations.core.batch import BatchDefinition

        batch_definition = BatchDefinition(
            datasource_name=self.datasource.name,
            data_connector_name=_DATA_CONNECTOR_NAME,
            data_asset_name=self.name,
            batch_identifiers=IDDict(batch_request.options),
            batch_spec_passthrough=None,
        )

        batch_metadata: BatchMetadata = self._get_batch_metadata_from_batch_request(
            batch_request=batch_request
        )

        # Some pydantic annotations are postponed due to circular imports.
        # Batch.update_forward_refs() will set the annotations before we
        # instantiate the Batch class since we can import them in this scope.
        Batch.update_forward_refs()
        batch_list.append(
            Batch(
                datasource=self.datasource,
                data_asset=self,
                batch_request=batch_request,
                data=data,
                metadata=batch_metadata,
                legacy_batch_markers=markers,
                legacy_batch_spec=batch_spec,
                legacy_batch_definition=batch_definition,
            )
        )
        return batch_list

    @public_api
    def build_batch_request(self) -> BatchRequest:  # type: ignore[override]
        """A batch request that can be used to obtain batches for this DataAsset.

        Returns:
            A BatchRequest object that can be used to obtain a batch list from a Datasource by calling the
            get_batch_list_from_batch_request method.
        """
        return BatchRequest(
            datasource_name=self.datasource.name,
            data_asset_name=self.name,
            options={},
        )

    def _validate_batch_request(self, batch_request: BatchRequest) -> None:
        """Validates the batch_request has the correct form.

        Args:
            batch_request: A batch request object to be validated.
        """
        if not (
            batch_request.datasource_name == self.datasource.name
            and batch_request.data_asset_name == self.name
            and not batch_request.options
        ):
            expect_batch_request_form = BatchRequest(
                datasource_name=self.datasource.name,
                data_asset_name=self.name,
                options={},
            )
            raise gx_exceptions.InvalidBatchRequestError(
                "BatchRequest should have form:\n"
                f"{pf(dataclasses.asdict(expect_batch_request_form))}\n"
                f"but actually has form:\n{pf(dataclasses.asdict(batch_request))}\n"
            )

    def json(
        self,
        *,
        include: AbstractSetIntStr | MappingIntStrAny | None = None,
        exclude: AbstractSetIntStr | MappingIntStrAny | None = None,
        by_alias: bool = False,
        # deprecated - use exclude_unset instead
        skip_defaults: bool | None = None,
        # Default to True to prevent serializing long configs full of unset default values
        exclude_unset: bool = True,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        encoder: Callable[[Any], Any] | None = None,
        models_as_dict: bool = True,
        **dumps_kwargs: Any,
    ) -> str:
        """
        Generate a JSON representation of the model, `include` and `exclude` arguments
        as per `dict()`.

        `encoder` is an optional function to supply as `default` to json.dumps(), other
        arguments as per `json.dumps()`.

        Deviates from pydantic `exclude_unset` `True` by default instead of `False` by
        default.
        """
        exclude_fields: dict[int | str, Any] = self._include_exclude_to_dict(
            include_exclude=exclude
        )
        # don't check fields that should always be set
        check_fields: set[str] = self.__fields_set__.copy().difference(
            _FIELDS_ALWAYS_SET
        )
        for field in check_fields:
            if isinstance(getattr(self, field), tuple(_EXCLUDE_TYPES_FROM_JSON)):
                exclude_fields[field] = True

        return super().json(
            include=include,
            exclude=exclude_fields,
            by_alias=by_alias,
            skip_defaults=skip_defaults,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            encoder=encoder,
            models_as_dict=models_as_dict,
            **dumps_kwargs,
        )


_PANDAS_READER_METHOD_UNSUPPORTED_LIST = (
    # "read_csv",
    # "read_json",
    # "read_excel",
    # "read_parquet",
    # "read_clipboard",
    # "read_feather",
    "read_fwf",  # unhandled type
    # "read_gbq",
    # "read_hdf",
    # "read_html",
    # "read_orc",
    # "read_pickle",
    # "read_sas",
    # "read_spss",
    # "read_sql",
    # "read_sql_query",
    # "read_sql_table",
    # "read_stata",
    # "read_table",
    # "read_xml",
)


_PANDAS_ASSET_MODELS = _generate_pandas_data_asset_models(
    _PandasDataAsset,
    blacklist=_PANDAS_READER_METHOD_UNSUPPORTED_LIST,
    use_docstring_from_method=True,
    skip_first_param=False,
)


ClipboardAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "clipboard", _PandasDataAsset
)
CSVAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("csv", _PandasDataAsset)
ExcelAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("excel", _PandasDataAsset)
FeatherAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "feather", _PandasDataAsset
)
GBQAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("gbq", _PandasDataAsset)
HDFAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("hdf", _PandasDataAsset)
HTMLAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("html", _PandasDataAsset)
JSONAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("json", _PandasDataAsset)
ORCAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("orc", _PandasDataAsset)
ParquetAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "parquet", _PandasDataAsset
)
PickleAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "pickle", _PandasDataAsset
)
SQLAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("sql", _PandasDataAsset)
SQLQueryAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "sql_query", _PandasDataAsset
)
SQLTableAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "sql_table", _PandasDataAsset
)
SASAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("sas", _PandasDataAsset)
SPSSAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("spss", _PandasDataAsset)
StataAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("stata", _PandasDataAsset)
TableAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get("table", _PandasDataAsset)
XMLAsset: Type[_PandasDataAsset] = _PANDAS_ASSET_MODELS.get(
    "xml", _PandasDataAsset
)  # read_xml doesn't exist for pandas < 1.3


class DataFrameAsset(_PandasDataAsset, Generic[_PandasDataFrameT]):
    # instance attributes
    type: Literal["dataframe"] = "dataframe"
    dataframe: _PandasDataFrameT = pydantic.Field(..., exclude=True, repr=False)

    class Config:
        extra = pydantic.Extra.forbid

    @pydantic.validator("dataframe")
    def _validate_dataframe(cls, dataframe: pd.DataFrame) -> pd.DataFrame:
        if not isinstance(dataframe, pd.DataFrame):
            raise ValueError("dataframe must be of type pandas.DataFrame")
        return dataframe

    def _get_reader_method(self) -> str:
        raise NotImplementedError(
            """Pandas DataFrameAsset does not implement "_get_reader_method()" method, because DataFrame is already available."""
        )

    def _get_reader_options_include(self) -> set[str] | None:
        raise NotImplementedError(
            """Pandas DataFrameAsset does not implement "_get_reader_options_include()" method, because DataFrame is already available."""
        )

    def get_batch_list_from_batch_request(
        self, batch_request: BatchRequest
    ) -> list[Batch]:
        self._validate_batch_request(batch_request)

        batch_spec = RuntimeDataBatchSpec(batch_data=self.dataframe)
        execution_engine: PandasExecutionEngine = self.datasource.get_execution_engine()
        data, markers = execution_engine.get_batch_data_and_markers(
            batch_spec=batch_spec
        )

        # batch_definition (along with batch_spec and markers) is only here to satisfy a
        # legacy constraint when computing usage statistics in a validator. We hope to remove
        # it in the future.
        # imports are done inline to prevent a circular dependency with core/batch.py
        from great_expectations.core import IDDict
        from great_expectations.core.batch import BatchDefinition

        batch_definition = BatchDefinition(
            datasource_name=self.datasource.name,
            data_connector_name=_DATA_CONNECTOR_NAME,
            data_asset_name=self.name,
            batch_identifiers=IDDict(batch_request.options),
            batch_spec_passthrough=None,
        )

        batch_metadata: BatchMetadata = self._get_batch_metadata_from_batch_request(
            batch_request=batch_request
        )

        # Some pydantic annotations are postponed due to circular imports.
        # Batch.update_forward_refs() will set the annotations before we
        # instantiate the Batch class since we can import them in this scope.
        Batch.update_forward_refs()

        return [
            Batch(
                datasource=self.datasource,
                data_asset=self,
                batch_request=batch_request,
                data=data,
                metadata=batch_metadata,
                legacy_batch_markers=markers,
                legacy_batch_spec=batch_spec,
                legacy_batch_definition=batch_definition,
            )
        ]


class _PandasDatasource(Datasource, Generic[_DataAssetT]):
    # class attributes
    asset_types: ClassVar[Sequence[Type[DataAsset]]] = []

    # instance attributes
    assets: MutableSequence[_DataAssetT] = []

    # Abstract Methods
    @property
    def execution_engine_type(self) -> Type[PandasExecutionEngine]:
        """Return the PandasExecutionEngine unless the override is set"""
        from great_expectations.execution_engine.pandas_execution_engine import (
            PandasExecutionEngine,
        )

        return PandasExecutionEngine

    def test_connection(self, test_assets: bool = True) -> None:
        """Test the connection for the _PandasDatasource.

        Args:
            test_assets: If assets have been passed to the _PandasDatasource,
                         an attempt can be made to test them as well.

        Raises:
            TestConnectionError: If the connection test fails.
        """
        raise NotImplementedError(
            """One needs to implement "test_connection" on a _PandasDatasource subclass."""
        )

    # End Abstract Methods

    def json(
        self,
        *,
        include: AbstractSetIntStr | MappingIntStrAny | None = None,
        exclude: AbstractSetIntStr | MappingIntStrAny | None = None,
        by_alias: bool = False,
        # deprecated - use exclude_unset instead
        skip_defaults: bool | None = None,
        # Default to True to prevent serializing long configs full of unset default values
        exclude_unset: bool = True,
        exclude_defaults: bool = False,
        exclude_none: bool = False,
        encoder: Callable[[Any], Any] | None = None,
        models_as_dict: bool = True,
        **dumps_kwargs: Any,
    ) -> str:
        """
        Generate a JSON representation of the model, `include` and `exclude` arguments
        as per `dict()`.

        `encoder` is an optional function to supply as `default` to json.dumps(), other
        arguments as per `json.dumps()`.

        Deviates from pydantic `exclude_unset` `True` by default instead of `False` by
        default.
        """
        exclude_fields: dict[int | str, Any] = self._include_exclude_to_dict(
            include_exclude=exclude
        )
        if "assets" in self.__fields_set__:
            exclude_assets = {}
            for asset in self.assets:
                # don't check fields that should always be set
                check_fields: set[str] = asset.__fields_set__.copy().difference(
                    _FIELDS_ALWAYS_SET
                )
                for field in check_fields:
                    if isinstance(
                        getattr(asset, field), tuple(_EXCLUDE_TYPES_FROM_JSON)
                    ):
                        exclude_assets[asset.name] = {field: True}
            if exclude_assets:
                exclude_fields["assets"] = exclude_assets

        return super().json(
            include=include,
            exclude=exclude_fields,
            by_alias=by_alias,
            skip_defaults=skip_defaults,
            exclude_unset=exclude_unset,
            exclude_defaults=exclude_defaults,
            exclude_none=exclude_none,
            encoder=encoder,
            models_as_dict=models_as_dict,
            **dumps_kwargs,
        )

    def _add_asset(
        self, asset: _DataAssetT, connect_options: dict | None = None
    ) -> _DataAssetT:
        """Adds an asset to this "_PandasDatasource" object.

        The reserved asset name "DEFAULT_PANDAS_DATA_ASSET_NAME" undergoes replacement (rather than signaling error).

        Args:
            asset: The DataAsset to be added to this datasource.
        """
        asset_name: str = asset.name

        asset_names: Set[str] = self.get_asset_names()

        if asset_name == DEFAULT_PANDAS_DATA_ASSET_NAME:
            if asset_name in asset_names:
                self.delete_asset(asset_name=asset_name)

        return super()._add_asset(asset=asset, connect_options=connect_options)


_DYNAMIC_ASSET_TYPES = list(_PANDAS_ASSET_MODELS.values())


@public_api
class PandasDatasource(_PandasDatasource):
    """Adds a single-batch pandas datasource to the data context.

    Args:
        name: The name of this datasource.
        assets: An optional dictionary whose keys are Pandas DataAsset names and whose values
            are Pandas DataAsset objects.
    """

    # class attributes
    asset_types: ClassVar[Sequence[Type[DataAsset]]] = _DYNAMIC_ASSET_TYPES + [
        DataFrameAsset
    ]

    # instance attributes
    type: Literal["pandas"] = "pandas"
    assets: List[_PandasDataAsset] = []

    def test_connection(self, test_assets: bool = True) -> None:
        ...

    @staticmethod
    def _validate_asset_name(asset_name: Optional[str] = None) -> str:
        if asset_name == DEFAULT_PANDAS_DATA_ASSET_NAME:
            raise PandasDatasourceError(
                f"""An asset_name of {DEFAULT_PANDAS_DATA_ASSET_NAME} cannot be passed because it is a reserved name."""
            )
        if not asset_name:
            asset_name = DEFAULT_PANDAS_DATA_ASSET_NAME
        return asset_name

    def _get_validator(self, asset: _PandasDataAsset) -> Validator:
        batch_request: BatchRequest = asset.build_batch_request()
        return self._data_context.get_validator(batch_request=batch_request)  # type: ignore[arg-type] # got BatchRequest expected BatchRequestBase

    def add_dataframe_asset(
        self,
        name: str,
        dataframe: pd.DataFrame,
        **kwargs,
    ) -> DataFrameAsset:
        asset = DataFrameAsset(
            name=name,
            dataframe=dataframe,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_dataframe(
        self,
        dataframe: pd.DataFrame,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: DataFrameAsset = self.add_dataframe_asset(
            name=name,
            dataframe=dataframe,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_clipboard_asset(
        self,
        name: str,
        **kwargs,
    ) -> ClipboardAsset:  # type: ignore[valid-type]
        asset = ClipboardAsset(
            name=name,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_clipboard(
        self,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: ClipboardAsset = self.add_clipboard_asset(  # type: ignore[valid-type]
            name=name,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_csv_asset(
        self,
        name: str,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> CSVAsset:  # type: ignore[valid-type]
        asset = CSVAsset(
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_csv(
        self,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: CSVAsset = self.add_csv_asset(  # type: ignore[valid-type]
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_excel_asset(
        self,
        name: str,
        io: os.PathLike | str | bytes,
        **kwargs,
    ) -> ExcelAsset:  # type: ignore[valid-type]
        asset = ExcelAsset(
            name=name,
            io=io,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_excel(
        self,
        io: os.PathLike | str | bytes,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: ExcelAsset = self.add_excel_asset(  # type: ignore[valid-type]
            name=name,
            io=io,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_feather_asset(
        self,
        name: str,
        path: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> FeatherAsset:  # type: ignore[valid-type]
        asset = FeatherAsset(
            name=name,
            path=path,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_feather(
        self,
        path: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: FeatherAsset = self.add_feather_asset(  # type: ignore[valid-type]
            name=name,
            path=path,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_gbq_asset(
        self,
        name: str,
        query: str,
        **kwargs,
    ) -> GBQAsset:  # type: ignore[valid-type]
        asset = GBQAsset(
            name=name,
            query=query,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_gbq(
        self,
        query: str,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: GBQAsset = self.add_gbq_asset(  # type: ignore[valid-type]
            name=name,
            query=query,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_hdf_asset(
        self,
        name: str,
        path_or_buf: pd.HDFStore | os.PathLike | str,
        **kwargs,
    ) -> HDFAsset:  # type: ignore[valid-type]
        asset = HDFAsset(
            name=name,
            path_or_buf=path_or_buf,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_hdf(
        self,
        path_or_buf: pd.HDFStore | os.PathLike | str,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: HDFAsset = self.add_hdf_asset(  # type: ignore[valid-type]
            name=name,
            path_or_buf=path_or_buf,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_html_asset(
        self,
        name: str,
        io: os.PathLike | str,
        **kwargs,
    ) -> HTMLAsset:  # type: ignore[valid-type]
        asset = HTMLAsset(
            name=name,
            io=io,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_html(
        self,
        io: os.PathLike | str,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: HTMLAsset = self.add_html_asset(  # type: ignore[valid-type]
            name=name,
            io=io,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_json_asset(
        self,
        name: str,
        path_or_buf: pydantic.Json | pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> JSONAsset:  # type: ignore[valid-type]
        asset = JSONAsset(
            name=name,
            path_or_buf=path_or_buf,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_json(
        self,
        path_or_buf: pydantic.Json | pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: JSONAsset = self.add_json_asset(  # type: ignore[valid-type]
            name=name,
            path_or_buf=path_or_buf,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_orc_asset(
        self,
        name: str,
        path: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> ORCAsset:  # type: ignore[valid-type]
        asset = ORCAsset(
            name=name,
            path=path,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_orc(
        self,
        path: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: ORCAsset = self.add_orc_asset(  # type: ignore[valid-type]
            name=name,
            path=path,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_parquet_asset(
        self,
        name: str,
        path: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> ParquetAsset:  # type: ignore[valid-type]
        asset = ParquetAsset(
            name=name,
            path=path,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_parquet(
        self,
        path: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: ParquetAsset = self.add_parquet_asset(  # type: ignore[valid-type]
            name=name,
            path=path,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_pickle_asset(
        self,
        name: str,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> PickleAsset:  # type: ignore[valid-type]
        asset = PickleAsset(
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_pickle(
        self,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: PickleAsset = self.add_pickle_asset(  # type: ignore[valid-type]
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_sas_asset(
        self,
        name: str,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> SASAsset:  # type: ignore[valid-type]
        asset = SASAsset(
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_sas(
        self,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: SASAsset = self.add_sas_asset(  # type: ignore[valid-type]
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_spss_asset(
        self,
        name: str,
        path: pydantic.FilePath,
        **kwargs,
    ) -> SPSSAsset:  # type: ignore[valid-type]
        asset = SPSSAsset(
            name=name,
            path=path,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_spss(
        self,
        path: pydantic.FilePath,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: SPSSAsset = self.add_parquet_asset(  # type: ignore[valid-type]
            name=name,
            path=path,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_sql_asset(
        self,
        name: str,
        sql: sa.select | sa.text | str,
        con: sqlalchemy.Engine | sqlite3.Connection | str,
        **kwargs,
    ) -> SQLAsset:  # type: ignore[valid-type]
        asset = SQLAsset(
            name=name,
            sql=sql,
            con=con,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_sql(
        self,
        sql: sa.select | sa.text | str,
        con: sqlalchemy.Engine | sqlite3.Connection | str,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: SQLAsset = self.add_sql_asset(  # type: ignore[valid-type]
            name=name,
            sql=sql,
            con=con,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_sql_query_asset(
        self,
        name: str,
        sql: sa.select | sa.text | str,
        con: sqlalchemy.Engine | sqlite3.Connection | str,
        **kwargs,
    ) -> SQLQueryAsset:  # type: ignore[valid-type]
        asset = SQLQueryAsset(
            name=name,
            sql=sql,
            con=con,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_sql_query(
        self,
        sql: sa.select | sa.text | str,
        con: sqlalchemy.Engine | sqlite3.Connection | str,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: SQLQueryAsset = self.add_sql_query_asset(  # type: ignore[valid-type]
            name=name,
            sql=sql,
            con=con,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_sql_table_asset(
        self,
        name: str,
        table_name: str,
        con: sqlalchemy.Engine | str,
        **kwargs,
    ) -> SQLTableAsset:  # type: ignore[valid-type]
        asset = SQLTableAsset(
            name=name,
            table_name=table_name,
            con=con,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_sql_table(
        self,
        table_name: str,
        con: sqlalchemy.Engine | str,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: SQLTableAsset = self.add_sql_table_asset(  # type: ignore[valid-type]
            name=name,
            table_name=table_name,
            con=con,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_stata_asset(
        self,
        name: str,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> StataAsset:  # type: ignore[valid-type]
        asset = StataAsset(
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_stata(
        self,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: StataAsset = self.add_stata_asset(  # type: ignore[valid-type]
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_table_asset(
        self,
        name: str,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> TableAsset:  # type: ignore[valid-type]
        asset = TableAsset(
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_table(
        self,
        filepath_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: TableAsset = self.add_table_asset(  # type: ignore[valid-type]
            name=name,
            filepath_or_buffer=filepath_or_buffer,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    def add_xml_asset(
        self,
        name: str,
        path_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        **kwargs,
    ) -> XMLAsset:  # type: ignore[valid-type]
        asset = XMLAsset(
            name=name,
            path_or_buffer=path_or_buffer,
            **kwargs,
        )
        return self._add_asset(asset=asset)

    def read_xml(
        self,
        path_or_buffer: pydantic.FilePath | pydantic.AnyUrl,
        asset_name: Optional[str] = None,
        **kwargs,
    ) -> Validator:
        name: str = self._validate_asset_name(asset_name=asset_name)
        asset: XMLAsset = self.add_xml_asset(  # type: ignore[valid-type]
            name=name,
            path_or_buffer=path_or_buffer,
            **kwargs,
        )
        return self._get_validator(asset=asset)

    # attr-defined issue
    # https://github.com/python/mypy/issues/12472
    add_clipboard_asset.__signature__ = _merge_signatures(add_clipboard_asset, ClipboardAsset, exclude={"type"})  # type: ignore[attr-defined]
    read_clipboard.__signature__ = _merge_signatures(read_clipboard, ClipboardAsset, exclude={"type"})  # type: ignore[attr-defined]
    add_csv_asset.__signature__ = _merge_signatures(add_csv_asset, CSVAsset, exclude={"type"})  # type: ignore[attr-defined]
    read_csv.__signature__ = _merge_signatures(read_csv, CSVAsset, exclude={"type"})  # type: ignore[attr-defined]
    add_excel_asset.__signature__ = _merge_signatures(add_excel_asset, ExcelAsset, exclude={"type"})  # type: ignore[attr-defined]
    read_excel.__signature__ = _merge_signatures(read_excel, ExcelAsset, exclude={"type"})  # type: ignore[attr-defined]
    add_feather_asset.__signature__ = _merge_signatures(add_feather_asset, FeatherAsset, exclude={"type"})  # type: ignore[attr-defined]
    read_feather.__signature__ = _merge_signatures(read_feather, FeatherAsset, exclude={"type"})  # type: ignore[attr-defined]
    add_gbq_asset.__signature__ = _merge_signatures(add_gbq_asset, GBQAsset, exclude={"type"})  # type: ignore[attr-defined]
    read_gbq.__signature__ = _merge_signatures(read_gbq, GBQAsset, exclude={"type"})  # type: ignore[attr-defined]
    add_hdf_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_hdf_asset, HDFAsset, exclude={"type"}
    )
    read_hdf.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_hdf, HDFAsset, exclude={"type"}
    )
    add_html_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_html_asset, HTMLAsset, exclude={"type"}
    )
    read_html.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_html, HTMLAsset, exclude={"type"}
    )
    add_json_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_json_asset, JSONAsset, exclude={"type"}
    )
    read_json.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_json, JSONAsset, exclude={"type"}
    )
    add_orc_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_orc_asset, ORCAsset, exclude={"type"}
    )
    read_orc.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_orc, ORCAsset, exclude={"type"}
    )
    add_parquet_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_parquet_asset, ParquetAsset, exclude={"type"}
    )
    read_parquet.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_parquet, ParquetAsset, exclude={"type"}
    )
    add_pickle_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_pickle_asset, PickleAsset, exclude={"type"}
    )
    read_pickle.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_pickle, PickleAsset, exclude={"type"}
    )
    add_sas_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_sas_asset, SASAsset, exclude={"type"}
    )
    read_sas.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_sas, SASAsset, exclude={"type"}
    )
    add_spss_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_spss_asset, SPSSAsset, exclude={"type"}
    )
    read_spss.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_spss, SPSSAsset, exclude={"type"}
    )
    add_sql_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_sql_asset, SQLAsset, exclude={"type"}
    )
    read_sql.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_sql, SQLAsset, exclude={"type"}
    )
    add_sql_query_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_sql_query_asset, SQLQueryAsset, exclude={"type"}
    )
    read_sql_query.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_sql_query, SQLQueryAsset, exclude={"type"}
    )
    add_sql_table_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_sql_table_asset, SQLTableAsset, exclude={"type"}
    )
    read_sql_table.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_sql_table, SQLTableAsset, exclude={"type"}
    )
    add_stata_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_stata_asset, StataAsset, exclude={"type"}
    )
    read_stata.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_stata, StataAsset, exclude={"type"}
    )
    add_table_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_table_asset, TableAsset, exclude={"type"}
    )
    read_table.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_table, TableAsset, exclude={"type"}
    )
    add_xml_asset.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        add_xml_asset, XMLAsset, exclude={"type"}
    )
    read_xml.__signature__ = _merge_signatures(  # type: ignore[attr-defined]
        read_xml, XMLAsset, exclude={"type"}
    )
