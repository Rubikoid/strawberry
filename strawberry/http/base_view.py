import abc
import json
from typing import Any, Dict, Generic, Mapping, Optional, TypeVar, Union
from typing_extensions import Protocol

from strawberry.exceptions import MissingQueryError
from strawberry.file_uploads.utils import replace_placeholders_with_files
from strawberry.http import GraphQLHTTPResponse, GraphQLRequestData, process_result
from strawberry.schema import BaseSchema
from strawberry.schema.exceptions import InvalidOperationTypeError
from strawberry.types import ExecutionResult
from strawberry.types.graphql import OperationType


class RequestProtocol(Protocol):
    method: str
    content_type: Optional[str]
    # TODO: this is probably going to be different
    # body, content, data, etc, binary or not
    data: Union[str, bytes]
    form: Mapping[str, Union[str, bytes]]
    files: Mapping[str, Any]


Request = TypeVar("Request", bound=RequestProtocol)
Response = TypeVar("Response")
Context = TypeVar("Context")
RootValue = TypeVar("RootValue")


class HTTPException(Exception):
    def __init__(self, status_code: int, reason: str):
        self.status_code = status_code
        self.reason = reason


class BaseHTTPView(abc.ABC, Generic[Request, Response, Context, RootValue]):
    schema: BaseSchema

    def is_request_allowed(self, request: Request) -> bool:
        return request.method.lower() in ("get", "post")

    def should_render_graphiql(self, request: Request) -> bool:
        return False

    @property
    @abc.abstractmethod
    def allow_queries_via_get(self) -> bool:
        ...

    @abc.abstractmethod
    def get_sub_response(self, request: Request) -> Response:
        ...

    @abc.abstractmethod
    def get_context(self, request: Request, response: Response) -> Context:
        ...

    @abc.abstractmethod
    def get_root_value(self, request: Request) -> Optional[RootValue]:
        ...

    @abc.abstractmethod
    def render_graphiql(self, request: Request) -> Response:
        # TODO: this could be non abstract
        # maybe add a get template function?
        ...

    def execute_operation(self, request: Request, sub_response: Response) -> Any:
        try:
            request_data = self.parse_http_body(request)
        except json.decoder.JSONDecodeError as e:
            raise HTTPException(400, "Unable to parse request body as JSON") from e
            # DO this only when doing files
        except KeyError as e:
            raise HTTPException(400, "File(s) missing in form data") from e

        context = self.get_context(request, response=sub_response)
        root_value = self.get_root_value(request)

        method: str = request.method

        allowed_operation_types = OperationType.from_http(method)

        if not self.allow_queries_via_get and method == "GET":
            allowed_operation_types = allowed_operation_types - {OperationType.QUERY}

        assert self.schema

        return self.schema.execute_sync(
            request_data.query,
            root_value=root_value,
            variable_values=request_data.variables,
            context_value=context,
            operation_name=request_data.operation_name,
            allowed_operation_types=allowed_operation_types,
        )

    def parse_json(self, data: Union[str, bytes]) -> Dict[str, str]:
        try:
            return json.loads(data)
        except json.JSONDecodeError as e:
            raise HTTPException(400, "Unable to parse request body as JSON") from e

    def encode_json(self, response_data: GraphQLHTTPResponse) -> str:
        return json.dumps(response_data)

    def parse_form(self, request: Request) -> Dict[str, str]:
        operations = self.parse_json(request.form.get("operations", "{}"))
        files_map = self.parse_json(request.form.get("map", "{}"))

        # TODO: remove type ignore below

        try:
            return replace_placeholders_with_files(operations, files_map, request.files)  # type: ignore
        except KeyError:
            raise HTTPException(400, "File(s) missing in form data")

    def get_query_params(self, request: Request) -> Dict[str, str]:
        try:
            return request.GET.copy()
        except AttributeError:
            pass

        try:
            return request.args.copy()
        except AttributeError:
            return dict(request.query_params)

    def parse_http_body(self, request: Request) -> GraphQLRequestData:
        # TODO: use adapters
        try:
            content_type = request.content_type or ""
        except AttributeError:
            content_type = request.headers.get("content-type", "")

        if "application/json" in content_type:
            # TODO: use adapters

            try:
                body = request.data.decode("utf-8")
            except AttributeError:
                try:
                    body = request.body
                except AttributeError:
                    body = request.raw_body

            data = self.parse_json(body)
        elif content_type.startswith("multipart/form-data"):
            data = self.parse_form(request)
        elif request.method.lower() == "get":
            query_params = self.get_query_params(request)
            data = self.parse_query_params(query_params)
        else:
            # TODO, is this raise fine?
            raise HTTPException(400, "Unsupported content type")

        return GraphQLRequestData(
            query=data.get("query"),
            variables=data.get("variables"),
            operation_name=data.get("operationName"),
        )

    def parse_query_params(self, params: Dict[str, str]) -> Dict[str, Any]:
        if "variables" in params:
            params["variables"] = json.loads(params["variables"])

        return params

    def run(self, request: Request) -> Response:
        if not self.is_request_allowed(request):
            raise HTTPException(405, "GraphQL only supports GET and POST requests.")

        if self.should_render_graphiql(request):
            return self.render_graphiql(request)

        sub_response = self.get_sub_response(request)

        try:
            result = self.execute_operation(request=request, sub_response=sub_response)
        except InvalidOperationTypeError as e:
            raise HTTPException(400, e.as_http_error_reason(request.method)) from e
        except MissingQueryError as e:
            raise HTTPException(400, "No GraphQL query found in the request") from e

        response_data = self.process_result(request=request, result=result)

        return self._create_response(
            response_data=response_data, sub_response=sub_response
        )

    def process_result(
        self, request: Request, result: ExecutionResult
    ) -> GraphQLHTTPResponse:
        return process_result(result)
